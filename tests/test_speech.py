"""Texto para voz: escolha de backend, geração e envio como nota de voz."""

import sys
import types

import pytest

from evoapi_mcp.config import EvolutionConfig
from evoapi_mcp.speech import MAX_TEXT, Speaker, SpeechError


def make_config(tmp_path, **kwargs):
    base = dict(
        base_url="http://evolution.test",
        api_token="token",
        instance_name="inst",
        media_dir=str(tmp_path / "media"),
        transcribe_backend="off",
    )
    base.update(kwargs)
    return EvolutionConfig(_env_file=None, **base)


class FakeEdge:
    """Módulo edge_tts falso: grava bytes fixos e registra a voz usada."""

    def __init__(self):
        self.chamadas = []
        modulo = self

        class Communicate:
            def __init__(self, texto, voz):
                modulo.chamadas.append((texto, voz))

            async def save(self, destino):
                with open(destino, "wb") as f:
                    f.write(b"ID3fake-mp3")

        self.Communicate = Communicate


@pytest.fixture
def edge(monkeypatch):
    falso = FakeEdge()
    monkeypatch.setitem(sys.modules, "edge_tts", falso)
    return falso


@pytest.fixture
def sem_edge(monkeypatch):
    monkeypatch.setitem(sys.modules, "edge_tts", None)  # import falha


@pytest.fixture(autouse=True)
def sem_chaves(monkeypatch):
    for k in ("EVOLUTION_TTS_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- backend

def test_auto_usa_edge_quando_instalado(tmp_path, edge):
    s = Speaker(make_config(tmp_path))
    assert s.active_backend == "edge"
    assert s.voice == "pt-BR-FranciscaNeural"
    assert s.describe() == {"backend": "edge", "available": True, "voice": "pt-BR-FranciscaNeural"}


def test_auto_prefere_api_quando_ha_chave(tmp_path, edge):
    s = Speaker(make_config(tmp_path, tts_api_key="sk-x"))
    assert s.active_backend == "api"
    assert s.voice == "nova"
    assert s.describe()["model"] == "gpt-4o-mini-tts"


def test_chave_do_ambiente_conta(tmp_path, edge, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert Speaker(make_config(tmp_path)).active_backend == "api"


def test_sem_nada_fica_off_com_dica(tmp_path, sem_edge):
    s = Speaker(make_config(tmp_path))
    assert not s.available
    assert "edge-tts" in s.describe()["hint"]


def test_off_explicito(tmp_path, edge):
    s = Speaker(make_config(tmp_path, tts_backend="off"))
    assert not s.available
    with pytest.raises(SpeechError, match="indisponível"):
        s.synthesize("oi")


def test_voz_configurada_vence_o_padrao(tmp_path, edge):
    s = Speaker(make_config(tmp_path, tts_voice="pt-BR-AntonioNeural"))
    assert s.voice == "pt-BR-AntonioNeural"


# ---------------------------------------------------------------- síntese

def test_edge_gera_mp3_na_pasta_de_midia(tmp_path, edge):
    s = Speaker(make_config(tmp_path))
    p = s.synthesize("Oi Keilla, tudo bem?")
    assert p.parent == tmp_path / "media" / "voz"
    assert p.suffix == ".mp3"
    assert p.read_bytes().startswith(b"ID3")
    assert edge.chamadas == [("Oi Keilla, tudo bem?", "pt-BR-FranciscaNeural")]


def test_mesmo_texto_reaproveita_o_arquivo(tmp_path, edge):
    s = Speaker(make_config(tmp_path))
    a = s.synthesize("repetido")
    b = s.synthesize("repetido")
    assert a == b
    assert len(edge.chamadas) == 1


def test_voz_diferente_gera_outro_arquivo(tmp_path, edge):
    s = Speaker(make_config(tmp_path))
    a = s.synthesize("texto", voice="pt-BR-AntonioNeural")
    b = s.synthesize("texto")
    assert a != b
    assert edge.chamadas[0][1] == "pt-BR-AntonioNeural"


def test_texto_vazio_e_longo_sao_recusados(tmp_path, edge):
    s = Speaker(make_config(tmp_path))
    with pytest.raises(SpeechError, match="vazio"):
        s.synthesize("   ")
    with pytest.raises(SpeechError, match="máximo"):
        s.synthesize("a" * (MAX_TEXT + 1))
    assert edge.chamadas == []


def test_falha_do_edge_vira_speecherror_sem_deixar_lixo(tmp_path, edge):
    async def explode(destino):
        raise RuntimeError("serviço fora")

    edge.Communicate.save = lambda self, destino: explode(destino)
    s = Speaker(make_config(tmp_path))
    with pytest.raises(SpeechError, match="serviço fora"):
        s.synthesize("oi")
    assert not list((tmp_path / "media" / "voz").glob("*.mp3"))


def test_api_manda_o_pedido_certo(tmp_path, monkeypatch):
    pedidos = []

    class Resp:
        status_code = 200
        content = b"ID3api"
        text = ""

    def fake_post(url, headers=None, json=None, timeout=None):
        pedidos.append({"url": url, "headers": headers, "json": json})
        return Resp()

    monkeypatch.setattr("requests.post", fake_post)
    s = Speaker(make_config(tmp_path, tts_api_key="sk-x"))
    p = s.synthesize("Bom dia")
    assert p.read_bytes() == b"ID3api"
    pedido = pedidos[0]
    assert pedido["url"] == "https://api.openai.com/v1/audio/speech"
    assert pedido["headers"]["Authorization"] == "Bearer sk-x"
    assert pedido["json"]["input"] == "Bom dia"
    assert pedido["json"]["voice"] == "nova"
    assert pedido["json"]["response_format"] == "mp3"
    assert "português do Brasil" in pedido["json"]["instructions"]


def test_api_com_erro_http(tmp_path, monkeypatch):
    class Resp:
        status_code = 401
        content = b""
        text = "invalid key"

    monkeypatch.setattr("requests.post", lambda *a, **k: Resp())
    s = Speaker(make_config(tmp_path, tts_api_key="sk-x"))
    with pytest.raises(SpeechError, match="401"):
        s.synthesize("oi")


# ---------------------------------------------------------------- envio

def test_client_send_voice_envia_como_nota_de_voz(client, edge):
    client.responses = [{"key": {"id": "V1", "remoteJid": "5584999290327@s.whatsapp.net"}}]
    r = client.send_voice("5584999290327", "Oi, tudo bem?")
    chamada = client.calls[-1]
    assert chamada["endpoint"] == "/message/sendWhatsAppAudio/{instanceId}"
    assert chamada["data"]["number"] == "5584999290327"
    assert chamada["data"]["audio"]  # base64 do mp3
    assert r["_voice"]["backend"] == "edge"
    assert r["_voice"]["voice"] == "pt-BR-FranciscaNeural"
    assert r["_voice"]["chars"] == len("Oi, tudo bem?")


def test_client_send_voice_sem_backend_levanta(client, sem_edge):
    with pytest.raises(SpeechError):
        client.send_voice("5584999290327", "oi")
    assert client.calls == []
