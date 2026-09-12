"""Testes do cliente: download de mídia para disco, envio de arquivo local, busca e paginação."""

import base64
import json

import pytest

from evoapi_mcp.client import EvolutionAPIError, MAX_SEARCH_SCAN, SEARCH_PAGE_SIZE


def rec(msg_id, text, ts=1757600000, jid="5511999999999@s.whatsapp.net"):
    return {
        "key": {"remoteJid": jid, "fromMe": False, "id": msg_id},
        "pushName": "Fulano",
        "messageType": "conversation",
        "message": {"conversation": text},
        "messageTimestamp": ts,
    }


def v2(records, total=None, pages=1, page=1):
    return {"messages": {"total": total or len(records), "pages": pages, "currentPage": page, "records": records}}


# ---------------------------------------------------------------------------
# findMessages: payload e formato de saída
# ---------------------------------------------------------------------------

def test_get_messages_by_number_sends_v1_and_v2_fields(client):
    client.responses.append([{"remoteJid": "5511999999999@s.whatsapp.net"}])  # findChats (resolução do jid)
    client.responses.append(v2([rec("A", "oi")], total=1))
    out = client.get_messages_by_number("5511999999999", limit=10, page=2)

    assert client.calls[0]["endpoint"] == "/chat/findChats/{instanceId}"
    call = client.calls[1]
    assert call["endpoint"] == "/chat/findMessages/{instanceId}"
    assert out["chat"] == "5511999999999@s.whatsapp.net"
    assert call["data"]["where"] == {"key": {"remoteJid": "5511999999999@s.whatsapp.net"}}
    assert call["data"]["page"] == 2
    assert call["data"]["offset"] == 10
    assert call["data"]["limit"] == 10

    assert out["count"] == 1
    assert out["total"] == 1
    assert out["messages"][0] == {"id": "A", "ts": out["messages"][0]["ts"], "from": "5511999999999", "name": "Fulano", "type": "text", "text": "oi"}


def test_full_mode_returns_raw_records(client):
    raw = rec("A", "oi")
    client.responses.append(v2([raw]))
    out = client.find_messages(chat_id="x@s.whatsapp.net", compact=False)
    assert out["messages"][0] is raw


def test_local_search_filters_and_paginates(client):
    page1 = [rec(f"P1-{i}", "conversa normal") for i in range(SEARCH_PAGE_SIZE)]
    page2 = [rec("HIT", "Segue a NOTA fiscal"), rec("X", "outra")]
    client.responses.extend([v2(page1, total=102, pages=2, page=1), v2(page2, total=102, pages=2, page=2)])

    out = client.find_messages(query="nota fiscal", limit=5)

    assert [c["data"]["page"] for c in client.calls] == [1, 2]
    assert client.calls[0]["data"]["offset"] == SEARCH_PAGE_SIZE
    assert out["count"] == 1
    assert out["messages"][0]["id"] == "HIT"
    assert out["scanned"] == 102
    assert out["query"] == "nota fiscal"


def test_local_search_stops_at_limit(client):
    client.responses.append(v2([rec(f"H{i}", "pedido") for i in range(10)], pages=1))
    out = client.find_messages(query="pedido", limit=3)
    assert out["count"] == 3
    assert len(client.calls) == 1


def test_local_search_respects_scan_cap(client):
    pages = MAX_SEARCH_SCAN // SEARCH_PAGE_SIZE + 3
    for p in range(pages):
        client.responses.append(v2([rec(f"{p}-{i}", "nada") for i in range(SEARCH_PAGE_SIZE)], pages=pages, page=p + 1))
    out = client.find_messages(query="inexistente", limit=5)
    assert out["count"] == 0
    assert out["scanned"] == MAX_SEARCH_SCAN
    assert len(client.calls) == MAX_SEARCH_SCAN // SEARCH_PAGE_SIZE


# ---------------------------------------------------------------------------
# download_media
# ---------------------------------------------------------------------------

PDF_BYTES = b"%PDF-1.4 fake"


def test_download_media_saves_file_and_omits_base64(client, tmp_path):
    client.responses.append({
        "mediaType": "document",
        "fileName": "boleto setembro.pdf",
        "mimetype": "application/pdf",
        "base64": base64.b64encode(PDF_BYTES).decode(),
        "buffer": {"type": "Buffer", "data": list(PDF_BYTES)},
    })
    out = client.download_media("MSG1")

    assert client.calls[0]["endpoint"] == "/chat/getBase64FromMediaMessage/{instanceId}"
    assert client.calls[0]["data"]["message"] == {"key": {"id": "MSG1"}}

    path = tmp_path / "media" / "boleto setembro.pdf"
    assert path.read_bytes() == PDF_BYTES
    assert out["path"] == str(path)
    assert out["file"] == "boleto setembro.pdf"
    assert out["mime"] == "application/pdf"
    assert out["size"] == len(PDF_BYTES)
    assert out["type"] == "document"
    assert "base64" not in json.dumps(out)


def test_download_media_generates_name_and_avoids_overwrite(client, tmp_path):
    payload = {"mediaType": "image", "mimetype": "image/jpeg", "base64": base64.b64encode(b"img").decode()}
    client.responses.extend([dict(payload), dict(payload)])
    first = client.download_media("IMG1")
    second = client.download_media("IMG1")
    assert first["file"] == "IMG1.jpg"
    assert second["file"] == "IMG1_1.jpg"


def test_download_media_custom_dir_and_name(client, tmp_path):
    client.responses.append({"mimetype": "image/png", "base64": base64.b64encode(b"png").decode()})
    target = tmp_path / "docs"
    out = client.download_media("X", save_dir=str(target), filename="../../evil")
    assert out["file"] == "evil.png"  # sanitizado + extensão pelo mimetype
    assert (target / "evil.png").exists()


def test_download_media_without_base64_raises(client):
    client.responses.append({"mediaType": "document"})
    with pytest.raises(EvolutionAPIError):
        client.download_media("MSG1")


def test_download_media_extract_text_txt(client):
    client.responses.append({"fileName": "nota.txt", "mimetype": "text/plain", "base64": base64.b64encode("Valor: R$ 10,00".encode()).decode()})
    out = client.download_media("T", extract_text=True, max_chars=8)
    assert out["text"] == "Valor: R…"
    assert out["text_truncated"] is True


def test_download_media_extract_text_pdf(client):
    pypdf = pytest.importorskip("pypdf")
    import io

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    client.responses.append({"fileName": "b.pdf", "mimetype": "application/pdf", "base64": base64.b64encode(buf.getvalue()).decode()})
    out = client.download_media("P", extract_text=True)
    assert out["pages"] == 1
    assert "text_error" in out  # página em branco não tem texto


def test_download_media_extract_text_unsupported(client):
    client.responses.append({"mimetype": "image/png", "base64": base64.b64encode(b"x").decode()})
    out = client.download_media("I", extract_text=True)
    assert "text_error" in out and "text" not in out


# ---------------------------------------------------------------------------
# send_file / send_media_base64
# ---------------------------------------------------------------------------

def test_send_file_document(client, tmp_path):
    f = tmp_path / "relatório.pdf"
    f.write_bytes(PDF_BYTES)
    client.responses.append({"key": {"remoteJid": "5511999999999@s.whatsapp.net", "id": "S1"}, "status": "PENDING"})

    out = client.send_file("5511999999999", str(f), caption="segue")

    call = client.calls[0]
    assert call["endpoint"] == "/message/sendMedia/{instanceId}"
    assert call["data"]["mediatype"] == "document"
    assert call["data"]["fileName"] == "relatório.pdf"
    assert call["data"]["caption"] == "segue"
    assert call["data"]["mimetype"] == "application/pdf"
    assert base64.b64decode(call["data"]["media"]) == PDF_BYTES
    assert out["_file"]["size"] == len(PDF_BYTES)


def test_send_file_detects_image_and_audio(client, tmp_path):
    img = tmp_path / "foto.jpg"
    img.write_bytes(b"jpg")
    aud = tmp_path / "voz.ogg"
    aud.write_bytes(b"ogg")
    client.responses.extend([{}, {}])

    client.send_file("5511999999999", str(img))
    client.send_file("5511999999999", str(aud))

    assert client.calls[0]["data"]["mediatype"] == "image"
    assert client.calls[1]["endpoint"] == "/message/sendWhatsAppAudio/{instanceId}"
    assert base64.b64decode(client.calls[1]["data"]["audio"]) == b"ogg"


def test_send_file_missing(client, tmp_path):
    with pytest.raises(ValueError):
        client.send_file("5511999999999", str(tmp_path / "nao-existe.pdf"))


def test_send_media_base64_strips_data_prefix(client):
    client.responses.append({})
    client.send_media_base64("5511999999999", "data:image/png;base64,QUJD", "image", file_name="a.png")
    assert client.calls[0]["data"]["media"] == "QUJD"


# ---------------------------------------------------------------------------
# contatos
# ---------------------------------------------------------------------------

def test_get_contact_name_falls_back_to_bulk_map(client):
    client.responses.extend([
        [],  # filtro por id ignorado pela API
        [{"remoteJid": "5511999999999@s.whatsapp.net", "pushName": "Fulano"}],  # fetch_contacts completo
    ])
    assert client.get_contact_name("5511999999999") == "Fulano"


def test_instance_info_reads_nested_state(client):
    client.responses.append({"instance": {"instanceName": "inst", "state": "open"}})
    assert client.get_instance_info()["status"] == "open"


def test_instance_info_reads_flat_state(client):
    client.responses.append({"state": "connecting"})
    assert client.get_instance_info()["status"] == "connecting"


def test_download_media_explains_message_without_content(client):
    """A Evolution API devolve um TypeError quando o registro não tem `message`."""
    def boom(data):
        raise EvolutionAPIError(
            "HTTP 400: {\"message\":[\"TypeError: Cannot read properties of null (reading 'ephemeralMessage')\"]}"
        )
    client.responses.append(boom)
    with pytest.raises(EvolutionAPIError, match="salva sem conteúdo"):
        client.download_media("MSG1")
