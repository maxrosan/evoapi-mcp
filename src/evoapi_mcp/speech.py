"""Texto para voz: gera um áudio a partir de um texto, para mandar como nota de voz.

É o caminho inverso da transcrição. Dois backends:

- `edge`: as vozes neurais do Microsoft Edge, pelo pacote `edge-tts`. Sem chave,
  sem custo, e com vozes brasileiras de verdade (Francisca, Antonio, Thalita).
  Depende de um serviço não documentado da Microsoft, que pode mudar sem aviso;
  por isso existe o segundo.
- `api`: qualquer endpoint compatível com `/v1/audio/speech` da OpenAI. Precisa
  de chave (`EVOLUTION_TTS_API_KEY` ou `OPENAI_API_KEY`).

`auto` prefere a API quando há chave, senão o Edge quando está instalado, senão
desliga. O áudio sai em MP3 na pasta de mídia; a Evolution converte para o formato
de nota de voz na hora de enviar.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from pathlib import Path
from typing import Any

_DEFAULT_API_URL = "https://api.openai.com/v1/audio/speech"
_DEFAULT_API_MODEL = "gpt-4o-mini-tts"
_DEFAULT_API_VOICE = "nova"
_DEFAULT_EDGE_VOICE = "pt-BR-FranciscaNeural"
_ENV_KEYS = ("EVOLUTION_TTS_API_KEY", "OPENAI_API_KEY")
MAX_TEXT = 3000
SUBDIR = "voz"


class SpeechError(Exception):
    """Erro ao gerar a voz."""


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Voz: {message}", file=sys.stderr)


def _run_async(coro) -> None:
    """Roda uma corrotina numa thread própria, com laço novo.

    As tools do MCP podem rodar dentro de um laço asyncio já ativo; `asyncio.run`
    ali levantaria erro. Uma thread dedicada evita a pergunta.
    """
    import asyncio

    erro: list[BaseException] = []

    def alvo() -> None:
        try:
            asyncio.run(coro)
        except BaseException as e:  # repassado para quem chamou
            erro.append(e)

    t = threading.Thread(target=alvo, name="voz", daemon=True)
    t.start()
    t.join()
    if erro:
        raise erro[0]


class Speaker:
    """Escolhe e executa o backend de voz configurado."""

    def __init__(self, config: Any):
        self.backend = (getattr(config, "tts_backend", "auto") or "auto").lower()
        self.api_url = (getattr(config, "tts_api_url", "") or "").strip()
        self.timeout = int(getattr(config, "tts_timeout", 60))
        self._configured_voice = (getattr(config, "tts_voice", "") or "").strip()
        self._configured_model = (getattr(config, "tts_model", "") or "").strip()
        self._api_key = (getattr(config, "tts_api_key", "") or "").strip() or self._key_from_env()
        self.out_dir = Path(getattr(config, "media_dir", "media")) / SUBDIR

    # ------------------------------------------------------------------ setup

    @staticmethod
    def _key_from_env() -> str:
        for name in _ENV_KEYS:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _edge_available() -> bool:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return False
        return True

    def _api_configured(self) -> bool:
        return bool(self._api_key)

    @property
    def active_backend(self) -> str:
        """Backend efetivo: 'api', 'edge' ou 'off'."""
        if self.backend == "off":
            return "off"
        if self.backend == "api":
            return "api" if self._api_configured() else "off"
        if self.backend == "edge":
            return "edge" if self._edge_available() else "off"
        if self._api_configured():
            return "api"
        if self._edge_available():
            return "edge"
        return "off"

    @property
    def available(self) -> bool:
        return self.active_backend != "off"

    @property
    def voice(self) -> str:
        if self._configured_voice:
            return self._configured_voice
        return _DEFAULT_API_VOICE if self.active_backend == "api" else _DEFAULT_EDGE_VOICE

    @property
    def model(self) -> str:
        return self._configured_model or _DEFAULT_API_MODEL

    def unavailable_reason(self) -> str:
        if self.backend == "off":
            return "desligado por EVOLUTION_TTS_BACKEND=off"
        if self.backend == "api":
            return "defina EVOLUTION_TTS_API_KEY (ou OPENAI_API_KEY)"
        if self.backend == "edge":
            return "edge-tts não está instalado: pip install edge-tts"
        return "sem chave de API e sem edge-tts: pip install edge-tts, ou defina EVOLUTION_TTS_API_KEY"

    def describe(self) -> dict[str, Any]:
        """Resumo da configuração, sem expor a chave."""
        backend = self.active_backend
        info: dict[str, Any] = {"backend": backend, "available": backend != "off"}
        if backend != "off":
            info["voice"] = self.voice
        if backend == "api":
            info["model"] = self.model
            info["url"] = self.api_url or _DEFAULT_API_URL
        if backend == "off":
            info["hint"] = self.unavailable_reason()
        return info

    # ------------------------------------------------------------------ síntese

    def synthesize(self, text: str, voice: str | None = None) -> Path:
        """Gera o MP3 e devolve o caminho. O mesmo texto na mesma voz reaproveita o arquivo."""
        texto = (text or "").strip()
        if not texto:
            raise SpeechError("texto vazio")
        if len(texto) > MAX_TEXT:
            raise SpeechError(f"texto com {len(texto)} caracteres; o máximo é {MAX_TEXT}")
        backend = self.active_backend
        if backend == "off":
            raise SpeechError(f"voz indisponível: {self.unavailable_reason()}")
        voz = (voice or "").strip() or self.voice

        self.out_dir.mkdir(parents=True, exist_ok=True)
        chave = hashlib.sha1(f"{backend}|{voz}|{texto}".encode("utf-8")).hexdigest()[:16]
        destino = self.out_dir / f"{chave}.mp3"
        if destino.is_file() and destino.stat().st_size > 0:
            return destino

        try:
            if backend == "edge":
                self._synthesize_edge(texto, voz, destino)
            else:
                self._synthesize_api(texto, voz, destino)
        except SpeechError:
            raise
        except Exception as e:
            destino.unlink(missing_ok=True)
            raise SpeechError(f"falha ao gerar a voz ({backend}): {e}") from e
        if not destino.is_file() or destino.stat().st_size == 0:
            destino.unlink(missing_ok=True)
            raise SpeechError(f"o backend {backend} não produziu áudio")
        _log(f"{len(texto)} caracteres -> {destino.name} ({destino.stat().st_size} bytes, {backend}, {voz})")
        return destino

    def _synthesize_edge(self, texto: str, voz: str, destino: Path) -> None:
        import edge_tts

        _run_async(edge_tts.Communicate(texto, voz).save(str(destino)))

    def _synthesize_api(self, texto: str, voz: str, destino: Path) -> None:
        import requests

        corpo: dict[str, Any] = {"model": self.model, "voice": voz, "input": texto, "response_format": "mp3"}
        if self.model.startswith("gpt-4o"):
            # Estes modelos aceitam instrução de estilo; sem ela o sotaque sai genérico.
            corpo["instructions"] = "Fale em português do Brasil, com sotaque brasileiro natural, em tom de conversa."
        r = requests.post(
            self.api_url or _DEFAULT_API_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=corpo,
            timeout=self.timeout,
        )
        if r.status_code >= 400:
            raise SpeechError(f"API de voz respondeu {r.status_code}: {r.text[:300]}")
        destino.write_bytes(r.content)
