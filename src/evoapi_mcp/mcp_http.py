"""MCP via Streamable HTTP com autenticação Bearer.

Substitui o wrapper usado no Easypanel: expõe as mesmas tools do `server.py`
(já com saída compacta, download_media, send_file etc.) em
`http://0.0.0.0:$PORT/mcp`, exigindo o header `Authorization: Bearer $MCP_AUTH_TOKEN`.

Variáveis de ambiente:
    MCP_AUTH_TOKEN            obrigatória; token esperado no header Authorization
    PORT                      porta HTTP (padrão: 3000)
    MCP_HOST                  interface (padrão: 0.0.0.0)
    EVOLUTION_WEBHOOK_SECRET  opcional; ativa o receptor de eventos da Evolution
                              em /webhook/<segredo>, e a leitura em
                              /webhook/<segredo>/log. Sem ela as duas rotas não existem.
    EVOLUTION_BOT_ENABLED     "1" liga o bot "IA:". **Desligado por padrão**: sem isto
                              o receptor apenas observa, sem responder a ninguém.
    ANTHROPIC_API_KEY         necessária quando o bot está ligado.
    EVOLUTION_WATCHDOG_MINUTES
                              vigia da fila: se uma instrução ficar mais que isto sem
                              ser tratada, avisa o dono na conversa pessoal. Padrão 10;
                              0 desliga. Exige EVOLUTION_OWNER_NUMBER.
    EVOLUTION_WATCHDOG_COOLDOWN_MINUTES
                              intervalo mínimo entre avisos (padrão 60).
    EVOLUTION_WAKE_WORD       palavra que abre um comando de voz (padrão "computador").
                              Exige backend de transcrição.
    EVOLUTION_TIMEZONE        fuso de Max para mensagens agendadas (padrão America/Fortaleza).

Uso:
    python -m evoapi_mcp.mcp_http
    # ou, instalado: evoapi-mcp-http
"""

import hmac
import threading
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

src_dir = Path(__file__).parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))


def build_app(token: str):
    """Monta o ASGI app: MCP streamable HTTP protegido por Bearer token."""
    from mcp.server.transport_security import TransportSecuritySettings
    from evoapi_mcp.server import mcp
    from evoapi_mcp.webhook import EVENTS

    expected = f"Bearer {token}".encode()

    # Receptor de eventos da Evolution API. O segredo vai no caminho porque a
    # configuração de webhook da Evolution nem sempre deixa mandar cabeçalho.
    webhook_secret = os.environ.get("EVOLUTION_WEBHOOK_SECRET", "").strip()
    eventos = EVENTS

    # O bot fica DESLIGADO até alguém dizer o contrário. Ligar significa passar a
    # responder a terceiros, e isso não deve acontecer por acidente num deploy.
    bot = None
    executor = None
    if os.environ.get("EVOLUTION_BOT_ENABLED", "").strip() == "1":
        from evoapi_mcp.bot import Bot
        from evoapi_mcp.server import client as evolution_client

        bot = Bot(evolution_client)
        # Uma thread só: as respostas saem em ordem e duas não se atropelam.
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bot")
        print(f"Bot 'IA:' LIGADO (modelo {bot.model})", file=sys.stderr)
    else:
        print("Bot 'IA:' desligado; o receptor apenas observa.", file=sys.stderr)

    # Vigia da fila: a única falha que o servidor consegue ver sozinho é "a fila
    # parou de andar". Quando vê, avisa o dono no WhatsApp em vez de ficar calado.
    watchdog = None
    try:
        minutos = float(os.environ.get("EVOLUTION_WATCHDOG_MINUTES", "10") or 0)
    except ValueError:
        minutos = 0.0
    dono = os.environ.get("EVOLUTION_OWNER_NUMBER", "").strip()
    if minutos > 0 and dono:
        from evoapi_mcp.server import client as evolution_client
        from evoapi_mcp.watchdog import Watchdog

        try:
            cooldown = float(os.environ.get("EVOLUTION_WATCHDOG_COOLDOWN_MINUTES", "60") or 60)
        except ValueError:
            cooldown = 60.0
        watchdog = Watchdog(
            events=eventos,
            send=lambda numero, texto: evolution_client.send_text(number=numero, text=texto, link_preview=False),
            owner_number=dono,
            max_age_s=minutos * 60,
            cooldown_s=cooldown * 60,
        )
        watchdog.start()
    elif minutos > 0:
        print("Vigia desligado: EVOLUTION_OWNER_NUMBER não definido (não sei para quem avisar).", file=sys.stderr)
    else:
        print("Vigia desligado (EVOLUTION_WATCHDOG_MINUTES=0).", file=sys.stderr)
    eventos.watchdog = watchdog

    # Comando de voz: áudio do dono é transcrito em segundo plano e, se começar
    # com a palavra de ativação ("Computador, ..."), vira instrução na fila.
    from evoapi_mcp.server import client as evolution_client

    voz = None
    if evolution_client.transcriber.available:
        voz = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voz")
        print(f"Comando de voz ligado: palavra de ativação '{eventos.wake_word}'", file=sys.stderr)
    else:
        print("Comando de voz desligado: sem backend de transcrição.", file=sys.stderr)

    # Agenda: mensagens marcadas para mais tarde ficam no banco e saem daqui,
    # que é o único processo que está sempre de pé.
    from evoapi_mcp.scheduler import Scheduler
    from evoapi_mcp.server import config as evolution_config

    agenda = Scheduler(
        store=eventos.store,
        send_text=lambda chat, texto: evolution_client.send_text(number=chat, text=texto, link_preview=False),
        send_voice=(lambda chat, texto: evolution_client.send_voice(number=chat, text=texto))
        if evolution_client.speaker.available else None,
        tz=evolution_config.timezone,
    )
    agenda.start()
    eventos.scheduler = agenda

    # Memória de longo prazo: lembranças no mesmo banco, vetores de um modelo local.
    # O modelo carrega em segundo plano para a subida não esperar por ele.
    eventos.memory = None
    if evolution_config.memory_enabled:
        from evoapi_mcp.memory import Embedder, MemoryBank

        embedder = Embedder(evolution_config.memory_model)
        if embedder.available:
            memoria = MemoryBank(eventos.store, embedder, tz=evolution_config.timezone)
            eventos.memory = memoria
            threading.Thread(target=memoria.warm, name="memoria", daemon=True).start()
            print(f"Memória ligada: modelo {embedder.model}, armazenamento {eventos.store.kind}", file=sys.stderr)
        else:
            print("Memória desligada: fastembed não instalado.", file=sys.stderr)
    else:
        print("Memória desligada (EVOLUTION_MEMORY_ENABLED=false).", file=sys.stderr)

    def transcrever(message_id):
        try:
            resultado = evolution_client.transcribe_message(message_id, max_chars=0)
            eventos.resolve_voice(message_id, resultado.get("text"))
        except Exception as e:  # nunca deixar a thread morrer calada
            print(f"[ERROR] Voz: falha ao transcrever {message_id}: {e}", file=sys.stderr)
            eventos.resolve_voice(message_id, None)

    def processar(payload, resumo):
        """Roda fora do ciclo da requisição: a Evolution só quer o 200 rápido."""
        try:
            resultado = bot.handle(resumo, (payload or {}).get("data"))
            if resultado.get("acao") not in ("ignorado",):
                print(f"[INFO] Bot: {resultado}", file=sys.stderr)
        except Exception as e:  # nunca deixar a thread morrer calada
            print(f"[ERROR] Bot: falha ao processar: {e}", file=sys.stderr)

    # Atrás de proxy (Easypanel/Traefik) o Host não é localhost: desliga a proteção de DNS rebinding
    mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    inner = mcp.streamable_http_app()

    async def respond(send, status: int, body: bytes, content_type: bytes) -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", content_type), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})

    async def read_body(receive) -> bytes:
        corpo = b""
        while True:
            evento = await receive()
            corpo += evento.get("body", b"")
            if not evento.get("more_body"):
                return corpo

    async def app(scope, receive, send):
        if scope["type"] == "http":
            caminho = scope.get("path", "").rstrip("/")

            # /health fica fora da autenticação: é o que o Docker e o Easypanel
            # consultam para saber se o container subiu. Não expõe nada.
            if caminho == "/health":
                await respond(send, 200, b'{"status":"healthy"}', b"application/json")
                return

            if webhook_secret and caminho.startswith(f"/webhook/{webhook_secret}"):
                resto = caminho[len(f"/webhook/{webhook_secret}"):]
                if resto == "/log":
                    consulta = dict(
                        p.split("=", 1) for p in scope.get("query_string", b"").decode().split("&") if "=" in p
                    )
                    dados = eventos.snapshot(
                        limit=int(consulta.get("limit", 50) or 50),
                        only_mine=consulta.get("minhas") == "1",
                        only_triggers=consulta.get("acionamentos") == "1",
                    )
                    corpo = json.dumps(dados, ensure_ascii=False).encode()
                    await respond(send, 200, corpo, b"application/json")
                    return
                if resto == "":
                    try:
                        payload = json.loads(await read_body(receive) or b"{}")
                    except ValueError:
                        payload = {"event": "?", "erro": "corpo inválido"}
                    resumo = eventos.add(payload)
                    if voz is not None and resumo.get("voice_pending"):
                        voz.submit(transcrever, resumo["message_id"])
                    if bot is not None and resumo.get("trigger"):
                        executor.submit(processar, payload, resumo)
                    # A Evolution só quer um 200; qualquer outra coisa vira reenvio.
                    await respond(send, 200, b'{"ok":true}', b"application/json")
                    return

            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"")
            if not hmac.compare_digest(provided, expected):
                await respond(send, 401, b"Unauthorized", b"text/plain; charset=utf-8")
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
    if os.environ.get("EVOLUTION_WEBHOOK_SECRET", "").strip():
        print("Receptor de eventos da Evolution ativo em /webhook/<segredo>", file=sys.stderr)
    uvicorn.run(build_app(token), host=host, port=port)


if __name__ == "__main__":
    main()
