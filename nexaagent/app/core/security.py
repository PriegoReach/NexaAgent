"""Autenticación JWT (Modelo B: cliente único).

- create_access_token: emite un JWT HS256 firmado con caducidad.
- require_jwt: dependencia FastAPI que toma el JWT del header Authorization
  (Bearer; lo usan curl y Swagger) o de la cookie de sesión httpOnly que pone
  /auth/login (la usa la web, y así la sesión sobrevive a recargar la página sin
  dejar el token al alcance del JavaScript), valida la firma y la caducidad, y
  devuelve los claims.
"""
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

# auto_error=False: sin header no se responde aún; puede venir la cookie. El
# esquema sigue en el OpenAPI, así que el botón "Authorize" de Swagger funciona.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="JWT obtenido en /auth/login (la web usa la cookie de sesión)",
)

# Nombre de la cookie de sesión que pone /auth/login.
SESSION_COOKIE = "nexa_session"


def create_access_token(extra_claims: dict | None = None) -> tuple[str, int]:
    """Emite un JWT firmado con la caducidad configurada.

    Devuelve (token, segundos_hasta_expirar).
    """
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=settings.jwt_expires_hours)
    payload: dict = {
        "iss": settings.app_name,       # emisor
        "iat": int(now.timestamp()),    # emitido
        "exp": int(expires.timestamp()),# expira
        "sub": "client",                # un solo cliente (Modelo B)
    }
    if extra_claims:
        payload.update(extra_claims)

    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, settings.jwt_expires_hours * 3600


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_jwt(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict:
    """Valida el JWT (header Bearer o cookie de sesión). Devuelve los claims o
    levanta 401.

    Errores cubiertos: sin credenciales, token mal firmado, expirado o malformado.
    Todos responden 401 con WWW-Authenticate: Bearer (estándar).
    """
    token = credentials.credentials if credentials else session_cookie
    if not token:
        raise _unauthorized("Not authenticated")
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Token expired")
    except jwt.InvalidTokenError:
        # Cubre firma inválida, payload corrupto, alg no permitido, etc.
        raise _unauthorized("Invalid token")
    return claims
