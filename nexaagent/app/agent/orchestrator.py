import logging
import re
import unicodedata

import httpx
from kombu.exceptions import OperationalError
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_ollama import ChatOllama
from sqlalchemy import text

from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.agent import memory, pending
from app.agent.confirmable import CONFIRMABLE_ACTIONS
from app.agent.tools import get_tools
from app.core.config import settings
from app.core.exceptions import UpstreamUnavailable
from app.core.log_context import conversation_id_var, request_id_var, user_input_var
from app.db.models import Message
from app.db.session import SessionLocal
from app.workers.tasks import extract_memories_task

logger = logging.getLogger("nexa.agent")

SYSTEM_PROMPT = (
    "Eres NexaAgent, un asistente empresarial autónomo. Respondes en español, de forma concisa.\n\n"
    "REGLA OBLIGATORIA SOBRE HERRAMIENTAS:\n"
    "- Siempre que el usuario pregunte por documentos internos, políticas, proyectos, "
    "códigos, responsables, presupuestos, fechas o CUALQUIER dato que pueda vivir en "
    "archivos de la empresa, DEBES llamar primero a la herramienta `search_knowledge_base`.\n"
    "- Si el usuario se refiere a algo que te contó antes, pregunta qué recuerdas, o si "
    "el contexto de conversaciones pasadas ayudaría, USA `search_long_term_memory`.\n"
    "- PROHIBIDO responder 'no tengo acceso' o afirmar que buscaste sin haber llamado "
    "realmente a la herramienta. Ante la duda, USA la herramienta.\n"
    "- Basa tu respuesta ÚNICAMENTE en lo que devuelvan las herramientas. Solo si no "
    "devuelven nada, di que no encontraste información.\n"
    "- Cuando el usuario te pida recordar, anotar o agendar algo, USA `create_task`, "
    "pasando la fecha TAL COMO la dijo el usuario (p. ej. 'el viernes', 'mañana', "
    "'15 de junio'). NO la conviertas a otro formato ni calcules el día; de eso se "
    "encarga la herramienta.\n"
    "- Para ver, completar o borrar tareas usa `list_tasks`, `update_task`, `delete_task`. "
    "Si el usuario se refiere a una tarea por descripción ('la tarea de Rubí'), PRIMERO "
    "llama `list_tasks` para ver los id, identifica la correcta, y luego actúa con su id. "
    "Si hay varias que coinciden, pregunta cuál antes de actuar.\n"
    "- Para enviar una notificación o alerta al sistema externo, usa `call_webhook`.\n"
    "- Para ver reuniones, citas, eventos, la agenda o qué tiene agendado el "
    "usuario en su calendario, usa `list_calendar_events`.\n"
    "- Para AGENDAR o CREAR una reunión, cita o evento en el calendario, usa "
    "`create_calendar_event` (pedirá confirmación antes de crearlo). Pasa la fecha "
    "y la hora TAL COMO las dijo el usuario ('mañana', 'el viernes', '3pm', "
    "'15:00'); NO las conviertas ni calcules, de eso se encarga la herramienta.\n"
    "- Para ENVIAR un correo a alguien, usa `send_email` con destinatario, asunto y "
    "cuerpo (mostrará el correo completo y pedirá confirmación antes de enviarlo). "
    "NO afirmes que enviaste el correo: la herramienta solo lo propone.\n"
    "- Para BUSCAR archivos en el Google Drive del usuario, usa `list_drive_files` "
    "con un texto de búsqueda. Para TRAER e INDEXAR un documento de Drive al RAG (y "
    "poder responder sobre su contenido después), usa `ingest_drive_file` con el id "
    "que devolvió `list_drive_files`. Tras indexarlo, responde sobre su contenido con "
    "`search_knowledge_base` como con cualquier documento.\n"
    "- Para consultar una página o una API externa usa `http_get`. Solo funciona con "
    "dominios que el usuario haya escrito en la conversación: si necesitas otro, "
    "pregúntale nombrando el dominio y pídele que lo escriba para confirmarlo."
)

MEMORY_EXTRACTION_EVERY = 6  # cada 3 intercambios (user + assistant)


def _build_agent():
    llm = ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        temperature=0,
        keep_alive="30m",   # mantiene el modelo residente en VRAM; mata el cold-load recurrente
        timeout=120,    
        client_kwargs={"timeout": 120},
        async_client_kwargs={"timeout": 120},    # red de seguridad: Ollama colgado no cuelga el request para siempre
    )
    return create_agent(model=llm, tools=get_tools(), system_prompt=SYSTEM_PROMPT)


_agent = None


def _agent_singleton():
    global _agent
    if _agent is None:
        _agent = _build_agent()
    return _agent


def _to_lc_messages(history: list[dict]) -> list:
    out = []
    for m in history:
        if m["role"] == "user":
            out.append(HumanMessage(content=m["content"]))
        elif m["role"] == "assistant":
            out.append(AIMessage(content=m["content"]))
    return out


async def _persist_messages(conversation_id: int, user_input: str, answer: str) -> None:
    """Guarda el par user/assistant en la tabla messages (fuente durable).

    Redis es memoria de corto plazo (TTL 24h, solo últimos N). Postgres es el
    registro permanente y la fuente del conteo para el disparador de extracción.
    """
    async with SessionLocal() as session:
        session.add_all(
            [
                Message(conversation_id=conversation_id, role="user", content=user_input),
                Message(conversation_id=conversation_id, role="assistant", content=answer),
            ]
        )
        await session.commit()


async def _maybe_extract_memories(conversation_id: int) -> None:
    """Si el total de mensajes es múltiplo de N, encola la extracción de hechos."""
    async with SessionLocal() as session:
        result = await session.execute(
            text("SELECT count(*) FROM messages WHERE conversation_id = :cid"),
            {"cid": conversation_id},
        )
        total = result.scalar_one()
    if total > 0 and total % MEMORY_EXTRACTION_EVERY == 0:
        # El contextvar no cruza el proceso del worker: pasamos el request_id
        # como argumento para que la tarea lo vuelva a fijar (correlación E2E).
        try:
            extract_memories_task.delay(
                conversation_id, request_id=request_id_var.get()
            )
        except OperationalError as exc:
            # Broker (Redis) caído: la respuesta al usuario ya está. La extracción
            # se pierde en este turno; no debe tumbar el request (disparar-y-olvidar).
            logger.warning(
                "could not enqueue memory extraction, broker unavailable",
                extra={"event": "broker_degraded", "exc_type": type(exc).__name__},
            )


# Reintento SOLO de la inferencia, transitorios, pensado para interactivo:
# 2 intentos totales (1 reintento), backoff corto (~0.5s). Hay un humano esperando.
# tenacity envuelve la llamada; tras agotar, reraise=True relanza la excepción
# ORIGINAL (httpx.*) que el try/except de run_agent traduce a UpstreamUnavailable.
@retry(
    retry=retry_if_exception_type(
        (httpx.ConnectError, httpx.TimeoutException, ConnectionError)
    ),
    stop=stop_after_attempt(2),                       # 2 intentos totales = 1 reintento
    wait=wait_exponential(multiplier=0.5, max=2),     # ~0.5s, tope 2s; corto a propósito
    before_sleep=before_sleep_log(logger, logging.WARNING),  # loguea el reintento
    reraise=True,                                     # tras agotar, relanza la EXCEPCIÓN ORIGINAL
)
async def _ainvoke_with_retry(agent, messages):
    return await agent.ainvoke({"messages": messages})


# --- Confirmación asistida: heurística determinística sí/no/ambiguo -----
# Coincidencia EXACTA sobre el texto normalizado (lower, sin acentos, sin
# puntuación). A propósito conservadora: solo un sí/no claro coincide; CUALQUIER
# otra cosa (incl. "no sé", una pregunta) cae en AMBIGUO -> no ejecuta, no limpia
# la intención, repregunta. La regla de seguridad: ante la duda, NO se borra.
_AFIRMATIVO = {
    "si", "si borra", "si borrala", "si quiero", "si claro", "claro", "claro que si",
    "confirmo", "confirmar", "confirmado", "dale", "ok", "okay", "vale", "hazlo",
    "adelante", "borra", "borrala", "borralo", "eliminala", "elimina", "sip", "yes",
}
_NEGATIVO = {
    "no", "no borres", "no gracias", "no quiero", "mejor no", "cancela", "cancelar",
    "cancelado", "dejalo", "olvidalo", "nop", "nel", "para", "detente",
}


# --- Propuestas sin acción detrás -----------------------------------------
# Las tools piden la confirmación con "Responde sí para confirmar o no para
# cancelar". El modelo a veces COPIA ese texto del historial sin llamar a la tool:
# el usuario ve una propuesta, pero no hay nada pendiente, y su "sí" le llegaría al
# modelo como un mensaje normal (que podría responder "listo, enviado" sin haber
# hecho nada). Este patrón reconoce una petición explícita de sí/no; las preguntas
# normales del modelo ("¿quieres que lo busque?") no lo cumplen.
_CONFIRM_REQUEST = re.compile(
    r"\bs[ií]\s+para\s+confirmar\b"                          # "sí para confirmar"
    r"|\bs[ií]\s+o\s+no\b"                                   # "responde sí o no"
    r"|\b(responde|contesta)\s+(con\s+(un\s+)?)?[\"'«“]?sí\b",  # "responde sí", "contesta con un sí"
    re.IGNORECASE,
)

_PENDING_MINUTES = pending._TTL // 60

_NO_ACTION_PREPARED = (
    "Iba a pedirte que confirmaras, pero no llegué a preparar la acción, así que no "
    "hay nada que confirmar ni se hizo nada. Pídemelo de nuevo."
)


def _asks_confirmation(text: str) -> bool:
    return bool(_CONFIRM_REQUEST.search(text or ""))


def _stale_confirmation_reply(user_input: str, history: list[dict]) -> str | None:
    """Respuesta fija para un sí/no que contesta a una propuesta que ya NO está
    pendiente: caducó, o el modelo la imitó sin crearla. Sin esto, el "sí" iría al
    modelo como mensaje normal. Solo actúa si el último mensaje del asistente pedía
    confirmación, para no cortar un "sí" a una pregunta normal del modelo."""
    last = next((m for m in reversed(history) if m.get("role") == "assistant"), None)
    if last is None or not _asks_confirmation(last.get("content", "")):
        return None
    norm = _normalize_reply(user_input)
    if norm in _AFIRMATIVO:
        return (
            "No hay ninguna acción esperando tu confirmación: la propuesta caducó (duran "
            f"{_PENDING_MINUTES} minutos) o no llegué a prepararla. No hice nada; "
            "pídemelo de nuevo."
        )
    if norm in _NEGATIVO:
        return "No había ninguna acción esperando confirmación, así que no hice nada."
    return None


def _normalize_reply(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower().strip())
    s = "".join(c for c in s if not unicodedata.combining(c))   # sin acentos
    s = re.sub(r"[^\w\s]", " ", s)                              # sin puntuación
    return re.sub(r"\s+", " ", s).strip()


async def _handle_confirmation(conversation_id: int, user_input: str, intent: dict) -> str:
    """Interpreta la respuesta del usuario a una acción CONFIRMABLE pendiente
    (delete_task, create_calendar_event, ...). Determinística; lo ambiguo SIEMPRE
    falla hacia no-ejecutar (no actúa, conserva la intención, repregunta).

    No menciona ninguna acción por nombre: tras un "sí" busca el ejecutor en
    CONFIRMABLE_ACTIONS[intent["action"]].
    """
    action = intent.get("action")
    description = intent.get("description", "la acción pendiente")
    norm = _normalize_reply(user_input)

    if norm in _AFIRMATIVO:
        perform_fn = CONFIRMABLE_ACTIONS.get(action)
        if perform_fn is None:
            # Defensivo: no debería pasar (la tool que la propuso la registró). Es
            # una intención muerta -> la limpiamos para no quedar en bucle.
            await pending.clear_pending(conversation_id)
            logger.error("confirmed action not in registry",
                         extra={"action": action})
            return "No pude ejecutar la acción pendiente (acción desconocida)."
        result = await perform_fn(intent.get("args", {}))   # la acción real, aquí y solo aquí
        await pending.clear_pending(conversation_id)
        logger.info("confirmable action executed", extra={"action": action})
        return result

    if norm in _NEGATIVO:
        await pending.clear_pending(conversation_id)
        logger.info("confirmable action cancelled by user", extra={"action": action})
        return "Cancelado, no hice nada."

    # AMBIGUO: la regla de seguridad crítica. No ejecuta, NO limpia la intención
    # (sigue viva para un sí/no posterior), repregunta.
    logger.info("confirmation ambiguous, NOT executing", extra={"action": action})
    return f"No entendí. ¿Confirmas {description}? Responde sí o no."


def _proposal_text(intent: dict) -> str:
    """La pregunta LITERAL que guardó la tool (lo que ejecutará el "sí") y, si en el
    mismo turno se pidieron otras acciones confirmables que quedaron sin proponer
    (solo cabe una pendiente, ver pending.py), un aviso para que el usuario no las
    dé por hechas."""
    text = intent["question"]
    deferred = intent.get("deferred") or []
    if deferred:
        text += (
            f"\n\nTambién me pediste {'; '.join(deferred)}, pero solo puedo dejar una "
            "acción esperando confirmación. Pídemelo de nuevo cuando respondas a esta."
        )
    return text


async def run_agent(conversation_id: int, user_input: str) -> str:
    conversation_id_var.set(str(conversation_id))
    user_input_var.set(user_input)
    logger.info("agent run start", extra={"event": "agent_start"})

    history = await memory.load_history(conversation_id)

    # Bifurcación de confirmación: si hay una acción irreversible pendiente, este
    # turno es la RESPUESTA a la propuesta -> NO se llama al modelo.
    pending_intent = await pending.get_pending(conversation_id)
    if pending_intent is not None:
        answer = await _handle_confirmation(conversation_id, user_input, pending_intent)
        await memory.append(conversation_id, "user", user_input)
        await memory.append(conversation_id, "assistant", answer)
        await _persist_messages(conversation_id, user_input, answer)
        await _maybe_extract_memories(conversation_id)
        return answer

    # Un sí/no a una propuesta que ya no está pendiente tampoco va al modelo.
    stale = _stale_confirmation_reply(user_input, history)
    if stale is not None:
        logger.info("confirmation without pending action, NOT calling the model")
        await memory.append(conversation_id, "user", user_input)
        await memory.append(conversation_id, "assistant", stale)
        await _persist_messages(conversation_id, user_input, stale)
        await _maybe_extract_memories(conversation_id)
        return stale

    messages = _to_lc_messages(history) + [HumanMessage(content=user_input)]

    logger.info(
        "enviando al modelo",
        extra={"event": "llm_call", "n_messages": len(messages)},
    )

    try:
        result = await _ainvoke_with_retry(_agent_singleton(), messages)
    except (httpx.ConnectError, httpx.TimeoutException, ConnectionError) as exc:
        logger.error(
            "ollama unreachable after retries",
            extra={"event": "upstream_error", "exc_type": type(exc).__name__},
            exc_info=exc,
        )
        raise UpstreamUnavailable() from exc
    answer = result["messages"][-1].content

    # --- Override "propuesta sin modelo" -------------------------------------
    # Al entrar NO había pending (si lo hubiera, habríamos ido por la rama de
    # confirmación y no llegaríamos aquí). Si tras la llamada al modelo SÍ hay un
    # pending con `question`, una tool acaba de PROPONER una acción confirmable.
    # DESCARTAMOS la narración del modelo (tiende a decir "ya lo hice" al proponer) y
    # devolvemos la pregunta LITERAL que guardó la tool. El modelo sigue eligiendo la
    # tool; su prosa de la propuesta NO se muestra. Garantiza "lo mostrado = lo que se
    # ejecutará" — crítico para un correo, donde el modelo podría alterar
    # destinatario/cuerpo al narrar.
    new_pending = await pending.get_pending(conversation_id)
    if new_pending is not None and new_pending.get("question"):
        answer = _proposal_text(new_pending)
        logger.info("proposal override applied (no-model proposal)",
                    extra={"action": new_pending.get("action")})
    elif new_pending is None and _asks_confirmation(answer):
        # El modelo imitó una propuesta sin llamar a la tool: no hay nada que
        # confirmar. Se sustituye también en el historial, para que no la vuelva a copiar.
        answer = _NO_ACTION_PREPARED
        logger.warning("imitated proposal replaced: no pending action")

    # Memoria de corto plazo (Redis) + registro durable (Postgres).
    await memory.append(conversation_id, "user", user_input)
    await memory.append(conversation_id, "assistant", answer)
    await _persist_messages(conversation_id, user_input, answer)

    # Disparador de memoria de largo plazo (cada N mensajes).
    await _maybe_extract_memories(conversation_id)

    return answer


async def _proposal_event(conversation_id: int) -> dict | None:
    """Señal estructurada para la UI: si al CERRAR el turno sigue habiendo una
    acción pendiente de confirmar, devuelve un evento `proposal` (action +
    description) para que el frontend pinte la tarjeta Sí/No. Sin esto, la
    pregunta de la propuesta viaja como tokens indistinguibles de una respuesta
    normal y la UI no sabría mostrar botones.

    El clic en Sí/No NO añade un camino de ejecución nuevo: envía un 'sí'/'no'
    normal que la heurística determinística (_handle_confirmation) ya interpreta.
    Regla robusta: vale tanto para la propuesta inicial como para la repregunta
    ambigua (la intención sigue viva) y desaparece sola tras confirmar/cancelar
    (que limpian el pending)."""
    p = await pending.get_pending(conversation_id)
    if p is None:
        return None
    return {
        "type": "proposal",
        "action": p.get("action"),
        "description": p.get("description"),
    }


async def run_agent_stream(conversation_id: int, user_input: str):
    """Versión streaming de run_agent. Genera eventos (dicts) que el endpoint
    traduce a SSE. Emite tokens de la respuesta y señales de herramientas, y
    al cerrar persiste igual que run_agent (Redis + Postgres + extracción).
    """
    conversation_id_var.set(str(conversation_id))
    user_input_var.set(user_input)
    logger.info("agent run start", extra={"event": "agent_start", "stream": True})

    history = await memory.load_history(conversation_id)

    # Bifurcación de confirmación (igual que run_agent): si hay acción pendiente,
    # este turno la resuelve SIN llamar al modelo. No hay tokens del modelo que
    # transmitir -> se emite la respuesta como un único token + done.
    pending_intent = await pending.get_pending(conversation_id)
    if pending_intent is not None:
        answer = await _handle_confirmation(conversation_id, user_input, pending_intent)
        yield {"type": "token", "value": answer}
        await memory.append(conversation_id, "user", user_input)
        await memory.append(conversation_id, "assistant", answer)
        await _persist_messages(conversation_id, user_input, answer)
        await _maybe_extract_memories(conversation_id)
        # Repregunta ambigua: la intención sigue viva -> la tarjeta reaparece.
        proposal = await _proposal_event(conversation_id)
        if proposal is not None:
            yield proposal
        yield {"type": "done"}
        return

    # Un sí/no a una propuesta que ya no está pendiente tampoco va al modelo.
    stale = _stale_confirmation_reply(user_input, history)
    if stale is not None:
        logger.info("confirmation without pending action, NOT calling the model")
        yield {"type": "token", "value": stale}
        await memory.append(conversation_id, "user", user_input)
        await memory.append(conversation_id, "assistant", stale)
        await _persist_messages(conversation_id, user_input, stale)
        await _maybe_extract_memories(conversation_id)
        yield {"type": "done"}
        return

    messages = _to_lc_messages(history) + [HumanMessage(content=user_input)]

    logger.info(
        "enviando al modelo",
        extra={"event": "llm_call", "n_messages": len(messages)},
    )

    full_answer: list[str] = []
    # Override "propuesta sin modelo", versión streaming: si una tool PROPONE
    # una acción confirmable, capturamos su pregunta LITERAL y, desde ese punto,
    # SUPRIMIMOS la narración del modelo (no transmitimos su paráfrasis). Al cerrar
    # se emite el texto literal como respuesta autoritativa.
    proposal_question: str | None = None

    try:
        async for ev in _agent_singleton().astream_events(
            {"messages": messages}, version="v2"
        ):
            etype = ev["event"]

            if etype == "on_chat_model_stream":
                chunk = ev["data"]["chunk"]
                token = getattr(chunk, "content", "") or ""
                # Una vez detectada una propuesta confirmable, no se emite más texto
                # del modelo (su narración "ya lo hice" no debe verse).
                if token and proposal_question is None:
                    full_answer.append(token)
                    yield {"type": "token", "value": token}

            elif etype == "on_tool_start":
                yield {"type": "tool_start", "tool": ev.get("name", "")}

            elif etype == "on_tool_end":
                yield {"type": "tool_end", "tool": ev.get("name", "")}
                # ¿esta tool acaba de guardar un pending con `question`? -> propuesta.
                if proposal_question is None:
                    p = await pending.get_pending(conversation_id)
                    if p is not None and p.get("question"):
                        proposal_question = p["question"]
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        # El 200 (y cabeceras SSE) ya salieron: no podemos relanzar a un handler.
        # Emitimos un evento de error en el stream y cerramos sin persistir.
        logger.error(
            "ollama unreachable mid-stream",
            extra={"event": "upstream_error", "exc_type": type(exc).__name__},
            exc_info=exc,
        )
        yield {"type": "error", "message": "El servicio de inferencia no está disponible."}
        return

    if proposal_question is not None:
        # Reemplazo autoritativo: el usuario ve y aprueba el texto LITERAL, no la
        # prosa del modelo. (El modelo ya hizo su trabajo: elegir la tool.) Se relee
        # la pendiente para incluir el aviso de las propuestas que quedaron aplazadas.
        final = await pending.get_pending(conversation_id)
        answer = (
            _proposal_text(final) if final and final.get("question") else proposal_question
        )
        logger.info("proposal override applied (no-model proposal, stream)")
        yield {"type": "token", "value": answer}
    else:
        answer = "".join(full_answer).strip()
        if _asks_confirmation(answer) and await pending.get_pending(conversation_id) is None:
            # El modelo imitó una propuesta sin llamar a la tool. Sus tokens ya se
            # mostraron: `replace` le dice a la UI que sustituya ese texto.
            answer = _NO_ACTION_PREPARED
            logger.warning("imitated proposal replaced: no pending action (stream)")
            yield {"type": "replace", "value": answer}

    # Persistencia diferida (con la respuesta completa), igual que run_agent.
    await memory.append(conversation_id, "user", user_input)
    await memory.append(conversation_id, "assistant", answer)
    await _persist_messages(conversation_id, user_input, answer)
    await _maybe_extract_memories(conversation_id)

    # Si una tool acaba de PROPONER una acción confirmable, el pending sigue puesto
    # -> emite la señal para que la UI muestre la tarjeta Sí/No bajo la pregunta.
    proposal = await _proposal_event(conversation_id)
    if proposal is not None:
        yield proposal

    yield {"type": "done"}