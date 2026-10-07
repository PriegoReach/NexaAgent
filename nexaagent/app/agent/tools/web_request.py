"""Tool http_get: leer una página o una API pública.

Es la única tool que sale a cualquier sitio de internet, y el modelo puede llamarla
por algo que leyó en un documento o en otra página (instrucciones escondidas). Por
eso tiene tres límites:
  - Solo http(s) y solo direcciones públicas: nada de localhost, la red local, los
    servicios internos de compose (db, redis, ollama…) ni los metadatos de la nube.
    Se comprueba en cada redirección, que se siguen a mano.
  - Solo dominios que el usuario ha ESCRITO en la conversación, o que están en
    settings.http_get_allowed_domains (con sus subdominios). Una instrucción
    escondida no puede mandar datos a un sitio que el usuario nunca nombró. Si el
    dominio no está, la tool se lo dice al modelo, que lo pregunta; basta con que
    el usuario lo escriba.
  - Respuesta acotada: se leen como mucho _MAX_BYTES y se devuelven _MAX_CHARS.

Límite conocido: la dirección se resuelve para comprobarla y httpx la vuelve a
resolver al conectar; un DNS que cambie de respuesta entre las dos (DNS rebinding)
podría colarse. Para un asistente personal se acepta.
"""
import asyncio
import ipaddress
import logging
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

import httpx
from langchain_core.tools import tool
from sqlalchemy import text

from app.core.config import settings
from app.core.log_context import conversation_id_var, user_input_var
from app.db.worker_db import worker_session

logger = logging.getLogger("nexa.tools")

_TIMEOUT = 15
_MAX_REDIRECTS = 5
_MAX_BYTES = 200_000
_MAX_CHARS = 4000
# Tipos que se pueden devolver como texto; el resto (PDF, imágenes, zips…) no.
_TEXT_TYPES = ("text/", "json", "xml", "javascript")
# Un dominio escrito por el usuario, con o sin http(s):// delante.
_DOMAIN_RE = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63})\b")


class _Refused(Exception):
    """La petición no se hace. str(exc) es el mensaje para el modelo."""


def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _domains_in(texts: list[str]) -> set[str]:
    found: set[str] = set()
    for t in texts:
        for match in _DOMAIN_RE.finditer(t.lower()):
            found.add(match.group(1).removeprefix("www."))
    return found


async def _previous_user_messages(conversation_id: int) -> list[str]:
    async with worker_session() as session:
        rows = await session.execute(
            text(
                "SELECT content FROM messages "
                "WHERE conversation_id = :cid AND role = 'user' "
                "ORDER BY id DESC LIMIT 50"
            ),
            {"cid": conversation_id},
        )
        return [r[0] for r in rows]


def _allowed_domains() -> set[str]:
    """Dominios configurados + los que el usuario ha escrito: en este turno (aún sin
    guardar, llega por contextvar) y en los anteriores de la conversación."""
    texts = [user_input_var.get()]
    cid_raw = conversation_id_var.get()
    if cid_raw and cid_raw != "-":
        texts += _run_async(_previous_user_messages(int(cid_raw)))
    configured = {d.lower().removeprefix("www.") for d in settings.http_get_allowed_domains}
    return configured | _domains_in(texts)


def _is_allowed(host: str, allowed: set[str]) -> bool:
    host = host.removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in allowed)


def _resolve(host: str, port: int) -> list:
    """Aparte para que los tests sustituyan solo esta resolución (no la de todo el
    proceso, que usa también la conexión a Postgres)."""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _check_public(host: str, port: int) -> None:
    """Rechaza un host que resuelva a una dirección no pública (loopback, red local,
    servicios de compose, metadatos de la nube…)."""
    try:
        infos = _resolve(host, port)
    except socket.gaierror as exc:
        raise _Refused(f"No pude encontrar el dominio '{host}'.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            raise _Refused(
                f"'{host}' apunta a una dirección interna o privada ({address}); "
                "por seguridad no la consulto."
            )


def _fetch(url: str, allowed: set[str]) -> str:
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise _Refused("Solo puedo consultar direcciones http:// o https://.")
            host = parts.hostname.lower()
            if not _is_allowed(host, allowed):
                raise _Refused(
                    f"No consulté {host}: el usuario no ha escrito ese dominio en esta "
                    "conversación. Pregúntale si quiere que lo consulte, nombrando el "
                    f"dominio, y pídele que lo escriba (por ejemplo: «sí, consulta {host}»)."
                )
            _check_public(host, parts.port or (443 if parts.scheme == "https" else 80))

            with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise _Refused("La página redirige sin decir a dónde.")
                    url = urljoin(url, location)
                    continue
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "").lower()
                if ctype and not any(t in ctype for t in _TEXT_TYPES):
                    return (f"La respuesta es un archivo ({ctype.split(';')[0]}), no texto; "
                            "no puedo leerla.")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) >= _MAX_BYTES:
                        break
                content = bytes(body[:_MAX_BYTES]).decode(
                    resp.charset_encoding or "utf-8", errors="replace"
                )
                return content[:_MAX_CHARS]
        raise _Refused("La página redirige demasiadas veces.")


@tool
def http_get(url: str) -> str:
    """Fetch the contents of a public URL via HTTP GET.

    Use this to call external read-only APIs or fetch public web pages. It only
    works for domains the user has written in this conversation; for any other
    domain, ask the user first, naming the domain.
    """
    try:
        return _fetch(url, _allowed_domains())
    except _Refused as exc:
        logger.info("http_get refused", extra={"url_host": urlsplit(url).hostname})
        return str(exc)
    except httpx.HTTPError as exc:
        return f"Request failed: {exc}"
