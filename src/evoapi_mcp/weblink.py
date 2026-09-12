"""Busca no servidor um arquivo que já existe na web, para enviá-lo ao WhatsApp.

É o atalho para a imagem (ou o PDF) que já está em algum lugar: em vez de trazer
o conteúdo para a conversa em base64, o modelo passa o **link**, que custa algumas
dezenas de tokens, e quem baixa é o servidor.

`send_image` já aceita URL e faz a própria Evolution baixar, que é ainda mais
barato — mas só funciona quando a Evolution enxerga a URL e falha de forma opaca
quando não enxerga. Aqui o download acontece no servidor, então dá para dizer o que
houve (link privado, arquivo grande demais, 404) e para tratar o caso do link de
compartilhamento, que não é o link do arquivo.

Sem credenciais: só chega a arquivo publicamente acessível. Arquivo do Drive que o
próprio servidor arquivou continua saindo por `archive_to_drive`/`send_file`.
"""

from __future__ import annotations

import ipaddress
import mimetypes
import re
import socket
import sys
from urllib.parse import parse_qs, unquote, urlparse

import requests

# Teto do download. O WhatsApp recusa mídia muito maior que isto, e sem um teto um
# link errado encheria o disco do servidor.
MAX_FETCH_BYTES = 16 * 1024 * 1024

MAX_REDIRECTS = 5

# Tipos que o Google devolve quando o link NÃO dá acesso ao arquivo: em vez de 403
# vem a página de login ou o aviso de vírus, com status 200.
_HTML_TYPES = ("text/html", "application/xhtml+xml")

_DRIVE_ID = re.compile(r"/file/d/([A-Za-z0-9_-]+)")


class WebLinkError(Exception):
    """Erro ao buscar o arquivo pela URL."""


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Link: {message}", file=sys.stderr)


def _dica_drive(url: str) -> str:
    """A dica sobre compartilhamento só serve para link do Drive; em outra URL é ruído."""
    if (urlparse(url).hostname or "").lower().endswith("google.com"):
        return " No Drive, o arquivo precisa estar compartilhado como 'qualquer pessoa com o link'."
    return ""


def normalize(url: str) -> str:
    """Troca o link de compartilhamento pelo link do arquivo em si.

    O link que o Drive e o Dropbox dão para copiar abre uma *página* com o arquivo
    dentro; baixá-lo traz HTML, não a imagem. As duas conversões abaixo cobrem o
    que aparece no dia a dia; qualquer outra URL passa intacta.
    """
    url = (url or "").strip()
    partes = urlparse(url)
    host = (partes.hostname or "").lower()

    if host in ("drive.google.com", "docs.google.com"):
        achado = _DRIVE_ID.search(partes.path)
        file_id = achado.group(1) if achado else (parse_qs(partes.query).get("id") or [""])[0]
        if file_id:
            return f"https://drive.google.com/uc?export=download&id={file_id}"

    if host.endswith("dropbox.com"):
        limpa = re.sub(r"[?&]dl=\d", "", url)
        return limpa + ("&" if "?" in limpa else "?") + "dl=1"

    return url


def _ensure_public(url: str) -> None:
    """Recusa o que não é http(s) público.

    O endereço vem de fora (uma mensagem do WhatsApp pode chegar até aqui pelo bot),
    e um servidor que busca qualquer URL que lhe mandam vira uma porta para a rede
    interna e para o metadata da nuvem. Cada salto do redirecionamento passa por
    esta checagem, senão bastaria um 302 para contornar a primeira.
    """
    partes = urlparse(url)
    if partes.scheme not in ("http", "https"):
        raise WebLinkError(f"Só http e https são aceitos (recebido: {partes.scheme or 'nada'}).")
    if not partes.hostname:
        raise WebLinkError(f"URL sem host: {url}")

    try:
        enderecos = socket.getaddrinfo(partes.hostname, partes.port or (443 if partes.scheme == "https" else 80))
    except socket.gaierror as e:
        raise WebLinkError(f"Não foi possível resolver {partes.hostname}: {e}")

    for familia, _, _, _, sockaddr in enderecos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise WebLinkError(f"Endereço interno recusado: {partes.hostname} ({ip}).")


def _file_name_from(response: requests.Response, url: str, mime: str) -> str:
    disposicao = response.headers.get("Content-Disposition", "")
    achado = re.search(r"filename\*=UTF-8''([^;]+)", disposicao) or re.search(
        r'filename="?([^";]+)"?', disposicao
    )
    if achado:
        nome = unquote(achado.group(1)).strip()
        if nome:
            return nome

    caminho = unquote(urlparse(url).path)
    nome = caminho.rsplit("/", 1)[-1].strip()
    if nome and "." in nome:
        return nome

    return f"arquivo{mimetypes.guess_extension(mime) or '.bin'}"


def fetch(url: str, timeout: int = 30, max_bytes: int = MAX_FETCH_BYTES) -> dict:
    """Baixa o arquivo da URL, no servidor.

    Args:
        url: endereço público do arquivo (link de compartilhamento é convertido)
        timeout: timeout de cada requisição
        max_bytes: teto do download

    Returns:
        dict: {content: bytes, mime, file_name, url}

    Raises:
        WebLinkError: endereço recusado, erro HTTP, arquivo grande demais ou
                      página HTML no lugar do arquivo
    """
    atual = normalize(url)
    if atual != url:
        _log(f"link de compartilhamento convertido em download direto: {atual}")

    resposta = None
    for _ in range(MAX_REDIRECTS + 1):
        _ensure_public(atual)
        try:
            resposta = requests.get(atual, timeout=timeout, stream=True, allow_redirects=False)
        except requests.RequestException as e:
            raise WebLinkError(f"Falha ao baixar {atual}: {e}")

        if resposta.is_redirect or resposta.is_permanent_redirect:
            destino = resposta.headers.get("Location", "")
            resposta.close()
            if not destino:
                raise WebLinkError(f"Redirecionamento sem destino em {atual}.")
            atual = requests.compat.urljoin(atual, destino)
            continue
        break
    else:
        raise WebLinkError(f"Redirecionamentos demais a partir de {url}.")

    if resposta.status_code >= 400:
        resposta.close()
        raise WebLinkError(
            f"O servidor respondeu {resposta.status_code} para {atual}.{_dica_drive(atual)}"
        )

    mime = (resposta.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    # Content-Length evita começar um download que já se sabe grande demais; quando
    # vem ausente ou torto, o teto ainda vale contando o que chega.
    try:
        tamanho = int(resposta.headers.get("Content-Length") or 0)
    except ValueError:
        tamanho = 0
    if tamanho > max_bytes:
        resposta.close()
        raise WebLinkError(f"Arquivo de {tamanho} bytes, acima do limite de {max_bytes}.")

    conteudo = bytearray()
    try:
        for pedaco in resposta.iter_content(chunk_size=64 * 1024):
            conteudo.extend(pedaco)
            if len(conteudo) > max_bytes:
                raise WebLinkError(f"O download passou do limite de {max_bytes} bytes.")
    except requests.RequestException as e:
        raise WebLinkError(f"Download interrompido: {e}")
    finally:
        resposta.close()

    if not conteudo:
        raise WebLinkError(f"O download de {atual} veio vazio.")

    if mime in _HTML_TYPES:
        raise WebLinkError(
            f"O endereço devolveu uma página HTML, não um arquivo: {atual}.{_dica_drive(atual)}"
        )

    nome = _file_name_from(resposta, atual, mime)
    _log(f"{len(conteudo)} bytes baixados de {atual} ({mime or 'sem mime'})")
    return {"content": bytes(conteudo), "mime": mime, "file_name": nome, "url": atual}
