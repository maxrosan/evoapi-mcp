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


# ---------------------------------------------------------------------------
# resposta com citação a uma mensagem do assistente
# ---------------------------------------------------------------------------

KEILLA = "558498140038@s.whatsapp.net"


def evento_citando(texto, citada_id, citada_texto="Pode ser um vídeo qualquer?", from_me=True, msg_id="R1"):
    e = evento(texto, from_me=from_me, jid=KEILLA)
    e["data"]["key"]["id"] = msg_id
    e["data"]["message"] = {"extendedTextMessage": {"text": texto, "contextInfo": {
        "stanzaId": citada_id, "participant": "5584999290327@s.whatsapp.net",
        "quotedMessage": {"conversation": citada_texto},
    }}}
    return e


def test_citacao_de_mensagem_do_assistente_aciona_sem_prefixo():
    log = EventLog(owner_number="5584999290327")
    log.store.mark("Q1", instruction="[enviada pelo assistente]")
    r = log.add(evento_citando("um vídeo pronto do YouTube sobre café", "Q1"))
    assert r["trigger"] is True
    assert r["instruction"] == "um vídeo pronto do YouTube sobre café"
    assert r["reply_to"] == {"id": "Q1", "text": "Pode ser um vídeo qualquer?", "type": "text"}
    assert log.pending()[0]["message_id"] == "R1"


def test_citacao_de_mensagem_desconhecida_nao_aciona():
    log = EventLog(owner_number="5584999290327")
    r = log.add(evento_citando("um vídeo pronto do YouTube sobre café", "DESCONHECIDA"))
    assert r.get("trigger") is None
    assert r["reply_to"]["id"] == "DESCONHECIDA"   # a citação é guardada; só não aciona


def test_terceiro_citando_o_assistente_nao_aciona():
    log = EventLog(owner_number="5584999290327")
    log.store.mark("Q1", instruction="[enviada pelo assistente]")
    r = log.add(evento_citando("pode sim", "Q1", from_me=False))
    assert r.get("trigger") is None


def test_prefixo_com_citacao_carrega_o_contexto():
    log = EventLog(owner_number="5584999290327")
    log.store.mark("Q1", instruction="[enviada pelo assistente]")
    r = log.add(evento_citando("IA: pode ser esse mesmo", "Q1"))
    assert r["instruction"] == "pode ser esse mesmo"
    assert r["reply_to"]["text"] == "Pode ser um vídeo qualquer?"


def test_contextinfo_no_topo_de_data_tambem_vale():
    log = EventLog(owner_number="5584999290327")
    log.store.mark("Q1", instruction="[enviada pelo assistente]")
    e = evento("sim, pode", jid=KEILLA,
               extra={"contextInfo": {"stanzaId": "Q1", "quotedMessage": {"conversation": "Confirma?"}}})
    e["data"]["key"]["id"] = "R2"
    r = log.add(e)
    assert r["trigger"] is True
    assert r["reply_to"]["text"] == "Confirma?"


# ---------------------------------------------------------------------------
# comando de voz: áudio do dono que começa com a palavra de ativação
# ---------------------------------------------------------------------------

from evoapi_mcp.webhook import voice_instruction  # noqa: E402


def audio(from_me=True, jid=KEILLA, msg_id="A1"):
    e = evento(None, from_me=from_me, jid=jid, tipo="audioMessage")
    e["data"]["key"]["id"] = msg_id
    e["data"]["message"] = {"audioMessage": {"mimetype": "audio/ogg; codecs=opus", "seconds": 4, "ptt": True}}
    return e


@pytest.mark.parametrize("texto,esperado", [
    ("Computador, explique pra Keilla o que é Bambu Lab A1", "explique pra Keilla o que é Bambu Lab A1"),
    ("computador explique isso", "explique isso"),
    ("COMPUTADOR: manda o boleto", "manda o boleto"),
    ("Computador.", None),
    ("Computadores estão caros", None),
    ("Ei computador, faça", None),
    ("", None),
    (None, None),
])
def test_voice_instruction(texto, esperado):
    assert voice_instruction(texto) == esperado


def test_palavra_de_ativacao_configuravel():
    assert voice_instruction("Jarvis, apaga a luz", wake_word="jarvis") == "apaga a luz"
    assert voice_instruction("Computador, apaga a luz", wake_word="jarvis") is None


def test_audio_do_dono_fica_pendente_de_voz_sem_acionar():
    log = EventLog(owner_number="5584999290327")
    r = log.add(audio())
    assert r.get("trigger") is None
    assert r["voice_pending"] is True
    assert log.pending() == []


def test_audio_de_terceiro_nao_fica_pendente():
    log = EventLog(owner_number="5584999290327")
    r = log.add(audio(from_me=False))
    assert r.get("voice_pending") is None


def test_resolve_voice_com_palavra_vira_acionamento():
    log = EventLog(owner_number="5584999290327")
    log.add(audio())
    e = log.resolve_voice("A1", "Computador, explique praquê ele o que é bambu lab A1")
    assert e["trigger"] is True
    assert e["voice"] is True
    assert e["instruction"] == "explique praquê ele o que é bambu lab A1"
    assert e.get("voice_pending") is None
    assert log.pending()[0]["message_id"] == "A1"


def test_resolve_voice_sem_palavra_em_chat_de_terceiro_nao_aciona():
    log = EventLog(owner_number="5584999290327")
    log.add(audio())
    e = log.resolve_voice("A1", "oi Keilla, depois te ligo")
    assert e.get("trigger") is None
    assert e["transcript"] == "oi Keilla, depois te ligo"
    assert log.pending() == []


def test_audio_na_conversa_pessoal_e_instrucao_mesmo_sem_palavra():
    log = EventLog(owner_number="5584999290327")
    e = audio(jid="110818863673433@lid", msg_id="A2")
    e["data"]["key"]["remoteJidAlt"] = "5584999290327@s.whatsapp.net"
    r = log.add(e)
    assert r["voice_pending"] is True
    res = log.resolve_voice("A2", "manda o resumo das notas de agosto")
    assert res["trigger"] is True
    assert res["instruction"] == "manda o resumo das notas de agosto"


def test_audio_na_conversa_pessoal_tira_a_palavra_se_vier():
    log = EventLog(owner_number="5584999290327")
    e = audio(jid="110818863673433@lid", msg_id="A3")
    e["data"]["key"]["remoteJidAlt"] = "5584999290327@s.whatsapp.net"
    log.add(e)
    assert log.resolve_voice("A3", "Computador, manda o resumo")["instruction"] == "manda o resumo"


def test_resolve_voice_com_transcricao_vazia_nao_aciona():
    log = EventLog(owner_number="5584999290327")
    log.add(audio())
    e = log.resolve_voice("A1", None)
    assert e.get("trigger") is None
    assert log.pending() == []


def test_resolve_voice_de_id_desconhecido_devolve_none():
    log = EventLog(owner_number="5584999290327")
    assert log.resolve_voice("NAO_EXISTE", "Computador, oi") is None


def test_resolve_voice_so_uma_vez():
    log = EventLog(owner_number="5584999290327")
    log.add(audio())
    log.resolve_voice("A1", "Computador, faça")
    assert log.resolve_voice("A1", "Computador, faça de novo") is None


# ---------------------------------------------------------------------------
# citação de uma mensagem com anexo: a instrução é sobre aquele arquivo
# ---------------------------------------------------------------------------

def evento_citando_arquivo(texto, msg_id="R9", jid=KEILLA, from_me=True):
    e = evento(texto, from_me=from_me, jid=jid)
    e["data"]["key"]["id"] = msg_id
    e["data"]["message"] = {"extendedTextMessage": {"text": texto, "contextInfo": {
        "stanzaId": "DOC1", "participant": "558498140038@s.whatsapp.net",
        "quotedMessage": {"documentMessage": {
            "fileName": "NF 1234 Econtec.pdf", "mimetype": "application/pdf", "caption": "segue a nota",
        }},
    }}}
    return e


def test_ia_citando_pdf_de_terceiro_aponta_para_o_pdf():
    log = EventLog(owner_number="5584999290327")
    r = log.add(evento_citando_arquivo("IA: arquive isso em Sol Prime, nota"))
    assert r["trigger"] is True
    assert r["instruction"] == "arquive isso em Sol Prime, nota"
    assert r["reply_to"] == {
        "id": "DOC1", "text": "segue a nota", "type": "document",
        "file": "NF 1234 Econtec.pdf", "mime": "application/pdf",
    }


def test_citar_pdf_sem_prefixo_em_chat_de_terceiro_nao_aciona():
    log = EventLog(owner_number="5584999290327")
    r = log.add(evento_citando_arquivo("recebi, obrigado"))
    assert r.get("trigger") is None
    assert r["reply_to"]["file"] == "NF 1234 Econtec.pdf"


def test_audio_citando_pdf_guarda_a_citacao_para_depois_da_transcricao():
    log = EventLog(owner_number="5584999290327")
    e = audio(msg_id="A9")
    e["data"]["contextInfo"] = {"stanzaId": "DOC1", "quotedMessage": {
        "documentMessage": {"fileName": "boleto.pdf", "mimetype": "application/pdf"}}}
    r = log.add(e)
    assert r["voice_pending"] is True
    assert r["reply_to"]["file"] == "boleto.pdf"
    res = log.resolve_voice("A9", "Computador, arquiva isso em MR, boleto")
    assert res["trigger"] is True
    assert res["reply_to"]["id"] == "DOC1"


def test_citacao_de_imagem_sem_nome_tem_tipo_e_mime():
    log = EventLog(owner_number="5584999290327")
    e = evento("IA: o que é isso?", jid=KEILLA, extra={"contextInfo": {
        "stanzaId": "IMG1", "quotedMessage": {"imageMessage": {"mimetype": "image/jpeg"}}}})
    e["data"]["key"]["id"] = "R10"
    r = log.add(e)
    assert r["reply_to"] == {"id": "IMG1", "type": "image", "mime": "image/jpeg"}
