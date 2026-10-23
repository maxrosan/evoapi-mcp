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

    token = links._token(
        {"exp": int(time.time() + 600), "k": "dl", "rel": "../segredo.env"}, SEGREDO
    )

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


# ----------------------------------------------------------------- subir (/up)


def criar_upload(**destino):
    return links.build_upload(
        {"to": "drive", **destino}, "https://servidor.exemplo", SEGREDO, minutes=30
    )


def token_de(criado):
    return criado["url"].rsplit("/", 1)[1]


def test_link_de_upload_carrega_o_destino_assinado():
    criado = criar_upload(folder="FAS")
    assert criado["url"].startswith("https://servidor.exemplo/up/")
    assert criado["to"] == "drive"
    assert criado["destino"] == "Google Drive (ARQUIVOS/FAS)"

    destino = links.resolve_upload(token_de(criado), SEGREDO)
    assert destino["to"] == "drive"
    assert destino["folder"] == "FAS"


def test_destino_nao_pode_ser_trocado_por_quem_recebe():
    """O número e a pasta vão dentro da assinatura, não na URL."""
    criado = links.build_upload(
        {"to": "whatsapp", "number": "5511999999999"},
        "https://servidor.exemplo", SEGREDO,
    )
    token = token_de(criado)
    payload, _, assinatura = token.partition(".")

    outro = links._b64(b'{"exp":9999999999,"k":"up","number":"5500000000000","to":"whatsapp"}')
    with pytest.raises(LinkError, match="inválido"):
        links.resolve_upload(f"{outro}.{assinatura}", SEGREDO)


def test_link_de_baixar_nao_serve_para_subir(pasta):
    """Autorizações diferentes, mesmo segredo: a finalidade entra na assinatura."""
    criado = links.build(pasta / "boleto.pdf", pasta, "https://servidor.exemplo", SEGREDO)
    token = criado["url"].split("/dl/")[1].split("/")[0]

    with pytest.raises(LinkError, match="não serve"):
        links.resolve_upload(token, SEGREDO)


def test_link_de_subir_nao_serve_para_baixar(pasta):
    token = token_de(criar_upload())
    with pytest.raises(LinkError, match="não serve"):
        links.resolve(token, pasta, SEGREDO)


def test_upload_vencido():
    agora = time.time()
    criado = links.build_upload(
        {"to": "server"}, "https://servidor.exemplo", SEGREDO, minutes=1, now=agora
    )
    with pytest.raises(LinkExpired):
        links.resolve_upload(token_de(criado), SEGREDO, now=agora + 61)


def test_whatsapp_sem_numero_nao_vira_link():
    with pytest.raises(LinkError, match="número"):
        links.build_upload({"to": "whatsapp"}, "https://servidor.exemplo", SEGREDO)


def test_destino_desconhecido():
    with pytest.raises(LinkError, match="desconhecido"):
        links.build_upload({"to": "telegram"}, "https://servidor.exemplo", SEGREDO)


@pytest.mark.parametrize("destino,esperado", [
    ({"to": "server"}, "fica guardado no servidor"),
    ({"to": "drive"}, "Google Drive (ARQUIVOS)"),
    ({"to": "drive", "financeiro": True, "folder": "MR/2026"}, "Google Drive (FINANCEIRO/MR/2026)"),
    ({"to": "whatsapp", "number": "5511999999999"}, "WhatsApp (5511999999999)"),
    ({"to": "both", "number": "5511999999999"}, "Google Drive (ARQUIVOS) e WhatsApp (5511999999999)"),
])
def test_descricao_do_destino(destino, esperado):
    assert links.descrever_destino(links.checar_destino(destino)) == esperado


@pytest.mark.parametrize("entrada,esperado", [
    ("../../etc/passwd", "passwd"),
    (r"C:\Users\max\nota.pdf", "nota.pdf"),
    ("pasta/sub/arquivo.txt", "arquivo.txt"),
    ("com:dois|pontos?.pdf", "comdoispontos.pdf"),
    ("   ", "arquivo.bin"),
    (None, "arquivo.bin"),
    ("...", "arquivo.bin"),
])
def test_nome_vindo_do_navegador_vira_so_nome(entrada, esperado):
    assert links.nome_seguro(entrada) == esperado


def test_upload_gravado_na_pasta_de_upload(tmp_path):
    arquivo = links.guardar_upload(tmp_path, "nota.pdf", [b"%PDF-", b"1.4"], 1024)
    assert arquivo["file"] == "nota.pdf"
    assert arquivo["size"] == 8
    assert arquivo["mime"] == "application/pdf"
    caminho = tmp_path / links.PASTA_UPLOAD / "nota.pdf"
    assert caminho.read_bytes() == b"%PDF-1.4"


def test_dois_uploads_com_o_mesmo_nome_nao_se_atropelam(tmp_path):
    links.guardar_upload(tmp_path, "nota.pdf", [b"primeiro"], 1024)
    segundo = links.guardar_upload(tmp_path, "nota.pdf", [b"segundo"], 1024)

    assert segundo["file"] == "nota-1.pdf"
    assert (tmp_path / links.PASTA_UPLOAD / "nota.pdf").read_bytes() == b"primeiro"


def test_upload_acima_do_teto_nao_deixa_rastro(tmp_path):
    with pytest.raises(LinkError, match="acima do limite"):
        links.guardar_upload(tmp_path, "grande.bin", [b"x" * 600, b"x" * 600], 1000)
    assert list((tmp_path / links.PASTA_UPLOAD).iterdir()) == []


def test_upload_vazio(tmp_path):
    with pytest.raises(LinkError, match="vazio"):
        links.guardar_upload(tmp_path, "nada.txt", [b""], 1024)


def test_entrega_nos_dois_destinos():
    chamadas = {}
    arquivo = {"file": "n.pdf", "size": 10, "path": "/m/upload/n.pdf"}

    def drive(caminho, pasta, financeiro):
        chamadas["drive"] = (caminho, pasta, financeiro)
        return {"link": "https://drive/x"}

    def zap(caminho, numero, legenda):
        chamadas["zap"] = (caminho, numero, legenda)
        return {"ok": True}

    destino = links.checar_destino(
        {"to": "both", "number": "5511999999999", "folder": "FAS", "caption": "segue"}
    )
    r = links.entregar(arquivo, destino, para_drive=drive, para_whatsapp=zap)

    assert chamadas["drive"] == ("/m/upload/n.pdf", "FAS", False)
    assert chamadas["zap"] == ("/m/upload/n.pdf", "5511999999999", "segue")
    assert r["drive"]["link"] == "https://drive/x"
    assert r["whatsapp"] == {"ok": True}
    assert "erros" not in r


def test_uma_entrega_falhar_nao_derruba_a_outra():
    """O arquivo já está no servidor; perder o resto por causa de um destino seria pior."""
    def drive(*_):
        raise RuntimeError("Drive não configurado")

    destino = links.checar_destino({"to": "both", "number": "5511999999999"})
    r = links.entregar(
        {"file": "n.pdf", "size": 1, "path": "/m/n.pdf"}, destino,
        para_drive=drive, para_whatsapp=lambda *a: {"ok": True},
    )

    assert r["erros"]["drive"] == "Drive não configurado"
    assert r["whatsapp"] == {"ok": True}
    assert r["path"] == "/m/n.pdf"


def test_destino_server_nao_chama_ninguem():
    def nao(*_):
        raise AssertionError("não devia ser chamado")

    r = links.entregar(
        {"file": "n.pdf", "size": 1, "path": "/m/n.pdf"},
        links.checar_destino({"to": "server"}),
        para_drive=nao, para_whatsapp=nao,
    )
    assert r == {"file": "n.pdf", "size": 1, "path": "/m/n.pdf"}


def test_pagina_de_upload_diz_para_onde_vai():
    html = links.pagina_upload(
        links.checar_destino({"to": "whatsapp", "number": "5511999999999"}),
        "2026-10-23 15:40", 32,
    ).decode("utf-8")

    assert "WhatsApp (5511999999999)" in html
    assert "2026-10-23 15:40" in html
    assert "32 MB" in html
    assert "<input type=\"file\"" in html
