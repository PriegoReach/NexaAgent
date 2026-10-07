"""Tests del dispositivo del reranker.

Antes, el reranker se cargaba siempre con device="cuda" y en fp16: en un equipo sin
GPU NVIDIA fallaba y con él todo el RAG. Ahora usa la GPU si la hay y, si no, la CPU
en fp32. El modelo real no se carga: CrossEncoder se sustituye por un doble.
"""
from typing import ClassVar

import pytest
import sentence_transformers
import torch

from app.rag import reranker


class _FakeCrossEncoder:
    instances: ClassVar[list["_FakeCrossEncoder"]] = []

    def __init__(self, name, device, max_length):
        self.device = device
        self.halved = False
        self.model = self
        _FakeCrossEncoder.instances.append(self)

    def half(self):
        self.halved = True

    def predict(self, pairs):
        return [len(chunk) for _, chunk in pairs]


@pytest.fixture
def fake_encoder(monkeypatch):
    _FakeCrossEncoder.instances = []
    monkeypatch.setattr(sentence_transformers, "CrossEncoder", _FakeCrossEncoder)
    monkeypatch.setattr(reranker, "_model", None)
    return _FakeCrossEncoder


@pytest.mark.parametrize("cuda, device, halved", [(True, "cuda", True), (False, "cpu", False)])
def test_uses_gpu_when_available_and_cpu_otherwise(fake_encoder, monkeypatch, cuda, device, halved):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: cuda)

    ranked = reranker.rerank("q", ["corto", "el más largo", "medio"], top_k=2)

    encoder = fake_encoder.instances[0]
    assert (encoder.device, encoder.halved) == (device, halved)
    assert ranked == ["el más largo", "corto"]
