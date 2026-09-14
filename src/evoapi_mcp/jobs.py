"""Duas filas de processamento em segundo plano.

- `rapida`: trabalho leve e frequente, que não deve esperar atrás de nada pesado:
  vetores do histórico de conversas, texto de PDF que já tem camada de texto.
- `pesada`: OCR de imagens e de PDFs escaneados, vetores visuais. Pode levar
  segundos por página e não pode atrasar a fila rápida.

Cada fila tem um único trabalhador, então as tarefas de uma fila saem em ordem e
não disputam CPU entre si. A transcrição dos comandos de voz continua na própria
fila do receptor de webhook, porque ali o que importa é responder rápido.
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

RAPIDA = "rapida"
PESADA = "pesada"
FILAS = (RAPIDA, PESADA)


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Filas: {message}", file=sys.stderr)


class JobQueues:
    """Filas rápida e pesada. `sync=True` executa na hora, para testes."""

    def __init__(self, sync: bool = False):
        self.sync = sync
        self._pools: dict[str, ThreadPoolExecutor] = {} if sync else {
            nome: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"fila-{nome}") for nome in FILAS
        }
        self._stats = {nome: {"na_fila": 0, "concluidas": 0, "falhas": 0} for nome in FILAS}
        self._lock = threading.Lock()

    def submit(self, fila: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        if fila not in self._stats:
            raise ValueError(f"fila desconhecida: {fila!r}")
        with self._lock:
            self._stats[fila]["na_fila"] += 1

        def rodar() -> Any:
            try:
                resultado = fn(*args, **kwargs)
            except Exception as e:
                with self._lock:
                    self._stats[fila]["falhas"] += 1
                _log(f"tarefa na fila {fila} falhou: {e}", "ERROR")
                raise
            finally:
                with self._lock:
                    self._stats[fila]["na_fila"] -= 1
            with self._lock:
                self._stats[fila]["concluidas"] += 1
            return resultado

        if self.sync:
            futuro: Future = Future()
            try:
                futuro.set_result(rodar())
            except Exception as e:
                futuro.set_exception(e)
            return futuro
        return self._pools[fila].submit(rodar)

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return {nome: dict(valores) for nome, valores in self._stats.items()}

    def shutdown(self) -> None:
        for pool in self._pools.values():
            pool.shutdown(wait=False, cancel_futures=True)
