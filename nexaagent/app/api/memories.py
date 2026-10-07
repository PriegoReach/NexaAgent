"""Memoria de largo plazo: ver y borrar lo que Nexa recuerda del usuario.

Los hechos los extrae app/agent/memory_extraction.py cada pocos mensajes y viven
en long_term_memories, ligados a la conversación de la que salieron: borrar esa
conversación también los borra (FK ON DELETE CASCADE). Aquí se pueden revisar y
olvidar uno a uno o todos, sin tocar las conversaciones.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.security import require_jwt
from app.db.session import SessionLocal

router = APIRouter(prefix="/memories", tags=["memories"], dependencies=[Depends(require_jwt)])


class MemoryItem(BaseModel):
    id: int
    content: str
    created_at: datetime
    conversation_id: int
    conversation_title: str | None = None


class MemoryListResponse(BaseModel):
    items: list[MemoryItem]
    total: int


class MemoryDeleteResponse(BaseModel):
    deleted: int


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> MemoryListResponse:
    """Lo que Nexa recuerda, lo más reciente primero, con la conversación de origen."""
    async with SessionLocal() as session:
        total = (await session.execute(text("SELECT count(*) FROM long_term_memories"))).scalar_one()
        rows = await session.execute(
            text(
                "SELECT m.id, m.content, m.created_at, m.conversation_id, c.title "
                "FROM long_term_memories m JOIN conversations c ON c.id = m.conversation_id "
                "ORDER BY m.id DESC LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset},
        )
        items = [
            MemoryItem(id=r.id, content=r.content, created_at=r.created_at,
                       conversation_id=r.conversation_id, conversation_title=r.title)
            for r in rows
        ]
    return MemoryListResponse(items=items, total=total)


@router.delete("/{memory_id}", response_model=MemoryDeleteResponse)
async def delete_memory(memory_id: int) -> MemoryDeleteResponse:
    """Olvida un hecho. 404 si no existe."""
    async with SessionLocal() as session:
        row = (await session.execute(
            text("DELETE FROM long_term_memories WHERE id = :id RETURNING id"), {"id": memory_id}
        )).first()
        await session.commit()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Memory {memory_id} not found")
    return MemoryDeleteResponse(deleted=1)


@router.delete("", response_model=MemoryDeleteResponse)
async def delete_all_memories() -> MemoryDeleteResponse:
    """Olvida todo lo que Nexa recordaba. Las conversaciones no se tocan."""
    async with SessionLocal() as session:
        result = await session.execute(text("DELETE FROM long_term_memories"))
        await session.commit()
    return MemoryDeleteResponse(deleted=result.rowcount or 0)
