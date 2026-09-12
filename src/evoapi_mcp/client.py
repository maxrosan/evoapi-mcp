"""Cliente HTTP direto para Evolution API."""

import base64
import json
import mimetypes
import os
import sys
import re
import requests
from datetime import datetime, timedelta
from typing import Any
from pathlib import Path

# Adiciona o diretório src ao path para permitir importações
src_dir = Path(__file__).parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from evoapi_mcp.config import EvolutionConfig
from evoapi_mcp.formatters import (
    compact_error,
    compact_message,
    extract_records,
    message_matches,
    truncate,
)
from evoapi_mcp.drive import DriveClient, DriveError
from evoapi_mcp.rendering import RenderError, is_renderable, render, render_svg
from evoapi_mcp.storage import sweep_if_due
from evoapi_mcp.transcription import TranscriptionError, Transcriber, is_transcribable
from evoapi_mcp.weblink import fetch as fetch_url

PERSONAL_JID_SUFFIX = "@s.whatsapp.net"
GROUP_JID_SUFFIX = "@g.us"
LID_JID_SUFFIX = "@lid"


# Constantes de validação
VALID_MEDIA_TYPES = {"image", "video", "document", "audio"}
VALID_PRESENCE_STATUS = {"available", "unavailable", "composing", "recording"}
MAX_TEXT_LENGTH = 65536  # 64KB - limite do WhatsApp
MAX_CAPTION_LENGTH = 1024  # Limite de legenda
MAX_SEARCH_SCAN = 500  # Máximo de mensagens varridas na busca local por texto
SEARCH_PAGE_SIZE = 100  # Tamanho da página usado na varredura

# Extensão -> tipo de mídia para send_file
_EXT_MEDIA_TYPE = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image", ".webp": "image",
    ".mp4": "video", ".mov": "video", ".3gp": "video", ".mkv": "video",
    ".mp3": "audio", ".ogg": "audio", ".opus": "audio", ".m4a": "audio", ".wav": "audio", ".aac": "audio",
}

# Mimetype -> extensão para mídias baixadas sem nome de arquivo
_MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "video/mp4": ".mp4", "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
    "application/pdf": ".pdf",
}


class EvolutionAPIError(Exception):
    """Erro base para operações da Evolution API."""
    pass


class InstanceDisconnectedError(EvolutionAPIError):
    """Erro quando a instância não está conectada."""
    pass


class InvalidPhoneNumberError(EvolutionAPIError):
    """Erro quando o número de telefone é inválido."""
    pass


class EvolutionClient:
    """Cliente HTTP direto para Evolution API.

    Faz chamadas HTTP diretas à API REST seguindo a documentação oficial.
    Usa o header 'apikey' para autenticação e {instanceId} nos endpoints.
    """

    def __init__(self, config: EvolutionConfig):
        """Inicializa o cliente Evolution API.

        Args:
            config: Configuração da Evolution API
        """
        self.config = config
        self.base_url = config.base_url.rstrip('/')
        self.api_key = config.api_token
        self.instance_id = config.instance_name
        self.timeout = config.timeout
        self.media_dir = Path(config.media_dir)
        self.transcriber = Transcriber(config)
        self.drive = DriveClient(config)

        # Headers padrão para todas as requisições
        self.headers = {
            'apikey': self.api_key,
            'Content-Type': 'application/json'
        }

        # Cache de nomes de contatos (número -> nome)
        self._contact_names_cache: dict[str, str | None] = {}
        # número -> jid real da conversa (pode ser @lid); expira com o mesmo TTL
        self._jid_cache: dict[str, tuple[str, datetime]] = {}
        self._cache_timestamp: datetime | None = None
        self._cache_ttl = timedelta(minutes=5)  # Cache expira após 5 minutos

        self._log(f"Cliente inicializado para instância '{self.instance_id}'")

    def _log(self, message: str, level: str = "INFO") -> None:
        """Registra uma mensagem no stderr.

        Args:
            message: Mensagem a ser registrada
            level: Nível do log (INFO, WARNING, ERROR)
        """
        print(f"[{level}] Evolution API: {message}", file=sys.stderr)

    @staticmethod
    def validate_phone_number(number: str) -> str:
        """Valida e normaliza um número de telefone.

        O número deve estar no formato internacional sem '+' ou espaços.
        Exemplo: 5511999999999 (Brasil)

        Args:
            number: Número de telefone a validar

        Returns:
            str: Número normalizado

        Raises:
            InvalidPhoneNumberError: Se o número for inválido
        """
        # Remove caracteres não numéricos
        clean_number = re.sub(r'\D', '', number)

        # Valida formato básico (mínimo 10 dígitos, máximo 15)
        if not re.match(r'^\d{10,15}$', clean_number):
            raise InvalidPhoneNumberError(
                f"Número inválido: '{number}'. "
                "Use formato internacional sem '+' (ex: 5511999999999)"
            )

        return clean_number

    @staticmethod
    def validate_url(url: str, param_name: str = "url") -> None:
        """Valida se uma URL é válida.

        Args:
            url: URL a validar
            param_name: Nome do parâmetro (para mensagem de erro)

        Raises:
            ValueError: Se a URL for inválida
        """
        if not url or not isinstance(url, str):
            raise ValueError(f"{param_name} não pode ser vazio")

        if not url.startswith(("http://", "https://")):
            raise ValueError(
                f"{param_name} inválida: '{url}'. "
                "URL deve começar com http:// ou https://"
            )

    @staticmethod
    def validate_text_length(text: str, max_length: int, param_name: str = "text") -> None:
        """Valida o tamanho de um texto.

        Args:
            text: Texto a validar
            max_length: Tamanho máximo permitido
            param_name: Nome do parâmetro (para mensagem de erro)

        Raises:
            ValueError: Se o texto exceder o tamanho máximo
        """
        if len(text) > max_length:
            raise ValueError(
                f"{param_name} muito longo: {len(text)} caracteres. "
                f"Máximo permitido: {max_length} caracteres"
            )

    @staticmethod
    def validate_media_type(media_type: str) -> None:
        """Valida o tipo de mídia.

        Args:
            media_type: Tipo de mídia a validar

        Raises:
            ValueError: Se o tipo de mídia for inválido
        """
        if media_type not in VALID_MEDIA_TYPES:
            raise ValueError(
                f"media_type inválido: '{media_type}'. "
                f"Valores válidos: {', '.join(sorted(VALID_MEDIA_TYPES))}"
            )

    @staticmethod
    def _personal_jid_number(remote_jid: str | None) -> str:
        """Dígitos de um jid @s.whatsapp.net; vazio para grupos e @lid."""
        if not remote_jid or not remote_jid.endswith(PERSONAL_JID_SUFFIX):
            return ""
        return re.sub(r"\D", "", remote_jid[: -len(PERSONAL_JID_SUFFIX)])

    @staticmethod
    def _chat_alt_number(chat: dict[str, Any]) -> str:
        """Telefone de uma conversa @lid, exposto em lastMessage.key.remoteJidAlt."""
        key = (chat.get("lastMessage") or {}).get("key") or {}
        alt = key.get("remoteJidAlt") or ""
        return re.sub(r"\D", "", alt.split("@")[0])

    def resolve_send_target(self, number: str) -> str:
        """Destino de envio: número validado, ou jid (@lid/@g.us/@s.whatsapp.net) intacto.

        Nunca remove os não-dígitos de um jid: '1000...@lid' viraria um número
        de 15 dígitos válido que endereça outra pessoa.
        """
        if "@" in number:
            return number.strip()
        return self.validate_phone_number(number)

    def resolve_chat_jid(self, identifier: str) -> tuple[str, bool]:
        """Resolve número ou jid para o jid em que a conversa está salva.

        O WhatsApp está migrando conversas de <numero>@s.whatsapp.net para
        <id opaco>@lid; nesse caso o número só aparece em remoteJidAlt da
        lista de conversas. Retorna (jid, resolvido). Quando não há
        correspondência devolve o palpite <numero>@s.whatsapp.net com False.
        """
        if "@" in identifier:
            return identifier.strip(), True

        clean_number = self.validate_phone_number(identifier)
        cached = self._jid_cache.get(clean_number)
        if cached and datetime.now() - cached[1] <= self._cache_ttl:
            return cached[0], True

        fallback = f"{clean_number}{PERSONAL_JID_SUFFIX}"
        try:
            chats = self.find_chats(enrich_with_names=False)
        except EvolutionAPIError as e:
            self._log(f"Falha ao listar conversas para resolver {clean_number}: {e}", "WARNING")
            return fallback, False

        for chat in chats if isinstance(chats, list) else []:
            remote_jid = chat.get("remoteJid") or ""
            if remote_jid == fallback or (
                remote_jid.endswith(LID_JID_SUFFIX) and self._chat_alt_number(chat) == clean_number
            ):
                self._jid_cache[clean_number] = (remote_jid, datetime.now())
                return remote_jid, True

        return fallback, False

    def _is_cache_expired(self) -> bool:
        """Verifica se o cache de contatos expirou.

        Returns:
            bool: True se o cache expirou ou nunca foi construído, False caso contrário
        """
        if not self._cache_timestamp:
            return True
        return datetime.now() - self._cache_timestamp > self._cache_ttl

    def clear_cache(self) -> None:
        """Limpa o cache de nomes de contatos.

        Este método é útil quando você quer forçar a atualização dos nomes
        dos contatos sem precisar reiniciar o cliente.

        Example:
            client.clear_cache()  # Cache será reconstruído na próxima chamada
        """
        self._contact_names_cache.clear()
        self._jid_cache.clear()
        self._cache_timestamp = None
        self._log("Cache de contatos limpo")

    def _make_request(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
        params: dict | None = None
    ) -> dict[str, Any]:
        """Faz uma requisição HTTP à API.

        Args:
            method: Método HTTP (GET, POST, PUT, DELETE)
            endpoint: Endpoint da API (ex: /chat/findChats/{instanceId})
            data: Dados do corpo da requisição (para POST/PUT)
            params: Parâmetros de query string (para GET)

        Returns:
            dict: Resposta JSON da API

        Raises:
            EvolutionAPIError: Se houver erro na requisição
        """
        # Substitui {instanceId} no endpoint
        endpoint = endpoint.replace('{instanceId}', self.instance_id)
        url = f"{self.base_url}{endpoint}"

        try:
            self._log(f"{method} {endpoint}")

            response = requests.request(
                method=method,
                url=url,
                headers=self.headers,
                json=data,
                params=params,
                timeout=self.timeout
            )

            # Verifica se a resposta foi bem-sucedida
            response.raise_for_status()

            # Tenta retornar JSON, se houver
            try:
                return response.json()
            except ValueError:
                return {"status": "success", "data": response.text}

        except requests.exceptions.HTTPError as e:
            # Corpo de erro truncado: páginas HTML/stack traces custam muitos tokens
            error_msg = f"HTTP {e.response.status_code}: {compact_error(e.response.text)}"
            self._log(error_msg, "ERROR")

            # Detecta erros específicos
            if e.response.status_code == 401:
                raise EvolutionAPIError("Falha de autenticação. Verifique o EVOLUTION_API_TOKEN")
            elif e.response.status_code == 404:
                raise EvolutionAPIError(f"Endpoint não encontrado: {endpoint}")
            else:
                raise EvolutionAPIError(error_msg)

        except requests.exceptions.Timeout:
            raise EvolutionAPIError(
                f"Timeout ao executar {method} {endpoint}. "
                f"Tente novamente ou aumente EVOLUTION_TIMEOUT"
            )

        except requests.exceptions.ConnectionError as e:
            raise EvolutionAPIError(f"Erro de conexão: {str(e)}")

        except Exception as e:
            self._log(f"Erro inesperado: {str(e)}", "ERROR")
            raise EvolutionAPIError(f"Erro em {method} {endpoint}: {str(e)}")

    # =========================================================================
    # CHAT OPERATIONS
    # =========================================================================

    def find_chats(self, enrich_with_names: bool = True) -> dict[str, Any]:
        """Busca todas as conversas ativas.

        Endpoint: POST /chat/findChats/{instanceId}

        Args:
            enrich_with_names: Se True, enriquece conversas com nomes dos contatos quando pushName for null

        Returns:
            dict: Lista de conversas com informações detalhadas

        Raises:
            EvolutionAPIError: Se houver erro na requisição
        """
        self._log("Buscando conversas")
        chats = self._make_request("POST", "/chat/findChats/{instanceId}", data={})

        # Enriquece com nomes de contatos se solicitado
        if enrich_with_names and isinstance(chats, list):
            self._log("Enriquecendo conversas com nomes de contatos")

            # OTIMIZAÇÃO: Busca TODOS os contatos de uma vez ao invés de um por um
            contacts_map = self._build_contacts_map()

            for chat in chats:
                if chat.get("pushName") is None and chat.get("remoteJid"):
                    # Extrai o número do remoteJid
                    remote_jid = chat["remoteJid"]
                    # Ignora grupos (terminam com @g.us)
                    if not remote_jid.endswith(GROUP_JID_SUFFIX):
                        clean_number = self._personal_jid_number(remote_jid) or self._chat_alt_number(chat)
                        # Lookup local (muito mais rápido que HTTP)
                        if clean_number and clean_number in contacts_map:
                            chat["pushName"] = contacts_map[clean_number]
                            chat["_enriched"] = True

        return chats

    def _build_contacts_map(self) -> dict[str, str]:
        """Constrói um mapa de número -> nome a partir de todos os contatos.

        Usa cache com TTL de 5 minutos. Se o cache expirou, reconstrói o mapa.

        Returns:
            dict: Mapeamento de número limpo para nome do contato
        """
        # Se cache não expirou, retorna cache existente
        if not self._is_cache_expired() and self._contact_names_cache:
            self._log("Usando cache de contatos existente")
            return self._contact_names_cache

        try:
            # Cache expirou ou está vazio - reconstrói
            self._log("Reconstruindo cache de contatos...")

            # Busca todos os contatos de uma vez (retorna lista direta)
            contact_list = self.fetch_contacts()
            contacts_map = {}

            for contact in contact_list:
                # Extrai número do remoteJid (formato: 5511999999999@s.whatsapp.net)
                remote_jid = contact.get("remoteJid", "")
                # Ignora grupos e ids opacos (@lid não é telefone)
                clean_number = self._personal_jid_number(remote_jid)
                if not clean_number:
                    continue

                # Pega o pushName
                name = contact.get("pushName")
                if clean_number and name:
                    contacts_map[clean_number] = name

            # Atualiza cache e timestamp
            self._contact_names_cache = contacts_map
            self._cache_timestamp = datetime.now()

            self._log(f"Cache de contatos atualizado: {len(contacts_map)} contatos")
            return contacts_map

        except Exception as e:
            self._log(f"Erro ao construir mapa de contatos: {e}", "WARNING")
            return {}

    def find_messages(
        self,
        query: str | None = None,
        chat_id: str | None = None,
        limit: int = 50,
        page: int = 1,
        max_text: int | None = 500,
        compact: bool = True,
    ) -> dict[str, Any]:
        """Busca mensagens, opcionalmente filtrando por chat e por texto.

        Endpoint: POST /chat/findMessages/{instanceId}

        A Evolution API não faz busca textual: quando `query` é informado, as
        mensagens são varridas em páginas (até MAX_SEARCH_SCAN) e filtradas
        localmente, devolvendo só as que casam. Isso evita mandar centenas de
        mensagens irrelevantes para o LLM.

        Args:
            query: Termo de busca (case-insensitive) em texto, legenda, nome de arquivo
            chat_id: ID do chat (ex: 5511999999999@s.whatsapp.net)
            limit: Máximo de mensagens retornadas
            page: Página (1 = mais recentes)
            max_text: Corte de texto por mensagem no modo compacto (0/None = sem corte)
            compact: Se True, retorna mensagens compactas; se False, registros brutos

        Returns:
            dict: {total?, pages?, page?, count, messages: [...]}
        """
        self._log(f"Buscando mensagens (limit={limit}, page={page}, query={'sim' if query else 'não'})")

        if query:
            return self._search_messages_locally(query, chat_id, limit, max_text, compact)

        raw = self._fetch_messages_page(chat_id, limit, page)
        records, meta = extract_records(raw)
        if not compact:
            return {**meta, "count": len(records), "messages": records}
        return {**meta, "count": len(records), "messages": [compact_message(r, max_text) for r in records]}

    def _fetch_messages_page(self, chat_id: str | None, page_size: int, page: int) -> Any:
        """Chama findMessages com os campos aceitos pela v1 (limit) e v2 (page/offset)."""
        payload: dict[str, Any] = {"page": page, "offset": page_size, "limit": page_size}
        if chat_id:
            payload["where"] = {"key": {"remoteJid": chat_id}}
        return self._make_request("POST", "/chat/findMessages/{instanceId}", data=payload)

    def _search_messages_locally(
        self,
        query: str,
        chat_id: str | None,
        limit: int,
        max_text: int | None,
        compact: bool,
    ) -> dict[str, Any]:
        matches: list[dict[str, Any]] = []
        scanned = 0
        page = 1
        while scanned < MAX_SEARCH_SCAN and len(matches) < limit:
            raw = self._fetch_messages_page(chat_id, SEARCH_PAGE_SIZE, page)
            records, meta = extract_records(raw)
            if not records:
                break
            for record in records:
                scanned += 1
                c = compact_message(record, None)
                if message_matches(c, query):
                    matches.append(compact_message(record, max_text) if compact else record)
                    if len(matches) >= limit:
                        break
            total_pages = meta.get("pages")
            if total_pages is not None and page >= int(total_pages):
                break
            if len(records) < SEARCH_PAGE_SIZE:
                break
            page += 1
        return {"query": query, "scanned": scanned, "count": len(matches), "messages": matches}

    def get_messages_by_number(
        self,
        number: str,
        limit: int = 50,
        page: int = 1,
        max_text: int | None = 500,
        compact: bool = True,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Obtém mensagens de uma conversa por número.

        Args:
            number: Número de telefone
            limit: Número máximo de mensagens
            page: Página (1 = mais recentes)
            max_text: Corte de texto por mensagem (modo compacto)
            compact: Retorna mensagens compactas (True) ou brutas (False)
            query: Filtro textual opcional

        Returns:
            dict: Mensagens da conversa

        Raises:
            InvalidPhoneNumberError: Se o número for inválido
            EvolutionAPIError: Se houver erro
        """
        chat_id, resolved = self.resolve_chat_jid(number)
        result = self.find_messages(
            query=query, chat_id=chat_id, limit=limit, page=page, max_text=max_text, compact=compact
        )
        if isinstance(result, dict):
            result["chat"] = chat_id
            if not resolved and not result.get("count"):
                result["hint"] = (
                    "Nenhuma conversa encontrada para este número. "
                    "Confira o jid em list_chats e passe-o diretamente."
                )
        return result

    def fetch_contacts(self, contact_id: str | None = None) -> list[dict[str, Any]]:
        """Busca contatos salvos no WhatsApp com filtros opcionais.

        Endpoint: POST /chat/findContacts/{instanceId}

        Args:
            contact_id: ID do contato específico (ex: 5511999999999@s.whatsapp.net).
                       Se None, retorna todos os contatos.

        Returns:
            list: Lista de contatos com informações completas:
                  - remoteJid: ID do contato
                  - pushName: Nome do contato
                  - isGroup: Se é grupo ou contato individual
                  - profilePicUrl: URL da foto de perfil

        Raises:
            EvolutionAPIError: Se houver erro na requisição

        Example:
            # Buscar todos os contatos
            all_contacts = client.fetch_contacts()

            # Buscar contato específico
            contact = client.fetch_contacts(contact_id="5511999999999@s.whatsapp.net")
        """
        self._log(f"Buscando contatos{' (filtrado)' if contact_id else ''}")

        payload = {}
        if contact_id:
            payload["where"] = {"id": contact_id}

        result = self._make_request(
            "POST",
            "/chat/findContacts/{instanceId}",
            data=payload
        )

        # A API retorna uma lista diretamente, não um objeto com "data"
        if not isinstance(result, list):
            self._log(f"Formato inesperado de resposta: {type(result)}", "WARNING")
            return []

        return result

    def get_contact_name(self, number: str, use_cache: bool = True) -> str | None:
        """Busca o nome de um contato por número.

        Args:
            number: Número de telefone
            use_cache: Se deve usar cache de nomes (padrão: True)

        Returns:
            str | None: Nome do contato ou None se não encontrado

        Raises:
            EvolutionAPIError: Se houver erro
        """
        try:
            clean_number = self.validate_phone_number(number)

            # Verifica cache primeiro (se não expirou)
            if use_cache and not self._is_cache_expired() and clean_number in self._contact_names_cache:
                return self._contact_names_cache[clean_number]

            contact_id = f"{clean_number}@s.whatsapp.net"

            # Tenta buscar contato específico com filtro (retorna lista direta)
            contact_list = self.fetch_contacts(contact_id=contact_id)

            name = None
            if contact_list and len(contact_list) > 0:
                contact = contact_list[0]
                # Retorna pushName
                name = contact.get("pushName")
            else:
                # Algumas versões da API ignoram o filtro por id: usa o mapa em cache
                name = self._build_contacts_map().get(clean_number)

            # Salva no cache e atualiza timestamp
            if use_cache:
                self._contact_names_cache[clean_number] = name
                if not self._cache_timestamp:
                    self._cache_timestamp = datetime.now()

            return name

        except Exception as e:
            self._log(f"Erro ao buscar nome do contato: {e}", "WARNING")
            return None

    # =========================================================================
    # MESSAGE SENDING
    # =========================================================================

    def send_text(
        self,
        number: str,
        text: str,
        link_preview: bool = True
    ) -> dict[str, Any]:
        """Envia uma mensagem de texto.

        Endpoint: POST /message/sendText/{instanceId}

        Args:
            number: Número de telefone no formato internacional
            text: Texto da mensagem (máximo 65536 caracteres)
            link_preview: Se deve mostrar preview de links

        Returns:
            dict: Resposta da API

        Raises:
            InvalidPhoneNumberError: Se o número for inválido
            ValueError: Se o texto exceder o tamanho máximo
            EvolutionAPIError: Erros da API
        """
        # Validações
        clean_number = self.resolve_send_target(number)
        self.validate_text_length(text, MAX_TEXT_LENGTH, "text")

        self._log(f"Enviando mensagem de texto para {clean_number}")

        payload = {
            "number": clean_number,
            "text": text,
            "linkPreview": link_preview
        }

        return self._make_request(
            "POST",
            "/message/sendText/{instanceId}",
            data=payload
        )

    def send_media(
        self,
        number: str,
        media_url: str,
        media_type: str,
        caption: str | None = None,
        filename: str | None = None
    ) -> dict[str, Any]:
        """Envia mídia (imagem, vídeo, documento, áudio).

        Endpoint: POST /message/sendMedia/{instanceId}

        Args:
            number: Número de telefone no formato internacional
            media_url: URL da mídia a enviar
            media_type: Tipo de mídia (image, video, document, audio)
            caption: Legenda da mídia (opcional)
            filename: Nome do arquivo para documentos (opcional)

        Returns:
            dict: Resposta da API

        Raises:
            InvalidPhoneNumberError: Se o número for inválido
            ValueError: Se media_type, URL ou caption forem inválidos
            EvolutionAPIError: Erros da API
        """
        # Validações
        clean_number = self.resolve_send_target(number)
        self.validate_media_type(media_type)
        self.validate_url(media_url, "media_url")
        if caption:
            self.validate_text_length(caption, MAX_CAPTION_LENGTH, "caption")

        self._log(f"Enviando {media_type} para {clean_number}")

        payload = {
            "number": clean_number,
            "mediatype": media_type,
            "media": media_url
        }

        if caption:
            payload["caption"] = caption
        if filename:
            payload["fileName"] = filename

        return self._make_request(
            "POST",
            "/message/sendMedia/{instanceId}",
            data=payload
        )

    # =========================================================================
    # MEDIA (download para disco / envio de arquivos locais)
    # =========================================================================

    def get_media(self, message_id: str, convert_to_mp4: bool = False) -> dict[str, Any]:
        """Obtém a mídia de uma mensagem em base64 (resposta bruta da API).

        Endpoint: POST /chat/getBase64FromMediaMessage/{instanceId}

        Prefira `download_media`, que grava em disco e não devolve o base64.
        """
        if not message_id or not isinstance(message_id, str):
            raise ValueError("message_id não pode ser vazio")
        self._log(f"Baixando mídia da mensagem {message_id}")
        payload = {"message": {"key": {"id": message_id}}, "convertToMp4": convert_to_mp4}
        return self._make_request("POST", "/chat/getBase64FromMediaMessage/{instanceId}", data=payload)

    @staticmethod
    def _safe_filename(name: str) -> str:
        name = os.path.basename(name or "").strip()
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
        return name[:150] or "media"

    def _faxina(self) -> None:
        """Faxina da pasta de mídias, disparada por quem acabou de gravar.

        Fica aqui, e não num agendador, porque o servidor não tem um: quem grava é
        quem enche o disco, então é ele quem paga a conta. `sweep_if_due` desiste
        na hora se já varreu nas últimas 24h, e falha nunca atrapalha o envio.
        """
        try:
            sweep_if_due(self.media_dir, int(getattr(self.config, "media_ttl_days", 0) or 0))
        except Exception as e:
            self._log(f"faxina da pasta de mídias falhou: {e}", "WARN")

    def _unique_path(self, directory: Path, filename: str) -> Path:
        candidate = directory / filename
        if not candidate.exists():
            return candidate
        stem, suffix = candidate.stem, candidate.suffix
        for i in range(1, 1000):
            candidate = directory / f"{stem}_{i}{suffix}"
            if not candidate.exists():
                return candidate
        raise EvolutionAPIError(f"Não foi possível gerar nome único para {filename}")

    def download_media(
        self,
        message_id: str,
        save_dir: str | None = None,
        filename: str | None = None,
        extract_text: bool = False,
        max_chars: int = 3000,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Baixa a mídia de uma mensagem e grava em disco.

        O base64 nunca é devolvido ao chamador: só caminho e metadados. Isso
        reduz o custo de tokens de dezenas/centenas de milhares para ~50.

        Args:
            message_id: id da mensagem (campo `id` das mensagens compactas)
            save_dir: pasta de destino (padrão: EVOLUTION_MEDIA_DIR)
            filename: nome do arquivo (padrão: nome original ou id + extensão)
            extract_text: se True, extrai texto de PDF/texto puro (requer pypdf para PDF)
            max_chars: limite de caracteres do texto extraído
            password: senha de um PDF protegido. O arquivo é gravado **já destravado**,
                      para seguir acessível sem depender de lembrar a senha

        Returns:
            dict: {path, file, mime, size, type?, decrypted?, pages?, text?, text_truncated?, text_error?}
        """
        try:
            data = self.get_media(message_id)
        except EvolutionAPIError as e:
            # A Evolution API guarda algumas mensagens sem o campo `message`
            # (histórico sincronizado, mensagens efêmeras). Sem ele a mídia não
            # pode ser baixada, e a API devolve um TypeError pouco claro.
            if "ephemeralMessage" in str(e) or "properties of null" in str(e):
                raise EvolutionAPIError(
                    f"A mensagem {message_id} está salva sem conteúdo no banco da Evolution API, "
                    "então a mídia não pode mais ser baixada. Isso afeta uma fração das mensagens "
                    "antigas ou sincronizadas do histórico. Tente outra mensagem."
                ) from e
            raise
        b64 = data.get("base64") if isinstance(data, dict) else None
        if not b64:
            raise EvolutionAPIError(
                f"API não retornou mídia para a mensagem {message_id}. "
                "Verifique se o id é de uma mensagem com imagem/documento/áudio/vídeo."
            )
        if isinstance(b64, str) and b64.startswith("data:"):
            b64 = b64.split(",", 1)[-1]
        try:
            content = base64.b64decode(b64)
        except Exception as e:
            raise EvolutionAPIError(f"Base64 inválido retornado pela API: {e}")

        mime = (data.get("mimetype") or "application/octet-stream").split(";")[0].strip()
        original = data.get("fileName") or ""
        if not filename:
            filename = original
        if not filename:
            ext = _MIME_EXT.get(mime) or mimetypes.guess_extension(mime) or ""
            filename = f"{message_id}{ext}"
        elif not Path(filename).suffix:
            ext = _MIME_EXT.get(mime) or mimetypes.guess_extension(mime)
            if ext:
                filename = f"{filename}{ext}"

        directory = Path(os.path.expandvars(save_dir)).expanduser() if save_dir else self.media_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = self._unique_path(directory, self._safe_filename(filename))
        path.write_bytes(content)
        self._log(f"Mídia salva em {path} ({len(content)} bytes)")
        self._faxina()

        result: dict[str, Any] = {
            "path": str(path),
            "file": path.name,
            "mime": mime,
            "size": len(content),
        }
        if data.get("mediaType"):
            result["type"] = data["mediaType"]

        if password:
            if self._decrypt_pdf(path, password):
                result["decrypted"] = True
                content = path.read_bytes()
                result["size"] = len(content)
                self._log(f"PDF destravado: {path.name}")
            else:
                result["decrypted"] = False

        if extract_text:
            result.update(self._extract_text(path, mime, content, max_chars))
        return result

    @staticmethod
    def _decrypt_pdf(path: Path, password: str) -> bool:
        """Remove a senha de um PDF, gravando a versão destravada por cima.

        Returns:
            bool: True se destravou, False se o arquivo não estava protegido.

        Raises:
            ValueError: senha incorreta, ou pypdf ausente
        """
        if path.suffix.lower() != ".pdf":
            return False
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            raise ValueError('pypdf não instalado (pip install -e ".[pdf]")')

        try:
            reader = PdfReader(str(path))
            if not reader.is_encrypted:
                return False
            if not reader.decrypt(password):
                raise ValueError(
                    "Senha incorreta para este PDF. Boletos costumam pedir os primeiros "
                    "dígitos do CPF do pagador."
                )
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            temp = path.with_name(path.name + ".tmp")
            with temp.open("wb") as fh:
                writer.write(fh)
            temp.replace(path)
            return True
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Falha ao destravar o PDF: {e}")

    def _extract_text(self, path: Path, mime: str, content: bytes, max_chars: int) -> dict[str, Any]:
        """Extrai texto de PDF, texto puro ou áudio (transcrição). Nunca lança exceção."""
        text: str | None = None
        pages: int | None = None
        if is_transcribable(mime, path):
            try:
                result = self.transcriber.transcribe(path)
            except TranscriptionError as e:
                return {"text_error": str(e)}
            out = {k: v for k, v in result.items() if k != "text"}
            transcript, cut = truncate(result.get("text") or "", max_chars)
            if not transcript:
                out["text_error"] = "nenhuma fala reconhecida no áudio"
                return out
            out["text"] = transcript
            if cut:
                out["text_truncated"] = True
            return out
        try:
            if mime == "application/pdf" or path.suffix.lower() == ".pdf":
                try:
                    from pypdf import PdfReader  # dependência opcional
                except ImportError:
                    return {"text_error": "pypdf não instalado (pip install pypdf)"}
                reader = PdfReader(str(path))
                if reader.is_encrypted:
                    return {"text_error": "PDF protegido por senha. Repita a chamada informando password."}
                pages = len(reader.pages)
                parts: list[str] = []
                total = 0
                for page in reader.pages:
                    chunk = page.extract_text() or ""
                    parts.append(chunk)
                    total += len(chunk)
                    if max_chars and total >= max_chars:
                        break
                text = "\n".join(parts)
            elif mime.startswith("text/") or path.suffix.lower() in (".txt", ".csv", ".md", ".json", ".xml"):
                text = content.decode("utf-8", errors="replace")
            else:
                return {"text_error": f"extração de texto não suportada para {mime}"}
        except Exception as e:
            return {"text_error": f"falha ao extrair texto: {e}"}

        text = re.sub(r"[ \t]+", " ", text or "").strip()
        out: dict[str, Any] = {}
        if pages:
            out["pages"] = pages
        if not text:
            out["text_error"] = "nenhum texto extraível (PDF escaneado? use OCR)"
            return out
        if max_chars and len(text) > max_chars:
            out["text"] = text[:max_chars].rstrip() + "…"
            out["text_truncated"] = True
        else:
            out["text"] = text
        return out

    def send_media_base64(
        self,
        number: str,
        base64_data: str,
        media_type: str,
        file_name: str | None = None,
        caption: str | None = None,
        mimetype: str | None = None,
    ) -> dict[str, Any]:
        """Envia mídia a partir de conteúdo base64 (sem URL pública).

        Endpoint: POST /message/sendMedia/{instanceId}
                  POST /message/sendWhatsAppAudio/{instanceId} (áudio)
        """
        clean_number = self.resolve_send_target(number)
        self.validate_media_type(media_type)
        if not base64_data or not isinstance(base64_data, str):
            raise ValueError("base64_data não pode ser vazio")
        if base64_data.startswith("data:"):
            base64_data = base64_data.split(",", 1)[-1]
        if caption:
            self.validate_text_length(caption, MAX_CAPTION_LENGTH, "caption")

        self._log(f"Enviando {media_type} (base64, {len(base64_data)} chars) para {clean_number}")

        if media_type == "audio":
            return self._make_request(
                "POST", "/message/sendWhatsAppAudio/{instanceId}",
                data={"number": clean_number, "audio": base64_data},
            )

        payload: dict[str, Any] = {
            "number": clean_number,
            "mediatype": media_type,
            "media": base64_data,
        }
        if mimetype:
            payload["mimetype"] = mimetype
        if caption:
            payload["caption"] = caption
        if file_name:
            payload["fileName"] = file_name
        elif media_type == "document":
            payload["fileName"] = "documento"
        return self._make_request("POST", "/message/sendMedia/{instanceId}", data=payload)

    def send_file(
        self,
        number: str,
        file_path: str,
        caption: str | None = None,
        media_type: str | None = None,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        """Envia um arquivo local. O base64 é gerado aqui, sem passar pelo LLM.

        Args:
            number: Número de destino
            file_path: Caminho do arquivo no disco
            caption: Legenda opcional
            media_type: image/video/audio/document (padrão: deduzido da extensão;
                        qualquer extensão desconhecida vira document)
            file_name: Nome exibido no WhatsApp (padrão: nome do arquivo)
        """
        path = Path(os.path.expandvars(file_path)).expanduser()
        if not path.is_file():
            raise ValueError(f"Arquivo não encontrado: {file_path}")
        content = path.read_bytes()
        if not content:
            raise ValueError(f"Arquivo vazio: {file_path}")
        media_type = media_type or _EXT_MEDIA_TYPE.get(path.suffix.lower(), "document")
        mime = mimetypes.guess_type(path.name)[0]
        result = self.send_media_base64(
            number=number,
            base64_data=base64.b64encode(content).decode("ascii"),
            media_type=media_type,
            file_name=file_name or path.name,
            caption=caption,
            mimetype=mime,
        )
        if isinstance(result, dict):
            result.setdefault("_file", {"path": str(path), "size": len(content), "type": media_type})
        return result

    def send_render(
        self,
        number: str,
        svg: str,
        caption: str | None = None,
        file_name: str | None = None,
        width: int | None = None,
        height: int | None = None,
        background: str | None = "white",
    ) -> dict[str, Any]:
        """Rasteriza um SVG aqui e envia o PNG resultante.

        É o caminho barato para uma imagem criada pelo próprio modelo: o desenho
        atravessa a conversa como texto (um gráfico dá 2 a 5 KB) e vira pixel só
        no servidor. O mesmo desenho em base64 custaria dezenas de milhares de
        tokens, porque é o conteúdo do arquivo que pesa, não o envio.

        O PNG fica gravado em `<media_dir>/render/`, então dá para reenviar ou
        arquivar depois pelo `path` devolvido, sem desenhar de novo.

        Args:
            number: Número de destino
            svg: O documento SVG
            caption: Legenda opcional
            file_name: Nome exibido no WhatsApp (padrão: imagem.png)
            width: Largura em pixels (padrão: a do próprio SVG)
            height: Altura em pixels (padrão: proporcional à largura)
            background: Cor de fundo; None mantém a transparência

        Raises:
            RenderError: SVG inválido, grande demais ou dependência ausente
        """
        png = render_svg(svg, width=width, height=height, background=background)

        nome = self._safe_filename(file_name or "imagem.png")
        if not nome.lower().endswith(".png"):
            nome = f"{Path(nome).stem or 'imagem'}.png"
        # Subpasta própria: o que o modelo desenhou não se mistura com o que veio
        # do WhatsApp, e limpar um não apaga o outro.
        directory = self.media_dir / "render"
        directory.mkdir(parents=True, exist_ok=True)
        path = self._unique_path(directory, nome)
        path.write_bytes(png)
        self._log(f"SVG rasterizado em {path} ({len(png)} bytes)")
        self._faxina()

        return self.send_file(
            number=number,
            file_path=str(path),
            caption=caption,
            media_type="image",
            file_name=nome,
        )

    def send_url(
        self,
        number: str,
        url: str,
        caption: str | None = None,
        media_type: str | None = None,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        """Baixa o arquivo da URL aqui e o envia.

        Atalho para o que já existe na web: pela conversa passa só o link. A
        diferença para `send_media(media_url=...)`, em que a própria Evolution
        baixa, é que aqui o download é do servidor — dá para converter link de
        compartilhamento, deduzir o tipo pelo Content-Type e dizer o que houve
        quando o link não serve, em vez de uma falha opaca lá na Evolution.

        Args:
            number: Número de destino
            url: Endereço público do arquivo
            caption: Legenda opcional
            media_type: image/video/audio/document (padrão: deduzido do Content-Type)
            file_name: Nome exibido no WhatsApp (padrão: o nome que veio no link)

        Raises:
            WebLinkError: endereço recusado, erro HTTP ou arquivo grande demais
        """
        baixado = fetch_url(url, timeout=self.timeout)
        mime = baixado["mime"]
        nome = self._safe_filename(file_name or baixado["file_name"])

        # Muito servidor devolve octet-stream para qualquer arquivo. Com isso o
        # WhatsApp não sabe o que exibir, e a extensão do nome diz mais.
        if mime in ("", "application/octet-stream", "binary/octet-stream"):
            mime = mimetypes.guess_type(nome)[0] or mime

        if not media_type:
            raiz = mime.split("/")[0]
            media_type = (
                raiz if raiz in ("image", "video", "audio")
                else _EXT_MEDIA_TYPE.get(Path(nome).suffix.lower(), "document")
            )

        # Guardado em disco como o resto: reenviar ou arquivar depois não baixa de novo.
        directory = self.media_dir / "web"
        directory.mkdir(parents=True, exist_ok=True)
        path = self._unique_path(directory, nome)
        path.write_bytes(baixado["content"])
        self._log(f"Arquivo de {baixado['url']} salvo em {path} ({len(baixado['content'])} bytes)")
        self._faxina()

        result = self.send_media_base64(
            number=number,
            base64_data=base64.b64encode(baixado["content"]).decode("ascii"),
            media_type=media_type,
            file_name=nome,
            caption=caption,
            mimetype=mime or mimetypes.guess_type(nome)[0],
        )
        if isinstance(result, dict):
            result.setdefault("_file", {
                "path": str(path),
                "size": len(baixado["content"]),
                "type": media_type,
                "url": baixado["url"],
            })
        return result

    def send_drive_file(
        self,
        number: str,
        file_ref: str | None = None,
        folder: str | None = None,
        name: str | None = None,
        caption: str | None = None,
        media_type: str | None = None,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        """Reenvia pelo WhatsApp um arquivo que este servidor arquivou no Drive.

        Fecha o ciclo do `archive_to_drive`, que até aqui era mão única: o boleto
        arquivado em agosto voltava a ser alcançável só enquanto a cópia local
        existisse. Nada disso passa pela conversa — o arquivo vai do Drive para o
        servidor e do servidor para o WhatsApp.

        Args:
            number: Número de destino
            file_ref: id do arquivo ou o link devolvido por archive_to_drive
            folder: caminho da pasta, quando for procurar por nome (ex: "MR/2026/08.2026/BOLETO")
            name: nome do arquivo dentro dessa pasta
            caption: Legenda opcional
            media_type: image/video/audio/document (padrão: deduzido do tipo no Drive)
            file_name: Nome exibido no WhatsApp (padrão: o nome no Drive)

        Raises:
            DriveError: Drive não configurado, arquivo não encontrado ou grande demais
        """
        if file_ref:
            file_id = self.drive.file_id_from(file_ref)
        elif name:
            file_id = self.drive.find_in_folder(folder or "", name)
        else:
            raise DriveError("Informe file_ref (id ou link) ou name (com folder).")

        baixado = self.drive.download_file(file_id)
        mime = baixado["mime"]
        nome = self._safe_filename(file_name or baixado["name"])

        if not media_type:
            raiz = mime.split("/")[0]
            media_type = (
                raiz if raiz in ("image", "video", "audio")
                else _EXT_MEDIA_TYPE.get(Path(nome).suffix.lower(), "document")
            )

        directory = self.media_dir / "drive"
        directory.mkdir(parents=True, exist_ok=True)
        path = self._unique_path(directory, nome)
        path.write_bytes(baixado["content"])
        self._log(f"Arquivo {baixado['name']} trazido do Drive para {path}")
        self._faxina()

        result = self.send_media_base64(
            number=number,
            base64_data=base64.b64encode(baixado["content"]).decode("ascii"),
            media_type=media_type,
            file_name=nome,
            caption=caption,
            mimetype=mime or mimetypes.guess_type(nome)[0],
        )
        if isinstance(result, dict):
            result.setdefault("_file", {
                "path": str(path),
                "size": baixado["size"],
                "type": media_type,
                "drive_id": file_id,
                "link": baixado.get("link"),
            })
        return result

    # =========================================================================
    # DOCUMENTO COMO IMAGEM (para o modelo ler o que está escrito)
    # =========================================================================

    def render_media(
        self,
        message_id: str,
        first_page: int = 1,
        pages: int = 1,
        max_side: int = 1568,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Baixa o anexo e devolve as páginas como imagens JPEG.

        Serve para comprovante fotografado e PDF escaneado, que não têm camada de
        texto. As imagens voltam em bytes, para o servidor MCP entregá-las pelo
        protocolo como imagem de verdade.

        Returns:
            dict: {images, format, pages_rendered, file, mime, size, path}
        """
        baixado = self.download_media(message_id, password=password)
        if not is_renderable(baixado.get("mime"), Path(baixado["path"])):
            raise RenderError(
                f"A mensagem {message_id} não é imagem nem PDF (mime: {baixado.get('mime')})."
            )
        resultado = render(
            baixado["path"],
            mime=baixado.get("mime"),
            first_page=first_page,
            pages=pages,
            max_side=max_side,
            password=None if baixado.get("decrypted") else password,
        )
        resultado.update({
            "file": baixado["file"],
            "mime": baixado.get("mime"),
            "size": baixado.get("size"),
            "path": baixado["path"],
        })
        return resultado

    # =========================================================================
    # TRANSCRIÇÃO DE ÁUDIOS
    # =========================================================================

    def _transcript_cache_path(self, message_id: str) -> Path:
        return self.media_dir / ".transcripts" / f"{self._safe_filename(message_id)}.json"

    def _cached_audio_path(self, message_id: str) -> Path | None:
        """Procura um áudio já baixado para esta mensagem, evitando novo download."""
        if not self.media_dir.is_dir():
            return None
        safe = self._safe_filename(message_id)
        for candidate in sorted(self.media_dir.glob(f"{safe}.*")):
            if candidate.is_file() and is_transcribable(None, candidate):
                return candidate
        return None

    def transcribe_message(
        self,
        message_id: str,
        language: str | None = None,
        max_chars: int = 4000,
        force: bool = False,
    ) -> dict[str, Any]:
        """Baixa (se preciso) e transcreve o áudio de uma mensagem.

        O resultado é gravado em `<media_dir>/.transcripts/<id>.json` e reusado
        nas próximas chamadas, então repetir a pergunta não paga transcrição de novo.

        Args:
            message_id: id da mensagem de áudio (campo `id` das mensagens compactas)
            language: código ISO do idioma (ex: 'pt'); padrão: configuração/detecção
            max_chars: corte do texto devolvido (0 = sem corte)
            force: True refaz a transcrição ignorando o cache

        Returns:
            dict: {text, backend, model, language?, seconds?, path, cached?, truncated?}

        Raises:
            TranscriptionError: Sem backend configurado ou falha ao transcrever
            EvolutionAPIError: Falha ao baixar a mídia
        """
        if not message_id or not isinstance(message_id, str):
            raise ValueError("message_id não pode ser vazio")

        cache_path = self._transcript_cache_path(message_id)
        result: dict[str, Any] | None = None

        if not force and cache_path.is_file():
            try:
                result = json.loads(cache_path.read_text(encoding="utf-8"))
                result["cached"] = True
                self._log(f"Transcrição em cache para {message_id}")
            except (ValueError, OSError):
                result = None

        if result is None:
            path = self._cached_audio_path(message_id)
            if path is None:
                downloaded = self.download_media(message_id, filename=message_id)
                path = Path(downloaded["path"])
                if not is_transcribable(downloaded.get("mime"), path):
                    raise TranscriptionError(
                        f"A mensagem {message_id} não é áudio nem vídeo "
                        f"(mime: {downloaded.get('mime')})."
                    )
            result = self.transcriber.transcribe(path, language)
            result["path"] = str(path)
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            except OSError as e:
                self._log(f"Não foi possível gravar o cache de transcrição: {e}", "WARNING")

        text, cut = truncate(result.get("text") or "", max_chars)
        out = dict(result)
        out["text"] = text
        if cut:
            out["truncated"] = True
        if not text:
            out["warning"] = "nenhuma fala reconhecida no áudio"
        return out

    def transcribe_file(
        self,
        file_path: str,
        language: str | None = None,
        max_chars: int = 4000,
    ) -> dict[str, Any]:
        """Transcreve um arquivo de áudio/vídeo que já está no disco."""
        path = Path(os.path.expandvars(file_path)).expanduser()
        if not path.is_file():
            raise ValueError(f"Arquivo não encontrado: {file_path}")
        result = self.transcriber.transcribe(path, language)
        result["path"] = str(path)
        text, cut = truncate(result.get("text") or "", max_chars)
        result["text"] = text
        if cut:
            result["truncated"] = True
        return result

    # =========================================================================
    # ARQUIVAMENTO NO GOOGLE DRIVE
    # =========================================================================

    def archive_media(
        self,
        message_id: str,
        folder: str,
        filename: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Baixa o anexo de uma mensagem e envia ao Drive, sem passar pela conversa.

        Args:
            message_id: id da mensagem com o anexo
            folder: caminho da pasta, relativo a EVOLUTION_DRIVE_ROOT
                    (ex: "MR/2026/08.2026/BOLETO")
            filename: nome final do arquivo (padrão: o nome original)
            password: senha de um PDF protegido; a versão arquivada vai destravada

        Returns:
            dict: {id, name, folder, size, link, decrypted?, source}
        """
        if not self.drive.available:
            raise DriveError(self.drive.describe()["hint"])

        baixado = self.download_media(message_id, filename=filename, password=password)
        enviado = self.drive.upload_file(
            baixado["path"],
            name=filename or baixado["file"],
            folder=folder,
            mime=baixado.get("mime"),
        )
        if baixado.get("decrypted"):
            enviado["decrypted"] = True
        enviado["source"] = {"message_id": message_id, "path": baixado["path"]}
        return enviado

    def archive_file(
        self,
        file_path: str,
        folder: str,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Envia ao Drive um arquivo que já está no disco do servidor."""
        if not self.drive.available:
            raise DriveError(self.drive.describe()["hint"])
        path = Path(os.path.expandvars(file_path)).expanduser()
        return self.drive.upload_file(path, name=filename, folder=folder)

    # =========================================================================
    # INSTANCE OPERATIONS
    # =========================================================================

    def get_connection_state(self) -> dict[str, Any]:
        """Obtém o estado da conexão da instância.

        Endpoint: GET /instance/connectionState/{instanceId}

        Returns:
            dict: Estado da conexão

        Raises:
            EvolutionAPIError: Se houver erro ao consultar o estado
        """
        self._log("Consultando estado da conexão")

        response = self._make_request(
            "GET",
            "/instance/connectionState/{instanceId}"
        )

        state = response.get('state', 'unknown')
        self._log(f"Estado da conexão: {state}")

        return response

    def set_presence(
        self,
        status: str,
        number: str | None = None
    ) -> dict[str, Any]:
        """Define presença.

        Endpoint: POST /chat/presenceUpdate/{instanceId}

        Args:
            status: Status (available, unavailable, composing, recording)
            number: Número para enviar presença (opcional)

        Returns:
            dict: Resposta

        Raises:
            EvolutionAPIError: Se houver erro
        """
        self._log(f"Definindo presença como '{status}'")

        payload = {
            "presence": status
        }

        if number:
            payload["number"] = self.resolve_send_target(number)

        return self._make_request(
            "POST",
            "/chat/presenceUpdate/{instanceId}",
            data=payload
        )

    def get_instance_info(self) -> dict[str, Any]:
        """Obtém informações detalhadas da instância.

        Returns:
            dict: Informações da instância

        Raises:
            EvolutionAPIError: Se houver erro ao consultar
        """
        self._log("Consultando informações da instância")

        # Usa get_connection_state que retorna info da instância
        response = self.get_connection_state()

        # A v2 aninha o estado em {"instance": {"state": ...}}; a v1 devolve no topo
        inner = response.get("instance") if isinstance(response, dict) else None
        state = (inner or {}).get("state") if isinstance(inner, dict) else None

        return {
            "instance_name": self.instance_id,
            "status": state or response.get("state", "unknown"),
            "info": response
        }
