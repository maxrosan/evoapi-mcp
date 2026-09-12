"""Converte documentos em imagens, para o modelo LER o que está escrito.

Comprovante fotografado e PDF escaneado não têm camada de texto: `extract_text`
devolve vazio e o arquivo, que está no disco do servidor, fica ilegível para quem
só troca texto. Trazer o base64 para a conversa não resolve, porque chega como
texto e o modelo não enxerga imagem nenhuma, pagando dezenas de milhares de
tokens por nada.

Aqui a página vira imagem de verdade, devolvida pelo protocolo, e o modelo lê com
a própria visão. Uma página custa algo entre 1 e 2 mil tokens, contra dezenas de
milhares do base64, e ainda por cima funciona.

Renderização por `pypdfium2` (licença BSD/Apache, sem dependência de sistema) e
redimensionamento por Pillow. Ambos entram pelo extra opcional `[image]`.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

# Lado maior da imagem entregue ao modelo. Acima disso o custo em tokens sobe sem
# ganho de legibilidade.
DEFAULT_MAX_SIDE = 1568
MAX_PAGES = 5


class RenderError(Exception):
    """Erro ao transformar o documento em imagem."""


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Render: {message}", file=sys.stderr)


def is_renderable(mime: str | None, path: Path | None = None) -> bool:
    """True para imagens e PDF."""
    m = (mime or "").split(";")[0].strip().lower()
    if m.startswith("image/") or m == "application/pdf":
        return True
    if path is not None:
        return path.suffix.lower() in (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff")
    return False


def _require_pillow():
    try:
        from PIL import Image
    except ImportError:
        raise RenderError('Pillow não instalado (pip install -e ".[image]")')
    return Image


def _to_jpeg(pil_image, max_side: int) -> bytes:
    """Redimensiona e serializa como JPEG, que é o formato mais barato aqui."""
    Image = _require_pillow()

    if pil_image.mode not in ("RGB", "L"):
        if pil_image.mode in ("RGBA", "LA", "P"):
            fundo = Image.new("RGB", pil_image.size, (255, 255, 255))
            convertida = pil_image.convert("RGBA")
            fundo.paste(convertida, mask=convertida.split()[-1])
            pil_image = fundo
        else:
            pil_image = pil_image.convert("RGB")

    maior = max(pil_image.size)
    if maior > max_side:
        fator = max_side / maior
        novo = (max(1, round(pil_image.width * fator)), max(1, round(pil_image.height * fator)))
        pil_image = pil_image.resize(novo, Image.LANCZOS)

    buf = io.BytesIO()
    pil_image.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()


def _render_pdf(path: Path, first_page: int, pages: int, max_side: int, password: str | None) -> list[bytes]:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise RenderError('pypdfium2 não instalado (pip install -e ".[image]")')

    try:
        documento = pdfium.PdfDocument(str(path), password=password)
    except Exception as e:
        texto = str(e).lower()
        if "password" in texto or "senha" in texto:
            raise RenderError("PDF protegido por senha. Repita a chamada informando password.")
        raise RenderError(f"Não foi possível abrir o PDF: {e}")

    total = len(documento)
    inicio = max(1, first_page) - 1
    if inicio >= total:
        raise RenderError(f"O PDF tem {total} página(s); a página {first_page} não existe.")

    fim = min(inicio + max(1, pages), total)
    saida: list[bytes] = []
    for indice in range(inicio, fim):
        pagina = documento[indice]
        largura = float(pagina.get_width()) or 612.0
        escala = min(4.0, max(1.0, max_side / largura))
        bitmap = pagina.render(scale=escala)
        saida.append(_to_jpeg(bitmap.to_pil(), max_side))
    _log(f"{len(saida)} página(s) de {total} renderizada(s) de {path.name}")
    return saida


def render(
    path: str | Path,
    mime: str | None = None,
    first_page: int = 1,
    pages: int = 1,
    max_side: int = DEFAULT_MAX_SIDE,
    password: str | None = None,
) -> dict:
    """Transforma um documento em imagens JPEG.

    Args:
        path: arquivo no disco do servidor
        mime: mimetype, quando conhecido
        first_page: primeira página (PDF), começando em 1
        pages: quantas páginas renderizar, no máximo MAX_PAGES
        max_side: lado maior da imagem resultante
        password: senha, para PDF protegido

    Returns:
        dict: {images: [bytes...], format: "jpeg", pages_rendered, total_pages?}

    Raises:
        RenderError: formato não suportado, dependência ausente ou falha
    """
    path = Path(path)
    if not path.is_file():
        raise RenderError(f"Arquivo não encontrado no servidor: {path}")

    pages = max(1, min(int(pages or 1), MAX_PAGES))
    max_side = max(256, min(int(max_side or DEFAULT_MAX_SIDE), 2000))
    m = (mime or "").split(";")[0].strip().lower()

    if m == "application/pdf" or path.suffix.lower() == ".pdf":
        imagens = _render_pdf(path, first_page, pages, max_side, password)
        return {"images": imagens, "format": "jpeg", "pages_rendered": len(imagens)}

    if m.startswith("image/") or is_renderable(None, path):
        Image = _require_pillow()
        try:
            with Image.open(path) as imagem:
                imagem.load()
                dados = _to_jpeg(imagem, max_side)
        except Exception as e:
            raise RenderError(f"Não foi possível abrir a imagem: {e}")
        return {"images": [dados], "format": "jpeg", "pages_rendered": 1}

    raise RenderError(f"Não dá para transformar em imagem um arquivo do tipo {mime or path.suffix}.")
