"""Quadros de um vídeo, para o modelo VER o que o vídeo mostra.

A trilha de áudio já era resolvida: `transcribe_audio` lê o áudio de dentro do mp4 e
devolve o texto. Falta a imagem — tela gravada, placa, produto, cena — e mandar o vídeo
inteiro para o modelo é impossível: um minuto de vídeo em base64 passa de um milhão de
caracteres, e ainda chega como texto, ilegível.

Aqui o servidor abre o vídeo, amostra candidatos ao longo do tempo e escolhe os quadros
mais DIFERENTES entre si: numa gravação parada, 6 quadros iguais não dizem nada, e é a
mudança de cena que carrega a informação. Cada quadro vira um JPEG no disco, com o
instante em que aparece, e custa como uma imagem (1 a 2 mil tokens), não como um vídeo.

Decodificação por `av` (PyAV), que já vem com o faster-whisper; Pillow para redimensionar.
"""

from __future__ import annotations

import hashlib
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

FRAMES_DIRNAME = "video"
MAX_FRAMES = 12
DEFAULT_FRAMES = 4
MAX_SIDE = 1024
CANDIDATES_PER_FRAME = 4      # quantos candidatos amostrar por quadro entregue
ASSINATURA = 16               # lado da miniatura usada para comparar quadros
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".3gp", ".mpeg", ".mpg"}


class VideoError(Exception):
    """Erro ao ler o vídeo."""


def _log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] Vídeo: {message}", file=sys.stderr)


def is_video(mime: str | None, path: str | Path | None = None) -> bool:
    if (mime or "").split(";")[0].strip().lower().startswith("video/"):
        return True
    return path is not None and Path(path).suffix.lower() in VIDEO_EXT


def _require_av():
    try:
        import av
    except ImportError:
        raise VideoError('av (PyAV) não instalado (pip install -e ".[image]")')
    return av


def _require_pillow():
    try:
        from PIL import Image
    except ImportError:
        raise VideoError('Pillow não instalado (pip install -e ".[image]")')
    return Image


def tempo(segundos: float) -> str:
    """12.0 -> '0:12'; 3742.5 -> '1:02:22'."""
    total = int(round(max(segundos, 0)))
    h, resto = divmod(total, 3600)
    m, s = divmod(resto, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def probe(path: str | Path) -> dict[str, Any]:
    """{arquivo, segundos, duracao, largura, altura, fps, audio}."""
    av = _require_av()
    caminho = Path(path)
    if not caminho.is_file():
        raise VideoError(f"arquivo não encontrado: {path}")
    try:
        with av.open(str(caminho)) as container:
            fluxo = next((s for s in container.streams if s.type == "video"), None)
            audio = any(s.type == "audio" for s in container.streams)
            segundos = float(container.duration / av.time_base) if container.duration else 0.0
            if not segundos and fluxo is not None and fluxo.duration and fluxo.time_base:
                segundos = float(fluxo.duration * fluxo.time_base)
            if fluxo is None:
                raise VideoError(f"{caminho.name} não tem faixa de vídeo")
            taxa = fluxo.average_rate or fluxo.guessed_rate or Fraction(0)
            return {
                "arquivo": caminho.name,
                "segundos": round(segundos, 1),
                "duracao": tempo(segundos),
                "largura": fluxo.codec_context.width,
                "altura": fluxo.codec_context.height,
                "fps": round(float(taxa), 2) if taxa else None,
                "audio": audio,
            }
    except VideoError:
        raise
    except Exception as e:
        raise VideoError(f"não consegui abrir o vídeo ({e})")


def _assinatura(imagem) -> bytes:
    return imagem.convert("L").resize((ASSINATURA, ASSINATURA)).tobytes()


def _distancia(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _escolher(candidatos: list[dict], quantidade: int) -> list[dict]:
    """Os mais diferentes entre si, na ordem do tempo.

    Guloso: começa pelo primeiro e, a cada rodada, leva o candidato mais distante do
    que já foi escolhido. Assim uma cena parada não gasta os quadros todos.
    """
    if len(candidatos) <= quantidade:
        return candidatos
    escolhidos = [candidatos[0]]
    restantes = candidatos[1:]
    while len(escolhidos) < quantidade and restantes:
        melhor = max(restantes, key=lambda c: min(_distancia(c["sig"], e["sig"]) for e in escolhidos))
        escolhidos.append(melhor)
        restantes.remove(melhor)
    return sorted(escolhidos, key=lambda c: c["t"])


def extract_frames(
    path: str | Path,
    out_dir: str | Path,
    frames: int = DEFAULT_FRAMES,
    start_s: float = 0.0,
    end_s: float | None = None,
    max_side: int = MAX_SIDE,
) -> dict[str, Any]:
    """Tira `frames` quadros representativos e grava como JPEG.

    Returns: {video_id, pasta, info, quadros:[{id, t, tempo, arquivo}]}
    """
    av = _require_av()
    Image = _require_pillow()
    caminho = Path(path)
    info = probe(caminho)
    quantidade = max(1, min(int(frames or DEFAULT_FRAMES), MAX_FRAMES))
    inicio = max(0.0, float(start_s or 0))
    fim = float(end_s) if end_s else info["segundos"]
    if info["segundos"] and fim > info["segundos"]:
        fim = info["segundos"]
    if fim <= inicio:
        fim = inicio + 1.0

    video_id = hashlib.sha1(caminho.read_bytes()).hexdigest()[:12]
    pasta = Path(out_dir) / FRAMES_DIRNAME / video_id
    pasta.mkdir(parents=True, exist_ok=True)

    quantos_candidatos = min(quantidade * CANDIDATES_PER_FRAME, 40)
    passo = (fim - inicio) / quantos_candidatos
    alvos = [inicio + passo * (i + 0.5) for i in range(quantos_candidatos)]

    candidatos: list[dict] = []
    try:
        with av.open(str(caminho)) as container:
            fluxo = next(s for s in container.streams if s.type == "video")
            fluxo.thread_type = "AUTO"
            for alvo in alvos:
                try:
                    container.seek(int(alvo / fluxo.time_base), stream=fluxo)
                    quadro = next(container.decode(fluxo), None)
                except Exception:
                    quadro = None
                if quadro is None:
                    continue
                t = float(quadro.pts * fluxo.time_base) if quadro.pts is not None else alvo
                if candidatos and abs(t - candidatos[-1]["t"]) < 0.05:
                    continue
                imagem = quadro.to_image()
                imagem.thumbnail((max_side, max_side))
                candidatos.append({"t": round(t, 2), "img": imagem, "sig": _assinatura(imagem)})
    except Exception as e:
        raise VideoError(f"falha ao ler quadros de {caminho.name}: {e}")
    if not candidatos:
        raise VideoError(f"nenhum quadro legível em {caminho.name}")

    escolhidos = _escolher(candidatos, quantidade)
    quadros = []
    for n, item in enumerate(escolhidos, start=1):
        arquivo = pasta / f"frame{n}.jpg"
        item["img"].convert("RGB").save(arquivo, "JPEG", quality=85, optimize=True)
        quadros.append({"id": f"frame{n}", "t": item["t"], "tempo": tempo(item["t"]), "arquivo": str(arquivo)})
    _log(f"{caminho.name}: {len(quadros)} quadros de {len(candidatos)} candidatos em {pasta}")
    return {"video_id": video_id, "pasta": str(pasta), "info": info, "quadros": quadros}
