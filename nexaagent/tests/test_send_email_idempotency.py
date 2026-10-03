"""Test de idempotencia del envío de correo (P27).

La Gmail API no admite clave de idempotencia: el mismo mensaje enviado dos veces
llega DOS veces. La única barrera es el registro-primero en sent_emails (UNIQUE
sobre idempotency_key). Si un cambio la rompe, el reintento llega a Gmail y este
test falla.

Se prueba perform_send_email (el envío real tras el "sí"), no la tool send_email,
que solo propone. Gmail y el token OAuth se sustituyen por falsos; la BD es la
real de test, porque la idempotencia vive en el UNIQUE de Postgres.
"""
import httpx
from sqlalchemy import text

from app.agent.tools import gmail
from app.db.session import engine

_ARGS = {
    "to": "juan@example.com",
    "subject": "Reunión del lunes",
    "body": "Hola Juan, nos vemos el lunes a las 10.",
}


def _fake_gmail(monkeypatch) -> list[httpx.Request]:
    """Sustituye la Gmail API por un transporte falso que anota cada petición en
    vez de enviar, y responde 200. Devuelve la lista de peticiones anotadas."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "fake-message-id"})

    real_async_client = httpx.AsyncClient

    def fake_async_client(**kwargs):
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    async def fake_token() -> str:
        return "fake-token"

    monkeypatch.setattr(gmail.httpx, "AsyncClient", fake_async_client)
    monkeypatch.setattr(gmail, "get_valid_token", fake_token)
    return calls


async def test_same_email_is_sent_only_once(monkeypatch):
    calls = _fake_gmail(monkeypatch)

    first = await gmail.perform_send_email(dict(_ARGS))
    second = await gmail.perform_send_email(dict(_ARGS))   # reintento del mismo correo

    # Gmail recibió UNA sola petición, y fue el envío.
    assert len(calls) == 1
    assert str(calls[0].url) == gmail._GMAIL_SEND_URL

    assert first == "Correo enviado a juan@example.com (asunto: 'Reunión del lunes')."
    assert second == "Ese correo ya se había enviado (no lo reenvié)."

    async with engine.begin() as conn:
        statuses = (await conn.execute(text("SELECT status FROM sent_emails"))).scalars().all()
    assert statuses == ["sent"]
