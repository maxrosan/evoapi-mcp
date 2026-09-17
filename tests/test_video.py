"""Quadros de vídeo: escolhe os mais diferentes entre si e grava JPEG no servidor."""

from pathlib import Path

import pytest

pytest.importorskip("av")
Image = pytest.importorskip("PIL.Image")

from evoapi_mcp import video  # noqa: E402
from evoapi_mcp.video import VideoError, extract_frames, is_video, probe, tempo  # noqa: E402

CENAS = [(200, 40, 40), (40, 160, 60), (30, 60, 200), (230, 200, 40)]


def _video(caminho, segundos=8, fps=10, cenas=CENAS, tamanho=(320, 240), audio=False):
    """Vídeo de teste: uma cor de fundo por trecho, com um quadrado que anda."""
    import av

    with av.open(str(caminho), mode="w") as container:
        fluxo = container.add_stream("mpeg4", rate=fps)
        fluxo.width, fluxo.height = tamanho
        fluxo.pix_fmt = "yuv420p"
        total = segundos * fps
        for n in range(total):
            cor = cenas[min(int(n / total * len(cenas)), len(cenas) - 1)]
            imagem = Image.new("RGB", tamanho, cor)
            x = int((n / total) * (tamanho[0] - 40))
            for i in range(x, x + 40):
                for j in range(60, 100):
                    imagem.putpixel((i, j), (255, 255, 255))
            quadro = av.VideoFrame.from_image(imagem)
            for pacote in fluxo.encode(quadro):
                container.mux(pacote)
        for pacote in fluxo.encode():
            container.mux(pacote)
    return caminho


@pytest.fixture
def filme(tmp_path):
    return _video(tmp_path / "filme.mp4")


def test_tempo_legivel():
    assert tempo(0) == "0:00" and tempo(12.4) == "0:12" and tempo(95) == "1:35" and tempo(3742) == "1:02:22"


def test_is_video_por_mime_ou_extensao():
    assert is_video("video/mp4", None) and is_video(None, "x.MOV") and is_video(None, "y.webm")
    assert not is_video("image/jpeg", "foto.jpg") and not is_video(None, "nota.pdf")


def test_probe_le_duracao_e_tamanho(filme):
    info = probe(filme)
    assert 7 <= info["segundos"] <= 9
    assert info["largura"] == 320 and info["altura"] == 240
    assert info["fps"] and info["audio"] is False
    assert info["duracao"] == "0:08"


def test_probe_recusa_arquivo_que_nao_e_video(tmp_path):
    ruim = tmp_path / "x.mp4"
    ruim.write_bytes(b"nao e video")
    with pytest.raises(VideoError):
        probe(ruim)
    with pytest.raises(VideoError, match="não encontrado"):
        probe(tmp_path / "sumiu.mp4")


def test_extract_frames_grava_jpegs_com_o_instante(filme, tmp_path):
    saida = extract_frames(filme, tmp_path / "media", frames=4)
    quadros = saida["quadros"]
    assert len(quadros) == 4
    assert [q["id"] for q in quadros] == ["frame1", "frame2", "frame3", "frame4"]
    assert [q["t"] for q in quadros] == sorted(q["t"] for q in quadros)      # na ordem do tempo
    for q in quadros:
        arquivo = tmp_path / "media" / "video" / saida["video_id"] / f"{q['id']}.jpg"
        assert arquivo.is_file() and arquivo.stat().st_size > 500
        assert q["tempo"] == video.tempo(q["t"])
    assert saida["info"]["segundos"] > 0
    assert (tmp_path / "media" / "video" / saida["video_id"]).is_dir()


def test_frames_escolhidos_cobrem_as_cenas_diferentes(filme, tmp_path):
    """Numa gravação com 4 cenas, os 4 quadros não podem sair todos da mesma."""
    saida = extract_frames(filme, tmp_path / "media", frames=4)
    cores = []
    for q in saida["quadros"]:
        with Image.open(q["arquivo"]) as im:
            cores.append(im.convert("RGB").resize((1, 1)).getpixel((0, 0)))
    distintas = {tuple(c // 40 for c in cor) for cor in cores}
    assert len(distintas) >= 3


def test_cena_parada_nao_gasta_quadros_repetidos(tmp_path):
    """Vídeo de uma cor só: sobra pouca diferença, mas ainda devolve o pedido."""
    parado = _video(tmp_path / "parado.mp4", segundos=6, cenas=[(80, 80, 80)])
    saida = extract_frames(parado, tmp_path / "media", frames=3)
    assert len(saida["quadros"]) == 3


def test_trecho_do_video(filme, tmp_path):
    saida = extract_frames(filme, tmp_path / "media", frames=3, start_s=4, end_s=6)
    assert all(3.5 <= q["t"] <= 6.5 for q in saida["quadros"]), saida["quadros"]


def test_limites_de_quantidade(filme, tmp_path):
    assert len(extract_frames(filme, tmp_path / "media", frames=99)["quadros"]) <= video.MAX_FRAMES
    assert len(extract_frames(filme, tmp_path / "media", frames=0)["quadros"]) >= 1


def test_mesmo_video_reaproveita_a_pasta(filme, tmp_path):
    a = extract_frames(filme, tmp_path / "media", frames=2)
    b = extract_frames(filme, tmp_path / "media", frames=2)
    assert a["video_id"] == b["video_id"] and a["pasta"] == b["pasta"]


# ------------------------------------------------------------------ tools do servidor

@pytest.fixture
def server(monkeypatch, tmp_path, filme):
    import importlib

    monkeypatch.setenv("EVOLUTION_BASE_URL", "http://evolution.test")
    monkeypatch.setenv("EVOLUTION_API_TOKEN", "token")
    monkeypatch.setenv("EVOLUTION_INSTANCE_NAME", "inst")
    import evoapi_mcp.server as modulo

    modulo = importlib.reload(modulo)

    class FakeClient:
        media_dir = tmp_path / "media"
        baixados = []

        def download_media(self, message_id, password=None, **_):
            self.baixados.append(message_id)
            if message_id == "FOTO":
                return {"path": str(tmp_path / "foto.jpg"), "mime": "image/jpeg"}
            return {"path": str(filme), "mime": "video/mp4"}

    monkeypatch.setattr(modulo, "client", FakeClient())
    return modulo


def test_tools_de_video_existem_e_estao_na_regra(server):
    import asyncio

    nomes = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"view_video", "video_frames"} <= nomes
    assert "view_video" in server.mcp.instructions and "video_frames" in server.mcp.instructions


def test_view_video_devolve_resumo_e_imagens(server):
    import json

    saida = server.view_video(message_id="VIDEO1", frames=3)
    resumo = json.loads(saida[0])
    assert resumo["segundos"] > 0 and len(resumo["quadros"]) == 3
    assert resumo["quadros"][0]["tempo"]
    imagens = saida[1:]
    assert len(imagens) == 3
    assert all(getattr(i, "_format", None) == "jpeg" or getattr(i, "format", None) == "jpeg" for i in imagens)


def test_video_frames_grava_sem_mandar_imagem(server, tmp_path):
    import json

    saida = json.loads(server.video_frames(message_id="VIDEO1", frames=2))
    assert len(saida["quadros"]) == 2
    for quadro in saida["quadros"]:
        caminho = Path(quadro["arquivo"])
        assert caminho.is_file() and str(tmp_path / "media") in str(caminho)
    assert saida["pasta"].endswith(saida["video_id"])


def test_video_tools_recusam_o_que_nao_e_video(server):
    import json

    assert "não é vídeo" in json.loads(server.video_frames(message_id="FOTO"))["error"]
    assert "Informe message_id" in json.loads(server.video_frames())["error"]
    assert "error" in json.loads(server.view_video(file_path="nao_existe.mp4")[0])
