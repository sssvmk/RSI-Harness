"""Content deletion (design §5.10): blobs and FTS rows go, events and the hash chain stay intact."""
from __future__ import annotations
import re

HASH_RE = re.compile(r"[0-9a-f]{64}")


async def delete_content(h, run_id: str, blob_hashes: list[str], requested_by: str, reason: str) -> dict:
    run = h.rm.get(run_id)
    deleted, fts_rows = [], 0
    for blob in blob_hashes:
        if h.blobs.delete(blob):
            deleted.append(blob)
        await h.writer.execute("UPDATE blobs SET deleted_at=? WHERE hash=?", (str(h.clock.now_ms()), blob))
    # derived records follow derived_from references (FTS rows for events whose payload references the deleted blobs)
    for ev in h.store.events(run_id):
        refs = set(HASH_RE.findall(str(ev["payload"])))
        derived = set(ev["payload"].get("derived_from", []))
        if (refs | derived) & set(blob_hashes):
            fts_rows += await h.writer.execute("DELETE FROM events_fts WHERE run_id=? AND seq=?", (run_id, ev["seq"]))
    await run.emit("content.deleted", {"blob_hashes": blob_hashes, "requested_by": requested_by, "reason": reason},
                   actor={"kind": "user", "id": requested_by})
    return {"deleted": len(deleted), "fts_rows": fts_rows}
