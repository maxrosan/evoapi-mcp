"""MCP via Streamable HTTP com autenticação Bearer.

Substitui o wrapper usado no Easypanel: expõe as mesmas tools do `server.py`
(já com saída compacta, download_media, send_file etc.) em
`http://0.0.0.0:$PORT/mcp`, exigindo o header `Authorization: Bearer $MCP_AUTH_TOKEN`.

Variáveis de ambiente:
    MCP_AUTH_TOKEN  obrigatória; token esperado no header Authorization
    PORT            porta HTTP (padrão: 3000)
    MCP_HOST        interface (padrão: 0.0.0.0)

Uso:
    python -m evoapi_mcp.mcp_http
    # ou, instalado: evoapi-mcp-http
"""

import hmac
import os
import sys
from pathlib import Path

src_dir = Path(__file__).parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))


def build_app(token: str):
    """Monta o ASGI app: MCP streamable HTTP protegido por Bearer token."""
    from mcp.server.transport_security import TransportSecuritySettings
    from evoapi_mcp.server import mcp

    expected = f"Bearer {token}".encode()

    # Atrás de proxy (Easypanel/Traefik) o Host não é localhost: desliga a proteção de DNS rebinding
    mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    inner = mcp.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"")
            if not hmac.compare_digest(provided, expected):
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                })
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await inner(scope, receive, send)

    return app


def main() -> None:
    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if not token:
        sys.exit("MCP_AUTH_TOKEN não definido.")

    import uvicorn

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "3000"))
    print(f"MCP streamable HTTP em http://{host}:{port}/mcp", file=sys.stderr)
    uvicorn.run(build_app(token), host=host, port=port)


if __name__ == "__main__":
    main()
