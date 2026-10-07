"""Tests de la idempotencia del webhook (call_webhook).

Tenía el mismo defecto que el correo: si una notificación fallaba, el mismo
mensaje ya no se podía reintentar y la tool respondía "ya se había enviado". Ahora
el fallo se clasifica igual que en Gmail: definitivo (4xx, la conexión ni se abrió)
se puede reintentar; dudoso (5xx, timeout) no se reintenta ni se da por enviado.

El POST se sustituye por un doble; webhook_events va a la BD real de test.
"""
import importlib

import httpx
import pytest
from sqlalchemy import text

from app.db.session import engine

webhook = importlib.import_module("app.agent.tools.call_webhook")   # el módulo, no la tool
_URL = "https://hooks.example.com/nexa"


@pytest.fixture
def posts(monkeypatch):
    """httpx.post falso: cada llamada consume el siguiente resultado (un status HTTP
    o una excepción de httpx); agotados, responde 200. Devuelve las llamadas."""
    state = {"outcomes": [], "calls": []}

    def fake_post(url, json=None, timeout=None):
        state["calls"].append(json)
        outcome = state["outcomes"].pop(0) if state["outcomes"] else 200
        request = httpx.Request("POST", url)
        if isinstance(outcome, type):
            raise outcome("falso", request=request)
        return httpx.Response(outcome, request=request)

    monkeypatch.setattr(webhook.httpx, "post", fake_post)
    monkeypatch.setattr(webhook, "_webhook_url", lambda: _URL)
    return state


async def _statuses() -> list[str]:
    async with engine.begin() as conn:
        return (await conn.execute(text("SELECT status FROM webhook_events"))).scalars().all()


@pytest.mark.parametrize("outcome", [httpx.ConnectError, 400], ids=["sin-conexión", "4xx"])
async def test_a_definitive_failure_can_be_retried(posts, outcome):
    posts["outcomes"] = [outcome]

    assert await webhook._register_and_send("informe listo", _URL) == "failed"
    assert await webhook._register_and_send("informe listo", _URL) == "sent"
    assert len(posts["calls"]) == 2
    assert await _statuses() == ["sent"]


@pytest.mark.parametrize("outcome", [500, httpx.ReadTimeout], ids=["5xx", "timeout"])
async def test_an_uncertain_failure_is_not_resent_nor_reported_as_sent(posts, outcome):
    posts["outcomes"] = [outcome]

    assert await webhook._register_and_send("informe listo", _URL) == "uncertain"
    reply = webhook.call_webhook.invoke({"message": "informe listo"})

    assert len(posts["calls"]) == 1
    assert reply.startswith("No sé si la notificación llegó")
    assert await _statuses() == ["uncertain"]


async def test_a_sent_notification_is_not_sent_twice(posts):
    assert webhook.call_webhook.invoke({"message": "informe listo"}) == "Notificación enviada."
    assert webhook.call_webhook.invoke({"message": "informe listo"}) == (
        "Esa notificación ya se había enviado (no se reenvió).")
    assert len(posts["calls"]) == 1
