import asyncio
import hashlib
import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Optional

from langchain_core.tools import tool
from sqlalchemy import text

from app.core.clock import local_today
from app.db.worker_db import worker_session

logger = logging.getLogger("nexa.tools")

_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
          "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
_WEEKDAY_IDX = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3,
                "viernes": 4, "sabado": 5, "domingo": 6}
_MONTH_IDX = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
              "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9,
              "octubre": 10, "noviembre": 11, "diciembre": 12}


def _strip(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower().strip())
    return "".join(c for c in s if not unicodedata.combining(c))


def _resolve_due(phrase: str, today: date) -> Optional[date]:
    """Resuelve una fecha en lenguaje natural (es) o ISO a un date real.
    Determinístico: la aritmética la hace el código, no el LLM.
    Devuelve None si no se entiende O si la fecha ya pasó (señal de calendario
    viejo del modelo, p. ej. un ISO de 2023) — para no guardar basura en silencio.
    """
    if not phrase:
        return None
    raw = phrase.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):          # ISO explícito
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            return None
        return d if d >= today else None                  # rechaza pasado
    s = _strip(raw)
    if s == "hoy":
        return today
    if s == "manana":
        return today + timedelta(days=1)
    if s in ("pasado manana", "pasadomanana"):
        return today + timedelta(days=2)
    for name, idx in _WEEKDAY_IDX.items():                # día de la semana
        if re.search(rf"\b{name}\b", s):
            ahead = (idx - today.weekday()) % 7
            return today + timedelta(days=ahead or 7)     # nunca hoy -> próximo
    m = re.search(r"(\d{1,2})\s+de\s+([a-zñ]+)", s)       # 'D de Mes'
    if m and (mon := _MONTH_IDX.get(_strip(m.group(2)))):
        cand = date(today.year, mon, int(m.group(1)))
        return cand if cand >= today else date(today.year + 1, mon, int(m.group(1)))
    return None


def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _idempotency_key(content: str, due: Optional[date]) -> str:
    # Clave sobre VALORES NORMALIZADOS, no sobre la frase cruda: 'el viernes' y
    # 'este viernes' resuelven al mismo due -> misma clave -> no duplican.
    # A prueba de replay, no de repeticiones legítimas en el tiempo.
    basis = f"{content.strip().lower()}|{due.isoformat() if due else ''}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


async def _insert_task(content: str, due: Optional[date]) -> tuple[int, bool]:
    key = _idempotency_key(content, due)
    async with worker_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO tasks (content, due_date, status, idempotency_key) "
                "VALUES (:content, :due_date, 'pending', :key) "
                "ON CONFLICT (idempotency_key) DO NOTHING "
                "RETURNING id"
            ),
            {"content": content, "due_date": due, "key": key},
        )
        row = result.first()
        await session.commit()                            # worker_session NO auto-commitea
        if row is None:
            # La clave ya existía -> no se insertó. Recuperamos el id existente.
            existing = await session.execute(
                text("SELECT id FROM tasks WHERE idempotency_key = :key"),
                {"key": key},
            )
            task_id = existing.scalar_one()
            logger.info(
                "create_task duplicate ignored",          # el rastro del no-op
                extra={"task_id": task_id, "idempotency_key": key},
            )
            return task_id, False
        task_id = row[0]
        logger.info(
            "create_task executed",
            extra={"task_id": task_id, "due_date": str(due), "idempotency_key": key},
        )
        return task_id, True


@tool
def create_task(content: str, due_date: Optional[str] = None) -> str:
    """Create a task or reminder in the user's task list.
    Use this when the user asks you to remember, note, or schedule something.

    Args:
        content: What the task is about, in plain language.
        due_date: The date EXACTLY as the user expressed it — e.g. "el viernes",
            "mañana", "15 de junio", or an ISO date. Do NOT compute or convert it;
            pass the user's words and the tool resolves the actual date. Omit if
            the user gave no date.
    """
    parsed: Optional[date] = None
    if due_date:
        parsed = _resolve_due(due_date, local_today())
        if parsed is None:
            return (
                f"No pude agendar la fecha '{due_date}' (no la entendí o ya pasó). "
                "Dime una fecha futura, p. ej. 'el viernes' o '2026-06-15'."
            )
    task_id, created = _run_async(_insert_task(content, parsed))
    if parsed:
        cuando = f" para el {_DIAS[parsed.weekday()]} {parsed.day} de {_MESES[parsed.month - 1]} de {parsed.year} ({parsed.isoformat()})"
    else:
        cuando = ""
    if created:
        return f"Tarea #{task_id} creada{cuando}: {content}"
    return f"Ya tenías esa tarea (#{task_id}){cuando}: {content}"
