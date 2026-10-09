"""Verify a pending refresh's committed ancestor and its actual current head."""

from .mine_progress import MineProgressError
from .receipt_verifier import ReceiptVerificationError, verify_receipt
from .write_receipts import ReceiptIdentityError


def _verify_head(receipt, verification_args):
    if receipt.get("disposition") not in {"WRITE", "UNCHANGED", "ZERO_OUTPUT"}:
        raise MineProgressError("pending refresh head has an unsupported terminal disposition")
    if receipt["source"]["version_hash"] != receipt["source"]["content_hash"]:
        raise MineProgressError("pending refresh head has an incompatible source version")
    try:
        proof = verify_receipt(
            receipt,
            collections={
                "drawers": verification_args["collection"],
                "closets": verification_args["closets_col"],
            },
            current_source_content_hash=receipt["source"]["content_hash"],
            store=verification_args["receipt_store"],
        )
    except (ReceiptVerificationError, ReceiptIdentityError, ValueError) as exc:
        raise MineProgressError("pending refresh head could not be verified") from exc
    if proof.status != "represented":
        raise MineProgressError("pending refresh head is not represented")
    return proof


def recover_pending_head(current, state, verification_args):
    """Require the prepared refresh to be a committed link in the head's chain."""
    store = verification_args["receipt_store"]
    # The current reader has already rejected forks, missing links and corruption.
    candidates = store._complete_events_for_source(current["source"]["identity"])
    receipt = current
    seen = set()
    while receipt["receipt_id"] not in seen:
        seen.add(receipt["receipt_id"])
        predecessor = receipt.get("relations", {}).get("predecessor_receipt_id")
        if predecessor == state["receipt_id"]:
            pending_hash = state["pending"]["item"]["content_hash"]
            source = receipt["source"]
            if (
                source["content_hash"] == pending_hash
                and source["version_hash"] == pending_hash
                and receipt["producer"]["config"]["digest"]
                == verification_args["receipt_run"].config_digest
            ):
                return _verify_head(current, verification_args)
            break
        if predecessor not in candidates:
            break
        receipt = candidates[predecessor][0]
    raise MineProgressError("pending refresh has no matching committed refresh ancestor")


def verify_recorded_head(state, verification_args):
    """Re-prove a recovered head after a second crash before any source rewrite."""
    from .miner import _verify_manifest_source_receipt

    recovered = state.get("recovered_head")
    if recovered is None:
        return _verify_manifest_source_receipt(item=state["item"], **verification_args)
    store = verification_args["receipt_store"]
    source_identity = store.source_identity(str(verification_args["filepath"]), local_path=True)
    current = store.find_current_read_only(
        source_identity,
        content_hash=recovered["content_hash"],
        version_digest=recovered["content_hash"],
        config_digest=recovered["config_digest"],
    )
    if current is None or current["receipt_id"] != state["receipt_id"]:
        raise MineProgressError("completed source no longer names the recorded current receipt")
    pending = state["recovery_pending"]
    original = {"receipt_id": pending["previous_receipt_id"], "pending": pending}
    return current, recover_pending_head(current, original, verification_args)
