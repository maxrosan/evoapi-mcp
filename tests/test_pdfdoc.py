"""Desmontar um PDF (texto em blocos + imagens com id) e remontar um PDF revisado."""

import json

import pytest

pytest.importorskip("pypdfium2")
pytest.importorskip("fpdf")
Image = pytest.importorskip("PIL.Image")

from evoapi_mcp import pdfdoc  # noqa: E402
from evoapi_mcp.pdfdoc import PdfDocError, build_pdf, extract_pdf, resolve_image  # noqa: E402


def _foto(caminho, cor, tamanho=(300, 400)):
    """Foto de teste com textura, para não virar uma cor só no JPEG."""
    im = Image.new("RGB", tamanho, cor)
    for x in range(0, tamanho[0], 20):
        for y in range(0, tamanho[1], 20):
            if (x + y) % 40 == 0:
                im.putpixel((x, y), (255 - cor[0], 255 - cor[1], 255 - cor[2]))
    im.save(caminho, "JPEG", quality=95)
    return caminho


def _texto_do_pdf(caminho):
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(caminho))
    try:
        return "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    finally:
        doc.close()


@pytest.fixture
def origem(tmp_path):
    """Um 'relatório' de 3 páginas com cabeçalho e logotipo repetidos, fotos e tabela."""
    fotos = tmp_path / "fotos"
    fotos.mkdir()
    logo = _foto(fotos / "logo.jpg", (20, 90, 160), (120, 120))
    f1 = _foto(fotos / "a.jpg", (200, 40, 40))
    f2 = _foto(fotos / "b.jpg", (40, 160, 60), (400, 300))
    f3 = _foto(fotos / "c.jpg", (230, 200, 30))
    spec = {
        "titulo": "Relatório de teste",
        "cabecalho": {"texto": "Escola Modelo | Relatório Individual", "imagem": str(logo)},
        "blocos": [
            {"tipo": "titulo", "texto": "O QUE VIVEMOS NESTE PERÍODO", "nivel": 2},
            {"tipo": "paragrafo", "texto": "A turma explorou frutas, receitas e histórias com atenção e alegria."},
            {"tipo": "quebra_de_pagina"},
            {"tipo": "titulo", "texto": "ANÁLISE DA ESCRITA", "nivel": 2},
            {"tipo": "imagem", "imagem": str(f1), "largura_mm": 50},
            {"tipo": "paragrafo", "texto": "Parágrafo depois da foto da escrita."},
            {"tipo": "quebra_de_pagina"},
            {"tipo": "titulo", "texto": "PORTFÓLIO", "nivel": 2},
            {"tipo": "galeria", "imagens": [{"imagem": str(f2), "legenda": "Parque"},
                                            {"imagem": str(f3), "legenda": "Massinha"}]},
            {"tipo": "tabela", "cabecalho": ["Habilidade", "Status"], "linhas": [["EI03EO03", "Desenvolvido"]]},
        ],
    }
    montado = build_pdf(spec, tmp_path, file_name="original.pdf")
    return tmp_path, montado["path"]


def test_extract_splits_text_and_images_and_drops_the_repeated_header(origem):
    media, caminho = origem
    r = extract_pdf(caminho, media)

    assert r["paginas"] == 3
    assert r["pdf_id"] and len(r["pdf_id"]) == 12
    assert any("Escola Modelo" in t for t in r.get("repetido_em_todas", []))
    textos = [b["texto"] for p in r["conteudo"] for b in p["blocos"] if "texto" in b]
    assert not any("Escola Modelo" in t for t in textos)
    assert any("frutas, receitas e histórias" in t for t in textos)

    repetidas = [i for i in r["imagens"] if i.get("repetida")]
    assert len(repetidas) == 1 and len(repetidas[0]["paginas"]) == 3      # o logotipo
    fotos = [i for i in r["imagens"] if not i.get("repetida")]
    assert len(fotos) == 3
    assert all("arquivo" not in i for i in r["imagens"])                  # o modelo usa o id
    for i in r["imagens"]:
        assert resolve_image(i["id"], media, r["pdf_id"]).is_file()


def test_extract_keeps_reading_order_between_text_and_images(origem):
    media, caminho = origem
    r = extract_pdf(caminho, media)
    pagina2 = next(p for p in r["conteudo"] if p["pagina"] == 2)["blocos"]
    rotulos = [b.get("imagem") or b["texto"] for b in pagina2]
    titulo = next(i for i, t in enumerate(rotulos) if "ANÁLISE DA ESCRITA" in t)
    foto = next(i for i, b in enumerate(pagina2) if "imagem" in b)
    depois = next(i for i, t in enumerate(rotulos) if "depois da foto" in t)
    assert titulo < foto < depois
    assert pagina2[titulo].get("tam")                     # título maior que o corpo


def test_rebuild_with_image_ids_produces_the_revised_pdf(origem):
    media, caminho = origem
    r = extract_pdf(caminho, media)
    fotos = [i["id"] for i in r["imagens"] if not i.get("repetida")]
    logo = next(i["id"] for i in r["imagens"] if i.get("repetida"))
    revisado = {
        "pdf_id": r["pdf_id"],
        "cabecalho": {"texto": "Escola Modelo | Relatório revisado", "imagem": logo},
        "rodape": "Página {pagina} de {total}",
        "blocos": [
            {"tipo": "titulo", "texto": "O QUE VIVEMOS NESTE PERÍODO", "nivel": 2},
            {"tipo": "paragrafo", "texto": "Texto **corrigido** conforme os comentários."},
            {"tipo": "lista", "itens": ["primeiro ponto", "segundo ponto"]},
            {"tipo": "galeria", "colunas": 3, "imagens": [{"imagem": f, "legenda": f"Foto {n}"}
                                                          for n, f in enumerate(fotos, 1)]},
        ],
    }
    out = build_pdf(revisado, media, file_name="Relatório - revisado")
    assert out["file"] == "Relatório - revisado.pdf"
    assert out["paginas"] >= 1 and out["size"] > 1000
    texto = _texto_do_pdf(out["path"])
    assert "corrigido" in texto and "Foto 3" in texto and "Página 1 de" in texto

    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    doc = pdfium.PdfDocument(out["path"])
    imagens = sum(len(list(doc[i].get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE]))) for i in range(len(doc)))
    doc.close()
    assert imagens >= 4                                     # 3 fotos + logotipo


def test_same_file_name_does_not_overwrite(tmp_path):
    a = build_pdf({"blocos": ["um"]}, tmp_path, file_name="x.pdf")
    b = build_pdf({"blocos": ["dois"]}, tmp_path, file_name="x.pdf")
    assert a["path"] != b["path"] and b["file"] == "x (2).pdf"


def test_spec_as_json_text_or_bare_list(tmp_path):
    assert build_pdf(json.dumps({"blocos": [{"tipo": "paragrafo", "texto": "oi"}]}), tmp_path)["paginas"] == 1
    assert build_pdf([{"tipo": "titulo", "texto": "só blocos"}], tmp_path)["paginas"] == 1


@pytest.mark.parametrize("ruim", [{}, {"blocos": []}, "não é json", {"blocos": "x"}])
def test_invalid_spec_is_a_clear_error(tmp_path, ruim):
    with pytest.raises(PdfDocError):
        build_pdf(ruim, tmp_path)


def test_unknown_block_type_is_reported_not_fatal(tmp_path):
    out = build_pdf({"blocos": ["texto", {"tipo": "grafico3d"}]}, tmp_path)
    assert any("grafico3d" in a for a in out["avisos"])


def test_image_must_come_from_the_pdf_or_the_media_folder(tmp_path, origem):
    media, caminho = origem
    r = extract_pdf(caminho, media)
    with pytest.raises(PdfDocError, match="pdf_id"):
        resolve_image("img1", media, None)
    with pytest.raises(PdfDocError, match="inválido"):
        resolve_image("img1", media, "../../etc")
    with pytest.raises(PdfDocError, match="não existe"):
        resolve_image("img99", media, r["pdf_id"])
    fora = tmp_path.parent / "fora.jpg"
    _foto(fora, (1, 2, 3))
    with pytest.raises(PdfDocError, match="fora da pasta"):
        resolve_image(str(fora), media, None)
    with pytest.raises(PdfDocError):
        build_pdf({"blocos": [{"tipo": "imagem", "imagem": str(fora)}]}, media)


def test_without_ttf_fonts_accents_survive_and_exotic_glyphs_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(pdfdoc, "find_fonts", lambda: {"base": {}, "emoji": None})
    out = build_pdf({"blocos": [
        {"tipo": "titulo", "texto": "🌱 Conclusão da professora"},
        {"tipo": "paragrafo", "texto": "corpo em მოძრაობ movimento — “aspas” ação"},
        {"tipo": "lista", "itens": ["ítem"]},
    ]}, tmp_path)
    texto = _texto_do_pdf(out["path"])
    assert "Conclusão da professora" in texto and "ação" in texto
    assert "მ" not in texto
    assert "მ" in out["caracteres_removidos"] and "🌱" in out["caracteres_removidos"]
    assert out["avisos"]


def test_emoji_font_failure_falls_back_to_plain_build(tmp_path, monkeypatch):
    original = pdfdoc._Desenhista.desenhar
    tentativas = []

    def quebra_com_emoji(self):
        tentativas.append(bool(self.pdf._fallback_font_ids) if hasattr(self.pdf, "_fallback_font_ids") else None)
        if len(tentativas) == 1:
            raise RuntimeError("fonte de emoji ilegível")
        return original(self)

    monkeypatch.setattr(pdfdoc._Desenhista, "desenhar", quebra_com_emoji)
    out = build_pdf({"blocos": ["olá"]}, tmp_path)
    assert len(tentativas) == 2 and out["paginas"] == 1


def test_long_text_is_cut_by_page_with_a_hint(tmp_path):
    blocos = []
    for n in range(4):
        blocos += [{"tipo": "paragrafo", "texto": f"Página {n} " + "palavra " * 300}, {"tipo": "quebra_de_pagina"}]
    fonte = build_pdf({"blocos": blocos[:-1]}, tmp_path, file_name="longo.pdf")
    r = extract_pdf(fonte["path"], tmp_path, max_chars=3000)
    assert r["cortado_na_pagina"] and "first_page" in r["aviso"]
    resto = extract_pdf(fonte["path"], tmp_path, first_page=r["cortado_na_pagina"])
    assert resto["conteudo"][0]["pagina"] == r["cortado_na_pagina"]


def test_photo_with_rounded_corner_mask_is_saved_as_jpeg_small_logo_as_png(tmp_path):
    foto = Image.new("RGBA", (500, 375), (120, 80, 40, 255))
    for x in range(10):
        foto.putpixel((x, 0), (0, 0, 0, 0))
    for n, x in enumerate(range(0, 500, 3)):
        foto.putpixel((x, 100), (n % 255, (3 * n) % 255, (7 * n) % 255, 255))
    assert pdfdoc._save_image(foto, tmp_path / "foto").suffix == ".jpg"

    logo = Image.new("RGBA", (120, 60), (0, 0, 0, 0))
    for x in range(20, 100):
        logo.putpixel((x, 30), (0, 120, 200, 255))
    assert pdfdoc._save_image(logo, tmp_path / "logo").suffix == ".png"


def test_not_a_pdf_or_missing_file(tmp_path):
    with pytest.raises(PdfDocError):
        extract_pdf(tmp_path / "nao_existe.pdf", tmp_path)
    falso = tmp_path / "falso.pdf"
    falso.write_bytes(b"isto nao e pdf")
    with pytest.raises(PdfDocError):
        extract_pdf(falso, tmp_path)


# ------------------------------------------------------------------ tools do servidor

@pytest.fixture
def server(monkeypatch, tmp_path):
    import importlib

    monkeypatch.setenv("EVOLUTION_BASE_URL", "http://evolution.test")
    monkeypatch.setenv("EVOLUTION_API_TOKEN", "token")
    monkeypatch.setenv("EVOLUTION_INSTANCE_NAME", "inst")
    import evoapi_mcp.server as modulo

    modulo = importlib.reload(modulo)

    class FakeClient:
        media_dir = tmp_path
        enviados = []
        baixar = {}

        def download_media(self, message_id, password=None, **_):
            return self.baixar[message_id]

        def send_file(self, **kwargs):
            self.enviados.append(kwargs)
            return {"key": {"id": "ENVIADO1", "remoteJid": kwargs["number"]}, "status": "PENDING"}

    fake = FakeClient()
    monkeypatch.setattr(modulo, "client", fake)
    return modulo, fake


def test_tools_are_exposed_and_routed(server):
    import asyncio

    modulo, _ = server
    nomes = {t.name for t in asyncio.run(modulo.mcp.list_tools())}
    assert {"open_pdf", "build_pdf"} <= nomes
    assert "open_pdf" in modulo.mcp.instructions and "build_pdf" in modulo.mcp.instructions


def test_open_pdf_from_a_quoted_message_then_build_and_send(server, origem):
    modulo, fake = server
    _media, caminho = origem
    fake.baixar["MSG1"] = {"path": caminho, "mime": "application/pdf"}

    aberto = json.loads(modulo.open_pdf(message_id="MSG1"))
    assert aberto["paginas"] == 3
    foto = next(i["id"] for i in aberto["imagens"] if not i.get("repetida"))

    documento = {"blocos": [{"tipo": "paragrafo", "texto": "revisado"}, {"tipo": "imagem", "imagem": foto}]}
    saida = json.loads(modulo.build_pdf(document=documento, pdf_id=aberto["pdf_id"], number="120363000@g.us",
                                        file_name="Relatório - revisado.pdf", caption="Versão revisada"))
    assert saida["enviado"]
    envio = fake.enviados[-1]
    assert envio["media_type"] == "document" and envio["file_name"] == "Relatório - revisado.pdf"
    assert envio["caption"] == "Versão revisada" and envio["file_path"] == saida["path"]


def test_build_pdf_without_number_only_builds(server):
    modulo, fake = server
    saida = json.loads(modulo.build_pdf(document={"blocos": ["só montar"]}))
    assert saida["paginas"] == 1 and "enviado" not in saida and not fake.enviados


def test_open_pdf_rejects_non_pdf_and_reports_errors(server, tmp_path):
    modulo, fake = server
    fake.baixar["FOTO"] = {"path": str(tmp_path / "x.jpg"), "mime": "image/jpeg"}
    assert "não é PDF" in json.loads(modulo.open_pdf(message_id="FOTO"))["error"]
    assert "message_id" in json.loads(modulo.open_pdf())["error"]
    assert json.loads(modulo.build_pdf(document={"blocos": []}))["error"]
