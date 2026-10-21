import { canonicalize } from "./util.js";
export const IMAGE_TOKENS = 1500;
export const estimateText = (t: string, d = 3) => Math.ceil(Buffer.byteLength(t, "utf8") / d);
export const estimateJson = (o: unknown, d = 3) => Math.ceil(Buffer.byteLength(canonicalize(o), "utf8") / d);
export function estimatePart(p: any, d = 3): number {
  switch (p.type) {
    case "text": return estimateText(p.text, d);
    case "image": return IMAGE_TOKENS;
    case "tool_call": return estimateText(p.name, d) + estimateJson(p.arguments, d);
    case "tool_result": return p.parts.reduce((a: number, q: any) => a + estimatePart(q, d), 0);
    default: throw new Error(`unknown part type ${p.type}`);
  }
}
export const estimateMessage = (m: any, d = 3) => m.parts.reduce((a: number, p: any) => a + estimatePart(p, d), 0);
export function estimateRequest(messages: any[], tools?: any[] | null, d = 3): number {
  let n = messages.reduce((a, m) => a + estimateMessage(m, d), 0);
  if (tools) n += tools.reduce((a, t) => a + estimateJson(t, d), 0);
  return n;
}
