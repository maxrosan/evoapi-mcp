"""Faxina da pasta de mídias: o disco do servidor não é infinito."""

import os
import time
from pathlib import Path

from evoapi_mcp.storage import MARCADOR, sweep, sweep_if_due, usage


def arquivo(caminho: Path, idade_dias: float = 0, tamanho: int = 100) -> Path:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(b"x" * tamanho)
    quando = time.time() - idade_dias * 86400
    os.utime(caminho, (quando, quando))
    return caminho


def test_usage_measures_what_is_there(tmp_path):
    arquivo(tmp_path / "a.png", tamanho=1024)
    arquivo(tmp_path / "web" / "b.pdf", idade_dias=3, tamanho=2048)

    medida = usage(tmp_path)

    assert medida["files"] == 2
    assert medida["bytes"] == 3072
    assert medida["oldest_days"] == 3.0
    assert "free_mb" in medida


def test_usage_on_a_missing_folder_is_zero(tmp_path):
    assert usage(tmp_path / "nao-existe") == {"files": 0, "bytes": 0, "mb": 0.0}


def test_sweep_removes_only_what_expired(tmp_path):
    velho = arquivo(tmp_path / "web" / "antigo.png", idade_dias=40)
    novo = arquivo(tmp_path / "render" / "recente.png", idade_dias=2)

    out = sweep(tmp_path, ttl_days=30)

    assert out["removed"] == 1
    assert not velho.exists()
    assert novo.exists()


def test_sweep_never_touches_transcripts(tmp_path):
    """Transcrição é registro, não mídia: barata de guardar e cara de refazer."""
    transcricao = arquivo(tmp_path / ".transcripts" / "MSG1.json", idade_dias=400)

    out = sweep(tmp_path, ttl_days=30)

    assert out["removed"] == 0
    assert transcricao.exists()


def test_sweep_cleans_up_the_folders_it_emptied(tmp_path):
    arquivo(tmp_path / "web" / "antigo.png", idade_dias=40)

    sweep(tmp_path, ttl_days=30)

    assert not (tmp_path / "web").exists()
    assert tmp_path.exists()


def test_dry_run_counts_without_deleting(tmp_path):
    velho = arquivo(tmp_path / "antigo.png", idade_dias=40)

    out = sweep(tmp_path, ttl_days=30, dry_run=True)

    assert out["removed"] == 1 and out["dry_run"] is True
    assert velho.exists()


def test_ttl_zero_turns_the_sweep_off(tmp_path):
    velho = arquivo(tmp_path / "antigo.png", idade_dias=999)

    assert sweep(tmp_path, ttl_days=0) == {"skipped": "ttl desligado"}
    assert velho.exists()


def test_sweep_if_due_runs_once_a_day(tmp_path):
    arquivo(tmp_path / "antigo.png", idade_dias=40)

    assert sweep_if_due(tmp_path, 30)["removed"] == 1
    # segunda chamada no mesmo dia não varre de novo
    assert sweep_if_due(tmp_path, 30) is None

    # marcador com mais de um dia libera a próxima varredura
    marcador = tmp_path / MARCADOR
    antigo = time.time() - 2 * 86400
    os.utime(marcador, (antigo, antigo))
    assert sweep_if_due(tmp_path, 30) is not None


def test_the_marker_is_not_counted_as_media(tmp_path):
    arquivo(tmp_path / "a.png")
    sweep_if_due(tmp_path, 30)
    assert usage(tmp_path)["files"] == 1


def test_the_client_sweeps_after_writing(client, config, monkeypatch):
    """Quem grava dispara a faxina — é o que evita depender de alguém lembrar."""
    media = Path(config.media_dir)
    velho = arquivo(media / "web" / "antigo.png", idade_dias=40)
    monkeypatch.setattr(config, "media_ttl_days", 30, raising=False)
    client.responses.append({
        "mediaType": "image", "fileName": "nova.png", "mimetype": "image/png",
        "base64": "QUJD",
    })

    client.download_media("MSG1")

    assert not velho.exists()
