"""Transcrição de áudios do WhatsApp."""

import base64
import json
from pathlib import Path

import pytest

from evoapi_mcp.config import EvolutionConfig
from evoapi_mcp.formatters import compact_message
from evoapi_mcp.transcription import TranscriptionError, Transcriber, is_transcribable

OGG = b"OggS fake voice note"


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


class StubTranscriber:
    """Backend falso: registra as chamadas e devolve um texto fixo."""

    def __init__(self, text="Bom dia, segue o combinado.", error=None):
        self.text = text
        self.error = error
        self.calls = []

    def transcribe(self, path, language=None, segments=False):
        self.calls.append({"path": Path(path), "language": language, "segments": segments})
        if self.error:
            raise TranscriptionError(self.error)
        saida = {"text": self.text, "backend": "stub", "model": "stub-1", "language": "pt", "seconds": 4.2}
        if segments:
            saida["segments"] = [{"t": 0.0, "tempo": "0:00", "fim": 4.2, "texto": self.text}]
        return saida

    def describe(self):
        return {"backend": "stub", "available": True, "model": "stub-1"}


@pytest.fixture
def audio_client(client):
    client.transcriber = StubTranscriber()
    return client


def audio_payload(mime="audio/ogg; codecs=opus", name=None):
    payload = {"mediaType": "audio", "mimetype": mime, "base64": base64.b64encode(OGG).decode()}
    if name:
        payload["fileName"] = name
    return payload


# ---------------------------------------------------------------------------
# formatters
# ---------------------------------------------------------------------------

def test_voice_note_is_flagged_in_compact_message():
    record = {
        "key": {"remoteJid": "5511999999999@s.whatsapp.net", "fromMe": False, "id": "A1"},
        "pushName": "Fulano",
        "messageType": "audioMessage",
        "message": {"audioMessage": {"mimetype": "audio/ogg; codecs=opus", "seconds": 7, "ptt": True, "fileLength": "5120", "mediaKey": "k" * 80}},
        "messageTimestamp": 1789170336,
    }
    c = compact_message(record)
    assert c["type"] == "audio"
    assert c["voice"] is True
    assert c["seconds"] == 7
    assert c["size"] == 5120
    assert "mediaKey" not in json.dumps(c)


def test_music_file_is_not_flagged_as_voice():
    record = {
        "key": {"remoteJid": "1@s.whatsapp.net", "id": "A2"},
        "messageType": "audioMessage",
        "message": {"audioMessage": {"mimetype": "audio/mpeg", "seconds": 180}},
        "messageTimestamp": 1789170336,
    }
    assert "voice" not in compact_message(record)


def test_is_transcribable():
    assert is_transcribable("audio/ogg; codecs=opus")
    assert is_transcribable("video/mp4")
    assert is_transcribable(None, Path("nota.opus"))
    assert not is_transcribable("application/pdf")
    assert not is_transcribable(None, Path("boleto.pdf"))


# ---------------------------------------------------------------------------
# seleção de backend
# ---------------------------------------------------------------------------

def test_backend_off_reports_reason(tmp_path, monkeypatch):
    for var in ("EVOLUTION_TRANSCRIBE_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    t = Transcriber(make_config(tmp_path, transcribe_backend="off"))
    assert not t.available
    assert t.describe()["backend"] == "off"
    with pytest.raises(TranscriptionError, match="desligada"):
        t.transcribe(tmp_path / "x.ogg")


def test_auto_backend_prefers_api_when_key_present(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    t = Transcriber(make_config(tmp_path, transcribe_backend="auto", transcribe_api_key="sk-test"))
    assert t.active_backend == "api"
    assert t.model == "whisper-1"
    assert "sk-test" not in json.dumps(t.describe())


def test_auto_backend_reads_key_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    cfg = make_config(tmp_path, transcribe_backend="auto", transcribe_api_url="https://api.groq.com/openai/v1/audio/transcriptions")
    t = Transcriber(cfg)
    assert t.active_backend == "api"
    assert t.model == "whisper-large-v3-turbo"


def test_invalid_backend_rejected(tmp_path):
    with pytest.raises(ValueError, match="transcribe_backend"):
        make_config(tmp_path, transcribe_backend="magic")


# ---------------------------------------------------------------------------
# backend de API
# ---------------------------------------------------------------------------

def test_api_backend_posts_multipart(tmp_path, monkeypatch):
    audio = tmp_path / "voz.ogg"
    audio.write_bytes(OGG)
    sent = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"text": " Oi, tudo certo ", "language": "portuguese", "duration": 3.5}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        sent.update(url=url, headers=headers, data=data, timeout=timeout, filename=files["file"][0], body=files["file"][1].read())
        return Response()

    monkeypatch.setattr("evoapi_mcp.transcription.requests.post", fake_post)

    t = Transcriber(make_config(tmp_path, transcribe_backend="api", transcribe_api_key="sk-test", transcribe_language="pt"))
    out = t.transcribe(audio)

    assert sent["url"].endswith("/audio/transcriptions")
    assert sent["headers"]["Authorization"] == "Bearer sk-test"
    assert sent["data"] == {"model": "whisper-1", "response_format": "json", "language": "pt"}
    assert sent["filename"] == "voz.ogg"
    assert sent["body"] == OGG
    assert out == {"text": "Oi, tudo certo", "language": "portuguese", "seconds": 3.5, "backend": "api", "model": "whisper-1"}


def test_api_backend_reports_http_error(tmp_path, monkeypatch):
    import requests

    audio = tmp_path / "voz.ogg"
    audio.write_bytes(OGG)

    class Response:
        status_code = 401
        text = "<html><body>Invalid API key</body></html>"

        def raise_for_status(self):
            raise requests.exceptions.HTTPError(response=self)

    monkeypatch.setattr("evoapi_mcp.transcription.requests.post", lambda *a, **k: Response())
    t = Transcriber(make_config(tmp_path, transcribe_backend="api", transcribe_api_key="sk-bad"))
    with pytest.raises(TranscriptionError, match="HTTP 401.*Invalid API key"):
        t.transcribe(audio)


def test_api_backend_rejects_oversized_audio(tmp_path):
    audio = tmp_path / "longo.ogg"
    audio.write_bytes(b"0" * (2 * 1024 * 1024))
    t = Transcriber(make_config(tmp_path, transcribe_backend="api", transcribe_api_key="sk", transcribe_max_mb=1))
    with pytest.raises(TranscriptionError, match="excede o limite"):
        t.transcribe(audio)


def test_missing_file(tmp_path):
    t = Transcriber(make_config(tmp_path, transcribe_backend="api", transcribe_api_key="sk"))
    with pytest.raises(TranscriptionError, match="não encontrado"):
        t.transcribe(tmp_path / "sumiu.ogg")


# ---------------------------------------------------------------------------
# cliente: transcribe_message
# ---------------------------------------------------------------------------

def test_transcribe_message_downloads_and_caches(audio_client, tmp_path):
    audio_client.responses.append(audio_payload())
    out = audio_client.transcribe_message("VOICE1", language="pt")

    assert out["text"] == "Bom dia, segue o combinado."
    assert out["seconds"] == 4.2
    assert out["backend"] == "stub"
    assert Path(out["path"]).read_bytes() == OGG
    assert audio_client.transcriber.calls[0]["language"] == "pt"
    assert "cached" not in out

    cache = tmp_path / "media" / ".transcripts" / "VOICE1.json"
    assert json.loads(cache.read_text(encoding="utf-8"))["text"] == out["text"]

    # segunda chamada: sem HTTP e sem transcrever de novo
    calls_before = len(audio_client.calls)
    again = audio_client.transcribe_message("VOICE1")
    assert again["cached"] is True
    assert again["text"] == out["text"]
    assert len(audio_client.calls) == calls_before
    assert len(audio_client.transcriber.calls) == 1


def test_transcribe_message_force_refreshes(audio_client):
    audio_client.responses.extend([audio_payload(), audio_payload()])
    audio_client.transcribe_message("VOICE1")
    audio_client.transcriber.text = "Texto novo"
    out = audio_client.transcribe_message("VOICE1", force=True)
    assert out["text"] == "Texto novo"
    assert len(audio_client.transcriber.calls) == 2


def test_transcribe_message_reuses_downloaded_audio(audio_client, tmp_path):
    media = tmp_path / "media"
    media.mkdir(parents=True)
    (media / "VOICE2.ogg").write_bytes(OGG)

    out = audio_client.transcribe_message("VOICE2")
    assert out["path"] == str(media / "VOICE2.ogg")
    assert audio_client.calls == []  # não baixou de novo


def test_transcribe_message_rejects_non_audio(audio_client):
    audio_client.responses.append({"mediaType": "document", "fileName": "boleto.pdf", "mimetype": "application/pdf", "base64": base64.b64encode(b"%PDF").decode()})
    with pytest.raises(TranscriptionError, match="não é áudio"):
        audio_client.transcribe_message("DOC1")


def test_transcribe_message_truncates(audio_client):
    audio_client.transcriber.text = "palavra " * 200
    audio_client.responses.append(audio_payload())
    out = audio_client.transcribe_message("VOICE3", max_chars=50)
    assert len(out["text"]) == 50
    assert out["truncated"] is True


def test_transcribe_message_empty_speech(audio_client):
    audio_client.transcriber.text = ""
    audio_client.responses.append(audio_payload())
    out = audio_client.transcribe_message("VOICE4")
    assert out["warning"] == "nenhuma fala reconhecida no áudio"


def test_transcribe_file(audio_client, tmp_path):
    f = tmp_path / "reuniao.m4a"
    f.write_bytes(OGG)
    out = audio_client.transcribe_file(str(f))
    assert out["text"] == "Bom dia, segue o combinado."
    assert out["path"] == str(f)
    with pytest.raises(ValueError, match="não encontrado"):
        audio_client.transcribe_file(str(tmp_path / "nada.mp3"))


# ---------------------------------------------------------------------------
# download_media(extract_text=True) em áudio
# ---------------------------------------------------------------------------

def test_download_media_extract_text_transcribes_audio(audio_client):
    audio_client.responses.append(audio_payload(name="PTT-20260911.ogg"))
    out = audio_client.download_media("VOICE5", extract_text=True)
    assert out["text"] == "Bom dia, segue o combinado."
    assert out["seconds"] == 4.2
    assert out["file"] == "PTT-20260911.ogg"


def test_download_media_extract_text_reports_transcription_error(client):
    client.transcriber = StubTranscriber(error="Nenhum backend de transcrição configurado.")
    client.responses.append(audio_payload())
    out = client.download_media("VOICE6", extract_text=True)
    assert out["text_error"] == "Nenhum backend de transcrição configurado."
    assert "text" not in out


# --- trechos com hora --------------------------------------------------------

def test_segments_come_from_the_cache_without_transcribing_again(audio_client):
    """Os trechos saem de graça na mesma passada, então ficam no cache."""
    audio_client.responses.append(audio_payload())
    primeira = audio_client.transcribe_message("MSG_SEG")
    assert "segments" not in primeira                            # sem pedir, não ocupa a resposta
    assert audio_client.transcriber.calls[-1]["segments"] is True  # mas foram calculados

    com_trechos = audio_client.transcribe_message("MSG_SEG", segments=True)
    assert com_trechos["cached"] is True and len(audio_client.transcriber.calls) == 1
    assert com_trechos["segments"][0]["tempo"] == "0:00"


def test_transcribe_file_can_ask_for_segments(audio_client, tmp_path):
    arquivo = tmp_path / "video.mp4"
    arquivo.write_bytes(b"dados")
    saida = audio_client.transcribe_file(str(arquivo), segments=True)
    assert saida["segments"] and audio_client.transcriber.calls[-1]["segments"] is True


def test_clock_and_trechos_are_readable():
    from evoapi_mcp.transcription import MAX_SEGMENTS, _trechos, clock

    assert clock(0) == "0:00" and clock(75.4) == "1:15" and clock(3742) == "1:02:22"
    trechos = _trechos([(0, 2.5, " oi "), (2.5, 4, ""), (4, 6, "tudo bem")])
    assert trechos == [
        {"t": 0.0, "tempo": "0:00", "fim": 2.5, "texto": "oi"},
        {"t": 4.0, "tempo": "0:04", "fim": 6.0, "texto": "tudo bem"},
    ]
    assert len(_trechos((i, i + 1, f"t{i}") for i in range(MAX_SEGMENTS + 50))) == MAX_SEGMENTS
