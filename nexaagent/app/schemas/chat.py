from datetime import datetime

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None


class ChatResponse(BaseModel):
    conversation_id: int
    answer: str


class MessageItem(BaseModel):
    role: str
    content: str
    created_at: datetime


class ConversationSummary(BaseModel):
    id: int
    title: str | None = None
    created_at: datetime
    message_count: int
    last_message: str | None = None
    last_message_at: datetime | None = None


class ConversationListResponse(BaseModel):
    items: list[ConversationSummary]
    total: int
    limit: int
    offset: int


class ConversationDetail(BaseModel):
    id: int
    title: str | None = None
    created_at: datetime
    messages: list[MessageItem]


class DocumentResponse(BaseModel):
    document_id: int
    filename: str
    status: str


class DocumentItem(BaseModel):
    id: int
    filename: str
    status: str  # pending | ready | empty (sin texto extraíble) | failed
    created_at: datetime
