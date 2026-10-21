import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { canonicalize, sha256Hex } from "./util.js";
export const INLINE_LIMIT = 4096; // proposal P8

export class BlobStore {
  root: string;
  constructor(root: string) { this.root = path.join(root, "blobs"); fs.mkdirSync(this.root, { recursive: true }); }
  p(h: string) { return path.join(this.root, h.slice(0, 2), h); }
  put(data: Uint8Array | string): string {
    const buf = typeof data === "string" ? Buffer.from(data, "utf8") : Buffer.from(data);
    const h = sha256Hex(buf), p = this.p(h);
    if (fs.existsSync(p)) return h;
    fs.mkdirSync(path.dirname(p), { recursive: true });
    const tmp = path.join(path.dirname(p), `.tmp-${process.pid}-${Math.random().toString(36).slice(2)}`);
    const fd = fs.openSync(tmp, "w");
    try { fs.writeSync(fd, buf); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    fs.renameSync(tmp, p);
    return h;
  }
  get(h: string): Buffer {
    const b = fs.readFileSync(this.p(h));
    if (sha256Hex(b) !== h) throw new Error(`blob ${h} failed integrity check`);
    return b;
  }
  has(h: string) { return fs.existsSync(this.p(h)); }
  delete(h: string) { if (this.has(h)) { fs.unlinkSync(this.p(h)); return true; } return false; }
  putJson(o: unknown) { return this.put(canonicalize(o)); }
  getJson(h: string) { return JSON.parse(this.get(h).toString("utf8")); }
  inlineOrRef(o: unknown, inlineKey: string, refKey: string): Record<string, any> {
    const raw = canonicalize(o);
    return Buffer.byteLength(raw) <= INLINE_LIMIT ? { [inlineKey]: o } : { [refKey]: this.put(raw) };
  }
}
void createHash;
