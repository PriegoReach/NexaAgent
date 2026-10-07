"""Recuperación híbrida RRF (vectorial + léxico) + re-ranking con cross-encoder.

`_retrieve` trae FETCH_N candidatos híbridos y el cross-encoder los reordena al
top-k. `dual=True` añade una segunda rama vectorial sobre la traducción inglesa de
la query (fusionada por RRF) para recuperar documentos en inglés ante preguntas en
español; si la traducción falla, se degrada a solo-es sin romper la búsqueda.
"""
import asyncio
import logging

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.db.worker_db import worker_session
from app.rag.embeddings import get_embeddings
from app.rag.reranker import rerank, rerank_order

logger = logging.getLogger("nexa.retriever")

RRF_K = 60      # constante estándar del algoritmo Reciprocal Rank Fusion
FETCH_N = 20    # candidatos que el retriever pasa al re-ranker
DUAL_FETCH_N = 40  # el pool dual fusiona 3 ramas (vec_es + vec_en + lex): necesita más holgura

_TRANSLATE_TIMEOUT = 30
_TRANSLATE_PROMPT = (
    "Translate the following search query to English. "
    "Output ONLY the translation, no quotes, no explanation.\n\nQuery: {q}"
)


async def _translate_to_english(query: str) -> str | None:
    """Traduce la query al inglés con el LLM. Devuelve None ante cualquier fallo
    (timeout/red/respuesta vacía) -> la dual degrada a solo-es. Cliente httpx efímero
    por llamada: corre en el loop del ThreadPoolExecutor de la tool, no se reutiliza
    entre loops."""
    try:
        async with httpx.AsyncClient(timeout=_TRANSLATE_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.ollama_base_url}/api/chat",
                json={
                    "model": settings.ollama_model,
                    "messages": [{"role": "user", "content": _TRANSLATE_PROMPT.format(q=query)}],
                    "stream": False,
                    "options": {"temperature": 0},
                },
            )
            resp.raise_for_status()
        out = (resp.json().get("message", {}).get("content") or "").strip()
        if not out:
            return None
        logger.info("dual: query translated", extra={"event": "dual_translate"})
        return out
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning(
            "dual: translation failed, degrading to es-only",
            extra={"event": "dual_degraded", "exc_type": type(exc).__name__},
        )
        return None


async def _retrieve_ids(query: str, k: int, dual: bool = False) -> list[tuple[int, str]]:
    """Búsqueda híbrida RRF; devuelve [(document_id, content), ...] (los ids permiten
    medir Recall por doc en el harness; producción usa solo el content vía _retrieve).

    Ramas fusionadas por RRF:
      - vec_es : ranking vectorial con la query original (siempre).
      - lex    : ranking léxico 'es_simple' (siempre; no ayuda cross-lingual pero
                 mantiene sano el español).
      - vec_en : ranking vectorial con la traducción inglesa (SOLO si dual y la
                 traducción no falló). Es la rama que recupera el doc inglés al pool.
    """
    embedder = get_embeddings()
    qvec_es = embedder.embed_query(query)
    pool = k * 5  # candidatos por rama antes de fusionar

    params = {
        "qvec_es": str(qvec_es),
        "qtext": query,
        "pool": pool,
        "rrf_k": RRF_K,
        "k": k,
    }

    # Rama inglesa (dual): si la traducción falla, se omite y queda como búsqueda es.
    vec_en_cte = ""
    vec_en_union = ""
    if dual:
        en_query = await _translate_to_english(query)
        if en_query:
            qvec_en = embedder.embed_query(en_query)
            params["qvec_en"] = str(qvec_en)
            vec_en_cte = """,
                vec_en AS (
                    SELECT id, document_id, content,
                           row_number() OVER (ORDER BY embedding <=> :qvec_en) AS rnk
                    FROM document_chunks
                    ORDER BY embedding <=> :qvec_en
                    LIMIT :pool
                )"""
            vec_en_union = "UNION ALL SELECT id, document_id, content, rnk FROM vec_en"

    sql = f"""
        WITH vec AS (
            SELECT id, document_id, content,
                   row_number() OVER (ORDER BY embedding <=> :qvec_es) AS rnk
            FROM document_chunks
            ORDER BY embedding <=> :qvec_es
            LIMIT :pool
        ),
        lex AS (
            SELECT id, document_id, content,
                   row_number() OVER (
                       ORDER BY ts_rank(content_tsv,
                                        plainto_tsquery('es_simple', :qtext)) DESC
                   ) AS rnk
            FROM document_chunks
            WHERE content_tsv @@ plainto_tsquery('es_simple', :qtext)
            LIMIT :pool
        ){vec_en_cte},
        fused AS (
            SELECT id, document_id, content, SUM(1.0 / (:rrf_k + rnk)) AS score
            FROM (
                SELECT id, document_id, content, rnk FROM vec
                UNION ALL
                SELECT id, document_id, content, rnk FROM lex
                {vec_en_union}
            ) t
            GROUP BY id, document_id, content
        )
        SELECT document_id, content FROM fused
        ORDER BY score DESC
        LIMIT :k
    """
    async with worker_session() as session:
        rows = await session.execute(text(sql), params)
        return [(r[0], r[1]) for r in rows.fetchall()]


async def _retrieve(query: str, k: int, dual: bool = False) -> list[str]:
    """Igual que _retrieve_ids pero devuelve solo el content (lo que usa producción)."""
    return [c for _, c in await _retrieve_ids(query, k, dual=dual)]


async def search(query: str, k: int = 8, dual: bool = False) -> list[str]:
    """Recupera FETCH_N candidatos híbridos y el cross-encoder los reordena al top-k.

    El re-ranking es sync (GPU); se delega a un hilo con asyncio.to_thread para no
    bloquear el event loop del caller. `dual=True` añade la rama vectorial inglesa y
    amplía el fetch a DUAL_FETCH_N; el reranker (multilingüe) ordena el pool combinado
    con la query original.
    """
    fetch_n = DUAL_FETCH_N if dual else FETCH_N
    candidates = await _retrieve(query, k=fetch_n, dual=dual)
    if not candidates:
        return []
    return await asyncio.to_thread(rerank, query, candidates, k)


async def search_with_sources(query: str, k: int = 8, dual: bool = False) -> list[tuple[str, str]]:
    """Como search, pero cada fragmento va con el nombre del documento del que sale,
    para que el agente pueda citar la fuente: [(contenido, nombre_de_archivo), ...]."""
    fetch_n = DUAL_FETCH_N if dual else FETCH_N
    candidates = await _retrieve_ids(query, k=fetch_n, dual=dual)
    if not candidates:
        return []
    order = await asyncio.to_thread(rerank_order, query, [c for _, c in candidates], k)
    names = await _document_names({doc_id for doc_id, _ in candidates})
    return [(candidates[i][1], names.get(candidates[i][0], "documento")) for i in order]


async def _document_names(document_ids: set[int]) -> dict[int, str]:
    async with worker_session() as session:
        rows = await session.execute(
            text("SELECT id, filename FROM documents WHERE id = ANY(:ids)"),
            {"ids": list(document_ids)},
        )
        return {r.id: r.filename for r in rows}
