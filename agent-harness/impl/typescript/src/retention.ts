/** Content deletion (design §5.10): blobs and FTS rows go, events and the hash chain stay intact. */
export async function deleteContent(h: any, runId: string, blobHashes: string[], requestedBy: string, reason: string) {
  const run = h.rm.get(runId); let deleted = 0, ftsRows = 0;
  for (const b of blobHashes) { if (h.blobs.delete(b)) deleted++; await h.writer.execute("UPDATE blobs SET deleted_at=? WHERE hash=?", [String(h.clock.nowMs()), b]); }
  for (const ev of h.store.events(runId)) {
    const refs = new Set<string>(JSON.stringify(ev.payload).match(/[0-9a-f]{64}/g) ?? []); for (const d of ev.payload.derived_from ?? []) refs.add(d);
    if (blobHashes.some((b) => refs.has(b))) ftsRows += await h.writer.execute("DELETE FROM events_fts WHERE run_id=? AND seq=?", [runId, ev.seq]);
  }
  await run.emit("content.deleted", { blob_hashes: blobHashes, requested_by: requestedBy, reason }, { kind: "user", id: requestedBy });
  return { deleted, fts_rows: ftsRows };
}
