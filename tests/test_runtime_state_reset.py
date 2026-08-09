"""Focused coverage for MemPalace process-wide state reset helpers."""

from __future__ import annotations

from mempalace import miner, palace
from mempalace.backends import chroma as chroma_module
from mempalace.backends.chroma import ChromaBackend


class _CloseSpy:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def test_backend_reset_closes_each_cached_client_once_and_remains_reusable():
    backend = ChromaBackend()
    first = _CloseSpy()
    second = _CloseSpy()
    backend._clients = {"one": first, "duplicate": first, "two": second}
    backend._freshness = {"one": (1, 1.0), "duplicate": (1, 1.0), "two": (2, 2.0)}

    backend.reset()

    assert backend._clients == {}
    assert backend._freshness == {}
    assert backend._closed is False
    assert first.close_calls == 1
    assert second.close_calls == 1


def test_backend_reset_opens_a_fresh_client_on_the_next_request(tmp_path, monkeypatch):
    backend = ChromaBackend()
    old_client = _CloseSpy()
    fresh_client = _CloseSpy()
    palace_path = str(tmp_path / "palace")
    backend._clients = {palace_path: old_client}
    backend._freshness = {palace_path: (1, 1.0)}
    monkeypatch.setattr(ChromaBackend, "_prepare_palace_for_open", lambda _path: None)
    monkeypatch.setattr(ChromaBackend, "_db_stat", staticmethod(lambda _path: (0, 0)))
    monkeypatch.setattr(chroma_module.chromadb, "PersistentClient", lambda **_kwargs: fresh_client)

    backend.reset()

    assert backend._client(palace_path) is fresh_client
    assert old_client.close_calls == 1
    assert backend._clients == {palace_path: fresh_client}


def test_reset_default_backend_delegates_to_the_process_singleton(monkeypatch):
    backend = ChromaBackend()
    calls = []

    monkeypatch.setattr(backend, "reset", lambda: calls.append("reset"))
    monkeypatch.setattr(palace, "_DEFAULT_BACKEND", backend)

    palace.reset_default_backend()

    assert calls == ["reset"]


def test_reset_entity_registry_cache_removes_session_shared_values(monkeypatch):
    monkeypatch.setitem(miner._ENTITY_REGISTRY_CACHE, "mtime", 123.0)
    monkeypatch.setitem(miner._ENTITY_REGISTRY_CACHE, "names", frozenset({"stale"}))
    monkeypatch.setitem(miner._ENTITY_REGISTRY_CACHE, "raw", {"known": ["stale"]})

    miner.reset_entity_registry_cache()

    assert miner._ENTITY_REGISTRY_CACHE == {
        "mtime": None,
        "names": frozenset(),
        "raw": {},
    }
