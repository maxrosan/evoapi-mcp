"""Vetores visuais (CLIP): achar uma imagem pelo que ela mostra, sem OCR e sem Claude.

Imagem e texto caem no mesmo espaço de vetores, então "photo of fried egg with
farofa" encontra a foto do ovo. O encoder de texto do CLIP só entende bem inglês:
quem chama a busca visual escreve a consulta em inglês, o que custa quase nada.

Modelos locais via fastembed/onnxruntime, sem chave: ViT-B/32, cerca de 0,6 GB
somando visão e texto.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

from evoapi_mcp.memory import default_cache_dir

VISION_MODEL = "Qdrant/clip-ViT-B-32-vision"
TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"
# Similaridade texto-imagem do CLIP é baixa em termos absolutos: acertos ficam
# por volta de 0,25 a 0,35. Abaixo disto é ruído.
MIN_VISUAL_SCORE = 0.18


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Visual: {message}", file=sys.stderr)


def _normalizar(matriz: Any) -> Any:
    import numpy as np

    m = np.asarray(matriz, dtype=np.float32)
    normas = np.linalg.norm(m, axis=1, keepdims=True)
    normas[normas == 0] = 1.0
    return m / normas


class VisualEmbedder:
    """Carrega os dois modelos sob demanda e devolve vetores normalizados."""

    def __init__(self, cache_dir: str | None = None):
        self.cache_dir = cache_dir or default_cache_dir()
        self._visao = None
        self._texto = None
        self._erro: str | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        if self._erro:
            return False
        try:
            from fastembed import ImageEmbedding  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def loaded(self) -> bool:
        return self._visao is not None

    def _carregar(self) -> None:
        with self._lock:
            if self._visao is None:
                import warnings

                from fastembed import ImageEmbedding, TextEmbedding

                opcoes = {"cache_dir": self.cache_dir} if self.cache_dir else {}
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        self._texto = TextEmbedding(TEXT_MODEL, **opcoes)
                        self._visao = ImageEmbedding(VISION_MODEL, **opcoes)
                except Exception as e:
                    # Modelo ausente ou corrompido: some da lista de recursos em vez de
                    # derrubar cada indexação de imagem.
                    self._erro = str(e)
                    self._texto = self._visao = None
                    _log(f"modelos CLIP indisponíveis: {e}", "ERROR")
                    raise
                _log("modelos CLIP carregados")

    def embed_image(self, path: str | Path) -> Any:
        self._carregar()
        return _normalizar(list(self._visao.embed([str(path)])))[0]

    def embed_query(self, text: str) -> Any:
        self._carregar()
        return _normalizar(list(self._texto.embed([text])))[0]


def preload() -> None:
    VisualEmbedder().embed_query("warm up")
    print("modelos visuais prontos", file=sys.stderr)
