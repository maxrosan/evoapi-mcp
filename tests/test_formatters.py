"""Testes dos formatadores compactos (economia de tokens)."""

import json
from datetime import datetime

import pytest

from evoapi_mcp.formatters import (
    compact_chat,
    compact_contact,
    compact_error,
    compact_message,
    compact_messages,
    compact_send_result,
    dumps,
    extract_records,
    fmt_ts,
    message_matches,
    truncate,
)

TS = 1757600000  # 2025-09-11 (segundos)


def raw_text_message(text="Olá, tudo bem?", from_me=False, ts=TS):
    return {
        "id": "cuid123",
        "key": {"remoteJid": "5511999999999@s.whatsapp.net", "fromMe": from_me, "id": "3EB0ABC"},
        "pushName": "Fulano",
        "messageType": "conversation",
        "message": {
            "conversation": text,
            "messageContextInfo": {"deviceListMetadata": {"senderKeyHash": "x" * 200}},
        },
        "messageTimestamp": ts,
        "instanceId": "inst",
        "source": "android",
    }


def raw_document_message():
    return {
        "key": {"remoteJid": "5511999999999@s.whatsapp.net", "fromMe": False, "id": "DOC1"},
        "pushName": "Contador",
        "messageType": "documentMessage",
        "message": {
            "documentMessage": {
                "url": "https://mmg.whatsapp.net/x",
                "mimetype": "application/pdf",
                "title": "boleto",
                "fileSha256": "abc",
                "fileLength": "48213",
                "pageCount": 1,
                "mediaKey": "k" * 100,
                "fileName": "boleto-setembro.pdf",
                "caption": "Segue o boleto",
                "jpegThumbnail": "/9j/" + "A" * 3000,
                "contextInfo": {"stanzaId": "QUOTED1", "quotedMessage": {"conversation": "manda"}},
            }
        },
        "messageTimestamp": str(TS),
    }


def raw_group_image_message():
    return {
        "key": {"remoteJid": "120363000@g.us", "fromMe": False, "id": "IMG1", "participant": "5521988887777@s.whatsapp.net"},
        "pushName": "Maria",
        "messageType": "imageMessage",
        "message": {"imageMessage": {"mimetype": "image/jpeg", "fileLength": 1200, "jpegThumbnail": "zzz"}},
        "messageTimestamp": TS * 1000,  # milissegundos
    }


def test_compact_text_message_drops_noise():
    c = compact_message(raw_text_message())
    assert c == {
        "id": "3EB0ABC",
        "ts": fmt_ts(TS),
        "from": "5511999999999",
        "name": "Fulano",
        "type": "text",
        "text": "Olá, tudo bem?",
    }
    assert "messageContextInfo" not in json.dumps(c)


def test_from_me_omits_name():
    c = compact_message(raw_text_message(from_me=True))
    assert c["from"] == "me"
    assert "name" not in c


def test_compact_document_message():
    c = compact_message(raw_document_message())
    assert c["type"] == "document"
    assert c["file"] == "boleto-setembro.pdf"
    assert c["mime"] == "application/pdf"
    assert c["size"] == 48213
    assert c["text"] == "Segue o boleto"
    assert c["reply_to"] == "QUOTED1"
    assert "jpegThumbnail" not in json.dumps(c)


def test_group_message_has_chat_and_participant():
    c = compact_message(raw_group_image_message())
    assert c["chat"] == "120363000@g.us"
    assert c["from"] == "5521988887777"
    assert c["type"] == "image"
    assert c["ts"] == fmt_ts(TS)  # ms normalizado


def test_wrapped_messages_are_unwrapped():
    rec = {
        "key": {"remoteJid": "1@s.whatsapp.net", "fromMe": False, "id": "W1"},
        "message": {
            "documentWithCaptionMessage": {
                "message": {"documentMessage": {"fileName": "nf.pdf", "mimetype": "application/pdf", "caption": "NF"}}
            }
        },
        "messageTimestamp": TS,
    }
    c = compact_message(rec)
    assert c["type"] == "document"
    assert c["file"] == "nf.pdf"
    assert c["text"] == "NF"


def test_truncation():
    long = "a" * 1000
    c = compact_message(raw_text_message(text=long), max_text=100)
    assert len(c["text"]) == 100
    assert c["text"].endswith("…")
    assert c["truncated"] is True
    c2 = compact_message(raw_text_message(text=long), max_text=None)
    assert len(c2["text"]) == 1000
    assert "truncated" not in c2


def test_truncate_helper():
    assert truncate("abc", 10) == ("abc", False)
    assert truncate("abcdef", 4) == ("abc…", True)
    assert truncate(None, 4) == (None, False)


def test_extract_records_v1_and_v2():
    v2 = {"messages": {"total": 120, "pages": 3, "currentPage": 1, "records": [raw_text_message()]}}
    records, meta = extract_records(v2)
    assert len(records) == 1
    assert meta == {"total": 120, "pages": 3, "page": 1}

    v1 = [raw_text_message(), raw_text_message()]
    records, meta = extract_records(v1)
    assert len(records) == 2 and meta == {}


def test_compact_messages_wrapper():
    v2 = {"messages": {"total": 1, "pages": 1, "currentPage": 1, "records": [raw_document_message()]}}
    out = compact_messages(v2)
    assert out["count"] == 1
    assert out["messages"][0]["file"] == "boleto-setembro.pdf"


def test_compact_is_much_smaller_than_raw():
    raw = raw_document_message()
    assert len(dumps(compact_message(raw))) < len(json.dumps(raw)) / 10


def test_compact_chat():
    chat = {
        "id": "c1",
        "remoteJid": "5511999999999@s.whatsapp.net",
        "name": None,
        "pushName": "Fulano",
        "profilePicUrl": "https://pps.whatsapp.net/" + "x" * 300,
        "updatedAt": "2025-09-11T10:00:00.000Z",
        "unreadCount": 3,
        "lastMessage": raw_document_message(),
    }
    c = compact_chat(chat)
    assert c["jid"] == "5511999999999@s.whatsapp.net"
    assert c["number"] == "5511999999999"
    assert c["name"] == "Fulano"
    assert c["unread"] == 3
    assert c["last"] == "[document: boleto-setembro.pdf] Segue o boleto"
    assert c["last_from"] == "5511999999999"
    assert "group" not in c
    assert "profilePicUrl" not in json.dumps(c)


def test_compact_chat_group_without_last_message():
    c = compact_chat({"remoteJid": "1203@g.us", "name": "Família", "updatedAt": "2025-09-11T10:00:00.000Z"})
    assert c["group"] is True
    assert "number" not in c
    assert c["last_ts"]


def test_compact_contact():
    c = compact_contact({"remoteJid": "5511999999999@s.whatsapp.net", "pushName": "Zé", "profilePicUrl": "http://x"})
    assert c == {"jid": "5511999999999@s.whatsapp.net", "number": "5511999999999", "name": "Zé"}


def test_compact_send_result():
    r = compact_send_result({
        "key": {"remoteJid": "5511999999999@s.whatsapp.net", "fromMe": True, "id": "SENT1"},
        "message": {"conversation": "oi", "messageContextInfo": {"x": "y" * 500}},
        "messageTimestamp": TS,
        "status": "PENDING",
    })
    assert r == {"ok": True, "id": "SENT1", "to": "5511999999999", "ts": fmt_ts(TS), "status": "PENDING"}


def test_fmt_ts_variants():
    expected = datetime.fromtimestamp(TS).strftime("%Y-%m-%d %H:%M")
    assert fmt_ts(TS) == expected
    assert fmt_ts(str(TS)) == expected
    assert fmt_ts(TS * 1000) == expected
    assert fmt_ts({"low": TS, "high": 0}) == expected
    assert fmt_ts(None) is None


def test_compact_error_strips_html():
    html = "<html><body><h1>502 Bad Gateway</h1>" + "<p>x</p>" * 500 + "</body></html>"
    e = compact_error(html)
    assert e.startswith("502 Bad Gateway")
    assert len(e) <= 401


def test_message_matches():
    c = compact_message(raw_document_message())
    assert message_matches(c, "BOLETO")
    assert message_matches(c, "setembro.pdf")
    assert not message_matches(c, "nota fiscal")


def test_dumps_is_compact_and_keeps_accents():
    s = dumps({"a": "ção", "b": [1, 2]})
    assert s == '{"a":"ção","b":[1,2]}'
