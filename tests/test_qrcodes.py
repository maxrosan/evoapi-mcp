"""QR Code e código do Pix: o dígito verificador é calculado, não adivinhado."""

import json

import pytest

pytest.importorskip("segno")

from evoapi_mcp.qrcodes import (  # noqa: E402
    QrError,
    campo,
    cpf_valido,
    crc16,
    normalizar_chave,
    pix_payload,
    render_qr,
)


def _campos(payload: str) -> dict:
    """Desmonta o BR Code em {id: valor}, do jeito que o app do banco lê."""
    saida, i = {}, 0
    while i + 4 <= len(payload):
        ident, tamanho = payload[i:i + 2], int(payload[i + 2:i + 4])
        saida[ident] = payload[i + 4:i + 4 + tamanho]
        i += 4 + tamanho
    return saida


def test_crc16_confere_com_o_vetor_conhecido():
    """CRC16/CCITT-FALSE de '123456789' é 0x29B1 — é o algoritmo que o BR Code exige."""
    assert crc16("123456789") == "29B1"
    assert crc16("") == "FFFF"


def test_campo_emv_leva_o_tamanho_em_dois_digitos():
    assert campo("00", "01") == "000201"
    assert campo("59", "MAX ROSAN") == "5909MAX ROSAN"


@pytest.mark.parametrize("entrada, esperado, tipo", [
    ("84996246771", "+5584996246771", "telefone"),
    ("(84) 99624-6771", "+5584996246771", "telefone"),
    ("+55 84 99624-6771", "+5584996246771", "telefone"),
    ("5584996246771", "+5584996246771", "telefone"),
    ("8432211234", "+558432211234", "telefone"),
    ("max@exemplo.com.br", "max@exemplo.com.br", "email"),
    ("MAX@Exemplo.com", "max@exemplo.com", "email"),
    ("123.456.789-09", "12345678909", "cpf"),
    ("12345678909", "12345678909", "cpf"),                 # CPF sem pontuação: o dígito fecha
    ("529.982.247-25", "52998224725", "cpf"),
    ("12.345.678/0001-95", "12345678000195", "cnpj"),
    ("6f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8", "6f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8", "aleatoria"),
])
def test_normalizar_chave(entrada, esperado, tipo):
    assert normalizar_chave(entrada) == (esperado, tipo)


def test_onze_digitos_decidem_pelo_digito_verificador_do_cpf():
    """84996246771 é celular; 123.456.789-09 é CPF. O verificador do CPF desempata."""
    assert not cpf_valido("84996246771") and cpf_valido("12345678909")
    assert not cpf_valido("11111111111") and not cpf_valido("1234567890")
    assert normalizar_chave("84996246771")[1] == "telefone"
    assert normalizar_chave("12345678909")[1] == "cpf"
    # escrito como telefone, é telefone mesmo quando os dígitos formariam um CPF válido
    assert normalizar_chave("(12) 34567-8909") == ("+5512345678909", "telefone")


@pytest.mark.parametrize("ruim", ["", "   ", "123", "arroba@", "0000000"])
def test_chave_que_nao_da_para_reconhecer_vira_erro(ruim):
    with pytest.raises(QrError):
        normalizar_chave(ruim)


def test_payload_do_pix_tem_os_campos_e_o_crc_certo():
    pix = pix_payload("84996246771", valor="37,00", nome="Ana Keilla", cidade="Currais Novos")
    payload = pix["payload"]

    campos = _campos(payload)
    assert campos["00"] == "01"                                  # versão
    assert campos["53"] == "986" and campos["58"] == "BR"        # real, Brasil
    assert campos["54"] == "37.00"
    assert campos["59"] == "ANA KEILLA" and campos["60"] == "CURRAIS NOVOS"
    conta = _campos(campos["26"])
    assert conta["00"] == "BR.GOV.BCB.PIX" and conta["01"] == "+5584996246771"

    # o CRC fecha: é o que o app do banco confere antes de aceitar o código
    assert payload[-8:-4] == "6304"
    assert payload[-4:] == crc16(payload[:-4])
    assert pix["valor"] == "37.00" and pix["tipo_chave"] == "telefone"


def test_pix_sem_valor_deixa_quem_paga_escolher():
    pix = pix_payload("max@exemplo.com", nome="Max")
    assert "54" not in _campos(pix["payload"]) and "valor" not in pix
    assert pix["payload"][-4:] == crc16(pix["payload"][:-4])


def test_nome_e_cidade_saem_sem_acento_e_no_tamanho_do_padrao():
    pix = pix_payload("12345678909", valor=10, nome="José Antônio da Silva Sauro Júnior",
                      cidade="São Gonçalo do Amarante")
    campos = _campos(pix["payload"])
    assert campos["59"] == "JOSE ANTONIO DA SILVA SAU" and len(campos["59"]) == 25
    assert campos["60"] == "SAO GONCALO DO " and len(campos["60"]) == 15
    assert pix["payload"][-4:] == crc16(pix["payload"][:-4])


def test_sem_nome_e_cidade_usa_padrao_valido():
    campos = _campos(pix_payload("84996246771")["payload"])
    assert campos["59"] == "RECEBEDOR" and campos["60"] == "BRASIL" and campos["62"] == "0503***"


@pytest.mark.parametrize("valor", ["abc", "0", "-5", 0, -1, "2000000"])
def test_valor_invalido_nao_vira_pix(valor):
    with pytest.raises(QrError):
        pix_payload("84996246771", valor=valor)


@pytest.mark.parametrize("entrada, esperado", [("37", "37.00"), (37, "37.00"), (37.5, "37.50"),
                                               ("R$ 1.234,56", "1234.56"), ("0,01", "0.01")])
def test_valor_aceita_os_jeitos_que_max_escreve(entrada, esperado):
    assert pix_payload("84996246771", valor=entrada)["valor"] == esperado


def test_render_grava_png_legivel(tmp_path):
    imagem = render_qr("https://exemplo.test/abc", tmp_path)
    caminho = tmp_path / "qrcode" / "qrcode.png"
    assert imagem["path"] == str(caminho) and caminho.is_file()
    assert imagem["size"] > 200 and imagem["correcao"] == "M"
    assert caminho.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_nao_sobrescreve_e_normaliza_o_nome(tmp_path):
    a = render_qr("um", tmp_path, file_name="pix da Ana")
    b = render_qr("dois", tmp_path, file_name="pix da Ana")
    assert a["file"] == "pix da Ana.png" and b["file"] == "pix da Ana (2).png"


def test_render_recusa_vazio_e_texto_gigante(tmp_path):
    with pytest.raises(QrError):
        render_qr("  ", tmp_path)
    with pytest.raises(QrError, match="longo demais"):
        render_qr("x" * 2000, tmp_path)


def test_o_qr_gerado_le_o_mesmo_payload(tmp_path):
    """Sem leitor instalado o teste é pulado; com ele, o QR tem que devolver o código do Pix."""
    zbar = pytest.importorskip("pyzbar.pyzbar", reason="leitor de QR não instalado")
    Image = pytest.importorskip("PIL.Image")

    pix = pix_payload("84996246771", valor="37,00", nome="Ana Keilla", cidade="Currais Novos")
    imagem = render_qr(pix["payload"], tmp_path)
    lido = zbar.decode(Image.open(imagem["path"]))
    assert lido and lido[0].data.decode() == pix["payload"]


# ------------------------------------------------------------------ tool do servidor

@pytest.fixture
def server(monkeypatch, tmp_path):
    import importlib

    monkeypatch.setenv("EVOLUTION_BASE_URL", "http://evolution.test")
    monkeypatch.setenv("EVOLUTION_API_TOKEN", "token")
    monkeypatch.setenv("EVOLUTION_INSTANCE_NAME", "inst")
    import evoapi_mcp.server as modulo

    modulo = importlib.reload(modulo)

    class FakeClient:
        media_dir = tmp_path
        enviados = []

        def send_file(self, **kwargs):
            self.enviados.append(kwargs)
            return {"key": {"id": "QR1", "remoteJid": kwargs["number"]}, "status": "PENDING"}

    fake = FakeClient()
    monkeypatch.setattr(modulo, "client", fake)
    return modulo, fake


def test_tool_existe_e_esta_na_regra_do_servidor(server):
    import asyncio

    modulo, _ = server
    assert "send_qrcode" in {t.name for t in asyncio.run(modulo.mcp.list_tools())}
    assert "send_qrcode" in modulo.mcp.instructions


def test_send_qrcode_do_pix_manda_imagem_e_copia_e_cola(server):
    modulo, fake = server
    saida = json.loads(modulo.send_qrcode(
        number="558494420469@s.whatsapp.net", pix_key="84996246771", amount="37,00",
        receiver="Ana Keilla", city="Currais Novos", file_name="pix Ana.png",
    ))
    assert saida["payload"][-4:] == crc16(saida["payload"][:-4])
    assert saida["enviado"]
    envio = fake.enviados[-1]
    assert envio["media_type"] == "image" and envio["file_name"] == "pix Ana.png"
    assert saida["payload"] in envio["caption"]            # o copia e cola vai junto
    assert "R$ 37,00" in envio["caption"] and "+5584996246771" in envio["caption"]


def test_send_qrcode_sem_numero_so_gera(server):
    modulo, fake = server
    saida = json.loads(modulo.send_qrcode(text="https://exemplo.test"))
    assert saida["file"].endswith(".png") and "enviado" not in saida
    assert not fake.enviados


def test_send_qrcode_cobra_uma_das_duas_origens(server):
    modulo, _ = server
    assert "text" in json.loads(modulo.send_qrcode(number="55849", ))["error"]
    assert "error" in json.loads(modulo.send_qrcode(text="oi", pix_key="84996246771"))


def test_chave_ruim_nao_vira_mensagem_enviada(server):
    modulo, fake = server
    saida = json.loads(modulo.send_qrcode(number="55849", pix_key="123"))
    assert "chave Pix" in saida["error"] and not fake.enviados
