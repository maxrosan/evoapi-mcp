"""A superfície que o modelo vê: quais tools existem e a regra de escolha entre elas.

Cinco tools mandam imagem. Qual usar não pode depender de o modelo ler as cinco
descrições e comparar: a regra vive uma vez, nas instructions do servidor.
"""

import asyncio
import importlib

import pytest


@pytest.fixture
def servidor(monkeypatch):
    """Recarrega o server.py com o ambiente pedido e devolve o módulo."""
    def carregar(**env):
        monkeypatch.setenv("EVOLUTION_BASE_URL", "http://evolution.test")
        monkeypatch.setenv("EVOLUTION_API_TOKEN", "token")
        monkeypatch.setenv("EVOLUTION_INSTANCE_NAME", "inst")
        monkeypatch.delenv("EVOLUTION_BASE64_TOOLS", raising=False)
        for chave, valor in env.items():
            monkeypatch.setenv(chave, valor)
        import evoapi_mcp.server as server
        return importlib.reload(server)

    yield carregar
    # Recarrega sem o flag, com o ambiente ainda de pé: o módulo carrega a
    # configuração no import e chama sys.exit se ela faltar, então desfazer as
    # variáveis ANTES de recarregar derrubaria o teardown.
    carregar()


def nomes(server):
    return sorted(t.name for t in asyncio.run(server.mcp.list_tools()))


def test_the_routing_rule_ships_with_the_server(servidor):
    server = servidor()
    instrucoes = server.mcp.instructions or ""

    # cada origem de arquivo aponta para um caminho, e todos estão ditos
    for tool in ("send_render", "send_url", "send_file", "send_drive_file"):
        assert tool in instrucoes
    assert "base64" in instrucoes


def test_base64_tools_are_hidden_by_default(servidor):
    """Tool visível é tool escolhida: o caminho caro não fica à mão sem querer."""
    assert [n for n in nomes(servidor()) if "base64" in n] == []


def test_base64_tools_come_back_with_the_flag(servidor):
    server = servidor(EVOLUTION_BASE64_TOOLS="1")
    assert [n for n in nomes(server) if "base64" in n] == [
        "get_media_base64", "send_document_base64", "send_image_base64",
    ]


def test_hiding_them_costs_nothing_else(servidor):
    """Só as três somem; o resto da superfície fica igual."""
    sem = set(nomes(servidor()))
    com = set(nomes(servidor(EVOLUTION_BASE64_TOOLS="1")))
    assert com - sem == {"get_media_base64", "send_document_base64", "send_image_base64"}
    assert sem - com == set()
