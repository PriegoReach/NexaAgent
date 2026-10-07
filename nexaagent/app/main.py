import logging
from contextlib import asynccontextmanager
from app.api.error_handlers import upstream_unavailable_handler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.exceptions import UpstreamUnavailable
from app.core.logging_config import setup_logging
from app.api.request_context import RequestIDMiddleware
from app.api import auth, chat, conversations, documents, health, memories, metrics, oauth, tts
from app.core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # El esquema lo gestiona Alembic (servicio migrate: alembic upgrade head).
    # init_db() queda como helper manual en app/db/session.py, ya no corre al arrancar.
    yield


setup_logging()                       # antes de instanciar FastAPI (captura logs de arranque)
logging.getLogger("nexa.api").info("app startup", extra={"event": "app_startup"})

app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestIDMiddleware)
# CORS se añade DESPUÉS de RequestIDMiddleware a propósito: en Starlette el último
# middleware añadido queda como el MÁS EXTERNO, así CORS envuelve todo (atiende el
# preflight OPTIONS antes de llegar a nada y pone los headers en TODA respuesta,
# incluidos los errores). Orígenes explícitos (no "*") porque allow_credentials=True.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],  # Starlette refleja los headers pedidos (incl. Authorization)
)

app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(documents.router)
app.include_router(conversations.router)
app.include_router(memories.router)
app.include_router(oauth.router)
app.include_router(tts.router)
# El orchestrator traduce httpx.ConnectError/TimeoutException a UpstreamUnavailable
# ANTES de que la excepción cruce el RequestIDMiddleware (BaseHTTPMiddleware), que
# de otro modo impide que los handlers de app la capturen.
app.add_exception_handler(UpstreamUnavailable, upstream_unavailable_handler)
@app.get("/")
async def root() -> dict:
    return {"app": settings.app_name, "docs": "/docs"}
