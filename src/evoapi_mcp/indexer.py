"""Índice de documentos e imagens, feito pelo servidor, sem tokens do Claude.

Max pede ("indexa isso"), o Claude chama uma ferramenta, e o resto é daqui:

fila rápida
  1. obtém o arquivo (mensagem do WhatsApp, caminho no servidor ou link público);
  2. calcula o hash: o mesmo arquivo nunca é indexado duas vezes;
  3. copia para o Drive em INDEXADOS/AAAA/MM.AAAA, a não ser que já esteja lá;
  4. PDF com texto e arquivo de texto: extrai, divide em trechos, gera vetores.
fila pesada
  5. PDF escaneado: renderiza as páginas e faz OCR;
  6. imagem: OCR do texto que houver e vetor visual (CLIP), para achar pelo que mostra.

Do nome no padrão do arquivamento ("15.08.2026 - Aldann (Lote 392) - R$ 490,15.pdf")
saem data, emitente e valor, e da pasta saem empresa e categoria: filtros exatos,
sem ler documento nenhum. Nada é censurado: Max decidiu que o banco é só dele.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
import sys
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from evoapi_mcp import ocr as ocr_padrao
from evoapi_mcp.jobs import PESADA, RAPIDA
from evoapi_mcp.memory import _plain
from evoapi_mcp.vectors import VectorSet
from evoapi_mcp.visual import MIN_VISUAL_SCORE

CHUNK_CHARS = 900
CHUNK_OVERLAP = 150
MAX_CHUNKS = 60
# Abaixo disto, o texto extraído de um PDF é lixo de layout: trata como escaneado.
MIN_TEXT_CHARS = 40
MAX_OCR_PAGES = 20
OCR_PAGE_SIDE = 2000
MAX_FULL_TEXT = 200_000
DRIVE_FOLDER = "INDEXADOS"
MAX_RESULTS = 20
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
TEXT_EXT = {".txt", ".csv", ".md", ".json", ".xml"}

_EXTENSAO = re.compile(r"\.(pdf|png|jpe?g|webp|gif|bmp|tiff?|txt|csv|md|json|xml)$", re.IGNORECASE)
_NOME = re.compile(r"^\s*(\d{2})\.(\d{2})\.(\d{4})\s*-\s*(.+?)\s*-\s*R\$\s*([\d.]+,\d{2})\s*$")
_DATA = re.compile(r"^\s*(\d{2})\.(\d{2})\.(\d{4})")


class IndexerError(Exception):
    """Pedido de índice inválido ou índice indisponível."""


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Índice: {message}", file=sys.stderr)


# ------------------------------------------------------------------ utilidades puras

def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def _data_valida(d: str, m: str, a: str) -> date | None:
    try:
        return date(int(a), int(m), int(d))
    except ValueError:
        return None


def parse_filename(name: str | None) -> dict[str, Any]:
    """Data, emitente e valor de um nome no padrão "DD.MM.AAAA - Emitente - R$ valor"."""
    out: dict[str, Any] = {"doc_date": None, "emitter": None, "amount": None}
    base = _EXTENSAO.sub("", (name or "").strip())
    m = _NOME.match(base)
    if m:
        out["doc_date"] = _data_valida(m.group(1), m.group(2), m.group(3))
        out["emitter"] = m.group(4).strip() or None
        out["amount"] = float(m.group(5).replace(".", "").replace(",", "."))
        return out
    m = _DATA.match(base)
    if m:
        out["doc_date"] = _data_valida(m.group(1), m.group(2), m.group(3))
    return out


def folder_parts(folder: str | None) -> tuple[str | None, str | None]:
    """Empresa e categoria de "MR/2026/08.2026/BOLETO"."""
    partes = [p.strip() for p in (folder or "").replace("\\", "/").split("/") if p.strip()]
    if partes and partes[0].upper() == "FINANCEIRO":
        partes = partes[1:]
    empresa = partes[0] if partes else None
    categoria = partes[-1] if len(partes) >= 2 and partes[-1].isupper() else None
    return empresa, categoria


def chunk_text(text: str | None, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP,
               max_chunks: int = MAX_CHUNKS) -> list[str]:
    """Trechos de até `size` caracteres, cortados em espaço ou quebra de linha, com sobreposição."""
    t = re.sub(r"[ \t]+", " ", text or "").strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    if not t:
        return []
    if len(t) <= size:
        return [t]
    pedacos: list[str] = []
    inicio = 0
    while inicio < len(t) and len(pedacos) < max_chunks:
        fim = min(len(t), inicio + size)
        if fim < len(t):
            corte = max(t.rfind("\n", inicio + size // 2, fim), t.rfind(" ", inicio + size // 2, fim))
            if corte > inicio:
                fim = corte
        pedaco = t[inicio:fim].strip()
        if pedaco:
            pedacos.append(pedaco)
        if fim >= len(t):
            break
        inicio = max(fim - overlap, inicio + 1)
    return pedacos


def kind_of(mime: str | None, path: str | Path) -> str | None:
    m = (mime or "").split(";")[0].strip().lower()
    ext = Path(path).suffix.lower()
    if m == "application/pdf" or ext == ".pdf":
        return "pdf"
    if m.startswith("image/") or ext in IMAGE_EXT:
        return "image"
    if m.startswith("text/") or ext in TEXT_EXT:
        return "text"
    return None


def extract_pdf_text(path: str | Path, password: str | None = None) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted and (not password or not reader.decrypt(password)):
        raise IndexerError("PDF protegido por senha: informe password")
    partes = [(pagina.extract_text() or "") for pagina in reader.pages]
    return "\n".join(partes).strip(), len(reader.pages)


def _render_pdf_pages(path: str | Path, pages: int, password: str | None) -> list[bytes]:
    from evoapi_mcp.rendering import _render_pdf

    return _render_pdf(Path(path), 1, pages, OCR_PAGE_SIDE, password)


def _corta(texto: str | None, n: int) -> str | None:
    if not texto:
        return None
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto if len(texto) <= n else texto[: n - 3] + "..."


# ------------------------------------------------------------------ índice

class DocumentIndex:
    def __init__(
        self,
        store: Any,
        client: Any,
        text_embedder: Any,
        visual_embedder: Any = None,
        queues: Any = None,
        tz: str = "America/Fortaleza",
        drive_folder: str = DRIVE_FOLDER,
        ocr: Any = ocr_padrao,
        ocr_lang: str = "por+eng",
        render_pdf: Callable[[str | Path, int, str | None], list[bytes]] = _render_pdf_pages,
        fetch: Callable[[str], dict] | None = None,
    ):
        self.store = store
        self.client = client
        self.text_embedder = text_embedder
        self.visual = visual_embedder
        self.queues = queues
        self.tz = tz
        self.drive_folder = drive_folder.strip("/") or DRIVE_FOLDER
        self.ocr = ocr
        self.ocr_lang = ocr_lang
        self.render_pdf = render_pdf
        self._fetch = fetch
        self.texts = VectorSet()
        self.visuals = VectorSet()
        self._arquivos: dict[int, dict[str, Any]] = {}
        self._carregado = False
        self._lock_carga = threading.Lock()

    @property
    def visual_ativo(self) -> bool:
        return bool(self.visual is not None and getattr(self.visual, "available", False))

    # ---------------------------------------------------------------- cache

    def _carregar(self) -> None:
        with self._lock_carga:
            if self._carregado:
                return
            arquivos = {a["id"]: a for a in self.store.load_indexed_files()}
            self._arquivos = arquivos
            self.texts.replace(
                self.store.load_chunks(),
                text_of=lambda item: f"{(arquivos.get(item['file_id']) or {}).get('name') or ''} {item['text']}",
            )
            self.visuals.replace(self.store.load_visual_embeddings(), text_of=lambda item: "")
            self._carregado = True
            _log(f"{len(arquivos)} documentos, {len(self.texts)} trechos, {len(self.visuals)} imagens carregados")

    def warm(self) -> None:
        try:
            self._carregar()
            if self.visual_ativo:
                self.visual.embed_query("warm up")
        except Exception as e:
            _log(f"falha ao aquecer: {e}", "ERROR")

    def _atualizar_arquivo(self, fid: int) -> None:
        if not self._carregado:
            return
        linha = self.store.get_indexed_file(fid)
        if linha is None:
            self._arquivos.pop(fid, None)
            return
        self._arquivos[fid] = {k: v for k, v in linha.items() if k not in ("text", "visual_embedding")}

    # ---------------------------------------------------------------- pedido

    def request(self, message_id: str | None = None, file_path: str | None = None, url: str | None = None,
                note: str | None = None, password: str | None = None, copy_to_drive: bool = True,
                wait_s: float = 30, drive: dict | None = None, folder: str | None = None,
                filename: str | None = None, chat: str | None = None) -> dict[str, Any]:
        if not (message_id or file_path or url):
            raise IndexerError("informe message_id, file_path ou url")
        spec = {
            "message_id": message_id, "file_path": file_path, "url": url, "note": (note or "").strip() or None,
            "password": password, "copy_to_drive": copy_to_drive, "drive": drive, "folder": folder,
            "filename": filename, "chat": chat,
        }
        futuro = self.queues.submit(RAPIDA, self._etapa_rapida, spec)
        if not wait_s:
            return {"status": "na fila"}
        limite = time.monotonic() + float(wait_s)
        try:
            etapa = futuro.result(timeout=float(wait_s))
        except FutureTimeout:
            return {"status": "na fila", "aviso": "ainda processando; consulte depois com search_documents"}
        except Exception as e:
            return {"status": "erro", "erro": str(e)}
        pesada = etapa.get("pesada")
        if pesada is not None:
            try:
                pesada.result(timeout=max(0.0, limite - time.monotonic()))
            except FutureTimeout:
                pass
            except Exception:
                pass  # a falha já ficou gravada no documento
        out = self.status(etapa["id"])
        if etapa.get("duplicado"):
            out["duplicado"] = True
        return out

    def _materializar(self, spec: dict[str, Any]) -> tuple[Path, str, str]:
        if spec.get("file_path"):
            caminho = Path(spec["file_path"]).expanduser()
            if not caminho.is_file():
                raise IndexerError(f"arquivo não encontrado no servidor: {caminho}")
            return caminho, caminho.name, mimetypes.guess_type(caminho.name)[0] or ""
        if spec.get("url"):
            if self._fetch is None:
                from evoapi_mcp import weblink

                buscar = weblink.fetch
            else:
                buscar = self._fetch
            baixado = buscar(spec["url"])
            nome = baixado.get("file_name") or "arquivo"
            pasta = Path(self.client.media_dir) / "indice"
            pasta.mkdir(parents=True, exist_ok=True)
            caminho = pasta / f"{hashlib.sha1(baixado['content']).hexdigest()[:10]}_{nome}"
            caminho.write_bytes(baixado["content"])
            return caminho, nome, baixado.get("mime") or mimetypes.guess_type(nome)[0] or ""
        baixado = self.client.download_media(spec["message_id"], password=spec.get("password"))
        caminho = Path(baixado["path"])
        return caminho, baixado.get("file") or caminho.name, baixado.get("mime") or ""

    def _etapa_rapida(self, spec: dict[str, Any]) -> dict[str, Any]:
        caminho, nome, mime = self._materializar(spec)
        sha = sha256_file(caminho)
        existente = self.store.get_indexed_file_by_sha(sha)
        if existente and existente.get("status") == "done":
            if spec.get("note") and spec["note"] not in (existente.get("note") or ""):
                nota = "\n".join(x for x in (existente.get("note"), spec["note"]) if x)
                self.store.update_indexed_file(existente["id"], note=nota)
                self._atualizar_arquivo(existente["id"])
            return {"id": existente["id"], "duplicado": True, "pesada": None}

        nome_final = spec.get("filename") or nome
        tipo = kind_of(mime, nome_final) or kind_of(mime, caminho)
        if tipo is None:
            raise IndexerError(f"tipo de arquivo não suportado para índice: {mime or caminho.suffix}")
        empresa, categoria = folder_parts(spec.get("folder") or (spec.get("drive") or {}).get("folder"))
        campos = {
            "kind": tipo, "name": nome_final, "mime": mime or None, "size": caminho.stat().st_size,
            "message_id": spec.get("message_id"), "chat": spec.get("chat"), "source_url": spec.get("url"),
            "company": empresa, "category": categoria, **parse_filename(nome_final),
            "note": spec.get("note"), "status": "processing", "error": None,
        }
        if existente:
            fid = existente["id"]
            self.store.replace_chunks(fid, [])
            self.store.update_indexed_file(fid, **campos)
        else:
            fid = self.store.add_indexed_file({"sha256": sha, **campos})
        _log(f"#{fid} {nome_final} ({tipo})")

        self._copiar_para_drive(fid, caminho, nome_final, campos["doc_date"], spec)

        if tipo == "text":
            self._gravar_texto(fid, caminho.read_text(encoding="utf-8", errors="replace"), nome_final)
            self._concluir(fid, pages=None, ocr=False)
            return {"id": fid, "pesada": None}
        if tipo == "pdf":
            try:
                texto, paginas = extract_pdf_text(caminho, spec.get("password"))
            except IndexerError as e:
                self._falhar(fid, str(e))
                return {"id": fid, "pesada": None}
            except Exception as e:
                _log(f"#{fid} texto do PDF ilegível ({e}); tentando OCR")
                texto, paginas = "", None
            if len(texto) >= MIN_TEXT_CHARS:
                self._gravar_texto(fid, texto, nome_final)
                self._concluir(fid, pages=paginas, ocr=False)
                return {"id": fid, "pesada": None}
        pesada = self.queues.submit(PESADA, self._etapa_pesada, fid, caminho, tipo, nome_final, spec.get("password"))
        return {"id": fid, "pesada": pesada}

    def _copiar_para_drive(self, fid: int, caminho: Path, nome: str, doc_date: date | None,
                           spec: dict[str, Any]) -> None:
        drive = spec.get("drive")
        if drive:
            self.store.update_indexed_file(
                fid, drive_id=drive.get("id"), drive_link=drive.get("link"),
                drive_folder=drive.get("folder"), drive_owned=False,
            )
            return
        cliente_drive = getattr(self.client, "drive", None)
        if not spec.get("copy_to_drive") or not getattr(cliente_drive, "available", False):
            return
        quando = doc_date or datetime.now(ZoneInfo(self.tz)).date()
        pasta = f"{self.drive_folder}/{quando:%Y}/{quando:%m.%Y}"
        try:
            enviado = cliente_drive.upload_file(caminho, name=nome, folder=pasta)
            self.store.update_indexed_file(
                fid, drive_id=enviado.get("id"), drive_link=enviado.get("link"),
                drive_folder=enviado.get("folder"), drive_owned=True,
            )
        except Exception as e:
            self.store.update_indexed_file(fid, error=f"cópia no Drive falhou: {e}")
            _log(f"#{fid} cópia no Drive falhou: {e}", "ERROR")

    def _etapa_pesada(self, fid: int, caminho: Path, tipo: str, nome: str, password: str | None) -> None:
        avisos: list[str] = []
        texto = ""
        usou_ocr = False
        paginas = None
        try:
            if tipo == "pdf":
                if self.ocr.available():
                    imagens = self.render_pdf(caminho, MAX_OCR_PAGES, password)
                    paginas = len(imagens)
                    texto = "\n\n".join(self.ocr.ocr_image(img, lang=self.ocr_lang) for img in imagens)
                    usou_ocr = True
                else:
                    avisos.append("PDF sem camada de texto e OCR indisponível")
            elif tipo == "image":
                if self.ocr.available():
                    texto = self.ocr.ocr_image(caminho, lang=self.ocr_lang)
                    usou_ocr = True
                else:
                    avisos.append("OCR indisponível")
                if self.visual_ativo:
                    try:
                        vetor = self.visual.embed_image(caminho)
                    except Exception as e:
                        avisos.append(f"busca visual falhou: {e}")
                    else:
                        self.store.update_indexed_file(fid, visual_embedding=[float(x) for x in vetor])
                        if self._carregado:
                            self.visuals.remove_where(lambda item: item.get("id") == fid)
                            self.visuals.add({"id": fid}, vetor, "")
                else:
                    avisos.append("busca visual indisponível")
            if texto.strip():
                self._gravar_texto(fid, texto, nome)
            elif usou_ocr:
                avisos.append("nenhum texto reconhecido")
            self._concluir(fid, pages=paginas, ocr=usou_ocr, aviso="; ".join(avisos) or None)
        except Exception as e:
            self._falhar(fid, str(e))
            raise

    def _gravar_texto(self, fid: int, texto: str, nome: str) -> None:
        partes = chunk_text(texto)
        self.store.update_indexed_file(fid, text=texto[:MAX_FULL_TEXT], text_chars=len(texto))
        if not partes:
            return
        vetores = self.text_embedder.embed([f"{nome}\n{p}" for p in partes])
        self.store.replace_chunks(fid, [(i, p, [float(x) for x in vetores[i]]) for i, p in enumerate(partes)])
        if self._carregado:
            self.texts.remove_where(lambda item: item.get("file_id") == fid)
            for i, parte in enumerate(partes):
                self.texts.add({"file_id": fid, "seq": i, "text": parte}, vetores[i], f"{nome} {parte}")

    def _concluir(self, fid: int, pages: int | None, ocr: bool, aviso: str | None = None) -> None:
        anterior = (self.store.get_indexed_file(fid) or {}).get("error")
        erro = "; ".join(x for x in (anterior, aviso) if x) or None
        self.store.update_indexed_file(
            fid, status="done", pages=pages, ocr=ocr, error=erro, indexed_at=datetime.now(timezone.utc),
        )
        self._atualizar_arquivo(fid)

    def _falhar(self, fid: int, erro: str) -> None:
        self.store.update_indexed_file(fid, status="failed", error=erro[:1000])
        self._atualizar_arquivo(fid)
        _log(f"#{fid} falhou: {erro}", "ERROR")

    # ---------------------------------------------------------------- consulta

    def _para_data(self, valor: Any) -> date | None:
        if isinstance(valor, datetime):
            return valor.astimezone(ZoneInfo(self.tz)).date()
        if isinstance(valor, date):
            return valor
        return None

    @staticmethod
    def _data_param(texto: str | None, nome: str) -> date | None:
        if not texto:
            return None
        try:
            return date.fromisoformat(str(texto).strip()[:10])
        except ValueError:
            raise IndexerError(f"{nome} inválida: {texto!r}; use AAAA-MM-DD") from None

    def search(self, query: str | None = None, visual_query: str | None = None, limit: int = 5,
               emitente: str | None = None, empresa: str | None = None, categoria: str | None = None,
               desde: str | None = None, ate: str | None = None, tipo: str | None = None) -> list[dict[str, Any]]:
        consulta = (query or "").strip()
        visual = (visual_query or "").strip()
        if visual and not self.visual_ativo and not consulta:
            raise IndexerError("busca visual indisponível neste servidor")
        d0 = self._data_param(desde, "desde")
        d1 = self._data_param(ate, "ate")
        n = max(1, min(int(limit or 5), MAX_RESULTS))
        self._carregar()
        arquivos = self._arquivos

        def passa(arquivo: dict[str, Any] | None) -> bool:
            if not arquivo or arquivo.get("status") != "done":
                return False
            if tipo and arquivo.get("kind") != tipo:
                return False
            if emitente and _plain(emitente) not in _plain(f"{arquivo.get('emitter') or ''} {arquivo.get('name') or ''}"):
                return False
            if empresa and _plain(empresa) not in _plain(arquivo.get("company") or ""):
                return False
            if categoria and _plain(categoria) != _plain(arquivo.get("category") or ""):
                return False
            quando = self._para_data(arquivo.get("doc_date")) or self._para_data(arquivo.get("created_at"))
            if d0 and (quando is None or quando < d0):
                return False
            if d1 and (quando is None or quando > d1):
                return False
            return True

        if not consulta and not visual:
            filtrados = [a for a in arquivos.values() if passa(a)]
            filtrados.sort(
                key=lambda a: self._para_data(a.get("doc_date")) or self._para_data(a.get("created_at")) or date.min,
                reverse=True,
            )
            return [self._saida(a) for a in filtrados[:n]]

        melhores: dict[int, dict[str, Any]] = {}
        if consulta:
            vetor = self.text_embedder.embed([consulta])[0]
            filtro = lambda item: passa(arquivos.get(item["file_id"]))  # noqa: E731
            for score, trecho in self.texts.rank(vetor, consulta, n * 8, filtro):
                atual = melhores.get(trecho["file_id"])
                if atual is None or score > atual["score"]:
                    melhores[trecho["file_id"]] = {"score": score, "trecho": trecho["text"], "origem": "texto"}
        if visual and self.visual_ativo:
            vetor = self.visual.embed_query(visual)
            filtro = lambda item: passa(arquivos.get(item["id"]))  # noqa: E731
            for score, item in self.visuals.rank(vetor, None, n * 4, filtro,
                                                  min_score=MIN_VISUAL_SCORE, lexical_weight=0.0):
                atual = melhores.get(item["id"])
                if atual is None:
                    melhores[item["id"]] = {"score": score, "trecho": None, "origem": "visual"}
                else:
                    atual["origem"] = "texto+visual"
        ordem = sorted(melhores.items(), key=lambda par: -par[1]["score"])[:n]
        return [self._saida(arquivos[fid], h["score"], h.get("trecho"), h["origem"]) for fid, h in ordem]

    def _saida(self, arquivo: dict[str, Any], score: float | None = None, trecho: str | None = None,
               origem: str | None = None) -> dict[str, Any]:
        doc_date = self._para_data(arquivo.get("doc_date"))
        criado = self._para_data(arquivo.get("created_at"))
        out = {
            "id": arquivo.get("id"),
            "arquivo": arquivo.get("name"),
            "tipo": arquivo.get("kind"),
            "data": doc_date.isoformat() if doc_date else None,
            "emitente": arquivo.get("emitter"),
            "valor": arquivo.get("amount"),
            "empresa": arquivo.get("company"),
            "categoria": arquivo.get("category"),
            "nota": _corta(arquivo.get("note"), 200),
            "link": arquivo.get("drive_link"),
            "trecho": _corta(trecho, 300),
            "score": round(score, 2) if score is not None else None,
            "origem": origem,
            "indexado_em": criado.isoformat() if criado else None,
        }
        return {k: v for k, v in out.items() if v is not None}

    def status(self, fid: int) -> dict[str, Any]:
        arquivo = self.store.get_indexed_file(int(fid))
        if arquivo is None:
            raise IndexerError(f"documento #{fid} não encontrado")
        out = self._saida(arquivo) | {"status": arquivo.get("status")}
        for chave, nome in (("pages", "paginas"), ("text_chars", "caracteres"), ("error", "aviso")):
            if arquivo.get(chave):
                out[nome] = arquivo[chave]
        if arquivo.get("ocr"):
            out["ocr"] = True
        if arquivo.get("visual_embedding") is not None:
            out["busca_visual"] = True
        return out

    def read(self, fid: int, max_chars: int = 4000) -> dict[str, Any]:
        arquivo = self.store.get_indexed_file(int(fid))
        if arquivo is None:
            raise IndexerError(f"documento #{fid} não encontrado")
        texto = arquivo.get("text") or ""
        out = self.status(fid)
        out["texto"] = texto[:max_chars] if max_chars else texto
        if max_chars and len(texto) > max_chars:
            out["texto_cortado"] = True
        return out

    def delete(self, fid: int) -> dict[str, Any]:
        fid = int(fid)
        arquivo = self.store.get_indexed_file(fid)
        if arquivo is None:
            return {"id": fid, "apagado": False}
        lixeira: Any = None
        if arquivo.get("drive_owned") and arquivo.get("drive_id"):
            try:
                self.client.drive.trash_file(arquivo["drive_id"])
                lixeira = True
            except Exception as e:
                lixeira = f"falhou: {e}"
        apagado = self.store.delete_indexed_file(fid)
        if self._carregado:
            self._arquivos.pop(fid, None)
            self.texts.remove_where(lambda item: item.get("file_id") == fid)
            self.visuals.remove_where(lambda item: item.get("id") == fid)
        out: dict[str, Any] = {"id": fid, "apagado": apagado}
        if lixeira is not None:
            out["drive_lixeira"] = lixeira
        return out

    def describe(self) -> dict[str, Any]:
        return {
            "ativo": True,
            "documentos": self.store.count_indexed_files(),
            "ocr": bool(self.ocr.available()),
            "busca_visual": self.visual_ativo,
            "copia_drive": f"{self.drive_folder}/AAAA/MM.AAAA",
            "filas": self.queues.describe() if self.queues is not None else None,
        }
