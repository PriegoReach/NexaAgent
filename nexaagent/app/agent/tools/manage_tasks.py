import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from langchain_core.tools import tool
from sqlalchemy import text

from app.agent import pending
from app.agent.confirmable import confirmable_action
from app.core.log_context import conversation_id_var
from app.db.worker_db import worker_session

logger = logging.getLogger("nexa.tools")

_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
          "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
_ESTADOS_VALIDOS = {"pending", "done"}


def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _fmt_due(due) -> str:
    if due is None:
        return "sin fecha"
    return f"{_DIAS[due.weekday()]} {due.day} de {_MESES[due.month - 1]} ({due.isoformat()})"


# ---------- list ----------
async def _list_tasks(include_done: bool) -> list[dict]:
    async with worker_session() as session:
        sql = "SELECT id, content, due_date, status FROM tasks"
        if not include_done:
            sql += " WHERE status != 'done'"
        sql += " ORDER BY due_date NULLS LAST, id"
        result = await session.execute(text(sql))
        return [dict(r._mapping) for r in result]


@tool
def list_tasks(include_done: bool = False) -> str:
    """List the user's tasks/reminders. Use this whenever the user asks what
    tasks they have, OR before updating/deleting a task referred to by description
    (e.g. "the Ruby task") — you need the task's id to act on it.

    Args:
        include_done: If true, also show completed tasks. Default false (pending only).
    """
    rows = _run_async(_list_tasks(include_done))
    logger.info("list_tasks", extra={"include_done": include_done, "n": len(rows)})
    if not rows:
        return "No tienes tareas pendientes." if not include_done else "No tienes tareas."
    lines = []
    for r in rows:
        mark = "✓" if r["status"] == "done" else "○"
        lines.append(f"#{r['id']} {mark} {r['content']} — {_fmt_due(r['due_date'])}")
    return "\n".join(lines)


# ---------- update ----------
async def _update_task(task_id: int, status: str) -> Optional[str]:
    async with worker_session() as session:
        result = await session.execute(
            text("UPDATE tasks SET status = :status WHERE id = :id RETURNING content"),
            {"status": status, "id": task_id},
        )
        row = result.first()
        await session.commit()
        if row is None:
            return None
        logger.info("update_task", extra={"task_id": task_id, "status": status})
        return row[0]


@tool
def update_task(task_id: int, status: str) -> str:
    """Update a task's status. Use this to mark a task done or reopen it.
    First call list_tasks if you only know the task by description, to get its id.

    Args:
        task_id: The numeric id of the task (from list_tasks).
        status: Either "done" or "pending".
    """
    status = status.strip().lower()
    if status not in _ESTADOS_VALIDOS:
        return f"Estado inválido '{status}'. Usa 'done' o 'pending'."
    content = _run_async(_update_task(task_id, status))
    if content is None:
        return f"No existe la tarea #{task_id}."
    estado_es = "completada" if status == "done" else "reabierta"
    return f"Tarea #{task_id} {estado_es}: {content}"


# ---------- delete (con confirmación asistida) ----------
@confirmable_action("delete_task")
async def perform_delete(args: dict) -> str:
    """Ejecuta el borrado REAL (DELETE...RETURNING + commit) y devuelve el mensaje
    de resultado YA LISTO para el usuario.

    NO es un @tool: la llama la rama de confirmación del orquestador tras un 'sí'
    del usuario (vía CONFIRMABLE_ACTIONS["delete_task"]), NUNCA el modelo directo.
    Firma uniforme: (args: dict) -> str. `args` trae {task_id}.
    Si la fila ya no existía es no-op benigno: se reporta como "ya no existía", no
    como error.
    """
    task_id = args["task_id"]
    async with worker_session() as session:
        result = await session.execute(
            text("DELETE FROM tasks WHERE id = :id RETURNING content"),
            {"id": task_id},
        )
        row = result.first()
        await session.commit()
    if row is None:
        logger.info("confirmed delete but row gone", extra={"task_id": task_id})
        return f"La tarea #{task_id} ya no existía (no borré nada)."
    logger.info("delete_task executed", extra={"task_id": task_id})
    return f"Tarea #{task_id} eliminada: {row[0]}"


async def _propose_delete(task_id: int, conversation_id: Optional[int]) -> str:
    """Verifica que la tarea existe (SIN borrar) y registra la intención pendiente.
    El borrado real NO ocurre aquí: este es el punto del flujo de confirmación."""
    async with worker_session() as session:
        result = await session.execute(
            text("SELECT content FROM tasks WHERE id = :id"),
            {"id": task_id},
        )
        row = result.first()
    if row is None:
        # No existe -> NO se escribe intención; no-op benigno de siempre.
        return f"No existe la tarea #{task_id}."
    content = row[0]
    question = (
        f"¿Seguro que quieres borrar la tarea #{task_id} '{content}'? "
        "Responde sí para confirmar o no para cancelar."
    )
    if conversation_id is not None:
        existing = await pending.set_pending(
            conversation_id,
            {
                "action": "delete_task",
                "args": {"task_id": task_id},
                "description": f"el borrado de la tarea #{task_id} '{content}'",
                "question": question,   # texto LITERAL para el override
            },
        )
        if existing is not None:
            return pending.busy_message(existing)
    logger.info("delete_task proposed", extra={"task_id": task_id})
    return question


@tool
def delete_task(task_id: int) -> str:
    """Delete a task. Use this when the user wants to remove or delete a task.
    First call list_tasks if you only know the task by description, to get its id.
    This will ask the user to confirm; the deletion itself happens after they agree.

    Args:
        task_id: The numeric id of the task (from list_tasks).
    """
    # El conversation_id viene del contextvar que fija run_agent (no es un arg del
    # modelo). Sin él no se puede guardar la intención -> se propone sin pendiente
    # (fail-safe: un 'sí' posterior no encontrará nada que ejecutar -> no borra).
    cid_raw = conversation_id_var.get()
    conversation_id = int(cid_raw) if cid_raw and cid_raw != "-" else None
    return _run_async(_propose_delete(task_id, conversation_id))
