"""Tests del harness de evaluación de recuperación.

Dos niveles, por la tensión que la Parte 15 ya marcó (nada de Ollama/no-determinismo
en CI):

  1. test_recall_mrr_math — LIGERO y DETERMINISTA: monkeypatchea `search` con
     chunks sintéticos y verifica la ARITMÉTICA (Recall@k, MRR, ranks). No toca
     Ollama ni la BD. Es el guardián de que el harness no "mida mentiras": si el
     cálculo regresiona, este test lo caza en la suite estándar.

  2. test_retrieval_recall_floor_real — PISO sobre datos REALES: corre el dataset
     contra la BD con los chunks + Ollama. Skippeado por defecto. OJO: hoy no hay
     dónde activarlo con pytest: el conftest fuerza la BD `<db>_test`, que no tiene el
     corpus, y la imagen de la API no trae pytest. La medición real se hace con el CLI:
         docker compose exec api python -m app.eval.retrieval
"""
import os

import pytest

from app.eval import retrieval


async def test_recall_mrr_math(tmp_path, monkeypatch):
    """La aritmética del harness, con search controlado: ranks [1, 3, None]."""
    dataset = tmp_path / "mini.yaml"
    dataset.write_text(
        "- pregunta: q-rank1\n  huella: HIT1\n  proyecto: A\n"
        "- pregunta: q-rank3\n  huella: HIT3\n  proyecto: B\n"
        "- pregunta: q-miss\n  huella: NOPE\n  proyecto: C\n",
        encoding="utf-8",
    )

    async def fake_search(query: str, k: int) -> list[str]:
        if query == "q-rank1":
            return ["xx HIT1 xx", "a", "b", "c"]      # huella en rank 1
        if query == "q-rank3":
            return ["a", "b", "yy HIT3 yy", "c"]      # huella en rank 3
        return ["a", "b", "c", "d"]                    # huella ausente → miss

    monkeypatch.setattr(retrieval, "search", fake_search)

    recall, mrr, ranks = await retrieval.evaluate(str(dataset))

    assert [r for _, r in ranks] == [1, 3, None]
    assert recall[1] == pytest.approx(1 / 3)   # solo q-rank1 está en top-1
    assert recall[3] == pytest.approx(2 / 3)   # q-rank1 y q-rank3 en top-3
    assert recall[5] == pytest.approx(2 / 3)   # el miss no entra ni con k mayor
    assert recall[8] == pytest.approx(2 / 3)
    assert mrr == pytest.approx((1 / 1 + 1 / 3 + 0) / 3)


@pytest.mark.skipif(
    not os.getenv("EVAL_REAL"),
    reason="piso de regresión sobre datos reales; requiere la BD con el corpus + Ollama. "
    "Para medir: docker compose exec api python -m app.eval.retrieval",
)
async def test_retrieval_recall_floor_real():
    """Piso de regresión sobre el corpus REAL single-copy (tras dedup de los
    triplicados docs 6/7/8 → queda solo doc 8).

    Medición del 2026-05-29 sobre corpus limpio: Recall@8 = 1.000, Recall@5 = 1.000,
    Recall@3 = 0.889, MRR = 0.744. Hallazgo: el corpus TRIPLICADO previo HUNDÍA la
    recuperación — la pregunta 'responsable de Rubí' pasó de miss (fuera de top-8)
    a rank 5 al quitar las copias redundantes que amontonaban el top-k. El piso
    exige que los 9 casos recuperen en top-8; si un cambio futuro (chunk_size,
    umbral, modelo) saca alguno, salta."""
    recall, _, _ = await retrieval.evaluate(
        "app/eval/datasets/retrieval_proyectos.yaml"
    )
    assert recall[8] == 1.0, f"Recall@8 cayó a {recall[8]:.3f} — la recuperación regresionó"
