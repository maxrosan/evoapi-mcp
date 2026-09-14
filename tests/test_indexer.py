"""Índice de documentos e imagens: nome, trechos, hash, OCR, visual, Drive, busca e remoção."""

import importlib.util
from datetime import date

import numpy as np
import pytest

from evoapi_mcp.indexer import (
    DocumentIndex,
    IndexerError,
    chunk_text,
    folder_parts,
    kind_of,
    parse_filename,
    sha256_file,
)
from evoapi_mcp.jobs import JobQueues
from evoapi_mcp.store import MemoryStore
from pdfgen import make_text_pdf
from test_memory import FakeEmbedder

requires_pypdf = pytest.mark.skipif(importlib.util.find_spec("pypdf") is None, reason="pypdf não instalado")

NOTA = "17.08.2026 - Econtec Contabilidade - R$ 350,00.pdf"
TEXTO_NOTA = [
    "NOTA FISCAL DE SERVICO",
    "Emitente: Econtec Contabilidade Ltda",
    "Valor total: R$ 350,00",
    "Servico de contabilidade mensal de agosto",
]


class FakeOcr:
    def __init__(self, texto="COMPROVANTE PIX Valor R$ 150,00 Favorecido Econtec Contabilidade", ok=True):
        self.texto = texto
        self.ok = ok
        self.chamadas = 0

    def available(self):
        return self.ok

    def ocr_image(self, image, lang="por+eng"):
        self.chamadas += 1
        return self.texto


class FakeVisual:
    available = True
    loaded = True

    @staticmethod
    def _vetor(indice):
        v = np.zeros(8, dtype=np.float32)
        v[indice] = 1.0
        return v

    def embed_image(self, path):
        return self._vetor(0 if "ovo" in str(path) else 1)

    def embed_query(self, text):
        return self._vetor(0 if "egg" in text.lower() else 1)


class FakeDrive:
    available = True

    def __init__(self):
        self.enviados = []
        self.lixeira = []

    def upload_file(self, path, name=None, folder=None, mime=None):
        self.enviados.append({"path": str(path), "name": name, "folder": folder})
        n = len(self.enviados)
        return {"id": f"D{n}", "name": name, "folder": f"Claude/FINANCEIRO/{folder}",
                "size": 1, "link": f"https://drive.test/D{n}", "path": str(path)}

    def trash_file(self, file_id):
        self.lixeira.append(file_id)
        return True


class FakeClient:
    def __init__(self, tmp):
        self.media_dir = tmp / "media"
        self.media_dir.mkdir()
        self.drive = FakeDrive()
        self.midias = {}

    def download_media(self, message_id, password=None):
        caminho = self.midias[message_id]
        mime = "application/pdf" if caminho.suffix == ".pdf" else "image/jpeg"
        return {"path": str(caminho), "file": caminho.name, "mime": mime, "size": caminho.stat().st_size}


class Ambiente:
    def __init__(self, tmp):
        self.tmp = tmp
        self.client = FakeClient(tmp)
        self.ocr = FakeOcr()
        self.renders = []
        self.idx = DocumentIndex(
            MemoryStore(), self.client, FakeEmbedder(), FakeVisual(), JobQueues(sync=True),
            ocr=self.ocr, render_pdf=self._render,
        )

    def _render(self, path, pages, password):
        self.renders.append(str(path))
        return [b"pagina 1", b"pagina 2"]

    def arquivo(self, nome, conteudo):
        caminho = self.tmp / nome
        caminho.write_bytes(conteudo)
        return caminho

    def indexar_nota(self, nome=NOTA, linhas=TEXTO_NOTA, **kwargs):
        return self.idx.request(file_path=str(self.arquivo(nome, make_text_pdf(linhas))), wait_s=5, **kwargs)


@pytest.fixture
def amb(tmp_path):
    return Ambiente(tmp_path)


# ---------------------------------------------------------------- funções puras

@pytest.mark.parametrize("nome,data,emitente,valor", [
    (NOTA, date(2026, 8, 17), "Econtec Contabilidade", 350.0),
    ("15.08.2026 - Aldann Construcoes (Lote 392) - R$ 490,15.pdf", date(2026, 8, 15), "Aldann Construcoes (Lote 392)", 490.15),
    ("01.09.2026 - Sol Prime - R$ 12.345,67.PDF", date(2026, 9, 1), "Sol Prime", 12345.67),
    ("05.09.2026 - sem valor.jpg", date(2026, 9, 5), None, None),
    ("31.02.2026 - Data errada - R$ 1,00.pdf", None, "Data errada", 1.0),
    ("CredCob-MAX-Protected.pdf", None, None, None),
    (None, None, None, None),
])
def test_parse_filename(nome, data, emitente, valor):
    assert parse_filename(nome) == {"doc_date": data, "emitter": emitente, "amount": valor}


@pytest.mark.parametrize("pasta,esperado", [
    ("MR/2026/08.2026/BOLETO", ("MR", "BOLETO")),
    ("FINANCEIRO/Sol Prime/2026/09.2026/NOTA", ("Sol Prime", "NOTA")),
    ("Construção - Paizinho Maria/2026", ("Construção - Paizinho Maria", None)),
    ("", (None, None)),
    (None, (None, None)),
])
def test_folder_parts(pasta, esperado):
    assert folder_parts(pasta) == esperado


def test_chunk_text():
    assert chunk_text("") == []
    assert chunk_text("curto") == ["curto"]
    longo = " ".join(f"palavra{i}" for i in range(400))
    pedacos = chunk_text(longo, size=200, overlap=50)
    assert all(len(p) <= 200 for p in pedacos)
    assert pedacos[0].split()[-1] in pedacos[1]          # sobreposição
    assert "palavra399" in pedacos[-1]
    assert len(chunk_text(longo, size=200, overlap=50, max_chunks=3)) == 3


def test_kind_of():
    assert kind_of("application/pdf", "x.bin") == "pdf"
    assert kind_of(None, "foto.JPG") == "image"
    assert kind_of("text/plain; charset=utf-8", "x") == "text"
    assert kind_of("application/vnd.ms-excel", "planilha.xlsx") is None


def test_sha256_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"abc")
    assert sha256_file(p) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


# ---------------------------------------------------------------- indexação

@requires_pypdf
def test_pdf_com_texto_indexa_sem_ocr_e_copia_no_drive(amb):
    r = amb.indexar_nota(note="nota da contabilidade de agosto")
    assert r["status"] == "done"
    assert (r["tipo"], r["emitente"], r["valor"], r["data"]) == ("pdf", "Econtec Contabilidade", 350.0, "2026-08-17")
    assert r["paginas"] == 1
    assert "ocr" not in r
    assert amb.ocr.chamadas == 0
    assert amb.client.drive.enviados[0]["folder"] == "INDEXADOS/2026/08.2026"
    assert r["link"] == "https://drive.test/D1"
    achado = amb.idx.search("contabilidade mensal Econtec")[0]
    assert achado["id"] == r["id"]
    assert "Econtec" in achado["trecho"]
    assert "Valor total" in amb.idx.read(r["id"])["texto"]


@requires_pypdf
def test_mesmo_arquivo_nao_indexa_duas_vezes_e_soma_a_nota(amb):
    conteudo = make_text_pdf(TEXTO_NOTA)
    a = amb.idx.request(file_path=str(amb.arquivo("a.pdf", conteudo)), note="primeira", wait_s=5)
    b = amb.idx.request(file_path=str(amb.arquivo("copia.pdf", conteudo)), note="segunda", wait_s=5)
    assert b["id"] == a["id"]
    assert b["duplicado"] is True
    assert len(amb.client.drive.enviados) == 1
    assert amb.idx.store.get_indexed_file(a["id"])["note"] == "primeira\nsegunda"


@requires_pypdf
def test_pdf_escaneado_vai_para_ocr(amb):
    amb.ocr.texto = "BOLETO ALDANN LOTE 392 VENCIMENTO 15/10/2026 VALOR 490,15"
    caminho = amb.arquivo("boleto_escaneado.pdf", make_text_pdf([]))
    r = amb.idx.request(file_path=str(caminho), wait_s=5)
    assert r["status"] == "done"
    assert r["ocr"] is True
    assert r["paginas"] == 2
    assert amb.renders == [str(caminho)]
    assert amb.ocr.chamadas == 2
    assert amb.idx.search("boleto Aldann lote 392")[0]["id"] == r["id"]


def test_imagem_tem_ocr_e_vetor_visual(amb):
    r = amb.idx.request(file_path=str(amb.arquivo("ovo_com_farofa.jpg", b"\xff\xd8falso")), wait_s=5)
    assert (r["status"], r["tipo"], r["ocr"], r["busca_visual"]) == ("done", "image", True, True)
    visual = amb.idx.search(visual_query="photo of a fried egg")[0]
    assert (visual["id"], visual["origem"]) == (r["id"], "visual")
    assert amb.idx.search("comprovante pix Econtec")[0]["id"] == r["id"]


def test_imagem_sem_ocr_nem_visual_fica_pronta_com_aviso(tmp_path):
    client = FakeClient(tmp_path)
    idx = DocumentIndex(MemoryStore(), client, FakeEmbedder(), None, JobQueues(sync=True), ocr=FakeOcr(ok=False))
    caminho = tmp_path / "foto.png"
    caminho.write_bytes(b"png")
    r = idx.request(file_path=str(caminho), wait_s=5)
    assert r["status"] == "done"
    assert "OCR indisponível" in r["aviso"]
    assert "busca visual indisponível" in r["aviso"]


@requires_pypdf
def test_origem_mensagem_do_whatsapp(amb):
    amb.client.midias["MSG1"] = amb.arquivo(NOTA, make_text_pdf(TEXTO_NOTA))
    r = amb.idx.request(message_id="MSG1", wait_s=5)
    assert r["status"] == "done"
    assert amb.idx.store.get_indexed_file(r["id"])["message_id"] == "MSG1"


@requires_pypdf
def test_origem_link_publico_grava_no_servidor(tmp_path):
    client = FakeClient(tmp_path)
    conteudo = make_text_pdf(TEXTO_NOTA)
    idx = DocumentIndex(
        MemoryStore(), client, FakeEmbedder(), None, JobQueues(sync=True), ocr=FakeOcr(),
        fetch=lambda url: {"content": conteudo, "mime": "application/pdf", "file_name": "nota.pdf", "url": url},
    )
    r = idx.request(url="https://exemplo.test/nota.pdf", wait_s=5)
    assert r["status"] == "done"
    assert list((client.media_dir / "indice").glob("*_nota.pdf"))
    assert idx.store.get_indexed_file(r["id"])["source_url"] == "https://exemplo.test/nota.pdf"


@requires_pypdf
def test_arquivo_ja_arquivado_usa_o_link_e_nao_vai_para_lixeira(amb):
    drive = {"id": "ARQ1", "link": "https://drive.test/ARQ1",
             "folder": "Claude/FINANCEIRO/MR/2026/08.2026/NOTA", "name": NOTA}
    caminho = amb.arquivo("baixado.pdf", make_text_pdf(TEXTO_NOTA))
    r = amb.idx.request(file_path=str(caminho), drive=drive, folder="MR/2026/08.2026/NOTA",
                        filename=NOTA, wait_s=5)
    assert amb.client.drive.enviados == []
    assert (r["link"], r["empresa"], r["categoria"], r["emitente"]) == ("https://drive.test/ARQ1", "MR", "NOTA", "Econtec Contabilidade")
    assert amb.idx.delete(r["id"]) == {"id": r["id"], "apagado": True}
    assert amb.client.drive.lixeira == []


@requires_pypdf
def test_apagar_tira_da_busca_e_manda_copia_para_lixeira(amb):
    r = amb.indexar_nota()
    assert amb.idx.search("Econtec")
    assert amb.idx.delete(r["id"]) == {"id": r["id"], "apagado": True, "drive_lixeira": True}
    assert amb.client.drive.lixeira == ["D1"]
    assert amb.idx.search("Econtec") == []
    assert amb.idx.delete(r["id"]) == {"id": r["id"], "apagado": False}


@requires_pypdf
def test_falha_no_drive_nao_impede_o_indice(amb):
    def explode(*args, **kwargs):
        raise RuntimeError("cota excedida")

    amb.client.drive.upload_file = explode
    r = amb.indexar_nota()
    assert r["status"] == "done"
    assert "cota excedida" in r["aviso"]
    assert "link" not in r


def test_erros_de_pedido(amb):
    with pytest.raises(IndexerError, match="informe"):
        amb.idx.request()
    r = amb.idx.request(file_path=str(amb.arquivo("planilha.xlsx", b"PK")), wait_s=5)
    assert r["status"] == "erro"
    assert "não suportado" in r["erro"]
    r = amb.idx.request(file_path=str(amb.tmp / "nao_existe.pdf"), wait_s=5)
    assert r["status"] == "erro"


def test_sem_esperar_devolve_na_fila(amb):
    assert amb.idx.request(file_path=str(amb.arquivo("foto.png", b"x")), wait_s=0) == {"status": "na fila"}


# ---------------------------------------------------------------- busca

@requires_pypdf
def test_listar_por_filtros_do_mais_recente(amb):
    amb.indexar_nota("10.07.2026 - Econtec Contabilidade - R$ 350,00.pdf", TEXTO_NOTA + ["julho"])
    amb.indexar_nota(NOTA, TEXTO_NOTA + ["agosto"])
    amb.indexar_nota("20.08.2026 - Brisanet - R$ 99,90.pdf", ["Fatura Brisanet internet residencial valor 99,90 agosto"])
    assert [d["data"] for d in amb.idx.search(emitente="econtec")] == ["2026-08-17", "2026-07-10"]
    assert [d["arquivo"] for d in amb.idx.search(desde="2026-08-18")] == ["20.08.2026 - Brisanet - R$ 99,90.pdf"]
    assert amb.idx.search(tipo="image") == []
    with pytest.raises(IndexerError, match="desde"):
        amb.idx.search(desde="ontem")


def test_busca_visual_sem_modelo(tmp_path):
    idx = DocumentIndex(MemoryStore(), FakeClient(tmp_path), FakeEmbedder(), None, JobQueues(sync=True), ocr=FakeOcr())
    with pytest.raises(IndexerError, match="visual"):
        idx.search(visual_query="a receipt")


@requires_pypdf
def test_outro_processo_encontra_o_que_foi_indexado(amb):
    r = amb.indexar_nota()
    novo = DocumentIndex(amb.idx.store, amb.client, FakeEmbedder(), FakeVisual(), JobQueues(sync=True), ocr=FakeOcr())
    assert novo.search("Econtec contabilidade")[0]["id"] == r["id"]


def test_describe(amb):
    d = amb.idx.describe()
    assert (d["ativo"], d["documentos"], d["ocr"], d["busca_visual"]) == (True, 0, True, True)
    assert d["copia_drive"] == "INDEXADOS/AAAA/MM.AAAA"
    assert set(d["filas"]) == {"rapida", "pesada"}
