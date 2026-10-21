import { canonicalize, sha256Hex } from "./util.js";
export const callSignature = (tool: string, args: unknown, digest: string) => sha256Hex(tool + canonicalize(args) + digest);
export const turnSignature = (sigs: string[]): string | null => (sigs.length ? sha256Hex(sigs.join("")) : null);
export function stuckAction(loop: { repeat_count: number; stuck_warned: boolean }, threshold = 4, extra = 2): "guidance" | "terminate" | null {
  const n = loop.repeat_count;
  if (n >= threshold + extra && loop.stuck_warned) return "terminate";
  if (n >= threshold && !loop.stuck_warned) return "guidance";
  return null;
}
