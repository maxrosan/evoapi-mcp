#!/usr/bin/env python3
"""Obtém o refresh token do Google Drive para o servidor, sem expor o segredo.

O token e o client secret **não são impressos na tela**: vão direto para um arquivo
local, que você abre e copia para as variáveis de ambiente do servidor. Assim o
segredo não passa por logs nem por conversa nenhuma.

Antes de rodar, no Google Cloud Console:

  1. Crie (ou escolha) um projeto e habilite a **Google Drive API**.
  2. Em "APIs e serviços > Tela de permissão OAuth", configure o app como **Externo**
     e **publique em produção**. Em modo de testes o refresh token expira em 7 dias.
  3. Em "Credenciais", crie um **ID do cliente OAuth** do tipo **App para computador**.
  4. Baixe o JSON do cliente, ou tenha em mãos o client ID e o client secret.

Uso:

    python scripts/google_oauth_setup.py --client-json caminho/client_secret.json
    python scripts/google_oauth_setup.py --client-id XXX --client-secret YYY

O navegador abre, você escolhe a conta do Drive e autoriza. O resultado fica em
`google_drive_env.txt`, no formato pronto para colar no Easypanel.
"""

from __future__ import annotations

import argparse
import getpass
import http.server
import json
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.file"

_resultado: dict[str, str] = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _resultado.update({k: v[0] for k, v in params.items()})
        corpo = (
            "<html><body style='font-family:sans-serif;padding:3rem'>"
            "<h2>Pode fechar esta aba.</h2>"
            "<p>A autorização voltou para o script.</p></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    ap = argparse.ArgumentParser(description="Gera o refresh token do Google Drive.")
    ap.add_argument("--client-json", help="JSON do cliente OAuth baixado do console")
    ap.add_argument("--client-id", help="Client ID (alternativa ao --client-json)")
    ap.add_argument("--client-secret", help="Client secret; se omitido, é pedido sem eco")
    ap.add_argument("--out", default="google_drive_env.txt", help="Arquivo de saída")
    args = ap.parse_args()

    if args.client_json:
        dados = json.loads(Path(args.client_json).read_text(encoding="utf-8"))
        bloco = dados.get("installed") or dados.get("web") or dados
        client_id = bloco["client_id"]
        client_secret = bloco["client_secret"]
    elif args.client_id:
        client_id = args.client_id
        client_secret = args.client_secret or getpass.getpass("Client secret (não aparece na tela): ")
    else:
        ap.error("informe --client-json ou --client-id")
        return 2

    porta = porta_livre()
    redirect = f"http://localhost:{porta}/"
    estado = secrets.token_urlsafe(16)

    servidor = http.server.HTTPServer(("127.0.0.1", porta), Handler)
    threading.Thread(target=servidor.handle_request, daemon=True).start()

    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": estado,
    })

    print("Abrindo o navegador para autorizar.")
    print("Escolha a conta dona do Drive onde os arquivos devem ficar.")
    print(f"\nSe não abrir sozinho, acesse:\n{url}\n")
    webbrowser.open(url)

    servidor.socket.settimeout(300)
    for _ in range(300):
        if _resultado:
            break
        threading.Event().wait(1)

    if not _resultado:
        print("Tempo esgotado sem resposta do navegador.", file=sys.stderr)
        return 1
    if _resultado.get("state") != estado:
        print("Estado não confere; possível interferência. Abortando.", file=sys.stderr)
        return 1
    if "error" in _resultado:
        print(f"Autorização negada: {_resultado['error']}", file=sys.stderr)
        return 1

    r = requests.post(TOKEN_URL, data={
        "code": _resultado["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }, timeout=60)

    if r.status_code >= 300:
        print(f"Troca do código falhou (HTTP {r.status_code}): {r.text[:300]}", file=sys.stderr)
        return 1

    payload = r.json()
    refresh = payload.get("refresh_token")
    if not refresh:
        print(
            "O Google não devolveu refresh token. Isso acontece quando a conta já tinha "
            "autorizado este app antes. Remova o acesso em "
            "https://myaccount.google.com/permissions e rode de novo.",
            file=sys.stderr,
        )
        return 1

    # cria a pasta base já com o escopo drive.file, para ter o id dela
    root_id = ""
    nome_raiz = "FINANCEIRO"
    try:
        cr = requests.post(
            "https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": f"Bearer {payload['access_token']}",
                     "Content-Type": "application/json"},
            params={"fields": "id"},
            json={"name": nome_raiz, "mimeType": "application/vnd.google-apps.folder"},
            timeout=60,
        )
        if cr.status_code < 300:
            root_id = cr.json().get("id", "")
    except requests.exceptions.RequestException:
        pass

    destino = Path(args.out).resolve()
    linhas = [
        "# Cole estas linhas nas variáveis de ambiente do serviço (Easypanel).",
        "# Trate como senha: quem tiver isto escreve no seu Drive.",
        f"EVOLUTION_DRIVE_CLIENT_ID={client_id}",
        f"EVOLUTION_DRIVE_CLIENT_SECRET={client_secret}",
        f"EVOLUTION_DRIVE_REFRESH_TOKEN={refresh}",
    ]
    if root_id:
        linhas.append(f"EVOLUTION_DRIVE_ROOT_ID={root_id}")
    destino.write_text("\n".join(linhas) + "\n", encoding="utf-8")

    print("\nPronto.")
    print(f"Credenciais gravadas em: {destino}")
    if root_id:
        print(f"Pasta '{nome_raiz}' criada no seu Drive e já referenciada no arquivo.")
    print("\nAbra o arquivo, copie as linhas para o Easypanel e faça o deploy.")
    print("Depois apague o arquivo: ele contém um segredo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
