"""Fecha de hoy en la zona horaria del usuario.

El contenedor corre en UTC, así que date.today() da la fecha UTC: en Ciudad de
México, desde las 18:00 (UTC-6) ya es el día siguiente en UTC, y "mañana" caería un
día tarde. Las fechas que dice el usuario ("hoy", "mañana", "el viernes") se
resuelven contra SU calendario: el de settings.calendar_timezone, la misma zona con
la que se crean los eventos.
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings


def _utcnow() -> datetime:
    """El instante actual. Aparte para que los tests puedan fijarlo."""
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    """La hora actual en la zona del usuario (con la zona puesta)."""
    return _utcnow().astimezone(ZoneInfo(settings.calendar_timezone))


def local_today() -> date:
    return local_now().date()
