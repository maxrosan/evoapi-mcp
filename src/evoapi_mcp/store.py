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
from typing import Any, Iterable

TABLE = "processed_triggers"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    message_id  TEXT PRIMARY KEY,
    chat        TEXT,
    instruction TEXT,
    handled_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Store: {message}", file=sys.stderr)


class MemoryStore:
    """Guarda em memória. Some quando o processo reinicia."""

    kind = "memoria"

    def __init__(self) -> None:
        self._ids: set[str] = set()

    @property
    def available(self) -> bool:
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


def build_store(url: str | None = None) -> MemoryStore | PostgresStore:
    """Postgres quando há EVOLUTION_DB_URL e ele responde; memória caso contrário."""
    url = (url if url is not None else os.environ.get("EVOLUTION_DB_URL", "")).strip()
    if not url:
        return MemoryStore()
    store = PostgresStore(url)
    return store if store.available else MemoryStore()
