"""Endpoints OAuth de Google (P25). Protegidos con require_jwt (P12): conectar
o consultar la cuenta Google requiere ser el cliente autenticado.

Flujo de copiar-pegar (loopback sin servidor, ver app/integrations/google_oauth.py):
  1) GET  /oauth/google/start    -> {"auth_url": ...}   (el usuario la abre)
  2) POST /oauth/google/callback {"code": ...}          (pega el code del navegador)
  3) GET  /oauth/google/status   -> ¿conectada? ¿token válido?
"""
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import settings
from app.core.security import require_jwt
from app.db.worker_db import worker_session
from app.integrations import google_oauth

logger = logging.getLogger("nexa.api.oauth")

router = APIRouter(
    prefix="/oauth", tags=["oauth"], dependencies=[Depends(require_jwt)]
)


class CallbackRequest(BaseModel):
    code: str


class StartResponse(BaseModel):
    auth_url: str


@router.get("/google/start", response_model=StartResponse)
async def google_start() -> StartResponse:
    """Devuelve la URL de autorización. El usuario la abre en su navegador,
    autoriza, y copia el `code` de la barra de direcciones (http://localhost/?code=...
    fallará al conectar — es esperado; el code está en la URL)."""
    if not (settings.google_client_id and settings.google_client_secret):
        # Integración opcional sin configurar: sin esto se devolvía una URL de Google
        # con client_id vacío, que acaba en una página de error de Google.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Google no está configurado: escribe las credenciales de tu cliente "
                "OAuth en secrets/google_client_id.txt y secrets/google_client_secret.txt "
                "y reinicia la API."
            ),
        )
    return StartResponse(auth_url=google_oauth.build_auth_url())


@router.post("/google/callback")
async def google_callback(payload: CallbackRequest) -> dict:
    """Intercambia el code por tokens y hace UPSERT en oauth_accounts (sobre
    provider='google'). Si Google no devolvió refresh_token (reautorización),
    se conserva el que ya estuviera guardado — no se sobreescribe con NULL."""
    code = payload.code.strip()
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="code vacío")

    try:
        tokens = await google_oauth.exchange_code(code)
    except httpx.HTTPStatusError as exc:
        # code inválido/expirado/reusado -> 400 de Google. No es un 500 nuestro.
        logger.warning("google code exchange failed",
                       extra={"status": exc.response.status_code})
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google rechazó el código (inválido, expirado o ya usado).",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo contactar a Google.",
        ) from exc

    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")  # puede faltar en reautorizaciones
    expires_at = google_oauth._expires_at_from(tokens["expires_in"])
    scope = tokens.get("scope")

    # UPSERT sobre el unique de provider. COALESCE en refresh_token: si esta
    # autorización no trajo uno (Google a veces lo omite si ya consentiste antes),
    # conservamos el refresh_token existente en vez de borrarlo.
    async with worker_session() as session:
        await session.execute(
            text(
                "INSERT INTO oauth_accounts "
                "(provider, access_token, refresh_token, expires_at, scope) "
                "VALUES ('google', :at, :rt, :exp, :scope) "
                "ON CONFLICT (provider) DO UPDATE SET "
                "  access_token = EXCLUDED.access_token, "
                "  refresh_token = COALESCE(EXCLUDED.refresh_token, oauth_accounts.refresh_token), "
                "  expires_at = EXCLUDED.expires_at, "
                "  scope = EXCLUDED.scope, "
                "  updated_at = now()"
            ),
            {"at": access_token, "rt": refresh_token, "exp": expires_at, "scope": scope},
        )
        await session.commit()

    logger.info("google account connected",
                extra={"has_refresh": refresh_token is not None, "scope": scope})
    return {
        "connected": True,
        "provider": "google",
        "scope": scope,
        "refresh_token_received": refresh_token is not None,
    }


@router.get("/google/status")
async def google_status() -> dict:
    """¿Hay cuenta Google conectada? ¿el token sigue válido o expiró? Útil para
    verificar SIN exponer el token (no se devuelve el access_token)."""
    async with worker_session() as session:
        result = await session.execute(
            text(
                "SELECT refresh_token, expires_at, scope, updated_at "
                "FROM oauth_accounts WHERE provider = 'google'"
            )
        )
        row = result.first()

    if row is None:
        return {"connected": False}

    now = datetime.now(timezone.utc)
    return {
        "connected": True,
        "provider": "google",
        "token_valid": row.expires_at > now,
        "expires_at": row.expires_at.isoformat(),
        "has_refresh_token": row.refresh_token is not None,
        "scope": row.scope,
        "updated_at": row.updated_at.isoformat(),
    }
