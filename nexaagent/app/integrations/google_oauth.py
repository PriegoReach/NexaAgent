"""Lógica OAuth 2.0 con Google (P25). SIN endpoints, SIN langchain: solo el
protocolo. Los endpoints (app/api/oauth.py) y la tool (app/agent/tools/calendar.py)
consumen estas funciones.

REDIRECT URI — qué método y por qué:
    El OOB clásico (urn:ietf:wg:oauth:2.0:oob, "copiar-pegar" nativo de Google)
    está MUERTO: Google lo bloqueó del todo el 31-ene-2023. Para un cliente tipo
    "Desktop app" el único redirect soportado hoy es loopback (http://localhost o
    http://127.0.0.1[:puerto]). El flujo oficial pide un servidor local escuchando
    para recibir el code... pero esta app corre en Docker y exponer un puerto al
    navegador del host es justo lo que queremos evitar (decisión de diseño P25).

    Método usado: LOOPBACK SIN SERVIDOR. redirect_uri = "http://localhost".
    El usuario abre build_auth_url() en su navegador, autoriza, y Google redirige
    a  http://localhost/?code=XXXX&scope=...  . No hay nada escuchando ahí, así que
    el navegador muestra "no se puede conectar" — PERO el `code` queda en la barra
    de direcciones. El usuario lo copia a mano y lo pega en /oauth/google/callback.
    Cero puertos expuestos. En el intercambio de tokens reenviamos el MISMO
    redirect_uri (Google exige que coincida con el de la autorización).

httpx directo (no google-auth): el protocolo OAuth de Google es 3 llamadas HTTP
sencillas; google-auth/google-api-python-client arrastrarían dependencias pesadas
para nada. Mismo criterio "httpx + timeout" del resto del proyecto (lección P15).
"""
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.db.worker_db import worker_session

logger = logging.getLogger("nexa.integrations.google")

# Endpoints OAuth de Google.
_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Scopes solicitados (separados por espacio, como exige OAuth2). Cada parte P
# AÑADE el permiso mínimo que necesita, nunca más:
#   - calendar.events (P26): crear/editar eventos. Cubre también la lectura de P25,
#     así que list_calendar_events sigue funcionando.
#   - gmail.send (P27): SOLO enviar correo. NO gmail.readonly ni el gmail completo
#     (menos scope = menos superficie de riesgo para la acción más irreversible).
#   - drive.readonly (P28): SOLO leer/descargar archivos de Drive para ingestarlos
#     al RAG. NO el scope 'drive' completo ni 'drive.file' (mínimo: leer).
# NOTA: al cambiar/añadir scope, el token anterior NO cubre el permiso nuevo ->
# hay que RE-AUTORIZAR. build_auth_url ya fuerza el re-consentimiento (prompt=
# consent); basta abrir la nueva auth_url y volver a dar el code.
SCOPE = (
    "https://www.googleapis.com/auth/calendar.events"
    " https://www.googleapis.com/auth/gmail.send"
    " https://www.googleapis.com/auth/drive.readonly"
)

# Loopback sin servidor (ver docstring del módulo). El usuario copia el `code`
# de la barra de direcciones cuando el navegador falle al conectar aquí.
REDIRECT_URI = "http://localhost"

# Margen para considerar un token "ya expirado" y refrescar de forma preventiva:
# evita la carrera de que el token muera entre el chequeo y el uso.
_EXPIRY_MARGIN_SECONDS = 60

_HTTP_TIMEOUT = 15


def build_auth_url() -> str:
    """Construye la URL de autorización que el usuario abre en su navegador.

    access_type=offline + prompt=consent son OBLIGATORIOS para que Google
    devuelva un REFRESH TOKEN. Sin ellos no hay refresh token y la sesión muere
    en ~1h sin posibilidad de renovar (lección clave de este flujo).
    """
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # -> refresh token
        "prompt": "consent",        # fuerza re-consentir -> refresh token incluso si ya autorizó antes
    }
    return f"{_AUTH_ENDPOINT}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """Cambia el authorization code por tokens (POST al token endpoint).

    Devuelve el dict crudo de Google: access_token, expires_in, scope, token_type,
    y refresh_token (solo si vino access_type=offline en la 1a autorización).
    """
    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(_TOKEN_ENDPOINT, data=data)
        resp.raise_for_status()
    tokens = resp.json()
    logger.info(
        "google oauth code exchanged",
        extra={"has_refresh": "refresh_token" in tokens, "scope": tokens.get("scope")},
    )
    return tokens


async def refresh_access_token(refresh_token: str) -> dict:
    """Usa el refresh token para obtener un nuevo access token.

    La respuesta de refresh NO trae un refresh_token nuevo (Google reusa el mismo);
    conservamos el que ya teníamos en la BD.
    """
    data = {
        "refresh_token": refresh_token,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(_TOKEN_ENDPOINT, data=data)
        resp.raise_for_status()
    tokens = resp.json()
    logger.info("google oauth token refreshed", extra={"scope": tokens.get("scope")})
    return tokens


def _expires_at_from(expires_in: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))


class GoogleTokenUnavailable(Exception):
    """No se pudo conseguir un access token de Google utilizable. str(exc) ya es el
    mensaje para el usuario: las tools lo devuelven tal cual, NO un 500."""


class NoGoogleAccount(GoogleTokenUnavailable):
    """No hay autorización utilizable: la cuenta no está conectada, o Google ya no
    acepta la guardada (refresh token caducado o revocado). Hay que (re)conectar."""


_REFRESH_UNAVAILABLE = (
    "No pude renovar el acceso a Google ahora mismo (problema de red o de Google). "
    "Inténtalo de nuevo en un momento."
)


def _oauth_error(resp: httpx.Response) -> str | None:
    """Código de error OAuth del cuerpo de la respuesta (p. ej. 'invalid_grant')."""
    try:
        data = resp.json()
    except ValueError:
        return None
    return data.get("error") if isinstance(data, dict) else None


async def get_valid_token() -> str:
    """Devuelve un access token VÁLIDO de la cuenta Google.

    Lee oauth_accounts(provider='google'). Si expires_at ya pasó (con margen),
    refresca con el refresh_token, actualiza la fila, y devuelve el token nuevo.
    Esta es la función que usan las tools.

    Usa worker_session() (NullPool, engine efímero atado al loop activo): seguro
    de llamar desde el ThreadPoolExecutor de una tool sin el cross-loop de P2/P24.

    Nunca deja escapar un error de httpx: levanta NoGoogleAccount si hay que
    (re)conectar la cuenta (no hay cuenta, no hay refresh_token, o Google lo
    rechaza) y GoogleTokenUnavailable si Google no responde al renovar.
    """
    async with worker_session() as session:
        result = await session.execute(
            text(
                "SELECT id, access_token, refresh_token, expires_at, scope "
                "FROM oauth_accounts WHERE provider = 'google'"
            )
        )
        row = result.first()
        if row is None:
            raise NoGoogleAccount(
                "No hay una cuenta de Google conectada. Conéctala desde Conexiones "
                "y vuelve a intentarlo."
            )

        account_id = row.id
        access_token = row.access_token
        refresh_token = row.refresh_token
        expires_at = row.expires_at

        # expires_at viene de la BD como aware (timestamptz). Comparamos con un
        # umbral adelantado por el margen (refresco preventivo).
        threshold = datetime.now(timezone.utc) + timedelta(seconds=_EXPIRY_MARGIN_SECONDS)
        if expires_at > threshold:
            return access_token  # todavía válido, no tocamos nada

        # Expirado (o a punto): hay que refrescar.
        if not refresh_token:
            raise NoGoogleAccount(
                "El acceso a Google caducó y no se puede renovar. Reconecta la "
                "cuenta desde Conexiones."
            )

        try:
            tokens = await refresh_access_token(refresh_token)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                raise GoogleTokenUnavailable(_REFRESH_UNAVAILABLE) from exc
            error = _oauth_error(exc.response)
            logger.warning(
                "google token refresh rejected",
                extra={"status": exc.response.status_code, "oauth_error": error},
            )
            if error == "invalid_grant":
                # El refresh token está muerto: caducó (en modo Testing duran ~7 días)
                # o se revocó. Se borra para que /oauth/google/status lo refleje
                # (has_refresh_token=false) y la UI pida reconectar en vez de prometer
                # una renovación que ya no va a ocurrir.
                await session.execute(
                    text(
                        "UPDATE oauth_accounts SET refresh_token = NULL, "
                        "updated_at = now() WHERE id = :id"
                    ),
                    {"id": account_id},
                )
                await session.commit()
            raise NoGoogleAccount(
                "Google ya no acepta la autorización guardada (caducó o fue revocada). "
                "Reconecta la cuenta desde Conexiones."
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning(
                "google token refresh failed", extra={"exc_type": type(exc).__name__}
            )
            raise GoogleTokenUnavailable(_REFRESH_UNAVAILABLE) from exc
        new_access = tokens["access_token"]
        new_expires_at = _expires_at_from(tokens["expires_in"])
        # Google no reemite refresh_token en el refresh -> conservamos el actual.
        new_scope = tokens.get("scope", row.scope)

        await session.execute(
            text(
                "UPDATE oauth_accounts SET access_token = :at, expires_at = :exp, "
                "scope = :scope, updated_at = now() WHERE id = :id"
            ),
            {"at": new_access, "exp": new_expires_at, "scope": new_scope, "id": account_id},
        )
        await session.commit()
        logger.info("google access token refreshed and persisted",
                    extra={"account_id": account_id})
        return new_access
