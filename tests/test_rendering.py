"""Documento virando imagem, para o modelo ler o que está escrito — e o caminho inverso."""

import base64
import io
from pathlib import Path

import pytest

from evoapi_mcp.rendering import (
    DEFAULT_MAX_SIDE,
    MAX_PAGES,
    MAX_SIDE_OUT,
    MAX_SVG_CHARS,
    RenderError,
    is_renderable,
    render,
    render_svg,
)

pytest.importorskip("PIL")
pytest.importorskip("pypdfium2")

from PIL import Image  # noqa: E402


def png_bytes(size=(2400, 1800), color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def pdf_bytes(paginas=3, senha=None):
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    for _ in range(paginas):
        writer.add_blank_page(width=595, height=842)  # A4 em pontos
    if senha:
        writer.encrypt(senha)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def dimensoes(dados):
    with Image.open(io.BytesIO(dados)) as im:
        return im.size


# ---------------------------------------------------------------------------
# detecção
# ---------------------------------------------------------------------------

def test_is_renderable():
    assert is_renderable("image/jpeg")
    assert is_renderable("application/pdf")
    assert is_renderable("image/png; charset=binary")
    assert not is_renderable("audio/ogg")
    assert not is_renderable("text/plain")
    from pathlib import Path
    assert is_renderable(None, Path("scan.tiff"))
    assert not is_renderable(None, Path("nota.txt"))


# ---------------------------------------------------------------------------
# imagens
# ---------------------------------------------------------------------------

def test_image_is_downscaled_to_the_cap(tmp_path):
    f = tmp_path / "comprovante.png"
    f.write_bytes(png_bytes((2400, 1800)))
    out = render(f, mime="image/png")

    assert out["format"] == "jpeg"
    assert out["pages_rendered"] == 1
    largura, altura = dimensoes(out["images"][0])
    assert max(largura, altura) == DEFAULT_MAX_SIDE
    assert altura == round(1800 * DEFAULT_MAX_SIDE / 2400)  # proporção mantida


def test_small_image_is_not_upscaled(tmp_path):
    f = tmp_path / "pequena.png"
    f.write_bytes(png_bytes((320, 240)))
    out = render(f, mime="image/png")
    assert dimensoes(out["images"][0]) == (320, 240)


def test_transparent_image_gets_white_background(tmp_path):
    f = tmp_path / "logo.png"
    buf = io.BytesIO()
    Image.new("RGBA", (100, 100), (255, 0, 0, 0)).save(buf, format="PNG")
    f.write_bytes(buf.getvalue())
    out = render(f, mime="image/png")
    with Image.open(io.BytesIO(out["images"][0])) as im:
        assert im.mode == "RGB"
        assert im.getpixel((50, 50)) == pytest.approx((255, 255, 255), abs=3)


def test_max_side_is_respected(tmp_path):
    f = tmp_path / "g.png"
    f.write_bytes(png_bytes((3000, 3000)))
    out = render(f, mime="image/png", max_side=800)
    assert max(dimensoes(out["images"][0])) == 800


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def test_pdf_renders_requested_pages(tmp_path):
    f = tmp_path / "escaneado.pdf"
    f.write_bytes(pdf_bytes(paginas=3))

    out = render(f, mime="application/pdf", pages=2)
    assert out["pages_rendered"] == 2
    for img in out["images"]:
        assert max(dimensoes(img)) <= DEFAULT_MAX_SIDE


def test_pdf_starts_at_requested_page(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(pdf_bytes(paginas=4))
    out = render(f, mime="application/pdf", first_page=3, pages=5)
    assert out["pages_rendered"] == 2  # só restam 2 a partir da página 3


def test_pdf_page_out_of_range(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(pdf_bytes(paginas=2))
    with pytest.raises(RenderError, match="não existe"):
        render(f, mime="application/pdf", first_page=9)


def test_pdf_page_limit(tmp_path):
    f = tmp_path / "longo.pdf"
    f.write_bytes(pdf_bytes(paginas=MAX_PAGES + 4))
    out = render(f, mime="application/pdf", pages=99)
    assert out["pages_rendered"] == MAX_PAGES


def test_encrypted_pdf_without_password(tmp_path):
    f = tmp_path / "boleto.pdf"
    f.write_bytes(pdf_bytes(paginas=1, senha="1234"))
    with pytest.raises(RenderError):
        render(f, mime="application/pdf")


def test_encrypted_pdf_with_password(tmp_path):
    f = tmp_path / "boleto.pdf"
    f.write_bytes(pdf_bytes(paginas=1, senha="1234"))
    out = render(f, mime="application/pdf", password="1234")
    assert out["pages_rendered"] == 1


# ---------------------------------------------------------------------------
# erros
# ---------------------------------------------------------------------------

def test_missing_file(tmp_path):
    with pytest.raises(RenderError, match="não encontrado"):
        render(tmp_path / "sumiu.png", mime="image/png")


def test_unsupported_type(tmp_path):
    f = tmp_path / "audio.ogg"
    f.write_bytes(b"OggS")
    with pytest.raises(RenderError, match="Não dá para transformar"):
        render(f, mime="audio/ogg")


# ---------------------------------------------------------------------------
# integração com o cliente
# ---------------------------------------------------------------------------

def test_render_media_downloads_then_renders(client):
    client.responses.append({
        "mediaType": "image", "fileName": "comprovante.png", "mimetype": "image/png",
        "base64": base64.b64encode(png_bytes((2000, 1000))).decode(),
    })
    out = client.render_media("MSG1")

    assert out["pages_rendered"] == 1
    assert out["file"] == "comprovante.png"
    assert out["mime"] == "image/png"
    assert max(dimensoes(out["images"][0])) == DEFAULT_MAX_SIDE


def test_render_media_rejects_audio(client):
    client.responses.append({
        "mediaType": "audio", "mimetype": "audio/ogg",
        "base64": base64.b64encode(b"OggS").decode(),
    })
    with pytest.raises(RenderError, match="não é imagem nem PDF"):
        client.render_media("MSG1")


def test_render_media_unlocks_then_renders(client):
    """O PDF é destravado no download; a renderização não precisa da senha de novo."""
    client.responses.append({
        "mediaType": "document", "fileName": "boleto.pdf", "mimetype": "application/pdf",
        "base64": base64.b64encode(pdf_bytes(paginas=1, senha="1234")).decode(),
    })
    out = client.render_media("MSG1", password="1234")
    assert out["pages_rendered"] == 1


# ---------------------------------------------------------------------------
# SVG do modelo virando imagem (send_render)
# ---------------------------------------------------------------------------

try:
    import cairosvg  # noqa: F401
    TEM_CAIRO = True
except Exception:  # sem o pacote ou sem a libcairo do sistema
    TEM_CAIRO = False

com_cairo = pytest.mark.skipif(not TEM_CAIRO, reason="cairosvg/libcairo ausente")

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
    '<rect width="200" height="100" fill="#0a6"/>'
    '<text x="10" y="55" font-family="sans-serif" font-size="20">Venda: 42</text>'
    "</svg>"
)


@com_cairo
def test_render_svg_uses_the_svg_own_size():
    assert dimensoes(render_svg(SVG)) == (200, 100)


@com_cairo
def test_render_svg_honours_width_and_height():
    assert dimensoes(render_svg(SVG, width=600, height=300)) == (600, 300)


@com_cairo
def test_render_svg_paints_the_background_and_keeps_it_optional():
    """Fundo branco por padrão; com background=None o que não foi desenhado fica transparente."""
    circulo = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60">'
        '<circle cx="30" cy="30" r="20" fill="#0a6"/></svg>'
    )

    def canto(png):
        with Image.open(io.BytesIO(png)) as im:
            return im.mode, im.getpixel((0, 0))

    assert canto(render_svg(circulo, background=None)) == ("RGBA", (0, 0, 0, 0))
    assert canto(render_svg(circulo)) == ("RGB", (255, 255, 255))


@com_cairo
def test_render_svg_rejects_junk():
    with pytest.raises(RenderError, match="vazio"):
        render_svg("   ")
    with pytest.raises(RenderError, match="não parece um SVG"):
        render_svg("<html><body>oi</body></html>")
    with pytest.raises(RenderError, match="acima do limite"):
        render_svg("<svg" + "x" * MAX_SVG_CHARS)
    with pytest.raises(RenderError, match="width deve estar entre"):
        render_svg(SVG, width=MAX_SIDE_OUT + 1)
    with pytest.raises(RenderError, match="rasterizar"):
        render_svg('<svg xmlns="http://www.w3.org/2000/svg"><rect</svg>')


@com_cairo
def test_send_render_rasterizes_before_sending(client, config):
    client.responses.append({"key": {"remoteJid": "5511999999999@s.whatsapp.net", "id": "S1"}})

    out = client.send_render("5511999999999", SVG, caption="o número de hoje", width=400)

    call = client.calls[0]
    assert call["endpoint"] == "/message/sendMedia/{instanceId}"
    assert call["data"]["mediatype"] == "image"
    assert call["data"]["caption"] == "o número de hoje"
    enviado = base64.b64decode(call["data"]["media"])
    assert dimensoes(enviado) == (400, 200)

    # o PNG fica no disco do servidor, para reenviar ou arquivar sem redesenhar
    salvo = Path(out["_file"]["path"])
    assert salvo.parent == Path(config.media_dir) / "render"
    assert salvo.read_bytes() == enviado


@com_cairo
def test_send_render_always_writes_a_png(client):
    client.responses.append({})
    out = client.send_render("5511999999999", SVG, file_name="grafico")
    assert Path(out["_file"]["path"]).name == "grafico.png"
    assert client.calls[0]["data"]["fileName"] == "grafico.png"
