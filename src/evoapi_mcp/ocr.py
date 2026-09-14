"""Reconhecimento de texto em imagem (OCR) com Tesseract, no próprio servidor.

Serve para comprovante fotografado, print de tela e PDF escaneado: o texto vira
parte do índice sem que o Claude precise olhar a imagem. Exige o binário
`tesseract` com o idioma português (apt: tesseract-ocr tesseract-ocr-por) e o
pacote `pytesseract`. Sem eles, `available()` é False e o índice guarda só o que
não depende de OCR.
"""

from __future__ import annotations

import io
import re
import shutil
from pathlib import Path

DEFAULT_LANG = "por+eng"
# Imagens pequenas reconhecem mal: sobe o lado maior até isto antes do OCR.
MIN_SIDE = 1600
TIMEOUT_S = 120


class OcrError(Exception):
    """OCR indisponível ou falhou."""


def available() -> bool:
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return shutil.which("tesseract") is not None


def _limpar(texto: str) -> str:
    linhas = [re.sub(r"[ \t]+", " ", linha).strip() for linha in (texto or "").splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(linhas)).strip()


def ocr_image(image: bytes | str | Path, lang: str = DEFAULT_LANG, timeout: int = TIMEOUT_S) -> str:
    """Texto de uma imagem (bytes ou caminho). Devolve string vazia se não houver texto."""
    if not available():
        raise OcrError("OCR indisponível: instale tesseract-ocr, tesseract-ocr-por e pytesseract")
    import pytesseract
    from PIL import Image, ImageOps

    try:
        origem = io.BytesIO(image) if isinstance(image, (bytes, bytearray)) else str(image)
        with Image.open(origem) as aberta:
            img = ImageOps.exif_transpose(aberta).convert("L")
    except Exception as e:
        raise OcrError(f"não consegui abrir a imagem: {e}") from e

    maior = max(img.size)
    if 0 < maior < MIN_SIDE:
        escala = MIN_SIDE / maior
        img = img.resize((round(img.size[0] * escala), round(img.size[1] * escala)))

    try:
        return _limpar(pytesseract.image_to_string(img, lang=lang, timeout=timeout))
    except pytesseract.TesseractError as e:
        if "por" in lang and "language" in str(e).lower():
            return _limpar(pytesseract.image_to_string(img, lang="eng", timeout=timeout))
        raise OcrError(f"Tesseract falhou: {e}") from e
    except RuntimeError as e:  # pytesseract sinaliza tempo esgotado assim
        raise OcrError(f"OCR demorou mais que {timeout} s") from e
