"""Arquivos da conversa: filtro por tipo, anexos recentes, pasta de arquivos no Drive e hora local."""

from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from evoapi_mcp.client import SEARCH_PAGE_SIZE
from evoapi_mcp.drive import DriveClient, DriveError
from evoapi_mcp.formatters import fmt_ts, message_kind_matches, set_display_timezone
from test_client import rec, v2
from test_drive import FakeResponse

GRUPO = "120363325369950073@g.us"
TS = 1757600000


def _base(mid, de, tipo, mensagem, ts=TS):
    return {
        "key": {"remoteJid": GRUPO, "fromMe": False, "id": mid, "participant": "558400000001@s.whatsapp.net"},
        "pushName": de, "messageType": tipo, "message": mensagem, "messageTimestamp": ts,
    }


def doc(mid, nome, mime="application/pdf", de="Alexandre"):
    return _base(mid, de, "documentMessage", {"documentMessage": {"fileName": nome, "mimetype": mime}})


def imagem(mid, legenda=None, de="Vitório"):
    corpo = {"mimetype": "image/jpeg"}
    if legenda:
        corpo["caption"] = legenda
    return _base(mid, de, "imageMessage", {"imageMessage": corpo})


def audio(mid, de="Vitório"):
    return _base(mid, de, "audioMessage", {"audioMessage": {"mimetype": "audio/ogg", "ptt": True}})


# ---------------------------------------------------------------- filtro por tipo

@pytest.mark.parametrize("compacta,tipo,esperado", [
    ({"type": "document", "mime": "application/pdf"}, "pdf", True),
    ({"type": "document", "file": "CNO.PDF"}, "pdf", True),
    ({"type": "document", "file": "planilha.xlsx", "mime": "application/vnd.ms-excel"}, "pdf", False),
    ({"type": "image"}, "anexo", True),
    ({"type": "audio"}, "anexo", False),
    ({"type": "image"}, "foto", True),
    ({"type": "document"}, "documento", True),
    ({"type": "text"}, "text", True),
    ({"type": "text"}, None, True),
])
def test_message_kind_matches(compacta, tipo, esperado):
    assert message_kind_matches(compacta, tipo) is esperado


def test_find_messages_por_tipo_pdf(client):
    client.responses.append(v2([
        audio("A1"), doc("D2", "CNO.pdf"), rec("T1", "oi", jid=GRUPO),
        doc("X1", "planilha.xlsx", mime="application/vnd.ms-excel"), doc("D1", "Certidao Negativa Obra.pdf"),
    ]))
    out = client.find_messages(chat_id=GRUPO, kind="pdf", limit=2)
    assert [m["id"] for m in out["messages"]] == ["D2", "D1"]
    assert out["type"] == "pdf"
    assert client.calls[0]["data"]["where"] == {"key": {"remoteJid": GRUPO}}


# ---------------------------------------------------------------- anexos recentes

def test_recent_attachments_de_qualquer_remetente(client):
    client.responses.append(v2([
        audio("A1"), imagem("I1", legenda="foto da obra"), rec("T1", "texto", jid=GRUPO),
        doc("D2", "CNO.pdf"), doc("D1", "Certidao Negativa Obra.pdf", de="Nícolas"),
    ]))
    anexos = client.recent_attachments(GRUPO, limit=3)
    assert [a["id"] for a in anexos] == ["I1", "D2", "D1"]
    assert anexos[0] == {"id": "I1", "tipo": "image", "mime": "image/jpeg", "legenda": "foto da obra",
                         "de": "Vitório", "quando": fmt_ts(TS)}
    assert anexos[1]["arquivo"] == "CNO.pdf"
    assert "legenda" not in anexos[1]           # legenda igual ao nome do arquivo é ruído
    assert anexos[2]["de"] == "Nícolas"
    assert client.calls[0]["endpoint"] == "/chat/findMessages/{instanceId}"   # jid direto, sem findChats


def test_recent_attachments_respeita_a_varredura_maxima(client):
    for pagina in range(1, 4):
        client.responses.append(v2([rec(f"{pagina}-{i}", "só texto", jid=GRUPO) for i in range(SEARCH_PAGE_SIZE)],
                                   pages=5, page=pagina))
    assert client.recent_attachments(GRUPO, limit=6, max_scan=150) == []
    assert len(client.calls) == 2


# ---------------------------------------------------------------- guardar no Drive

class DriveFalso:
    available = True

    def __init__(self):
        self.enviados = []

    def upload_file(self, path, name=None, folder=None, mime=None, base="financeiro"):
        self.enviados.append({"path": path, "name": name, "folder": folder, "base": base})
        n = len(self.enviados)
        return {"id": f"G{n}", "name": name, "folder": f"ARQUIVOS/{folder}", "size": 10,
                "link": f"https://drive.test/G{n}", "path": path}


def test_save_to_drive_guarda_na_pasta_de_arquivos(client, monkeypatch, tmp_path):
    client.drive = DriveFalso()
    baixados = {"D1": tmp_path / "CNO.pdf", "D2": tmp_path / "Certidao.pdf"}

    def download_media(message_id, password=None):
        if message_id == "RUIM":
            raise RuntimeError("mídia expirada")
        return {"path": str(baixados[message_id]), "file": baixados[message_id].name}

    monkeypatch.setattr(client, "download_media", download_media)
    out = client.save_to_drive(message_ids=["D1", "D2", "RUIM"], folder="Inverto Currais Novos")
    assert out["count"] == 2
    assert [a["link"] for a in out["arquivos"]] == ["https://drive.test/G1", "https://drive.test/G2"]
    assert [a["message_id"] for a in out["arquivos"]] == ["D1", "D2"]
    assert all("path" not in a for a in out["arquivos"])
    assert {e["base"] for e in client.drive.enviados} == {"arquivos"}
    assert client.drive.enviados[0]["folder"] == "Inverto Currais Novos"
    assert out["erros"] == [{"message_id": "RUIM", "erro": "mídia expirada"}]


def test_save_to_drive_nome_final_so_para_um_arquivo(client, monkeypatch, tmp_path):
    client.drive = DriveFalso()
    monkeypatch.setattr(client, "download_media", lambda mid, password=None: {"path": str(tmp_path / "x.pdf"), "file": "x.pdf"})
    client.save_to_drive(message_ids=["D1"], filename="Manual do Docente.pdf")
    assert client.drive.enviados[0]["name"] == "Manual do Docente.pdf"


def test_save_to_drive_sem_drive_ou_sem_origem(client):
    client.drive = SimpleNamespace(available=False)
    with pytest.raises(DriveError):
        client.save_to_drive(message_ids=["D1"])
    client.drive = DriveFalso()
    with pytest.raises(ValueError):
        client.save_to_drive()


# ---------------------------------------------------------------- base de arquivos no Drive

def drive_client(**kw):
    config = SimpleNamespace(drive_client_id="c", drive_client_secret="s", drive_refresh_token="r",
                             drive_root="", drive_root_id="FIN", timeout=5, **kw)
    d = DriveClient(config)
    d._require = lambda: None
    d._headers = lambda: {}
    return d


def test_pasta_de_arquivos_ao_lado_da_financeira():
    d = drive_client()
    criadas = []
    d._parent_of = lambda file_id: "CLAUDE"
    d.find_child = lambda name, parent, folder_only=False: None
    d.create_folder = lambda name, parent: criadas.append((name, parent)) or "ARQ"
    assert d.files_base_id() == "ARQ"
    assert d.files_base_id() == "ARQ"
    assert criadas == [("ARQUIVOS", "CLAUDE")]


def test_sem_acesso_a_pasta_mae_usa_a_raiz():
    d = drive_client()

    def negado(file_id):
        raise DriveError("403 insufficient permissions")

    criadas = []
    d._parent_of = negado
    d.find_child = lambda name, parent, folder_only=False: None
    d.create_folder = lambda name, parent: criadas.append((name, parent)) or f"{parent}/{name}"
    assert d.files_base_id() == "root/ARQUIVOS"
    assert criadas == [("ARQUIVOS", "root")]


def test_id_configurado_vence():
    d = drive_client(drive_files_root_id="CFG")
    d._parent_of = lambda file_id: pytest.fail("não deveria consultar a pasta-mãe")
    assert d.files_base_id() == "CFG"


def test_upload_na_base_de_arquivos(monkeypatch, tmp_path):
    d = drive_client()
    d.files_base_id = lambda: "ARQ"
    pedidos = []
    d.ensure_folder = lambda path, create=True, base_id=None: pedidos.append((path, base_id)) or "PASTA"
    monkeypatch.setattr("evoapi_mcp.drive.requests.post",
                        lambda *a, **k: FakeResponse({"id": "F1", "name": "CNO.pdf", "webViewLink": "https://l"}))
    arquivo = tmp_path / "CNO.pdf"
    arquivo.write_bytes(b"%PDF")
    out = d.upload_file(arquivo, folder="FAS", base="arquivos")
    assert pedidos == [("FAS", "ARQ")]
    assert out["folder"] == "ARQUIVOS/FAS"
    assert out["link"] == "https://l"


def test_upload_financeiro_continua_igual(monkeypatch, tmp_path):
    d = drive_client()
    pedidos = []
    d.ensure_folder = lambda path, create=True, base_id=None: pedidos.append((path, base_id)) or "PASTA"
    monkeypatch.setattr("evoapi_mcp.drive.requests.post",
                        lambda *a, **k: FakeResponse({"id": "F1", "name": "b.pdf", "webViewLink": "https://l"}))
    arquivo = tmp_path / "b.pdf"
    arquivo.write_bytes(b"%PDF")
    d.upload_file(arquivo, folder="MR/2026/09.2026/BOLETO")
    assert pedidos == [("MR/2026/09.2026/BOLETO", None)]


# ---------------------------------------------------------------- hora local

def test_hora_local_quando_configurada():
    try:
        set_display_timezone("America/Fortaleza")
        esperado = datetime.fromtimestamp(TS, timezone.utc).astimezone(ZoneInfo("America/Fortaleza"))
        assert fmt_ts(TS) == esperado.strftime("%Y-%m-%d %H:%M")
        assert fmt_ts("2025-09-11T14:13:20Z") == "2025-09-11 11:13"
    finally:
        set_display_timezone(None)
    assert fmt_ts(TS) == datetime.fromtimestamp(TS).strftime("%Y-%m-%d %H:%M")
