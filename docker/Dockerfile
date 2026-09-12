# Dockerfile para Evolution API MCP Server
# Multi-stage build para imagem otimizada
#
# Build args:
#   INSTALL_AUDIO=true|false  inclui faster-whisper e baixa o modelo (padrão: true)
#   O extra [image] (pypdfium2 + Pillow) entra sempre: é o que permite ler
#   comprovante escaneado, devolvendo a página como imagem.
#   WHISPER_MODEL=small       modelo pré-baixado na imagem (tiny, base, small, medium)
#
# Comandos disponíveis no container:
#   python -m evoapi_mcp.mcp_http                      MCP via HTTP com Bearer token (conector do Claude)
#   uvicorn evoapi_mcp.http_server:app --host 0.0.0.0  API REST (padrão)

# =============================================================================
# Stage 1: Builder
# =============================================================================
FROM python:3.11-slim as builder

ARG INSTALL_AUDIO=true
ARG WHISPER_MODEL=small

WORKDIR /build

# Instalar dependências de build
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copiar arquivos de dependências
COPY pyproject.toml ./
COPY README.md ./

# Copiar código fonte
COPY src/ ./src/

# Instalar dependências em virtualenv
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Instalar projeto e dependências.
# Instalação normal (não editável): o venv é copiado para o runtime, onde
# /build não existe, então um editable install deixaria o pacote inacessível.
# Extra [pdf]: extração de texto de PDF. Extra [audio]: transcrição local.
RUN pip install --no-cache-dir --upgrade pip && \
    if [ "$INSTALL_AUDIO" = "true" ]; then \
        pip install --no-cache-dir ".[pdf,audio,image,bot]"; \
    else \
        pip install --no-cache-dir ".[pdf,image,bot]"; \
    fi

# Pré-baixar o modelo de transcrição: sem isso cada restart do container
# baixaria de novo algumas centenas de MB na primeira transcrição.
ENV HF_HOME=/opt/models
RUN if [ "$INSTALL_AUDIO" = "true" ]; then \
        python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"; \
    else \
        mkdir -p /opt/models; \
    fi

# =============================================================================
# Stage 2: Runtime
# =============================================================================
FROM python:3.11-slim

# Metadata
LABEL maintainer="Pablo Bispo <pablo@example.com>"
LABEL description="Evolution API MCP Server"
LABEL version="1.2.0"

# Instalar apenas runtime dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Criar usuário não-root
RUN useradd -m -u 1000 evoapi && \
    mkdir -p /app /data/media && \
    chown -R evoapi:evoapi /app /data/media

# Copiar virtualenv do builder
COPY --from=builder /opt/venv /opt/venv

# Copiar modelo de transcrição pré-baixado
COPY --from=builder --chown=evoapi:evoapi /opt/models /opt/models

# Copiar código fonte
COPY --chown=evoapi:evoapi src/ /app/src/
COPY --chown=evoapi:evoapi pyproject.toml /app/
COPY --chown=evoapi:evoapi README.md /app/

WORKDIR /app

# Configurar PATH para usar virtualenv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/models \
    EVOLUTION_MEDIA_DIR=/data/media

# Mudar para usuário não-root
USER evoapi

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:3000/health || exit 1

# Expor porta HTTP
EXPOSE 3000

# Comando padrão: API REST. Para o conector MCP do Claude, sobrescreva com:
#   python -m evoapi_mcp.mcp_http
CMD ["uvicorn", "evoapi_mcp.http_server:app", "--host", "0.0.0.0", "--port", "3000"]
