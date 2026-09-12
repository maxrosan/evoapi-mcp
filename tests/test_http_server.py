"""API REST: as mesmas capacidades das tools MCP, para quem não fala MCP.

Estes testes existem porque as duas superfícies vinham divergindo em silêncio —
uma tool nova entrava no server.py e o http_server.py ficava para trás.
"""

import base64
import io

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from evoapi_mcp import http_server  # noqa: E402


@pytest.fixture
def api(client, monkeypatch):
    """App REST apontando para o cliente Evolution simulado do conftest."""
    monkeypatch.setattr(http_server, "client", client)
    return fastapi_testclient.TestClient(http_server.app)


def test_every_send_tool_has_an_endpoint(api):
    """Guarda contra a divergência: envio que existe no MCP existe aqui."""
    rotas = {r.path for r in http_server.app.routes}
    assert {
        "/messages/text", "/messages/media", "/messages/file", "/messages/base64",
        "/messages/render", "/messages/url", "/messages/drive",
        "/media/download", "/media/transcribe", "/media/archive", "/media/view",
        "/media/cleanup",
    } <= rotas


def test_render_endpoint_rasterizes_and_sends(api, client):
    pytest.importorskip("cairosvg")
    client.responses.append({"key": {"remoteJid": "5511999999999@s.whatsapp.net", "id": "S1"}})

    r = api.post("/messages/render", json={
        "number": "5511999999999",
        "svg": '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20"/></svg>',
        "caption": "olha",
        "width": 80,
    })

    assert r.status_code == 200
    assert r.json()["file"]["type"] == "image"
    enviado = base64.b64decode(client.calls[0]["data"]["media"])
    assert enviado[:4] == b"\x89PNG"


def test_render_endpoint_reports_a_bad_svg(api):
    pytest.importorskip("cairosvg")
    r = api.post("/messages/render", json={"number": "5511999999999", "svg": "isto não é svg"})
    assert r.status_code == 422
    assert "SVG" in r.json()["detail"]


def test_url_endpoint_refuses_an_internal_address(api):
    r = api.post("/messages/url", json={
        "number": "5511999999999", "url": "http://127.0.0.1:8080/segredo",
    })
    assert r.status_code == 422
    assert "interno" in r.json()["detail"]


def test_drive_endpoint_says_when_drive_is_not_configured(api):
    r = api.post("/messages/drive", json={"number": "5511999999999", "file_ref": "F1"})
    assert r.status_code == 422


def test_cleanup_endpoint_reports_what_it_would_remove(api, config, tmp_path):
    import os, time
    from pathlib import Path

    media = Path(config.media_dir)
    (media / "web").mkdir(parents=True, exist_ok=True)
    velho = media / "web" / "antigo.png"
    velho.write_bytes(b"x" * 10)
    quando = time.time() - 60 * 86400
    os.utime(velho, (quando, quando))

    r = api.post("/media/cleanup", json={"days": 30, "dry_run": True})

    assert r.status_code == 200 and r.json()["removed"] == 1
    assert velho.exists()  # dry_run não apaga


def test_view_endpoint_returns_the_pages_as_base64(api, client):
    pytest.importorskip("PIL")
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (10, 120, 90)).save(buf, format="PNG")
    client.responses.append({
        "mediaType": "image", "fileName": "comprovante.png", "mimetype": "image/png",
        "base64": base64.b64encode(buf.getvalue()).decode(),
    })

    r = api.post("/media/view", json={"message_id": "MSG1"})

    assert r.status_code == 200
    corpo = r.json()
    assert corpo["pages_rendered"] == 1 and corpo["format"] == "jpeg"
    assert base64.b64decode(corpo["images"][0])[:2] == b"\xff\xd8"  # JPEG


def test_instance_status_includes_disk_usage(api, client):
    client.responses.append({"instance": {"state": "open"}})
    corpo = api.get("/instance/status").json()
    assert corpo["status"] == "open"
    assert "media" in corpo and "files" in corpo["media"]
