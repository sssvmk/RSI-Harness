"""A retrieval plugin in its own process, speaking the newline-delimited JSON protocol on stdio. `--slow` makes `query` hang, so a test can see the host kill it."""
import json, sys, time
slow = "--slow" in sys.argv
OUT = {"describe": {"id": "proc-retrieval", "kind": "retrieval", "version": "1.0.0"}, "health": {"ok": True}, "index": {"indexed": 1, "index_version": "v1"}, "forget": {"removed": 1},
       "query": {"passages": [{"id": "d1", "text": "from a process", "score": 1.0, "source": "proc", "permissions": {}}]}}
for line in sys.stdin:
    req = json.loads(line)
    if req["method"] == "query" and slow: time.sleep(30)
    if req["method"] == "query" and req["params"].get("k", 1) < 1: print(json.dumps({"id": req["id"], "error": {"code": "invalid_argument", "message": "k must be at least 1"}}), flush=True); continue
    if req["method"] == "query" and req["params"].get("user") == "__no_access__": print(json.dumps({"id": req["id"], "error": {"code": "permission_denied", "message": "no access"}}), flush=True); continue
    if req["method"] == "index" and any(not d.get("id") for d in req["params"].get("documents", [])): print(json.dumps({"id": req["id"], "error": {"code": "invalid_argument", "message": "empty id"}}), flush=True); continue
    if req["method"] == "forget" and not req["params"].get("ids"): print(json.dumps({"id": req["id"], "error": {"code": "invalid_argument", "message": "no ids"}}), flush=True); continue
    if req["method"] == "boom": print(json.dumps({"id": req["id"], "error": {"code": "boom", "message": "exploded"}}), flush=True); continue
    print(json.dumps({"id": req["id"], "result": OUT[req["method"]]} if req["method"] in OUT else {"id": req["id"], "error": {"code": "no_method", "message": req["method"]}}), flush=True)
