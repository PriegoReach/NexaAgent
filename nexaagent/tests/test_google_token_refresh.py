"""Tests de get_valid_token cuando Google NO renueva el acceso.

Los refresh tokens de una app en modo Testing caducan a los ~7 días. Antes, el 400
'invalid_grant' de Google escapaba como httpx.HTTPStatusError: las tools de Google
reventaban y un correo quedaba bloqueado en 'pending'. Ahora:
  - invalid_grant -> NoGoogleAccount ("reconecta") y se borra el refresh token muerto,
    para que /oauth/google/status lo refleje;
  - 5xx o sin red -> GoogleTokenUnavailable (transitorio) y se conserva;
  - las tools devuelven el mensaje en vez de propagar la excepción.

La BD es la real de test; el endpoint de tokens de Google se sustituye por un falso.
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from app.agent.tools import calendar, drive
from app.db.session import engine
from app.integrations import google_oauth
from app.integrations.google_oauth import GoogleTokenUnavailable, NoGoogleAccount


async def _seed_expired_account() -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO oauth_accounts "
                "(provider, access_token, refresh_token, expires_at, scope) "
                "VALUES ('google', 'old-access', 'old-refresh', :exp, 'scope')"
            ),
            {"exp": datetime.now(timezone.utc) - timedelta(hours=1)},
        )


async def _stored_refresh_token() -> str | None:
    async with engine.begin() as conn:
        return (
            await conn.execute(text("SELECT refresh_token FROM oauth_accounts"))
        ).scalar_one()


def _fake_token_endpoint(monkeypatch, outcome) -> None:
    """El endpoint de tokens responde `outcome`: (status, cuerpo JSON) o una
    excepción de httpx a lanzar."""

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(outcome, type):
            raise outcome("falso", request=request)
        status, body = outcome
        return httpx.Response(status, json=body)

    real_async_client = httpx.AsyncClient

    def fake_async_client(**kwargs):
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(google_oauth.httpx, "AsyncClient", fake_async_client)


async def test_refresh_ok_returns_new_token(monkeypatch):
    await _seed_expired_account()
    _fake_token_endpoint(monkeypatch, (200, {"access_token": "new-access", "expires_in": 3600}))

    assert await google_oauth.get_valid_token() == "new-access"
    assert await _stored_refresh_token() == "old-refresh"


async def test_invalid_grant_asks_to_reconnect_and_drops_dead_token(monkeypatch):
    await _seed_expired_account()
    _fake_token_endpoint(
        monkeypatch,
        (400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}),
    )

    with pytest.raises(NoGoogleAccount, match="Reconecta"):
        await google_oauth.get_valid_token()
    assert await _stored_refresh_token() is None

    # Ya sin refresh token, el siguiente intento pide reconectar sin llamar a Google.
    with pytest.raises(NoGoogleAccount, match="Reconecta"):
        await google_oauth.get_valid_token()


@pytest.mark.parametrize(
    "outcome", [(503, {"error": "backend_error"}), httpx.ConnectError], ids=["5xx", "sin-red"]
)
async def test_transient_refresh_failure_keeps_refresh_token(monkeypatch, outcome):
    await _seed_expired_account()
    _fake_token_endpoint(monkeypatch, outcome)

    with pytest.raises(GoogleTokenUnavailable) as info:
        await google_oauth.get_valid_token()
    assert not isinstance(info.value, NoGoogleAccount)   # transitorio: no pide reconectar
    assert await _stored_refresh_token() == "old-refresh"


_EVENT = {"summary": "Reunión", "start": "2026-10-06T15:00:00", "end": "2026-10-06T16:00:00"}


@pytest.mark.parametrize(
    "module, call",
    [
        (calendar, lambda: calendar._list_events(5)),
        (calendar, lambda: calendar.perform_create_event(dict(_EVENT))),
        (drive, lambda: drive._list_files("ventas")),
        (drive, lambda: drive._ingest_file("file-id")),
    ],
    ids=["list_calendar_events", "create_calendar_event", "list_drive_files", "ingest_drive_file"],
)
async def test_google_tools_answer_instead_of_crashing(monkeypatch, module, call):
    async def unavailable() -> str:
        raise GoogleTokenUnavailable("No pude renovar el acceso a Google ahora mismo.")

    monkeypatch.setattr(module, "get_valid_token", unavailable)

    assert "No pude renovar el acceso a Google ahora mismo." in await call()
