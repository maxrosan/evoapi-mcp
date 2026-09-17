"""Abrir um PDF em partes e montar um PDF novo a partir delas.

Para "ajusta esse relatório com base nos comentários": editar o PDF por cima não
funciona, porque num PDF montado o texto não reflui (um parágrafo que cresce invade
o de baixo). O caminho é desmontar e remontar:

1. `extract_pdf` separa o texto de cada página, em blocos na ordem de leitura, e grava
   as imagens no disco, cada uma com um id (`img1`, `img2`...). Cabeçalho e rodapé que
   se repetem em quase toda página saem uma vez só, para não pagar os mesmos tokens
   nove vezes. Quem pediu lê só o texto; as fotos ficam no servidor.
2. O modelo reescreve o documento como uma lista de blocos (título, parágrafo, lista,
   imagem, galeria, tabela...) apontando para as imagens pelo id.
3. `build_pdf` desenha o PDF novo com as imagens originais, sem que um pixel passe
   pela conversa.

Extração por `pypdfium2` e desenho por `fpdf2` (Python puro). Fontes: DejaVu no
servidor (apt fonts-dejavu-core), Arial no Windows, e emoji colorido quando existe uma
fonte de emoji. Sem nenhuma TTF, cai na Helvetica embutida, que só tem Latin-1.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

MAX_CHARS = 30000
MIN_IMAGE_PX = 24          # menor que isso é ícone ou fio decorativo
PARTS_DIRNAME = "pdf"
BUILT_DIRNAME = "pdf_gerado"


class PdfDocError(Exception):
    """Erro ao abrir ou montar um PDF."""


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] PdfDoc: {message}", file=sys.stderr)


# ------------------------------------------------------------------ extração

def _require_pdfium():
    try:
        import pypdfium2 as pdfium
        import pypdfium2.raw as raw
    except ImportError:
        raise PdfDocError('pypdfium2 não instalado (pip install -e ".[image]")')
    return pdfium, raw


def _page_lines(textpage, raw) -> list[dict]:
    """Linhas da página na ordem do texto, com posição, tamanho da fonte e peso."""
    linhas: list[dict] = []
    atual: dict | None = None
    total = textpage.count_chars()
    i = 0
    while i < total:
        codigo = raw.FPDFText_GetUnicode(textpage.raw, i)
        indice = i
        i += 1
        if 0xD800 <= codigo < 0xDC00 and i < total:        # emoji vem em dois pedaços
            baixo = raw.FPDFText_GetUnicode(textpage.raw, i)
            if 0xDC00 <= baixo < 0xE000:
                codigo = 0x10000 + ((codigo - 0xD800) << 10) + (baixo - 0xDC00)
                i += 1
        try:
            caractere = chr(codigo)
        except ValueError:
            continue
        if caractere in "\r\n":
            if atual is not None:
                linhas.append(atual)
                atual = None
            continue
        if atual is None:
            atual = {"texto": "", "top": None, "bottom": None, "left": None, "size": 0.0, "bold": False}
        atual["texto"] += caractere
        if caractere.isspace() or unicodedata.category(caractere).startswith("C"):
            continue
        esquerda, baixo_, direita, topo = textpage.get_charbox(indice)
        if atual["top"] is None:
            atual.update(top=topo, bottom=baixo_, left=esquerda)
            atual["size"] = float(raw.FPDFText_GetFontSize(textpage.raw, indice) or 0)
            atual["bold"] = (raw.FPDFText_GetFontWeight(textpage.raw, indice) or 0) >= 600
        else:
            atual["top"] = max(atual["top"], topo)
            atual["bottom"] = min(atual["bottom"], baixo_)
            atual["left"] = min(atual["left"], esquerda)
    if atual is not None:
        linhas.append(atual)
    saida = []
    for linha in linhas:
        texto = re.sub(r"\s+", " ", linha["texto"]).strip()
        if texto and linha["top"] is not None:
            linha["texto"] = texto
            saida.append(linha)
    return saida


def _normal(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip().casefold()


def _save_image(pil, destino_sem_ext: Path) -> Path:
    """Foto vira JPEG; PNG só para imagem pequena com transparência de verdade (logotipo).

    O Chromium recorta foto com canto arredondado usando máscara: gravada em PNG, cada
    foto do portfólio passava de 300 KB e o relatório de 1 MB voltava com 4 MB. Sobre
    fundo branco o canto continua arredondado na página branca.
    """
    tem_alfa = pil.mode in ("RGBA", "LA", "PA") and pil.convert("RGBA").getchannel("A").getextrema()[0] < 250
    if tem_alfa and max(pil.size) <= 400 and len(pil.convert("RGB").getcolors(4096) or []) < 4096:
        caminho = destino_sem_ext.with_suffix(".png")
        pil.save(caminho, "PNG", optimize=True)
        return caminho
    if tem_alfa:
        from PIL import Image

        fundo = Image.new("RGB", pil.size, (255, 255, 255))
        fundo.paste(pil.convert("RGBA"), mask=pil.convert("RGBA").getchannel("A"))
        pil = fundo
    caminho = destino_sem_ext.with_suffix(".jpg")
    pil.convert("RGB").save(caminho, "JPEG", quality=88, optimize=True)
    return caminho


def _paragraphs(linhas: list[dict], tamanho_corpo: float) -> list[dict]:
    """Junta linhas seguidas do mesmo estilo em parágrafos."""
    blocos: list[dict] = []
    anterior = None
    for linha in linhas:
        altura = max(linha["top"] - linha["bottom"], linha["size"] * 0.7, 1.0)
        continua = (
            anterior is not None
            and abs(linha["size"] - anterior["size"]) <= 0.6
            and linha["bold"] == anterior["bold"]
            and 0 <= anterior["bottom"] - linha["top"] <= altura * 1.1
        )
        if continua:
            blocos[-1]["texto"] += " " + linha["texto"]
            blocos[-1]["_bottom"] = linha["bottom"]
        else:
            bloco: dict[str, Any] = {"texto": linha["texto"], "_top": linha["top"], "_bottom": linha["bottom"]}
            if tamanho_corpo and abs(linha["size"] - tamanho_corpo) > 0.6:
                bloco["tam"] = round(linha["size"], 1)
            if linha["bold"]:
                bloco["negrito"] = True
            blocos.append(bloco)
        anterior = linha
    return blocos


def extract_pdf(
    path: str | Path,
    media_dir: str | Path,
    password: str | None = None,
    first_page: int = 1,
    last_page: int | None = None,
    max_chars: int = MAX_CHARS,
) -> dict:
    """Desmonta o PDF: texto por página em blocos e imagens gravadas no disco.

    Returns: {pdf_id, arquivo, paginas, imagens:[{id, px, paginas, repetida?}],
              repetido_em_todas?, conteudo:[{pagina, blocos:[{texto, tam?, negrito?} |
              {imagem, x, largura}]}], cortado_na_pagina?}
    """
    pdfium, raw = _require_pdfium()
    origem = Path(path)
    if not origem.is_file():
        raise PdfDocError(f"arquivo não encontrado: {path}")
    dados = origem.read_bytes()
    pdf_id = hashlib.sha1(dados).hexdigest()[:12]
    pasta = Path(media_dir) / PARTS_DIRNAME / pdf_id
    pasta.mkdir(parents=True, exist_ok=True)
    try:
        documento = pdfium.PdfDocument(dados, password=password)
    except pdfium.PdfiumError as e:
        raise PdfDocError(f"não consegui abrir o PDF ({e}); se tiver senha, passe password")

    try:
        total = len(documento)
        primeira = max(1, int(first_page or 1))
        ultima = min(total, int(last_page or total))
        imagens: dict[str, dict] = {}        # hash -> registro
        paginas: list[dict] = []
        for numero in range(primeira, ultima + 1):
            pagina = documento[numero - 1]
            largura_pag, altura_pag = pagina.get_size()
            textpage = pagina.get_textpage()
            linhas = _page_lines(textpage, raw)
            figuras = []
            for objeto in pagina.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=6):
                try:
                    px = objeto.get_px_size()
                    if min(px) < MIN_IMAGE_PX:
                        continue
                    esquerda, baixo, direita, topo = objeto.get_bounds()
                    try:
                        chave = hashlib.sha1(objeto.get_data(decode_simple=False).tobytes()).hexdigest()
                    except Exception:
                        chave = None
                    bitmap = objeto.get_bitmap(render=True).to_pil()
                    if chave is None:
                        chave = hashlib.sha1(bitmap.tobytes()).hexdigest()
                except Exception as e:  # imagem com filtro exótico: segue sem ela
                    _log(f"imagem ignorada na página {numero}: {e}", "WARN")
                    continue
                registro = imagens.get(chave)
                if registro is None:
                    ident = f"img{len(imagens) + 1}"
                    arquivo = _save_image(bitmap, pasta / ident)
                    registro = {"id": ident, "arquivo": str(arquivo), "px": list(bitmap.size), "paginas": []}
                    imagens[chave] = registro
                if numero not in registro["paginas"]:
                    registro["paginas"].append(numero)
                figuras.append({
                    "imagem": registro["id"],
                    "x": round(100 * esquerda / largura_pag),
                    "largura": round(100 * (direita - esquerda) / largura_pag),
                    "_top": topo,
                })
            for linha in linhas:     # faixa de cabeçalho ou rodapé: 15% de cima, 10% de baixo
                linha["margem"] = linha["top"] >= altura_pag * 0.85 or linha["bottom"] <= altura_pag * 0.10
            paginas.append({"pagina": numero, "linhas": linhas, "figuras": figuras})
            pagina.close()
    finally:
        documento.close()

    # Cabeçalho e rodapé: linha que aparece em quase toda página sai uma vez só.
    repetidas: set[str] = set()
    if len(paginas) >= 3:
        contagem = Counter()
        for p in paginas:
            contagem.update({_normal(l["texto"]) for l in p["linhas"] if l.get("margem")})
        minimo = max(3, math.ceil(0.6 * len(paginas)))
        repetidas = {t for t, n in contagem.items() if n >= minimo and len(t) <= 160}
    repetidas_texto: list[str] = []
    ids_repetidos = {r["id"] for r in imagens.values() if len(paginas) >= 3
                     and len(r["paginas"]) >= max(3, math.ceil(0.6 * len(paginas)))}

    tamanhos = Counter()
    for p in paginas:
        for l in p["linhas"]:
            tamanhos[round(l["size"], 1)] += len(l["texto"])
    corpo = tamanhos.most_common(1)[0][0] if tamanhos else 0.0

    conteudo = []
    caracteres = 0
    cortado = None
    for p in paginas:
        linhas = []
        for l in p["linhas"]:
            if l.get("margem") and _normal(l["texto"]) in repetidas:
                if l["texto"] not in repetidas_texto and _normal(l["texto"]) not in {_normal(t) for t in repetidas_texto}:
                    repetidas_texto.append(l["texto"])
                continue
            linhas.append(l)
        blocos = _paragraphs(linhas, corpo)
        figuras = sorted((f for f in p["figuras"] if f["imagem"] not in ids_repetidos),
                         key=lambda f: (-round(f["_top"] / 4), f["x"]))
        ordenados: list[dict] = []
        restantes = list(figuras)
        for bloco in blocos:
            while restantes and restantes[0]["_top"] > bloco["_top"] + 2:
                ordenados.append(restantes.pop(0))
            ordenados.append(bloco)
        ordenados.extend(restantes)
        limpos = [{k: v for k, v in b.items() if not k.startswith("_")} for b in ordenados]
        tamanho = sum(len(b.get("texto", "")) for b in limpos)
        if caracteres and caracteres + tamanho > max_chars:
            cortado = p["pagina"]
            break
        caracteres += tamanho
        conteudo.append({"pagina": p["pagina"], "blocos": limpos})

    lista_imagens = []
    for r in imagens.values():
        item = {k: v for k, v in r.items() if k != "arquivo"}   # o modelo usa o id, não o caminho
        if r["id"] in ids_repetidos:
            item["repetida"] = True       # logotipo de cabeçalho, em geral
        lista_imagens.append(item)

    resultado: dict[str, Any] = {
        "pdf_id": pdf_id,
        "arquivo": origem.name,
        "paginas": total,
        "imagens": lista_imagens,
        "conteudo": conteudo,
        "caracteres": caracteres,
    }
    if repetidas_texto:
        resultado["repetido_em_todas"] = repetidas_texto
    if primeira > 1 or ultima < total:
        resultado["trecho"] = [primeira, ultima]
    if cortado:
        resultado["cortado_na_pagina"] = cortado
        resultado["aviso"] = f"texto longo: chame de novo com first_page={cortado} para o resto"
    (pasta / "partes.json").write_text(json.dumps(resultado, ensure_ascii=False), encoding="utf-8")
    return resultado


# ------------------------------------------------------------------ fontes

_FONT_CANDIDATES = {
    "": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"],
    "B": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "I": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf", "C:/Windows/Fonts/ariali.ttf"],
    "BI": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf", "C:/Windows/Fonts/arialbi.ttf"],
}
_EMOJI_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "C:/Windows/Fonts/seguiemj.ttf",
]


def find_fonts() -> dict:
    """{"base": {estilo: caminho}, "emoji": caminho|None}. Variável EVOLUTION_PDF_FONT_DIR tem prioridade."""
    pasta = os.environ.get("EVOLUTION_PDF_FONT_DIR", "").strip()
    base: dict[str, str] = {}
    if pasta:
        nomes = {"": "Regular", "B": "Bold", "I": "Italic", "BI": "BoldItalic"}
        for estilo, sufixo in nomes.items():
            achados = sorted(Path(pasta).glob(f"*-{sufixo}.ttf")) if Path(pasta).is_dir() else []
            if achados:
                base[estilo] = str(achados[0])
    if "" not in base:
        base = {}
        for estilo, candidatos in _FONT_CANDIDATES.items():
            for c in candidatos:
                if Path(c).is_file():
                    base[estilo] = c
                    break
    if "" not in base:
        base = {}
    emoji = next((c for c in _EMOJI_CANDIDATES if Path(c).is_file()), None)
    return {"base": base, "emoji": emoji}


def _cmap(caminho: str) -> set[int]:
    try:
        from fontTools.ttLib import TTFont
        with TTFont(caminho, lazy=True, fontNumber=0) as fonte:
            return set(fonte.getBestCmap() or {})
    except Exception:
        return set()


_TROCAS_LATIN1 = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
    "\u2026": "...", "\u2022": "-", "\u00a0": " ", "\u2192": "->",
}


# ------------------------------------------------------------------ montagem

_ALINHAMENTO = {"esquerda": "L", "centro": "C", "direita": "R", "justificado": "J"}


def _cor(valor: str | None, padrao: tuple[int, int, int]) -> tuple[int, int, int]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", (valor or "").strip())
    if not m:
        return padrao
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _images_root(media_dir: Path) -> Path:
    return (Path(media_dir)).resolve()


def resolve_image(ref: str, media_dir: str | Path, pdf_id: str | None) -> Path:
    """`img3` (do PDF aberto) ou caminho de arquivo dentro da pasta de mídia do servidor."""
    ref = str(ref or "").strip()
    if not ref:
        raise PdfDocError("bloco de imagem sem id")
    raiz = _images_root(Path(media_dir))
    if re.fullmatch(r"img\d+", ref):
        if not pdf_id:
            raise PdfDocError(f"imagem {ref}: informe o pdf_id devolvido por open_pdf")
        if not re.fullmatch(r"[0-9a-f]{12}", pdf_id):
            raise PdfDocError(f"pdf_id inválido: {pdf_id}")
        pasta = raiz / PARTS_DIRNAME / pdf_id
        achados = sorted(pasta.glob(f"{ref}.*"))
        achados = [a for a in achados if a.suffix.lower() in (".jpg", ".jpeg", ".png")]
        if not achados:
            raise PdfDocError(f"imagem {ref} não existe no PDF {pdf_id} (abra de novo com open_pdf)")
        return achados[0]
    caminho = Path(ref).expanduser().resolve()
    if raiz not in caminho.parents:
        raise PdfDocError(f"imagem fora da pasta de mídia do servidor: {ref}")
    if not caminho.is_file():
        raise PdfDocError(f"imagem não encontrada: {ref}")
    return caminho


class _Desenhista:
    def __init__(self, spec: dict, media_dir: Path, pdf_id: str | None, usar_emoji: bool):
        from fpdf import FPDF

        self.spec = spec
        self.media_dir = media_dir
        self.pdf_id = spec.get("pdf_id") or pdf_id
        self.avisos: list[str] = []
        orientacao = "L" if str(spec.get("orientacao", "")).lower().startswith("paisag") else "P"
        formato = str(spec.get("tamanho") or "A4").upper()
        if formato not in ("A4", "A5", "A3", "LETTER", "LEGAL"):
            formato = "A4"
        desenhista = self

        class _PDF(FPDF):
            def header(self):
                desenhista._cabecalho()

            def footer(self):
                desenhista._rodape()

        self.pdf = _PDF(orientation=orientacao, unit="mm", format=formato)
        margem = float(spec.get("margem_mm") or 16)
        margem = min(max(margem, 5), 40)
        self.pdf.set_margins(margem, margem, margem)
        self.pdf.set_auto_page_break(True, margin=margem + 4)
        self.pdf.set_title(str(spec.get("titulo") or "Documento"))
        self.pdf.set_creator("evoapi-mcp")

        fontes = find_fonts()
        self.cobertura: set[int] | None = None
        if fontes["base"]:
            for estilo in ("", "B", "I", "BI"):
                arquivo = fontes["base"].get(estilo) or fontes["base"].get(estilo[:1]) or fontes["base"][""]
                self.pdf.add_font("base", estilo, arquivo)
            self.familia = "base"
            self.cobertura = _cmap(fontes["base"][""])
            if usar_emoji and fontes["emoji"]:
                try:
                    self.pdf.add_font("emoji", "", fontes["emoji"])
                    self.pdf.set_fallback_fonts(["emoji"], exact_match=False)
                    self.cobertura |= _cmap(fontes["emoji"])
                except Exception as e:
                    self.avisos.append(f"sem emoji colorido ({e})")
        else:
            self.familia = "helvetica"
            self.avisos.append("sem fonte TTF no servidor: acentos fora do Latin-1 e emojis foram removidos")

        self.corpo_pt = float(spec.get("fonte_pt") or 10.5)
        self.corpo_pt = min(max(self.corpo_pt, 7), 16)
        self.destaque = _cor(spec.get("cor_destaque"), (31, 78, 121))
        self.texto_cor = _cor(spec.get("cor_texto"), (33, 33, 33))
        cab = spec.get("cabecalho")
        self.cabecalho = {"texto": cab} if isinstance(cab, str) else (cab or {})
        rod = spec.get("rodape")
        self.rodape = {"texto": rod} if isinstance(rod, str) else (rod or {})
        self.removidos: set[str] = set()

    # --- texto

    def limpo(self, texto: Any) -> str:
        texto = unicodedata.normalize("NFC", str(texto if texto is not None else ""))
        if self.cobertura is None:
            for de, para in _TROCAS_LATIN1.items():
                texto = texto.replace(de, para)
            saida = []
            for c in texto:
                if c in "\n\t" or (ord(c) < 256 and c.isprintable()):
                    saida.append(c)
                else:
                    self.removidos.add(c)
            return "".join(saida)
        if not self.cobertura:
            return texto
        saida = []
        for c in texto:
            o = ord(c)
            if c in "\n\t " or o in self.cobertura or o in (0xFE0F, 0x200D):
                saida.append(c)
            else:
                self.removidos.add(c)
        return re.sub(r"[ \t]{2,}", " ", "".join(saida))

    def fonte(self, estilo: str = "", tamanho: float | None = None, cor=None) -> None:
        self.pdf.set_font(self.familia, estilo, tamanho or self.corpo_pt)
        self.pdf.set_text_color(*(cor or self.texto_cor))

    def altura_linha(self, tamanho: float) -> float:
        return tamanho * 0.3528 * 1.45

    # --- cabeçalho e rodapé

    def _cabecalho(self) -> None:
        cab = self.cabecalho
        if not cab or (self.pdf.page_no() == 1 and cab.get("primeira_pagina") is False):
            return
        pdf = self.pdf
        topo = pdf.t_margin - 10 if pdf.t_margin > 12 else 4
        altura_logo = 0.0
        if cab.get("imagem"):
            try:
                caminho = resolve_image(cab["imagem"], self.media_dir, self.pdf_id)
                pdf.image(str(caminho), x=pdf.l_margin, y=topo, h=9)
                altura_logo = 9
            except Exception as e:
                if "cabeçalho" not in " ".join(self.avisos):
                    self.avisos.append(f"imagem do cabeçalho: {e}")
        if cab.get("texto"):
            self.fonte("", 8, (110, 110, 110))
            pdf.set_xy(pdf.l_margin, topo + (altura_logo - 4) / 2 if altura_logo else topo)
            pdf.cell(pdf.epw, 4, self.limpo(cab["texto"]), align="R")
        linha_y = topo + max(altura_logo, 5) + 1.5
        pdf.set_draw_color(210, 210, 210)
        pdf.line(pdf.l_margin, linha_y, pdf.w - pdf.r_margin, linha_y)
        pdf.set_y(max(pdf.t_margin, linha_y + 4))

    def _rodape(self) -> None:
        texto = self.rodape.get("texto")
        if not texto:
            return
        pdf = self.pdf
        pdf.set_y(-12)
        self.fonte("", 8, (120, 120, 120))
        texto = str(texto).replace("{pagina}", str(pdf.page_no())).replace("{total}", "{nb}")
        pdf.cell(0, 5, self.limpo(texto), align="C")

    # --- blocos

    def cabe(self, altura: float) -> bool:
        return self.pdf.get_y() + altura <= self.pdf.page_break_trigger

    def garante(self, altura: float) -> None:
        if not self.cabe(altura):
            self.pdf.add_page()

    def titulo(self, b: dict) -> None:
        nivel = int(b.get("nivel") or 1)
        tamanho = {1: 20, 2: 15, 3: 12.5}.get(nivel, 12.5)
        if b.get("tam"):
            tamanho = float(b["tam"])
        cor = _cor(b.get("cor"), self.destaque)
        altura = self.altura_linha(tamanho)
        self.garante(altura * 2.5)
        if self.pdf.get_y() > self.pdf.t_margin + 1:
            self.pdf.ln(altura * 0.4)
        self.fonte("B", tamanho, cor)
        self.pdf.multi_cell(0, altura, self.limpo(b.get("texto")), align=_ALINHAMENTO.get(b.get("alinhar"), "L"),
                            new_x="LMARGIN", new_y="NEXT")
        self.pdf.ln(altura * 0.25)

    def paragrafo(self, b: dict) -> None:
        tamanho = float(b.get("tam") or self.corpo_pt)
        estilo = "B" if b.get("negrito") else ""
        estilo += "I" if b.get("italico") else ""
        self.fonte(estilo, tamanho, _cor(b.get("cor"), self.texto_cor))
        altura = self.altura_linha(tamanho)
        self.pdf.multi_cell(0, altura, self.limpo(b.get("texto")), align=_ALINHAMENTO.get(b.get("alinhar"), "J"),
                            markdown=True, new_x="LMARGIN", new_y="NEXT")
        self.pdf.ln(altura * 0.45)

    def lista(self, b: dict) -> None:
        itens = b.get("itens") or []
        self.fonte("", self.corpo_pt)
        altura = self.altura_linha(self.corpo_pt)
        recuo = 6
        for n, item in enumerate(itens, start=1):
            marcador = f"{n}." if b.get("numerada") else ("•" if self.cobertura is not None else "-")
            self.garante(altura)
            y = self.pdf.get_y()
            self.fonte("", self.corpo_pt)
            self.pdf.set_xy(self.pdf.l_margin, y)
            self.pdf.cell(recuo, altura, marcador)
            self.pdf.set_xy(self.pdf.l_margin + recuo, y)
            self.pdf.multi_cell(self.pdf.epw - recuo, altura, self.limpo(item), align="L", markdown=True,
                                new_x="LMARGIN", new_y="NEXT")
            self.pdf.ln(altura * 0.15)
        self.pdf.ln(altura * 0.3)

    def _dimensoes(self, caminho: Path) -> tuple[int, int]:
        from PIL import Image

        with Image.open(caminho) as im:
            return im.size

    def imagem(self, b: dict) -> None:
        caminho = resolve_image(b.get("imagem") or b.get("id") or b.get("arquivo"), self.media_dir, self.pdf_id)
        largura_px, altura_px = self._dimensoes(caminho)
        pdf = self.pdf
        largura = float(b.get("largura_mm") or 0) or min(pdf.epw, largura_px * 0.26)
        largura = min(largura, pdf.epw)
        altura = largura * altura_px / max(largura_px, 1)
        maximo = pdf.page_break_trigger - pdf.t_margin - 20
        if altura > maximo:
            altura = maximo
            largura = altura * largura_px / max(altura_px, 1)
        legenda = self.limpo(b.get("legenda") or "")
        altura_legenda = 0.0
        if legenda:
            self.fonte("I", self.corpo_pt - 1.5, (90, 90, 90))
            altura_legenda = pdf.multi_cell(pdf.epw, self.altura_linha(self.corpo_pt - 1.5), legenda,
                                            align="C", dry_run=True, output="HEIGHT")
        self.garante(altura + altura_legenda + 2)
        alinhar = b.get("alinhar") or "centro"
        x = {"esquerda": pdf.l_margin, "direita": pdf.w - pdf.r_margin - largura}.get(
            alinhar, pdf.l_margin + (pdf.epw - largura) / 2)
        y = pdf.get_y()
        pdf.image(str(caminho), x=x, y=y, w=largura, h=altura)
        pdf.set_y(y + altura + 1.5)
        if legenda:
            self.fonte("I", self.corpo_pt - 1.5, (90, 90, 90))
            pdf.multi_cell(0, self.altura_linha(self.corpo_pt - 1.5), legenda, align="C",
                           new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    def galeria(self, b: dict) -> None:
        itens = [i if isinstance(i, dict) else {"imagem": i} for i in (b.get("imagens") or [])]
        if not itens:
            return
        pdf = self.pdf
        colunas = min(max(int(b.get("colunas") or 3), 1), 5)
        espaco = 4.0
        largura_celula = (pdf.epw - espaco * (colunas - 1)) / colunas
        altura_caixa = float(b.get("altura_mm") or largura_celula * 1.15)
        tamanho_legenda = self.corpo_pt - 1.5
        linha_legenda = self.altura_linha(tamanho_legenda)
        for inicio in range(0, len(itens), colunas):
            linha = itens[inicio:inicio + colunas]
            preparados = []
            maior_legenda = 0.0
            for item in linha:
                caminho = resolve_image(item.get("imagem") or item.get("id") or item.get("arquivo"),
                                        self.media_dir, self.pdf_id)
                lp, ap = self._dimensoes(caminho)
                escala = min(largura_celula / max(lp, 1), altura_caixa / max(ap, 1))
                w, h = lp * escala, ap * escala
                legenda = self.limpo(item.get("legenda") or "")
                altura_leg = 0.0
                if legenda:
                    self.fonte("", tamanho_legenda, (70, 70, 70))
                    altura_leg = pdf.multi_cell(largura_celula, linha_legenda, legenda, align="C",
                                                dry_run=True, output="HEIGHT", markdown=True)
                maior_legenda = max(maior_legenda, altura_leg)
                preparados.append((caminho, w, h, legenda))
            altura_linha = max(h for _, _, h, _ in preparados)
            self.garante(altura_linha + maior_legenda + 4)
            y = pdf.get_y()
            for coluna, (caminho, w, h, legenda) in enumerate(preparados):
                x0 = pdf.l_margin + coluna * (largura_celula + espaco)
                pdf.image(str(caminho), x=x0 + (largura_celula - w) / 2, y=y + (altura_linha - h), w=w, h=h)
                if legenda:
                    self.fonte("", tamanho_legenda, (70, 70, 70))
                    pdf.set_xy(x0, y + altura_linha + 1.5)
                    pdf.multi_cell(largura_celula, linha_legenda, legenda, align="C", markdown=True,
                                   new_x="LEFT", new_y="NEXT")
            pdf.set_xy(pdf.l_margin, y + altura_linha + 1.5 + maior_legenda + espaco)

    def tabela(self, b: dict) -> None:
        cabecalho = [self.limpo(c) for c in (b.get("cabecalho") or [])]
        linhas = [[self.limpo(c) for c in linha] for linha in (b.get("linhas") or [])]
        colunas = max([len(cabecalho)] + [len(l) for l in linhas]) if (cabecalho or linhas) else 0
        if not colunas:
            return
        larguras = b.get("larguras")
        if not (isinstance(larguras, list) and len(larguras) == colunas):
            larguras = None
        tamanho = float(b.get("tam") or self.corpo_pt - 1)
        self.fonte("", tamanho)
        pdf = self.pdf
        from fpdf.fonts import FontFace

        estilo_cab = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=self.destaque)
        with pdf.table(
            col_widths=larguras, text_align="LEFT", line_height=self.altura_linha(tamanho),
            headings_style=estilo_cab, first_row_as_headings=bool(cabecalho), markdown=True,
            borders_layout="HORIZONTAL_LINES", padding=1.5,
        ) as tabela:
            for linha in ([cabecalho] if cabecalho else []) + linhas:
                celulas = tabela.row()
                for i in range(colunas):
                    celulas.cell(linha[i] if i < len(linha) else "")
        pdf.ln(self.altura_linha(tamanho))

    def desenhar(self) -> None:
        pdf = self.pdf
        pdf.add_page()
        desconhecidos = set()
        for b in self.spec.get("blocos") or []:
            if isinstance(b, str):
                b = {"tipo": "paragrafo", "texto": b}
            tipo = str(b.get("tipo") or "paragrafo").lower().replace("í", "i").replace("á", "a")
            if tipo in ("titulo", "heading"):
                self.titulo(b)
            elif tipo in ("paragrafo", "texto"):
                self.paragrafo(b)
            elif tipo in ("lista", "itens"):
                self.lista(b)
            elif tipo == "imagem":
                self.imagem(b)
            elif tipo == "galeria":
                self.galeria(b)
            elif tipo == "tabela":
                self.tabela(b)
            elif tipo == "espaco":
                pdf.ln(float(b.get("mm") or 5))
            elif tipo in ("quebra_de_pagina", "quebra"):
                pdf.add_page()
            elif tipo == "linha":
                pdf.ln(2)
                pdf.set_draw_color(200, 200, 200)
                pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
                pdf.ln(4)
            else:
                desconhecidos.add(tipo)
        if desconhecidos:
            self.avisos.append(f"blocos ignorados (tipo desconhecido): {', '.join(sorted(desconhecidos))}")


def _parse_spec(documento: dict | str) -> dict:
    if isinstance(documento, str):
        try:
            documento = json.loads(documento)
        except ValueError as e:
            raise PdfDocError(f"documento não é um JSON válido: {e}")
    if isinstance(documento, list):
        documento = {"blocos": documento}
    if not isinstance(documento, dict) or not isinstance(documento.get("blocos"), list) or not documento["blocos"]:
        raise PdfDocError("documento precisa de uma lista 'blocos' não vazia")
    return documento


def build_pdf(
    documento: dict | str,
    media_dir: str | Path,
    file_name: str = "documento.pdf",
    pdf_id: str | None = None,
) -> dict:
    """Desenha o PDF e grava em `<media_dir>/pdf_gerado/`.

    Returns: {path, file, size, paginas, avisos?, caracteres_removidos?}
    """
    try:
        import fpdf  # noqa: F401
    except ImportError:
        raise PdfDocError("fpdf2 não instalado (pip install fpdf2)")
    spec = _parse_spec(documento)
    media = Path(media_dir)

    def tentar(usar_emoji: bool) -> _Desenhista:
        d = _Desenhista(spec, media, pdf_id, usar_emoji)
        d.desenhar()
        d.bytes = bytes(d.pdf.output())
        return d

    try:
        desenhista = tentar(True)
    except PdfDocError:
        raise
    except Exception as e:
        _log(f"montagem com emoji falhou ({e}); tentando sem", "WARN")
        try:
            desenhista = tentar(False)
        except PdfDocError:
            raise
        except Exception as e2:
            raise PdfDocError(f"não consegui montar o PDF: {e2}")

    nome = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (file_name or "documento.pdf").strip()) or "documento.pdf"
    if not nome.lower().endswith(".pdf"):
        nome += ".pdf"
    pasta = media / BUILT_DIRNAME
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / nome
    n = 2
    while caminho.exists():
        caminho = pasta / f"{Path(nome).stem} ({n}).pdf"
        n += 1
    caminho.write_bytes(desenhista.bytes)
    resultado: dict[str, Any] = {
        "path": str(caminho),
        "file": caminho.name,
        "size": len(desenhista.bytes),
        "paginas": desenhista.pdf.pages_count,
    }
    if desenhista.avisos:
        resultado["avisos"] = desenhista.avisos
    if desenhista.removidos:
        resultado["caracteres_removidos"] = "".join(sorted(desenhista.removidos))[:60]
    _log(f"PDF montado em {caminho} ({len(desenhista.bytes)} bytes, {resultado['paginas']} páginas)")
    return resultado
