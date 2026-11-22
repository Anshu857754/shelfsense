"""Temporary connection problems are retried; real errors still surface clearly.

These tests replace the HTTP transport (not the AI): the real request code runs against a fake server.
"""

import asyncio

import httpx
import pytest

from app import vision
from app.config import Settings

SETTINGS = Settings(_env_file=None, openrouter_api_key="test-key", openrouter_model="test/model")
GOOD = {"choices": [{"message": {"content": "all good"}}]}


@pytest.fixture
def fake_server(monkeypatch):
    """Serve a scripted list of responses (or exceptions) and count the requests."""
    state = {"script": [], "calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        step = state["script"].pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        vision.httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(vision, "RETRY_DELAYS", (0.0, 0.0))  # no real waiting in tests
    return state


def call():
    return asyncio.run(vision._call_openrouter([{"role": "user", "content": "hi"}], SETTINGS))


def test_a_connection_blip_is_retried_and_succeeds(fake_server):
    fake_server["script"] = [httpx.ConnectError("blip"), httpx.Response(200, json=GOOD)]
    assert call() == "all good" and fake_server["calls"] == 2


@pytest.mark.parametrize("status", [429, 502, 503])
def test_busy_service_responses_are_retried(fake_server, status):
    fake_server["script"] = [httpx.Response(status, text="busy"), httpx.Response(200, json=GOOD)]
    assert call() == "all good" and fake_server["calls"] == 2


def test_it_gives_up_after_three_tries_with_a_clear_error(fake_server):
    fake_server["script"] = [httpx.ConnectError("down")] * 3
    with pytest.raises(vision.OpenRouterError) as exc:
        call()
    assert "Could not reach OpenRouter" in str(exc.value) and fake_server["calls"] == 3


def test_a_client_error_is_not_retried(fake_server):
    fake_server["script"] = [httpx.Response(401, text="bad key")]
    with pytest.raises(vision.OpenRouterError) as exc:
        call()
    assert "401" in str(exc.value) and fake_server["calls"] == 1  # retrying a wrong key would only waste time


def test_a_persistent_server_error_is_reported_after_the_last_try(fake_server):
    fake_server["script"] = [httpx.Response(503, text="down")] * 3
    with pytest.raises(vision.OpenRouterError) as exc:
        call()
    assert "503" in str(exc.value) and fake_server["calls"] == 3
