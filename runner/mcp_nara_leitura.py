"""Cria, no Claude Code (escopo user), cópias SÓ LEITURA dos servidores MCP do NARA
que estão no claude_desktop_config.json. É delas que o executor (`claude -p`) enxerga.

Uso:
    python mcp_nara_leitura.py                      # só mostra os comandos, com segredos mascarados
    python mcp_nara_leitura.py --aplicar            # roda os comandos
    python mcp_nara_leitura.py --aplicar --usuario nara_leitura
        # troca o usuário do banco pelo role só leitura (pede a senha sem mostrar)

O que é criado:
- easypanel-<painel>-leitura, um por painel, com EASYPANEL_READONLY=1 (sem mutação e
  sem reveal_secrets);
- postgres-nara-leitura, UM servidor para todos os bancos: DATABASE_URL do nara_unica e
  os outros em PG_DATABASES, escolhidos pelo parâmetro `connection` das ferramentas.
  Onze processos de Postgres atrasavam a partida do `claude -p` a ponto de o Trello,
  o Gmail e o Drive ficarem de fora da sessão.

Os segredos saem do arquivo direto para o comando; nada é impresso nem gravado aqui.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

SEGREDOS = ("TOKEN", "PASS", "SECRET", "KEY")
SERVIDOR_BANCOS = "postgres-nara-leitura"
BANCO_PADRAO = "postgres-nara-unica"


def achar_config_desktop() -> Path | None:
    """O app da Microsoft Store grava em LocalCache/Roaming, fora do %APPDATA% que o terminal vê."""
    candidatos = [Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json"]
    pacotes = Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
    if pacotes.is_dir():
        candidatos += sorted(pacotes.glob("Claude_*/LocalCache/Roaming/Claude/claude_desktop_config.json"))
    return next((c for c in candidatos if c.is_file()), None)


def trocar_usuario(url: str, usuario: str, senha: str) -> str:
    p = urlsplit(url)
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    return urlunsplit((p.scheme, f"{quote(usuario, safe='')}:{quote(senha, safe='')}@{host}", p.path, p.query, p.fragment))


def mascarar(chave: str, valor: str) -> str:
    if chave == "DATABASE_URL":
        p = urlsplit(valor)
        return f"{p.scheme}://{p.username}:***@{p.hostname}:{p.port}{p.path}"
    if any(s in chave.upper() for s in SEGREDOS):
        return "***"
    return valor


def montar_planos(servidores: dict, usuario: str | None, senha: str | None) -> tuple[list, list[str], list[str]]:
    """(planos, nomes antigos a remover, avisos)."""
    planos, antigos, avisos = [], ["postgres-nara-test", "postgres-nara-prod-leitura"], []

    for nome, cfg in servidores.items():
        env = dict(cfg.get("env") or {})
        if nome.startswith("easypanel-") and "nara" in nome and "EASYPANEL_URL" in env:
            env["EASYPANEL_READONLY"] = "1"
            env.pop("EASYPANEL_ALLOW_REVEAL", None)
            planos.append((nome, f"{nome}-leitura", env, [cfg["command"], *(cfg.get("args") or [])]))

    bancos = {n: c for n, c in servidores.items()
              if n.startswith("postgres-nara-") and "DATABASE_URL" in (c.get("env") or {})}
    antigos += [f"{n}-leitura" for n in bancos]
    if bancos:
        base_nome = BANCO_PADRAO if BANCO_PADRAO in bancos else next(iter(bancos))
        base = bancos[base_nome]
        base_url = urlsplit(base["env"]["DATABASE_URL"])
        nomes = []
        for n, c in bancos.items():
            u = urlsplit(c["env"]["DATABASE_URL"])
            if (u.hostname, u.port, u.username) != (base_url.hostname, base_url.port, base_url.username):
                avisos.append(f"{n} está em outro servidor ou usuário; ficou de fora")
                continue
            banco = u.path.lstrip("/")
            if banco and banco != base_url.path.lstrip("/") and banco not in nomes:
                nomes.append(banco)
        env = {
            "DATABASE_URL": base["env"]["DATABASE_URL"],
            "PG_READ_ONLY": "true",
            "PG_DISABLE_RUNTIME_CONNECT": "true",
            "PG_DATABASES": ",".join(nomes),
        }
        if usuario:
            env["DATABASE_URL"] = trocar_usuario(env["DATABASE_URL"], usuario, senha or "")
        planos.append((", ".join(bancos), SERVIDOR_BANCOS, env, [base["command"], *(base.get("args") or [])]))
    return planos, antigos, avisos


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true", help="roda os comandos (sem isto, só mostra)")
    ap.add_argument("--arquivo", help="caminho do claude_desktop_config.json (padrão: procura sozinho)")
    ap.add_argument("--usuario", help="usuário só leitura do Postgres (ex: nara_leitura); pede a senha")
    args = ap.parse_args()

    desktop = Path(args.arquivo) if args.arquivo else achar_config_desktop()
    if not desktop or not desktop.is_file():
        print("não achei o claude_desktop_config.json; passe o caminho com --arquivo")
        return 1
    claude = shutil.which("claude")
    if not claude:
        print("não achei o comando claude no PATH")
        return 1
    servidores = json.loads(desktop.read_text(encoding="utf-8")).get("mcpServers", {})

    senha = None
    if args.usuario:
        senha = getpass.getpass(f"Senha do usuário {args.usuario} no Postgres (não aparece): ")
        if not senha:
            print("senha vazia; nada feito")
            return 1

    planos, antigos, avisos = montar_planos(servidores, args.usuario, senha)
    if not planos:
        print("nenhum servidor do NARA encontrado no claude_desktop_config.json")
        return 1

    print(f"{len(planos)} servidores só leitura a partir de {desktop}:\n")
    for origem, novo, env, comando in planos:
        mostrado = ["claude", "mcp", "add", novo, "-s", "user"]
        for k, v in env.items():
            mostrado += ["-e", f"{k}={mascarar(k, v)}"]
        print(f"# de {origem}")
        print(" ".join(mostrado + ["--", *comando]) + "\n")
    for aviso in avisos:
        print(f"aviso: {aviso}")
    print(f"serão removidos, se existirem: {', '.join(antigos)}\n")

    if not args.aplicar:
        print("Nada foi alterado. Para criar: python mcp_nara_leitura.py --aplicar [--usuario nara_leitura]")
        return 0

    for nome in antigos + [novo for _, novo, _, _ in planos]:
        subprocess.run([claude, "mcp", "remove", nome, "-s", "user"], capture_output=True, text=True)
    falhas = 0
    for _, novo, env, comando in planos:
        cmd = [claude, "mcp", "add", novo, "-s", "user"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        r = subprocess.run(cmd + ["--", *comando], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            print(f"ok   {novo}")
        else:
            falhas += 1
            erro = (r.stderr or r.stdout).strip().replace(senha or "\0", "***")
            print(f"ERRO {novo}: {erro[:300]}")
    print("\nConferindo conexões (demora um pouco)...\n")
    r = subprocess.run([claude, "mcp", "list"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    for linha in (r.stdout or "").splitlines():
        if "nara" in linha and "leitura" in linha:
            nome = linha.split(":", 1)[0]
            estado = "conectado" if "✔" in linha or "Connected" in linha else linha.rsplit("-", 1)[-1].strip()
            print(f"  {nome}: {estado}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
