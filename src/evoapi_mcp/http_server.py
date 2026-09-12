"""HTTP Server para expor MCP tools via REST API.

Este servidor permite acessar as ferramentas MCP via HTTP/REST em vez de stdio,
possibilitando uso em containers Docker e acesso remoto.
"""

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Any
import sys

from .client import EvolutionClient
from .config import load_config
from .drive import DriveError
from .formatters import compact_chat, compact_contact, compact_send_result
from .rendering import RenderError
from .storage import sweep, usage
from .transcription import TranscriptionError
from .weblink import WebLinkError

# =============================================================================
# FASTAPI APP
# =============================================================================

app = FastAPI(
    title="Evolution API MCP Server",
    description="MCP Server para Evolution API - Integração WhatsApp via HTTP",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS para permitir chamadas de frontends
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Em produção, especificar origins permitidos
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cliente Evolution API (global)
client: EvolutionClient | None = None


# =============================================================================
# MODELS (Pydantic)
# =============================================================================

class HealthResponse(BaseModel):
    status: str
    version: str
    instance: str


class SendTextRequest(BaseModel):
    number: str = Field(..., description="Número de telefone (formato internacional)")
    text: str = Field(..., description="Texto da mensagem")
    link_preview: bool = Field(True, description="Exibir preview de links")


class SendMediaRequest(BaseModel):
    number: str = Field(..., description="Número de telefone")
    media_url: str = Field(..., description="URL da mídia")
    media_type: str = Field(..., description="Tipo: image, video, document, audio")
    caption: str | None = Field(None, description="Legenda da mídia")
    filename: str | None = Field(None, description="Nome do arquivo")


class SetPresenceRequest(BaseModel):
    number: str = Field(..., description="Número de telefone")
    presence: str = Field(..., description="Presença: available, unavailable, composing, recording")


class MarkAsReadRequest(BaseModel):
    number: str = Field(..., description="Número de telefone")


class ArchiveChatRequest(BaseModel):
    number: str = Field(..., description="Número de telefone")
    archive: bool = Field(True, description="True para arquivar, False para desarquivar")


class DeleteChatRequest(BaseModel):
    number: str = Field(..., description="Número de telefone")


class CheckNumberRequest(BaseModel):
    number: str = Field(..., description="Número de telefone")


class SendFileRequest(BaseModel):
    number: str = Field(..., description="Número de telefone")
    file_path: str = Field(..., description="Caminho do arquivo no servidor")
    caption: str | None = Field(None, description="Legenda")
    media_type: str | None = Field(None, description="image, video, audio, document (padrão: pela extensão)")
    file_name: str | None = Field(None, description="Nome exibido no WhatsApp")


class SendBase64Request(BaseModel):
    number: str = Field(..., description="Número de telefone")
    base64_data: str = Field(..., description="Conteúdo em base64 (sem prefixo data:)")
    media_type: str = Field("document", description="image, video, audio, document")
    file_name: str | None = Field(None, description="Nome do arquivo")
    caption: str | None = Field(None, description="Legenda")
    mimetype: str | None = Field(None, description="Mimetype (ex: application/pdf)")


class TranscribeRequest(BaseModel):
    message_id: str | None = Field(None, description="Id da mensagem de áudio")
    file_path: str | None = Field(None, description="Caminho de um áudio no servidor")
    language: str | None = Field(None, description="Idioma, ex: pt")
    max_chars: int = Field(4000, description="Corte do texto devolvido")
    force: bool = Field(False, description="Ignora o cache de transcrição")


class DownloadMediaRequest(BaseModel):
    message_id: str = Field(..., description="Id da mensagem com anexo")
    save_dir: str | None = Field(None, description="Pasta destino (padrão: EVOLUTION_MEDIA_DIR)")
    filename: str | None = Field(None, description="Nome do arquivo")
    extract_text: bool = Field(False, description="Extrair texto de PDF/txt")
    max_chars: int = Field(3000, description="Limite do texto extraído")
    password: str | None = Field(None, description="Senha de um PDF protegido")


class SendRenderRequest(BaseModel):
    number: str = Field(..., description="Número de telefone")
    svg: str = Field(..., description="Documento SVG a rasterizar")
    caption: str | None = Field(None, description="Legenda")
    file_name: str = Field("imagem.png", description="Nome exibido no WhatsApp")
    width: int | None = Field(None, description="Largura em pixels")
    height: int | None = Field(None, description="Altura em pixels")
    background: str | None = Field("white", description="Cor de fundo (null = transparente)")


class SendUrlRequest(BaseModel):
    number: str = Field(..., description="Número de telefone")
    url: str = Field(..., description="Endereço público do arquivo")
    caption: str | None = Field(None, description="Legenda")
    media_type: str | None = Field(None, description="image/video/audio/document")
    file_name: str | None = Field(None, description="Nome exibido no WhatsApp")


class SendDriveFileRequest(BaseModel):
    number: str = Field(..., description="Número de telefone")
    file_ref: str | None = Field(None, description="Id ou link do arquivo no Drive")
    folder: str | None = Field(None, description="Pasta onde procurar, com name")
    name: str | None = Field(None, description="Nome do arquivo dentro da pasta")
    caption: str | None = Field(None, description="Legenda")
    media_type: str | None = Field(None, description="image/video/audio/document")
    file_name: str | None = Field(None, description="Nome exibido no WhatsApp")


class ArchiveRequest(BaseModel):
    message_id: str | None = Field(None, description="Id da mensagem com o anexo")
    file_path: str | None = Field(None, description="Arquivo já no servidor")
    folder: str = Field("", description="Pasta destino, relativa à pasta base")
    filename: str | None = Field(None, description="Nome final do arquivo")
    password: str | None = Field(None, description="Senha de um PDF protegido")


class ViewMediaRequest(BaseModel):
    message_id: str = Field(..., description="Id da mensagem com o anexo")
    page: int = Field(1, description="Primeira página (PDF)")
    pages: int = Field(1, description="Quantas páginas, no máximo 5")
    password: str | None = Field(None, description="Senha de um PDF protegido")


class CleanupMediaRequest(BaseModel):
    days: int | None = Field(None, description="Idade máxima em dias (padrão: EVOLUTION_MEDIA_TTL_DAYS)")
    dry_run: bool = Field(False, description="Só conta, não apaga")


# =============================================================================
# LIFECYCLE EVENTS
# =============================================================================

@app.on_event("startup")
async def startup_event():
    """Inicializa o cliente Evolution API ao iniciar o servidor."""
    global client
    try:
        config = load_config()
        client = EvolutionClient(config)
        print(f"✅ MCP HTTP Server inicializado - Instância: {config.instance_name}", file=sys.stderr)
    except Exception as e:
        print(f"❌ Erro ao inicializar cliente: {e}", file=sys.stderr)
        raise


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup ao desligar o servidor."""
    print("🛑 MCP HTTP Server desligado", file=sys.stderr)


# =============================================================================
# ENDPOINTS - HEALTH & INFO
# =============================================================================

@app.get("/", response_model=dict)
async def root():
    """Endpoint raiz - informações básicas do servidor."""
    return {
        "name": "Evolution API MCP Server",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health"
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Healthcheck para Docker e monitoramento."""
    if not client:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cliente não inicializado"
        )

    return HealthResponse(
        status="healthy",
        version="1.0.0",
        instance=client.instance_id
    )


# =============================================================================
# ENDPOINTS - CHAT OPERATIONS
# =============================================================================

@app.get("/chats", response_model=list[dict[str, Any]])
async def get_chats(limit: int = 20, full: bool = False):
    """Lista conversas recentes (compactas por padrão; full=true devolve o objeto bruto)."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        chats = client.find_chats()
        if not isinstance(chats, list):
            return []
        chats = chats[:limit]
        return chats if full else [compact_chat(c) for c in chats]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/contacts", response_model=list[dict[str, Any]])
async def get_contacts(
    contact_id: str | None = None,
    limit: int | None = None,
    search: str | None = None,
    full: bool = False,
):
    """Busca contatos salvos no WhatsApp (compactos por padrão)."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        contacts = client.fetch_contacts(contact_id=contact_id)
        if search:
            s = search.casefold()
            contacts = [
                c for c in contacts
                if s in str(c.get("pushName") or "").casefold() or s in str(c.get("remoteJid") or "")
            ]
        if limit is not None:
            contacts = contacts[:limit]
        return contacts if full else [compact_contact(c) for c in contacts]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/messages/{number}", response_model=dict[str, Any])
async def get_messages(
    number: str,
    limit: int = 20,
    page: int = 1,
    query: str | None = None,
    max_text: int = 500,
    full: bool = False,
):
    """Busca mensagens de uma conversa (compactas por padrão)."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return client.get_messages_by_number(
            number=number, limit=limit, page=page, query=query,
            max_text=max_text or None, compact=not full,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/messages", response_model=dict[str, Any])
async def search_messages(
    query: str | None = None,
    chat_id: str | None = None,
    limit: int = 20,
    page: int = 1,
    max_text: int = 500,
    full: bool = False,
):
    """Busca mensagens em todas as conversas, com filtro textual local."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return client.find_messages(
            query=query, chat_id=chat_id, limit=limit, page=page,
            max_text=max_text or None, compact=not full,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# ENDPOINTS - MESSAGE SENDING
# =============================================================================

@app.post("/messages/text", response_model=dict[str, Any])
async def send_text(request: SendTextRequest):
    """Envia mensagem de texto."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return client.send_text(
            number=request.number,
            text=request.text,
            link_preview=request.link_preview
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/messages/media", response_model=dict[str, Any])
async def send_media(request: SendMediaRequest):
    """Envia mídia (imagem, vídeo, documento, áudio) a partir de URL pública."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return client.send_media(
            number=request.number,
            media_url=request.media_url,
            media_type=request.media_type,
            caption=request.caption,
            filename=request.filename
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/messages/file", response_model=dict[str, Any])
async def send_file(request: SendFileRequest):
    """Envia um arquivo que já está no disco do servidor (sem base64 no request)."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        result = client.send_file(
            number=request.number,
            file_path=request.file_path,
            caption=request.caption,
            media_type=request.media_type,
            file_name=request.file_name,
        )
        out = compact_send_result(result)
        if isinstance(result, dict) and result.get("_file"):
            out["file"] = result["_file"]
        return out
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/messages/base64", response_model=dict[str, Any])
async def send_base64(request: SendBase64Request):
    """Envia mídia a partir de conteúdo base64."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return compact_send_result(client.send_media_base64(
            number=request.number,
            base64_data=request.base64_data,
            media_type=request.media_type,
            file_name=request.file_name,
            caption=request.caption,
            mimetype=request.mimetype,
        ))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/messages/render", response_model=dict[str, Any])
async def send_render(request: SendRenderRequest):
    """Rasteriza um SVG no servidor e envia o PNG."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        result = client.send_render(
            number=request.number,
            svg=request.svg,
            caption=request.caption,
            file_name=request.file_name,
            width=request.width,
            height=request.height,
            background=request.background,
        )
        out = compact_send_result(result)
        if isinstance(result, dict) and result.get("_file"):
            out["file"] = result["_file"]
        return out
    except RenderError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/messages/url", response_model=dict[str, Any])
async def send_url(request: SendUrlRequest):
    """Baixa o arquivo da URL no servidor e o envia."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        result = client.send_url(
            number=request.number,
            url=request.url,
            caption=request.caption,
            media_type=request.media_type,
            file_name=request.file_name,
        )
        out = compact_send_result(result)
        if isinstance(result, dict) and result.get("_file"):
            out["file"] = result["_file"]
        return out
    except WebLinkError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/messages/drive", response_model=dict[str, Any])
async def send_drive_file(request: SendDriveFileRequest):
    """Reenvia pelo WhatsApp um arquivo arquivado no Drive por este servidor."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        result = client.send_drive_file(
            number=request.number,
            file_ref=request.file_ref,
            folder=request.folder,
            name=request.name,
            caption=request.caption,
            media_type=request.media_type,
            file_name=request.file_name,
        )
        out = compact_send_result(result)
        if isinstance(result, dict) and result.get("_file"):
            out["file"] = result["_file"]
        return out
    except DriveError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# ENDPOINTS - MEDIA
# =============================================================================

@app.post("/media/download", response_model=dict[str, Any])
async def download_media(request: DownloadMediaRequest):
    """Baixa o anexo de uma mensagem para o disco do servidor e devolve caminho e metadados."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return client.download_media(
            message_id=request.message_id,
            save_dir=request.save_dir,
            filename=request.filename,
            extract_text=request.extract_text,
            max_chars=request.max_chars,
            password=request.password,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/media/transcribe", response_model=dict[str, Any])
async def transcribe_audio(request: TranscribeRequest):
    """Transcreve um áudio do WhatsApp (por message_id) ou um arquivo local do servidor."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    if bool(request.message_id) == bool(request.file_path):
        raise HTTPException(status_code=400, detail="Informe message_id OU file_path")

    try:
        if request.message_id:
            return client.transcribe_message(
                message_id=request.message_id,
                language=request.language,
                max_chars=request.max_chars,
                force=request.force,
            )
        return client.transcribe_file(
            file_path=request.file_path,
            language=request.language,
            max_chars=request.max_chars,
        )
    except TranscriptionError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/media/archive", response_model=dict[str, Any])
async def archive_to_drive(request: ArchiveRequest):
    """Arquiva no Google Drive um anexo do WhatsApp, ou um arquivo já no servidor."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    if bool(request.message_id) == bool(request.file_path):
        raise HTTPException(status_code=400, detail="Informe message_id OU file_path")

    try:
        if request.message_id:
            return client.archive_media(
                message_id=request.message_id,
                folder=request.folder,
                filename=request.filename,
                password=request.password,
            )
        return client.archive_file(
            file_path=request.file_path,
            folder=request.folder,
            filename=request.filename,
        )
    except DriveError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/media/view", response_model=dict[str, Any])
async def view_media(request: ViewMediaRequest):
    """Renderiza o documento como imagem.

    Diferente da tool MCP, que entrega a imagem pelo protocolo, aqui as páginas voltam
    em base64: um cliente REST sabe decodificar, e não é ele que paga tokens.
    """
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    import base64 as _b64

    try:
        resultado = client.render_media(
            message_id=request.message_id,
            first_page=request.page,
            pages=request.pages,
            password=request.password,
        )
    except RenderError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "file": resultado.get("file"),
        "mime": resultado.get("mime"),
        "size": resultado.get("size"),
        "pages_rendered": resultado.get("pages_rendered"),
        "format": resultado.get("format"),
        "images": [_b64.b64encode(img).decode("ascii") for img in resultado["images"]],
    }


@app.post("/media/cleanup", response_model=dict[str, Any])
async def cleanup_media(request: CleanupMediaRequest):
    """Apaga da pasta de mídias o que passou do prazo."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    # A configuração já está no cliente; recarregar o .env a cada request só
    # criaria um jeito de o endpoint falhar por motivo que nada tem a ver com ele.
    ttl = getattr(client.config, "media_ttl_days", 0) if request.days is None else request.days
    resultado = sweep(client.media_dir, ttl_days=ttl, dry_run=request.dry_run)
    resultado["media"] = usage(client.media_dir)
    return resultado


# =============================================================================
# ENDPOINTS - INSTANCE & PRESENCE
# =============================================================================

@app.get("/instance/status", response_model=dict[str, Any])
async def get_instance_status():
    """Busca status da instância."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        info = client.get_instance_info()
        # Uso de disco junto do status: é aqui que alguém olha quando algo parou,
        # e "disco cheio" não se anuncia sozinho nas outras mensagens de erro.
        info["media"] = usage(client.media_dir)
        return info
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/presence", response_model=dict[str, Any])
async def set_presence(request: SetPresenceRequest):
    """Define presença (online, offline, digitando, gravando)."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return client.set_presence(
            status=request.presence,
            number=request.number,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# ENDPOINTS - CHAT MANAGEMENT
# =============================================================================

@app.post("/messages/mark-read", response_model=dict[str, Any])
async def mark_as_read(request: MarkAsReadRequest):
    """Marca mensagem como lida."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return client.mark_message_as_read(number=request.number)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chats/archive", response_model=dict[str, Any])
async def archive_chat(request: ArchiveChatRequest):
    """Arquiva ou desarquiva uma conversa."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return client.archive_chat(
            number=request.number,
            archive=request.archive
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/chats/{number}", response_model=dict[str, Any])
async def delete_chat(number: str):
    """Deleta uma conversa."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return client.delete_chat(number=number)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# ENDPOINTS - PROFILE & UTILITIES
# =============================================================================

@app.get("/profile/picture/{number}", response_model=dict[str, Any])
async def get_profile_picture(number: str):
    """Busca foto de perfil de um contato."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return client.get_profile_picture(number=number)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/profile/status/{number}", response_model=dict[str, Any])
async def get_profile_status(number: str):
    """Busca status/bio de um contato."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return client.get_profile_status(number=number)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/check-number", response_model=dict[str, Any])
async def check_number(request: CheckNumberRequest):
    """Verifica se um número está registrado no WhatsApp."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return client.check_number_on_whatsapp(number=request.number)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/profile/business/{number}", response_model=dict[str, Any])
async def get_business_profile(number: str):
    """Busca perfil comercial de um contato."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        return client.get_business_profile(number=number)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# CACHE MANAGEMENT
# =============================================================================

@app.post("/cache/clear", response_model=dict[str, str])
async def clear_cache():
    """Limpa o cache de contatos manualmente."""
    if not client:
        raise HTTPException(status_code=503, detail="Cliente não inicializado")

    try:
        client.clear_cache()
        return {"status": "success", "message": "Cache limpo com sucesso"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# MAIN (para desenvolvimento local)
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "evoapi_mcp.http_server:app",
        host="0.0.0.0",
        port=3000,
        reload=True,
        log_level="info"
    )
