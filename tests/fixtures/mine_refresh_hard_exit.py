"""Hard-exit this fixture child after refresh COMPLETE or durable preparation."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Reuse the suite's isolated HOME, native thread limits and offline embedding.
import conftest  # noqa: E402
from mempalace import mine_refresh, miner  # noqa: E402
from mempalace.backends.chroma import ChromaBackend  # noqa: E402


def hard_exit(*_args):
    os._exit(29)


def prepare_then_exit(path, payload, **kwargs):
    result = original_publish(path, payload, **kwargs)
    if payload["event"] == "prepared":
        os._exit(31)
    return result


if __name__ == "__main__":
    project, palace, plan, progress = sys.argv[1:5]
    ChromaBackend._resolve_embedding_function = staticmethod(
        lambda: conftest._TEST_EMBEDDING_FUNCTION
    )
    miner._compute_topic_tunnels_for_wing = lambda _wing: 0
    if sys.argv[5:] == ["--after-prepared"]:
        original_publish = mine_refresh._atomic_write_json
        mine_refresh._atomic_write_json = prepare_then_exit
    else:
        assert not sys.argv[5:], "unknown fixture crash point"
        mine_refresh._complete_refresh = hard_exit
    miner.mine(project, palace, manifest_path=plan, progress_jsonl=progress)
    raise AssertionError("fixture did not reach the refresh crash window")
