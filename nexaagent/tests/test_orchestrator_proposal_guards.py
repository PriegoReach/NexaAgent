"""Tests de las protecciones contra respuestas que aparentan una acción (orchestrator.py).

El modelo a veces COPIA del historial una propuesta ("Responde sí para confirmar…")
sin llamar a la tool: el usuario ve una propuesta, pero no hay nada pendiente, y su
"sí" le llegaría al modelo como un mensaje normal. Dos protecciones:
  - si la respuesta pide un sí/no y no hay acción pendiente, se sustituye por un
    aviso (en streaming, con un evento `replace`) y eso es lo que se guarda;
  - un sí/no que contesta a una propuesta que ya no está pendiente (caducó o se
    imitó) recibe una respuesta fija y no llega al modelo.
Y otra parecida: si el modelo ESCRIBE la llamada a una herramienta en vez de hacerla
('CallChecka_webhook({...})'), no se ejecutó nada; también se sustituye por un aviso.

Sin Ollama ni Redis: el modelo, la memoria de corto plazo y la acción pendiente se
sustituyen por dobles. Los mensajes van a la BD real de test.
"""
import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, AIMessageChunk
from sqlalchemy import text

from app.agent import memory, orchestrator, pending
from app.db.session import engine

_IMITATED = (
    "Voy a enviar el correo electrónico que has solicitado.\n\n"
    "Para: juan@example.com\nAsunto: Prueba\n\n"
    "¿Estás seguro de que quieres enviar este correo? "
    "Responde sí para confirmar o no para cancelar."
)
_REAL_QUESTION = "Voy a enviar este correo:\n\n…\n\n¿Lo envío? Responde sí para confirmar o no para cancelar."


@pytest_asyncio.fixture
async def conversation_id() -> int:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text("INSERT INTO conversations (title, created_at) VALUES ('t', now()) RETURNING id")
            )
        ).scalar_one()


@pytest.fixture
def env(monkeypatch):
    """Dobles del modelo, de la memoria de corto plazo y de la acción pendiente.

    `env["history"]` es lo que devuelve load_history; `env["pending"]` la acción
    pendiente (None = ninguna); `env["answer"]` lo que responde el modelo, y
    `env["tool_proposes"]`, si se pone, la acción que una tool deja pendiente
    durante la llamada al modelo."""
    state = {"history": [], "pending": None, "answer": "", "tool_proposes": None, "model_calls": 0}

    async def load_history(cid):
        return list(state["history"])

    async def append(cid, role, content):
        state["history"].append({"role": role, "content": content})

    async def get_pending(cid):
        return state["pending"]

    async def ainvoke(agent, messages):
        state["model_calls"] += 1
        if state["tool_proposes"] is not None:
            state["pending"] = state["tool_proposes"]
        return {"messages": [AIMessage(content=state["answer"])]}

    class _StreamingAgent:
        async def astream_events(self, payload, version):
            state["model_calls"] += 1
            for i in range(0, len(state["answer"]), 12):
                chunk = AIMessageChunk(content=state["answer"][i:i + 12])
                yield {"event": "on_chat_model_stream", "data": {"chunk": chunk}}

    monkeypatch.setattr(memory, "load_history", load_history)
    monkeypatch.setattr(memory, "append", append)
    monkeypatch.setattr(pending, "get_pending", get_pending)
    monkeypatch.setattr(orchestrator, "_ainvoke_with_retry", ainvoke)
    monkeypatch.setattr(orchestrator, "_agent_singleton", lambda: _StreamingAgent())
    return state


async def _stored_answer(cid: int) -> str:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT content FROM messages WHERE conversation_id = :cid "
                    "AND role = 'assistant' ORDER BY id DESC LIMIT 1"
                ),
                {"cid": cid},
            )
        ).scalar_one()


async def _stream(cid: int, user_input: str) -> list[dict]:
    return [ev async for ev in orchestrator.run_agent_stream(cid, user_input)]


@pytest.mark.parametrize(
    "answer",
    [
        "¿Lo envío? Responde sí para confirmar o no para cancelar.",
        "¿Confirmas el borrado? Responde sí o no.",
        "Si quieres que lo cree, contesta con un «sí».",
        "Para seguir, responde si para confirmar.",
    ],
)
def test_detects_explicit_confirmation_requests(answer):
    assert orchestrator._asks_confirmation(answer)


@pytest.mark.parametrize(
    "answer",
    [
        "¿Quieres que busque en tus documentos?",
        "Dime si quieres que agende la reunión.",
        "Tienes 2 tareas pendientes.",
        "Responde si prefieres otra hora y lo cambio.",
        "",
    ],
)
def test_ignores_normal_questions(answer):
    assert not orchestrator._asks_confirmation(answer)


async def test_imitated_proposal_is_replaced(env, conversation_id):
    env["answer"] = _IMITATED

    answer = await orchestrator.run_agent(conversation_id, "envía el correo a juan")

    assert answer == orchestrator._NO_ACTION_PREPARED
    assert await _stored_answer(conversation_id) == orchestrator._NO_ACTION_PREPARED
    # El historial tampoco guarda la imitación, para que el modelo no la vuelva a copiar.
    assert env["history"][-1]["content"] == orchestrator._NO_ACTION_PREPARED


async def test_imitated_proposal_is_replaced_in_stream(env, conversation_id):
    env["answer"] = _IMITATED

    events = await _stream(conversation_id, "envía el correo a juan")

    assert {"type": "replace", "value": orchestrator._NO_ACTION_PREPARED} in events
    assert not any(ev["type"] == "proposal" for ev in events)   # sin tarjeta Sí/No
    assert events[-1] == {"type": "done"}
    assert await _stored_answer(conversation_id) == orchestrator._NO_ACTION_PREPARED


async def test_normal_answer_is_untouched(env, conversation_id):
    env["answer"] = "¿Quieres que busque en tus documentos?"

    events = await _stream(conversation_id, "¿qué dice la política de vacaciones?")

    assert not any(ev["type"] == "replace" for ev in events)
    assert await _stored_answer(conversation_id) == "¿Quieres que busque en tus documentos?"


async def test_real_proposal_still_shows_the_tool_question(env, conversation_id):
    env["answer"] = "Listo, ya lo envié."   # la narración del modelo se descarta
    env["tool_proposes"] = {"action": "send_email", "description": "el envío", "question": _REAL_QUESTION}

    answer = await orchestrator.run_agent(conversation_id, "envía el correo a juan")

    assert answer == _REAL_QUESTION


@pytest.mark.parametrize("stream", [False, True], ids=["sync", "stream"])
async def test_yes_to_a_proposal_that_is_no_longer_pending_skips_the_model(
    env, conversation_id, stream
):
    """La propuesta caducó (o se imitó): el "sí" no llega al modelo, que podría
    responder "listo, enviado" sin haber hecho nada."""
    env["history"] = [
        {"role": "user", "content": "envía el correo a juan"},
        {"role": "assistant", "content": _REAL_QUESTION},
    ]

    if stream:
        events = await _stream(conversation_id, "Sí")
        answer = "".join(ev["value"] for ev in events if ev["type"] == "token")
    else:
        answer = await orchestrator.run_agent(conversation_id, "Sí")

    assert env["model_calls"] == 0
    assert answer.startswith("No hay ninguna acción esperando tu confirmación")
    assert "No hice nada" in answer
    assert await _stored_answer(conversation_id) == answer


async def test_no_to_a_proposal_that_is_no_longer_pending(env, conversation_id):
    env["history"] = [{"role": "assistant", "content": _REAL_QUESTION}]

    answer = await orchestrator.run_agent(conversation_id, "no")

    assert env["model_calls"] == 0
    assert answer == "No había ninguna acción esperando confirmación, así que no hice nada."


async def test_yes_to_a_normal_question_reaches_the_model(env, conversation_id):
    env["history"] = [{"role": "assistant", "content": "¿Quieres que busque en tus documentos?"}]
    env["answer"] = "Encontré la política de vacaciones."

    answer = await orchestrator.run_agent(conversation_id, "sí")

    assert env["model_calls"] == 1
    assert answer == "Encontré la política de vacaciones."


@pytest.mark.parametrize(
    "answer",
    [
        'CallChecka_webhook({"message": "El respaldo ha finalizado."})',
        "CallChecka la herramienta `search_knowledge_base` para encontrar el documento.",
        '<tool_call>\n{"name": "list_tasks", "arguments": {}}\n</tool_call>',
        'Voy a buscarlo: search_knowledge_base(query="vacaciones")',
        'send_email({"to": "juan@example.com", "subject": "Hola"})',
    ],
)
def test_detects_tool_calls_written_as_text(answer):
    assert orchestrator.is_tool_call_as_text(answer)


@pytest.mark.parametrize(
    "answer",
    [
        "Tienes 2 tareas pendientes: pagar la luz y llamar al contador.",
        "Según politica.pdf, son 15 días hábiles (más 2 por antigüedad).",
        'En JavaScript:\n```js\nguardar({"id": 1});\n```',
        "Busqué en tus documentos y no encontré nada sobre eso.",
        "Usé search_knowledge_base (la búsqueda en documentos) y no encontré nada.",
        "",
    ],
)
def test_ignores_normal_answers_and_code_blocks(answer):
    assert not orchestrator.is_tool_call_as_text(answer)


@pytest.mark.parametrize("stream", [False, True], ids=["sync", "stream"])
async def test_tool_call_written_as_text_is_replaced(env, conversation_id, stream):
    env["answer"] = 'CallChecka_webhook({"message": "El respaldo terminó."})'

    if stream:
        events = await _stream(conversation_id, "avisa al sistema que terminó el respaldo")
        assert {"type": "replace", "value": orchestrator._TOOL_CALL_AS_TEXT} in events
        assert events[-1] == {"type": "done"}
    else:
        answer = await orchestrator.run_agent(conversation_id, "avisa al sistema que terminó el respaldo")
        assert answer == orchestrator._TOOL_CALL_AS_TEXT

    assert await _stored_answer(conversation_id) == orchestrator._TOOL_CALL_AS_TEXT
    # El historial tampoco guarda la llamada escrita, para que el modelo no la repita.
    assert env["history"][-1]["content"] == orchestrator._TOOL_CALL_AS_TEXT
