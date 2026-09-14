"""Memória do que já foi processado, para não responder duas vezes à mesma coisa.

Sem isto o "já tratei" vive só na memória do processo: um deploy, um restart ou um
container reciclado e o laço responderia de novo a instruções antigas, em conversa
de terceiro. É o tipo de erro que aparece publicamente.

Com `EVOLUTION_DB_URL` apontando para um Postgres, o registro sobrevive a tudo isso.
Sem a variável, cai para memória, que é o comportamento antigo: serve para
desenvolvimento e para quem não quer banco.

A tabela é criada sozinha na primeira conexão.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Iterable

TABLE = "processed_triggers"
SCHEDULE_TABLE = "scheduled_messages"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    message_id  TEXT PRIMARY KEY,
    chat        TEXT,
    instruction TEXT,
    handled_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS {SCHEDULE_TABLE} (
    id          BIGSERIAL PRIMARY KEY,
    chat        TEXT NOT NULL,
    text        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'text',
    send_at     TIMESTAMPTZ NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ,
    message_id  TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS {SCHEDULE_TABLE}_due ON {SCHEDULE_TABLE} (send_at) WHERE status = 'pending'
"""
_SCHEDULE_COLS = "id, chat, text, kind, send_at, status, created_at, sent_at, message_id, error"


def _scheduled_row(row: tuple) -> dict[str, Any]:
    return dict(zip(_SCHEDULE_COLS.split(", "), row))


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Store: {message}", file=sys.stderr)


class MemoryStore:
    """Guarda em memória. Some quando o processo reinicia."""

    kind = "memoria"

    def __init__(self) -> None:
        self._ids: set[str] = set()
        self._agenda: dict[int, dict[str, Any]] = {}
        self._proximo = 1

    @property
    def available(self) -> bool:
        return True

    # ------------------------------------------------------------ agendadas

    def schedule(self, chat: str, text: str, send_at: datetime, kind: str = "text") -> int:
        sid = self._proximo
        self._proximo += 1
        self._agenda[sid] = {
            "id": sid, "chat": chat, "text": text, "kind": kind, "send_at": send_at,
            "status": "pending", "created_at": datetime.now(timezone.utc),
            "sent_at": None, "message_id": None, "error": None,
        }
        return sid

    def due(self, now: datetime, limit: int = 20) -> list[dict[str, Any]]:
        itens = [i for i in self._agenda.values() if i["status"] == "pending" and i["send_at"] <= now]
        return sorted(itens, key=lambda i: i["send_at"])[:limit]

    def pending_scheduled(self, limit: int = 50) -> list[dict[str, Any]]:
        itens = [i for i in self._agenda.values() if i["status"] == "pending"]
        return sorted(itens, key=lambda i: i["send_at"])[:limit]

    def mark_scheduled(self, sid: int, status: str, message_id: str | None = None, error: str | None = None) -> bool:
        item = self._agenda.get(int(sid))
        if item is None:
            return False
        item["status"] = status
        if status == "sent":
            item["sent_at"] = datetime.now(timezone.utc)
        item["message_id"] = message_id
        item["error"] = error
        return True

    def cancel_scheduled(self, sid: int) -> bool:
        item = self._agenda.get(int(sid))
        if item is None or item["status"] != "pending":
            return False
        item["status"] = "cancelled"
        return True

    def is_handled(self, message_id: str) -> bool:
        return message_id in self._ids

    def handled_among(self, message_ids: Iterable[str]) -> set[str]:
        return {m for m in message_ids if m in self._ids}

    def mark(self, message_id: str, chat: str | None = None, instruction: str | None = None) -> bool:
        if message_id in self._ids:
            return False
        self._ids.add(message_id)
        return True

    def count(self) -> int:
        return len(self._ids)

    def describe(self) -> dict[str, Any]:
        return {"tipo": self.kind, "tratados": self.count()}


class PostgresStore:
    """Guarda num Postgres. Sobrevive a restart e deploy."""

    kind = "postgres"

    def __init__(self, url: str):
        self.url = url
        self._pool = None
        self._falhou = False
        self._garantir()

    # -------------------------------------------------------------- conexão

    def _garantir(self) -> None:
        if self._pool is not None or self._falhou:
            return
        try:
            from psycopg_pool import ConnectionPool
        except ImportError:
            self._falhou = True
            _log('psycopg não instalado (pip install -e ".[db]"); usando memória', "WARNING")
            return
        try:
            # `open=True` conecta agora: melhor descobrir que a URL está errada
            # na subida do que na primeira mensagem que chegar.
            self._pool = ConnectionPool(self.url, min_size=1, max_size=3, open=True, timeout=10)
            with self._pool.connection() as conn:
                conn.execute(SCHEMA)
            _log(f"conectado; {self.count()} acionamentos já registrados")
        except Exception as e:
            self._falhou = True
            self._pool = None
            _log(f"não consegui conectar ({e}); caindo para memória", "ERROR")

    @property
    def available(self) -> bool:
        return self._pool is not None

    # ---------------------------------------------------------------- leitura

    def is_handled(self, message_id: str) -> bool:
        return bool(self.handled_among([message_id]))

    def handled_among(self, message_ids: Iterable[str]) -> set[str]:
        ids = [m for m in message_ids if m]
        if not ids or not self.available:
            return set()
        try:
            with self._pool.connection() as conn:
                linhas = conn.execute(
                    f"SELECT message_id FROM {TABLE} WHERE message_id = ANY(%s)", (ids,)
                ).fetchall()
            return {linha[0] for linha in linhas}
        except Exception as e:
            _log(f"falha ao consultar: {e}", "ERROR")
            # Em dúvida, dizer que NÃO foi tratado responderia de novo em público.
            # Dizer que foi apenas atrasa uma resposta. Escolho atrasar.
            return set(ids)

    def count(self) -> int:
        if not self.available:
            return 0
        try:
            with self._pool.connection() as conn:
                return conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
        except Exception:
            return 0

    # ----------------------------------------------------------------- escrita

    def mark(self, message_id: str, chat: str | None = None, instruction: str | None = None) -> bool:
        if not message_id or not self.available:
            return False
        try:
            with self._pool.connection() as conn:
                cur = conn.execute(
                    f"INSERT INTO {TABLE} (message_id, chat, instruction) VALUES (%s, %s, %s)"
                    " ON CONFLICT (message_id) DO NOTHING",
                    (message_id, chat, (instruction or "")[:500]),
                )
                return cur.rowcount > 0
        except Exception as e:
            _log(f"falha ao gravar {message_id}: {e}", "ERROR")
            return False

    def describe(self) -> dict[str, Any]:
        return {"tipo": self.kind, "conectado": self.available, "tratados": self.count()}

    # ------------------------------------------------------------ agendadas

    def schedule(self, chat: str, text: str, send_at: datetime, kind: str = "text") -> int:
        if not self.available:
            raise RuntimeError("banco indisponível")
        with self._pool.connection() as conn:
            row = conn.execute(
                f"INSERT INTO {SCHEDULE_TABLE} (chat, text, kind, send_at) VALUES (%s, %s, %s, %s) RETURNING id",
                (chat, text, kind, send_at),
            ).fetchone()
        return int(row[0])

    def due(self, now: datetime, limit: int = 20) -> list[dict[str, Any]]:
        if not self.available:
            return []
        try:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    f"SELECT {_SCHEDULE_COLS} FROM {SCHEDULE_TABLE}"
                    " WHERE status = 'pending' AND send_at <= %s ORDER BY send_at LIMIT %s",
                    (now, limit),
                ).fetchall()
            return [_scheduled_row(r) for r in rows]
        except Exception as e:
            _log(f"falha ao ler agendadas: {e}", "ERROR")
            return []

    def pending_scheduled(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.available:
            return []
        try:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    f"SELECT {_SCHEDULE_COLS} FROM {SCHEDULE_TABLE}"
                    " WHERE status = 'pending' ORDER BY send_at LIMIT %s",
                    (limit,),
                ).fetchall()
            return [_scheduled_row(r) for r in rows]
        except Exception as e:
            _log(f"falha ao listar agendadas: {e}", "ERROR")
            return []

    def mark_scheduled(self, sid: int, status: str, message_id: str | None = None, error: str | None = None) -> bool:
        if not self.available:
            return False
        try:
            with self._pool.connection() as conn:
                cur = conn.execute(
                    f"UPDATE {SCHEDULE_TABLE} SET status = %s, message_id = %s, error = %s,"
                    " sent_at = CASE WHEN %s = 'sent' THEN now() ELSE sent_at END WHERE id = %s",
                    (status, message_id, error, status, int(sid)),
                )
                return cur.rowcount > 0
        except Exception as e:
            _log(f"falha ao atualizar agendada #{sid}: {e}", "ERROR")
            return False

    def cancel_scheduled(self, sid: int) -> bool:
        if not self.available:
            return False
        try:
            with self._pool.connection() as conn:
                cur = conn.execute(
                    f"UPDATE {SCHEDULE_TABLE} SET status = 'cancelled' WHERE id = %s AND status = 'pending'",
                    (int(sid),),
                )
                return cur.rowcount > 0
        except Exception as e:
            _log(f"falha ao cancelar agendada #{sid}: {e}", "ERROR")
            return False


def build_store(url: str | None = None) -> MemoryStore | PostgresStore:
    """Postgres quando há EVOLUTION_DB_URL e ele responde; memória caso contrário."""
    url = (url if url is not None else os.environ.get("EVOLUTION_DB_URL", "")).strip()
    if not url:
        return MemoryStore()
    store = PostgresStore(url)
    return store if store.available else MemoryStore()
