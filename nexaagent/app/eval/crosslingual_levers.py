"""Medición de palancas cross-lingual (P29, diagnóstico — NO implementa nada).

A la luz del diagnóstico decisivo:
  - es-only deja el doc inglés ÚLTIMO en vectorial (nomic English-bias).
  - es+en (concatenado) lo deja casi último (el español arrastra el vector).
  - en-only (traducción) lo sube a top-1..4.
  - el reranker multilingüe coloca el doc correcto #1 SI lo ve.

Mide END-TO-END (retrieve top-20 -> rerank top-8 con la query ES) tres configs,
sobre el set cross-lingual Y el de regresión español (para ver si una palanca que
arregla es->en ROMPE el monolingüe):
  - baseline : RRF(vec_es, lex_es 'es_simple')                  [producción]
  - translate: RRF(vec_en, lex_en 'es_simple')                  [traducción pura]
  - dual     : RRF(vec_es, vec_en, lex_es 'es_simple')          [doble búsqueda]

No toca producción. Reranquea SIEMPRE con la query española original.
"""
import asyncio
import json
from pathlib import Path

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.db.worker_db import worker_session
from app.rag.embeddings import get_embeddings
from app.rag.reranker import rerank

RRF_K = 60
FETCH_N = 20
RERANK_K = 8
POOL = 100


def _translate(spanish: str) -> str:
    prompt = ("Translate the following search query to English. "
              "Output ONLY the translation.\n\nQuery: " + spanish)
    r = httpx.post(f"{settings.ollama_base_url}/api/chat",
                   json={"model": settings.ollama_model, "stream": False,
                         "messages": [{"role": "user", "content": prompt}],
                         "options": {"temperature": 0}}, timeout=60)
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


async def _vec(query: str) -> list[tuple]:
    qv = get_embeddings().embed_query(query)
    async with worker_session() as s:
        rows = await s.execute(
            text("SELECT id, document_id, content FROM document_chunks "
                 "ORDER BY embedding <=> :v LIMIT :p"),
            {"v": str(qv), "p": POOL})
        return [(r[0], r[1], r[2]) for r in rows.fetchall()]


async def _lex(query: str, cfg: str) -> list[tuple]:
    async with worker_session() as s:
        rows = await s.execute(
            text(f"SELECT id, document_id, content FROM document_chunks "
                 f"WHERE to_tsvector('{cfg}', content) @@ plainto_tsquery('{cfg}', :q) "
                 f"ORDER BY ts_rank(to_tsvector('{cfg}', content), "
                 f"plainto_tsquery('{cfg}', :q)) DESC LIMIT :p"),
            {"q": query, "p": POOL})
        return [(r[0], r[1], r[2]) for r in rows.fetchall()]


def _rrf(ranklists: list[list[tuple]], k: int) -> list[tuple]:
    """Fusiona varias listas (cada una ordenada) por Reciprocal Rank Fusion."""
    score: dict = {}
    meta: dict = {}
    for rl in ranklists:
        for rank, (cid, did, content) in enumerate(rl, start=1):
            score[cid] = score.get(cid, 0.0) + 1.0 / (RRF_K + rank)
            meta[cid] = (did, content)
    ordered = sorted(score, key=lambda c: score[c], reverse=True)[:k]
    return [(c, meta[c][0], meta[c][1]) for c in ordered]


def _target_rank(cands: list[tuple], case: dict) -> int | None:
    for i, (_, did, content) in enumerate(cands, start=1):
        if ("doc_id" in case and did == case["doc_id"]) or \
           ("huella" in case and case["huella"] in content):
            return i
    return None


async def _configs_for(case: dict, en: str) -> dict:
    es = case["pregunta"]
    vec_es, vec_en = await _vec(es), await _vec(en)
    lex_es = await _lex(es, "es_simple")
    lex_en = await _lex(en, "es_simple")
    pools = {
        "baseline":  _rrf([vec_es, lex_es], FETCH_N),
        "translate": _rrf([vec_en, lex_en], FETCH_N),
        "dual":      _rrf([vec_es, vec_en, lex_es], FETCH_N),
    }
    out = {}
    for name, cands in pools.items():
        retr = _target_rank(cands, case)
        reranked = await asyncio.to_thread(rerank, es, [c[2] for c in cands], RERANK_K)
        c2d = {c[2]: c[1] for c in cands}
        rr = _target_rank([(None, c2d.get(rc), rc) for rc in reranked], case)
        out[name] = (retr, rr)
    return out


async def run(path: str, label: str):
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    names = ["baseline", "translate", "dual"]
    agg = {n: [] for n in names}
    print(f"\n### {label}  ({len(cases)} pares)")
    print(f"  {'caso':26}" + "".join(f"{n[:9]:>12}" for n in names) + "   (retr|rrnk)")
    for case in cases:
        en = _translate(case["pregunta"])
        res = await _configs_for(case, en)
        for n in names:
            agg[n].append(res[n][1])
        cells = "".join(f"{(str(res[n][0]) or '-')+'|'+(str(res[n][1]) if res[n][1] else '-'):>12}" for n in names)
        print(f"  {(case.get('etiqueta') or case['pregunta'])[:25]:26}{cells}")
    print(f"  {'RECALL@1':26}" + "".join(f"{sum(1 for r in agg[n] if r==1)/len(cases):>12.3f}" for n in names))
    print(f"  {'RECALL@5':26}" + "".join(f"{sum(1 for r in agg[n] if r and r<=5)/len(cases):>12.3f}" for n in names))


async def main():
    await run("app/eval/datasets/crosslingual.json", "CROSS-LINGUAL (ES query -> EN doc)")
    await run("app/eval/datasets/spanish_regression.json", "REGRESIÓN ESPAÑOL (ES query -> ES doc)")


if __name__ == "__main__":
    asyncio.run(main())
