"""Endpoints de autenticación. Modelo B (cliente único).

La web no guarda el token: /auth/login lo deja en una cookie httpOnly
(SameSite=Strict) que el navegador manda sola, y /auth/session le dice al cargar
la página si la sesión sigue viva. curl y Swagger siguen usando el token del
cuerpo de la respuesta en el header Authorization.
"""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.security import SESSION_COOKIE, create_access_token, require_jwt

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # segundos


class SessionResponse(BaseModel):
    authenticated: bool
    expires_at: int  # epoch (s) de caducidad del token


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, response: Response) -> TokenResponse:
    """Intercambia AUTH_PASSWORD por un JWT firmado.

    compare_digest evita timing attacks (la comparación tarda lo mismo
    sea cual sea el prefijo coincidente). Sin esto, un atacante podría
    inferir bytes de la contraseña midiendo tiempos.
    """
    if not secrets.compare_digest(payload.password, settings.auth_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token, expires_in = create_access_token()
    # httpOnly: el JavaScript de la página no puede leerla. SameSite=Strict: el
    # navegador no la manda desde otros sitios (sin CSRF). Mismo sitio que la web
    # (localhost:5173 -> localhost:8000: el puerto no cambia el sitio).
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=expires_in,
        httponly=True,
        samesite="strict",
        secure=settings.session_cookie_secure,
        path="/",
    )
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/session", response_model=SessionResponse)
async def session(claims: dict = Depends(require_jwt)) -> SessionResponse:
    """¿Hay sesión válida? 200 si la cookie (o el header) es válida; 401 si no."""
    return SessionResponse(authenticated=True, expires_at=claims["exp"])


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    """Cierra la sesión de la web: borra la cookie. (Los JWT no se revocan uno a
    uno; caducan solos o al rotar jwt_secret.)"""
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict",
                           secure=settings.session_cookie_secure)
