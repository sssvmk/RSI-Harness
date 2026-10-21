/** Model gateway: retries (§8.3), middleware (§8.4), transports. */
import { Clock, SystemClock } from "./util.js";
import { ADAPTERS } from "./adapters.js";
import { KeyError, SecretProvider } from "./credentials.js";
const TRANSIENT = new Set([408, 429, 500, 502, 503, 504]);
export class GatewayError extends Error { constructor(public code: string, message: string, public retryable = false, public status?: number) { super(message); } }
export class Block extends Error {}
export class ConnectionResetError extends Error {}
export interface Transport { post(url: string, headers: Record<string, string>, body: any, timeoutS: number): Promise<[number, any]>; }
export class HttpTransport implements Transport {
  async post(url: string, headers: any, body: any, timeoutS: number): Promise<[number, any]> {
    try {
      const r = await fetch(url, { method: "POST", headers, body: JSON.stringify(body), signal: AbortSignal.timeout(timeoutS * 1000) });
      const text = await r.text();
      try { return [r.status, JSON.parse(text)]; } catch { return [r.status, { error: text.slice(0, 500) }]; }
    } catch (e: any) { if (e?.name === "TimeoutError") throw e; throw new ConnectionResetError(String(e)); }
  }
}
/** Plays back [status, body] tuples or Errors, recording every request. */
export class ScriptedTransport implements Transport {
  requests: any[] = []; script: any[];
  constructor(script: any[]) { this.script = [...script]; }   // never mutate the caller's array
  async post(url: string, headers: any, body: any): Promise<[number, any]> {
    this.requests.push({ url, headers, body });
    if (!this.script.length) throw new Error("scripted transport exhausted");
    const item = this.script.shift();
    if (item instanceof Error) throw item;
    return item;
  }
}
export class Middleware { name = "middleware"; optional = true; async preRequest(r: any): Promise<any> { return r; } async postResponse(r: any): Promise<any> { return r; } }
export function validateRequest(req: any) {
  for (const k of ["model_ref", "messages", "params", "metadata"]) if (!(k in req)) throw new Error(`request missing ${k}`);
  if (!("max_output_tokens" in req.params)) throw new Error("params.max_output_tokens is required");
  if (!("run_id" in req.metadata)) throw new Error("metadata.run_id is required");
}
export class ModelGateway {
  constructor(public models: Record<string, any>, public transport: Transport, public clock: Clock = new SystemClock(), public secrets?: SecretProvider,
              public middleware: Middleware[] = [], public o: { maxAttempts?: number; timeoutS?: number; rng?: () => number; idFactory?: () => string } = {}) {}
  private headers(m: any): Record<string, string> {
    let key = "";
    try { key = this.secrets && m.secret ? this.secrets.get(m.secret).reveal() : ""; } catch (e) { if (e instanceof KeyError) throw new GatewayError("gateway.credential_unavailable", `secret ${m.secret} unavailable`); throw e; }
    if (m.provider === "anthropic") return { "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json" };
    if (m.provider === "gemini") return { "x-goog-api-key": key, "content-type": "application/json" };
    return { authorization: `Bearer ${key}`, "content-type": "application/json" };
  }
  private url(m: any) {
    if (m.endpoint) return m.endpoint;
    return ({ openai: "https://api.openai.com/v1/chat/completions", anthropic: "https://api.anthropic.com/v1/messages", gemini: `https://generativelanguage.googleapis.com/v1beta/models/${m.model_id}:generateContent` } as any)[m.provider];
  }
  static backoffMs(attempt: number, rng: () => number, base = 1000, cap = 30000) { return rng() * Math.min(cap, base * 2 ** (attempt - 1)); }   // full jitter
  async call(req: any, idMap?: any, onFailure?: (i: any) => Promise<void>, remainingTimeMs?: number | null): Promise<any> {
    validateRequest(req);
    const m = this.models[req.model_ref], trace: any[] = [], maxAttempts = this.o.maxAttempts ?? 4, rng = this.o.rng ?? Math.random;
    for (const h of this.middleware) {
      try { req = await h.preRequest(req); trace.push({ hook: h.name, phase: "pre", ok: true }); }
      catch (e: any) {
        if (e instanceof Block) throw new GatewayError("gateway.blocked", `${h.name}: ${e.message}`);
        if (!h.optional) throw new GatewayError("gateway.hook_failed", `${h.name}: ${e.message}`);   // safety hooks fail closed
        trace.push({ hook: h.name, phase: "pre", ok: false, error: String(e.message) });
      }
    }
    const adapter = new ADAPTERS[m.provider](this.o.idFactory);
    const wire = adapter.toWire(req, m.model_id, idMap);
    const base = this.o.timeoutS ?? 120, timeout = remainingTimeMs == null ? base : Math.max(0.001, Math.min(base, remainingTimeMs / 1000));
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      const t0 = this.clock.monoMs();
      let err: GatewayError;
      try {
        const [status, body] = await this.transport.post(this.url(m), this.headers(m), wire, timeout);
        if (status === 200) {
          let resp = adapter.fromWire(body);
          resp.latency_ms = Math.trunc(this.clock.monoMs() - t0); resp.attempt = attempt; resp.provider_meta.hooks = trace;
          if (adapter.lastDowngrades?.length) resp.provider_meta.schema_downgrades = adapter.lastDowngrades;
          for (const h of this.middleware) {
            try { resp = await h.postResponse(resp); }
            catch (e: any) { if (e instanceof Block) throw new GatewayError("gateway.blocked", `${h.name}: ${e.message}`); if (!h.optional) throw new GatewayError("gateway.hook_failed", `${h.name}: ${e.message}`); }
          }
          return resp;
        }
        err = new GatewayError(`gateway.http_${status}`, JSON.stringify(body).slice(0, 300), TRANSIENT.has(status), status);
      } catch (e: any) {
        if (e instanceof GatewayError) throw e;
        if (e instanceof ConnectionResetError || e?.name === "TimeoutError") err = new GatewayError("gateway.connection", String(e), true);
        else throw e;
      }
      if (!err.retryable || attempt === maxAttempts) { await onFailure?.({ error: { code: err.code, message: err.message }, attempt, retry_in_ms: null }); throw err; }
      const delay = ModelGateway.backoffMs(attempt, rng);
      await onFailure?.({ error: { code: err.code, message: err.message }, attempt, retry_in_ms: Math.trunc(delay) });
      await this.clock.sleep(delay);
    }
    throw new Error("unreachable");
  }
}
