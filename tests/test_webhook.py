"""Receptor de eventos da Evolution API (fase de observação do bot "IA:")."""

import json

import pytest

from evoapi_mcp.webhook import (
    PREVIEW_CHARS,
    EventLog,
    instruction_of,
    is_self_chat,
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


# ---------------------------------------------------------------------------
# fila de pendências (para a sessão em laço)
# ---------------------------------------------------------------------------

def test_pendentes_traz_so_acionamentos_nao_tratados():
    log = EventLog()
    log.add(evento("IA: um"))
    log.add(evento("bom dia"))
    log.add(evento("IA: dois", from_me=False))

    pendentes = log.pending()
    assert len(pendentes) == 1
    assert pendentes[0]["preview"] == "IA: um"


def test_marcar_tratado_tira_da_fila():
    log = EventLog()
    log.add(evento("IA: um"))
    assert len(log.pending()) == 1

    assert log.mark_handled(["MSG1"]) == 1
    assert log.pending() == []
    assert log.mark_handled(["MSG1"]) == 0   # idempotente


def test_marcar_ids_invalidos_nao_quebra():
    log = EventLog()
    assert log.mark_handled([]) == 0
    assert log.mark_handled(None) == 0
    assert log.mark_handled(["", None]) == 0


def test_pendentes_respeita_limite():
    log = EventLog()
    for i in range(8):
        ev = evento(f"IA: {i}")
        ev["data"]["key"]["id"] = f"M{i}"
        log.add(ev)
    assert len(log.pending(limit=3)) == 3
    assert log.snapshot()["pendentes"] == 8


def test_pendentes_em_ordem_de_chegada():
    log = EventLog()
    for i in range(3):
        ev = evento(f"IA: {i}")
        ev["data"]["key"]["id"] = f"M{i}"
        log.add(ev)
    assert [p["message_id"] for p in log.pending()] == ["M0", "M1", "M2"]


# ---------------------------------------------------------------------------
# conversa pessoal: lá tudo é instrução, sem prefixo
# ---------------------------------------------------------------------------

DONO = "5584999290327"
MINHA_CONVERSA = "110818863673433@lid"


def evento_pessoal(texto, from_me=True, msg_id="P1"):
    return {
        "event": "messages.upsert",
        "data": {
            "key": {"id": msg_id, "fromMe": from_me, "remoteJid": MINHA_CONVERSA,
                    "remoteJidAlt": "558499290327@s.whatsapp.net"},
            "messageType": "conversation",
            "message": {"conversation": texto},
            "messageTimestamp": TS,
        },
    }


def test_reconhece_a_conversa_pessoal_pelo_numero_alternativo():
    """O jid é opaco; o telefone só aparece em remoteJidAlt."""
    assert is_self_chat(MINHA_CONVERSA, "558499290327@s.whatsapp.net", DONO)
    assert is_self_chat(f"{DONO}@s.whatsapp.net", None, DONO)
    assert not is_self_chat("5511888887777@s.whatsapp.net", None, DONO)
    assert not is_self_chat("120363@g.us", "558499290327@s.whatsapp.net", DONO)  # grupo nunca


def test_tolera_o_nono_digito():
    """O mesmo telefone aparece ora com o nono dígito, ora sem."""
    assert is_self_chat("558499290327@s.whatsapp.net", None, "5584999290327")
    assert is_self_chat("5584999290327@s.whatsapp.net", None, "558499290327")


def test_sem_dono_configurado_nada_e_conversa_pessoal():
    assert not is_self_chat(MINHA_CONVERSA, "558499290327@s.whatsapp.net", None)
    assert not is_self_chat(MINHA_CONVERSA, "558499290327@s.whatsapp.net", "")


def test_mensagem_sem_prefixo_na_conversa_pessoal_aciona():
    r = summarize_event(evento_pessoal("Coloque no Trello o boleto de amanhã"), owner_number=DONO)
    assert r["self_chat"] is True
    assert r["trigger"] is True
    assert r["instruction"] == "Coloque no Trello o boleto de amanhã"


def test_prefixo_na_conversa_pessoal_tambem_funciona():
    r = summarize_event(evento_pessoal("IA: qual a tabela?"), owner_number=DONO)
    # ali o prefixo não é necessário, então ele faz parte da instrução
    assert r["trigger"] is True
    assert r["instruction"] == "IA: qual a tabela?"


def test_terceiro_na_conversa_pessoal_nao_aciona():
    """Improvável, mas a regra é a mesma: só o dono comanda."""
    r = summarize_event(evento_pessoal("faça isso", from_me=False), owner_number=DONO)
    assert "trigger" not in r


def test_mensagem_vazia_na_conversa_pessoal_nao_aciona():
    r = summarize_event(evento_pessoal("   "), owner_number=DONO)
    assert "trigger" not in r


def test_instrucao_longa_nao_e_cortada_como_a_previa():
    """A prévia é curta por privacidade; a instrução precisa vir inteira."""
    longa = "Arquive " + "x" * 300
    r = summarize_event(evento_pessoal(longa), owner_number=DONO)
    assert len(r["preview"]) == PREVIEW_CHARS + 1
    assert r["instruction"] == longa


def test_sem_dono_a_conversa_pessoal_volta_a_exigir_prefixo():
    r = summarize_event(evento_pessoal("Coloque no Trello"), owner_number=None)
    assert "trigger" not in r


def test_resposta_do_assistente_nao_vira_nova_instrucao():
    """Na conversa pessoal a resposta do assistente também é `fromMe`: sem marcar,
    ela voltaria como pedido e o laço não pararia mais."""
    from evoapi_mcp.store import MemoryStore

    store = MemoryStore()
    log = EventLog(store=store, owner_number=DONO)

    store.mark("RESP1", instruction="[enviada pelo assistente]")   # o que _enviado() faz
    log.add(evento_pessoal("Pronto, criei o cartão no Trello", msg_id="RESP1"))

    assert log.pending() == []


# ---------------------------------------------------------------------------
# "checked": reação na instrução tratada
# ---------------------------------------------------------------------------

def test_trigger_devolve_so_acionamentos():
    log = EventLog(owner_number=DONO)
    log.add(evento("IA: resuma"))                 # acionamento (id MSG1)
    log.add(evento("bom dia"))                    # não é
    assert log.trigger("MSG1")["instruction"] == "resuma"
    assert log.trigger("inexistente") is None
    assert log.trigger(None) is None


def test_react_done_marca_a_instrucao(client):
    client.responses.append({})
    assert client.react_done("120363@g.us", "MSG1") is True
    chamada = client.calls[-1]
    assert chamada["endpoint"] == "/message/sendReaction/{instanceId}"
    assert chamada["data"]["reaction"] == "✅"
    assert chamada["data"]["key"] == {"remoteJid": "120363@g.us", "fromMe": True, "id": "MSG1"}


def test_react_done_desligado_por_emoji_vazio(client):
    client.config.done_reaction = ""
    assert client.react_done("120363@g.us", "MSG1") is False
    assert client.calls == []


def test_react_done_nunca_derruba_o_tratamento(client):
    def falha(data):
        raise RuntimeError("Evolution fora do ar")
    client.responses.append(falha)
    assert client.react_done("120363@g.us", "MSG1") is False   # avisa e segue
