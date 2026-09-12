"""Registro do que já foi tratado, para não responder duas vezes."""

import pytest

from evoapi_mcp.store import MemoryStore, PostgresStore, build_store
from evoapi_mcp.webhook import EventLog


# ---------------------------------------------------------------------------
# memória
# ---------------------------------------------------------------------------

def test_memoria_marca_e_reconhece():
    s = MemoryStore()
    assert not s.is_handled("A")
    assert s.mark("A", chat="1@x", instruction="resuma") is True
    assert s.is_handled("A")
    assert s.mark("A") is False           # idempotente
    assert s.count() == 1


def test_memoria_consulta_em_lote():
    s = MemoryStore()
    s.mark("A")
    s.mark("C")
    assert s.handled_among(["A", "B", "C"]) == {"A", "C"}
    assert s.handled_among([]) == set()


def test_memoria_descreve():
    s = MemoryStore()
    s.mark("A")
    assert s.describe() == {"tipo": "memoria", "tratados": 1}


# ---------------------------------------------------------------------------
# escolha do armazenamento
# ---------------------------------------------------------------------------

def test_sem_url_usa_memoria(monkeypatch):
    monkeypatch.delenv("EVOLUTION_DB_URL", raising=False)
    assert isinstance(build_store(), MemoryStore)
    assert isinstance(build_store(""), MemoryStore)


def test_url_ruim_cai_para_memoria():
    """Banco fora do ar não pode derrubar o servidor; degrada para memória."""
    store = build_store("postgresql://ninguem:nada@127.0.0.1:1/inexistente")
    assert isinstance(store, MemoryStore)


def test_postgres_indisponivel_nao_explode():
    p = PostgresStore("postgresql://ninguem:nada@127.0.0.1:1/inexistente")
    assert p.available is False
    assert p.is_handled("A") is False
    assert p.mark("A") is False
    assert p.count() == 0
    assert p.describe()["conectado"] is False


# ---------------------------------------------------------------------------
# integração com o registro de eventos
# ---------------------------------------------------------------------------

TS = 1789170336


def evento(texto, msg_id="M1", from_me=True):
    return {
        "event": "messages.upsert",
        "data": {
            "key": {"id": msg_id, "fromMe": from_me, "remoteJid": "120363@g.us"},
            "messageType": "conversation",
            "message": {"conversation": texto},
            "messageTimestamp": TS,
        },
    }


def test_marcar_persiste_no_armazenamento():
    store = MemoryStore()
    log = EventLog(store=store)
    log.add(evento("IA: resuma"))

    assert len(log.pending()) == 1
    log.mark_handled(["M1"])
    assert log.pending() == []
    assert store.is_handled("M1")
    assert log.is_handled("M1")


def test_registro_sobrevive_a_um_novo_processo():
    """O ponto da persistência: outro EventLog, mesmo armazenamento, nada se repete."""
    store = MemoryStore()
    antes = EventLog(store=store)
    antes.add(evento("IA: resuma"))
    antes.mark_handled(["M1"])

    depois = EventLog(store=store)        # como se o container tivesse reiniciado
    depois.add(evento("IA: resuma"))      # o evento chega de novo
    assert depois.pending() == []         # e mesmo assim não vira pendência


def test_marcacao_guarda_conversa_e_instrucao():
    store = MemoryStore()
    log = EventLog(store=store)
    log.add(evento("IA: resuma o combinado"))

    gravado = {}
    store.mark = lambda mid, chat=None, instruction=None: gravado.update(
        id=mid, chat=chat, instrucao=instruction) or True
    log.mark_handled(["M1"])

    assert gravado["id"] == "M1"
    assert gravado["chat"] == "120363@g.us"
    assert "resuma o combinado" in gravado["instrucao"]


def test_snapshot_reporta_o_armazenamento():
    log = EventLog(store=MemoryStore())
    assert log.snapshot()["armazenamento"]["tipo"] == "memoria"


def test_falha_de_consulta_prefere_nao_repetir(monkeypatch):
    """Na dúvida, tratar como já respondido: repetir em público é pior que atrasar."""
    p = PostgresStore.__new__(PostgresStore)
    p._pool = object()
    p._falhou = False

    class PoolQuebrado:
        def connection(self):
            raise RuntimeError("conexão caiu")

    p._pool = PoolQuebrado()
    assert p.handled_among(["A", "B"]) == {"A", "B"}
