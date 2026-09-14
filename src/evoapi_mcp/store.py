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
MEMORY_TABLE = "memories"
INDEX_TABLE = "indexed_files"
CHUNK_TABLE = "indexed_chunks"
CONVERSATION_TABLE = "conversations"
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
CREATE INDEX IF NOT EXISTS {SCHEDULE_TABLE}_due ON {SCHEDULE_TABLE} (send_at) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS {MEMORY_TABLE} (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    source      TEXT,
    chat        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding   REAL[] NOT NULL
);
CREATE TABLE IF NOT EXISTS {INDEX_TABLE} (
    id               BIGSERIAL PRIMARY KEY,
    sha256           TEXT UNIQUE NOT NULL,
    kind             TEXT NOT NULL,
    name             TEXT,
    mime             TEXT,
    size             BIGINT,
    message_id       TEXT,
    chat             TEXT,
    source_url       TEXT,
    drive_id         TEXT,
    drive_link       TEXT,
    drive_folder     TEXT,
    drive_owned      BOOLEAN NOT NULL DEFAULT false,
    company          TEXT,
    category         TEXT,
    doc_date         DATE,
    emitter          TEXT,
    amount           DOUBLE PRECISION,
    note             TEXT,
    text             TEXT,
    text_chars       INTEGER,
    pages            INTEGER,
    ocr              BOOLEAN NOT NULL DEFAULT false,
    visual_embedding REAL[],
    status           TEXT NOT NULL DEFAULT 'processing',
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_at       TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS {CHUNK_TABLE} (
    id          BIGSERIAL PRIMARY KEY,
    file_id     BIGINT NOT NULL REFERENCES {INDEX_TABLE}(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    embedding   REAL[] NOT NULL
);
CREATE INDEX IF NOT EXISTS {CHUNK_TABLE}_file ON {CHUNK_TABLE} (file_id);
CREATE TABLE IF NOT EXISTS {CONVERSATION_TABLE} (
    id          BIGSERIAL PRIMARY KEY,
    trigger_id  TEXT UNIQUE NOT NULL,
    chat        TEXT,
    request     TEXT NOT NULL,
    response    TEXT,
    voice       BOOLEAN NOT NULL DEFAULT false,
    quoted      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    handled_at  TIMESTAMPTZ,
    embedding   REAL[]
)
"""
_INDEX_COLS = (
    "sha256", "kind", "name", "mime", "size", "message_id", "chat", "source_url", "drive_id",
    "drive_link", "drive_folder", "drive_owned", "company", "category", "doc_date", "emitter",
    "amount", "note", "text", "text_chars", "pages", "ocr", "visual_embedding", "status", "error",
    "indexed_at",
)
_INDEX_META = [c for c in _INDEX_COLS if c not in ("text", "visual_embedding")]


def _rows(cur: Any) -> list[dict[str, Any]]:
    colunas = [d.name for d in cur.description]
    return [dict(zip(colunas, r)) for r in cur.fetchall()]
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
        self._memorias: dict[int, dict[str, Any]] = {}
        self._proxima_memoria = 1
        self._indexados: dict[int, dict[str, Any]] = {}
        self._trechos: dict[int, list[dict[str, Any]]] = {}
        self._conversas: dict[int, dict[str, Any]] = {}
        self._seq = {"indice": 1, "trecho": 1, "conversa": 1}

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

    # ------------------------------------------------------------ memória

    def add_memory(self, kind: str, text: str, source: str | None, chat: str | None,
                   embedding: list[float]) -> int:
        mid = self._proxima_memoria
        self._proxima_memoria += 1
        self._memorias[mid] = {
            "id": mid, "kind": kind, "text": text, "source": source, "chat": chat,
            "created_at": datetime.now(timezone.utc), "embedding": list(embedding),
        }
        return mid

    def load_memories(self) -> list[dict[str, Any]]:
        return [dict(m) for m in sorted(self._memorias.values(), key=lambda m: m["id"])]

    def delete_memory(self, mid: int) -> bool:
        return self._memorias.pop(int(mid), None) is not None

    def count_memories(self) -> int:
        return len(self._memorias)

    # ------------------------------------------------------------ índice de documentos

    def _novo_id(self, nome: str) -> int:
        valor = self._seq[nome]
        self._seq[nome] += 1
        return valor

    def add_indexed_file(self, fields: dict[str, Any]) -> int:
        if any(a["sha256"] == fields.get("sha256") for a in self._indexados.values()):
            raise ValueError("sha256 já indexado")
        fid = self._novo_id("indice")
        linha = {c: None for c in _INDEX_COLS}
        linha.update({"drive_owned": False, "ocr": False, "status": "processing"})
        linha.update({k: v for k, v in fields.items() if k in _INDEX_COLS})
        linha.update({"id": fid, "created_at": datetime.now(timezone.utc)})
        self._indexados[fid] = linha
        return fid

    def update_indexed_file(self, fid: int, **fields: Any) -> bool:
        linha = self._indexados.get(int(fid))
        if linha is None:
            return False
        linha.update({k: v for k, v in fields.items() if k in _INDEX_COLS})
        return True

    def get_indexed_file(self, fid: int) -> dict[str, Any] | None:
        linha = self._indexados.get(int(fid))
        return dict(linha) if linha else None

    def get_indexed_file_by_sha(self, sha: str) -> dict[str, Any] | None:
        for linha in self._indexados.values():
            if linha["sha256"] == sha:
                return dict(linha)
        return None

    def load_indexed_files(self) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in linha.items() if k not in ("text", "visual_embedding")}
            for linha in self._indexados.values()
        ]

    def load_visual_embeddings(self) -> list[dict[str, Any]]:
        return [
            {"id": linha["id"], "embedding": list(linha["visual_embedding"])}
            for linha in self._indexados.values()
            if linha.get("visual_embedding") is not None and linha.get("status") == "done"
        ]

    def replace_chunks(self, fid: int, chunks: list[tuple[int, str, list[float]]]) -> None:
        self._trechos[int(fid)] = [
            {"id": self._novo_id("trecho"), "file_id": int(fid), "seq": seq, "text": texto, "embedding": list(vetor)}
            for seq, texto, vetor in chunks
        ]

    def load_chunks(self) -> list[dict[str, Any]]:
        return [dict(t) for lista in self._trechos.values() for t in lista]

    def delete_indexed_file(self, fid: int) -> bool:
        self._trechos.pop(int(fid), None)
        return self._indexados.pop(int(fid), None) is not None

    def count_indexed_files(self) -> int:
        return len(self._indexados)

    # ------------------------------------------------------------ histórico de conversas

    def add_conversation(self, trigger_id: str, chat: str | None, request: str, voice: bool,
                         quoted: str | None) -> int | None:
        if any(c["trigger_id"] == trigger_id for c in self._conversas.values()):
            return None
        cid = self._novo_id("conversa")
        self._conversas[cid] = {
            "id": cid, "trigger_id": trigger_id, "chat": chat, "request": request, "response": None,
            "voice": bool(voice), "quoted": quoted, "created_at": datetime.now(timezone.utc),
            "handled_at": None, "embedding": None,
        }
        return cid

    def append_conversation_response(self, cid: int, text: str) -> bool:
        linha = self._conversas.get(int(cid))
        if linha is None:
            return False
        linha["response"] = f"{linha['response']}\n{text}" if linha.get("response") else text
        return True

    def finish_conversation(self, cid: int, embedding: list[float]) -> bool:
        linha = self._conversas.get(int(cid))
        if linha is None:
            return False
        linha["embedding"] = list(embedding)
        linha["handled_at"] = datetime.now(timezone.utc)
        return True

    def get_conversation_by_trigger(self, trigger_id: str) -> dict[str, Any] | None:
        for linha in self._conversas.values():
            if linha["trigger_id"] == trigger_id:
                return dict(linha)
        return None

    def load_conversations(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self._conversas.values() if c.get("embedding") is not None]

    def count_conversations(self) -> int:
        return len(self._conversas)

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

    # ------------------------------------------------------------ memória

    def add_memory(self, kind: str, text: str, source: str | None, chat: str | None,
                   embedding: list[float]) -> int:
        if not self.available:
            raise RuntimeError("banco indisponível")
        with self._pool.connection() as conn:
            row = conn.execute(
                f"INSERT INTO {MEMORY_TABLE} (kind, text, source, chat, embedding)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (kind, text, source, chat, embedding),
            ).fetchone()
        return int(row[0])

    def load_memories(self) -> list[dict[str, Any]]:
        if not self.available:
            return []
        try:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    f"SELECT id, kind, text, source, chat, created_at, embedding FROM {MEMORY_TABLE} ORDER BY id"
                ).fetchall()
            chaves = ("id", "kind", "text", "source", "chat", "created_at", "embedding")
            return [dict(zip(chaves, r)) for r in rows]
        except Exception as e:
            _log(f"falha ao ler memórias: {e}", "ERROR")
            return []

    def delete_memory(self, mid: int) -> bool:
        if not self.available:
            return False
        try:
            with self._pool.connection() as conn:
                cur = conn.execute(f"DELETE FROM {MEMORY_TABLE} WHERE id = %s", (int(mid),))
                return cur.rowcount > 0
        except Exception as e:
            _log(f"falha ao apagar memória #{mid}: {e}", "ERROR")
            return False

    def count_memories(self) -> int:
        if not self.available:
            return 0
        try:
            with self._pool.connection() as conn:
                return conn.execute(f"SELECT count(*) FROM {MEMORY_TABLE}").fetchone()[0]
        except Exception:
            return 0

    # ------------------------------------------------------------ índice de documentos

    def _conta(self, tabela: str) -> int:
        if not self.available:
            return 0
        try:
            with self._pool.connection() as conn:
                return conn.execute(f"SELECT count(*) FROM {tabela}").fetchone()[0]
        except Exception:
            return 0

    def add_indexed_file(self, fields: dict[str, Any]) -> int:
        if not self.available:
            raise RuntimeError("banco indisponível")
        colunas = [c for c in fields if c in _INDEX_COLS]
        with self._pool.connection() as conn:
            linha = conn.execute(
                f"INSERT INTO {INDEX_TABLE} ({', '.join(colunas)}) VALUES ({', '.join(['%s'] * len(colunas))})"
                " RETURNING id",
                [fields[c] for c in colunas],
            ).fetchone()
        return int(linha[0])

    def update_indexed_file(self, fid: int, **fields: Any) -> bool:
        colunas = [c for c in fields if c in _INDEX_COLS and c != "sha256"]
        if not colunas or not self.available:
            return False
        with self._pool.connection() as conn:
            cur = conn.execute(
                f"UPDATE {INDEX_TABLE} SET {', '.join(f'{c} = %s' for c in colunas)} WHERE id = %s",
                [fields[c] for c in colunas] + [int(fid)],
            )
            return cur.rowcount > 0

    def _um_indexado(self, onde: str, valor: Any) -> dict[str, Any] | None:
        if not self.available:
            return None
        with self._pool.connection() as conn:
            linhas = _rows(conn.execute(f"SELECT * FROM {INDEX_TABLE} WHERE {onde} = %s", (valor,)))
        return linhas[0] if linhas else None

    def get_indexed_file(self, fid: int) -> dict[str, Any] | None:
        return self._um_indexado("id", int(fid))

    def get_indexed_file_by_sha(self, sha: str) -> dict[str, Any] | None:
        return self._um_indexado("sha256", sha)

    def load_indexed_files(self) -> list[dict[str, Any]]:
        if not self.available:
            return []
        try:
            with self._pool.connection() as conn:
                return _rows(conn.execute(f"SELECT id, created_at, {', '.join(_INDEX_META)} FROM {INDEX_TABLE}"))
        except Exception as e:
            _log(f"falha ao ler índice: {e}", "ERROR")
            return []

    def load_visual_embeddings(self) -> list[dict[str, Any]]:
        if not self.available:
            return []
        try:
            with self._pool.connection() as conn:
                return _rows(conn.execute(
                    f"SELECT id, visual_embedding AS embedding FROM {INDEX_TABLE}"
                    " WHERE visual_embedding IS NOT NULL AND status = 'done'"
                ))
        except Exception as e:
            _log(f"falha ao ler vetores visuais: {e}", "ERROR")
            return []

    def replace_chunks(self, fid: int, chunks: list[tuple[int, str, list[float]]]) -> None:
        if not self.available:
            raise RuntimeError("banco indisponível")
        with self._pool.connection() as conn:
            conn.execute(f"DELETE FROM {CHUNK_TABLE} WHERE file_id = %s", (int(fid),))
            if chunks:
                with conn.cursor() as cur:
                    cur.executemany(
                        f"INSERT INTO {CHUNK_TABLE} (file_id, seq, text, embedding) VALUES (%s, %s, %s, %s)",
                        [(int(fid), seq, texto, vetor) for seq, texto, vetor in chunks],
                    )

    def load_chunks(self) -> list[dict[str, Any]]:
        if not self.available:
            return []
        try:
            with self._pool.connection() as conn:
                return _rows(conn.execute(f"SELECT id, file_id, seq, text, embedding FROM {CHUNK_TABLE}"))
        except Exception as e:
            _log(f"falha ao ler trechos: {e}", "ERROR")
            return []

    def delete_indexed_file(self, fid: int) -> bool:
        if not self.available:
            return False
        with self._pool.connection() as conn:
            return conn.execute(f"DELETE FROM {INDEX_TABLE} WHERE id = %s", (int(fid),)).rowcount > 0

    def count_indexed_files(self) -> int:
        return self._conta(INDEX_TABLE)

    # ------------------------------------------------------------ histórico de conversas

    def add_conversation(self, trigger_id: str, chat: str | None, request: str, voice: bool,
                         quoted: str | None) -> int | None:
        if not self.available:
            return None
        try:
            with self._pool.connection() as conn:
                linha = conn.execute(
                    f"INSERT INTO {CONVERSATION_TABLE} (trigger_id, chat, request, voice, quoted)"
                    " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (trigger_id) DO NOTHING RETURNING id",
                    (trigger_id, chat, request, bool(voice), quoted),
                ).fetchone()
            return int(linha[0]) if linha else None
        except Exception as e:
            _log(f"falha ao gravar pedido {trigger_id}: {e}", "ERROR")
            return None

    def append_conversation_response(self, cid: int, text: str) -> bool:
        if not self.available:
            return False
        try:
            with self._pool.connection() as conn:
                return conn.execute(
                    f"UPDATE {CONVERSATION_TABLE} SET response = CASE WHEN response IS NULL OR response = ''"
                    " THEN %s ELSE response || E'\\n' || %s END WHERE id = %s",
                    (text, text, int(cid)),
                ).rowcount > 0
        except Exception as e:
            _log(f"falha ao gravar resposta #{cid}: {e}", "ERROR")
            return False

    def finish_conversation(self, cid: int, embedding: list[float]) -> bool:
        if not self.available:
            return False
        try:
            with self._pool.connection() as conn:
                return conn.execute(
                    f"UPDATE {CONVERSATION_TABLE} SET embedding = %s, handled_at = now() WHERE id = %s",
                    (embedding, int(cid)),
                ).rowcount > 0
        except Exception as e:
            _log(f"falha ao fechar conversa #{cid}: {e}", "ERROR")
            return False

    def get_conversation_by_trigger(self, trigger_id: str) -> dict[str, Any] | None:
        if not self.available:
            return None
        with self._pool.connection() as conn:
            linhas = _rows(conn.execute(f"SELECT * FROM {CONVERSATION_TABLE} WHERE trigger_id = %s", (trigger_id,)))
        return linhas[0] if linhas else None

    def load_conversations(self) -> list[dict[str, Any]]:
        if not self.available:
            return []
        try:
            with self._pool.connection() as conn:
                return _rows(conn.execute(
                    f"SELECT id, trigger_id, chat, request, response, voice, quoted, created_at, handled_at, embedding"
                    f" FROM {CONVERSATION_TABLE} WHERE embedding IS NOT NULL"
                ))
        except Exception as e:
            _log(f"falha ao ler conversas: {e}", "ERROR")
            return []

    def count_conversations(self) -> int:
        return self._conta(CONVERSATION_TABLE)


def build_store(url: str | None = None) -> MemoryStore | PostgresStore:
    """Postgres quando há EVOLUTION_DB_URL e ele responde; memória caso contrário."""
    url = (url if url is not None else os.environ.get("EVOLUTION_DB_URL", "")).strip()
    if not url:
        return MemoryStore()
    store = PostgresStore(url)
    return store if store.available else MemoryStore()
