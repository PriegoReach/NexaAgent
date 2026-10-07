"""Tests de Calendar completo: consultar por día, invitar al crear, cambiar y borrar.

Antes solo se podían ver los próximos 10 eventos ("¿qué tengo el jueves?" fallaba
si el jueves quedaba lejos) y crear eventos sin invitados; no había forma de mover
ni borrar uno. Ahora list_calendar_events acepta un día, create_calendar_event
invitados, y update/delete proponen y piden confirmación como el resto.

Google Calendar se sustituye por un doble (httpx.MockTransport); el reloj se fija
en las 19:30 del lunes 5 de octubre de 2026 en Ciudad de México.
"""
from datetime import datetime, timezone

import httpx
import pytest

from app.agent import pending
from app.agent.tools import calendar
from app.core import clock
from app.core.config import settings

_EVENT = {
    "id": "ev1",
    "summary": "Reunión",
    "start": {"dateTime": "2026-10-06T15:00:00-06:00"},
    "end": {"dateTime": "2026-10-06T16:00:00-06:00"},
}
_CONFIRM = "Responde sí para confirmar o no para cancelar."


@pytest.fixture
def google(monkeypatch):
    """Google Calendar falso. `g["routes"][(MÉTODO, ruta)]` fija la respuesta;
    `g["requests"]` anota cada petición; `g["pending"]` la propuesta guardada."""
    g = {"routes": {}, "requests": [], "pending": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        g["requests"].append({
            "method": request.method,
            "path": request.url.path,
            "params": dict(request.url.params),
            "json": httpx.Response(200, content=body).json() if body else None,
        })
        return g["routes"].get((request.method, request.url.path)) or httpx.Response(200, json={"items": []})

    real_async_client = httpx.AsyncClient

    def fake_async_client(**kwargs):
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    async def fake_token() -> str:
        return "fake-token"

    async def fake_set_pending(conversation_id: int, intent: dict):
        g["pending"].append(intent)
        return None

    monkeypatch.setattr(calendar.httpx, "AsyncClient", fake_async_client)
    monkeypatch.setattr(calendar, "get_valid_token", fake_token)
    monkeypatch.setattr(pending, "set_pending", fake_set_pending)
    monkeypatch.setattr(settings, "calendar_timezone", "America/Mexico_City")
    monkeypatch.setattr(clock, "_utcnow", lambda: datetime(2026, 10, 6, 1, 30, tzinfo=timezone.utc))
    return g


_EVENTS_PATH = "/calendar/v3/calendars/primary/events"
_EV1_PATH = f"{_EVENTS_PATH}/ev1"


# --- Consultar por día -------------------------------------------------------
async def test_list_a_day_asks_google_for_that_whole_day(google):
    google["routes"][("GET", _EVENTS_PATH)] = httpx.Response(200, json={"items": [
        {"id": "ev9", "summary": "Dentista", "start": {"dateTime": "2026-10-06T16:00:00Z"}},
    ]})

    reply = await calendar._list_events(10, "mañana")

    params = google["requests"][0]["params"]
    assert (params["timeMin"], params["timeMax"]) == ("2026-10-06T00:00:00-06:00", "2026-10-07T00:00:00-06:00")
    # La hora se muestra en la zona del usuario (16:00 UTC = 10:00 en CDMX).
    assert reply == ("Tus eventos del martes 6 de octubre de 2026:\n"
                     "• martes 6 de octubre de 2026 a las 10:00: Dentista (id: ev9)")


async def test_list_several_days(google):
    reply = await calendar._list_events(10, "mañana", 7)

    assert google["requests"][0]["params"]["timeMax"] == "2026-10-13T00:00:00-06:00"
    assert reply == ("No tienes eventos entre el martes 6 de octubre de 2026 y el "
                     "lunes 12 de octubre de 2026.")


@pytest.mark.parametrize("phrase, day", [("ayer", "2026-10-04"), ("2026-09-01", "2026-09-01")])
async def test_list_past_days_too(google, phrase, day):
    await calendar._list_events(10, phrase)

    assert google["requests"][0]["params"]["timeMin"] == f"{day}T00:00:00-06:00"


async def test_list_with_a_date_it_cannot_read(google):
    reply = await calendar._list_events(10, "cuando pueda")

    assert reply.startswith("No pude entender la fecha 'cuando pueda'")
    assert google["requests"] == []


async def test_list_upcoming_without_a_date(google):
    google["routes"][("GET", _EVENTS_PATH)] = httpx.Response(200, json={"items": [_EVENT]})

    reply = await calendar._list_events(10)

    assert "timeMax" not in google["requests"][0]["params"]
    assert reply == "Tus próximos eventos:\n• martes 6 de octubre de 2026 a las 15:00: Reunión (id: ev1)"


# --- Crear con invitados -----------------------------------------------------
async def test_create_with_attendees_says_so_and_invites_them(google):
    question = await calendar._propose_event("Revisión", "mañana", "10am", 30, 1, ["ana@example.com"])

    assert question == ("¿Creo el evento 'Revisión' el martes 6 de octubre de 2026 a las 10:00 e "
                        "invito a ana@example.com? Google les enviará la invitación por correo. "
                        f"{_CONFIRM}")
    reply = await calendar.perform_create_event(google["pending"][0]["args"])

    sent = google["requests"][0]
    assert sent["json"]["attendees"] == [{"email": "ana@example.com"}]
    assert sent["params"] == {"sendUpdates": "all"}
    assert reply == ("Evento creado: 'Revisión' — martes 6 de octubre de 2026 a las 10:00. "
                     "Invité a ana@example.com.")


async def test_create_without_attendees_notifies_nobody(google):
    await calendar._propose_event("Gimnasio", "mañana", "7am", 60, 1)
    await calendar.perform_create_event(google["pending"][0]["args"])

    sent = google["requests"][0]
    assert "attendees" not in sent["json"]
    assert sent["params"] == {"sendUpdates": "none"}


async def test_create_rejects_an_invalid_attendee(google):
    reply = await calendar._propose_event("Revisión", "mañana", "10am", 30, 1, ["ana arroba example"])

    assert reply.startswith("Esto no parece una dirección de correo válida: ana arroba example.")
    assert google["pending"] == []


# --- Cambiar -----------------------------------------------------------------
async def test_update_proposes_then_moves_the_event(google):
    google["routes"][("GET", _EV1_PATH)] = httpx.Response(200, json=_EVENT)

    question = await calendar._propose_update("ev1", None, "5pm", None, None, 1)

    assert question == ("¿Cambio el evento 'Reunión' para pasarlo del martes 6 de octubre de 2026 "
                        "a las 15:00 al martes 6 de octubre de 2026 a las 17:00? " + _CONFIRM)
    args = google["pending"][0]["args"]
    assert (args["start"], args["end"], args["notify"]) == ("2026-10-06T17:00:00", "2026-10-06T18:00:00", False)

    google["routes"][("PATCH", _EV1_PATH)] = httpx.Response(200, json={
        **_EVENT, "start": {"dateTime": "2026-10-06T17:00:00-06:00"}})
    reply = await calendar.perform_update_event(args)

    patch = google["requests"][-1]
    assert (patch["method"], patch["path"], patch["params"]) == ("PATCH", _EV1_PATH, {"sendUpdates": "none"})
    assert patch["json"]["start"] == {"dateTime": "2026-10-06T17:00:00", "timeZone": "America/Mexico_City"}
    assert reply == "Evento actualizado: 'Reunión' el martes 6 de octubre de 2026 a las 17:00."


async def test_update_only_the_length(google):
    google["routes"][("GET", _EV1_PATH)] = httpx.Response(200, json=_EVENT)

    question = await calendar._propose_update("ev1", None, None, 90, None, 1)

    assert question.startswith("¿Cambio el evento 'Reunión' para que termine a las 16:30 en vez de a las 16:00?")


async def test_update_an_event_with_attendees_warns_and_notifies(google):
    google["routes"][("GET", _EV1_PATH)] = httpx.Response(200, json={
        **_EVENT, "attendees": [{"email": "ana@example.com"}]})

    question = await calendar._propose_update("ev1", "el viernes", None, None, "Revisión", 1)

    assert "al viernes 9 de octubre de 2026 a las 15:00 y llamarlo 'Revisión'?" in question
    assert "Google avisará a los invitados." in question
    await calendar.perform_update_event(google["pending"][0]["args"])
    assert google["requests"][-1]["params"] == {"sendUpdates": "all"}


async def test_update_with_nothing_to_change(google):
    google["routes"][("GET", _EV1_PATH)] = httpx.Response(200, json=_EVENT)

    assert await calendar._propose_update("ev1", None, "3pm", None, "Reunión", 1) == (
        "No hay nada que cambiar en 'Reunión'.")
    assert google["pending"] == []


async def test_all_day_events_can_only_be_renamed(google):
    google["routes"][("GET", _EV1_PATH)] = httpx.Response(200, json={
        "id": "ev1", "summary": "Vacaciones", "start": {"date": "2026-10-06"}, "end": {"date": "2026-10-07"}})

    reply = await calendar._propose_update("ev1", None, "5pm", None, None, 1)

    assert reply == "'Vacaciones' es un evento de todo el día; por ahora solo puedo cambiarle el título."


# --- Borrar ------------------------------------------------------------------
async def test_delete_proposes_then_deletes(google):
    google["routes"][("GET", _EV1_PATH)] = httpx.Response(200, json=_EVENT)

    question = await calendar._propose_delete("ev1", 1)

    assert question == f"¿Borro el evento 'Reunión' el martes 6 de octubre de 2026 a las 15:00? {_CONFIRM}"
    google["routes"][("DELETE", _EV1_PATH)] = httpx.Response(204)
    reply = await calendar.perform_delete_event(google["pending"][0]["args"])

    delete = google["requests"][-1]
    assert (delete["method"], delete["params"]) == ("DELETE", {"sendUpdates": "none"})
    assert reply == "Evento borrado: 'Reunión'."


async def test_deleting_twice_is_harmless(google):
    google["routes"][("DELETE", _EV1_PATH)] = httpx.Response(410)

    reply = await calendar.perform_delete_event({"event_id": "ev1", "title": "Reunión", "notify": False})

    assert reply == "El evento 'Reunión' ya no existía (no borré nada)."


async def test_proposals_on_a_missing_event(google):
    google["routes"][("GET", _EV1_PATH)] = httpx.Response(404)

    assert await calendar._propose_delete("ev1", 1) == calendar._NOT_FOUND
    assert await calendar._propose_update("ev1", None, "5pm", None, None, 1) == calendar._NOT_FOUND
    assert google["pending"] == []
