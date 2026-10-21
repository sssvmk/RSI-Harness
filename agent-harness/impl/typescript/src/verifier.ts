/** Verifiers produce the evidence required for COMPLETED (§6.3). */
export interface Verification { passed: boolean; detail: string; evidence: any }
export interface Verifier { verify(run: any, sandbox: any): Promise<Verification>; }
export class CommandVerifier implements Verifier {
  constructor(public argv: string[], public cwd = "/workspace", public timeoutS = 300, public user = "judge") {}
  async verify(_run: any, sandbox: any): Promise<Verification> {
    const r = await sandbox.executor.execRun(this.argv, { cwd: this.cwd, timeoutS: this.timeoutS, user: this.user });
    return { passed: r.exit_code === 0 && !r.timed_out, detail: `exit ${r.exit_code}`, evidence: { argv: this.argv, exit_code: r.exit_code, stdout: r.stdout.slice(-2000), stderr: r.stderr.slice(-2000) } };
  }
}
export class CallableVerifier implements Verifier { constructor(public fn: (run: any, sandbox: any) => Promise<Verification>) {} verify(run: any, sandbox: any) { return this.fn(run, sandbox); } }
