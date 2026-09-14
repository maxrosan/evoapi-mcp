"""Conjunto de vetores em memória, com a busca híbrida da memória de longo prazo.

Usado pelo histórico de conversas e pelo índice de documentos: os vetores ficam no
Postgres, e aqui fica a cópia em RAM que responde às buscas em milissegundos.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from evoapi_mcp.memory import LEXICAL_WEIGHT, MIN_SCORE, tokens


class VectorSet:
    """Itens com vetor normalizado. `rank` soma similaridade e palavras em comum."""

    def __init__(self) -> None:
        self._itens: list[dict[str, Any]] = []
        self._matriz = None
        self.lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._itens)

    def replace(self, rows: list[dict[str, Any]], text_of: Callable[[dict[str, Any]], str]) -> None:
        """Troca todo o conteúdo pelas linhas do armazenamento (as sem vetor são puladas)."""
        import numpy as np

        itens, vetores = [], []
        for linha in rows:
            vetor = linha.get("embedding")
            if vetor is None:
                continue
            item = {k: v for k, v in linha.items() if k != "embedding"}
            item["_tokens"] = tokens(text_of(item))
            itens.append(item)
            vetores.append(vetor)
        matriz = np.asarray(vetores, dtype=np.float32) if vetores else None
        with self.lock:
            self._itens = itens
            self._matriz = matriz

    def add(self, item: dict[str, Any], vector: Any, text: str) -> None:
        import numpy as np

        linha = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        novo = dict(item)
        novo["_tokens"] = tokens(text)
        with self.lock:
            self._itens.append(novo)
            self._matriz = linha if self._matriz is None else np.vstack([self._matriz, linha])

    def remove_where(self, predicate: Callable[[dict[str, Any]], bool]) -> int:
        with self.lock:
            manter = [i for i, item in enumerate(self._itens) if not predicate(item)]
            removidos = len(self._itens) - len(manter)
            if removidos:
                self._itens = [self._itens[i] for i in manter]
                self._matriz = self._matriz[manter] if manter and self._matriz is not None else None
            return removidos

    def rank(
        self,
        query_vector: Any,
        query_text: str | None,
        limit: int = 5,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        min_score: float = MIN_SCORE,
        lexical_weight: float = LEXICAL_WEIGHT,
    ) -> list[tuple[float, dict[str, Any]]]:
        import numpy as np

        with self.lock:
            itens = list(self._itens)
            matriz = self._matriz
        if not itens or matriz is None:
            return []
        semantica = matriz[: len(itens)] @ np.asarray(query_vector, dtype=np.float32)
        palavras = tokens(query_text) if (query_text and lexical_weight) else set()
        achados = []
        for i, item in enumerate(itens):
            if predicate and not predicate(item):
                continue
            lexica = len(palavras & item["_tokens"]) / len(palavras) if palavras else 0.0
            score = float(semantica[i]) + lexical_weight * lexica
            if score >= min_score:
                achados.append((score, item))
        achados.sort(key=lambda par: -par[0])
        return achados[: max(1, int(limit))]
