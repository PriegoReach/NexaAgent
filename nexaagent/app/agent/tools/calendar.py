"""Tools de Google Calendar: LECTURA (list_calendar_events) y ESCRITURA
(create_calendar_event, update_calendar_event, delete_calendar_event).

Escribir NO es directo: la tool PROPONE (guarda un intent en pending y devuelve la
pregunta de confirmación) y la llamada real a Google solo ocurre tras un "sí", vía
el registro CONFIRMABLE_ACTIONS (confirmable.py). Mover o borrar un evento que
tiene invitados les llega a ellos (Google les avisa), así que la pregunta lo dice.

Idempotencia LADO-RECEPTOR al crear: el event_id se deriva DETERMINÍSTICAMENTE de
la intención (summary+start+end). Insertar dos veces con el mismo id -> Google
responde 409 y NO duplica; Google es el árbitro de la unicidad (sin tabla propia).
Borrar dos veces es inocuo: el segundo DELETE da 404/410 y se reporta como "ya no
existía".

Molde compartido con el resto de tools: puente _run_async (la tool es síncrona
para langchain pero el I/O es async), httpx con timeout, y SIEMPRE devuelve un
string legible — nunca propaga una excepción al agente. El caso "no hay cuenta
conectada" se traduce a un mensaje claro, no a un 500.
"""
import asyncio
import base64
import hashlib
import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date as dt_date
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from langchain_core.tools import tool

from app.agent import pending
from app.agent.confirmable import confirmable_action
from app.agent.tools.create_task import _resolve_due  # reuso del resolver de fechas
from app.agent.tools.gmail import _EMAIL_RE
from app.core.clock import local_today
from app.core.config import settings
from app.core.log_context import conversation_id_var
from app.integrations.google_oauth import GoogleTokenUnavailable, get_valid_token

logger = logging.getLogger("nexa.tools")

_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
          "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

_CALENDAR_EVENTS_URL = (
    "https://www.googleapis.com/calendar/v3/calendars/primary/events"
)
_HTTP_TIMEOUT = 15
_CONFIRM = "Responde sí para confirmar o no para cancelar."


def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _event_url(event_id: str) -> str:
    return f"{_CALENDAR_EVENTS_URL}/{quote(event_id, safe='')}"


def _fmt_day(d: dt_date) -> str:
    return f"{_DIAS[d.weekday()]} {d.day} de {_MESES[d.month - 1]} de {d.year}"


def _event_when(event: dict) -> Optional[str]:
    """Cuándo es un evento de la API, legible, en la zona del usuario; None si no
    trae fecha."""
    start = event.get("start", {})
    # 'dateTime' para eventos con hora; 'date' para eventos de todo el día.
    raw = start.get("dateTime") or start.get("date")
    if not raw:
        return None
    try:
        if "T" in raw:  # con hora (ISO8601, p. ej. 2026-06-02T15:00:00-06:00)
            dt = datetime.fromisoformat(raw).astimezone(ZoneInfo(settings.calendar_timezone))
            return f"{_fmt_day(dt.date())} a las {dt.hour:02d}:{dt.minute:02d}"
        # Todo el día. El año es necesario: eventos anuales recurrentes (cumpleaños)
        # se expanden a instancias en años distintos y sin año parecen duplicados.
        return f"{_fmt_day(dt_date.fromisoformat(raw))} (todo el día)"
    except ValueError:
        return raw


def _fmt_event(event: dict) -> str:
    """Una línea por evento: cuándo, título y el id que piden update/delete."""
    title = event.get("summary", "(sin título)")
    when = _event_when(event)
    event_id = f" (id: {event['id']})" if event.get("id") else ""
    return f"• {when}: {title}{event_id}" if when else f"• {title}{event_id}"


def _describe(event: dict) -> str:
    title = event.get("summary", "(sin título)")
    when = _event_when(event)
    return f"'{title}' el {when}" if when else f"'{title}'"


def _google_error(exc: httpx.HTTPError, doing: str) -> str:
    """Mensaje legible para un fallo hablando con Google Calendar."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        logger.warning("calendar request failed", extra={"status": code, "doing": doing})
        if code in (401, 403):
            return f"Google rechazó {doing} (token o permisos). Reconecta la cuenta."
        return f"No pude completar {doing} ahora mismo (error de Google)."
    logger.warning("calendar http error", extra={"exc": str(exc), "doing": doing})
    return "No pude contactar a Google Calendar (problema de red)."


# ==================== LISTAR ====================
def _resolve_listing_day(phrase: str) -> Optional[dt_date]:
    """El día a consultar. Como _resolve_due, pero para CONSULTAR también valen los
    días pasados ('ayer', una fecha ISO pasada)."""
    today = local_today()
    raw = phrase.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            return dt_date.fromisoformat(raw)
        except ValueError:
            return None
    if _strip(raw) == "ayer":
        return today - timedelta(days=1)
    return _resolve_due(raw, today)


async def _list_events(max_results: int, date_phrase: Optional[str] = None, days: int = 1) -> str:
    first_day = last_day = None
    if date_phrase:
        first_day = _resolve_listing_day(date_phrase)
        if first_day is None:
            return (f"No pude entender la fecha '{date_phrase}'. Dímela como 'mañana', "
                    "'el jueves' o '2026-06-15'.")
        days = max(1, min(days, 31))
        last_day = first_day + timedelta(days=days - 1)

    try:
        token = await get_valid_token()
    except GoogleTokenUnavailable as exc:
        return str(exc)

    # singleEvents+orderBy=startTime es la combinación que Google exige para ordenar
    # por hora de inicio (expande eventos recurrentes en instancias individuales).
    params = {"maxResults": max_results, "singleEvents": "true", "orderBy": "startTime"}
    if first_day is None:
        params["timeMin"] = datetime.now(timezone.utc).isoformat()   # solo futuros
    else:
        # El día entero en la zona del usuario (sumar timedelta a un datetime con
        # ZoneInfo es aritmética de reloj de pared: medianoche a medianoche).
        tz = ZoneInfo(settings.calendar_timezone)
        start = datetime.combine(first_day, dtime(0, 0), tzinfo=tz)
        params["timeMin"] = start.isoformat()
        params["timeMax"] = (start + timedelta(days=days)).isoformat()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(_CALENDAR_EVENTS_URL, params=params, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return _google_error(exc, "la lectura del calendario")

    items = resp.json().get("items", [])
    logger.info("list_calendar_events", extra={"n": len(items), "by_date": first_day is not None})
    lines = "\n".join(_fmt_event(e) for e in items)
    if first_day is None:
        return f"Tus próximos eventos:\n{lines}" if items else "No tienes eventos próximos en tu calendario."
    if days == 1:
        span = f"el {_fmt_day(first_day)}"
        header = f"Tus eventos del {_fmt_day(first_day)}:"
    else:
        span = f"entre el {_fmt_day(first_day)} y el {_fmt_day(last_day)}"
        header = f"Tus eventos del {_fmt_day(first_day)} al {_fmt_day(last_day)}:"
    return f"{header}\n{lines}" if items else f"No tienes eventos {span}."


@tool
def list_calendar_events(date: Optional[str] = None, days: int = 1, max_results: int = 10) -> str:
    """List the user's Google Calendar events (read-only).
    Use this whenever the user asks about their meetings, appointments, events,
    agenda or schedule (e.g. "¿qué reuniones tengo?", "¿qué tengo el jueves?"), and
    before changing or deleting an event, to get its id.

    Args:
        date: A specific day EXACTLY as the user said it — "mañana", "el jueves",
            "15 de junio", "ayer" or an ISO date. Omit it to list upcoming events.
        days: How many days to list starting at `date` (e.g. 7 for "esta semana").
            Default 1.
        max_results: How many events to return at most. Default 10.
    """
    return _run_async(_list_events(max_results, date, days))


# ==================== CREAR EVENTO ====================
# Formato esperado tras el parseo: la tool resuelve la fecha (NL es / ISO) con el
# mismo _resolve_due de create_task y la hora con _resolve_time (abajo), y arma un
# datetime LOCAL sin offset (Google lo interpreta en settings.calendar_timezone).
# El intent guardado en pending lleva {summary, start, end, timezone} ya resueltos.

# 'HH', 'HH:MM', con sufijo am/pm opcional. El modelo pasa la hora COMO la dijo el
# usuario ("3pm", "15:00", "9", "8:30am"); la conversión 12h->24h la hace el código.
_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def _strip(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower().strip())
    return "".join(c for c in s if not unicodedata.combining(c))


def _resolve_time(phrase: str) -> Optional[tuple[int, int]]:
    """Resuelve una hora ('3pm', '15:00', '9', '8:30am') a (hora24, minuto).
    Determinístico (el LLM falla con aritmética). None si no se entiende."""
    if not phrase:
        return None
    s = _strip(phrase).replace(".", "").replace(" ", "")
    s = s.replace("hrs", "").rstrip("h")  # "15h"/"15hrs" -> "15"
    # tolera "3pm"/"3:00pm"/"15"/"15:00" (sufijo am/pm opcional)
    m = _TIME_RE.match(s)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ap = m.group(3)
    if ap == "pm" and hh != 12:
        hh += 12
    elif ap == "am" and hh == 12:
        hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def _event_id(summary: str, start: str, end: str) -> str:
    """ID DETERMINÍSTICO y válido para Google Calendar.

    Reglas de Google para el id de un evento: codificación base32hex, es decir el
    id solo puede usar los caracteres 'a'-'v' y '0'-'9', longitud 5-1024.
    Tomamos sha1(summary|start|end) (20 bytes), lo codificamos en base32hex
    (alfabeto 0-9A-V), lo pasamos a minúsculas (-> 0-9a-v, EXACTAMENTE el rango
    permitido) y quitamos el padding '='. 20 bytes -> 32 chars exactos, sin padding,
    dentro del rango de longitud. Misma intención -> mismo id -> Google no duplica.
    """
    basis = f"{summary.strip().lower()}|{start.strip()}|{end.strip()}"
    digest = hashlib.sha1(basis.encode("utf-8")).digest()
    return base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()


def _fmt_when(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    return f"{_fmt_day(dt.date())} a las {dt.hour:02d}:{dt.minute:02d}"


@confirmable_action("create_calendar_event")
async def perform_create_event(args: dict) -> str:
    """El INSERT REAL en Google Calendar (events.insert sobre 'primary').

    Ejecutor confirmable: firma (args: dict) -> str. `args` trae
    {summary, start, end, timezone, attendees?} ya resueltos por la tool. NO lo llama
    el modelo: lo invoca la rama de confirmación del orquestador tras un "sí".

    Idempotencia lado-receptor: pasamos un `id` determinístico (_event_id). Si ya
    existe (segundo intento con la MISMA intención), Google responde 409 y lo
    tratamos como "ya estaba creado" (no error, no duplicado).
    """
    summary = args["summary"]
    start = args["start"]
    end = args["end"]
    tz = args.get("timezone", settings.calendar_timezone)
    attendees = args.get("attendees") or []

    try:
        token = await get_valid_token()
    except GoogleTokenUnavailable as exc:
        return f"No creé el evento. {exc}"

    event_id = _event_id(summary, start, end)
    body = {
        "id": event_id,
        "summary": summary,
        # dateTime SIN offset + timeZone -> Google lo ubica en la zona (sin que
        # hagamos aritmética de offsets a mano).
        "start": {"dateTime": start, "timeZone": tz},
        "end": {"dateTime": end, "timeZone": tz},
    }
    if attendees:
        body["attendees"] = [{"email": email} for email in attendees]
    # Con invitados, Google les manda la invitación por correo (lo dijo la pregunta).
    params = {"sendUpdates": "all" if attendees else "none"}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(_CALENDAR_EVENTS_URL, json=body, params=params, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            # El id ya existe -> la intención ya se materializó. NO es error y NO duplica.
            logger.info("calendar event already exists (idempotent 409)",
                        extra={"event_id": event_id})
            return f"El evento '{summary}' ya estaba creado (no lo dupliqué)."
        return _google_error(exc, "la creación del evento")
    except httpx.HTTPError as exc:
        return _google_error(exc, "la creación del evento")

    logger.info("calendar event created", extra={"event_id": event_id, "attendees": len(attendees)})
    invited = f" Invité a {', '.join(attendees)}." if attendees else ""
    return f"Evento creado: '{summary}' — {_fmt_when(start)}.{invited}"


async def _propose_event(summary: str, date_phrase: str, time_phrase: str,
                         duration_minutes: int, conversation_id: Optional[int],
                         attendees: Optional[list[str]] = None) -> str:
    """Resuelve fecha+hora, arma el intent y lo guarda en pending. NO crea el
    evento: la acción externa pasa por confirmación igual que delete_task."""
    attendees = [a.strip() for a in (attendees or []) if a and a.strip()]
    invalid = [a for a in attendees if not _EMAIL_RE.match(a)]
    if invalid:
        return (f"Esto no parece una dirección de correo válida: {', '.join(invalid)}. "
                "Dame los correos de los invitados con el formato nombre@dominio.com.")
    resolved = _resolve_due(date_phrase, local_today())
    if resolved is None:
        return (f"No pude entender la fecha '{date_phrase}' (o ya pasó). "
                "Dime una fecha futura, p. ej. 'mañana' o '2026-06-15'.")
    t = _resolve_time(time_phrase)
    if t is None:
        return (f"No pude entender la hora '{time_phrase}'. "
                "Dímela como '15:00' o '3pm'.")
    hh, mm = t
    start_dt = datetime.combine(resolved, dtime(hh, mm))
    end_dt = start_dt + timedelta(minutes=max(1, duration_minutes))
    start = start_dt.isoformat()          # 'YYYY-MM-DDTHH:MM:SS' (sin offset)
    end = end_dt.isoformat()

    args = {
        "summary": summary.strip(),
        "start": start,
        "end": end,
        "timezone": settings.calendar_timezone,
    }
    event = f"'{summary.strip()}' el {_fmt_when(start)}"
    if attendees:
        args["attendees"] = attendees
        question = (f"¿Creo el evento {event} e invito a {', '.join(attendees)}? Google les "
                    f"enviará la invitación por correo. {_CONFIRM}")
    else:
        question = f"¿Creo el evento {event}? {_CONFIRM}"
    if conversation_id is not None:
        existing = await pending.set_pending(
            conversation_id,
            {
                "action": "create_calendar_event",
                "args": args,
                "description": f"la creación del evento {event}",
                "question": question,   # texto LITERAL para el override
            },
        )
        if existing is not None:
            return pending.busy_message(existing)
    logger.info("create_calendar_event proposed", extra={"event_id": _event_id(summary, start, end)})
    return question


@tool
def create_calendar_event(summary: str, date: str, time: str,
                          duration_minutes: int = 60,
                          attendees: Optional[list[str]] = None) -> str:
    """Propose creating an event in the user's Google Calendar. Use this whenever
    the user wants to schedule/agendar a meeting, appointment or event
    (e.g. "agenda una reunión mañana a las 3pm", "crea un evento el viernes a las 9").
    This does NOT create the event directly: it asks the user to confirm first, and
    the event is created only after they agree.

    Args:
        summary: The event title, in plain language (e.g. "reunión de equipo").
        date: The date EXACTLY as the user expressed it — "mañana", "el viernes",
            "15 de junio" or an ISO date. Do NOT compute or convert it.
        time: The start time as the user said it — "3pm", "15:00", "9". Do NOT
            convert 12h/24h; the tool handles it.
        duration_minutes: Event length in minutes. Default 60.
        attendees: Email addresses to invite, ONLY if the user asked to invite
            people. Google emails them the invitation.
    """
    # conversation_id del contextvar que fija run_agent (no es arg del modelo). Sin
    # él se propone sin pendiente (fail-safe: un 'sí' posterior no halla nada que
    # ejecutar -> no crea) — mismo criterio que delete_task.
    return _run_async(
        _propose_event(summary, date, time, duration_minutes, _conversation_id(), attendees)
    )


def _conversation_id() -> Optional[int]:
    cid_raw = conversation_id_var.get()
    return int(cid_raw) if cid_raw and cid_raw != "-" else None


# ==================== CAMBIAR Y BORRAR ====================
_NOT_FOUND = ("No encontré ese evento en tu calendario. Pídeme la lista de eventos "
              "para ver los ids.")


async def _fetch_event(event_id: str) -> dict | str:
    """El evento actual (para mostrar en la pregunta qué se cambia o borra), o un
    mensaje si no se puede leer."""
    try:
        token = await get_valid_token()
    except GoogleTokenUnavailable as exc:
        return str(exc)
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(_event_url(event_id),
                                    headers={"Authorization": f"Bearer {token}"})
            if resp.status_code in (404, 410):
                return _NOT_FOUND
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return _google_error(exc, "la lectura del evento")
    event = resp.json()
    return _NOT_FOUND if event.get("status") == "cancelled" else event


def _local_bounds(event: dict) -> Optional[tuple[datetime, datetime]]:
    """(inicio, fin) en hora local sin zona; None si el evento es de todo el día."""
    start = event.get("start", {}).get("dateTime")
    end = event.get("end", {}).get("dateTime")
    if not start or not end:
        return None
    tz = ZoneInfo(settings.calendar_timezone)
    return (datetime.fromisoformat(start).astimezone(tz).replace(tzinfo=None),
            datetime.fromisoformat(end).astimezone(tz).replace(tzinfo=None))


async def _store_proposal(conversation_id: Optional[int], intent: dict) -> Optional[str]:
    """Guarda la propuesta; devuelve el aviso para el modelo si ya había otra."""
    if conversation_id is None:
        return None
    existing = await pending.set_pending(conversation_id, intent)
    return pending.busy_message(existing) if existing is not None else None


async def _propose_update(event_id: str, date_phrase: Optional[str], time_phrase: Optional[str],
                          duration_minutes: Optional[int], summary: Optional[str],
                          conversation_id: Optional[int]) -> str:
    event = await _fetch_event(event_id)
    if isinstance(event, str):
        return event
    old_title = event.get("summary", "(sin título)")
    new_title = summary.strip() if summary and summary.strip() and summary.strip() != old_title else None

    changes = []
    start = end = None
    bounds = _local_bounds(event)
    if bounds is None:
        if date_phrase or time_phrase or duration_minutes:
            return (f"'{old_title}' es un evento de todo el día; por ahora solo puedo "
                    "cambiarle el título.")
    else:
        old_start, old_end = bounds
        day = old_start.date()
        if date_phrase:
            day = _resolve_due(date_phrase, local_today())
            if day is None:
                return (f"No pude entender la fecha '{date_phrase}' (o ya pasó). "
                        "Dime una fecha futura, p. ej. 'mañana' o '2026-06-15'.")
        hh, mm = old_start.hour, old_start.minute
        if time_phrase:
            t = _resolve_time(time_phrase)
            if t is None:
                return (f"No pude entender la hora '{time_phrase}'. "
                        "Dímela como '15:00' o '3pm'.")
            hh, mm = t
        length = timedelta(minutes=max(1, duration_minutes)) if duration_minutes else old_end - old_start
        new_start = datetime.combine(day, dtime(hh, mm))
        new_end = new_start + length
        if new_start != old_start:
            change = f"pasarlo del {_fmt_when(old_start.isoformat())} al {_fmt_when(new_start.isoformat())}"
            if length != old_end - old_start:
                change += f" (hasta las {new_end:%H:%M})"
            changes.append(change)
        elif new_end != old_end:
            changes.append(f"que termine a las {new_end:%H:%M} en vez de a las {old_end:%H:%M}")
        if changes:
            start, end = new_start.isoformat(), new_end.isoformat()

    if new_title is not None:
        changes.append(f"llamarlo '{new_title}'")
    if not changes:
        return f"No hay nada que cambiar en '{old_title}'."
    notify = bool(event.get("attendees"))
    notice = " Google avisará a los invitados." if notify else ""
    question = f"¿Cambio el evento '{old_title}' para {' y '.join(changes)}?{notice} {_CONFIRM}"
    busy = await _store_proposal(conversation_id, {
        "action": "update_calendar_event",
        "args": {"event_id": event_id, "summary": new_title, "start": start, "end": end,
                 "timezone": settings.calendar_timezone, "notify": notify},
        "description": f"el cambio del evento '{old_title}'",
        "question": question,
    })
    if busy:
        return busy
    logger.info("update_calendar_event proposed", extra={"event_id": event_id})
    return question


@confirmable_action("update_calendar_event")
async def perform_update_event(args: dict) -> str:
    """El PATCH REAL en Google Calendar, tras un "sí". `args` trae {event_id,
    summary?, start?, end?, timezone, notify}."""
    body: dict = {}
    if args.get("summary"):
        body["summary"] = args["summary"]
    if args.get("start"):
        tz = args.get("timezone", settings.calendar_timezone)
        body["start"] = {"dateTime": args["start"], "timeZone": tz}
        body["end"] = {"dateTime": args["end"], "timeZone": tz}
    try:
        token = await get_valid_token()
    except GoogleTokenUnavailable as exc:
        return f"No cambié el evento. {exc}"
    params = {"sendUpdates": "all" if args.get("notify") else "none"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.patch(_event_url(args["event_id"]), json=body, params=params,
                                      headers={"Authorization": f"Bearer {token}"})
            if resp.status_code in (404, 410):
                return "Ese evento ya no existe en tu calendario; no cambié nada."
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return _google_error(exc, "el cambio del evento")
    logger.info("calendar event updated", extra={"event_id": args["event_id"]})
    return f"Evento actualizado: {_describe(resp.json())}."


async def _propose_delete(event_id: str, conversation_id: Optional[int]) -> str:
    event = await _fetch_event(event_id)
    if isinstance(event, str):
        return event
    described = _describe(event)
    notify = bool(event.get("attendees"))
    notice = " Google avisará a los invitados." if notify else ""
    question = f"¿Borro el evento {described}?{notice} {_CONFIRM}"
    busy = await _store_proposal(conversation_id, {
        "action": "delete_calendar_event",
        "args": {"event_id": event_id, "title": event.get("summary", "(sin título)"),
                 "notify": notify},
        "description": f"el borrado del evento {described}",
        "question": question,
    })
    if busy:
        return busy
    logger.info("delete_calendar_event proposed", extra={"event_id": event_id})
    return question


@confirmable_action("delete_calendar_event")
async def perform_delete_event(args: dict) -> str:
    """El DELETE REAL en Google Calendar, tras un "sí". `args` trae {event_id,
    title, notify}. Borrar algo que ya no está es inocuo: se dice y ya."""
    try:
        token = await get_valid_token()
    except GoogleTokenUnavailable as exc:
        return f"No borré el evento. {exc}"
    params = {"sendUpdates": "all" if args.get("notify") else "none"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.delete(_event_url(args["event_id"]), params=params,
                                       headers={"Authorization": f"Bearer {token}"})
            if resp.status_code in (404, 410):
                return f"El evento '{args['title']}' ya no existía (no borré nada)."
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return _google_error(exc, "el borrado del evento")
    logger.info("calendar event deleted", extra={"event_id": args["event_id"]})
    return f"Evento borrado: '{args['title']}'."


@tool
def update_calendar_event(event_id: str, date: Optional[str] = None, time: Optional[str] = None,
                          duration_minutes: Optional[int] = None,
                          summary: Optional[str] = None) -> str:
    """Propose changing an existing event in the user's Google Calendar: move it to
    another day or time, change its length, or rename it. Call list_calendar_events
    first to get the event_id. It asks the user to confirm before changing anything.

    Args:
        event_id: The id shown by list_calendar_events.
        date: The new day EXACTLY as the user said it ("el viernes", "mañana").
            Omit to keep the current day.
        time: The new start time as the user said it ("5pm", "10:30"). Omit to keep it.
        duration_minutes: The new length in minutes. Omit to keep it.
        summary: The new title. Omit to keep it.
    """
    return _run_async(_propose_update(event_id, date, time, duration_minutes, summary,
                                      _conversation_id()))


@tool
def delete_calendar_event(event_id: str) -> str:
    """Propose deleting an event from the user's Google Calendar. Call
    list_calendar_events first to get the event_id. It asks the user to confirm
    before deleting.

    Args:
        event_id: The id shown by list_calendar_events.
    """
    return _run_async(_propose_delete(event_id, _conversation_id()))
