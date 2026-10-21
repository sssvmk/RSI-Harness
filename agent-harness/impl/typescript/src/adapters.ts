/** Provider adapters (design §8.2). Mirrors the Python adapters; both are checked by spec/vectors/adapter-vectors.json. */
import { ulid } from "./util.js";
const GEMINI_KEYS = new Set(["type", "format", "description", "nullable", "enum", "properties", "required", "items", "minItems", "maxItems", "minimum", "maximum"]);
export const wireName = (n: string) => n.replaceAll(".", "-");
export const canonName = (n: string) => n.replaceAll("-", ".");
const textOf = (parts: any[]) => parts.filter((p) => p.type === "text").map((p) => p.text).join("\n");
const callsOf = (m: any) => m.parts.filter((p: any) => p.type === "tool_call");

export function downgradeSchema(schema: any, path = "$", log: any[] = []): [any, any[]] {
  if (Array.isArray(schema)) return [schema.map((s) => downgradeSchema(s, path, log)[0]), log];
  if (schema === null || typeof schema !== "object") return [schema, log];
  const out: any = {};
  for (const [k, v] of Object.entries<any>(schema)) {
    if (!GEMINI_KEYS.has(k)) { log.push({ path, keyword: k }); continue; }
    if (k === "properties") out[k] = Object.fromEntries(Object.entries<any>(v).map(([pk, pv]) => [pk, downgradeSchema(pv, `${path}.properties.${pk}`, log)[0]]));
    else if (k === "items") out[k] = downgradeSchema(v, `${path}.items`, log)[0];
    else if (k === "type" && Array.isArray(v)) { const nn = v.filter((t) => t !== "null"); out.type = nn.length ? nn[0] : "string"; if (v.includes("null")) out.nullable = true; }
    else out[k] = v;
  }
  return [out, log];
}
export abstract class Adapter {
  provider = ""; lastDowngrades: any[] = [];
  constructor(public newId: () => string = () => ulid()) {}
  protected handle(map: Record<string, any>, cid: string) { return map[cid] ?? cid; }
  abstract toWire(req: any, modelId: string, idMap?: Record<string, any>): any;
  abstract fromWire(wire: any): any;
}
export class OpenAIAdapter extends Adapter {
  provider = "openai";
  toWire(req: any, modelId: string, idMap: any = {}) {
    const msgs: any[] = [];
    for (const m of req.messages) {
      if (m.role === "system" || m.role === "user") msgs.push({ role: m.role, content: textOf(m.parts) });
      else if (m.role === "assistant") {
        const calls = callsOf(m), w: any = { role: "assistant", content: textOf(m.parts) || null };
        if (calls.length) w.tool_calls = calls.map((c: any) => ({ id: this.handle(idMap, c.id), type: "function", function: { name: wireName(c.name), arguments: JSON.stringify(c.arguments) } }));
        msgs.push(w);
      } else if (m.role === "tool") for (const p of m.parts) if (p.type === "tool_result") msgs.push({ role: "tool", tool_call_id: this.handle(idMap, p.call_id), content: textOf(p.parts) });
    }
    const out: any = { model: modelId, messages: msgs, max_completion_tokens: req.params.max_output_tokens };
    if (req.tools?.length) out.tools = req.tools.map((t: any) => ({ type: "function", function: { name: wireName(t.name), description: t.description, parameters: t.input_schema } }));
    if ("temperature" in req.params) out.temperature = req.params.temperature;
    if (req.params.stop?.length) out.stop = req.params.stop;
    return out;
  }
  fromWire(wire: any) {
    const ch = wire.choices[0], m = ch.message, parts: any[] = [], idMap: any = {};
    if (m.content) parts.push({ type: "text", text: m.content });
    for (const tc of m.tool_calls ?? []) { const cid = this.newId(); idMap[cid] = tc.id; parts.push({ type: "tool_call", id: cid, name: canonName(tc.function.name), arguments: JSON.parse(tc.function.arguments || "{}") }); }
    let fr = ({ stop: "stop", tool_calls: "tool_calls", length: "length", content_filter: "content_filter" } as any)[ch.finish_reason] ?? "error";
    if (parts.some((p) => p.type === "tool_call") && fr === "stop") fr = "tool_calls";
    const u = wire.usage ?? {}, usage: any = { input_tokens: u.prompt_tokens ?? 0, output_tokens: u.completion_tokens ?? 0 };
    const cached = u.prompt_tokens_details?.cached_tokens; if (cached !== undefined && cached !== null) usage.cached_input_tokens = cached;
    return { message: { role: "assistant", parts }, finish_reason: fr, usage, provider_meta: { id_map: idMap } };
  }
}
export class AnthropicAdapter extends Adapter {
  provider = "anthropic";
  toWire(req: any, modelId: string, idMap: any = {}) {
    const system: string[] = [], msgs: any[] = [];
    const push = (role: string, blocks: any[]) => { if (msgs.length && msgs[msgs.length - 1].role === role) msgs[msgs.length - 1].content.push(...blocks); else msgs.push({ role, content: [...blocks] }); };
    for (const m of req.messages) {
      if (m.role === "system") system.push(textOf(m.parts));
      else if (m.role === "user") push("user", [{ type: "text", text: textOf(m.parts) }]);
      else if (m.role === "assistant") {
        const blocks: any[] = m.parts.filter((p: any) => p.type === "text" && p.text).map((p: any) => ({ type: "text", text: p.text }));
        blocks.push(...callsOf(m).map((c: any) => ({ type: "tool_use", id: this.handle(idMap, c.id), name: wireName(c.name), input: c.arguments })));
        push("assistant", blocks);
      } else if (m.role === "tool") {
        const blocks = m.parts.filter((p: any) => p.type === "tool_result").map((p: any) => ({ type: "tool_result", tool_use_id: this.handle(idMap, p.call_id), content: textOf(p.parts), ...(p.is_error ? { is_error: true } : {}) }));
        if (msgs.length && msgs[msgs.length - 1].role === "user") msgs[msgs.length - 1].content.unshift(...blocks); else push("user", blocks);
      }
    }
    const out: any = { model: modelId, max_tokens: req.params.max_output_tokens, messages: msgs };
    if (system.length) out.system = system.join("\n\n");
    if (req.tools?.length) out.tools = req.tools.map((t: any) => ({ name: wireName(t.name), description: t.description, input_schema: t.input_schema }));
    if ("temperature" in req.params) out.temperature = req.params.temperature;
    if (req.params.stop?.length) out.stop_sequences = req.params.stop;
    return out;
  }
  fromWire(wire: any) {
    const parts: any[] = [], idMap: any = {};
    for (const b of wire.content) {
      if (b.type === "text") parts.push({ type: "text", text: b.text });
      else if (b.type === "tool_use") { const cid = this.newId(); idMap[cid] = b.id; parts.push({ type: "tool_call", id: cid, name: canonName(b.name), arguments: b.input }); }
    }
    const fr = ({ end_turn: "stop", stop_sequence: "stop", tool_use: "tool_calls", max_tokens: "length", refusal: "content_filter" } as any)[wire.stop_reason] ?? "error";
    const u = wire.usage ?? {}, usage: any = { input_tokens: u.input_tokens ?? 0, output_tokens: u.output_tokens ?? 0 };
    if ("cache_read_input_tokens" in u) usage.cached_input_tokens = u.cache_read_input_tokens;
    return { message: { role: "assistant", parts }, finish_reason: fr, usage, provider_meta: { id_map: idMap } };
  }
}
export class GeminiAdapter extends Adapter {
  provider = "gemini";
  toWire(req: any, modelId: string) {
    const names: Record<string, string> = {}, order: Record<string, number> = {}, system: string[] = [], contents: any[] = [], downgrades: any[] = [];
    for (const m of req.messages) {
      if (m.role === "system") system.push(textOf(m.parts));
      else if (m.role === "user") contents.push({ role: "user", parts: [{ text: textOf(m.parts) }] });
      else if (m.role === "assistant") {
        const parts: any[] = m.parts.filter((p: any) => p.type === "text" && p.text).map((p: any) => ({ text: p.text }));
        callsOf(m).forEach((c: any, i: number) => { names[c.id] = c.name; order[c.id] = i; parts.push({ functionCall: { name: c.name, args: c.arguments } }); });
        contents.push({ role: "model", parts });
      } else if (m.role === "tool") {
        const rs = m.parts.filter((p: any) => p.type === "tool_result").sort((a: any, b: any) => (order[a.call_id] ?? 0) - (order[b.call_id] ?? 0));
        const parts = rs.map((p: any) => ({ functionResponse: { name: names[p.call_id] ?? "unknown", response: p.is_error ? { error: textOf(p.parts) } : { result: textOf(p.parts) } } }));
        const last = contents[contents.length - 1];
        if (last && last.role === "user" && last.parts[0] && "functionResponse" in last.parts[0]) last.parts.push(...parts); else contents.push({ role: "user", parts });
      }
    }
    const gen: any = { maxOutputTokens: req.params.max_output_tokens };
    if ("temperature" in req.params) gen.temperature = req.params.temperature;
    if (req.params.stop?.length) gen.stopSequences = req.params.stop;
    const out: any = { contents, generationConfig: gen };
    if (system.length) out.systemInstruction = { parts: [{ text: system.join("\n\n") }] };
    if (req.tools?.length) {
      out.tools = [{ functionDeclarations: req.tools.map((t: any) => { const [params, log] = downgradeSchema(t.input_schema, `tool:${t.name}`); downgrades.push(...log); return { name: t.name, description: t.description, parameters: params }; }) }];
    }
    this.lastDowngrades = downgrades;
    return out;
  }
  fromWire(wire: any) {
    const cand = wire.candidates[0], parts: any[] = [], idMap: any = {}; let n = 0;
    for (const p of cand.content?.parts ?? []) {
      if ("text" in p) parts.push({ type: "text", text: p.text });
      else if ("functionCall" in p) { const cid = this.newId(); idMap[cid] = { name: p.functionCall.name, index: n++ }; parts.push({ type: "tool_call", id: cid, name: p.functionCall.name, arguments: p.functionCall.args ?? {} }); }
    }
    let fr = ({ STOP: "stop", MAX_TOKENS: "length", SAFETY: "content_filter", RECITATION: "content_filter", BLOCKLIST: "content_filter", PROHIBITED_CONTENT: "content_filter", SPII: "content_filter" } as any)[cand.finishReason ?? "STOP"] ?? "error";
    if (n && fr === "stop") fr = "tool_calls";
    const u = wire.usageMetadata ?? {}, usage: any = { input_tokens: u.promptTokenCount ?? 0, output_tokens: (u.candidatesTokenCount ?? 0) + (u.thoughtsTokenCount ?? 0) };
    if ("cachedContentTokenCount" in u) usage.cached_input_tokens = u.cachedContentTokenCount;
    return { message: { role: "assistant", parts }, finish_reason: fr, usage, provider_meta: { id_map: idMap } };
  }
}
export const ADAPTERS: Record<string, new (f?: () => string) => Adapter> = { openai: OpenAIAdapter, anthropic: AnthropicAdapter, gemini: GeminiAdapter };
