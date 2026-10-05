"""Tool de ENVÍO de correo con Gmail.

EL PUNTO MÁS ALTO DE LA ESCALERA DE IRREVERSIBILIDAD del proyecto:
  - un correo enviado NO se deshace (un evento se borra; un correo ya llegó),
  - el daño es a una PERSONA REAL (un tercero), no a la propia cuenta,
  - y el modelo redacta el CONTENIDO (prosa para un humano, no args estructurados).
Por eso la confirmación NO es cosmética: es la única barrera entre "el modelo propuso"
y "un correo equivocado llegó a alguien". send_email SOLO PROPONE; el envío real
(perform_send_email) ocurre exclusivamente desde el registro CONFIRMABLE_ACTIONS
tras un "sí" explícito del usuario.

IDEMPOTENCIA — LADO EMISOR (obligado): la Gmail API users.messages.send NO admite
clave de idempotencia de cliente (a diferencia de Calendar, que acepta un `id` y
da 409 lado-receptor). Enviar el mismo raw dos veces entrega DOS correos. Por eso
registramos-primero en sent_emails con UNIQUE sobre idempotency_key; si choca, NO
se reenvía. Sesgo a no-duplicar.

CLASIFICACIÓN DEL FALLO (deuda de la P27): registrar-primero no debe bloquear el
reintento de un correo que sabemos que NO salió. Estados de sent_emails:
  - 'pending'    registrado y enviándose (o el proceso murió a mitad: ambiguo).
  - 'sent'       Gmail confirmó el envío.
  - 'failed'     fallo DEFINITIVO antes de salir (sin cuenta o token, 4xx de Gmail,
                 la conexión ni se abrió): el mismo correo SÍ se puede reintentar.
  - 'uncertain'  fallo AMBIGUO (5xx, timeout esperando respuesta): pudo salir, así
                 que no se reintenta y se pide al usuario que lo compruebe.
Si la clave ya existe, la respuesta depende del estado: nunca se da por enviado un
correo que no consta como 'sent'.

httpx directo + email.message de stdlib (criterio del proyecto: nada de
google-api-python-client pesado). Timeout explícito.
"""
import asyncio
import base64
import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

import httpx
from langchain_core.tools import tool
from sqlalchemy import text

from app.agent import pending
from app.agent.confirmable import confirmable_action
from app.core.log_context import conversation_id_var
from app.db.worker_db import worker_session
from app.integrations.google_oauth import GoogleTokenUnavailable, get_valid_token

logger = logging.getLogger("nexa.tools")

_GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_HTTP_TIMEOUT = 20

# Validación deliberadamente simple: "algo@algo.algo" sin espacios. No pretende
# validar RFC 5322 completo (imposible con regex); solo descarta basura evidente
# antes de guardar un pending (no agendamos un envío a un destinatario que no es
# ni un email).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Fallo AMBIGUO (el correo pudo salir): ni se reintenta ni se da por enviado.
_UNCERTAIN_MSG = (
    "No sé si el correo llegó a salir: Gmail no respondió bien. Para no mandarlo dos "
    "veces no lo reintento; revisa tu carpeta de Enviados en Gmail y, si no está, "
    "envíalo desde ahí."
)


def _run_async(coro):
    """Puente síncrono->async (mismo molde que las demás tools): la tool es
    síncrona para langchain pero set_pending es async."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _idempotency_key(to: str, subject: str, body: str) -> str:
    """Clave sobre los VALORES normalizados. Mismo correo (mismo
    destinatario+asunto+cuerpo) -> misma clave -> el UNIQUE bloquea el reenvío.
    A prueba de replay, no de correos legítimamente distintos."""
    basis = f"{to.strip().lower()}|{subject.strip().lower()}|{body.strip().lower()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _build_question(to: str, subject: str, body: str) -> str:
    """La pregunta de confirmación LITERAL: muestra los TRES campos y el cuerpo
    COMPLETO (no resumido). Este texto es el que el usuario verá y aprobará — y,
    gracias al override 'propuesta sin modelo' del orquestador, es EXACTAMENTE lo
    que se enviará. 'Lo mostrado = lo enviado'."""
    return (
        "Voy a enviar este correo:\n\n"
        f"Para: {to}\n"
        f"Asunto: {subject}\n\n"
        f"{body}\n\n"
        "¿Lo envío? Responde sí para confirmar o no para cancelar."
    )


def _build_mime(to: str, subject: str, body: str) -> str:
    """Construye el mensaje RFC 2822 (MIME) y lo codifica en base64url, como pide
    la Gmail API. EmailMessage de stdlib maneja el encoding UTF-8 del asunto
    (RFC 2047) y del cuerpo automáticamente. No fijamos From: Gmail usa la cuenta
    autenticada."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


async def _register(key: str, to: str, subject: str) -> int | None:
    """Registra el intento ANTES de enviar (sesgo a no-duplicar).

    Devuelve el id de la fila si este intento puede enviar: la clave es nueva, o un
    intento anterior falló SIN salir ('failed') y se reclama. None si la clave está
    en cualquier otro estado: ese correo no se vuelve a mandar.
    """
    async with worker_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO sent_emails (idempotency_key, to_addr, subject, status) "
                "VALUES (:key, :to, :subject, 'pending') "
                "ON CONFLICT (idempotency_key) DO UPDATE SET status = 'pending' "
                "WHERE sent_emails.status = 'failed' "
                "RETURNING id"
            ),
            {"key": key, "to": to, "subject": subject},
        )
        row = result.first()
        await session.commit()
    return row[0] if row is not None else None


async def _not_resent_message(key: str) -> str:
    """Respuesta cuando la clave ya existe y no se puede reclamar. Depende de lo que
    consta: un correo solo se da por enviado si está como 'sent'."""
    async with worker_session() as session:
        result = await session.execute(
            text("SELECT status FROM sent_emails WHERE idempotency_key = :key"),
            {"key": key},
        )
        status = result.scalar_one_or_none()
    logger.info("send_email not resent", extra={"idempotency_key": key, "status": status})
    if status == "sent":
        return "Ese correo ya se había enviado (no lo reenvié)."
    # 'pending' o 'uncertain': un intento anterior quedó a medias y pudo salir.
    return (
        "Un intento anterior de este mismo correo no terminó bien y no sé si llegó a "
        "salir, así que no lo reenvié para no duplicarlo. Revisa tu carpeta de Enviados "
        "en Gmail y, si no está, envíalo desde ahí."
    )


@confirmable_action("send_email")
async def perform_send_email(args: dict) -> str:
    """El ENVÍO REAL del correo. Ejecutor confirmable: firma (args: dict) -> str.
    NO lo llama el modelo: lo invoca la rama de confirmación del orquestador tras un
    "sí". `args` trae {to, subject, body}.

    Registra el intento antes de enviar y deja en sent_emails el resultado real ya
    clasificado (ver docstring del módulo): solo un fallo DEFINITIVO libera el mismo
    correo para reintentarlo.
    """
    to = args["to"]
    subject = args.get("subject", "")
    body = args.get("body", "")
    key = _idempotency_key(to, subject, body)

    # PASO 1: registrar ANTES de enviar. None = la clave existe y no es reclamable.
    email_id = await _register(key, to, subject)
    if email_id is None:
        return await _not_resent_message(key)

    # PASO 2: token y mensaje (fuera de la sesión; el registro ya está commiteado).
    # Un fallo aquí ocurre ANTES de que la petición salga: seguro que no se envió.
    try:
        token = await get_valid_token()
        raw = _build_mime(to, subject, body)
    except GoogleTokenUnavailable as exc:
        await _mark_status(email_id, "failed")
        return f"El correo no se envió. {exc}"
    except Exception:
        logger.exception("gmail message build failed", extra={"email_id": email_id})
        await _mark_status(email_id, "failed")
        return "No pude preparar el correo (error interno); no se envió."

    # PASO 3: enviar. La clase del fallo decide si el mismo correo se puede reintentar.
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(_GMAIL_SEND_URL, json={"raw": raw}, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        logger.warning("gmail send failed", extra={"status": code, "email_id": email_id})
        if code >= 500:
            # Error interno de Google: pudo procesarlo antes de fallar.
            await _mark_status(email_id, "uncertain")
            return _UNCERTAIN_MSG
        # 4xx: Gmail rechazó la petición, el correo no salió.
        await _mark_status(email_id, "failed")
        if code in (401, 403):
            return ("Google rechazó el envío (token o permisos de Gmail); el correo no "
                    "se envió. Reconecta la cuenta y vuelve a pedírmelo.")
        return f"Gmail rechazó el correo (error {code}); no se envió."
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # La conexión ni se abrió: la petición no llegó a salir.
        await _mark_status(email_id, "failed")
        logger.warning("gmail unreachable", extra={"email_id": email_id, "exc": str(exc)})
        return ("No pude contactar a Gmail (problema de red); el correo no se envió. "
                "Vuelve a pedírmelo en un momento.")
    except httpx.HTTPError as exc:
        # La petición pudo salir y perderse la respuesta (p. ej. timeout de lectura).
        await _mark_status(email_id, "uncertain")
        logger.warning("gmail send outcome unknown",
                       extra={"email_id": email_id, "exc": str(exc)})
        return _UNCERTAIN_MSG

    # PASO 4: registrar el resultado real.
    await _mark_status(email_id, "sent")
    logger.info("send_email executed", extra={"email_id": email_id, "idempotency_key": key})
    return f"Correo enviado a {to} (asunto: '{subject}')."


async def _mark_status(email_id: int, status: str) -> None:
    async with worker_session() as session:
        await session.execute(
            text("UPDATE sent_emails SET status = :s WHERE id = :id"),
            {"s": status, "id": email_id},
        )
        await session.commit()


@tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email on the user's behalf via Gmail. Use this whenever the user
    asks to send/write an email or message to someone (e.g. "envía un correo a
    juan@x.com diciendo que...", "escríbele a María que...").
    This does NOT send the email directly: it shows the full email (recipient,
    subject and body) and asks the user to confirm; it is sent only after they agree.

    Args:
        to: The recipient's email address.
        subject: A short subject line for the email.
        body: The full body text of the email, written in plain language.
    """
    to = to.strip()
    if not _EMAIL_RE.match(to):
        # Destinatario inválido -> NO se guarda pending (no agendamos un envío a
        # algo que no es un email). El modelo recibe un error claro.
        return (f"'{to}' no parece una dirección de correo válida. "
                "Dame un email con el formato nombre@dominio.com.")
    # Un salto de línea no cabe en un asunto (EmailMessage lo rechaza al enviar): se
    # normaliza ANTES de proponer, así lo mostrado sigue siendo lo que se envía.
    subject = " ".join(subject.split())

    question = _build_question(to, subject, body)
    description = f"el envío del correo a {to} (asunto: '{subject}')"

    cid_raw = conversation_id_var.get()
    conversation_id = int(cid_raw) if cid_raw and cid_raw != "-" else None
    if conversation_id is not None:
        # set_pending es async; la tool es síncrona para langchain -> puente.
        existing = _run_async(
            pending.set_pending(
                conversation_id,
                {
                    "action": "send_email",
                    "args": {"to": to, "subject": subject, "body": body},
                    "description": description,
                    "question": question,   # texto LITERAL para el override
                },
            )
        )
        if existing is not None:
            return pending.busy_message(existing)
    logger.info("send_email proposed", extra={"to": to})
    return question
