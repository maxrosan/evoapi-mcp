"""Conversa dividida entre @lid e número: aprender o par e ler pelos dois endereços."""

from evoapi_mcp.webhook import EventLog
from test_client import rec, v2

NUMERO = "558488399008@s.whatsapp.net"
LID = "199673163813042@lid"
GRUPO = "120363405632090471@g.us"


def doc(mid, nome, jid, ts):
    return {
        "key": {"remoteJid": jid, "fromMe": True, "id": mid},
        "messageType": "documentMessage",
        "message": {"documentMessage": {"fileName": nome, "mimetype": "application/pdf"}},
        "messageTimestamp": ts,
    }


def chats_da_bia():
    return [
        {"remoteJid": LID, "lastMessage": {"key": {"id": "X", "remoteJid": LID, "remoteJidAlt": NUMERO}}},
        {"remoteJid": NUMERO, "lastMessage": {"key": {"id": "Y", "remoteJid": NUMERO}}},
        {"remoteJid": GRUPO, "lastMessage": {"key": {"id": "Z", "remoteJid": GRUPO,
                                                     "participant": "111@lid", "participantAlt": "5584111@s.whatsapp.net"}}},
    ]


# ---------------------------------------------------------------- aprender pares

def test_learn_chat_alias_so_aceita_lid_com_numero(client):
    assert client.learn_chat_alias(LID, NUMERO) is True
    assert client.learn_chat_alias(NUMERO, LID) is False          # já conhecido
    assert client.learn_chat_alias(GRUPO, NUMERO) is False
    assert client.learn_chat_alias(NUMERO, NUMERO) is False
    assert client.learn_chat_alias(None, LID) is False
    assert client.chat_addresses(NUMERO) == [NUMERO, LID]
    assert client.chat_addresses(LID) == [LID, NUMERO]
    assert client.chat_addresses(GRUPO) == [GRUPO]


def test_aprende_pela_lista_de_conversas_com_cache(client):
    client.responses.append(chats_da_bia())
    assert client.refresh_chat_aliases() == 2
    assert client.chat_addresses(NUMERO) == [NUMERO, LID]
    assert client.chat_addresses("5584111@s.whatsapp.net") == ["5584111@s.whatsapp.net", "111@lid"]
    assert client.refresh_chat_aliases() == 0                       # dentro dos 5 minutos
    assert len(client.calls) == 1


def test_resolver_numero_tambem_ensina_o_par(client):
    client.responses.append(chats_da_bia())
    client.resolve_chat_jid("558488399008")
    assert LID in client.chat_addresses(NUMERO)


def test_eventlog_ensina_o_par_pelo_webhook():
    from test_webhook import evento

    pares = []
    log = EventLog(owner_number="5584999290327")
    log.on_alias = lambda a, b: pares.append((a, b))
    e = evento("IA: grave um áudio", jid=LID)
    e["data"]["key"]["remoteJidAlt"] = NUMERO
    log.add(e)
    assert pares == [(LID, NUMERO)]


# ---------------------------------------------------------------- ler pelos dois endereços

def test_sem_par_conhecido_le_um_endereco_so(client):
    client.responses.append(v2([rec("A", "oi", jid=NUMERO)]))
    out = client.find_messages(chat_id=NUMERO, limit=5)
    assert len(client.calls) == 1
    assert "chats" not in out


def test_listagem_junta_os_dois_enderecos_pela_hora(client):
    client.learn_chat_alias(LID, NUMERO)
    client.responses.append(v2([rec("RESP", "Não achei nada sobre a Silvana", ts=1789589078, jid=NUMERO)]))
    client.responses.append(v2([
        rec("PEDIDO", "IA: grave um áudio explicando essa situação de Silvana", ts=1789588989, jid=LID),
        rec("EXPLICA", "A conta antiga da Silvana foi desativada...", ts=1789588860, jid=LID),
    ]))
    out = client.find_messages(chat_id=NUMERO, limit=3)
    assert [c["data"]["where"] for c in client.calls] == [{"key": {"remoteJid": NUMERO}}, {"key": {"remoteJid": LID}}]
    assert [m["id"] for m in out["messages"]] == ["RESP", "PEDIDO", "EXPLICA"]
    assert out["chats"] == [NUMERO, LID]


def test_listagem_corta_no_limite_depois_de_juntar(client):
    client.learn_chat_alias(LID, NUMERO)
    client.responses.append(v2([rec("N1", "a", ts=100, jid=NUMERO), rec("N2", "b", ts=10, jid=NUMERO)]))
    client.responses.append(v2([rec("L1", "c", ts=50, jid=LID), rec("L2", "d", ts=5, jid=LID)]))
    out = client.find_messages(chat_id=NUMERO, limit=2)
    assert [m["id"] for m in out["messages"]] == ["N1", "L1"]


def test_busca_por_tipo_nos_dois_enderecos(client):
    client.learn_chat_alias(LID, NUMERO)
    client.responses.append(v2([doc("N1", "velho.pdf", NUMERO, 10)]))
    client.responses.append(v2([doc("L1", "CNO.pdf", LID, 300), rec("T", "texto", ts=200, jid=LID),
                                doc("L2", "Certidao.pdf", LID, 100)]))
    out = client.find_messages(chat_id=NUMERO, kind="pdf", limit=2)
    assert [m["file"] for m in out["messages"]] == ["CNO.pdf", "Certidao.pdf"]
    assert out["chats"] == [NUMERO, LID]


def test_anexos_recentes_nos_dois_enderecos(client):
    client.learn_chat_alias(LID, NUMERO)
    client.responses.append(v2([doc("N1", "enviado_pela_api.pdf", NUMERO, 500)]))
    client.responses.append(v2([doc("L1", "do_celular.pdf", LID, 400)]))
    assert [a["arquivo"] for a in client.recent_attachments(NUMERO, limit=5)] == ["enviado_pela_api.pdf", "do_celular.pdf"]


def test_grupo_nunca_e_expandido(client):
    client.learn_chat_alias(LID, NUMERO)
    client.responses.append(v2([rec("G", "oi", jid=GRUPO)]))
    client.find_messages(chat_id=GRUPO, limit=5)
    assert len(client.calls) == 1
