"""Duas filas: contagem, ordem, isolamento entre rápida e pesada."""

import threading

import pytest

from evoapi_mcp.jobs import PESADA, RAPIDA, JobQueues


def test_modo_sincrono_conta_sucesso_e_falha():
    filas = JobQueues(sync=True)
    assert filas.submit(RAPIDA, lambda x: x * 2, 21).result() == 42

    def explode():
        raise RuntimeError("quebrou")

    futuro = filas.submit(PESADA, explode)
    with pytest.raises(RuntimeError, match="quebrou"):
        futuro.result()
    assert filas.describe() == {
        "rapida": {"na_fila": 0, "concluidas": 1, "falhas": 0},
        "pesada": {"na_fila": 0, "concluidas": 0, "falhas": 1},
    }


def test_fila_desconhecida():
    with pytest.raises(ValueError):
        JobQueues(sync=True).submit("lenta", lambda: None)


def test_cada_fila_tem_sua_thread_e_mantem_a_ordem():
    filas = JobQueues()
    try:
        ordem = []
        nomes = set()

        def tarefa(i):
            nomes.add(threading.current_thread().name.rsplit("_", 1)[0])
            ordem.append(i)

        futuros = [filas.submit(RAPIDA, tarefa, i) for i in range(10)]
        futuros.append(filas.submit(PESADA, tarefa, 99))
        for f in futuros:
            f.result(timeout=5)
        assert [i for i in ordem if i != 99] == list(range(10))
        assert nomes == {"fila-rapida", "fila-pesada"}
    finally:
        filas.shutdown()


def test_pesada_travada_nao_segura_a_rapida():
    filas = JobQueues()
    liberar = threading.Event()
    try:
        pesada = filas.submit(PESADA, liberar.wait, 5)
        rapida = filas.submit(RAPIDA, lambda: "pronto")
        assert rapida.result(timeout=2) == "pronto"
        assert not pesada.done()
        assert filas.describe()["pesada"]["na_fila"] == 1
    finally:
        liberar.set()
        filas.shutdown()
