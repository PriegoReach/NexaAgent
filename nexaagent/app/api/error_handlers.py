"""Exception handlers: traducen fallos de infraestructura a respuestas HTTP limpias."""
import logging
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("nexa.errors")


async def upstream_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """Ollama (u otro upstream httpx) caído o lento → 503, no 500 crudo."""
    logger.error(
        "upstream unavailable",
        extra={"event": "upstream_error", "exc_type": type(exc).__name__},
        exc_info=exc,   # traceback completo al log (JSON), NO al cliente
    )
    return JSONResponse(
        status_code=503,
        content={"detail": "El servicio de inferencia no está disponible. Inténtalo de nuevo."},
    )