"""Formatadores compactos para reduzir o volume de tokens enviado ao LLM.

A Evolution API devolve objetos brutos do WhatsApp (Baileys) com dezenas de
campos irrelevantes para o assistente: thumbnails em base64, chaves de mídia,
hashes, metadados de dispositivo, etc. Uma mensagem "crua" costuma ter 1-3 KB
de JSON; a versão compacta fica em torno de 100-200 bytes.

Todos os formatadores omitem campos nulos/vazios para economizar tokens.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

# Tipos de mensagem do WhatsApp -> tipo simplificado
_MEDIA_TYPES = {
    "imageMessage": "image",
    "videoMessage": "video",
    "audioMessage": "audio",
    "documentMessage": "document",
    "stickerMessage": "sticker",
    "ptvMessage": "video",
}

_OTHER_TYPES = {
    "conversation": "text",
    "extendedTextMessage": "text",
    "reactionMessage": "reaction",
    "locationMessage": "location",
    "liveLocationMessage": "location",
    "contactMessage": "contact",
    "contactsArrayMessage": "contact",
    "pollCreationMessage": "poll",
    "pollCreationMessageV3": "poll",
    "protocolMessage": "system",
    "senderKeyDistributionMessage": "system",
    "call": "call",
}

# Wrappers que envolvem a mensagem real em `.message`
_WRAPPERS = (
    "ephemeralMessage",
    "viewOnceMessage",
    "viewOnceMessageV2",
    "viewOnceMessageV2Extension",
    "documentWithCaptionMessage",
    "editedMessage",
)

_ELLIPSIS = "…"


def dumps(obj: Any) -> str:
    """Serializa JSON compacto (sem espaços, sem escapar acentos)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def clean(d: dict[str, Any]) -> dict[str, Any]:
    """Remove chaves com valor None, "", [] ou {}."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def truncate(text: str | None, max_chars: int | None) -> tuple[str | None, bool]:
    """Corta o texto em max_chars. Retorna (texto, foi_truncado)."""
    if text is None:
        return None, False
    if not max_chars or max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[: max_chars - 1].rstrip() + _ELLIPSIS, True


def fmt_ts(ts: Any) -> str | None:
    """Converte timestamp (segundos, ms, string ou ISO) para 'YYYY-MM-DD HH:MM'."""
    if ts is None or ts == "":
        return None
    try:
        if isinstance(ts, str):
            if ts.isdigit():
                ts = int(ts)
            else:
                # ISO 8601 (ex: updatedAt dos chats)
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
        if isinstance(ts, dict):  # protobuf Long {low, high}
            ts = int(ts.get("low", 0)) + (int(ts.get("high", 0)) << 32)
        ts = float(ts)
        if ts > 1e12:  # milissegundos
            ts /= 1000
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError, OverflowError):
        return str(ts)


def jid_to_number(jid: str | None) -> str | None:
    """'5511999999999@s.whatsapp.net' -> '5511999999999'.

    Grupos (@g.us) e ids opacos (@lid) são devolvidos inteiros: a parte local
    de um @lid NÃO é um telefone.
    """
    if not jid:
        return None
    if jid.endswith("@g.us") or jid.endswith("@lid"):
        return jid
    return jid.split("@", 1)[0].split(":", 1)[0]


def sender_of(key: dict[str, Any], record: dict[str, Any] | None = None) -> str | None:
    """Remetente de uma mensagem, preferindo o telefone ao id opaco @lid.

    Com addressingMode=lid a API manda `participant`/`remoteJid` como @lid e o
    telefone em `participantAlt`/`remoteJidAlt`.
    """
    record = record or {}
    if key.get("fromMe"):
        return "me"
    participant = key.get("participant") or record.get("participant")
    if participant:
        jid = key.get("participantAlt") or participant
    else:
        jid = key.get("remoteJidAlt") or key.get("remoteJid") or record.get("remoteJid")
    return jid_to_number(jid)


def _unwrap(message: dict[str, Any] | None) -> dict[str, Any]:
    """Desembrulha ephemeral/viewOnce/documentWithCaption até chegar ao conteúdo."""
    msg = message or {}
    for _ in range(4):
        for w in _WRAPPERS:
            inner = msg.get(w)
            if isinstance(inner, dict) and isinstance(inner.get("message"), dict):
                msg = inner["message"]
                break
        else:
            return msg
    return msg


def _detect_type(msg: dict[str, Any], declared: str | None) -> tuple[str, dict[str, Any] | None]:
    """Retorna (tipo simplificado, payload da mídia se houver)."""
    for key, kind in _MEDIA_TYPES.items():
        if isinstance(msg.get(key), dict):
            return kind, msg[key]
    for key, kind in _OTHER_TYPES.items():
        if key in msg:
            return kind, None
    if declared in _MEDIA_TYPES:
        return _MEDIA_TYPES[declared], None
    if declared in _OTHER_TYPES:
        return _OTHER_TYPES[declared], None
    return "other", None


def _extract_text(msg: dict[str, Any], kind: str, media: dict[str, Any] | None) -> str | None:
    if kind == "text":
        if isinstance(msg.get("conversation"), str):
            return msg["conversation"]
        ext = msg.get("extendedTextMessage")
        if isinstance(ext, dict):
            return ext.get("text")
        return None
    if media:
        return media.get("caption")
    if kind == "reaction":
        r = msg.get("reactionMessage") or {}
        return r.get("text")
    if kind == "location":
        loc = msg.get("locationMessage") or msg.get("liveLocationMessage") or {}
        lat, lng = loc.get("degreesLatitude"), loc.get("degreesLongitude")
        if lat is not None and lng is not None:
            name = loc.get("name") or loc.get("address")
            return f"{lat},{lng}" + (f" ({name})" if name else "")
    if kind == "contact":
        c = msg.get("contactMessage") or {}
        return c.get("displayName")
    if kind == "poll":
        p = msg.get("pollCreationMessage") or msg.get("pollCreationMessageV3") or {}
        return p.get("name")
    return None


def _to_int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def compact_message(record: dict[str, Any], max_text: int | None = 500) -> dict[str, Any]:
    """Reduz um registro de mensagem da Evolution API ao essencial.

    Campos de saída (nulos omitidos):
        id        - id da mensagem (use em download_media)
        ts        - 'YYYY-MM-DD HH:MM'
        from      - 'me' quando enviada pela instância; senão o número/participante
        name      - pushName do remetente
        chat      - jid do chat (só quando for grupo, para distinguir do remetente)
        type      - text | image | video | audio | document | sticker | reaction |
                    location | contact | poll | system | call | other
        text      - conteúdo/legenda (truncado em max_text)
        truncated - True se o texto foi cortado
        file      - nome do arquivo (documentos)
        mime      - mimetype da mídia
        size      - tamanho em bytes
        reply_to  - id da mensagem citada
    """
    key = record.get("key") or {}
    msg = _unwrap(record.get("message"))
    kind, media = _detect_type(msg, record.get("messageType"))
    text, truncated = truncate(_extract_text(msg, kind, media), max_text)

    remote_jid = key.get("remoteJid") or record.get("remoteJid")
    is_group = bool(remote_jid and remote_jid.endswith("@g.us"))
    sender = sender_of(key, record)

    ctx = record.get("contextInfo") or {}
    if not ctx:
        for v in msg.values():
            if isinstance(v, dict) and isinstance(v.get("contextInfo"), dict):
                ctx = v["contextInfo"]
                break

    out: dict[str, Any] = {
        "id": key.get("id") or record.get("id"),
        "ts": fmt_ts(record.get("messageTimestamp")),
        "from": sender,
        "name": record.get("pushName") if sender != "me" else None,
        "chat": remote_jid if is_group else None,
        "type": kind,
        "text": text,
        "truncated": True if truncated else None,
    }
    if media:
        out["file"] = media.get("fileName") or media.get("title")
        out["mime"] = media.get("mimetype")
        out["size"] = _to_int(media.get("fileLength"))
        if kind == "audio":
            if media.get("seconds"):
                out["seconds"] = _to_int(media.get("seconds"))
            if media.get("ptt"):
                out["voice"] = True
    if isinstance(ctx, dict) and ctx.get("stanzaId"):
        out["reply_to"] = ctx["stanzaId"]
    return clean(out)


def extract_records(response: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normaliza a resposta de findMessages (v1: lista; v2: {messages:{records}}).

    Returns:
        (registros, metadados de paginação)
    """
    if isinstance(response, list):
        return response, {}
    if isinstance(response, dict):
        inner = response.get("messages", response)
        if isinstance(inner, dict):
            records = inner.get("records") or inner.get("messages") or []
            meta = clean({
                "total": inner.get("total"),
                "pages": inner.get("pages"),
                "page": inner.get("currentPage"),
            })
            return list(records), meta
        if isinstance(inner, list):
            return inner, {}
    return [], {}


def compact_messages(response: Any, max_text: int | None = 500) -> dict[str, Any]:
    records, meta = extract_records(response)
    return clean({**meta, "count": len(records), "messages": [compact_message(r, max_text) for r in records]})


def _preview_from_message(last: dict[str, Any] | None, max_text: int | None) -> dict[str, Any]:
    if not isinstance(last, dict):
        return {}
    c = compact_message(last, max_text)
    label = c.get("text")
    if c.get("type") not in (None, "text"):
        tag = f"[{c['type']}]"
        if c.get("file"):
            tag = f"[{c['type']}: {c['file']}]"
        label = f"{tag} {label}" if label else tag
    return clean({"last_ts": c.get("ts"), "last_from": c.get("from"), "last": label})


def compact_chat(chat: dict[str, Any], max_text: int | None = 120) -> dict[str, Any]:
    """Reduz um chat de findChats ao essencial.

    Campos: jid, number, name, group, unread, last_ts, last_from, last
    """
    jid = chat.get("remoteJid") or chat.get("id")
    is_group = bool(jid and str(jid).endswith("@g.us"))
    number = None
    if not is_group:
        # Conversas @lid: o telefone só aparece em lastMessage.key.remoteJidAlt
        last_key = (chat.get("lastMessage") or {}).get("key") or {}
        alt = last_key.get("remoteJidAlt") if str(jid).endswith("@lid") else None
        number = jid_to_number(alt or jid)
        if number == jid:  # @lid sem alternativa: não há telefone conhecido
            number = None
    out = {
        "jid": jid,
        "number": number,
        "name": chat.get("name") or chat.get("pushName"),
        "group": True if is_group else None,
        "unread": _to_int(chat.get("unreadCount")) or None,
    }
    preview = _preview_from_message(chat.get("lastMessage"), max_text)
    if not preview.get("last_ts"):
        preview["last_ts"] = fmt_ts(chat.get("updatedAt") or chat.get("lastMsgTimestamp"))
    out.update(preview)
    return clean(out)


def compact_contact(contact: dict[str, Any]) -> dict[str, Any]:
    """Reduz um contato ao essencial: jid, number, name, group."""
    jid = contact.get("remoteJid") or contact.get("id")
    is_group = bool(jid and str(jid).endswith("@g.us")) or bool(contact.get("isGroup"))
    return clean({
        "jid": jid,
        "number": None if is_group else jid_to_number(jid),
        "name": contact.get("pushName") or contact.get("name"),
        "group": True if is_group else None,
    })


def compact_send_result(response: Any) -> dict[str, Any]:
    """Reduz a resposta de envio (sendText/sendMedia) a {ok, id, to, ts, status}."""
    if not isinstance(response, dict):
        return {"ok": True, "raw": response}
    key = response.get("key") or {}
    return clean({
        "ok": True,
        "id": key.get("id") or response.get("messageId"),
        "to": jid_to_number(key.get("remoteJid")),
        "ts": fmt_ts(response.get("messageTimestamp")),
        "status": response.get("status"),
    })


def compact_error(message: str, max_chars: int = 400) -> str:
    """Resume corpos de erro HTTP longos (HTML, stack traces)."""
    text = re.sub(r"<[^>]+>", " ", message)
    text = re.sub(r"\s+", " ", text).strip()
    return truncate(text, max_chars)[0] or message


def message_matches(compact: dict[str, Any], query: str) -> bool:
    """Busca case-insensitive em texto, nome de arquivo e remetente."""
    q = query.casefold()
    for field in ("text", "file", "name"):
        v = compact.get(field)
        if isinstance(v, str) and q in v.casefold():
            return True
    return False
