import asyncio, hashlib, os
import pytest
from harness.blobs import BlobStore
from harness.preflight import run_preflight
from harness.sandbox import egress
from harness.sandbox.backend import LocalProcessBackend
from harness.sandbox.executor import ExecError
from harness.sandbox.manager import SandboxManager, TierError
from harness.sandbox.snapshots import SnapshotIntegrityError, restore_snapshot, take_snapshot
from conftest import make_config, events_of


@pytest.fixture
def sb(tmp_path):
    b = LocalProcessBackend(str(tmp_path / "sbx"))
    h = b.create({"role": "work", "run_id": "R"})
    yield h, b
    b.destroy(h)


def test_executor_edit_requires_unique_match(sb):
    ex = sb[0].executor
    ex.fs_write("/workspace/a.py", "x = 1\nx = 1\ny = 2\n")
    with pytest.raises(ExecError) as e: ex.fs_edit("/workspace/a.py", "x = 1", "x = 9")
    assert e.value.code == "fs.edit_no_unique_match"
    assert ex.fs_edit("/workspace/a.py", "y = 2", "y = 3") == {"replaced": 1}
    assert "y = 3" in ex.fs_read("/workspace/a.py")["content"]
    with pytest.raises(ExecError): ex.fs_edit("/workspace/a.py", "absent", "z")


def test_executor_roots_and_traversal(sb):
    ex = sb[0].executor
    for bad in ("/etc/passwd", "/workspace/../etc/passwd", "/workspacex/a", "rel/path", "/tests/x"):
        with pytest.raises(ExecError): ex.fs_read(bad)
    ex.fs_write("/scratch/t.txt", "ok")
    assert ex.fs_read("/scratch/t.txt")["content"] == "ok"
    assert ex.fs_glob("/workspace/*")["paths"] == []


def test_grep_glob_stat(sb):
    ex = sb[0].executor
    ex.fs_write("/workspace/src/a.py", "def foo():\n    pass\n"); ex.fs_write("/workspace/src/b.txt", "foo bar")
    assert [m["path"] for m in ex.fs_grep("foo", "/workspace")["matches"]] == ["/workspace/src/a.py", "/workspace/src/b.txt"]
    assert ex.fs_glob("/workspace/src/*.py")["paths"] == ["/workspace/src/a.py"]
    assert ex.fs_stat("/workspace/src")["is_dir"]
    with pytest.raises(ExecError): ex.fs_grep("(", "/workspace")


async def test_session_persists_cwd_and_exports_and_reports_loss(sb):
    ex = sb[0].executor
    ex.fs_write("/workspace/sub/f.txt", "x")
    sid = ex.session_open(user="agent")["session_id"]
    r = await ex.session_run(sid, "cd sub && export FOO=bar && pwd"); assert r["exit_code"] == 0 and r["cwd"] == "/workspace/sub"
    r = await ex.session_run(sid, "echo $FOO; ls"); assert r["stdout"].split() == ["bar", "f.txt"]
    r = await ex.session_run(sid, "exit 3"); assert r["exit_code"] == 3
    ex.session_close(sid)
    with pytest.raises(ExecError) as e: await ex.session_run(sid, "true")
    assert e.value.code == "session.lost"


async def test_exec_timeout_kills_process_group_and_unprovisioned_user_is_refused(sb):
    ex = sb[0].executor
    r = await ex.exec_run(["bash", "-c", "sleep 30"], timeout_s=1)
    assert r["timed_out"] and r["exit_code"] is None
    with pytest.raises(ExecError) as e: ex.exec_start(["true"], user="root")
    assert e.value.code == "exec.unknown_user"
    r = await ex.exec_run(["bash", "-c", "echo $HARNESS_WORKSPACE"], user="candidate")
    assert r["stdout"].strip() == ex.roots["/workspace"]


def test_snapshot_roundtrip_is_content_addressed_and_dedupes(sb, tmp_path):
    h, b = sb
    blobs = BlobStore(tmp_path / "blobs")
    h.executor.fs_write("/workspace/a.txt", "alpha"); h.executor.fs_write("/workspace/d/b.txt", "beta")
    s1 = take_snapshot(h, blobs); assert s1["files"] == 2 and s1["new_blobs"] == 2
    h.executor.fs_write("/workspace/c.txt", "alpha")                        # same content as a.txt
    s2 = take_snapshot(h, blobs); assert s2["new_blobs"] == 0 and s2["manifest_hash"] != s1["manifest_hash"]
    assert take_snapshot(h, blobs)["manifest_hash"] == s2["manifest_hash"]   # deterministic
    h2 = b.create({"role": "judge", "run_id": "R"})
    assert restore_snapshot(s1["manifest_hash"], blobs, h2) == 2
    assert h2.executor.fs_read("/workspace/d/b.txt")["content"] == "beta"
    assert not os.path.exists(h2.executor.roots["/workspace"] + "/c.txt")    # restores the snapshot, not the later state


def test_restore_detects_blob_tampering(sb, tmp_path):
    h, b = sb
    blobs = BlobStore(tmp_path / "blobs")
    h.executor.fs_write("/workspace/a.txt", "alpha")
    s = take_snapshot(h, blobs)
    digest = hashlib.sha256(b"alpha").hexdigest()
    blobs._path(digest).write_bytes(b"EVIL")
    with pytest.raises(IOError): restore_snapshot(s["manifest_hash"], blobs, b.create({"role": "judge", "run_id": "R"}))


def test_egress_default_deny_and_ssrf():
    g = [{"host_glob": "*.pypi.org", "ports": [443]}, {"host_glob": "internal.corp", "ports": [443], "allow_private": True}]
    assert egress.decide("files.pypi.org", 443, g, ["151.101.1.1"])["decision"] == "allow"
    assert egress.decide("files.pypi.org", 80, g)["reason"] == "default_deny"
    assert egress.decide("evil.com", 443, g)["reason"] == "default_deny"
    assert egress.decide("FILES.PyPI.org.", 443, g, ["151.101.1.1"])["decision"] == "allow"           # normalized
    for ip in ("10.0.0.5", "192.168.1.1", "127.0.0.1", "169.254.169.254", "::1", "fd00:ec2::254"):
        assert egress.decide("files.pypi.org", 443, g, [ip])["reason"] == "ssrf_blocked_address", ip  # DNS rebinding to private space
    assert egress.decide("internal.corp", 443, g, ["10.1.2.3"])["decision"] == "allow"                 # explicit grant allows private


async def test_tier_enforcement_refuses_weak_backend(tmp_path):
    m = SandboxManager(LocalProcessBackend(str(tmp_path)), allow_dev_tier=True)
    m.check_tier("core")
    for p in ("standard", "high"):
        with pytest.raises(TierError): m.check_tier(p)
    with pytest.raises(TierError): SandboxManager(LocalProcessBackend(str(tmp_path))).check_tier("core")


async def test_standard_profile_run_is_blocked_by_preflight_on_dev_backend(mk):
    h = mk([], profile="standard", tier="T-Dev")
    run, _ = await h.create_run("u", "x")
    t = h.store.events(run.run_id, types=["run.terminated"])[0]["payload"]
    assert "PF-02" in t["detail"] and await run.state() == "TERMINATED"


async def test_teardown_is_idempotent_and_revokes_everything(mk):
    h = mk([], egress={"default": "deny", "grants": [{"host_glob": "pypi.org", "ports": [443]}]})
    run, _ = await h.create_run("u", "x")
    assert len(await run.proj("grants_active")) == 2
    await h.sandboxes.teardown(run); await h.sandboxes.teardown(run)
    assert await run.proj("grants_active") == {} and await run.proj("sandboxes") == {}
    assert len(events_of(h, run, "grant.revoked")) == 2


def pf(profile, cfg=None, **kw):
    return {c["id"]: c for c in run_preflight(profile, cfg or {}, **kw)["checks"]}


def test_preflight_individual_checks(tmp_path, mk):
    h = mk([])
    base = h.config
    assert not pf("bogus")["PF-01"]["ok"]
    assert not pf("core", {**base, "egress": {"default": "allow"}})["PF-03"]["ok"]
    assert not pf("core", {**base, "egress": {"default": "deny", "grants": [{"host_glob": "x", "ports": ["443"]}]}})["PF-03"]["ok"]
    assert not pf("standard", {"budgets": {"turns": 1, "tokens": 1}})["PF-04"]["ok"]
    assert pf("core", {"budgets": {"turns": 1, "tokens": 1, "time_s": 1}})["PF-04"]["ok"]
    leak = pf("core", base, known_secrets=["s3cr3t-value"], sandbox_envs=[{"A": "ok"}, {"B": "x s3cr3t-value y"}])
    assert not leak["PF-05"]["ok"] and "#1" in leak["PF-05"]["message"]
    exe = tmp_path / "exec.bin"; exe.write_bytes(b"binary")
    cfg = {**base, "sandbox": {**base["sandbox"], "executor_sha256": hashlib.sha256(b"binary").hexdigest()}}
    assert pf("core", cfg, executor_path=str(exe))["PF-06"]["ok"]
    exe.write_bytes(b"tampered"); assert not pf("core", cfg, executor_path=str(exe))["PF-06"]["ok"]
    assert not pf("core", base, ruleset={"schema_version": 1, "rules": [{"id": "a", "action": "allow"}, {"id": "a", "action": "deny"}]})["PF-07"]["ok"]
    assert not pf("core", base, ruleset={"schema_version": 1, "rules": [{"id": "a", "action": "maybe"}]})["PF-07"]["ok"]
    assert not pf("core", base, ruleset={"schema_version": 1, "rules": [{"id": "a", "action": "deny", "match": {"args": [{"path": "$.x", "op": "regex", "value": "(?=x)"}]}}]})["PF-07"]["ok"]
    assert not pf("standard", base, registry=type("R", (), {"warnings": [1]})())["PF-08"]["ok"] and pf("core", base, registry=type("R", (), {"warnings": [1]})())["PF-08"]["severity"] == "warn"
    assert not pf("core", {"mcp": {"servers": [{"tools": {"t": "short"}}]}})["PF-09"]["ok"]
    assert not pf("standard", {"approvals": {"channels": ["cli"], "approvers": []}})["PF-10"]["ok"]
    assert pf("core", base, store=h.store, writer=h.writer)["PF-11"]["ok"]
    assert not pf("standard", {"evaluator": {"independence": "same_model_low_independence"}})["PF-12"]["ok"]
    assert pf("core", {"evaluator": {"independence": "same_model_low_independence"}})["PF-12"]["ok"]
    assert not pf("core", {}, work_roots=["/sbx/work"], hidden_tests_dirs=["/sbx/work/tests"])["PF-13"]["ok"]
    assert not pf("standard", {"cache": {"semantic": "on"}})["PF-14"]["ok"]
    assert not pf("standard", {})["PF-15"]["ok"] and not pf("standard", {})["PF-16"]["ok"]
    assert pf("standard", {"retention": {"events_days": 1, "blobs_days": 1}, "auth": {"oidc": {"issuer": "i", "audience": "a"}}})["PF-16"]["ok"]


async def test_host_secrets_never_reach_sandbox_processes(sb, monkeypatch):
    """H-4.2 zero-credential sandboxing: the daemon's environment is not inherited by exec, sessions or verifiers."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-LEAKCHECK-0000000000000000")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "LEAKCHECK-aws")
    ex = sb[0].executor
    r = await ex.exec_run(["bash", "-c", "env"], user="candidate")
    assert "LEAKCHECK" not in r["stdout"] and "PATH=" in r["stdout"] and "HARNESS_WORKSPACE=" in r["stdout"]
    sid = ex.session_open(user="agent")["session_id"]
    r = await ex.session_run(sid, "export FOO=bar; env; echo key=$ANTHROPIC_API_KEY")
    assert "LEAKCHECK" not in r["stdout"] and "key=\n" in r["stdout"]
    r = await ex.session_run(sid, "echo $FOO")                      # session state still persists
    assert r["stdout"].strip() == "bar"
    r = await ex.exec_run(["python3", "-c", "import os;print(any('LEAKCHECK' in v for v in os.environ.values()))"])
    assert r["stdout"].strip() == "False"
