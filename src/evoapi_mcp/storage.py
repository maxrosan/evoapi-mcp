"""Faxina e medição da pasta de mídias.

Tudo que o servidor toca vira arquivo: o anexo baixado do WhatsApp, o PNG que ele
rasterizou, o arquivo que ele buscou na web. Nada apagava nada, e num container do
Easypanel o disco é pequeno. Quando ele enche, não quebra só o download: quebra o
envio, a transcrição e o arquivamento ao mesmo tempo, e a mensagem de erro não diz
"disco cheio", diz qualquer outra coisa.

Então a pasta tem prazo de validade. A varredura roda no máximo uma vez por dia
(marcada num arquivo, para sobreviver a restart), apaga o que passou do prazo e
nunca toca no que é registro, não mídia: a subpasta `.transcripts` guarda texto de
áudio já transcrito, barato de manter e caro de refazer.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

# Pastas que a faxina não varre, por guardarem registro em vez de mídia.
PRESERVADAS = {".transcripts"}

MARCADOR = ".ultima-faxina"
INTERVALO_FAXINA = 24 * 3600


class StorageError(Exception):
    """Erro ao varrer ou medir a pasta de mídias."""


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Disco: {message}", file=sys.stderr)


def _arquivos(media_dir: Path):
    for caminho in media_dir.rglob("*"):
        if not caminho.is_file():
            continue
        if any(parte in PRESERVADAS for parte in caminho.relative_to(media_dir).parts):
            continue
        if caminho.name == MARCADOR:
            continue
        yield caminho


def usage(media_dir: str | Path) -> dict:
    """Mede a pasta: quantos arquivos, quantos bytes, e quanto sobra no disco.

    Returns:
        dict: {files, bytes, mb, free_mb, oldest?} — ou {error} se a pasta não existe
    """
    media_dir = Path(media_dir)
    if not media_dir.is_dir():
        return {"files": 0, "bytes": 0, "mb": 0.0}

    total = 0
    quantidade = 0
    mais_antigo = None
    for caminho in _arquivos(media_dir):
        try:
            info = caminho.stat()
        except OSError:  # apagado no meio da varredura
            continue
        total += info.st_size
        quantidade += 1
        if mais_antigo is None or info.st_mtime < mais_antigo:
            mais_antigo = info.st_mtime

    resultado = {
        "files": quantidade,
        "bytes": total,
        "mb": round(total / (1024 * 1024), 1),
    }
    try:
        resultado["free_mb"] = round(shutil.disk_usage(media_dir).free / (1024 * 1024), 1)
    except OSError:
        pass
    if mais_antigo:
        resultado["oldest_days"] = round((time.time() - mais_antigo) / 86400, 1)
    return resultado


def sweep(media_dir: str | Path, ttl_days: int, dry_run: bool = False) -> dict:
    """Apaga da pasta o que passou do prazo.

    Args:
        media_dir: pasta de mídias
        ttl_days: idade máxima em dias; 0 ou menos desliga a faxina
        dry_run: só conta, não apaga

    Returns:
        dict: {removed, freed_bytes, freed_mb, ttl_days, dry_run?} ou {skipped: "..."}
    """
    media_dir = Path(media_dir)
    if ttl_days <= 0:
        return {"skipped": "ttl desligado"}
    if not media_dir.is_dir():
        return {"skipped": "pasta não existe"}

    limite = time.time() - ttl_days * 86400
    removidos = 0
    liberados = 0

    for caminho in _arquivos(media_dir):
        try:
            info = caminho.stat()
            if info.st_mtime >= limite:
                continue
            if not dry_run:
                caminho.unlink()
            removidos += 1
            liberados += info.st_size
        except OSError as e:  # permissão, corrida com outro processo
            _log(f"não deu para apagar {caminho}: {e}", "WARN")

    if not dry_run:
        # Pasta que ficou vazia depois da faxina vai junto; a raiz fica.
        for pasta in sorted(media_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if pasta.is_dir() and pasta.name not in PRESERVADAS and not any(pasta.iterdir()):
                try:
                    pasta.rmdir()
                except OSError:
                    pass

    resultado = {
        "removed": removidos,
        "freed_bytes": liberados,
        "freed_mb": round(liberados / (1024 * 1024), 1),
        "ttl_days": ttl_days,
    }
    if dry_run:
        resultado["dry_run"] = True
    elif removidos:
        _log(f"{removidos} arquivo(s) além de {ttl_days} dia(s) apagados ({resultado['freed_mb']} MB)")
    return resultado


def sweep_if_due(media_dir: str | Path, ttl_days: int) -> dict | None:
    """Varre no máximo uma vez por dia.

    Chamada sempre que o servidor grava um arquivo: é barato (só lê a data do
    marcador) e não depende de ninguém lembrar de rodar manutenção.
    """
    media_dir = Path(media_dir)
    if ttl_days <= 0 or not media_dir.is_dir():
        return None

    marcador = media_dir / MARCADOR
    agora = time.time()
    try:
        if marcador.exists() and agora - marcador.stat().st_mtime < INTERVALO_FAXINA:
            return None
        marcador.touch()
    except OSError as e:
        _log(f"não deu para marcar a faxina: {e}", "WARN")
        return None

    return sweep(media_dir, ttl_days)
