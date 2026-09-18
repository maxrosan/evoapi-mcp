"""QR Code no servidor, inclusive o do Pix ("copia e cola" + imagem).

Por que existe: pedir "gera o QR de R$ 37 para essa chave" não tinha caminho. Desenhar
um QR à mão em SVG sai ilegível, e site de terceiros some ou passa a cobrar — foi o que
aconteceu em 18/09/2026, quando o executor tentou um e levou HTTP 402. Pior seria montar
o código do Pix no chute: ele termina num dígito verificador (CRC16) e, com um caractere
errado, o app do banco recusa — ou aceita apontando para outro recebedor.

Aqui o código do Pix é montado pelo padrão do Banco Central (EMV/BR Code, campos
`ID+tamanho+valor`) e o CRC é calculado, não adivinhado. O desenho do QR fica com o
`segno` (Python puro, sem dependência de sistema). Nada vai à internet: um Pix estático
é só texto, e o mesmo texto que vira imagem é o "copia e cola".

O servidor NÃO paga nada e não fala com banco nenhum: ele só escreve o pedido de
pagamento. Quem confere a chave é quem manda.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

QR_DIRNAME = "qrcode"
MAX_DATA = 1200          # acima disso o QR fica denso demais para a câmera do celular
GUI_PIX = "BR.GOV.BCB.PIX"


class QrError(Exception):
    """Pedido de QR Code inválido."""


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] QRCode: {message}", file=sys.stderr)


# ------------------------------------------------------------------ Pix

def crc16(dados: str) -> str:
    """CRC16/CCITT-FALSE em hexadecimal, como o BR Code exige (polinômio 0x1021, inicial 0xFFFF)."""
    crc = 0xFFFF
    for byte in dados.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def campo(identificador: str, valor: str) -> str:
    """Campo EMV: id + tamanho com dois dígitos + valor."""
    return f"{identificador}{len(valor):02d}{valor}"


def _texto(valor: str | None, limite: int, padrao: str = "") -> str:
    """Sem acento e em maiúsculas, como os aplicativos de banco esperam."""
    texto = unicodedata.normalize("NFKD", str(valor or padrao))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"[^A-Za-z0-9 .\-]", "", texto).strip().upper()
    return texto[:limite] or padrao


def cpf_valido(digitos: str) -> bool:
    """Dígitos verificadores do CPF. É o que desempata 11 dígitos entre CPF e celular."""
    if len(digitos) != 11 or len(set(digitos)) == 1:
        return False
    for tamanho in (9, 10):
        soma = sum(int(digitos[i]) * (tamanho + 1 - i) for i in range(tamanho))
        resto = (soma * 10) % 11 % 10
        if resto != int(digitos[tamanho]):
            return False
    return True


def normalizar_chave(chave: str) -> tuple[str, str]:
    """(chave no formato do Pix, tipo). Telefone vira +55DDDNÚMERO, CPF/CNPJ só dígitos."""
    bruta = str(chave or "").strip()
    if not bruta:
        raise QrError("informe a chave Pix")
    if "@" in bruta:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", bruta):
            raise QrError(f"e-mail inválido como chave Pix: {bruta}")
        return bruta.lower(), "email"
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", bruta):
        return bruta.lower(), "aleatoria"
    digitos = re.sub(r"\D", "", bruta)
    # Ponto e hífen aparecem nos dois (123.456.789-09 e 99624-6771); só "+" e parênteses
    # são exclusivos de telefone.
    escrito_como_telefone = bruta.startswith("+") or "(" in bruta
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        return f"+{digitos}", "telefone"
    if len(digitos) == 14:
        return digitos, "cnpj"
    if escrito_como_telefone and 10 <= len(digitos) <= 11:
        return f"+55{digitos}", "telefone"           # quem escreveu com + ou (DDD) quis telefone
    if len(digitos) == 11:
        # Ambíguo: 11 dígitos são CPF e também celular com DDD. Quem decide é o
        # dígito verificador do CPF; sem ele fechar, vale o formato de celular.
        # Para não depender disso, escreva o celular como +55DDDNÚMERO.
        if cpf_valido(digitos):
            return digitos, "cpf"
        if re.fullmatch(r"[1-9][0-9]9[0-9]{8}", digitos):
            return f"+55{digitos}", "telefone"
    if len(digitos) == 10 and re.fullmatch(r"[1-9][0-9][2-5][0-9]{7}", digitos):
        return f"+55{digitos}", "telefone"                # fixo com DDD
    raise QrError(
        f"não reconheci '{bruta}' como chave Pix (CPF, CNPJ, telefone, e-mail ou chave aleatória)"
    )


def _valor(valor: Any) -> str | None:
    if valor is None or valor == "":
        return None
    if isinstance(valor, str):
        limpo = valor.replace("R$", "").strip().replace(".", "").replace(",", ".")
    else:
        limpo = str(valor)
    try:
        numero = round(float(limpo), 2)
    except ValueError:
        raise QrError(f"valor inválido: {valor}")
    if numero <= 0:
        raise QrError("o valor precisa ser maior que zero")
    if numero > 1_000_000:
        raise QrError("valor acima de R$ 1.000.000: confirme antes com quem vai receber")
    return f"{numero:.2f}"


def pix_payload(
    chave: str,
    valor: Any = None,
    nome: str | None = None,
    cidade: str | None = None,
    txid: str | None = None,
    mensagem: str | None = None,
) -> dict[str, Any]:
    """Monta o "copia e cola" do Pix estático (BR Code).

    Returns: {payload, chave, tipo_chave, valor?, nome, cidade, txid}
    """
    chave_pix, tipo = normalizar_chave(chave)
    quantia = _valor(valor)
    recebedor = _texto(nome, 25, "RECEBEDOR")
    municipio = _texto(cidade, 15, "BRASIL")
    identificador = re.sub(r"[^A-Za-z0-9]", "", str(txid or "")).upper()[:25] or "***"

    conta = campo("00", GUI_PIX) + campo("01", chave_pix)
    if mensagem:
        conta += campo("02", _texto(mensagem, 40))
    partes = [
        campo("00", "01"),                       # versão do formato
        campo("26", conta),                      # conta do recebedor (Pix)
        campo("52", "0000"),                     # categoria do estabelecimento: não informada
        campo("53", "986"),                      # moeda: real
    ]
    if quantia:
        partes.append(campo("54", quantia))
    partes += [
        campo("58", "BR"),
        campo("59", recebedor),
        campo("60", municipio),
        campo("62", campo("05", identificador)),
    ]
    corpo = "".join(partes) + "6304"             # o CRC entra depois do próprio marcador
    payload = corpo + crc16(corpo)
    saida: dict[str, Any] = {
        "payload": payload, "chave": chave_pix, "tipo_chave": tipo,
        "nome": recebedor, "cidade": municipio, "txid": identificador,
    }
    if quantia:
        saida["valor"] = quantia
    return saida


# ------------------------------------------------------------------ imagem

def _require_segno():
    try:
        import segno
    except ImportError:
        raise QrError("segno não instalado (pip install segno)")
    return segno


def render_qr(
    data: str,
    out_dir: str | Path,
    file_name: str = "qrcode.png",
    scale: int = 10,          # ~400 px num Pix: lê bem na tela de outro celular
    border: int = 4,
    dark: str = "#000000",
    light: str = "#FFFFFF",
) -> dict[str, Any]:
    """Desenha o QR e grava um PNG. Returns: {path, file, size, versao, correcao}."""
    segno = _require_segno()
    texto = str(data or "").strip()
    if not texto:
        raise QrError("nada para codificar no QR Code")
    if len(texto) > MAX_DATA:
        raise QrError(f"texto longo demais para um QR legível ({len(texto)} caracteres, máximo {MAX_DATA})")
    try:
        qr = segno.make(texto, error="m")     # 'm': lê bem mesmo com a tela suja ou torta
    except Exception as e:
        raise QrError(f"não consegui montar o QR Code: {e}")

    nome = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (file_name or "qrcode.png").strip()) or "qrcode.png"
    if not nome.lower().endswith(".png"):
        nome += ".png"
    pasta = Path(out_dir) / QR_DIRNAME
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / nome
    n = 2
    while caminho.exists():
        caminho = pasta / f"{Path(nome).stem} ({n}).png"
        n += 1
    qr.save(str(caminho), scale=max(2, min(int(scale or 10), 20)), border=max(0, min(int(border or 4), 8)),
            dark=dark, light=light)
    tamanho = caminho.stat().st_size
    _log(f"QR de {len(texto)} caracteres em {caminho} ({tamanho} bytes)")
    return {"path": str(caminho), "file": caminho.name, "size": tamanho,
            "versao": qr.version, "correcao": qr.error}
