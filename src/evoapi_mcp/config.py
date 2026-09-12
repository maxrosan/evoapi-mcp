"""Configuração do MCP Evolution API."""

import sys
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EvolutionConfig(BaseSettings):
    """Configuração para conexão com Evolution API.

    As variáveis de ambiente necessárias são:
    - EVOLUTION_BASE_URL: URL do servidor Evolution API (ex: http://localhost:8080)
    - EVOLUTION_API_TOKEN: Token de autenticação da API
    - EVOLUTION_INSTANCE_NAME: Nome da instância WhatsApp configurada
    - EVOLUTION_TIMEOUT: (Opcional) Timeout para requisições em segundos (padrão: 30)
    - EVOLUTION_MEDIA_DIR: (Opcional) Pasta onde mídias baixadas são salvas
    - EVOLUTION_MEDIA_TTL_DAYS: (Opcional) Idade máxima dos arquivos dessa pasta, em dias
      (padrão: ~/.evoapi-mcp/media)
    - EVOLUTION_DEFAULT_LIMIT: (Opcional) Quantidade padrão de itens em listagens (padrão: 20)
    - EVOLUTION_MAX_TEXT_CHARS: (Opcional) Corte de texto por mensagem no modo compacto (padrão: 500)

    Transcrição de áudios (voice notes):
    - EVOLUTION_TRANSCRIBE_BACKEND: auto | api | local | off (padrão: auto)
    - EVOLUTION_TRANSCRIBE_API_URL: endpoint compatível com a API da OpenAI
    - EVOLUTION_TRANSCRIBE_API_KEY: chave da API (ou use OPENAI_API_KEY / GROQ_API_KEY)
    - EVOLUTION_TRANSCRIBE_MODEL: modelo (padrão: whisper-1 na API, small no local)
    - EVOLUTION_TRANSCRIBE_LANGUAGE: idioma dos áudios, ex: pt (padrão: detectar)
    - EVOLUTION_TRANSCRIBE_TIMEOUT: timeout da transcrição em segundos (padrão: 120)
    - EVOLUTION_TRANSCRIBE_MAX_MB: tamanho máximo aceito pela API (padrão: 25)

    Arquivamento no Google Drive (opcional):
    - EVOLUTION_DRIVE_CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN: credenciais OAuth
    - EVOLUTION_DRIVE_ROOT / _ROOT_ID: pasta base do arquivamento

    Conversa pessoal (eu comigo mesmo):
    - EVOLUTION_OWNER_NUMBER: seu número, ex: 5584999290327. Nessa conversa toda
      mensagem sua é instrução, sem precisar do prefixo "IA:"
    """

    base_url: str = Field(
        ...,
        description="URL base do servidor Evolution API"
    )
    api_token: str = Field(
        ...,
        description="Token de autenticação da API"
    )
    instance_name: str = Field(
        ...,
        description="Nome da instância WhatsApp"
    )
    timeout: int = Field(
        default=30,
        description="Timeout para requisições em segundos",
        ge=5,
        le=300
    )
    media_dir: str = Field(
        default="~/.evoapi-mcp/media",
        description="Pasta onde mídias baixadas do WhatsApp são salvas"
    )
    media_ttl_days: int = Field(
        default=30,
        description="Idade máxima dos arquivos em media_dir, em dias (0 desliga a faxina)",
        ge=0,
        le=3650
    )
    default_limit: int = Field(
        default=20,
        description="Quantidade padrão de mensagens/chats/contatos retornados",
        ge=1,
        le=500
    )
    max_text_chars: int = Field(
        default=500,
        description="Tamanho máximo do texto de cada mensagem no modo compacto (0 = sem corte)",
        ge=0,
        le=65536
    )
    transcribe_backend: str = Field(
        default="auto",
        description="Backend de transcrição de áudios: auto, api, local ou off"
    )
    transcribe_api_url: str = Field(
        default="",
        description="Endpoint de transcrição compatível com a API da OpenAI"
    )
    transcribe_api_key: str = Field(
        default="",
        description="Chave do serviço de transcrição (ou use OPENAI_API_KEY / GROQ_API_KEY)"
    )
    transcribe_model: str = Field(
        default="",
        description="Modelo de transcrição (padrão: whisper-1 na API, small no local)"
    )
    transcribe_language: str = Field(
        default="",
        description="Idioma dos áudios (ex: pt). Vazio = detecção automática"
    )
    transcribe_timeout: int = Field(
        default=120,
        description="Timeout da transcrição em segundos",
        ge=10,
        le=1800
    )
    transcribe_max_mb: int = Field(
        default=25,
        description="Tamanho máximo de áudio aceito pelo backend de API, em MB",
        ge=1,
        le=1024
    )

    model_config = SettingsConfigDict(
        env_prefix="EVOLUTION_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: str) -> str:
        """Normaliza a URL base removendo trailing slash."""
        return v.rstrip("/")

    @field_validator("media_dir")
    @classmethod
    def expand_media_dir(cls, v: str) -> str:
        """Expande ~ e variáveis de ambiente no caminho da pasta de mídia."""
        from pathlib import Path
        import os
        return str(Path(os.path.expandvars(v)).expanduser())

    drive_client_id: str = Field(default="", description="Client ID OAuth do Google")
    drive_client_secret: str = Field(default="", description="Client secret OAuth do Google")
    drive_refresh_token: str = Field(default="", description="Refresh token OAuth do Google")
    drive_root: str = Field(default="", description="Pasta base no Drive, ex: FINANCEIRO")
    drive_root_id: str = Field(default="", description="Id da pasta base (criada pelo próprio app)")
    owner_number: str = Field(
        default="",
        description="Número do dono da instância; identifica a conversa dele com ele mesmo"
    )

    @field_validator("transcribe_backend")
    @classmethod
    def validate_transcribe_backend(cls, v: str) -> str:
        """Valida o backend de transcrição."""
        value = (v or "auto").strip().lower()
        valid = {"auto", "api", "local", "off"}
        if value not in valid:
            raise ValueError(f"transcribe_backend inválido: '{v}'. Válidos: {', '.join(sorted(valid))}")
        return value

    @field_validator("api_token", "instance_name")
    @classmethod
    def validate_not_empty(cls, v: str) -> str:
        """Valida que campos obrigatórios não estão vazios."""
        if not v or not v.strip():
            raise ValueError("Campo não pode estar vazio")
        return v.strip()


def load_config() -> EvolutionConfig:
    """Carrega e valida a configuração.

    Returns:
        EvolutionConfig: Configuração validada

    Raises:
        ValueError: Se a configuração for inválida
        SystemExit: Se variáveis obrigatórias estiverem faltando
    """
    try:
        config = EvolutionConfig()

        # Log da configuração (sem expor o token)
        print(
            f"Configuração carregada:",
            f"  Base URL: {config.base_url}",
            f"  Instância: {config.instance_name}",
            f"  Timeout: {config.timeout}s",
            f"  Media dir: {config.media_dir}",
            f"  Media TTL: {config.media_ttl_days} dia(s)" if config.media_ttl_days else "  Media TTL: desligado",
            sep="\n",
            file=sys.stderr
        )

        return config

    except Exception as e:
        print(
            f"Erro ao carregar configuração: {e}",
            "",
            "Variáveis de ambiente necessárias:",
            "  - EVOLUTION_BASE_URL",
            "  - EVOLUTION_API_TOKEN",
            "  - EVOLUTION_INSTANCE_NAME",
            "",
            "Copie .env.example para .env e configure os valores.",
            sep="\n",
            file=sys.stderr
        )
        sys.exit(1)
