"""Endereçamento @lid: o WhatsApp troca <numero>@s.whatsapp.net por <id opaco>@lid."""

import pytest

from evoapi_mcp.client import InvalidPhoneNumberError
from evoapi_mcp.formatters import compact_chat, compact_message, jid_to_number, sender_of

LID = "47206119772316@lid"
PHONE_JID = "554484389644@s.whatsapp.net"


def group_record():
    return {
        "key": {
            "id": "A5E6",
            "fromMe": False,
            "remoteJid": "120363037915894420@g.us",
            "participant": LID,
            "addressingMode": "lid",
            "participantAlt": PHONE_JID,
        },
        "pushName": "Anderson",
        "messageType": "conversation",
        "message": {
            "conversation": "Obrigado",
            "messageContextInfo": {"messageSecret": "x"},
            "senderKeyDistributionMessage": {"groupId": "120363037915894420@g.us", "axolotlSenderKeyDistributionMessage": "y" * 100},
        },
        "messageTimestamp": 1789170336,
    }


def dm_lid_record(with_alt=True):
    key = {"id": "B1", "fromMe": False, "remoteJid": LID, "addressingMode": "lid"}
    if with_alt:
        key["remoteJidAlt"] = PHONE_JID
    return {"key": key, "pushName": "Fulano", "messageType": "conversation", "message": {"conversation": "oi"}, "messageTimestamp": 1789170336}


def test_jid_to_number_keeps_lid_and_groups():
    assert jid_to_number(PHONE_JID) == "554484389644"
    assert jid_to_number(LID) == LID
    assert jid_to_number("1@g.us") == "1@g.us"


def test_group_message_prefers_participant_alt_phone():
    c = compact_message(group_record())
    assert c["from"] == "554484389644"
    assert c["chat"] == "120363037915894420@g.us"
    assert c["type"] == "text"  # senderKeyDistributionMessage não vira 'system'
    assert c["text"] == "Obrigado"


def test_dm_lid_prefers_remote_jid_alt():
    assert compact_message(dm_lid_record())["from"] == "554484389644"
    assert compact_message(dm_lid_record(with_alt=False))["from"] == LID
    assert sender_of({"fromMe": True, "remoteJid": LID}) == "me"


def test_compact_chat_lid_uses_alt_number():
    chat = {"remoteJid": LID, "pushName": "Fulano", "lastMessage": dm_lid_record(), "unreadCount": 1}
    c = compact_chat(chat)
    assert c["jid"] == LID
    assert c["number"] == "554484389644"

    chat_no_alt = {"remoteJid": LID, "pushName": "Fulano", "lastMessage": dm_lid_record(with_alt=False)}
    assert "number" not in compact_chat(chat_no_alt)


# ---------------------------------------------------------------------------
# cliente: resolução de jid e destino de envio
# ---------------------------------------------------------------------------

def v2(records):
    return {"messages": {"total": len(records), "pages": 1, "currentPage": 1, "records": records}}


def chats_with_lid():
    return [
        {"remoteJid": "1@g.us", "pushName": "Grupo"},
        {"remoteJid": LID, "pushName": "Fulano", "lastMessage": {"key": {"remoteJid": LID, "remoteJidAlt": PHONE_JID}}},
    ]


def test_resolve_chat_jid_finds_lid_via_chats(client):
    client.responses.append(chats_with_lid())
    jid, resolved = client.resolve_chat_jid("554484389644")
    assert (jid, resolved) == (LID, True)
    assert client.calls[0]["endpoint"] == "/chat/findChats/{instanceId}"

    # segunda chamada vem do cache, sem HTTP
    assert client.resolve_chat_jid("554484389644") == (LID, True)
    assert len(client.calls) == 1


def test_resolve_chat_jid_passthrough_and_fallback(client):
    assert client.resolve_chat_jid(LID) == (LID, True)
    assert client.resolve_chat_jid("1@g.us") == ("1@g.us", True)
    client.responses.append([])  # nenhuma conversa
    assert client.resolve_chat_jid("5511999999999") == ("5511999999999@s.whatsapp.net", False)
    with pytest.raises(InvalidPhoneNumberError):
        client.resolve_chat_jid("abc")


def test_get_messages_by_number_queries_lid_chat(client):
    client.responses.extend([chats_with_lid(), v2([dm_lid_record()])])
    out = client.get_messages_by_number("554484389644", limit=5)

    assert client.calls[1]["data"]["where"] == {"key": {"remoteJid": LID}}
    assert out["chat"] == LID
    assert out["count"] == 1
    assert out["messages"][0]["from"] == "554484389644"
    assert "hint" not in out


def test_get_messages_by_number_hint_when_unresolved(client):
    client.responses.extend([[], v2([])])
    out = client.get_messages_by_number("5511999999999")
    assert out["count"] == 0
    assert out["chat"] == "5511999999999@s.whatsapp.net"
    assert "hint" in out


def test_send_target_keeps_jid_intact(client):
    client.responses.extend([{}, {}])
    client.send_text(LID, "oi")
    client.send_text("55 (11) 99999-9999", "oi")
    assert client.calls[0]["data"]["number"] == LID
    assert client.calls[1]["data"]["number"] == "5511999999999"


def test_contacts_map_ignores_lid_entries(client):
    client.responses.append([
        {"remoteJid": LID, "pushName": "Opaco"},
        {"remoteJid": PHONE_JID, "pushName": "Fulano"},
    ])
    assert client._build_contacts_map() == {"554484389644": "Fulano"}


def test_find_chats_enriches_lid_chat_via_alt(client):
    client.responses.extend([
        [{"remoteJid": LID, "pushName": None, "lastMessage": {"key": {"remoteJid": LID, "remoteJidAlt": PHONE_JID}}}],
        [{"remoteJid": PHONE_JID, "pushName": "Fulano"}],
    ])
    chats = client.find_chats()
    assert chats[0]["pushName"] == "Fulano"
