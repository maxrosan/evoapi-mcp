"""MCP Server para Evolution API.

Todas as tools devolvem JSON compacto (sem espaços, acentos sem escape, campos
nulos omitidos) e, por padrão, versões resumidas dos objetos da Evolution API.
Isso reduz em ~10x os tokens consumidos pelo LLM em cada chamada. Passe
`full=True` nas tools de leitura quando precisar do objeto bruto.
"""

import sys
from pathlib import Path
from typing import Any

# Adiciona o diretório src ao path para permitir importações
src_dir = Path(__file__).parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from mcp.server.fastmcp import FastMCP, Image
from evoapi_mcp.config import load_config
from evoapi_mcp.client import EvolutionClient
from evoapi_mcp.drive import DriveError
from evoapi_mcp.rendering import RenderError
from evoapi_mcp.storage import sweep, usage
from evoapi_mcp.weblink import WebLinkError
from evoapi_mcp.transcription import TranscriptionError
from evoapi_mcp.formatters import (
    clean,
    compact_chat,
    compact_contact,
    compact_send_result,
    dumps,
)

# A regra de escolha mora aqui, e não espalhada pelas descrições das tools.
#
# São muitos caminhos para a mesma coisa — mandar uma imagem tem cinco —, e cada
# docstring apontando para as outras é regra de roteamento repetida em cinco lugares:
# funciona enquanto o modelo lê todas, e falha calado quando ele escolhe a primeira
# que serve. Dita uma vez, no nível do servidor, ela chega antes da escolha.
INSTRUCTIONS = """Servidor do WhatsApp de Max, pela Evolution API.

O que encarece uma conversa aqui não é o envio, é o conteúdo de arquivo atravessando
o chat em base64: um PNG de 500 KB custa mais de 150 mil tokens, e chega ilegível
para você, porque vira texto. Toda tool daqui existe para o arquivo NÃO passar por
você. Escolha o caminho pela origem do arquivo:

- Imagem que VOCÊ vai criar (gráfico, cartão, aviso, tabela) → send_render: você
  escreve o SVG, o servidor rasteriza. É a diferença entre mil e cem mil tokens.
- Arquivo que já está na web → send_url (o servidor baixa; converte link de
  compartilhamento do Drive e do Dropbox), ou send_image se a URL for direta.
- Arquivo já no disco do servidor, como o `path` que download_media devolveu → send_file.
- Arquivo arquivado no Drive por este servidor → send_drive_file, por id, pelo link
  ou por pasta + nome.
- Texto → send_text_message.

Para LER o que chegou, na mesma lógica: download_media(extract_text=True) quando o
documento tem camada de texto, view_media quando não tem (comprovante fotografado,
PDF escaneado) e transcribe_audio para áudio. get_media_base64 e as tools de base64
estão desligadas por padrão justamente por serem o caminho caro; ligue-as em
EVOLUTION_BASE64_TOOLS só se nada mais servir.

Números vão no formato internacional sem '+' (5511999999999). Um jid (@g.us, @lid)
também é aceito onde se pede número."""

# Inicializa o MCP server
mcp = FastMCP("Evolution API", instructions=INSTRUCTIONS)

# Carrega configuração e inicializa cliente
try:
    config = load_config()
    client = EvolutionClient(config)
except Exception as e:
    print(f"Falha ao inicializar o servidor: {e}", file=sys.stderr)
    sys.exit(1)


def _out(obj: Any) -> str:
    """Serializa a resposta de forma compacta (uma única string JSON)."""
    return dumps(obj)


def _enviado(resultado: Any) -> dict:
    """Compacta a resposta de um envio e registra a mensagem como já tratada.

    O registro importa por causa da conversa pessoal: lá toda mensagem do dono é
    instrução, e a instância não distingue o que ele digitou do que o assistente
    mandou. Sem marcar, a própria resposta voltaria como pedido e entraria em laço.
    """
    try:
        message_id = ((resultado or {}).get("key") or {}).get("id")
        if message_id:
            from evoapi_mcp.webhook import EVENTS
            EVENTS.store.mark(message_id, instruction="[enviada pelo assistente]")
    except Exception:  # nunca deixar o registro atrapalhar um envio bem-sucedido
        pass
    return compact_send_result(resultado)


def _limit(value: int | None) -> int:
    return value if value and value > 0 else config.default_limit


def _max_text(value: int | None) -> int | None:
    if value is None:
        value = config.max_text_chars
    return value if value > 0 else None


# ============================================================================
# TOOLS - Envio de Mensagens
# ============================================================================

@mcp.tool()
def send_text_message(number: str, text: str, link_preview: bool = True) -> str:
    """Envia texto para um número WhatsApp.

    Args:
        number: internacional sem '+' (ex: 5511999999999)
        text: conteúdo da mensagem
        link_preview: mostrar preview de links
    Returns: {ok, id, to, ts, status}
    """
    return _out(_enviado(client.send_text(number=number, text=text, link_preview=link_preview)))


@mcp.tool()
def send_image(number: str, image_url: str, caption: str | None = None) -> str:
    """Envia imagem a partir de URL pública: quem baixa é a Evolution.

    É o envio mais barato de todos quando a URL é direta e a Evolution a enxerga. Se
    falhar, ou se o link for de compartilhamento (Drive, Dropbox), use send_url, que
    baixa aqui no servidor. Para arquivo local use send_file.
    """
    return _out(_enviado(
        client.send_media(number=number, media_url=image_url, media_type="image", caption=caption)
    ))


@mcp.tool()
def send_document(
    number: str,
    document_url: str,
    filename: str | None = None,
    caption: str | None = None,
) -> str:
    """Envia documento (pdf, docx, xlsx...) a partir de URL pública. Para arquivo local use send_file."""
    return _out(_enviado(client.send_media(
        number=number, media_url=document_url, media_type="document", caption=caption, filename=filename
    )))


@mcp.tool()
def send_video(number: str, video_url: str, caption: str | None = None) -> str:
    """Envia vídeo a partir de URL pública. Para arquivo local use send_file."""
    return _out(_enviado(
        client.send_media(number=number, media_url=video_url, media_type="video", caption=caption)
    ))


@mcp.tool()
def send_audio(number: str, audio_url: str) -> str:
    """Envia áudio a partir de URL pública. Para arquivo local use send_file."""
    return _out(_enviado(client.send_media(number=number, media_url=audio_url, media_type="audio")))


@mcp.tool()
def send_file(
    number: str,
    file_path: str,
    caption: str | None = None,
    media_type: str | None = None,
    file_name: str | None = None,
) -> str:
    """Envia um arquivo do disco (imagem, vídeo, áudio ou documento) sem passar base64 pelo chat.

    Preferir esta tool a send_*_base64: o servidor lê e codifica o arquivo.

    Args:
        number: internacional sem '+'
        file_path: caminho do arquivo local (ex: o `path` retornado por download_media)
        caption: legenda opcional
        media_type: image|video|audio|document (padrão: deduzido pela extensão)
        file_name: nome exibido no WhatsApp (padrão: nome do arquivo)
    """
    result = client.send_file(
        number=number, file_path=file_path, caption=caption, media_type=media_type, file_name=file_name
    )
    out = _enviado(result)
    if isinstance(result, dict) and result.get("_file"):
        out["file"] = result["_file"]
    return _out(out)


@mcp.tool()
def react_to_message(
    number: str,
    message_id: str,
    emoji: str,
    from_me: bool = False,
) -> str:
    """Reage a uma mensagem com um emoji, em vez de mandar outra mensagem.

    Use quando a resposta certa é um sinal, não um texto: confirmar que viu, concordar,
    agradecer. Não polui a conversa e as outras pessoas do grupo não leem um status.

    Args:
        number: internacional sem '+', ou o jid da conversa
        message_id: id da mensagem (campo `id` em get_chat_messages/find_messages)
        emoji: o emoji, ex: "👍". String vazia REMOVE a reação
        from_me: True se a mensagem é do próprio Max — sem isso a reação não aparece
    """
    return _out(client.send_reaction(
        number=number, message_id=message_id, emoji=emoji, from_me=from_me
    ))


@mcp.tool()
def send_render(
    number: str,
    svg: str,
    caption: str | None = None,
    file_name: str = "imagem.png",
    width: int | None = None,
    height: int | None = None,
    background: str | None = "white",
) -> str:
    """Desenha uma imagem a partir de SVG e a envia. Use quando VOCÊ for criar a imagem.

    É para gráfico, cartão, aviso, tabela, comparativo, convite: você escreve o SVG
    e o servidor rasteriza e manda. O desenho viaja como texto (2 a 5 KB num gráfico
    inteiro); o mesmo PNG em send_image_base64 custaria dezenas de milhares de tokens.

    Escreva um SVG completo, com xmlns e width/height no elemento raiz, e use fontes
    comuns (sans-serif, serif, monospace): quem desenha é o servidor, com as fontes dele.
    Para imagem que já existe em arquivo no servidor use send_file, e para imagem que já
    está na web, send_url (ou send_image, se a URL for direta).

    Args:
        number: internacional sem '+'
        svg: o documento SVG
        caption: legenda opcional
        file_name: nome exibido no WhatsApp
        width: largura em pixels (padrão: a do próprio SVG)
        height: altura em pixels (padrão: proporcional à largura)
        background: cor de fundo; null mantém a transparência
    Returns: {ok, id, to, ts, status, file: {path, size, type}}
    """
    try:
        result = client.send_render(
            number=number, svg=svg, caption=caption, file_name=file_name,
            width=width, height=height, background=background,
        )
    except RenderError as e:
        return _out({"error": str(e)})
    out = _enviado(result)
    if isinstance(result, dict) and result.get("_file"):
        out["file"] = result["_file"]
    return _out(out)


@mcp.tool()
def send_url(
    number: str,
    url: str,
    caption: str | None = None,
    media_type: str | None = None,
    file_name: str | None = None,
) -> str:
    """Envia um arquivo que já está na web, a partir do link. Pela conversa passa só a URL.

    Use para imagem, PDF ou vídeo que já existe em algum lugar público: o servidor
    baixa e envia, sem base64 no chat. Link de compartilhamento do Google Drive e do
    Dropbox é convertido sozinho para o link do arquivo — no Drive, ele precisa estar
    como "qualquer pessoa com o link".

    Para imagem que você mesmo vai desenhar use send_render, e para arquivo que já está
    no servidor (o `path` de download_media, por exemplo) use send_file.

    Args:
        number: internacional sem '+'
        url: endereço público do arquivo
        caption: legenda opcional
        media_type: image|video|audio|document (padrão: deduzido do Content-Type)
        file_name: nome exibido no WhatsApp (padrão: o nome que veio no link)
    Returns: {ok, id, to, ts, status, file: {path, size, type, url}}
    """
    try:
        result = client.send_url(
            number=number, url=url, caption=caption,
            media_type=media_type, file_name=file_name,
        )
    except WebLinkError as e:
        return _out({"error": str(e)})
    out = _enviado(result)
    if isinstance(result, dict) and result.get("_file"):
        out["file"] = result["_file"]
    return _out(out)


def send_document_base64(
    number: str,
    base64_data: str,
    file_name: str,
    caption: str = "",
    mimetype: str = "application/pdf",
) -> str:
    """Envia documento a partir de base64 (sem prefixo data:). Custa muitos tokens: prefira send_file."""
    return _out(_enviado(client.send_media_base64(
        number=number, base64_data=base64_data, media_type="document",
        file_name=file_name, caption=caption or None, mimetype=mimetype,
    )))


def send_image_base64(
    number: str,
    base64_data: str,
    caption: str = "",
    file_name: str = "image.png",
    mimetype: str = "image/png",
) -> str:
    """Envia imagem a partir de base64 (sem prefixo data:).

    Custa muitos tokens: prefira send_file (arquivo já no servidor), send_image (URL
    pública) ou send_render (imagem que você mesmo desenha, em SVG).
    """
    return _out(_enviado(client.send_media_base64(
        number=number, base64_data=base64_data, media_type="image",
        file_name=file_name, caption=caption or None, mimetype=mimetype,
    )))


# ============================================================================
# TOOLS - Chats e Mensagens
# ============================================================================

@mcp.tool()
def get_chat_messages(
    number: str,
    limit: int | None = None,
    page: int = 1,
    query: str | None = None,
    max_text: int | None = None,
    full: bool = False,
) -> str:
    """Mensagens de uma conversa (mais recentes primeiro), em formato compacto.

    Cada mensagem: {id, ts, from ('me' ou número), name, type, text, file, mime, size, reply_to}.
    Use o `id` em download_media para baixar anexos.

    Args:
        number: internacional sem '+'
        limit: quantidade (padrão: EVOLUTION_DEFAULT_LIMIT=20). Ajuste quando o usuário pedir N mensagens
        page: página, 1 = mais recentes
        query: filtra localmente por texto/legenda/nome de arquivo (case-insensitive)
        max_text: corte do texto por mensagem (0 = sem corte; padrão: 500)
        full: True devolve os registros brutos da API (muito mais tokens)
    """
    return _out(client.get_messages_by_number(
        number=number, limit=_limit(limit), page=page, query=query,
        max_text=_max_text(max_text), compact=not full,
    ))


@mcp.tool()
def find_messages(
    query: str | None = None,
    chat_id: str | None = None,
    limit: int | None = None,
    page: int = 1,
    max_text: int | None = None,
    full: bool = False,
) -> str:
    """Busca mensagens em todas as conversas (ou em chat_id), formato compacto.

    Args:
        query: termo buscado em texto/legenda/nome de arquivo; a varredura é local, até 500 mensagens
        chat_id: jid do chat (ex: 5511999999999@s.whatsapp.net ou grupo ...@g.us)
        limit: máximo de resultados (padrão: 20)
        page: página quando não há query
        max_text: corte do texto por mensagem (0 = sem corte)
        full: True devolve registros brutos (muito mais tokens)
    """
    return _out(client.find_messages(
        query=query, chat_id=chat_id, limit=_limit(limit), page=page,
        max_text=_max_text(max_text), compact=not full,
    ))


@mcp.tool()
def list_chats(limit: int | None = None, full: bool = False) -> str:
    """Conversas recentes: [{jid, number, name, group, unread, last_ts, last_from, last}].

    Args:
        limit: quantidade (padrão: 20). Ajuste quando o usuário pedir N conversas
        full: True devolve objetos brutos (muito mais tokens)
    """
    chats = client.find_chats()
    if not isinstance(chats, list):
        return _out(chats)
    chats = chats[: _limit(limit)]
    if full:
        return _out(chats)
    return _out([compact_chat(c) for c in chats])


@mcp.tool()
def get_contacts(
    contact_id: str | None = None,
    limit: int | None = None,
    search: str | None = None,
    full: bool = False,
) -> str:
    """Contatos salvos: [{jid, number, name, group}].

    Args:
        contact_id: jid exato (ex: 5511999999999@s.whatsapp.net)
        limit: quantidade (padrão: 20)
        search: filtra por trecho do nome ou do número (case-insensitive)
        full: True devolve objetos brutos, com URL de foto etc. (muito mais tokens)
    """
    contacts = client.fetch_contacts(contact_id=contact_id)
    if search:
        s = search.casefold()
        contacts = [
            c for c in contacts
            if s in str(c.get("pushName") or "").casefold() or s in str(c.get("remoteJid") or "")
        ]
    total = len(contacts)
    contacts = contacts[: _limit(limit)]
    items = contacts if full else [compact_contact(c) for c in contacts]
    return _out({"total": total, "count": len(items), "contacts": items})


@mcp.tool()
def get_contact_name_by_number(number: str) -> str:
    """Nome salvo de um contato pelo número. Returns: {number, name|null}"""
    return _out({"number": number, "name": client.get_contact_name(number)})


# ============================================================================
# TOOLS - Mídia
# ============================================================================

@mcp.tool()
def download_media(
    message_id: str,
    save_dir: str | None = None,
    filename: str | None = None,
    extract_text: bool = False,
    max_chars: int = 3000,
    password: str | None = None,
) -> str:
    """Baixa o anexo de uma mensagem para o disco e devolve só caminho e metadados (sem base64).

    Args:
        message_id: campo `id` da mensagem (get_chat_messages/find_messages)
        save_dir: pasta destino (padrão: EVOLUTION_MEDIA_DIR)
        filename: nome do arquivo (padrão: nome original)
        extract_text: True extrai texto de PDF/txt, ou transcreve quando o anexo é áudio/vídeo
        max_chars: limite do texto extraído
        password: senha de um PDF protegido. O arquivo é gravado já destravado, e o texto
                  passa a ser extraível. Sem ela, um PDF com senha volta com text_error
    Returns: {path, file, mime, size, type, decrypted?, pages?, text?, text_error?}
    """
    return _out(client.download_media(
        message_id=message_id, save_dir=save_dir, filename=filename,
        extract_text=extract_text, max_chars=max_chars, password=password,
    ))


@mcp.tool()
def transcribe_audio(
    message_id: str | None = None,
    file_path: str | None = None,
    language: str | None = None,
    max_chars: int = 4000,
    force: bool = False,
) -> str:
    """Transcreve em texto um áudio do WhatsApp (voice note) ou um arquivo local.

    Use quando o usuário pedir o conteúdo de um áudio: "o que ele falou no áudio",
    "transcreva o áudio", "resuma os áudios de hoje". Mensagens de áudio aparecem
    em get_chat_messages/find_messages com type "audio" (voice=true para voice note).
    O áudio é baixado e transcrito no servidor; só o texto volta. O resultado fica
    em cache por message_id.

    Args:
        message_id: id da mensagem de áudio (informe este OU file_path)
        file_path: caminho de um áudio/vídeo já no disco
        language: idioma, ex: 'pt' (padrão: configuração ou detecção automática)
        max_chars: corte do texto devolvido (0 = sem corte)
        force: refaz a transcrição ignorando o cache
    Returns: {text, backend, model, language, seconds, path, cached}
    """
    if bool(message_id) == bool(file_path):
        raise ValueError("Informe message_id OU file_path (exatamente um dos dois)")
    try:
        if message_id:
            return _out(client.transcribe_message(
                message_id=message_id, language=language, max_chars=max_chars, force=force
            ))
        return _out(client.transcribe_file(file_path=file_path, language=language, max_chars=max_chars))
    except TranscriptionError as e:
        return _out({"error": str(e), "transcription": client.transcriber.describe()})


@mcp.tool()
def archive_to_drive(
    message_id: str | None = None,
    file_path: str | None = None,
    folder: str = "",
    filename: str | None = None,
    password: str | None = None,
) -> str:
    """Arquiva um anexo do WhatsApp no Google Drive sem trazer o arquivo para a conversa.

    O arquivo vai do WhatsApp para o servidor e do servidor para o Drive. Use no lugar
    de get_media_base64 + upload: funciona com arquivos grandes e custa alguns tokens
    em vez de milhares. As pastas que faltarem no caminho são criadas.

    Args:
        message_id: id da mensagem com o anexo (informe este OU file_path)
        file_path: caminho de um arquivo que já está no servidor
        folder: caminho da pasta, relativo à pasta base configurada
                (ex: "MR/2026/08.2026/BOLETO")
        filename: nome final do arquivo (padrão: o nome original)
        password: senha de um PDF protegido; a versão arquivada vai destravada
    Returns: {id, name, folder, size, link, decrypted?}
    """
    if bool(message_id) == bool(file_path):
        raise ValueError("Informe message_id OU file_path (exatamente um dos dois)")
    try:
        if message_id:
            return _out(client.archive_media(
                message_id=message_id, folder=folder, filename=filename, password=password
            ))
        return _out(client.archive_file(file_path=file_path, folder=folder, filename=filename))
    except DriveError as e:
        return _out({"error": str(e), "drive": client.drive.describe()})


@mcp.tool()
def send_drive_file(
    number: str,
    file_ref: str | None = None,
    folder: str | None = None,
    name: str | None = None,
    caption: str | None = None,
    media_type: str | None = None,
    file_name: str | None = None,
) -> str:
    """Reenvia pelo WhatsApp um arquivo já arquivado no Drive por archive_to_drive.

    Use para "manda de novo aquele boleto que arquivamos": o arquivo vai do Drive para
    o servidor e do servidor para o WhatsApp, sem base64 na conversa e sem precisar que
    ele ainda esteja no disco daqui.

    Aponte o arquivo de um dos dois jeitos: por `file_ref` (o id ou o `link` que
    archive_to_drive devolveu) ou por `folder` + `name`. Só alcança o que este servidor
    arquivou — arquivo que outro aplicativo criou no Drive ele não enxerga.

    Args:
        number: internacional sem '+'
        file_ref: id do arquivo no Drive, ou o link do arquivo
        folder: pasta onde procurar, relativa à pasta base (ex: "MR/2026/08.2026/BOLETO")
        name: nome do arquivo dentro dessa pasta
        caption: legenda opcional
        media_type: image|video|audio|document (padrão: deduzido do tipo no Drive)
        file_name: nome exibido no WhatsApp (padrão: o nome no Drive)
    Returns: {ok, id, to, ts, status, file: {path, size, type, drive_id, link}}
    """
    try:
        result = client.send_drive_file(
            number=number, file_ref=file_ref, folder=folder, name=name,
            caption=caption, media_type=media_type, file_name=file_name,
        )
    except DriveError as e:
        return _out({"error": str(e), "drive": client.drive.describe()})
    out = _enviado(result)
    if isinstance(result, dict) and result.get("_file"):
        out["file"] = result["_file"]
    return _out(out)


@mcp.tool()
def view_media(
    message_id: str,
    page: int = 1,
    pages: int = 1,
    password: str | None = None,
) -> list:
    """Mostra o documento como imagem, para você LER o que está escrito nele.

    Use quando download_media disser que não há texto extraível: comprovante
    fotografado, PDF escaneado, print de tela. A página vira imagem e você lê com
    a própria visão, sem OCR. Custa cerca de 1.500 tokens por página, contra
    dezenas de milhares do base64.

    Para documento que já tem texto, prefira download_media com extract_text:
    é muito mais barato.

    Args:
        message_id: id da mensagem com o anexo
        page: primeira página a mostrar (PDF), começando em 1
        pages: quantas páginas, no máximo 5
        password: senha, se o PDF for protegido
    """
    try:
        resultado = client.render_media(
            message_id=message_id, first_page=page, pages=pages, password=password
        )
    except RenderError as e:
        return [_out({"error": str(e)})]

    resumo = _out({
        "file": resultado.get("file"),
        "mime": resultado.get("mime"),
        "size": resultado.get("size"),
        "pages_rendered": resultado.get("pages_rendered"),
        "first_page": page,
    })
    return [resumo] + [Image(data=img, format="jpeg") for img in resultado["images"]]


def get_media_base64(message_id: str) -> str:
    """Devolve o anexo em base64. EVITE: custa dezenas de milhares de tokens; use download_media."""
    data = client.get_media(message_id)
    if isinstance(data, dict):
        data.pop("buffer", None)
    return _out(data)


# As três acima são o caminho caro, e ficam DESLIGADAS por padrão.
#
# Elas existem por paridade com o conector antigo, mas uma tool visível é uma tool
# que será escolhida: estando na lista, o modelo eventualmente manda um PNG inteiro
# em base64 e queima cem mil tokens fazendo o que send_file faz de graça. Escondê-las
# é mais eficaz que avisar na descrição que são caras — o aviso concorre com a
# conveniência, a ausência não. EVOLUTION_BASE64_TOOLS=1 traz as três de volta para
# quem depende delas.
if config.base64_tools:
    for _tool in (send_document_base64, send_image_base64, get_media_base64):
        mcp.tool()(_tool)
    print("Tools de base64 EXPOSTAS (EVOLUTION_BASE64_TOOLS=1)", file=sys.stderr)


# ============================================================================
# TOOLS - Status e Presença
# ============================================================================

@mcp.tool()
def cleanup_media(days: int | None = None, dry_run: bool = False) -> str:
    """Apaga da pasta de mídias os arquivos mais velhos que `days`.

    A faxina já roda sozinha uma vez por dia (EVOLUTION_MEDIA_TTL_DAYS). Use esta
    tool quando o disco apertar antes disso, ou com dry_run=True para ver o que
    sairia. Texto de áudio já transcrito (.transcripts) nunca é apagado.

    Args:
        days: idade máxima em dias (padrão: EVOLUTION_MEDIA_TTL_DAYS)
        dry_run: só conta, não apaga
    Returns: {removed, freed_mb, ttl_days, media: {files, mb, free_mb}}
    """
    ttl = config.media_ttl_days if days is None else days
    resultado = sweep(client.media_dir, ttl_days=ttl, dry_run=dry_run)
    resultado["media"] = usage(client.media_dir)
    return _out(resultado)


@mcp.tool()
def get_connection_status() -> str:
    """Estado da conexão da instância. Returns: {instance, state}"""
    response = client.get_connection_state()
    inst = response.get("instance", response) if isinstance(response, dict) else {}
    return _out({
        "instance": client.instance_id,
        "state": (inst.get("state") if isinstance(inst, dict) else None) or response.get("state", "unknown"),
    })


@mcp.tool()
def set_presence(status: str, number: str | None = None) -> str:
    """Define presença: available | unavailable | composing | recording."""
    valid_statuses = ["available", "unavailable", "composing", "recording"]
    if status not in valid_statuses:
        raise ValueError(f"Status inválido: '{status}'. Válidos: {', '.join(valid_statuses)}")
    return _out(client.set_presence(status=status, number=number))


@mcp.tool()
def get_instance_info(full: bool = False) -> str:
    """Informações da instância. full=True inclui a resposta bruta da API."""
    info = client.get_instance_info()
    if not full:
        info.pop("info", None)
    info["transcription"] = client.transcriber.describe()
    info["drive"] = client.drive.describe()
    info["media"] = usage(client.media_dir) | {"ttl_days": config.media_ttl_days}
    from evoapi_mcp.webhook import EVENTS
    info["armazenamento"] = EVENTS.store.describe()
    return _out(info)


@mcp.tool()
def pending_triggers(limit: int = 10) -> str:
    """Mensagens "IA:" que Max mandou e que ainda não foram respondidas.

    Use numa sessão em laço: chame, trate o que vier e confirme com mark_triggers_handled.
    Cada item traz o id, a conversa, a instrução e quando chegou.

    Duas portas, as duas só para mensagens do próprio Max:
    - na conversa dele com ele mesmo (tipo_conversa "pessoal"), TODA mensagem é
      instrução, sem prefixo;
    - nas demais conversas e grupos, só o que começa com "IA:".
    Mensagem de terceiro nunca entra aqui.

    Devolve {count, pendentes: [...]} — count 0 significa que não há nada a fazer.

    Args:
        limit: máximo de pendências devolvidas (padrão: 10)
    """
    from evoapi_mcp.webhook import EVENTS

    pendentes = []
    for e in EVENTS.pending(limit=limit):
        pendentes.append(clean({
            "id": e.get("message_id"),
            "chat": e.get("chat_jid") or e.get("chat"),
            "tipo_conversa": "pessoal" if e.get("self_chat") else e.get("chat_type"),
            "quando": e.get("at"),
            "instrucao": e.get("instruction"),
        }))
    return _out({"count": len(pendentes), "pendentes": pendentes})


@mcp.tool()
def mark_triggers_handled(message_ids: list[str]) -> str:
    """Marca acionamentos como tratados, para não voltarem em pending_triggers.

    Chame depois de responder, mesmo que você tenha decidido não responder: sem isso
    a mesma instrução reaparece a cada volta do laço.

    Args:
        message_ids: os ids devolvidos por pending_triggers
    """
    from evoapi_mcp.webhook import EVENTS

    return _out({
        "marcados": EVENTS.mark_handled(message_ids),
        "restantes": len(EVENTS.pending(limit=999)),
        "armazenamento": EVENTS.store.describe(),
    })


@mcp.tool()
def clear_cache() -> str:
    """Limpa o cache de nomes de contatos (força atualização na próxima consulta)."""
    client.clear_cache()
    return _out({"ok": True})


# ============================================================================
# Entry Point
# ============================================================================

def main() -> None:
    """Executa o servidor MCP em stdio (Claude Desktop / Claude Code)."""
    mcp.run()


if __name__ == "__main__":
    main()
