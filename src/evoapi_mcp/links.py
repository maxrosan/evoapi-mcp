"""Links assinados: o arquivo sai do servidor pelo navegador, ou entra por ele.

As duas pontas que faltavam, e que são a mesma ideia virada ao contrário.

**Baixar** (`/dl`): `download_media` já traz o anexo do WhatsApp para o disco, mas o
`path` que ele devolve só existe dentro do container. O link transforma esse arquivo
num endereço que abre em qualquer navegador.

**Subir** (`/up`): o caminho de volta. Max tem um arquivo no celular ou no computador
e quer que ele vá para o Drive, para uma conversa do WhatsApp, ou para os dois — sem
passar o arquivo pela conversa com o modelo, que é o que encarece tudo neste servidor.
O link abre uma página com um seletor de arquivo; o destino já viaja **dentro** da
assinatura, então quem recebe o link não consegue redirecionar o arquivo para outro
número nem para outra pasta.

Nos dois casos o link não é um caminho público: é o que ele autoriza, assinado com um
segredo do servidor, junto com a hora em que deixa de valer. Não dá para trocar o
caminho nem esticar o prazo, e o servidor não guarda lista nenhuma de links emitidos —
a própria URL carrega o que ele precisa conferir.

Prazo curto de propósito: o link anda por WhatsApp, e-mail e histórico de navegador,
lugares de onde não volta. Uma hora é o padrão. Dentro desse prazo ele vale mais de uma
vez, o que é proposital para subir três arquivos com um link só.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import mimetypes
import re
import sys
import time
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

# Prefixos das rotas servidas pelo `mcp_http`. Ficam fora da autenticação Bearer: o
# que protege os dois é a assinatura, não o header, porque o link precisa funcionar
# num navegador qualquer.
PREFIXO = "/dl"
PREFIXO_UP = "/up"

TTL_PADRAO_MIN = 60
TTL_MAX_MIN = 24 * 60

# Pasta onde o que chega pelo navegador é gravado, dentro da pasta de mídias.
PASTA_UPLOAD = "upload"

DESTINOS = ("server", "drive", "whatsapp", "both")

# Tipos que o navegador mostra na própria aba. Nos outros, o link baixa o
# arquivo em vez de tentar exibi-lo.
_PREFIXOS_EXIBIVEIS = ("image/", "video/", "audio/")
_TIPOS_EXIBIVEIS = ("text/plain", "application/pdf")


class LinkError(Exception):
    """Erro ao criar ou validar um link assinado."""


class LinkExpired(LinkError):
    """O link existia e era válido, mas passou da hora.

    Separado do resto para quem serve a rota poder responder 410 em vez de 404:
    "esse link venceu" e "esse link nunca existiu" são coisas diferentes para
    quem está do outro lado.
    """


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Link: {message}", file=sys.stderr)


def _b64(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).decode().rstrip("=")


def _desb64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _assinar(payload: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), sha256).digest())


def _token(dados: dict, secret: str) -> str:
    payload = _b64(json.dumps(dados, separators=(",", ":"), sort_keys=True).encode())
    return f"{payload}.{_assinar(payload, secret)}"


def _abrir(token: str, secret: str, kind: str, now: float | None) -> dict:
    """Confere assinatura, finalidade e prazo, e devolve o que foi assinado.

    A finalidade entra na conferência porque um link de baixar e um de subir são
    autorizações diferentes: sem isso, um link de download viraria, com o mesmo
    segredo, permissão de gravar no servidor.
    """
    if not (secret or "").strip():
        raise LinkError("Servidor sem segredo de assinatura; links desligados.")

    payload, _, assinatura = (token or "").partition(".")
    if not payload or not assinatura:
        raise LinkError("Link malformado.")
    if not hmac.compare_digest(assinatura, _assinar(payload, secret)):
        raise LinkError("Link inválido.")

    try:
        dados = json.loads(_desb64(payload))
        expira = int(dados["exp"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, binascii.Error):
        raise LinkError("Link malformado.")
    if not isinstance(dados, dict) or dados.get("k") != kind:
        raise LinkError("Link não serve para isto.")

    if (now if now is not None else time.time()) >= expira:
        raise LinkExpired("Link vencido; peça outro.")
    return dados


def _prazo(minutes: int, now: float | None) -> int:
    if not 1 <= minutes <= TTL_MAX_MIN:
        raise LinkError(f"Prazo de {minutes} min fora da faixa (1 a {TTL_MAX_MIN}).")
    return int((now if now is not None else time.time()) + minutes * 60)


def _exige_configuracao(public_url: str, secret: str) -> None:
    if not (public_url or "").strip():
        raise LinkError(
            "Sem endereço público: defina EVOLUTION_PUBLIC_URL com o endereço externo "
            "deste servidor (ex: https://meuservidor.exemplo) para emitir links."
        )
    if not (secret or "").strip():
        raise LinkError(
            "Sem segredo para assinar: defina EVOLUTION_LINK_SECRET (ou MCP_AUTH_TOKEN)."
        )


def _dentro(media_dir: Path, caminho: Path) -> Path:
    """Devolve o caminho relativo à pasta de mídias, ou recusa.

    O arquivo só pode sair daqui — ou entrar aqui — se estiver na pasta que o
    servidor administra. Sem isto, um caminho com `..` (ou um link simbólico)
    viraria uma porta para `/etc/passwd` e para o `.env` do próprio container.
    """
    raiz = media_dir.resolve()
    try:
        alvo = caminho.resolve()
        return alvo.relative_to(raiz)
    except (OSError, ValueError):
        raise LinkError(
            f"O arquivo precisa estar dentro de {raiz}; recebido: {caminho}"
        )


# ---------------------------------------------------------------- baixar (/dl)


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
    _exige_configuracao(public_url, secret)
    expira = _prazo(minutes, now)

    caminho = Path(file_path)
    rel = _dentro(Path(media_dir), caminho)
    if not caminho.is_file():
        raise LinkError(f"Arquivo não encontrado: {caminho}")

    token = _token({"exp": expira, "k": "dl", "rel": rel.as_posix()}, secret)
    nome = caminho.name
    tamanho = caminho.stat().st_size
    _log(f"{nome} ({tamanho} bytes) para baixar, válido por {minutes} min")
    return {
        "url": f"{public_url.rstrip('/')}{PREFIXO}/{token}/{quote(nome)}",
        "expires_at": expira,
        "expires_in_min": minutes,
        "file": nome,
        "size": tamanho,
        "mime": mimetypes.guess_type(nome)[0] or "application/octet-stream",
    }


def resolve(
    token: str,
    media_dir: str | Path,
    secret: str,
    now: float | None = None,
) -> dict:
    """Confere o token do link e devolve o arquivo a servir.

    Returns:
        dict: {path, file, mime, size, inline, expires_at}

    Raises:
        LinkError: token torto, assinatura inválida, finalidade errada, prazo
                   vencido, arquivo fora da pasta ou já apagado
    """
    dados = _abrir(token, secret, "dl", now)
    rel = dados.get("rel")
    if not rel:
        raise LinkError("Link malformado.")

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
        "expires_at": dados["exp"],
    }


# ------------------------------------------------------------------ subir (/up)


def build_upload(
    destino: dict,
    public_url: str,
    secret: str,
    minutes: int = TTL_PADRAO_MIN,
    now: float | None = None,
) -> dict:
    """Cria o link da página de upload, com o destino dentro da assinatura.

    Args:
        destino: {to, number?, caption?, folder?, financeiro?} — veja `checar_destino`
        public_url: endereço externo deste servidor
        secret: segredo da assinatura
        minutes: validade em minutos
        now: hora de referência, para teste

    Returns:
        dict: {url, expires_at, expires_in_min, to, destino}
    """
    _exige_configuracao(public_url, secret)
    expira = _prazo(minutes, now)
    destino = checar_destino(destino)

    token = _token({"exp": expira, "k": "up", **destino}, secret)
    _log(f"página de upload para '{destino['to']}', válida por {minutes} min")
    return {
        "url": f"{public_url.rstrip('/')}{PREFIXO_UP}/{token}",
        "expires_at": expira,
        "expires_in_min": minutes,
        "to": destino["to"],
        "destino": descrever_destino(destino),
    }


def resolve_upload(token: str, secret: str, now: float | None = None) -> dict:
    """Confere o token de upload e devolve o destino assinado, mais o prazo."""
    dados = _abrir(token, secret, "up", now)
    return {**checar_destino(dados), "exp": dados["exp"]}


def checar_destino(destino: dict) -> dict:
    """Valida e normaliza o destino, mantendo só os campos que importam."""
    to = (destino.get("to") or "server").strip().lower()
    if to not in DESTINOS:
        raise LinkError(f"Destino '{to}' desconhecido (use: {', '.join(DESTINOS)}).")

    numero = (destino.get("number") or "").strip()
    if to in ("whatsapp", "both") and not numero:
        raise LinkError("Para mandar no WhatsApp é preciso o número do destinatário.")

    limpo = {"to": to}
    if numero:
        limpo["number"] = numero
    for campo in ("caption", "folder"):
        valor = (destino.get(campo) or "").strip()
        if valor:
            limpo[campo] = valor
    if destino.get("financeiro"):
        limpo["financeiro"] = True
    return limpo


def descrever_destino(destino: dict) -> str:
    """Uma linha em português dizendo para onde o arquivo vai, para a página."""
    to = destino["to"]
    if to == "server":
        return "fica guardado no servidor"
    base = "FINANCEIRO" if destino.get("financeiro") else "ARQUIVOS"
    pasta = f"{base}/{destino['folder']}" if destino.get("folder") else base
    drive = f"Google Drive ({pasta})"
    zap = f"WhatsApp ({destino.get('number', '?')})"
    if to == "drive":
        return drive
    if to == "whatsapp":
        return zap
    return f"{drive} e {zap}"


def nome_seguro(nome: str | None) -> str:
    """Reduz o nome que veio do navegador ao nome de um arquivo, e nada mais.

    Vem de fora, então some com caminho, separador e nome escondido; o que sobra
    é usado dentro da pasta de upload.
    """
    nome = (nome or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    nome = re.sub(r'[\x00-\x1f<>:"|?*]', "", nome).strip(" .")
    return nome[:120] or "arquivo.bin"


def guardar_upload(
    media_dir: str | Path,
    nome: str,
    pedacos,
    max_bytes: int,
) -> dict:
    """Grava na pasta de upload o que chegou pela rede.

    Args:
        media_dir: pasta de mídias
        nome: nome pedido pelo navegador (passa por `nome_seguro`)
        pedacos: iterável de bytes
        max_bytes: teto do upload

    Returns:
        dict: {path, file, size, mime}

    Raises:
        LinkError: upload acima do teto (o arquivo parcial é apagado) ou vazio
    """
    pasta = Path(media_dir) / PASTA_UPLOAD
    pasta.mkdir(parents=True, exist_ok=True)

    nome = nome_seguro(nome)
    destino = pasta / nome
    # Dois arquivos com o mesmo nome não se atropelam: o segundo ganha sufixo.
    if destino.exists():
        base, ponto, ext = nome.partition(".")
        for i in range(1, 1000):
            candidato = pasta / f"{base}-{i}{ponto}{ext}"
            if not candidato.exists():
                destino = candidato
                break

    tamanho = 0
    try:
        with open(destino, "wb") as arquivo:
            for pedaco in pedacos:
                tamanho += len(pedaco)
                if tamanho > max_bytes:
                    raise LinkError(
                        f"Arquivo acima do limite de {max_bytes // (1024 * 1024)} MB."
                    )
                arquivo.write(pedaco)
    except LinkError:
        destino.unlink(missing_ok=True)
        raise
    if not tamanho:
        destino.unlink(missing_ok=True)
        raise LinkError("O upload veio vazio.")

    _log(f"{destino.name} ({tamanho} bytes) recebido pelo navegador")
    return {
        "path": str(destino),
        "file": destino.name,
        "size": tamanho,
        "mime": mimetypes.guess_type(destino.name)[0] or "application/octet-stream",
    }


def entregar(arquivo: dict, destino: dict, *, para_drive=None, para_whatsapp=None) -> dict:
    """Leva o arquivo recebido ao destino que estava assinado no link.

    As duas entregas são independentes: quando o link pede as duas e uma falha, a
    outra continua valendo, e o resultado diz o que aconteceu com cada uma. Perder
    o arquivo inteiro porque o WhatsApp recusou seria o pior dos mundos — ele já
    está no servidor, e o `path` volta no resultado de qualquer jeito.

    Args:
        arquivo: o que `guardar_upload` devolveu
        destino: o destino já validado
        para_drive: f(path, folder, financeiro) -> dict, ou None
        para_whatsapp: f(path, number, caption) -> dict, ou None

    Returns:
        dict: {file, size, path, drive?, whatsapp?, erros?}
    """
    resultado = {"file": arquivo["file"], "size": arquivo["size"], "path": arquivo["path"]}
    erros = {}
    to = destino["to"]

    if to in ("drive", "both") and para_drive is not None:
        try:
            resultado["drive"] = para_drive(
                arquivo["path"], destino.get("folder", ""), bool(destino.get("financeiro"))
            )
        except Exception as e:  # a entrega falhar não pode derrubar a outra
            erros["drive"] = str(e)
            _log(f"falha ao mandar {arquivo['file']} para o Drive: {e}", "ERROR")

    if to in ("whatsapp", "both") and para_whatsapp is not None:
        try:
            resultado["whatsapp"] = para_whatsapp(
                arquivo["path"], destino.get("number", ""), destino.get("caption")
            )
        except Exception as e:
            erros["whatsapp"] = str(e)
            _log(f"falha ao mandar {arquivo['file']} para o WhatsApp: {e}", "ERROR")

    if erros:
        resultado["erros"] = erros
    return resultado


PAGINA = """<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Enviar arquivo</title><style>
 body{{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:2rem 1.25rem;
      background:#f6f7f9;color:#1b1f24}}
 .cartao{{max-width:30rem;margin:0 auto;background:#fff;border-radius:14px;padding:1.5rem;
      box-shadow:0 1px 3px rgba(0,0,0,.08)}}
 h1{{font-size:1.25rem;margin:0 0 .25rem}}
 .destino{{color:#57606a;font-size:.95rem;margin:0 0 1.25rem}}
 .destino b{{color:#1b1f24}}
 input[type=file]{{width:100%;padding:.9rem;border:2px dashed #d0d7de;border-radius:10px;
      background:#fafbfc;margin-bottom:1rem}}
 button{{width:100%;padding:.85rem;font-size:1rem;font-weight:600;color:#fff;background:#1f6feb;
      border:0;border-radius:10px;cursor:pointer}}
 button:disabled{{background:#8c959f;cursor:default}}
 #estado{{margin-top:1rem;font-size:.95rem;white-space:pre-wrap}}
 .ok{{color:#1a7f37}} .erro{{color:#cf222e}} .limite{{color:#8c959f;font-size:.85rem;margin-top:1rem}}
</style></head><body><div class="cartao">
<h1>Enviar arquivo</h1>
<p class="destino">Vai para: <b>{destino}</b></p>
<input type="file" id="arquivo">
<button id="enviar">Enviar</button>
<div id="estado"></div>
<p class="limite">O link vale at&eacute; {hora} e aceita at&eacute; {max_mb} MB por arquivo.</p>
</div><script>
const campo=document.getElementById('arquivo'),botao=document.getElementById('enviar'),
      estado=document.getElementById('estado');
botao.onclick=()=>{{
  const f=campo.files[0];
  if(!f){{estado.className='erro';estado.textContent='Escolha um arquivo.';return;}}
  botao.disabled=true;estado.className='';estado.textContent='Enviando '+f.name+'...';
  const r=new XMLHttpRequest();
  r.open('PUT',location.pathname+'?name='+encodeURIComponent(f.name));
  r.upload.onprogress=e=>{{if(e.lengthComputable)
     estado.textContent='Enviando '+f.name+' ('+Math.round(e.loaded/e.total*100)+'%)';}};
  r.onload=()=>{{
    botao.disabled=false;
    let d={{}};try{{d=JSON.parse(r.responseText)}}catch(e){{}}
    if(r.status>=200&&r.status<300&&!d.error){{
      estado.className='ok';
      const t=d.size<1024?d.size+' B':d.size<1048576?Math.round(d.size/1024)+' KB'
              :(d.size/1048576).toFixed(1)+' MB';
      let linhas=['Pronto: '+d.file+' ('+t+')'];
      if(d.drive&&d.drive.link)linhas.push('No Drive: '+d.drive.link);
      else if(d.drive)linhas.push('Guardado no Drive.');
      if(d.whatsapp)linhas.push('Enviado no WhatsApp.');
      if(d.erros)for(const k in d.erros)linhas.push('Falhou em '+k+': '+d.erros[k]);
      estado.textContent=linhas.join('\\n');
      campo.value='';
    }}else{{estado.className='erro';
      estado.textContent='Nao deu: '+(d.error||r.responseText||r.status);}}
  }};
  r.onerror=()=>{{botao.disabled=false;estado.className='erro';
    estado.textContent='Falha de rede durante o envio.';}};
  r.send(f);
}};
</script></body></html>"""


def pagina_upload(destino: dict, hora: str, max_mb: int) -> bytes:
    """A página que o link de upload abre: um seletor de arquivo e para onde ele vai."""
    return PAGINA.format(
        destino=descrever_destino(destino), hora=hora, max_mb=max_mb
    ).encode("utf-8")


def secret_from(config, env: dict) -> str:
    """O segredo da assinatura: o próprio, ou o token do MCP como reserva.

    Quem sobe este servidor já define MCP_AUTH_TOKEN; exigir uma segunda
    variável só para os links faria a funcionalidade nascer desligada no
    ambiente onde ela é útil.
    """
    return (getattr(config, "link_secret", "") or env.get("MCP_AUTH_TOKEN", "") or "").strip()
