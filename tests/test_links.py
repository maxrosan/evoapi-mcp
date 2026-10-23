"""Link temporário: o arquivo do servidor vira endereço, e o endereço vence."""

import time
from pathlib import Path

import pytest

from evoapi_mcp import links
from evoapi_mcp.links import LinkError, LinkExpired

SEGREDO = "segredo-de-teste"


@pytest.fixture
def pasta(tmp_path):
    """Pasta de mídias com um arquivo dentro."""
    (tmp_path / "boleto.pdf").write_bytes(b"%PDF-1.4 conteudo")
    return tmp_path


def test_link_criado_e_resolvido_devolve_o_mesmo_arquivo(pasta):
    criado = links.build(
        pasta / "boleto.pdf", pasta, "https://servidor.exemplo", SEGREDO, minutes=60
    )
    assert criado["file"] == "boleto.pdf"
    assert criado["size"] == len(b"%PDF-1.4 conteudo")
    assert criado["mime"] == "application/pdf"
    assert criado["url"].startswith("https://servidor.exemplo/dl/")
    assert criado["url"].endswith("/boleto.pdf")

    token = criado["url"].split("/dl/")[1].split("/")[0]
    alvo = links.resolve(token, pasta, SEGREDO)
    assert Path(alvo["path"]) == pasta / "boleto.pdf"
    assert alvo["file"] == "boleto.pdf"
    assert alvo["inline"] is True  # PDF o navegador mostra


def test_barra_final_do_endereco_nao_duplica(pasta):
    criado = links.build(
        pasta / "boleto.pdf", pasta, "https://servidor.exemplo/", SEGREDO, minutes=5
    )
    assert "//dl/" not in criado["url"]


def test_nome_com_espaco_e_acento_vai_escapado(pasta):
    (pasta / "nota fiscal março.pdf").write_bytes(b"%PDF-1.4")
    criado = links.build(
        pasta / "nota fiscal março.pdf", pasta, "https://servidor.exemplo", SEGREDO
    )
    assert " " not in criado["url"]
    assert "março" not in criado["url"]

    token = criado["url"].split("/dl/")[1].split("/")[0]
    assert links.resolve(token, pasta, SEGREDO)["file"] == "nota fiscal março.pdf"


def test_assinatura_mexida_nao_abre(pasta):
    criado = links.build(pasta / "boleto.pdf", pasta, "https://servidor.exemplo", SEGREDO)
    token = criado["url"].split("/dl/")[1].split("/")[0]
    payload, _, assinatura = token.partition(".")

    torto = f"{payload}.{'a' * len(assinatura)}"
    with pytest.raises(LinkError, match="inválido"):
        links.resolve(torto, pasta, SEGREDO)


def test_outro_segredo_nao_abre(pasta):
    criado = links.build(pasta / "boleto.pdf", pasta, "https://servidor.exemplo", SEGREDO)
    token = criado["url"].split("/dl/")[1].split("/")[0]
    with pytest.raises(LinkError, match="inválido"):
        links.resolve(token, pasta, "outro-segredo")


def test_link_vencido_se_identifica_como_vencido(pasta):
    """Quem serve a rota precisa distinguir 'venceu' de 'nunca existiu' (410 x 404)."""
    agora = time.time()
    criado = links.build(
        pasta / "boleto.pdf", pasta, "https://servidor.exemplo", SEGREDO,
        minutes=1, now=agora,
    )
    token = criado["url"].split("/dl/")[1].split("/")[0]

    links.resolve(token, pasta, SEGREDO, now=agora + 59)
    with pytest.raises(LinkExpired):
        links.resolve(token, pasta, SEGREDO, now=agora + 61)


def test_arquivo_fora_da_pasta_nao_ganha_link(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    fora = tmp_path / "segredo.env"
    fora.write_text("TOKEN=123")

    with pytest.raises(LinkError, match="precisa estar dentro"):
        links.build(fora, media, "https://servidor.exemplo", SEGREDO)


def test_caminho_para_fora_assinado_ainda_e_recusado(tmp_path):
    """Mesmo com o segredo em mãos, '..' não sai da pasta de mídias."""
    media = tmp_path / "media"
    media.mkdir()
    (tmp_path / "segredo.env").write_text("TOKEN=123")

    expira = int(time.time() + 600)
    payload = links._b64(f"{expira}:../segredo.env".encode())
    token = f"{payload}.{links._assinar(payload, SEGREDO)}"

    with pytest.raises(LinkError, match="precisa estar dentro"):
        links.resolve(token, media, SEGREDO)


def test_arquivo_apagado_depois_do_link(pasta):
    criado = links.build(pasta / "boleto.pdf", pasta, "https://servidor.exemplo", SEGREDO)
    token = criado["url"].split("/dl/")[1].split("/")[0]
    (pasta / "boleto.pdf").unlink()

    with pytest.raises(LinkError, match="não está mais"):
        links.resolve(token, pasta, SEGREDO)


def test_arquivo_inexistente_nao_ganha_link(pasta):
    with pytest.raises(LinkError, match="não encontrado"):
        links.build(pasta / "nao-existe.pdf", pasta, "https://servidor.exemplo", SEGREDO)


def test_sem_endereco_publico_a_mensagem_diz_a_variavel(pasta):
    with pytest.raises(LinkError, match="EVOLUTION_PUBLIC_URL"):
        links.build(pasta / "boleto.pdf", pasta, "", SEGREDO)


def test_sem_segredo_nao_assina_nem_abre(pasta):
    with pytest.raises(LinkError, match="EVOLUTION_LINK_SECRET"):
        links.build(pasta / "boleto.pdf", pasta, "https://servidor.exemplo", "")
    with pytest.raises(LinkError, match="segredo"):
        links.resolve("qualquer.coisa", pasta, "")


@pytest.mark.parametrize("minutos", [0, -5, links.TTL_MAX_MIN + 1])
def test_prazo_fora_da_faixa(pasta, minutos):
    with pytest.raises(LinkError, match="fora da faixa"):
        links.build(pasta / "boleto.pdf", pasta, "https://servidor.exemplo", SEGREDO, minutes=minutos)


def test_link_malformado(pasta):
    for token in ("", "sem-ponto", ".", "a.b"):
        with pytest.raises(LinkError):
            links.resolve(token, pasta, SEGREDO)


def test_arquivo_que_o_navegador_nao_mostra_vem_para_baixar(pasta):
    (pasta / "pacote.zip").write_bytes(b"PK\x03\x04")
    criado = links.build(pasta / "pacote.zip", pasta, "https://servidor.exemplo", SEGREDO)
    token = criado["url"].split("/dl/")[1].split("/")[0]
    assert links.resolve(token, pasta, SEGREDO)["inline"] is False


def test_segredo_cai_no_token_do_mcp_quando_nao_ha_um_proprio():
    class Config:
        link_secret = ""

    assert links.secret_from(Config(), {"MCP_AUTH_TOKEN": " abc "}) == "abc"

    Config.link_secret = "proprio"
    assert links.secret_from(Config(), {"MCP_AUTH_TOKEN": "abc"}) == "proprio"
    assert links.secret_from(Config(), {}) == "proprio"
