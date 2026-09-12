"""Upload para o Google Drive feito pelo servidor."""

import json

import pytest

from evoapi_mcp.config import EvolutionConfig
from evoapi_mcp.drive import SCOPE, DriveClient, DriveError


def make_config(tmp_path, **kwargs):
    base = dict(
        base_url="http://evolution.test", api_token="t", instance_name="i",
        media_dir=str(tmp_path / "media"), transcribe_backend="off",
    )
    base.update(kwargs)
    return EvolutionConfig(_env_file=None, **base)


def creds(tmp_path, **kw):
    return make_config(
        tmp_path,
        drive_client_id="cid", drive_client_secret="sec", drive_refresh_token="ref",
        **kw,
    )


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture
def drive(tmp_path, monkeypatch):
    """DriveClient com HTTP simulado; as chamadas ficam em `drive.calls`."""
    d = DriveClient(creds(tmp_path, drive_root_id="ROOT"))
    d.calls = []
    d.queue = []

    def fake(method):
        def call(url, **kw):
            d.calls.append({"method": method, "url": url, **kw})
            if url.endswith("/token"):
                return FakeResponse({"access_token": "tok", "expires_in": 3600})
            if not d.queue:
                return FakeResponse({})
            item = d.queue.pop(0)
            return item if isinstance(item, FakeResponse) else FakeResponse(item)
        return call

    monkeypatch.setattr("evoapi_mcp.drive.requests.get", fake("GET"))
    monkeypatch.setattr("evoapi_mcp.drive.requests.post", fake("POST"))
    return d


# ---------------------------------------------------------------------------
# configuração
# ---------------------------------------------------------------------------

def test_unconfigured_explains_itself(tmp_path):
    d = DriveClient(make_config(tmp_path))
    assert not d.available
    assert "EVOLUTION_DRIVE_CLIENT_ID" in d.describe()["hint"]
    with pytest.raises(DriveError, match="não configurado"):
        d.ensure_folder("X")


def test_scope_is_not_restricted():
    """`drive` é escopo restrito e forçaria verificação do Google; drive.file não."""
    assert SCOPE.endswith("/auth/drive.file")


def test_describe_reports_scope_and_root(tmp_path):
    d = DriveClient(creds(tmp_path, drive_root_id="ROOT", drive_root="FINANCEIRO"))
    info = d.describe()
    assert info["available"] is True
    assert info["scope"] == SCOPE
    assert info["root_id"] == "ROOT"
    assert "hint" not in info


# ---------------------------------------------------------------------------
# token
# ---------------------------------------------------------------------------

def test_token_is_reused_until_expiry(drive):
    drive.queue.append({"files": []})
    drive.queue.append({"id": "F1"})
    drive.ensure_folder("A")
    tokens = [c for c in drive.calls if c["url"].endswith("/token")]
    assert len(tokens) == 1
    assert tokens[0]["data"]["grant_type"] == "refresh_token"

    drive.queue.append({"files": [{"id": "F1"}]})
    drive._folder_cache.clear()
    drive.ensure_folder("A")
    assert len([c for c in drive.calls if c["url"].endswith("/token")]) == 1


def test_revoked_refresh_token_is_explained(tmp_path, monkeypatch):
    d = DriveClient(creds(tmp_path))
    monkeypatch.setattr(
        "evoapi_mcp.drive.requests.post",
        lambda *a, **k: FakeResponse({"error": "invalid_grant"}, status=400),
    )
    with pytest.raises(DriveError, match="google_oauth_setup"):
        d.ensure_folder("X")


# ---------------------------------------------------------------------------
# pastas
# ---------------------------------------------------------------------------

def test_ensure_folder_creates_missing_levels(drive):
    drive.queue.extend([
        {"files": [{"id": "MR"}]},   # MR existe
        {"files": []},               # 2026 não existe
        {"id": "Y26"},               # cria 2026
        {"files": []},               # BOLETO não existe
        {"id": "BOL"},               # cria BOLETO
    ])
    assert drive.ensure_folder("MR/2026/BOLETO") == "BOL"

    buscas = [c for c in drive.calls if c["method"] == "GET"]
    assert "'ROOT' in parents" in buscas[0]["params"]["q"]
    assert "mimeType = 'application/vnd.google-apps.folder'" in buscas[0]["params"]["q"]
    criadas = [c["json"]["name"] for c in drive.calls if c["method"] == "POST" and "json" in c]
    assert criadas == ["2026", "BOLETO"]


def test_ensure_folder_uses_cache(drive):
    drive.queue.extend([{"files": [{"id": "A1"}]}])
    assert drive.ensure_folder("A") == "A1"
    antes = len(drive.calls)
    assert drive.ensure_folder("A") == "A1"
    assert len(drive.calls) == antes  # nenhuma chamada nova


def test_empty_path_is_the_root(drive):
    assert drive.ensure_folder("") == "ROOT"
    assert drive.calls == []


def test_folder_name_with_quote_is_escaped(drive):
    drive.queue.extend([{"files": [{"id": "X"}]}])
    drive.ensure_folder("O'Brien")
    assert "O\\'Brien" in drive.calls[-1]["params"]["q"]


def test_missing_folder_without_create(drive):
    drive.queue.append({"files": []})
    with pytest.raises(DriveError, match="não encontrada"):
        drive.ensure_folder("Sumida", create=False)


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------

def test_upload_sends_metadata_and_content(drive, tmp_path):
    f = tmp_path / "boleto.pdf"
    f.write_bytes(b"%PDF-1.4 conteudo")
    drive.queue.extend([
        {"files": [{"id": "BOL"}]},
        {"id": "FILE1", "name": "17.08.2026 - Econtec - R$ 350,00.pdf",
         "webViewLink": "https://drive.google.com/file/d/FILE1/view"},
    ])

    out = drive.upload_file(f, name="17.08.2026 - Econtec - R$ 350,00.pdf", folder="BOLETO")

    envio = drive.calls[-1]
    assert envio["url"].startswith("https://www.googleapis.com/upload/")
    assert envio["params"]["uploadType"] == "multipart"
    meta = json.loads(envio["files"]["metadata"][1])
    assert meta["parents"] == ["BOL"]
    assert meta["name"].startswith("17.08.2026")
    assert envio["files"]["file"][1] == b"%PDF-1.4 conteudo"
    assert envio["files"]["file"][2] == "application/pdf"

    assert out["id"] == "FILE1"
    assert out["link"].endswith("/view")
    assert out["size"] == 17


def test_upload_rejects_missing_file(drive, tmp_path):
    with pytest.raises(DriveError, match="não encontrado"):
        drive.upload_file(tmp_path / "nao-existe.pdf")


def test_upload_rejects_oversized_file(drive, tmp_path):
    import evoapi_mcp.drive as mod

    f = tmp_path / "grande.bin"
    f.write_bytes(b"0" * 1024)
    mod_max = mod.MAX_SIMPLE_UPLOAD
    try:
        mod.MAX_SIMPLE_UPLOAD = 100
        with pytest.raises(DriveError, match="excede o limite"):
            drive.upload_file(f)
    finally:
        mod.MAX_SIMPLE_UPLOAD = mod_max


def test_api_error_is_readable(drive, tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"x")
    drive.queue.append(FakeResponse({"error": {"message": "File not found: ROOT"}}, status=404))
    with pytest.raises(DriveError, match="File not found"):
        drive.upload_file(f, folder="")


# ---------------------------------------------------------------------------
# integração com o cliente do WhatsApp
# ---------------------------------------------------------------------------

def test_archive_media_downloads_then_uploads(client, tmp_path, monkeypatch):
    import base64

    client.drive = DriveClient(creds(tmp_path, drive_root_id="ROOT"))
    enviados = {}

    def fake_upload(file_path, name=None, folder=None, mime=None):
        enviados.update(path=str(file_path), name=name, folder=folder, mime=mime)
        return {"id": "F1", "name": name, "folder": folder, "size": 4, "link": "http://x"}

    monkeypatch.setattr(client.drive, "upload_file", fake_upload)
    client.responses.append({
        "mediaType": "document", "fileName": "orig.pdf", "mimetype": "application/pdf",
        "base64": base64.b64encode(b"%PDF").decode(),
    })

    out = client.archive_media("MSG1", folder="MR/2026/BOLETO", filename="novo.pdf")

    assert enviados["folder"] == "MR/2026/BOLETO"
    assert enviados["name"] == "novo.pdf"
    assert enviados["mime"] == "application/pdf"
    assert out["source"]["message_id"] == "MSG1"
    assert "base64" not in json.dumps(out)


def test_archive_media_without_credentials(client):
    with pytest.raises(DriveError, match="não configurado"):
        client.archive_media("MSG1", folder="X")
