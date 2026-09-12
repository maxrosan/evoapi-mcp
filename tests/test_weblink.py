"""Arquivo que já existe na web: link no lugar do base64."""

import base64

import pytest

from evoapi_mcp import weblink
from evoapi_mcp.weblink import MAX_FETCH_BYTES, WebLinkError, normalize

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeResponse:
    def __init__(self, content=PNG, status=200, headers=None, location=None):
        self.status_code = status
        self.headers = dict(headers or {})
        if location:
            self.status_code = 302
            self.headers["Location"] = location
        self._content = content
        self.closed = False

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308)

    is_permanent_redirect = is_redirect

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]

    def close(self):
        self.closed = True


@pytest.fixture
def web(monkeypatch):
    """Rede simulada: `web.respostas` é consumida em ordem e `web.urls` registra o que foi pedido."""
    class Rede:
        def __init__(self):
            self.respostas = []
            self.urls = []

    rede = Rede()

    def fake_get(url, **kwargs):
        rede.urls.append(url)
        return rede.respostas.pop(0)

    # Todo host resolve para um IP público, menos os que o próprio teste quiser interno.
    def fake_getaddrinfo(host, port):
        ip = "127.0.0.1" if host in ("localhost", "interno.test") else "93.184.216.34"
        return [(2, 1, 6, "", (ip, port))]

    monkeypatch.setattr(weblink.requests, "get", fake_get)
    monkeypatch.setattr(weblink.socket, "getaddrinfo", fake_getaddrinfo)
    return rede


# ---------------------------------------------------------------------------
# link de compartilhamento -> link do arquivo
# ---------------------------------------------------------------------------

def test_normalize_google_drive():
    direto = "https://drive.google.com/uc?export=download&id=1AbC_dEf-123"
    assert normalize("https://drive.google.com/file/d/1AbC_dEf-123/view?usp=sharing") == direto
    assert normalize("https://drive.google.com/open?id=1AbC_dEf-123") == direto
    assert normalize("https://docs.google.com/uc?id=1AbC_dEf-123") == direto


def test_normalize_dropbox_asks_for_the_file():
    assert normalize("https://www.dropbox.com/s/abc/foto.png?dl=0").endswith("?dl=1")


def test_normalize_leaves_a_direct_url_alone():
    assert normalize("https://exemplo.com/foto.png") == "https://exemplo.com/foto.png"


# ---------------------------------------------------------------------------
# o que o servidor recusa buscar
# ---------------------------------------------------------------------------

def test_fetch_refuses_internal_addresses(web):
    with pytest.raises(WebLinkError, match="Endereço interno"):
        weblink.fetch("http://localhost:8080/segredo")
    assert not web.urls


def test_fetch_refuses_other_schemes(web):
    with pytest.raises(WebLinkError, match="http e https"):
        weblink.fetch("file:///etc/passwd")


def test_fetch_checks_every_redirect_hop(web):
    """Um 302 não pode ser a porta dos fundos para a rede interna."""
    web.respostas.append(FakeResponse(location="http://interno.test/metadata"))
    with pytest.raises(WebLinkError, match="Endereço interno"):
        weblink.fetch("https://exemplo.com/foto.png")


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

def test_fetch_follows_redirects_and_reads_the_name(web):
    web.respostas.append(FakeResponse(location="https://cdn.exemplo.com/x"))
    web.respostas.append(FakeResponse(headers={
        "Content-Type": "image/png",
        "Content-Disposition": 'attachment; filename="gráfico final.png"',
    }))

    out = weblink.fetch("https://exemplo.com/foto")

    assert out["content"] == PNG
    assert out["mime"] == "image/png"
    assert out["file_name"] == "gráfico final.png"
    assert out["url"] == "https://cdn.exemplo.com/x"


def test_fetch_falls_back_to_the_name_in_the_url(web):
    web.respostas.append(FakeResponse(headers={"Content-Type": "image/png"}))
    assert weblink.fetch("https://exemplo.com/pasta/foto%20nova.png")["file_name"] == "foto nova.png"


def test_fetch_explains_a_private_drive_link(web):
    web.respostas.append(FakeResponse(content=b"<html>Fa\xc3\xa7a login</html>",
                                      headers={"Content-Type": "text/html; charset=utf-8"}))
    with pytest.raises(WebLinkError, match="qualquer pessoa com o link"):
        weblink.fetch("https://drive.google.com/file/d/XYZ/view")
    # e foi buscar o link de download, não a página de compartilhamento
    assert web.urls == ["https://drive.google.com/uc?export=download&id=XYZ"]


def test_fetch_reports_http_errors(web):
    web.respostas.append(FakeResponse(status=404))
    with pytest.raises(WebLinkError, match="404"):
        weblink.fetch("https://exemplo.com/sumiu.png")


def test_fetch_respects_the_size_cap(web):
    web.respostas.append(FakeResponse(headers={
        "Content-Type": "video/mp4", "Content-Length": str(MAX_FETCH_BYTES + 1),
    }))
    with pytest.raises(WebLinkError, match="acima do limite"):
        weblink.fetch("https://exemplo.com/filme.mp4")


def test_fetch_stops_a_body_that_lies_about_its_size(web):
    """Sem Content-Length o teto vale do mesmo jeito, contando o que chega."""
    web.respostas.append(FakeResponse(content=b"x" * 5000, headers={"Content-Type": "image/png"}))
    with pytest.raises(WebLinkError, match="passou do limite"):
        weblink.fetch("https://exemplo.com/grande.png", max_bytes=1024)


# ---------------------------------------------------------------------------
# envio
# ---------------------------------------------------------------------------

def test_send_url_downloads_then_sends(client, config, web):
    web.respostas.append(FakeResponse(headers={"Content-Type": "image/png"}))
    client.responses.append({"key": {"remoteJid": "5511999999999@s.whatsapp.net", "id": "S1"}})

    out = client.send_url("5511999999999", "https://exemplo.com/cartaz.png", caption="chegou")

    call = client.calls[0]
    assert call["endpoint"] == "/message/sendMedia/{instanceId}"
    assert call["data"]["mediatype"] == "image"  # deduzido do Content-Type
    assert call["data"]["fileName"] == "cartaz.png"
    assert call["data"]["caption"] == "chegou"
    assert base64.b64decode(call["data"]["media"]) == PNG

    from pathlib import Path
    salvo = Path(out["_file"]["path"])
    assert salvo.parent == Path(config.media_dir) / "web"
    assert salvo.read_bytes() == PNG


def test_send_url_sends_a_pdf_as_document(client, web):
    web.respostas.append(FakeResponse(content=b"%PDF-1.4", headers={"Content-Type": "application/pdf"}))
    client.responses.append({})

    client.send_url("5511999999999", "https://exemplo.com/boleto.pdf")

    assert client.calls[0]["data"]["mediatype"] == "document"
    assert client.calls[0]["data"]["mimetype"] == "application/pdf"
