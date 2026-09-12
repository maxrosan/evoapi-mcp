"""Receptor de eventos da Evolution API (fase de observação do bot "IA:")."""

import json

import pytest

from evoapi_mcp.webhook import (
    PREVIEW_CHARS,
    EventLog,
    instruction_of,
    is_trigger,
    summarize_event,
)

TS = 1789170336


def evento(texto=None, from_me=True, jid="5511999999999@s.whatsapp.net", tipo="conversation", extra=None):
    mensagem = {"conversation": texto} if texto is not None else {"imageMessage": {"mimetype": "image/jpeg"}}
    dados = {
        "key": {"id": "MSG1", "fromMe": from_me, "remoteJid": jid},
        "pushName": "Max",
        "messageType": tipo,
        "message": mensagem,
        "messageTimestamp": TS,
    }
    if extra:
        dados.update(extra)
    return {"event": "messages.upsert", "instance": "Max 1", "data": dados}


# ---------------------------------------------------------------------------
# reconhecimento do prefixo
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("texto", ["IA: resuma", "ia: resuma", "Ia: resuma", "  IA: resuma", "IA:resuma"])
def test_prefixo_reconhecido(texto):
    assert is_trigger(texto)


@pytest.mark.parametrize("texto", [None, "", "bom dia", "IA vai chover?", "falar com IA: depois", "IAM:"])
def test_prefixo_nao_reconhecido(texto):
    assert not is_trigger(texto)


def test_instrucao_extraida():
    assert instruction_of("IA: resuma a conversa") == "resuma a conversa"
    assert instruction_of("  ia:   responda isso  ") == "responda isso"
    assert instruction_of("IA:") is None          # sem instrução
    assert instruction_of("bom dia") is None      # não é acionamento


# ---------------------------------------------------------------------------
# resumo do evento
# ---------------------------------------------------------------------------

def test_minha_mensagem_com_acionamento():
    r = summarize_event(evento("IA: resuma a conversa"))
    assert r["event"] == "messages.upsert"
    assert r["from_me"] is True
    assert r["trigger"] is True
    assert r["chat_type"] == "direto"
    assert r["chat"] == "5511999999999"
    assert r["preview"] == "IA: resuma a conversa"
    assert r["message_id"] == "MSG1"


def test_mensagem_de_terceiro_nao_aciona_mesmo_com_prefixo():
    """Só o dono da instância aciona. Esta é a regra de segurança principal."""
    r = summarize_event(evento("IA: apague tudo", from_me=False))
    assert r["from_me"] is False
    assert "trigger" not in r


def test_minha_mensagem_comum_nao_aciona():
    r = summarize_event(evento("bom dia"))
    assert r["from_me"] is True
    assert "trigger" not in r


def test_grupo_identificado():
    r = summarize_event(evento("IA: resuma", jid="120363000@g.us"))
    assert r["chat_type"] == "grupo"
    assert r["trigger"] is True


def test_preview_e_cortado():
    r = summarize_event(evento("IA: " + "x" * 300))
    assert len(r["preview"]) == PREVIEW_CHARS + 1  # corte + reticência
    assert r["preview"].endswith("…")


def test_evento_sem_dados():
    r = summarize_event({"event": "connection.update", "instance": "Max 1"})
    assert r["event"] == "connection.update"
    assert "from_me" not in r


def test_corpo_invalido():
    assert summarize_event("isso não é um objeto")["erro"]
    assert summarize_event(None)["erro"]


def test_midia_nao_vaza_conteudo():
    r = summarize_event(evento(texto=None, tipo="imageMessage"))
    assert r["type"] == "image"
    assert "preview" not in r
    assert "base64" not in json.dumps(r)


# ---------------------------------------------------------------------------
# histórico em memória
# ---------------------------------------------------------------------------

def test_log_conta_e_filtra():
    log = EventLog()
    log.add(evento("IA: um"))
    log.add(evento("bom dia"))
    log.add(evento("IA: dois", from_me=False))
    log.add({"event": "connection.update"})

    resumo = log.snapshot()
    assert resumo["total_recebido"] == 4
    assert resumo["minhas_mensagens"] == 2
    assert resumo["acionamentos"] == 1
    assert resumo["por_evento"]["messages.upsert"] == 3
    assert resumo["por_evento"]["connection.update"] == 1

    assert len(log.snapshot(only_mine=True)["eventos"]) == 2
    acionamentos = log.snapshot(only_triggers=True)["eventos"]
    assert len(acionamentos) == 1
    assert acionamentos[0]["preview"] == "IA: um"


def test_log_descarta_os_mais_antigos():
    log = EventLog(maxlen=3)
    for i in range(10):
        log.add(evento(f"msg {i}"))
    resumo = log.snapshot()
    assert resumo["total_recebido"] == 10
    assert resumo["em_memoria"] == 3
    assert resumo["eventos"][-1]["preview"] == "msg 9"


def test_log_respeita_limite_de_leitura():
    log = EventLog()
    for i in range(30):
        log.add(evento(f"msg {i}"))
    assert len(log.snapshot(limit=5)["eventos"]) == 5
