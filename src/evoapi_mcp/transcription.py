"""Transcrição de áudios do WhatsApp (voice notes) em texto.

Um áudio de 1 minuto tem ~500 KB; em base64 isso passa de 600 mil caracteres e
é impossível de mandar para o LLM. A transcrição roda aqui no servidor e o que
volta é só o texto, tipicamente algumas dezenas de tokens.

Dois backends:

- `api`: qualquer endpoint compatível com a API de transcrição da OpenAI
  (OpenAI, Groq, whisper.cpp server, faster-whisper-server local).
  Configure `EVOLUTION_TRANSCRIBE_API_URL` e `EVOLUTION_TRANSCRIBE_API_KEY`
  (ou deixe o token em `OPENAI_API_KEY` / `GROQ_API_KEY`).
- `local`: roda `faster-whisper` na própria máquina, sem chave e sem rede
  (`pip install -e ".[audio]"`). O modelo é carregado uma vez e reaproveitado.

`EVOLUTION_TRANSCRIBE_BACKEND=auto` (padrão) usa a API quando há chave/URL
configurada e cai para o modelo local quando o pacote está instalado.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import requests

# Endpoints e modelos padrão por provedor
_DEFAULT_API_URL = "https://api.openai.com/v1/audio/transcriptions"
_DEFAULT_API_MODELS = {
    "groq": "whisper-large-v3-turbo",
    "openai": "whisper-1",
}
_DEFAULT_LOCAL_MODEL = "small"

# Chaves aceitas do ambiente quando transcribe_api_key não é informada
_ENV_KEYS = ("EVOLUTION_TRANSCRIBE_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY")

_AUDIO_MIME_PREFIXES = ("audio/", "video/")


# Quantos trechos com hora devolver: acima disso a lista passa a custar mais
# tokens do que o próprio texto.
MAX_SEGMENTS = 200


class TranscriptionError(Exception):
    """Erro ao transcrever um áudio."""


def clock(seconds: float | None) -> str:
    """12.0 -> '0:12'; 3742.5 -> '1:02:22'."""
    total = int(round(max(float(seconds or 0), 0)))
    h, resto = divmod(total, 3600)
    m, s = divmod(resto, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _trechos(itens) -> list[dict[str, Any]]:
    """[(inicio, fim, texto)] -> [{t, tempo, fim, texto}], sem trecho vazio."""
    saida = []
    for inicio, fim, texto in itens:
        texto = (texto or "").strip()
        if not texto:
            continue
        saida.append({
            "t": round(float(inicio or 0), 1),
            "tempo": clock(inicio),
            "fim": round(float(fim or 0), 1),
            "texto": texto,
        })
        if len(saida) >= MAX_SEGMENTS:
            break
    return saida


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Transcrição: {message}", file=sys.stderr)


def is_transcribable(mime: str | None, path: Path | None = None) -> bool:
    """True quando o arquivo é áudio ou vídeo (vídeo tem trilha de áudio)."""
    if mime and mime.split(";")[0].strip().startswith(_AUDIO_MIME_PREFIXES):
        return True
    if path is not None:
        return path.suffix.lower() in (".ogg", ".opus", ".mp3", ".m4a", ".wav", ".aac", ".mp4", ".mov", ".webm", ".flac")
    return False


class Transcriber:
    """Escolhe e executa o backend de transcrição configurado."""

    def __init__(self, config: Any):
        self.backend = (getattr(config, "transcribe_backend", "auto") or "auto").lower()
        self.language = (getattr(config, "transcribe_language", "") or "").strip()
        self.api_url = (getattr(config, "transcribe_api_url", "") or "").strip()
        self.timeout = int(getattr(config, "transcribe_timeout", 120))
        self.max_mb = int(getattr(config, "transcribe_max_mb", 25))
        self._configured_model = (getattr(config, "transcribe_model", "") or "").strip()
        self._api_key = (getattr(config, "transcribe_api_key", "") or "").strip() or self._key_from_env()
        self._local_model = None  # instância do WhisperModel, carregada sob demanda

    # ------------------------------------------------------------------ setup

    @staticmethod
    def _key_from_env() -> str:
        for name in _ENV_KEYS:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _local_available() -> bool:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    def _api_configured(self) -> bool:
        return bool(self._api_key or self.api_url)

    @property
    def active_backend(self) -> str:
        """Backend efetivo: 'api', 'local' ou 'off'."""
        if self.backend == "off":
            return "off"
        if self.backend == "api":
            return "api" if self._api_configured() else "off"
        if self.backend == "local":
            return "local" if self._local_available() else "off"
        # auto
        if self._api_configured():
            return "api"
        if self._local_available():
            return "local"
        return "off"

    @property
    def available(self) -> bool:
        return self.active_backend != "off"

    @property
    def model(self) -> str:
        if self._configured_model:
            return self._configured_model
        if self.active_backend == "api":
            url = self.api_url or _DEFAULT_API_URL
            for marker, model in _DEFAULT_API_MODELS.items():
                if marker in url:
                    return model
            return _DEFAULT_API_MODELS["openai"]
        return _DEFAULT_LOCAL_MODEL

    def describe(self) -> dict[str, Any]:
        """Resumo da configuração, sem expor a chave."""
        backend = self.active_backend
        info: dict[str, Any] = {"backend": backend, "available": backend != "off"}
        if backend != "off":
            info["model"] = self.model
        if backend == "api":
            info["url"] = self.api_url or _DEFAULT_API_URL
        if backend == "off":
            info["hint"] = self.unavailable_reason()
        return info

    def unavailable_reason(self) -> str:
        if self.backend == "off":
            return "Transcrição desligada (EVOLUTION_TRANSCRIBE_BACKEND=off)."
        return (
            "Nenhum backend de transcrição configurado. Defina "
            "EVOLUTION_TRANSCRIBE_API_KEY (OpenAI/Groq) ou instale o modelo local "
            'com: pip install -e ".[audio]"'
        )

    # -------------------------------------------------------------- transcribe

    def transcribe(self, path: Path, language: str | None = None,
                   segments: bool = False) -> dict[str, Any]:
        """Transcreve um arquivo de áudio/vídeo.

        Args:
            path: Caminho do arquivo já em disco
            language: Código ISO (ex: 'pt'); None usa EVOLUTION_TRANSCRIBE_LANGUAGE
                      e, se vazio, deixa o modelo detectar
            segments: devolve também os trechos com hora ("no minuto 3:20 ele diz X")

        Returns:
            dict: {text, backend, model, language?, seconds?, segments?}

        Raises:
            TranscriptionError: Sem backend disponível ou falha na transcrição
        """
        backend = self.active_backend
        if backend == "off":
            raise TranscriptionError(self.unavailable_reason())
        if not path.is_file():
            raise TranscriptionError(f"Arquivo não encontrado: {path}")

        size_mb = path.stat().st_size / (1024 * 1024)
        if backend == "api" and self.max_mb and size_mb > self.max_mb:
            raise TranscriptionError(
                f"Áudio de {size_mb:.1f} MB excede o limite de {self.max_mb} MB da API. "
                "Use o backend local (EVOLUTION_TRANSCRIBE_BACKEND=local)."
            )

        lang = (language or self.language or "").strip() or None
        _log(f"Transcrevendo {path.name} ({size_mb:.2f} MB) via {backend}/{self.model}")

        result = (self._transcribe_api(path, lang, segments) if backend == "api"
                  else self._transcribe_local(path, lang, segments))
        result.setdefault("backend", backend)
        result.setdefault("model", self.model)
        return result

    def _transcribe_api(self, path: Path, language: str | None,
                        segments: bool = False) -> dict[str, Any]:
        url = self.api_url or _DEFAULT_API_URL
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data = {"model": self.model, "response_format": "verbose_json" if segments else "json"}
        if language:
            data["language"] = language

        try:
            with path.open("rb") as fh:
                response = requests.post(
                    url,
                    headers=headers,
                    data=data,
                    files={"file": (path.name, fh, "application/octet-stream")},
                    timeout=self.timeout,
                )
            response.raise_for_status()
            payload = response.json()
        except requests.exceptions.Timeout:
            raise TranscriptionError(f"Timeout de {self.timeout}s ao transcrever {path.name}")
        except requests.exceptions.HTTPError as e:
            from evoapi_mcp.formatters import compact_error

            raise TranscriptionError(
                f"HTTP {e.response.status_code} na transcrição: {compact_error(e.response.text, 200)}"
            )
        except requests.exceptions.RequestException as e:
            raise TranscriptionError(f"Falha de conexão com o serviço de transcrição: {e}")
        except ValueError:
            raise TranscriptionError("Resposta do serviço de transcrição não é JSON")

        text = (payload.get("text") if isinstance(payload, dict) else None) or ""
        out: dict[str, Any] = {"text": text.strip()}
        if isinstance(payload, dict):
            if segments and isinstance(payload.get("segments"), list):
                out["segments"] = _trechos(
                    (s.get("start"), s.get("end"), s.get("text")) for s in payload["segments"]
                )
            if payload.get("language"):
                out["language"] = payload["language"]
            if payload.get("duration"):
                try:
                    out["seconds"] = round(float(payload["duration"]), 1)
                except (TypeError, ValueError):
                    pass
        return out

    def _transcribe_local(self, path: Path, language: str | None,
                          segments: bool = False) -> dict[str, Any]:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise TranscriptionError('faster-whisper não instalado: pip install -e ".[audio]"')

        if self._local_model is None:
            _log(f"Carregando modelo local '{self.model}' (primeira transcrição pode demorar)")
            try:
                self._local_model = WhisperModel(self.model, device="auto", compute_type="int8")
            except Exception as e:
                raise TranscriptionError(f"Falha ao carregar o modelo '{self.model}': {e}")

        try:
            trechos, info = self._local_model.transcribe(str(path), language=language, vad_filter=True)
            trechos = list(trechos)                      # gerador: só aqui a transcrição roda
            text = " ".join(t.text.strip() for t in trechos).strip()
        except Exception as e:
            raise TranscriptionError(f"Falha ao transcrever {path.name}: {e}")

        out: dict[str, Any] = {"text": text}
        if segments:
            out["segments"] = _trechos((t.start, t.end, t.text) for t in trechos)
        if getattr(info, "language", None):
            out["language"] = info.language
        if getattr(info, "duration", None):
            out["seconds"] = round(float(info.duration), 1)
        return out
