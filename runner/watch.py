"""Executor local: vigia a fila do EVOAPI e acorda o Claude Code quando há pendência.

Por que existe: o laço dentro de um chat do Claude gasta um turno a cada 30 s
mesmo sem nada para fazer, e quando a sessão morre ninguém percebe. Aqui a vigília
é uma chamada HTTP barata a cada poucos segundos, sem modelo envolvido. Só quando
o servidor diz que há instrução pendente é que o Claude Code é acordado, em modo
não interativo (`claude -p`), com contexto limpo, trata a fila e termina.

Cada acionamento é uma sessão nova: não há memória entre eles, e é assim que se
quer. As instruções que o Claude segue estão em PROMPT.md e CLAUDE.md, ao lado.

Defesas:
- trava de instância única (msvcrt), para dois executores não se atropelarem;
- se o Claude Code não estiver logado, não acorda ninguém e espera;
- se acionamentos seguidos não tirarem nada da fila, recua por BACKOFF_S em vez de
  queimar uso à toa; o vigia no servidor avisa Max nesse caso;
- nunca morre por exceção: registra e continua.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

AQUI = Path(__file__).resolve().parent
LOCAL = AQUI / ".local"
LOCAL.mkdir(exist_ok=True)

log = logging.getLogger("executor")


# ------------------------------------------------------------------ configuração

def carregar_env(caminho: Path) -> None:
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        os.environ.setdefault(chave.strip(), valor.strip())


carregar_env(AQUI / ".env")

MCP_URL = os.environ.get("MCP_URL", "").strip()
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "10") or 10)
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
CLAUDE_TIMEOUT_S = float(os.environ.get("CLAUDE_TIMEOUT_S", "600") or 600)
STUCK_RUNS = int(os.environ.get("STUCK_RUNS", "3") or 3)
BACKOFF_S = float(os.environ.get("BACKOFF_S", "300") or 300)
CLAUDE_BIN = (
    os.environ.get("CLAUDE_BIN")
    or shutil.which("claude")
    or str(Path.home() / ".local" / "bin" / "claude.exe")
)


def configurar_log() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    arquivo = RotatingFileHandler(LOCAL / "executor.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    arquivo.setFormatter(fmt)
    log.addHandler(arquivo)
    if sys.stderr is not None:  # pythonw não tem stderr
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        log.addHandler(console)
    log.setLevel(logging.INFO)


# ------------------------------------------------------------------ trava

def travar():
    """Uma instância só. Devolve o handle (mantido aberto) ou None se já há outra."""
    caminho = LOCAL / "executor.lock"
    handle = open(caminho, "a+")
    try:
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except ImportError:
        pass  # fora do Windows a trava é só o arquivo
    except OSError:
        handle.close()
        return None
    return handle


# ------------------------------------------------------------------ MCP

class Mcp:
    """Cliente mínimo do MCP streamable HTTP: só o suficiente para pending_triggers."""

    def __init__(self, url: str, token: str):
        self.url = url
        self.sessao = requests.Session()
        self.sessao.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
        self.sid: str | None = None
        self.n = 0

    def _rpc(self, metodo: str, params: dict | None = None, notificacao: bool = False):
        self.n += 1
        corpo: dict = {"jsonrpc": "2.0", "method": metodo}
        if not notificacao:
            corpo["id"] = self.n
        if params is not None:
            corpo["params"] = params
        cabecalhos = {"mcp-session-id": self.sid} if self.sid else {}
        r = self.sessao.post(self.url, json=corpo, headers=cabecalhos, timeout=60)
        if "mcp-session-id" in r.headers:
            self.sid = r.headers["mcp-session-id"]
        if r.status_code == 404 and self.sid:
            self.sid = None  # sessão expirou no servidor
            raise RuntimeError("sessão MCP expirada")
        r.raise_for_status()
        if notificacao or not r.content:
            return None
        for linha in r.content.split(b"\n"):
            if linha.startswith(b"data:"):
                dado = json.loads(linha[5:].strip().decode("utf-8"))
                if "error" in dado:
                    raise RuntimeError(dado["error"])
                return dado.get("result")
        raise RuntimeError(f"resposta sem dados: {r.text[:200]}")

    def conectar(self) -> None:
        self.sid = None
        self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "evoapi-executor", "version": "1"},
        })
        self._rpc("notifications/initialized", notificacao=True)

    def pendentes(self) -> list[dict]:
        if self.sid is None:
            self.conectar()
        res = self._rpc("tools/call", {"name": "pending_triggers", "arguments": {"limit": 50}})
        texto = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        return json.loads(texto).get("pendentes", [])


# ------------------------------------------------------------------ Claude Code

def ambiente_limpo() -> dict:
    env = dict(os.environ)
    # Se este script for iniciado de dentro de uma sessão do Claude Code, estas
    # variáveis fariam o filho achar que está aninhado.
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def claude_logado() -> bool:
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "auth", "status"], capture_output=True, text=True, timeout=60,
            env=ambiente_limpo(), encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        )
        return bool(json.loads(r.stdout).get("loggedIn"))
    except Exception as e:
        log.warning("não consegui ler o estado do login: %s", e)
        return False


def escrever_mcp_config() -> Path:
    caminho = LOCAL / "mcp.json"
    caminho.write_text(json.dumps({"mcpServers": {"evoapi": {
        "type": "http",
        "url": MCP_URL,
        "headers": {"Authorization": f"Bearer {MCP_AUTH_TOKEN}"},
    }}}), encoding="utf-8")
    return caminho


def acordar_claude() -> dict:
    """Roda `claude -p` uma vez. Devolve o resumo do resultado."""
    prompt = (AQUI / "PROMPT.md").read_text(encoding="utf-8")
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--model", CLAUDE_MODEL,
        "--mcp-config", str(escrever_mcp_config()),
        "--strict-mcp-config",
        "--permission-mode", "dontAsk",
        "--allowedTools", "mcp__evoapi", "Skill", "Read",
        "--output-format", "json",
    ]
    inicio = time.time()
    try:
        r = subprocess.run(
            cmd, cwd=AQUI, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=CLAUDE_TIMEOUT_S, env=ambiente_limpo(), stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"erro": f"tempo esgotado após {int(CLAUDE_TIMEOUT_S)} s"}
    duracao = round(time.time() - inicio)
    try:
        saida = json.loads(r.stdout)
    except ValueError:
        return {"erro": f"saída inesperada (código {r.returncode}): {(r.stdout or r.stderr)[:400]}", "s": duracao}
    return {
        "erro": saida.get("result") if saida.get("is_error") else None,
        "resultado": (saida.get("result") or "")[:500],
        "turnos": saida.get("num_turns"),
        "s": duracao,
        "custo_usd": saida.get("total_cost_usd"),
        "negados": [d.get("tool_name") for d in saida.get("permission_denials", [])],
    }


# ------------------------------------------------------------------ laço

def main() -> int:
    configurar_log()
    if not MCP_URL or not MCP_AUTH_TOKEN:
        log.error("MCP_URL e MCP_AUTH_TOKEN precisam estar no .env")
        return 2
    trava = travar()
    if trava is None:
        log.info("já existe um executor rodando; saindo")
        return 0
    log.info("executor ativo: consulta a cada %ss, modelo %s, claude em %s", POLL_SECONDS, CLAUDE_MODEL, CLAUDE_BIN)

    mcp = Mcp(MCP_URL, MCP_AUTH_TOKEN)
    travados_seguidos = 0
    while True:
        try:
            pend = mcp.pendentes()
        except Exception as e:
            log.warning("falha ao consultar a fila: %s", e)
            mcp.sid = None
            time.sleep(POLL_SECONDS)
            continue

        if not pend:
            travados_seguidos = 0
            time.sleep(POLL_SECONDS)
            continue

        ids_antes = {p.get("id") for p in pend}
        descricao = "; ".join(
            f"{p.get('chat', '?')} -> {str(p.get('instruction') or p.get('file') or '')[:50]!r}" for p in pend
        )
        log.info("%d pendência(s): %s", len(pend), descricao)

        if not claude_logado():
            log.error("Claude Code não está logado. Rode `claude auth login` num terminal. Tentando de novo em %ss", BACKOFF_S)
            time.sleep(BACKOFF_S)
            continue

        res = acordar_claude()
        if res.get("erro"):
            log.error("acionamento falhou (%ss): %s", res.get("s"), res["erro"])
        else:
            negados = f", negados: {res['negados']}" if res.get("negados") else ""
            log.info(
                "acionamento ok: %s turnos, %ss, US$ %s%s | %s",
                res.get("turnos"), res.get("s"), res.get("custo_usd"), negados,
                res.get("resultado", "").replace("\n", " "),
            )

        try:
            ids_depois = {p.get("id") for p in mcp.pendentes()}
        except Exception:
            ids_depois = set()
        if ids_antes and ids_antes <= ids_depois:
            travados_seguidos += 1
            log.warning("nada saiu da fila (%d/%d)", travados_seguidos, STUCK_RUNS)
            if travados_seguidos >= STUCK_RUNS:
                log.error("fila travada; recuando %ss para não queimar uso. O vigia do servidor avisa Max.", BACKOFF_S)
                time.sleep(BACKOFF_S)
                travados_seguidos = 0
                continue
        else:
            travados_seguidos = 0
        time.sleep(2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
