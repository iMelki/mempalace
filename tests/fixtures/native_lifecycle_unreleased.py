"""Caller-faithful negative fixture for leaked Chroma/ONNX threads (#50).

This file is invoked explicitly. It is not part of the default ``test_*.py``
collection. The fixture first proves the leak is visible, then fails the gate
with a stable reason. A ``finally`` block joins the waiting workers so an
accidental collection cannot poison the parent suite.
"""

from mempalace.native_lifecycle import (
    NativeSessionRegistry,
    SyntheticNativePool,
    inspect_native_leak,
    sample_native_resources,
)


def test_unreleased_native_pool_fails_the_lifecycle_gate():
    baseline = sample_native_resources()
    registry = NativeSessionRegistry()
    pool = SyntheticNativePool(workers=8, name_prefix="fixture-onnx")
    registry.track(pool)
    try:
        report = inspect_native_leak(registry, baseline)
        assert report["live_owners"] == 1
        assert report["python_thread_delta"] >= 8
        raise AssertionError("native-lifecycle-leak: unclosed chroma/onnx session")
    finally:
        pool.close()
