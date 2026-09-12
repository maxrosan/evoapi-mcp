"""Configuração comum dos testes: adiciona src/ ao path e fornece um cliente offline."""

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evoapi_mcp.client import EvolutionClient  # noqa: E402
from evoapi_mcp.config import EvolutionConfig  # noqa: E402


@pytest.fixture
def config(tmp_path):
    return EvolutionConfig(
        base_url="http://evolution.test",
        api_token="token",
        instance_name="inst",
        media_dir=str(tmp_path / "media"),
        transcribe_backend="off",  # determinístico: testes injetam o backend
        owner_number="5584999290327",
        _env_file=None,
    )


@pytest.fixture
def client(config):
    """Cliente cujas requisições HTTP são interceptadas por `client.calls` / `client.responses`."""
    c = EvolutionClient(config)
    c.calls = []
    c.responses = []

    def fake_request(method, endpoint, data=None, params=None):
        c.calls.append({"method": method, "endpoint": endpoint, "data": data, "params": params})
        if not c.responses:
            return {}
        resp = c.responses.pop(0)
        if callable(resp):
            return resp(data)
        return resp

    c._make_request = fake_request
    return c


@pytest.fixture(autouse=True)
def registro_limpo():
    """Zera o registro global entre testes: ele persiste "já tratei" de propósito."""
    from evoapi_mcp import webhook
    from evoapi_mcp.store import MemoryStore

    anterior = webhook.EVENTS
    webhook.EVENTS = webhook.EventLog(store=MemoryStore(), owner_number="5584999290327")
    yield webhook.EVENTS
    webhook.EVENTS = anterior


@pytest.fixture
def eventos():
    """Um registro isolado, para injetar no Bot."""
    from evoapi_mcp.webhook import EventLog
    from evoapi_mcp.store import MemoryStore

    return EventLog(store=MemoryStore(), owner_number="5584999290327")
