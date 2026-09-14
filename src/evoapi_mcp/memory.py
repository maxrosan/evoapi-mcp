"""Memória de longo prazo do assistente (RAG).

Cada acionamento do executor é uma sessão nova: sem isto ele não sabe quem é a
Keilla, onde ficam os bugs do NARA ou o que foi feito com o boleto do mês passado.
Aqui ficam dois tipos de lembrança:

- `fato`: o que Max mandou lembrar ("a contadora da MR é a Keilla");
- `episodio`: o resumo do que o assistente fez ("arquivou o boleto da Aldann em ...").

Busca híbrida, por dois motivos medidos em português:
- vetores (modelo multilíngue local, sem chave) acham o sentido: "contabilidade"
  encontra "contadora", "boneca" encontra "NARA é a boneca educacional";
- palavras em comum acham o que vetor pequeno erra: nomes próprios e termos exatos
  ("senha do boleto" -> "boletos da Aldann pedem o CPF como senha").
Num teste com sete perguntas, só vetores acertou seis; o híbrido, sete.

Os vetores ficam no mesmo Postgres, numa coluna `real[]`, e a comparação é feita em
memória com numpy. Para milhares de lembranças isso leva milissegundos e dispensa
pgvector, que exigiria trocar a imagem do banco.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
KINDS = ("fato", "episodio")
MAX_TEXT = 2000
MAX_RESULTS = 20
# Peso da coincidência de palavras somada à similaridade dos vetores.
LEXICAL_WEIGHT = 0.35
# Abaixo disto a lembrança não tem relação útil com a pergunta.
MIN_SCORE = 0.25
# Acima disto, um texto novo do mesmo tipo é considerado repetido.
DUPLICATE_SCORE = 0.93


class MemoriaError(Exception):
    """Pedido de memória inválido ou memória indisponível."""


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Memória: {message}", file=sys.stderr)


def _plain(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(c for c in t if not unicodedata.combining(c))


_STOP = frozenset(_plain(w) for w in """
a o as os um uma uns umas de da do das dos e em no na nos nas ao aos à às
para pra pro por pelo pela com sem que se é são ser foi era vai vou ter tem
meu minha seu sua isso isto esse essa este esta the and of to
""".split())


def tokens(text: str | None) -> set[str]:
    """Palavras da frase, sem acento, sem as vazias, cortadas em 5 letras.

    O corte é um radical barato: "boleto" e "boletos" viram "bolet".
    """
    return {w[:5] for w in re.findall(r"[a-z0-9]+", _plain(text or "")) if len(w) > 1 and w not in _STOP}


def default_cache_dir() -> str | None:
    """Onde o modelo fica guardado. Na imagem, dentro de HF_HOME, junto do Whisper."""
    explicito = os.environ.get("FASTEMBED_CACHE_PATH", "").strip()
    if explicito:
        return explicito
    hf = os.environ.get("HF_HOME", "").strip()
    return os.path.join(hf, "fastembed") if hf else None


class Embedder:
    """Transforma texto em vetor com um modelo local (fastembed + onnxruntime)."""

    def __init__(self, model: str | None = None, cache_dir: str | None = None):
        self.model = (model or "").strip() or DEFAULT_MODEL
        self.cache_dir = (cache_dir or "").strip() or default_cache_dir()
        self._modelo = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        try:
            import fastembed  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def loaded(self) -> bool:
        return self._modelo is not None

    def _carregar(self):
        with self._lock:
            if self._modelo is None:
                import warnings

                from fastembed import TextEmbedding

                opcoes = {"cache_dir": self.cache_dir} if self.cache_dir else {}
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    self._modelo = TextEmbedding(self.model, **opcoes)
                _log(f"modelo {self.model} carregado")
        return self._modelo

    def embed(self, texts: Iterable[str]):
        """Vetores normalizados (norma 1), um por texto."""
        import numpy as np

        textos = list(texts)
        if not textos:
            return np.zeros((0, 0), dtype=np.float32)
        matriz = np.asarray(list(self._carregar().embed(textos)), dtype=np.float32)
        normas = np.linalg.norm(matriz, axis=1, keepdims=True)
        normas[normas == 0] = 1.0
        return matriz / normas


def preload(model: str | None = None) -> None:
    """Baixa o modelo durante o build da imagem, para o primeiro uso não esperar."""
    Embedder(model or os.environ.get("EVOLUTION_MEMORY_MODEL")).embed(["aquecimento"])
    print("modelo de memória pronto", file=sys.stderr)
    # Os modelos visuais do índice vão na mesma imagem, pela mesma linha do Dockerfile.
    try:
        from evoapi_mcp.visual import preload as preload_visual

        preload_visual()
    except Exception as e:
        print(f"modelos visuais não pré-carregados: {e}", file=sys.stderr)


class MemoryBank:
    """Guarda e busca lembranças. Os vetores ficam no armazenamento; a busca, em memória."""

    def __init__(self, store: Any, embedder: Any, tz: str = "America/Fortaleza"):
        self.store = store
        self.embedder = embedder
        self.tz = tz
        self._itens: list[dict[str, Any]] | None = None
        self._matriz = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- cache

    def _carregar(self) -> list[dict[str, Any]]:
        """Lê tudo do armazenamento uma vez. Chamar com o lock."""
        import numpy as np

        if self._itens is None:
            linhas = self.store.load_memories()
            self._itens = []
            vetores = []
            for linha in linhas:
                item = {k: v for k, v in linha.items() if k != "embedding"}
                item["_tokens"] = tokens(item.get("text"))
                self._itens.append(item)
                vetores.append(linha["embedding"])
            self._matriz = np.asarray(vetores, dtype=np.float32) if vetores else None
            _log(f"{len(self._itens)} lembranças carregadas")
        return self._itens

    def warm(self) -> None:
        """Carrega modelo e lembranças em segundo plano, na subida do servidor."""
        try:
            self.embedder.embed(["aquecimento"])
            with self._lock:
                self._carregar()
        except Exception as e:
            _log(f"falha ao aquecer: {e}", "ERROR")

    # ---------------------------------------------------------------- operações

    @staticmethod
    def _tipo(kind: str | None) -> str:
        tipo = (kind or "fato").strip().casefold()
        tipo = {"episódio": "episodio"}.get(tipo, tipo)
        if tipo not in KINDS:
            raise MemoriaError(f"tipo inválido: {kind!r}; use 'fato' ou 'episodio'")
        return tipo

    def remember(self, text: str, kind: str = "fato", source: str | None = None,
                 chat: str | None = None) -> dict[str, Any]:
        import numpy as np

        texto = (text or "").strip()
        if not texto:
            raise MemoriaError("texto vazio")
        if len(texto) > MAX_TEXT:
            raise MemoriaError(f"texto com {len(texto)} caracteres; o máximo é {MAX_TEXT}. Resuma.")
        tipo = self._tipo(kind)
        vetor = self.embedder.embed([texto])[0]

        with self._lock:
            itens = self._carregar()
            if self._matriz is not None and itens:
                parecidos = self._matriz @ vetor
                for i in np.argsort(-parecidos):
                    if parecidos[i] < DUPLICATE_SCORE:
                        break
                    if itens[i]["kind"] == tipo:
                        return self._saida(itens[i], float(parecidos[i])) | {"duplicada": True}

            mid = self.store.add_memory(tipo, texto, source, chat, [float(x) for x in vetor])
            item = {
                "id": mid, "kind": tipo, "text": texto, "source": source, "chat": chat,
                "created_at": datetime.now(timezone.utc), "_tokens": tokens(texto),
            }
            itens.append(item)
            linha = vetor.reshape(1, -1).astype(np.float32)
            self._matriz = linha if self._matriz is None else np.vstack([self._matriz, linha])
        _log(f"#{mid} ({tipo}) {texto[:60]!r}")
        return self._saida(item)

    def recall(self, query: str, limit: int = 5, kind: str | None = None) -> list[dict[str, Any]]:
        consulta = (query or "").strip()
        if not consulta:
            raise MemoriaError("consulta vazia")
        tipo = self._tipo(kind) if kind else None
        with self._lock:
            itens = list(self._carregar())
            matriz = self._matriz
        if not itens or matriz is None:
            return []

        vetor = self.embedder.embed([consulta])[0]
        semantica = matriz[: len(itens)] @ vetor
        palavras = tokens(consulta)
        achados = []
        for i, item in enumerate(itens):
            if tipo and item["kind"] != tipo:
                continue
            lexica = len(palavras & item["_tokens"]) / len(palavras) if palavras else 0.0
            score = float(semantica[i]) + LEXICAL_WEIGHT * lexica
            if score >= MIN_SCORE:
                achados.append((score, item))
        achados.sort(key=lambda par: -par[0])
        n = max(1, min(int(limit or 5), MAX_RESULTS))
        return [self._saida(item, score) for score, item in achados[:n]]

    def forget(self, mid: int) -> bool:
        import numpy as np

        mid = int(mid)
        apagou = self.store.delete_memory(mid)
        with self._lock:
            if self._itens is not None:
                for i, item in enumerate(self._itens):
                    if item["id"] == mid:
                        del self._itens[i]
                        if self._matriz is not None:
                            self._matriz = np.delete(self._matriz, i, axis=0)
                            if not len(self._matriz):
                                self._matriz = None
                        break
        if apagou:
            _log(f"#{mid} esquecida")
        return apagou

    # ---------------------------------------------------------------- saída

    def _saida(self, item: dict[str, Any], score: float | None = None) -> dict[str, Any]:
        quando = item.get("created_at")
        out = {
            "id": item.get("id"),
            "tipo": item.get("kind"),
            "texto": item.get("text"),
            "fonte": item.get("source"),
            "chat": item.get("chat"),
            "quando": quando.astimezone(ZoneInfo(self.tz)).strftime("%Y-%m-%d") if quando else None,
            "score": round(score, 2) if score is not None else None,
        }
        return {k: v for k, v in out.items() if v is not None}

    def describe(self) -> dict[str, Any]:
        return {
            "ativa": True,
            "modelo": getattr(self.embedder, "model", None),
            "modelo_carregado": getattr(self.embedder, "loaded", None),
            "lembrancas": self.store.count_memories(),
            "armazenamento": getattr(self.store, "kind", None),
        }
