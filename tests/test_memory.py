"""Memória de longo prazo: tokens, gravação, busca híbrida, duplicatas e esquecimento."""

import sys
import zlib

import numpy as np
import pytest

from evoapi_mcp.memory import (
    DUPLICATE_SCORE,
    MAX_TEXT,
    Embedder,
    MemoriaError,
    MemoryBank,
    default_cache_dir,
    tokens,
)
from evoapi_mcp.store import MemoryStore

DIM = 1024


class FakeEmbedder:
    """Vetor por palavras (hash estável): textos com as mesmas palavras ficam próximos."""

    model = "fake"
    loaded = True
    available = True

    def __init__(self):
        self.chamadas = 0

    def embed(self, texts):
        self.chamadas += 1
        saida = []
        for texto in texts:
            v = np.zeros(DIM, dtype=np.float32)
            for tok in tokens(texto):
                v[zlib.crc32(tok.encode()) % DIM] += 1.0
            n = np.linalg.norm(v)
            saida.append(v / n if n else v)
        return np.asarray(saida, dtype=np.float32)


@pytest.fixture
def banco():
    return MemoryBank(MemoryStore(), FakeEmbedder())


# ---------------------------------------------------------------- tokens

def test_tokens_sem_acento_sem_vazias_e_com_radical():
    assert tokens("Boletos são da Aldann, é?") == {"bolet", "aldan"}
    assert tokens("boleto") == tokens("BOLETOS")
    assert tokens("") == set()
    assert tokens(None) == set()


def test_cache_dir_padrao(monkeypatch):
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    monkeypatch.setenv("HF_HOME", "/opt/models")
    assert default_cache_dir().replace("\\", "/") == "/opt/models/fastembed"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", "/cache")
    assert default_cache_dir() == "/cache"
    monkeypatch.delenv("FASTEMBED_CACHE_PATH")
    monkeypatch.delenv("HF_HOME")
    assert default_cache_dir() is None


def test_embedder_indisponivel_sem_fastembed(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    assert Embedder().available is False


# ---------------------------------------------------------------- gravar

def test_lembrar_devolve_id_tipo_e_data(banco):
    r = banco.remember("A contadora da MR é a Keilla", source="whatsapp")
    assert r["id"] == 1
    assert r["tipo"] == "fato"
    assert r["fonte"] == "whatsapp"
    assert len(r["quando"]) == 10
    assert "duplicada" not in r


@pytest.mark.parametrize("texto,erro", [("   ", "vazio"), ("x" * (MAX_TEXT + 1), "máximo")])
def test_lembrar_recusa_texto_invalido(banco, texto, erro):
    with pytest.raises(MemoriaError, match=erro):
        banco.remember(texto)


def test_tipo_invalido_e_acento_aceito(banco):
    with pytest.raises(MemoriaError, match="tipo"):
        banco.remember("algo", kind="opiniao")
    assert banco.remember("arquivou o boleto", kind="Episódio")["tipo"] == "episodio"


def test_texto_repetido_nao_grava_de_novo(banco):
    a = banco.remember("NARA é a boneca educacional da FAS")
    b = banco.remember("NARA é a boneca educacional da FAS")
    assert b["id"] == a["id"]
    assert b["duplicada"] is True
    assert b["score"] >= DUPLICATE_SCORE
    assert banco.store.count_memories() == 1


def test_mesmo_texto_em_outro_tipo_nao_e_duplicata(banco):
    banco.remember("boleto da Aldann arquivado", kind="fato")
    r = banco.remember("boleto da Aldann arquivado", kind="episodio")
    assert "duplicada" not in r
    assert banco.store.count_memories() == 2


# ---------------------------------------------------------------- buscar

def _carga(banco):
    banco.remember("A contadora da MR Empreendimentos é a Keilla")
    banco.remember("Bugs do NARA vão no quadro Kanban - Nara do Trello")
    banco.remember("Boletos da Aldann pedem os dígitos do CPF como senha")
    banco.remember("Em 14/09 o boleto da Aldann foi arquivado em MR/2026/09.2026/BOLETO", kind="episodio")


def test_busca_traz_o_mais_relevante_primeiro(banco):
    _carga(banco)
    r = banco.recall("qual a senha do boleto da Aldann?")
    assert "CPF" in r[0]["texto"]
    assert all(r[i]["score"] >= r[i + 1]["score"] for i in range(len(r) - 1))


def test_busca_sem_relacao_volta_vazia(banco):
    _carga(banco)
    assert banco.recall("receita de bolo de cenoura") == []


def test_filtro_por_tipo(banco):
    _carga(banco)
    r = banco.recall("boleto Aldann", kind="episodio")
    assert [x["tipo"] for x in r] == ["episodio"]


def test_limite(banco):
    _carga(banco)
    assert len(banco.recall("boleto Aldann", limit=1)) == 1


def test_memoria_vazia_e_consulta_vazia(banco):
    assert banco.recall("qualquer coisa") == []
    with pytest.raises(MemoriaError, match="vazia"):
        banco.recall("  ")


def test_lembrancas_sobrevivem_a_outro_processo():
    store = MemoryStore()
    MemoryBank(store, FakeEmbedder()).remember("O quadro da FAS no Trello se chama Faculdade do Seridó")
    novo = MemoryBank(store, FakeEmbedder())
    assert "Faculdade" in novo.recall("trello da FAS")[0]["texto"]


# ---------------------------------------------------------------- esquecer

def test_esquecer_tira_da_busca_e_do_armazenamento(banco):
    _carga(banco)
    alvo = banco.recall("senha do boleto")[0]
    assert banco.forget(alvo["id"]) is True
    assert all(x["id"] != alvo["id"] for x in banco.recall("senha do boleto"))
    assert banco.forget(alvo["id"]) is False
    assert banco.store.count_memories() == 3


def test_esquecer_a_unica_e_lembrar_de_novo(banco):
    r = banco.remember("fato único")
    banco.forget(r["id"])
    assert banco.recall("fato único") == []
    assert banco.remember("outro fato")["id"] == 2
    assert banco.recall("outro fato")[0]["texto"] == "outro fato"


def test_describe(banco):
    banco.remember("algo")
    assert banco.describe() == {
        "ativa": True, "modelo": "fake", "modelo_carregado": True,
        "lembrancas": 1, "armazenamento": "memoria",
    }
