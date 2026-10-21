import { describe, expect, it } from "vitest";
import { Block, GatewayError, Middleware, ModelGateway, ScriptedTransport, ConnectionResetError } from "../src/gateway.js";
import { CredentialUnavailable, DictSecretProvider, Redactor, Secret, TokenSource } from "../src/credentials.js";
import { FakeClock } from "../src/util.js";
import { oa } from "./helpers.js";

const MODELS = { main: { ref: "main", provider: "openai", model_id: "m", window: 1000, secret: "k" }, claude: { ref: "claude", provider: "anthropic", model_id: "c", window: 1000, secret: "k" } };
const REQ = { model_ref: "main", messages: [{ role: "user", parts: [{ type: "text", text: "hi" }] }], params: { max_output_tokens: 100 }, metadata: { run_id: "R", turn: 1 } };
const gw = (script: any[], mw: Middleware[] = [], secrets = new DictSecretProvider({ k: "sk-abc" })) => { const t = new ScriptedTransport(script), clock = new FakeClock(); return { g: new ModelGateway(MODELS, t, clock, secrets, mw, { rng: () => 0.5 }), t, clock }; };
describe("gateway", () => {
  it("success and provider-specific auth headers", async () => {
    const { g, t } = gw([oa("hello")]); const r = await g.call(REQ); expect(r.message.parts[0].text).toBe("hello"); expect(t.requests[0].headers.authorization).toBe("Bearer sk-abc");
    const r2 = await gw([[200, { content: [{ type: "text", text: "x" }], stop_reason: "end_turn", usage: { input_tokens: 1, output_tokens: 1 } }]]).g.call({ ...REQ, model_ref: "claude" }); expect(r2.finish_reason).toBe("stop");
  });
  it("transient errors retry with exponential full-jitter backoff then succeed", async () => {
    const { g, t, clock } = gw([[429, {}], [503, {}], new ConnectionResetError("rst"), oa("ok")]); const fails: any[] = [];
    const r = await g.call(REQ, undefined, async (i) => { fails.push(i); }); expect(r.attempt).toBe(4); expect(t.requests).toHaveLength(4); expect(fails.map((f) => f.attempt)).toEqual([1, 2, 3]);
    expect(clock.slept).toEqual([500, 1000, 2000]);   // rng fixed at 0.5 => half of each exponential cap (1s, 2s, 4s)
  });
  it("backoff is capped at 30 s", () => { expect(Math.max(...Array.from({ length: 50 }, (_, i) => ModelGateway.backoffMs(20, () => (i + 1) / 50)))).toBeLessThanOrEqual(30000); });
  it("gives up after max attempts", async () => { const { g, t } = gw(Array(4).fill([500, {}])); await expect(g.call(REQ)).rejects.toMatchObject({ code: "gateway.http_500" }); expect(t.requests).toHaveLength(4); });
  it.each([400, 401, 403, 404])("status %i is not retried", async (s) => { const { g, t } = gw([[s, { error: "bad" }]]); await expect(g.call(REQ)).rejects.toMatchObject({ retryable: false }); expect(t.requests).toHaveLength(1); });
  it("requires max_output_tokens", async () => { await expect(gw([oa("x")]).g.call({ ...REQ, params: {} })).rejects.toThrow(/max_output_tokens/); });
  it("safety hooks fail closed, optional hooks are skipped", async () => {
    const mkMw = (name: string, optional: boolean, fn: (r: any) => any) => Object.assign(new Middleware(), { name, optional, preRequest: async (r: any) => fn(r) });
    const screen = mkMw("screen", false, (r) => { if (JSON.stringify(r).includes("attack")) throw new Block("injection"); return r; });
    const a = gw([oa("x")], [screen]); await expect(a.g.call({ ...REQ, messages: [{ role: "user", parts: [{ type: "text", text: "attack" }] }] })).rejects.toMatchObject({ code: "gateway.blocked" }); expect(a.t.requests).toHaveLength(0);
    const flaky = mkMw("flaky", true, () => { throw new Error("boom"); }); const r = await gw([oa("x")], [flaky]).g.call(REQ); expect(r.provider_meta.hooks.some((h: any) => h.hook === "flaky" && !h.ok)).toBe(true);
    const broken = mkMw("broken-safety", false, () => { throw new Error("boom"); }); const b = gw([oa("x")], [broken]); await expect(b.g.call(REQ)).rejects.toMatchObject({ code: "gateway.hook_failed" }); expect(b.t.requests).toHaveLength(0);
  });
  it("a missing secret is a structured error", async () => { await expect(gw([oa("x")], [], new DictSecretProvider({})).g.call(REQ)).rejects.toBeInstanceOf(GatewayError); });
});
describe("credentials", () => {
  it("secrets never print", () => { expect(String(new Secret("sk-abc"))).not.toContain("sk-abc"); expect(JSON.stringify({ s: new Secret("sk-abc") })).not.toContain("sk-abc"); });
  it("redactor handles known values and secret shapes", () => {
    const r = new Redactor(["hunter2-secret-value"]), out = r.redact("pw=hunter2-secret-value key=sk-ABCDEFGHIJKLMNOP12345 Authorization: Bearer abcdefghijklmnopqrstu aws AKIAABCDEFGHIJKLMNOP");
    for (const s of ["hunter2", "sk-ABC", "abcdefghijklmnopqrstu", "AKIA"]) expect(out).not.toContain(s); expect(r.redactObj({ a: ["x sk-ABCDEFGHIJKLMNOP12345"] })).toEqual({ a: ["x [REDACTED]"] });
  });
  it("token source refreshes at half-life and keeps leased tokens until released", () => {
    const clock = new FakeClock(); let n = 0; const ts = new TokenSource(() => [`tok${++n}`, 100], clock);
    const l1 = ts.lease(); expect(l1[0]).toBe("tok1"); clock.advance(49_000); const l2 = ts.lease(); expect(l2[0]).toBe("tok1"); clock.advance(2_000); expect(ts.lease()[0]).toBe("tok2");
    expect(ts.revocable()).toEqual([]); ts.release(l1); expect(ts.revocable()).toEqual([]); ts.release(l2); expect(ts.revocable()).toEqual(["tok1"]); expect(ts.revocable()).toEqual([]);
  });
  it("refresh failure keeps a valid token, then fails fast after expiry", () => {
    const clock = new FakeClock(); let calls = 0; const ts = new TokenSource(() => { if (++calls > 1) throw new Error("idp down"); return ["tok", 100]; }, clock); ts.lease();
    clock.advance(60_000); expect(ts.lease()[0]).toBe("tok"); clock.advance(50_000); expect(() => ts.lease()).toThrow(CredentialUnavailable);
  });
});
