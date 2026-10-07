"""Contextvars del turno en curso: correlación de logs (request_id, conversation_id)
y datos que las tools leen sin recibirlos del modelo, que podría falsearlos."""
import logging
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
conversation_id_var: ContextVar[str] = ContextVar("conversation_id", default="-")
# El mensaje del usuario en este turno (aún no está guardado en messages). No va a
# los logs: lo usa http_get para saber qué dominios ha mencionado el usuario.
user_input_var: ContextVar[str] = ContextVar("user_input", default="")


class ContextFilter(logging.Filter):
    """Inyecta request_id y conversation_id en cada LogRecord desde los contextvars."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.conversation_id = conversation_id_var.get()
        return True