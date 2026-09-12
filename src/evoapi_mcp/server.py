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
from evoapi_mcp.transcription import TranscriptionError
from evoapi_mcp.formatters import (
    clean,
    compact_chat,
    compact_contact,
    compact_send_result,
    dumps,
)

# Inicializa o MCP server
mcp = FastMCP("Evolution API")

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
    """Envia imagem a partir de URL pública. Para arquivo local use send_file."""
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


@mcp.tool()
def send_image_base64(
    number: str,
    base64_data: str,
    caption: str = "",
    file_name: str = "image.png",
    mimetype: str = "image/png",
) -> str:
    """Envia imagem a partir de base64 (sem prefixo data:). Custa muitos tokens: prefira send_file."""
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


@mcp.tool()
def get_media_base64(message_id: str) -> str:
    """Devolve o anexo em base64. EVITE: custa dezenas de milhares de tokens; use download_media."""
    data = client.get_media(message_id)
    if isinstance(data, dict):
        data.pop("buffer", None)
    return _out(data)


# ============================================================================
# TOOLS - Status e Presença
# ============================================================================

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
