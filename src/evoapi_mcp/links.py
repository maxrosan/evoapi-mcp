"""Link temporário para baixar do servidor um arquivo que está no disco dele.

O caminho oposto ao do resto do servidor: aqui o arquivo não vai para o WhatsApp
nem para o Drive — vai para o navegador de quem pediu. É o que faltava para
"me manda aquele PDF que chegou": `download_media` já traz o anexo para o disco,
mas o `path` que ele devolve só existe dentro do container.

O link não é um caminho público: é o caminho do arquivo **assinado** com um
segredo do servidor, junto com a hora em que deixa de valer. Quem recebe não
consegue trocar o caminho por outro (a assinatura não fecha) nem esticar o prazo
(ele faz parte do que foi assinado), e o servidor não precisa guardar lista
nenhuma de links emitidos — a própria URL carrega o que ele precisa conferir.

Prazo curto de propósito: o link anda por WhatsApp, e-mail e histórico de
navegador, lugares de onde não volta. Uma hora é o padrão.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import mimetypes
import sys
import time
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

# Prefixo da rota servida pelo `mcp_http`. Fica fora da autenticação Bearer: o
# que protege o arquivo é a assinatura, não o header, porque o link precisa
# funcionar num navegador qualquer.
PREFIXO = "/dl"

TTL_PADRAO_MIN = 60
TTL_MAX_MIN = 24 * 60

# Tipos que o navegador mostra na própria aba. Nos outros, o link baixa o
# arquivo em vez de tentar exibi-lo.
_PREFIXOS_EXIBIVEIS = ("image/", "video/", "audio/")
_TIPOS_EXIBIVEIS = ("text/plain", "application/pdf")


class LinkError(Exception):
    """Erro ao criar ou validar um link temporário."""


class LinkExpired(LinkError):
    """O link existia e era válido, mas passou da hora.

    Separado do resto para quem serve a rota poder responder 410 em vez de 404:
    "esse link venceu" e "esse link nunca existiu" são coisas diferentes para
    quem está do outro lado.
    """


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Link temporário: {message}", file=sys.stderr)


def _b64(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).decode().rstrip("=")


def _desb64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _assinar(payload: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), sha256).digest())


def _dentro(media_dir: Path, caminho: Path) -> Path:
    """Devolve o caminho relativo à pasta de mídias, ou recusa.

    O arquivo só pode sair daqui se estiver na pasta que o servidor administra.
    Sem isto, um caminho com `..` (ou um link simbólico) viraria uma porta para
    `/etc/passwd` e para o `.env` do próprio container.
    """
    raiz = media_dir.resolve()
    try:
        alvo = caminho.resolve()
        return alvo.relative_to(raiz)
    except (OSError, ValueError):
        raise LinkError(
            f"O arquivo precisa estar dentro de {raiz} para ganhar link; "
            f"recebido: {caminho}"
        )


def build(
    file_path: str | Path,
    media_dir: str | Path,
    public_url: str,
    secret: str,
    minutes: int = TTL_PADRAO_MIN,
    now: float | None = None,
) -> dict:
    """Cria o link temporário de um arquivo que já está no disco do servidor.

    Args:
        file_path: caminho do arquivo (o `path` que download_media devolveu)
        media_dir: pasta de mídias; o arquivo precisa estar dentro dela
        public_url: endereço externo deste servidor (EVOLUTION_PUBLIC_URL)
        secret: segredo da assinatura
        minutes: validade em minutos (1 a 1440)
        now: hora de referência, para teste

    Returns:
        dict: {url, expires_at, expires_in_min, file, size, mime}

    Raises:
        LinkError: sem endereço público ou segredo, arquivo fora da pasta,
                   arquivo inexistente ou prazo fora da faixa
    """
    if not (public_url or "").strip():
        raise LinkError(
            "Sem endereço público: defina EVOLUTION_PUBLIC_URL com o endereço externo "
            "deste servidor (ex: https://meuservidor.exemplo) para emitir links."
        )
    if not (secret or "").strip():
        raise LinkError(
            "Sem segredo para assinar: defina EVOLUTION_LINK_SECRET (ou MCP_AUTH_TOKEN)."
        )
    if not 1 <= minutes <= TTL_MAX_MIN:
        raise LinkError(f"Prazo de {minutes} min fora da faixa (1 a {TTL_MAX_MIN}).")

    caminho = Path(file_path)
    rel = _dentro(Path(media_dir), caminho)
    if not caminho.is_file():
        raise LinkError(f"Arquivo não encontrado: {caminho}")

    expira = int((now if now is not None else time.time()) + minutes * 60)
    payload = _b64(f"{expira}:{rel.as_posix()}".encode())
    token = f"{payload}.{_assinar(payload, secret)}"

    nome = caminho.name
    mime = mimetypes.guess_type(nome)[0] or "application/octet-stream"
    tamanho = caminho.stat().st_size
    url = f"{public_url.rstrip('/')}{PREFIXO}/{token}/{quote(nome)}"

    _log(f"{nome} ({tamanho} bytes) com link válido por {minutes} min")
    return {
        "url": url,
        "expires_at": expira,
        "expires_in_min": minutes,
        "file": nome,
        "size": tamanho,
        "mime": mime,
    }


def resolve(
    token: str,
    media_dir: str | Path,
    secret: str,
    now: float | None = None,
) -> dict:
    """Confere o token do link e devolve o arquivo a servir.

    Args:
        token: a parte assinada da URL (o que vem depois de /dl/)
        media_dir: pasta de mídias
        secret: segredo da assinatura
        now: hora de referência, para teste

    Returns:
        dict: {path, file, mime, size, inline, expires_at}

    Raises:
        LinkError: token torto, assinatura inválida, prazo vencido, arquivo fora
                   da pasta ou já apagado
    """
    if not (secret or "").strip():
        raise LinkError("Servidor sem segredo de assinatura; links desligados.")

    payload, _, assinatura = (token or "").partition(".")
    if not payload or not assinatura:
        raise LinkError("Link malformado.")
    if not hmac.compare_digest(assinatura, _assinar(payload, secret)):
        raise LinkError("Link inválido.")

    try:
        expira_txt, _, rel = _desb64(payload).decode().partition(":")
        expira = int(expira_txt)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise LinkError("Link malformado.")
    if not rel:
        raise LinkError("Link malformado.")

    agora = now if now is not None else time.time()
    if agora >= expira:
        raise LinkExpired("Link vencido; peça outro.")

    # O caminho assinado ainda passa pela checagem de pasta: assinar "../.env"
    # seria possível para quem tivesse o segredo, e o segredo também protege o
    # resto do servidor. Uma defesa só nunca é uma defesa.
    caminho = Path(media_dir) / rel
    _dentro(Path(media_dir), caminho)
    if not caminho.is_file():
        raise LinkError("O arquivo não está mais no servidor.")

    mime = mimetypes.guess_type(caminho.name)[0] or "application/octet-stream"
    return {
        "path": str(caminho),
        "file": caminho.name,
        "mime": mime,
        "size": caminho.stat().st_size,
        "inline": mime.startswith(_PREFIXOS_EXIBIVEIS) or mime in _TIPOS_EXIBIVEIS,
        "expires_at": expira,
    }


def secret_from(config, env: dict) -> str:
    """O segredo da assinatura: o próprio, ou o token do MCP como reserva.

    Quem sobe este servidor já define MCP_AUTH_TOKEN; exigir uma segunda
    variável só para os links faria a funcionalidade nascer desligada no
    ambiente onde ela é útil.
    """
    return (getattr(config, "link_secret", "") or env.get("MCP_AUTH_TOKEN", "") or "").strip()
