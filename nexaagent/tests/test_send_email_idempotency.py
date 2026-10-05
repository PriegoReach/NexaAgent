"""Test de idempotencia del envío de correo (P27).

La Gmail API no admite clave de idempotencia: el mismo mensaje enviado dos veces
llega DOS veces. La única barrera es el registro-primero en sent_emails (UNIQUE
sobre idempotency_key). Si un cambio la rompe, el reintento llega a Gmail y este
test falla.

La barrera no debe pasarse de largo: un correo que falló SIN salir (sin cuenta, 4xx,
sin red) se puede volver a pedir, y uno cuyo resultado es incierto (5xx, timeout) no
se reenvía pero tampoco se da por enviado.

Se prueba perform_send_email (el envío real tras el "sí"), no la tool send_email,
que solo propone. Gmail y el token OAuth se sustituyen por falsos; la BD es la
real de test, porque la idempotencia vive en el UNIQUE de Postgres.
"""
import httpx
import pytest
from sqlalchemy import text

from app.agent.tools import gmail
from app.db.session import engine
from app.integrations.google_oauth import NoGoogleAccount

_ARGS = {
    "to": "juan@example.com",
    "subject": "Reunión del lunes",
    "body": "Hola Juan, nos vemos el lunes a las 10.",
}
_SENT = "Correo enviado a juan@example.com (asunto: 'Reunión del lunes')."


def _fake_gmail(monkeypatch, *outcomes) -> list[httpx.Request]:
    """Sustituye la Gmail API por un transporte falso que anota cada petición en
    vez de enviar. Cada petición consume el siguiente outcome (un status HTTP o una
    excepción de httpx a lanzar); agotados, responde 200. Devuelve las peticiones."""
    calls: list[httpx.Request] = []
    queue = list(outcomes)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        outcome = queue.pop(0) if queue else 200
        if isinstance(outcome, type):
            raise outcome("falso", request=request)
        return httpx.Response(outcome, json={"id": "fake-message-id"})

    real_async_client = httpx.AsyncClient

    def fake_async_client(**kwargs):
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    async def fake_token() -> str:
        return "fake-token"

    monkeypatch.setattr(gmail.httpx, "AsyncClient", fake_async_client)
    monkeypatch.setattr(gmail, "get_valid_token", fake_token)
    return calls


async def _statuses() -> list[str]:
    async with engine.begin() as conn:
        return (await conn.execute(text("SELECT status FROM sent_emails"))).scalars().all()


async def test_same_email_is_sent_only_once(monkeypatch):
    calls = _fake_gmail(monkeypatch)

    first = await gmail.perform_send_email(dict(_ARGS))
    second = await gmail.perform_send_email(dict(_ARGS))   # reintento del mismo correo

    # Gmail recibió UNA sola petición, y fue el envío.
    assert len(calls) == 1
    assert str(calls[0].url) == gmail._GMAIL_SEND_URL

    assert first == _SENT
    assert second == "Ese correo ya se había enviado (no lo reenvié)."

    assert await _statuses() == ["sent"]


async def test_email_before_connecting_google_is_sent_after_connecting(monkeypatch):
    """Pedir un correo sin cuenta conectada, conectarla y repetirlo. Antes, el
    reintento respondía "ya se había enviado" sin haberlo enviado nunca."""
    calls = _fake_gmail(monkeypatch)
    failures = [NoGoogleAccount("No hay una cuenta de Google conectada.")]

    async def token_after_connecting() -> str:
        if failures:
            raise failures.pop()
        return "fake-token"

    monkeypatch.setattr(gmail, "get_valid_token", token_after_connecting)

    first = await gmail.perform_send_email(dict(_ARGS))
    assert first.startswith("El correo no se envió.")
    assert calls == []                      # ni siquiera se intentó
    assert await _statuses() == ["failed"]

    second = await gmail.perform_send_email(dict(_ARGS))
    assert second == _SENT
    assert len(calls) == 1
    assert await _statuses() == ["sent"]    # la misma fila, reclamada


@pytest.mark.parametrize(
    "outcome", [400, 403, 429, httpx.ConnectError], ids=["400", "403", "429", "sin-red"]
)
async def test_definitive_failure_can_be_retried(monkeypatch, outcome):
    """Gmail rechazó la petición o la conexión ni se abrió: el correo no salió, así
    que el mismo correo se puede volver a pedir y llega una sola vez."""
    calls = _fake_gmail(monkeypatch, outcome)

    first = await gmail.perform_send_email(dict(_ARGS))
    assert "no se envió" in first
    assert await _statuses() == ["failed"]

    second = await gmail.perform_send_email(dict(_ARGS))
    assert second == _SENT
    assert len(calls) == 2                  # el intento fallido + el envío
    assert await _statuses() == ["sent"]


@pytest.mark.parametrize("outcome", [500, httpx.ReadTimeout], ids=["5xx", "timeout"])
async def test_uncertain_failure_is_not_resent_nor_reported_as_sent(monkeypatch, outcome):
    """La petición pudo llegar a Gmail: no se reenvía (sesgo a no-duplicar), pero el
    usuario sabe que debe comprobarlo, no que "ya se envió"."""
    calls = _fake_gmail(monkeypatch, outcome)

    first = await gmail.perform_send_email(dict(_ARGS))
    second = await gmail.perform_send_email(dict(_ARGS))

    assert first == gmail._UNCERTAIN_MSG
    assert len(calls) == 1                  # el reintento NO llegó a Gmail
    assert "no sé si llegó a salir" in second
    assert "ya se había enviado" not in second
    assert await _statuses() == ["uncertain"]
