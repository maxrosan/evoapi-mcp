"""Executor local: sessão que nasce sem um servidor liberado recomeça antes do primeiro turno."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))
watch = pytest.importorskip("watch")


def test_servidores_exigidos_pelo_nome():
    permitidas = [
        "mcp__evoapi", "Read", "mcp__claude_ai_Trello",
        "mcp__claude_ai_Gmail__search_threads", "mcp__claude_ai_Gmail__get_thread",
        "mcp__postgres-nara-prod-leitura__pg_query",
    ]
    assert watch.servidores_exigidos(permitidas) == [
        "evoapi", "claude_ai_Trello", "claude_ai_Gmail", "postgres-nara-prod-leitura",
    ]


def test_servidores_ausentes_olha_as_ferramentas_do_init():
    init = {"tools": ["Read", "mcp__evoapi__send_text_message", "mcp__claude_ai_Gmail__get_thread"]}
    assert watch.servidores_ausentes(init, ["evoapi", "claude_ai_Trello", "claude_ai_Gmail"]) == [
        "claude_ai_Trello",
    ]


def _falso_claude(tools, resultado="feito"):
    """Comando que imita o stream-json do `claude -p`."""
    init = json.dumps({"type": "system", "subtype": "init", "tools": tools})
    fim = json.dumps({"type": "result", "result": resultado, "num_turns": 3, "total_cost_usd": 0.1})
    script = f"import sys,time; print({init!r}); sys.stdout.flush(); time.sleep(0.5); print({fim!r})"
    return [sys.executable, "-c", script]


def test_rodar_claude_le_o_resultado():
    r = watch.rodar_claude(_falso_claude(["mcp__evoapi__x"]), 30, ["evoapi"], abortar_se_faltar=True)
    assert r["ausentes"] == []
    assert r["resultado"]["result"] == "feito"
    assert not r["estourou"]


def test_rodar_claude_aborta_no_init_quando_falta_servidor():
    r = watch.rodar_claude(_falso_claude(["mcp__evoapi__x"]), 30, ["evoapi", "claude_ai_Trello"],
                           abortar_se_faltar=True)
    assert r["ausentes"] == ["claude_ai_Trello"]
    assert r["resultado"] is None


def test_rodar_claude_na_ultima_tentativa_segue_sem_o_servidor():
    r = watch.rodar_claude(_falso_claude(["mcp__evoapi__x"]), 30, ["evoapi", "claude_ai_Trello"],
                           abortar_se_faltar=False)
    assert r["ausentes"] == ["claude_ai_Trello"]
    assert r["resultado"]["result"] == "feito"


def test_rodar_claude_respeita_o_limite():
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    r = watch.rodar_claude(cmd, 1, [], abortar_se_faltar=True)
    assert r["estourou"]


def test_acordar_claude_recomeca_ate_o_servidor_aparecer(monkeypatch):
    respostas = [
        {"resultado": None, "ausentes": ["claude_ai_Trello"], "estourou": False, "codigo": 1, "resto": ""},
        {"resultado": {"result": "ok", "num_turns": 2}, "ausentes": [], "estourou": False, "codigo": 0, "resto": ""},
    ]
    chamadas = []

    def falso(cmd, limite, exigidos, abortar_se_faltar):
        chamadas.append(abortar_se_faltar)
        return respostas[len(chamadas) - 1]

    monkeypatch.setattr(watch, "rodar_claude", falso)
    monkeypatch.setattr(watch, "MCP_START_RETRIES", 2)
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)
    res = watch.acordar_claude()
    assert chamadas == [True, True]
    assert res["erro"] is None and res["resultado"] == "ok"


def test_acordar_claude_desiste_de_esperar_na_ultima_tentativa(monkeypatch):
    chamadas = []

    def falso(cmd, limite, exigidos, abortar_se_faltar):
        chamadas.append(abortar_se_faltar)
        if abortar_se_faltar:
            return {"resultado": None, "ausentes": ["claude_ai_Trello"], "estourou": False, "codigo": 1, "resto": ""}
        return {"resultado": {"result": "sem trello"}, "ausentes": ["claude_ai_Trello"],
                "estourou": False, "codigo": 0, "resto": ""}

    monkeypatch.setattr(watch, "rodar_claude", falso)
    monkeypatch.setattr(watch, "MCP_START_RETRIES", 2)
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)
    res = watch.acordar_claude()
    assert chamadas == [True, True, False]
    assert res["ausentes"] == ["claude_ai_Trello"]


def test_ambiente_espera_os_servidores_conectarem(monkeypatch):
    monkeypatch.setenv("MCP_CONNECTION_NONBLOCKING", "true")
    assert watch.ambiente_limpo()["MCP_CONNECTION_NONBLOCKING"] == "false"
