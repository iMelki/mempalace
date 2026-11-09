"""One exception identity for imported and ``python -m`` snapshot entrypoints."""


class PalaceSnapshotError(RuntimeError):
    """A snapshot precondition, lease, or proof failed. Always fail closed."""
