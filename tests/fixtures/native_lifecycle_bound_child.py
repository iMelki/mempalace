"""Pre-push-shaped child: open a Chroma client and do not close it.

The suite autouse bound in ``tests/conftest.py`` must track and close the
client. Invoked explicitly with ``pytest -q -p no:cacheprovider``.
"""


def test_child_opens_chroma_client_without_explicit_close(tmp_path):
    import chromadb

    palace = tmp_path / "palace"
    palace.mkdir()
    chromadb.PersistentClient(path=str(palace))
