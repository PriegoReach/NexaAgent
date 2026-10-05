"""Tools de Google Calendar: LECTURA (list_calendar_events) y ESCRITURA
(create_calendar_event), la primera acción externa irreversible.

Crear un evento NO es directo: la tool PROPONE (guarda un intent en pending y
devuelve la pregunta de confirmación) y el INSERT real solo ocurre tras un "sí",
vía el registro CONFIRMABLE_ACTIONS (confirmable.py).

Idempotencia LADO-RECEPTOR: el event_id se deriva DETERMINÍSTICAMENTE de la
intención (summary+start+end). Insertar dos veces con el mismo id -> Google
responde 409 y NO duplica; Google es el árbitro de la unicidad (sin tabla propia).

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
from datetime import datetime, date, time as dtime, timedelta, timezone
from typing import Optional

import httpx
from langchain_core.tools import tool

from app.agent import pending
from app.agent.confirmable import confirmable_action
from app.agent.tools.create_task import _resolve_due  # reuso del resolver de fechas
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


def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _fmt_event(event: dict) -> str:
    """Formatea un evento de la API a una línea legible: fecha, hora, título."""
    title = event.get("summary", "(sin título)")
    start = event.get("start", {})
    # 'dateTime' para eventos con hora; 'date' para eventos de todo el día.
    raw = start.get("dateTime") or start.get("date")
    if not raw:
        return f"• {title}"
    try:
        if "T" in raw:  # con hora (ISO8601, p. ej. 2026-06-02T15:00:00-06:00)
            dt = datetime.fromisoformat(raw)
            cuando = (
                f"{_DIAS[dt.weekday()]} {dt.day} de {_MESES[dt.month - 1]} de {dt.year} "
                f"a las {dt.hour:02d}:{dt.minute:02d}"
            )
        else:  # todo el día (solo fecha)
            dt = datetime.fromisoformat(raw)
            # El año es necesario: eventos anuales recurrentes (cumpleaños) se
            # expanden a instancias en años distintos y sin año parecen duplicados.
            cuando = (
                f"{_DIAS[dt.weekday()]} {dt.day} de {_MESES[dt.month - 1]} "
                f"de {dt.year} (todo el día)"
            )
    except ValueError:
        cuando = raw
    return f"• {cuando}: {title}"


async def _list_events(max_results: int) -> str:
    try:
        token = await get_valid_token()
    except GoogleTokenUnavailable as exc:
        return str(exc)

    # timeMin = ahora -> solo eventos futuros. singleEvents+orderBy=startTime es
    # la combinación que Google exige para ordenar por hora de inicio (expande
    # eventos recurrentes en instancias individuales).
    params = {
        "timeMin": datetime.now(timezone.utc).isoformat(),
        "maxResults": max_results,
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(_CALENDAR_EVENTS_URL, params=params, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("calendar list failed", extra={"status": exc.response.status_code})
        if exc.response.status_code in (401, 403):
            return ("Google rechazó el acceso al calendario (token o permisos). "
                    "Reconecta la cuenta.")
        return "No pude leer el calendario ahora mismo (error de Google)."
    except httpx.HTTPError as exc:
        logger.warning("calendar list http error", extra={"exc": str(exc)})
        return "No pude contactar a Google Calendar (problema de red)."

    items = resp.json().get("items", [])
    logger.info("list_calendar_events", extra={"n": len(items)})
    if not items:
        return "No tienes eventos próximos en tu calendario."
    return "Tus próximos eventos:\n" + "\n".join(_fmt_event(e) for e in items)


@tool
def list_calendar_events(max_results: int = 10) -> str:
    """List the user's upcoming Google Calendar events (read-only).
    Use this whenever the user asks about their meetings, appointments, events,
    agenda or schedule (e.g. "¿qué reuniones tengo?", "qué tengo agendado").

    Args:
        max_results: How many upcoming events to return. Default 10.
    """
    return _run_async(_list_events(max_results))


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
    return (
        f"{_DIAS[dt.weekday()]} {dt.day} de {_MESES[dt.month - 1]} de {dt.year} "
        f"a las {dt.hour:02d}:{dt.minute:02d}"
    )


@confirmable_action("create_calendar_event")
async def perform_create_event(args: dict) -> str:
    """El INSERT REAL en Google Calendar (events.insert sobre 'primary').

    Ejecutor confirmable: firma (args: dict) -> str. `args` trae
    {summary, start, end, timezone} ya resueltos por la tool. NO lo llama el modelo:
    lo invoca la rama de confirmación del orquestador tras un "sí".

    Idempotencia lado-receptor: pasamos un `id` determinístico (_event_id). Si ya
    existe (segundo intento con la MISMA intención), Google responde 409 y lo
    tratamos como "ya estaba creado" (no error, no duplicado).
    """
    summary = args["summary"]
    start = args["start"]
    end = args["end"]
    tz = args.get("timezone", settings.calendar_timezone)

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
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(_CALENDAR_EVENTS_URL, json=body, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 409:
            # El id ya existe -> la intención ya se materializó. NO es error y NO duplica.
            logger.info("calendar event already exists (idempotent 409)",
                        extra={"event_id": event_id})
            return f"El evento '{summary}' ya estaba creado (no lo dupliqué)."
        logger.warning("calendar insert failed",
                       extra={"status": code, "event_id": event_id})
        if code in (401, 403):
            return ("Google rechazó la creación del evento (token o permisos). "
                    "Reconecta la cuenta.")
        return "No pude crear el evento ahora mismo (error de Google)."
    except httpx.HTTPError as exc:
        logger.warning("calendar insert http error", extra={"exc": str(exc)})
        return "No pude contactar a Google Calendar (problema de red)."

    logger.info("calendar event created", extra={"event_id": event_id})
    return f"Evento creado: '{summary}' — {_fmt_when(start)}."


async def _propose_event(summary: str, date_phrase: str, time_phrase: str,
                         duration_minutes: int, conversation_id: Optional[int]) -> str:
    """Resuelve fecha+hora, arma el intent y lo guarda en pending. NO crea el
    evento: la acción externa pasa por confirmación igual que delete_task."""
    resolved = _resolve_due(date_phrase, date.today())
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
    question = (f"¿Creo el evento {event}? "
                "Responde sí para confirmar o no para cancelar.")
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
                          duration_minutes: int = 60) -> str:
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
    """
    # conversation_id del contextvar que fija run_agent (no es arg del modelo). Sin
    # él se propone sin pendiente (fail-safe: un 'sí' posterior no halla nada que
    # ejecutar -> no crea) — mismo criterio que delete_task.
    cid_raw = conversation_id_var.get()
    conversation_id = int(cid_raw) if cid_raw and cid_raw != "-" else None
    return _run_async(
        _propose_event(summary, date, time, duration_minutes, conversation_id)
    )
