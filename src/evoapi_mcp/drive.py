"""Upload para o Google Drive feito pelo próprio servidor.

O conector de Drive do Claude só aceita o conteúdo embutido na chamada
(`base64Content`), então arquivar um documento custava o arquivo inteiro em
base64 dentro da conversa: dezenas de milhares de tokens para um boleto e
centenas de milhares para um PDF grande, que simplesmente não cabia.

Aqui o arquivo vai do WhatsApp para o disco do servidor e do servidor para o
Drive, sem passar pela conversa. O que volta para o modelo é o link.

Autenticação por refresh token OAuth da conta do próprio usuário, para que os
arquivos continuem pertencendo a ele (uma conta de serviço seria dona deles).
Configure com:

    EVOLUTION_DRIVE_CLIENT_ID
    EVOLUTION_DRIVE_CLIENT_SECRET
    EVOLUTION_DRIVE_REFRESH_TOKEN
    EVOLUTION_DRIVE_ROOT          (opcional, ex: "Claude/FINANCEIRO")

O refresh token sai do script `scripts/google_oauth_setup.py`.
"""

from __future__ import annotations

import json
import mimetypes
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"

# `drive.file` dá acesso apenas aos arquivos que o próprio app criou. É o escopo
# certo aqui por um motivo prático: `drive` (acesso total) é um escopo RESTRITO, e
# com ele o app só sai do modo de testes passando pela verificação de segurança do
# Google. Em modo de testes o refresh token expira em 7 dias, o que quebraria o
# servidor toda semana. `drive.file` não é restrito: publica na hora e o token dura.
#
# A consequência é que o app não enxerga pastas criadas por outros aplicativos.
# Por isso a árvore de arquivamento fica sob uma pasta que ele mesmo cria, e cujo id
# vai em EVOLUTION_DRIVE_ROOT_ID.
SCOPE = "https://www.googleapis.com/auth/drive.file"

# Limite do upload simples da API; acima disso seria preciso upload resumível
MAX_SIMPLE_UPLOAD = 5 * 1024 * 1024

# Teto do download. O WhatsApp recusa mídia muito maior, e sem teto um arquivo
# grande no Drive viraria memória e disco do servidor de uma vez.
MAX_DOWNLOAD = 16 * 1024 * 1024

# Id do arquivo dentro de um link do Drive (o que archive_to_drive devolve em `link`)
_LINK_ID = re.compile(r"/(?:file|d)/d/([A-Za-z0-9_-]+)|[?&]id=([A-Za-z0-9_-]+)")


class DriveError(Exception):
    """Erro ao falar com o Google Drive."""


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Drive: {message}", file=sys.stderr)


class DriveClient:
    """Cliente mínimo do Drive v3, sem as bibliotecas do Google."""

    def __init__(self, config: Any):
        self.client_id = (getattr(config, "drive_client_id", "") or "").strip()
        self.client_secret = (getattr(config, "drive_client_secret", "") or "").strip()
        self.refresh_token = (getattr(config, "drive_refresh_token", "") or "").strip()
        self.root = (getattr(config, "drive_root", "") or "").strip("/")
        self.root_id = (getattr(config, "drive_root_id", "") or "").strip()
        self.timeout = int(getattr(config, "timeout", 30))
        self._token: str | None = None
        self._expires: datetime | None = None
        self._folder_cache: dict[str, str] = {}

    # ------------------------------------------------------------------ estado

    @property
    def available(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {"available": self.available, "scope": SCOPE}
        if self.root:
            info["root"] = self.root
        if self.root_id:
            info["root_id"] = self.root_id
        if not self.available:
            info["hint"] = (
                "Drive não configurado. Defina EVOLUTION_DRIVE_CLIENT_ID, "
                "EVOLUTION_DRIVE_CLIENT_SECRET e EVOLUTION_DRIVE_REFRESH_TOKEN "
                "(use scripts/google_oauth_setup.py para obter o refresh token)."
            )
        return info

    def _require(self) -> None:
        if not self.available:
            raise DriveError(self.describe()["hint"])

    # ------------------------------------------------------------------ token

    def _access_token(self) -> str:
        """Troca o refresh token por um access token, reaproveitando enquanto vale."""
        if self._token and self._expires and datetime.now() < self._expires:
            return self._token

        self._require()
        try:
            r = requests.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as e:
            raise DriveError(f"Falha de conexão ao renovar o token: {e}")

        if r.status_code >= 300:
            detalhe = ""
            try:
                detalhe = r.json().get("error_description") or r.json().get("error") or ""
            except ValueError:
                detalhe = r.text[:200]
            raise DriveError(
                f"Não foi possível renovar o acesso ao Drive (HTTP {r.status_code}): {detalhe}. "
                "Se o refresh token foi revogado, gere outro com scripts/google_oauth_setup.py."
            )

        payload = r.json()
        self._token = payload["access_token"]
        # margem de 60s para não usar um token que expira no meio da requisição
        self._expires = datetime.now() + timedelta(seconds=int(payload.get("expires_in", 3600)) - 60)
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def _check(self, r: requests.Response, acao: str) -> Any:
        if r.status_code >= 300:
            detalhe = r.text[:300]
            try:
                detalhe = r.json().get("error", {}).get("message", detalhe)
            except ValueError:
                pass
            raise DriveError(f"{acao} falhou (HTTP {r.status_code}): {detalhe}")
        try:
            return r.json()
        except ValueError:
            return {}

    # ----------------------------------------------------------------- pastas

    @staticmethod
    def _escape(name: str) -> str:
        return name.replace("\\", "\\\\").replace("'", "\\'")

    def find_child(self, name: str, parent_id: str, folder_only: bool = False) -> str | None:
        """Id de um filho direto pelo nome, ou None."""
        q = f"name = '{self._escape(name)}' and '{parent_id}' in parents and trashed = false"
        if folder_only:
            q += f" and mimeType = '{FOLDER_MIME}'"
        r = requests.get(
            FILES_URL,
            headers=self._headers(),
            params={"q": q, "fields": "files(id,name)", "pageSize": 10,
                    "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"},
            timeout=self.timeout,
        )
        files = self._check(r, f"Busca por '{name}'").get("files") or []
        return files[0]["id"] if files else None

    def create_folder(self, name: str, parent_id: str) -> str:
        r = requests.post(
            FILES_URL,
            headers={**self._headers(), "Content-Type": "application/json"},
            params={"fields": "id", "supportsAllDrives": "true"},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            timeout=self.timeout,
        )
        folder_id = self._check(r, f"Criação da pasta '{name}'").get("id")
        _log(f"pasta criada: {name}")
        return folder_id

    def ensure_folder(self, path: str, create: bool = True) -> str:
        """Resolve (criando o que faltar) um caminho tipo 'MR/2026/08.2026/BOLETO'.

        O caminho é relativo a EVOLUTION_DRIVE_ROOT, quando definido.
        """
        self._require()
        partes = [p for p in f"{self.root}/{path}".split("/") if p.strip()]
        base = self.root_id or "root"
        chave = f"{base}:" + "/".join(partes)
        if chave in self._folder_cache:
            return self._folder_cache[chave]
        if not partes:
            return base

        parent = base
        andado: list[str] = []
        for parte in partes:
            andado.append(parte)
            cache_key = f"{base}:" + "/".join(andado)
            if cache_key in self._folder_cache:
                parent = self._folder_cache[cache_key]
                continue
            achado = self.find_child(parte, parent, folder_only=True)
            if achado is None:
                if not create:
                    raise DriveError(f"Pasta não encontrada no Drive: {cache_key}")
                achado = self.create_folder(parte, parent)
            self._folder_cache[cache_key] = achado
            parent = achado
        return parent

    # ---------------------------------------------------------------- upload

    def upload_file(
        self,
        file_path: str | Path,
        name: str | None = None,
        folder: str | None = None,
        mime: str | None = None,
    ) -> dict[str, Any]:
        """Sobe um arquivo do disco do servidor para o Drive.

        Returns:
            dict: {id, name, folder, size, link}
        """
        self._require()
        path = Path(file_path)
        if not path.is_file():
            raise DriveError(f"Arquivo não encontrado no servidor: {file_path}")

        conteudo = path.read_bytes()
        if len(conteudo) > MAX_SIMPLE_UPLOAD:
            raise DriveError(
                f"Arquivo de {len(conteudo) / 1048576:.1f} MB excede o limite de "
                f"{MAX_SIMPLE_UPLOAD // 1048576} MB deste upload."
            )

        name = name or path.name
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        parent = self.ensure_folder(folder or "")

        meta = {"name": name, "parents": [parent]}
        r = requests.post(
            UPLOAD_URL,
            headers=self._headers(),
            params={"uploadType": "multipart", "fields": "id,name,webViewLink,size",
                    "supportsAllDrives": "true"},
            files={
                "metadata": ("metadata", json.dumps(meta), "application/json"),
                "file": (name, conteudo, mime),
            },
            timeout=max(self.timeout, 120),
        )
        d = self._check(r, f"Upload de '{name}'")
        _log(f"enviado ao Drive: {name} ({len(conteudo)} bytes)")
        return {
            "id": d.get("id"),
            "name": d.get("name", name),
            "folder": f"{self.root}/{folder}".strip("/") if folder else self.root or "root",
            "size": len(conteudo),
            "link": d.get("webViewLink"),
        }

    # -------------------------------------------------------------- download

    @staticmethod
    def file_id_from(ref: str) -> str:
        """Aceita tanto o id cru quanto o link que `archive_to_drive` devolveu."""
        ref = (ref or "").strip()
        if not ref:
            raise DriveError("Informe o id ou o link do arquivo no Drive.")
        if "://" not in ref:
            return ref
        achado = _LINK_ID.search(ref)
        if not achado:
            raise DriveError(f"Não achei o id do arquivo neste link: {ref}")
        return achado.group(1) or achado.group(2)

    def get_metadata(self, file_id: str) -> dict[str, Any]:
        """Nome, tipo e tamanho de um arquivo, sem baixá-lo."""
        self._require()
        r = requests.get(
            f"{FILES_URL}/{file_id}",
            headers=self._headers(),
            params={"fields": "id,name,mimeType,size,webViewLink", "supportsAllDrives": "true"},
            timeout=self.timeout,
        )
        return self._check(r, f"Consulta do arquivo {file_id}")

    def find_in_folder(self, folder: str, name: str) -> str:
        """Id de um arquivo pelo caminho da pasta e pelo nome, sem precisar guardar id."""
        parent = self.ensure_folder(folder or "", create=False)
        achado = self.find_child(name, parent)
        if not achado:
            raise DriveError(f"Arquivo '{name}' não encontrado em '{folder or self.root or 'raiz'}'.")
        return achado

    def download_file(self, file_id: str, max_bytes: int = MAX_DOWNLOAD) -> dict[str, Any]:
        """Baixa para a memória um arquivo que este app criou.

        O escopo `drive.file` é de leitura E escrita sobre o que o próprio app criou,
        então isto alcança tudo que saiu de `upload_file` — sem escopo restrito, sem
        verificação do Google e sem precisar tornar nada público.

        Returns:
            dict: {content: bytes, name, mime, size, id, link}
        """
        self._require()
        meta = self.get_metadata(file_id)

        declarado = int(meta.get("size") or 0)
        if declarado > max_bytes:
            raise DriveError(
                f"'{meta.get('name')}' tem {declarado / 1048576:.1f} MB, acima do limite de "
                f"{max_bytes // 1048576} MB."
            )

        r = requests.get(
            f"{FILES_URL}/{file_id}",
            headers=self._headers(),
            params={"alt": "media", "supportsAllDrives": "true"},
            stream=True,
            timeout=max(self.timeout, 120),
        )
        if r.status_code >= 300:
            r.close()
            self._check(r, f"Download de {file_id}")

        conteudo = bytearray()
        try:
            for pedaco in r.iter_content(chunk_size=64 * 1024):
                conteudo.extend(pedaco)
                if len(conteudo) > max_bytes:
                    raise DriveError(f"O download passou do limite de {max_bytes} bytes.")
        finally:
            r.close()

        if not conteudo:
            raise DriveError(f"O arquivo {file_id} veio vazio do Drive.")

        _log(f"baixado do Drive: {meta.get('name')} ({len(conteudo)} bytes)")
        return {
            "content": bytes(conteudo),
            "name": meta.get("name") or file_id,
            "mime": meta.get("mimeType") or "",
            "size": len(conteudo),
            "id": file_id,
            "link": meta.get("webViewLink"),
        }
