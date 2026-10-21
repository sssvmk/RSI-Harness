"""Closure ledger: tracks every PRD requirement through three states.

  open  the design does not yet close the requirement (it is missing, deviates from the PRD, rests on an undecided question, or lags the code).
  soft  CLOSED IN DESIGN, waiting for implementation in code. The design fully specifies the requirement; the code is partial, absent, or needs updating.
  hard  IMPLEMENTED as the PRD and the design say, and closed for good.

A hard close is bound to the exact text of the requirement and of the design sections it cites (SHA-256), and to the evidence recorded when it was closed.
It is permanent in this sense: nothing changes it silently. If the requirement or its design changes afterwards, `check` reports the close as STALE, and the only way back
to soft or open is an explicit `reopen` with an actor and a reason, which is appended to the entry's history and never removed. History is append-only.

This module is project governance and has no runtime role, so it exists in Python only."""
from __future__ import annotations
import argparse, datetime, hashlib, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
STATES = ("open", "soft", "hard")
CODE_STATUS = ("implemented", "partial", "not_implemented", "needs_update")
VERIFICATION = ("graded", "verified")
REQ_RE = re.compile(r"^\s*(?:\* |- )?\*\*(.+?)\*\*\s*\[([^\]]+)\]:?\s*(.*)$")
HEAD_RE = re.compile(r"^(#{1,6}) (.*)$")
SEC_RE = re.compile(r"^#{2,5} (\d+(?:\.\d+)*)\.? ")
PROPOSED_RE = re.compile(r"proposed (P\d+)")


class ClosureError(ValueError):
    pass


def slug(t: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", t.lower())).strip("-")


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def sha(t: str) -> str:
    return hashlib.sha256(norm(t).encode("utf-8")).hexdigest()


def today() -> str:
    return datetime.date.today().isoformat()


# --------------------------------------------------------------------------------------------------------------------------------- reading the documents
def parse_prd(text: str) -> dict[str, dict]:
    """Every tagged requirement, in both bullet styles (`**Title** [Profile]:` and `**Title.** [Profile]`), nested bullets included, keyed `H-<section>-<slug>`."""
    lines = text.split("\n")
    stop = next((i for i, l in enumerate(lines) if l.startswith("## Appendix") or l.startswith("# Appendix")), len(lines))
    out: dict[str, dict] = {}
    sec, cur = None, None
    for l in lines[:stop]:
        m = SEC_RE.match(l)
        if m: sec, cur = m.group(1), None; continue
        if HEAD_RE.match(l): cur = None; continue
        m = REQ_RE.match(l)
        if m and sec:
            title = m.group(1).rstrip(". ").strip(); rid = f"H-{sec}-{slug(title)}"
            if rid in out: raise ClosureError(f"duplicate requirement id {rid}")
            out[rid] = {"id": rid, "section": sec, "title": title, "profile": m.group(2).strip(), "text": m.group(3)}; cur = rid; continue
        if cur and l.strip() and not l.lstrip().startswith("|"): out[cur]["text"] += " " + l.strip()
    return out


def parse_matrix(design: str) -> dict[str, dict]:
    """Appendix B: each requirement's design sections."""
    out = {}
    for m in re.finditer(r"^\| `(H-[^`]+)` \|(.*)\|\s*$", design, re.M):
        cols = [c.strip() for c in m.group(2).split("|")]
        if len(cols) >= 5: out[m.group(1)] = {"title": cols[0], "profile": cols[1], "train": cols[2], "components": cols[3], "refs": re.findall(r"§(\d+(?:\.\d+)*)", cols[4])}
    return out


def section_text(design: str, num: str) -> tuple[str | None, bool]:
    """The text of design section `num`, with its sub-sections. A chapter-level reference (a single number such as `§9`) is *coarse*: only the chapter's own introduction is taken,
    so that adding a sub-section elsewhere in the chapter does not disturb every requirement that cites the chapter."""
    lines = design.split("\n")
    pat = re.compile(r"^(#{1,6}) " + re.escape(num) + r"(?:\.\s|\s)")
    for i, l in enumerate(lines):
        m = pat.match(l)
        if not m: continue
        level, coarse, j = len(m.group(1)), "." not in num, i + 1
        while j < len(lines):
            h = HEAD_RE.match(lines[j])
            if h and (coarse or len(h.group(1)) <= level): break
            j += 1
        return "\n".join(lines[i:j]), coarse
    return None, "." not in num


def hashes(rid: str, prd: dict, matrix: dict, design: str) -> dict:
    req = prd[rid]; refs = matrix.get(rid, {}).get("refs", [])
    parts, unresolved, coarse = [], [], []
    for r in sorted(set(refs)):
        t, c = section_text(design, r)
        if t is None: unresolved.append("§" + r); continue
        parts.append(f"§{r}\n{t}"); coarse += ["§" + r] if c else []
    pend = sorted(set(PROPOSED_RE.findall("\n".join(parts))), key=lambda x: int(x[1:]))
    return {"req": sha(f"{req['title']} [{req['profile']}] {req['text']}"), "design": sha("\n".join(parts)), "unresolved": unresolved, "coarse": coarse, "pending": pend}


# --------------------------------------------------------------------------------------------------------------------------------- the ledger
def load(path: Path | str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(ledger: dict, path: Path | str) -> None:
    Path(path).write_text(json.dumps(ledger, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def entry(ledger: dict, rid: str) -> dict:
    for e in ledger["entries"]:
        if e["id"] == rid: return e
    raise ClosureError(f"no ledger entry for {rid}")


def check(ledger: dict, prd_text: str, design_text: str) -> dict:
    """Validate the ledger against the PRD and the design. Returns {errors, stale, drift, pending}: errors are structural and always fatal; `stale` lists hard closes whose
    requirement or design changed since they were closed; `drift` lists soft closes whose design changed since."""
    prd, matrix = parse_prd(prd_text), parse_matrix(design_text)
    errors: list[str] = []; stale: list[str] = []; drift: list[str] = []; pending: dict[str, list[str]] = {}
    ids = [e["id"] for e in ledger["entries"]]
    for d in {i for i in ids if ids.count(i) > 1}: errors.append(f"{d}: duplicate ledger entry")
    for rid in prd:
        if rid not in set(ids): errors.append(f"{rid}: requirement has no ledger entry")
        if rid not in matrix: errors.append(f"{rid}: requirement has no row in the design's traceability matrix")
    for i in ids:
        if i not in prd: errors.append(f"{i}: ledger entry has no requirement in the PRD")
    for e in ledger["entries"]:
        rid = e["id"]
        if rid not in prd: continue
        h = hashes(rid, prd, matrix, design_text)
        if h["unresolved"]: errors.append(f"{rid}: design reference(s) {h['unresolved']} do not resolve to a section")
        if h["pending"]: pending[rid] = h["pending"]
        s = e.get("state")
        if s not in STATES: errors.append(f"{rid}: state {s!r} is not one of {STATES}"); continue
        if e.get("code_status") not in CODE_STATUS: errors.append(f"{rid}: code_status {e.get('code_status')!r} is not one of {CODE_STATUS}")
        hist = e.get("history") or []
        if not hist: errors.append(f"{rid}: no history")
        else:
            if hist[-1]["to"] != s: errors.append(f"{rid}: state {s} does not match the last history entry ({hist[-1]['to']})")
            for a, b in zip(hist, hist[1:]):
                if b["from"] != a["to"]: errors.append(f"{rid}: history is not continuous ({a['to']} then from {b['from']})")
        if s == "open" and not (e.get("open") or {}).get("reason"): errors.append(f"{rid}: an open entry must say why it is open")
        if s != "open" and e.get("open"): errors.append(f"{rid}: only an open entry carries an open reason")
        if s == "hard":
            if e.get("code_status") != "implemented": errors.append(f"{rid}: a hard close needs code_status implemented")
            if not e.get("evidence"): errors.append(f"{rid}: a hard close needs evidence")
            if e.get("verification") not in VERIFICATION: errors.append(f"{rid}: a hard close needs verification in {VERIFICATION}")
        if s in ("soft", "hard"):
            rec = e.get("closed_hashes") or {}
            if not rec: errors.append(f"{rid}: {s} close has no recorded hashes")
            elif s == "hard" and (rec.get("req") != h["req"] or rec.get("design") != h["design"]): stale.append(rid)
            elif s == "soft" and rec.get("design") != h["design"]: drift.append(rid)
    return {"errors": errors, "stale": sorted(stale), "drift": sorted(drift), "pending": pending}


# --------------------------------------------------------------------------------------------------------------------------------- transitions
def _need(actor: str, note: str) -> None:
    """Checked first in every transition, so that a refused transition changes nothing."""
    if not actor or not note: raise ClosureError("a transition needs an actor and a note")


def _log(e: dict, to: str, actor: str, note: str, kind: str) -> None:
    _need(actor, note)
    e.setdefault("history", []).append({"at": today(), "from": e["state"], "to": to, "kind": kind, "by": actor, "note": note}); e["state"] = to


def _stamp(e: dict, prd_text: str, design_text: str) -> None:
    prd, matrix = parse_prd(prd_text), parse_matrix(design_text); h = hashes(e["id"], prd, matrix, design_text)
    if h["unresolved"]: raise ClosureError(f"{e['id']}: cannot close, design reference(s) {h['unresolved']} do not resolve")
    e["closed_hashes"] = {"req": h["req"], "design": h["design"]}


def soft_close(ledger: dict, rid: str, actor: str, note: str, prd_text: str, design_text: str, code_status: str = "not_implemented") -> dict:
    """The design now closes the requirement; the code has not caught up (or needs updating)."""
    _need(actor, note); e = entry(ledger, rid)
    if e["state"] == "hard": raise ClosureError(f"{rid} is hard closed; use reopen")
    if code_status == "implemented": raise ClosureError("a soft close means implementation is outstanding; close it hard instead")
    if code_status not in CODE_STATUS: raise ClosureError(f"unknown code_status {code_status!r}")
    _stamp(e, prd_text, design_text); e["open"] = None; e["code_status"] = code_status; _log(e, "soft", actor, note, "close"); return e


def hard_close(ledger: dict, rid: str, actor: str, note: str, evidence: list[dict], prd_text: str, design_text: str, verification: str = "verified") -> dict:
    """The requirement is implemented as the PRD and the design say. Needs evidence, and is bound to the current text of both documents."""
    _need(actor, note); e = entry(ledger, rid)
    if e["state"] != "soft": raise ClosureError(f"{rid} must be soft closed (closed in design) before it can be hard closed; it is {e['state']}")
    if not evidence: raise ClosureError("a hard close needs evidence")
    if verification not in VERIFICATION: raise ClosureError(f"verification must be one of {VERIFICATION}")
    _stamp(e, prd_text, design_text); e["code_status"] = "implemented"; e["evidence"] = evidence; e["verification"] = verification; _log(e, "hard", actor, note, "close"); return e


def reattest(ledger: dict, rid: str, actor: str, note: str, prd_text: str, design_text: str) -> dict:
    """A reviewer has read what changed in the requirement or its design and confirms the implementation still conforms. The close stays hard, the new hashes are recorded,
    and the confirmation is added to the history. Without this, an unrelated edit to a cited design section would force a reopen."""
    _need(actor, note); e = entry(ledger, rid)
    if e["state"] != "hard": raise ClosureError(f"{rid} is not hard closed")
    prd, matrix = parse_prd(prd_text), parse_matrix(design_text); h = hashes(rid, prd, matrix, design_text)
    if (e.get("closed_hashes") or {}) == {"req": h["req"], "design": h["design"]}: raise ClosureError(f"{rid} is not stale; there is nothing to re-attest")
    _stamp(e, prd_text, design_text); _log(e, "hard", actor, note, "reattest"); return e


def add_requirement(ledger: dict, rid: str, actor: str, prd_text: str) -> dict:
    """A requirement added to the PRD. It enters as open, because the design has not been reviewed against it; soft_close it once the design closes it."""
    _need(actor, "new requirement"); p = parse_prd(prd_text)
    if rid not in p: raise ClosureError(f"{rid} is not in the PRD")
    if any(x["id"] == rid for x in ledger["entries"]): raise ClosureError(f"{rid} is already in the ledger")
    e = {"id": rid, "title": p[rid]["title"], "section": p[rid]["section"], "profile": p[rid]["profile"], "state": "open", "code_status": "not_implemented", "code_note": "", "open": {"reason": "a new requirement that the design has not yet been reviewed against"},
         "evidence": [], "verification": None, "basis": None, "closed_hashes": None, "history": []}
    _log(e, "open", actor, "added to the PRD", "add"); ledger["entries"].append(e); return e


def update_code(ledger: dict, rid: str, actor: str, status: str, note: str, plugin: str | None = None) -> dict:
    """Record progress on a requirement that is not yet hard closed (what the code has now). The state does not change; the update is logged in the history."""
    _need(actor, note); e = entry(ledger, rid)
    if e["state"] == "hard": raise ClosureError(f"{rid} is hard closed; its code status is fixed (use reopen)")
    if status not in CODE_STATUS or status == "implemented": raise ClosureError("record partial, not_implemented, or needs_update; close it hard when it is implemented")
    e["code_status"], e["code_note"] = status, note
    if plugin is not None: e["plugin"] = plugin                    # the kind of plugin that supplies this capability (design §18.11)
    e.setdefault("history", []).append({"at": today(), "from": e["state"], "to": e["state"], "kind": "code", "by": actor, "note": note}); return e


def open_item(ledger: dict, rid: str, actor: str, reason: str) -> dict:
    """A problem in the design is found for a soft-closed requirement."""
    _need(actor, reason); e = entry(ledger, rid)
    if e["state"] == "hard": raise ClosureError(f"{rid} is hard closed; use reopen")
    if not reason: raise ClosureError("an open item needs a reason")
    e["open"] = {"reason": reason}; _log(e, "open", actor, reason, "open"); return e


def reopen(ledger: dict, rid: str, actor: str, reason: str, to: str = "soft") -> dict:
    """The only way back from a hard close. It is explicit, it needs a reason, and it is recorded in the history for good."""
    _need(actor, reason); e = entry(ledger, rid)
    if e["state"] != "hard": raise ClosureError(f"{rid} is not hard closed")
    if to not in ("soft", "open"): raise ClosureError("a hard close reopens to soft or open")
    e["code_status"] = "needs_update"; e["evidence"] = e.get("evidence", []); e["verification"] = None
    if to == "open": e["open"] = {"reason": reason}
    _log(e, to, actor, reason, "reopen"); return e


# --------------------------------------------------------------------------------------------------------------------------------- reporting
def counts(ledger: dict) -> dict:
    c = {s: 0 for s in STATES}
    for e in ledger["entries"]: c[e["state"]] += 1
    return c


def report_markdown(ledger: dict, prd_text: str, design_text: str) -> str:
    res = check(ledger, prd_text, design_text); E = ledger["entries"]; c = counts(ledger)
    prof = lambda p: p.split(",")[0].strip()
    out = ["# Closure report: open, soft closed, hard closed", "",
           f"*Generated from `closure/ledger.json` on {ledger['as_of']}; do not edit by hand. Change states with `python -m harness.closure` (see README §4.16).*", "",
           "| State | Meaning |", "|---|---|",
           "| **Open** | The design does not close the requirement yet: it is missing, deviates from the PRD, rests on an undecided question, or lags behind the code |",
           "| **Soft closed** | **Closed in the design, waiting for implementation in code** |",
           "| **Hard closed** | **Implemented as the PRD and the design say, and closed for good.** A change to the requirement or its design makes the close *stale*; only an explicit, logged `reopen` takes it back |", "",
           f"**{len(E)} requirements:** {c['hard']} hard closed, {c['soft']} soft closed, {c['open']} open.", ""]
    ver = sum(1 for e in E if e["state"] == "hard" and e.get("verification") == "verified")
    out += [f"Of the {c['hard']} hard closes, **{ver} were verified clause by clause and {c['hard'] - ver} are graded**: they rest on the gap review of the code and tests, and have not been re-verified against the current PRD and design text. Treat a graded hard close as provisional until it is verified.", ""]
    out += ["## Summary by profile", "", "| Profile | Open | Soft closed | Hard closed | Total |", "|---|---|---|---|---|"]
    for p in ("Core", "Standard", "High"):
        rows = [e for e in E if prof(e["profile"]) == p]; out.append(f"| {p} | " + " | ".join(str(sum(1 for e in rows if e['state'] == s)) for s in STATES) + f" | {len(rows)} |")
    out += ["", "## Summary by section", "", "| Section | Open | Soft | Hard | Total |", "|---|---|---|---|---|"]
    secs: dict[str, list] = {}
    for e in E: secs.setdefault(e["section"].split(".")[0] if e["section"].split(".")[0] in ("5", "7", "8", "9", "10") else ".".join(e["section"].split(".")[:2]), []).append(e)
    for k in sorted(secs, key=lambda s: [int(x) for x in s.split(".")]): out.append(f"| §{k} | " + " | ".join(str(sum(1 for e in secs[k] if e['state'] == s)) for s in STATES) + f" | {len(secs[k])} |")
    out += ["", "## Health of the ledger", "",
            f"* Structural errors: **{len(res['errors'])}**" + ("" if not res["errors"] else "\n" + "\n".join(f"  * {x}" for x in res["errors"][:20])),
            f"* Hard closes that are **stale** (the requirement or its design changed since they were closed): **{len(res['stale'])}**" + ("" if not res["stale"] else " (" + ", ".join(res["stale"][:10]) + ")"),
            f"* Soft closes whose design changed since they were closed: **{len(res['drift'])}**",
            f"* Soft closes resting on a *proposed* design parameter that nobody has confirmed: **{sum(1 for e in E if e['state'] == 'soft' and e['id'] in res['pending'])}**",
            f"* Soft closes **awaiting a plugin** (the harness defines and enforces the contract; a deployment supplies the capability): **{sum(1 for e in E if e['state'] == 'soft' and e.get('plugin'))}**", ""]
    out += ["## Open: the design does not close these yet", "", "| Requirement | Profile | Why it is open |", "|---|---|---|"]
    for e in sorted((e for e in E if e["state"] == "open"), key=lambda e: (prof(e["profile"]) != "Core", e["id"])): out.append(f"| §{e['section']} {e['title']} | {prof(e['profile'])} | {e['open']['reason'].replace('|', '/')} |")
    out += ["", "## Soft closed: closed in design, waiting for code", "",
            "Ordered Core first. *Code* says how far the code is: **needs update** means it was implemented against an earlier version of the requirement or design; **partial** and **not implemented** are as they say. The last column is the gap review's note on what exists now, so what remains is the difference between it and the requirement and design. A star marks a close that rests on a proposed parameter (listed) not yet confirmed, and ⧉ names the plugin kind that will supply the capability.", "",
            "| Requirement | Profile | Code | What the code has today |", "|---|---|---|---|"]
    label = {"needs_update": "needs update", "partial": "partial", "not_implemented": "not implemented"}
    for e in sorted((e for e in E if e["state"] == "soft"), key=lambda e: (["Core", "Standard", "High"].index(prof(e["profile"])) if prof(e["profile"]) in ("Core", "Standard", "High") else 3, e["code_status"] != "partial", e["id"])):
        star = f" ★ {', '.join(res['pending'][e['id']])}" if e["id"] in res["pending"] else ""
        plug = f" ⧉ {e['plugin']}" if e.get("plugin") else ""
        out.append(f"| §{e['section']} {e['title']}{star}{plug} | {prof(e['profile'])} | {label.get(e['code_status'], e['code_status'])} | {(e.get('code_note') or '').replace('|', '/')[:230]} |")
    out += ["", "## Hard closed: implemented and closed", "", "| Requirement | Profile | Verification | Closed on | Evidence |", "|---|---|---|---|---|"]
    for e in sorted((e for e in E if e["state"] == "hard"), key=lambda e: (["Core", "Standard", "High"].index(prof(e["profile"])) if prof(e["profile"]) in ("Core", "Standard", "High") else 3, e["id"])):
        ev = "; ".join(x["ref"] if isinstance(x, dict) else str(x) for x in e["evidence"])[:200].replace("|", "/")
        out.append(f"| §{e['section']} {e['title']} | {prof(e['profile'])} | {e['verification']} | {e['history'][-1]['at']} | {ev} |")
    reopened = [(e, h) for e in E for h in e["history"] if h["kind"] == "reopen"]
    if reopened:
        out += ["", "## Reopened hard closes (permanent record)", "", "| Requirement | Date | By | Reason |", "|---|---|---|---|"]
        for e, h in reopened: out.append(f"| {e['title']} | {h['at']} | {h['by']} | {h['note'].replace('|', '/')} |")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------------------------------------------------------------- command line
def _paths(a): return Path(a.ledger or ROOT / "closure" / "ledger.json"), Path(a.prd or ROOT / "docs" / "harness_intent.md"), Path(a.design or ROOT / "docs" / "design.md")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m harness.closure", description="Maintain soft and hard closes.")
    p.add_argument("--ledger"); p.add_argument("--prd"); p.add_argument("--design")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="validate the ledger against the PRD and design"); c.add_argument("--fail-on-stale", action="store_true")
    r = sub.add_parser("report", help="write the markdown report"); r.add_argument("--out")
    s = sub.add_parser("show"); s.add_argument("id")
    sc = sub.add_parser("soft", help="close in design"); sc.add_argument("id"); sc.add_argument("--by", required=True); sc.add_argument("--note", required=True); sc.add_argument("--code", default="not_implemented", choices=("partial", "not_implemented", "needs_update"))
    hc = sub.add_parser("hard", help="close as implemented"); hc.add_argument("id"); hc.add_argument("--by", required=True); hc.add_argument("--note", required=True)
    hc.add_argument("--evidence", action="append", required=True, help="kind:ref, e.g. test:impl/python/tests/test_x.py::test_y"); hc.add_argument("--verification", default="verified", choices=VERIFICATION)
    cd = sub.add_parser("code", help="record what the code has now for a requirement that is not hard closed"); cd.add_argument("--plugin"); cd.add_argument("id"); cd.add_argument("--by", required=True); cd.add_argument("--note", required=True); cd.add_argument("--status", default="partial", choices=("partial", "not_implemented", "needs_update"))
    oc = sub.add_parser("open", help="a design problem was found"); oc.add_argument("id"); oc.add_argument("--by", required=True); oc.add_argument("--reason", required=True)
    ra = sub.add_parser("reattest", help="a reviewer confirms a stale hard close still conforms"); ra.add_argument("id"); ra.add_argument("--by", required=True); ra.add_argument("--note", required=True)
    rc = sub.add_parser("reopen", help="take a hard close back (permanent record)"); rc.add_argument("id"); rc.add_argument("--by", required=True); rc.add_argument("--reason", required=True); rc.add_argument("--to", default="soft", choices=("soft", "open"))
    a = p.parse_args(argv); lp, pp, dp = _paths(a); led = load(lp); prd, des = pp.read_text(encoding="utf-8"), dp.read_text(encoding="utf-8")
    try:
        if a.cmd == "check":
            res = check(led, prd, des); c_ = counts(led)
            print(f"{len(led['entries'])} requirements: {c_['hard']} hard, {c_['soft']} soft, {c_['open']} open | errors {len(res['errors'])} | stale hard closes {len(res['stale'])} | soft closes with design drift {len(res['drift'])}")
            for x in res["errors"]: print("  ERROR", x)
            for x in res["stale"]: print("  STALE", x)
            return 1 if res["errors"] or (a.fail_on_stale and res["stale"]) else 0
        if a.cmd == "report":
            text = report_markdown(led, prd, des)
            if a.out: Path(a.out).write_text(text, encoding="utf-8"); print("wrote", a.out)
            else: sys.stdout.write(text)
            return 0
        if a.cmd == "show": print(json.dumps(entry(led, a.id), indent=1, ensure_ascii=False)); return 0
        if a.cmd == "soft": soft_close(led, a.id, a.by, a.note, prd, des, a.code)
        elif a.cmd == "hard": hard_close(led, a.id, a.by, a.note, [{"kind": x.split(":", 1)[0], "ref": x.split(":", 1)[1]} for x in a.evidence], prd, des, a.verification)
        elif a.cmd == "code": update_code(led, a.id, a.by, a.status, a.note, a.plugin)
        elif a.cmd == "reattest": reattest(led, a.id, a.by, a.note, prd, des)
        elif a.cmd == "open": open_item(led, a.id, a.by, a.reason)
        elif a.cmd == "reopen": reopen(led, a.id, a.by, a.reason, a.to)
        led["as_of"] = today(); save(led, lp); print(f"{a.id}: now {entry(led, a.id)['state']}"); return 0
    except ClosureError as e:
        print("error:", e, file=sys.stderr); return 2


if __name__ == "__main__":
    sys.exit(main())
