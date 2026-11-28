"""Offline tests for the B1 + B3 ops-console fixes. No AWS or GitHub calls: every client the module
uses is replaced with a fake. Usage: python3 tests/test_b1_b3.py [<path/to/lambda_function.py>]  (default: this repo)
Prints PASS/FAIL per test and exits 1 if any fail (run it against the ORIGINAL source too: the
fix-specific tests must FAIL there, otherwise they don't discriminate)."""
import base64
import importlib.util
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

os.environ.update({"ACCOUNT_ID": "123456789012", "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1",
                   "UI_HARNESS": "UITestAgentHarness-AAAAAAAAAA", "BUGFIX_HARNESS": "BugFixAgentHarness-BBBBBBBBBB",
                   "RUNS_TABLE": "AgentAdminRuns", "QA_BUCKET": "test-qa-bucket", "COGNITO_POOL_ID": "us-east-1_test",
                   "TARGET_REPO": "owner/repo", "AWS_LAMBDA_FUNCTION_NAME": "agent-cicd-admin"})
from pathlib import Path
SRC_PATH = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1] / "src/lambda_function.py")
spec = importlib.util.spec_from_file_location("lf", SRC_PATH)
lf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lf)

REAL_GH_GET = lf._gh_get  # saved before any test replaces it
T0 = datetime(2026, 7, 26, 4, 23, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 30, 7, 13, tzinfo=timezone.utc)
results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, "PASS", ""))
    except Exception as e:
        results.append((fn.__name__, "FAIL", f"{type(e).__name__}: {e}"[:160]))
    return fn


class FakeS3:
    """run-latest (Jul 26, FAIL), pr-29 (open, Jul 25), pr-113 (closed, Oct 30, PASS)."""
    reports = {"run-latest": (T0, {"overall": "FAIL", "findings": [{"id": "F-301"}]}),
               "pr-29": (T0 - timedelta(days=1), {"overall": "FAIL", "findings": []}),
               "pr-113": (T1, {"overall": "PASS", "findings": []})}

    def list_objects_v2(self, Bucket, Delimiter=None, Prefix=None):
        if Delimiter:
            return {"CommonPrefixes": [{"Prefix": p + "/"} for p in list(self.reports) + ["intro"]]}
        p = Prefix.split("/")[0]
        return {"Contents": [{"Key": f"{p}/screenshots/{i:02d}.png"} for i in range(3)]}

    def head_object(self, Bucket, Key):
        p = Key.split("/")[0]
        if p not in self.reports:
            raise KeyError(Key)
        return {"LastModified": self.reports[p][0]}

    def get_object(self, Bucket, Key):
        ts, rep = self.reports[Key.split("/")[0]]
        return {"Body": type("B", (), {"read": lambda self: json.dumps(rep).encode()})(), "LastModified": ts}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://example.invalid/{Params['Key']}?exp={ExpiresIn}"


lf.s3 = FakeS3()
lf._open_pr_numbers = lambda: {29, 120}


class FakeCognito:
    class exceptions:
        NotAuthorizedException = type("NotAuthorizedException", (Exception,), {})

    def get_user(self, AccessToken):
        if AccessToken != "good":
            raise Exception("bad token")
        return {"Username": "admin"}


lf.cognito = FakeCognito()


def call(method, path, qs=None, headers=None, body=None):
    ev = {"requestContext": {"http": {"method": method, "path": path}}, "headers": headers or {},
          "queryStringParameters": qs or {}, "body": json.dumps(body) if body is not None else None}
    r = lf.handler(ev, None)
    return r["statusCode"], json.loads(r["body"]) if r["body"] else None


# ── B3: latest QA = newest overall, dated ────────────────────────────────────────────────
@test
def latest_report_is_newest_overall():
    r = lf.latest_qa_run()
    assert r["prefix"] == "pr-113", r["prefix"]
    assert r["report"]["overall"] == "PASS"
    assert r.get("prState") == "closed", r.get("prState")
    assert r["updated"].startswith("2026-10-30"), r["updated"]


# ── B1: screenshots only for signed-in callers ───────────────────────────────────────────
@test
def anonymous_gets_no_screenshot_urls():
    code, b = call("GET", "/api/qa-latest")
    assert code == 200, code
    assert b["screenshots"] == [], b["screenshots"][:1]
    assert b.get("screenshotsRequireAuth") is True and b.get("screenshotCount") == 3


@test
def signed_in_gets_short_lived_urls():
    code, b = call("GET", "/api/qa-latest", headers={"authorization": "Bearer good"})
    assert code == 200 and len(b["screenshots"]) == 3
    assert all("exp=600" in s["url"] for s in b["screenshots"]), b["screenshots"][0]["url"]


@test
def bad_token_treated_as_anonymous():
    code, b = call("GET", "/api/qa-latest", headers={"authorization": "Bearer nope"})
    assert b["screenshots"] == []


@test
def arbitrary_prefix_rejected():
    code, b = call("GET", "/api/qa-latest", qs={"prefix": "intro"})
    assert code == 400, code


# ── B3: evaluation scores use a time window, all pages, newest kept ──────────────────────
class FakeLogs:
    class exceptions:
        ResourceNotFoundException = type("RNF", (Exception,), {})

    def __init__(self):
        self.calls = []
        base = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
        self.events = [{"timestamp": base + k * 1000, "message": json.dumps(
            {"attributes": {"gen_ai.evaluation.name": "Builtin.GoalSuccessRate",
                            "gen_ai.evaluation.score.value": 1 if k % 4 else 0}})} for k in range(120)]

    def filter_log_events(self, **kw):
        self.calls.append(kw)
        if "startTime" not in kw:  # mimic CloudWatch: no start time -> oldest first, one page of `limit`
            return {"events": self.events[:kw.get("limit", 50)]}
        i = int(kw.get("nextToken", 0))
        page = self.events[i:i + 50]
        return {"events": page, **({"nextToken": str(i + 50)} if i + 50 < len(self.events) else {})}

    # Logs Insights: one stats query, aggregated "server-side" over the requested window
    def start_query(self, **kw):
        self.queries = getattr(self, "queries", []) + [kw]
        return {"queryId": "q1"}

    def get_query_results(self, queryId):
        q = self.queries[-1]
        vals = [json.loads(e["message"])["attributes"]["gen_ai.evaluation.score.value"] for e in self.events
                if q["startTime"] * 1000 <= e["timestamp"] <= q["endTime"] * 1000]
        rows = [[{"field": "name", "value": "Builtin.GoalSuccessRate"}, {"field": "n", "value": str(len(vals))},
                 {"field": "avg", "value": str(sum(vals) / len(vals))}]] if vals else []
        return {"status": "Complete", "results": rows}


class FakeCtl:
    def list_online_evaluation_configs(self):
        return {"onlineEvaluationConfigs": [{"onlineEvaluationConfigId": "ui_qa_x", "onlineEvaluationConfigName": "ui_qa_x", "status": "ACTIVE"}]}

    def get_online_evaluation_config(self, onlineEvaluationConfigId):
        return {"evaluators": [{"evaluatorId": "Builtin.GoalSuccessRate"}], "status": "ACTIVE"}

    def get_harness(self, harnessId):
        return {"harness": {"harnessName": "X", "harnessId": harnessId, "status": "READY"}}

    def update_harness(self, **kw):
        self.updated = kw


@test
def evaluation_stats_server_side_over_window_no_raw_records_cached():
    import time
    lf.logsc = FakeLogs()
    lf.ctl = FakeCtl()
    if hasattr(lf, "_EVAL_CACHE"):
        lf._EVAL_CACHE.clear()
    payload = lf.evaluations()
    cfg = payload["configs"][0]
    qs = getattr(lf.logsc, "queries", [])
    assert qs, "no Logs Insights query: stats were not computed server-side"
    assert "stats" in qs[0]["queryString"] and "evaluation.score.value" in qs[0]["queryString"]
    span = qs[0]["endTime"] - qs[0]["startTime"]
    assert span == 90 * 86400 and abs(qs[0]["endTime"] - time.time()) < 60, (span, qs[0]["endTime"])
    st = cfg["scoreStats"]["GoalSuccessRate"]
    assert st["count"] == 120 and abs(st["avg"] - 0.75) < 1e-9, st
    assert cfg.get("windowDays") == 90
    assert "recentScores" not in cfg, "raw result records still returned"
    lf.evaluations()
    assert len(lf.logsc.queries) == 1, "second call re-queried instead of using the cache"


# ── GitHub helper: failures are logged with the HTTP status; successes are cached ────────
@test
def github_failure_logged_with_status_and_success_cached():
    import contextlib
    import io
    import urllib.error
    calls = {"n": 0}
    real_urlopen = lf.urllib.request.urlopen

    def fail(req, timeout=8):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 403, "rate limited", {"X-RateLimit-Remaining": "0"}, None)
    try:
        if hasattr(lf, "_GH_CACHE"):
            lf._GH_CACHE.clear()
        lf.urllib.request.urlopen = fail
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert REAL_GH_GET("pulls?state=open") is None
        out = buf.getvalue()
        assert "HTTP 403" in out and "remaining=0" in out, f"failure not logged: {out!r}"

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'[{"number": 1}]'

        def ok(req, timeout=8):
            calls["n"] += 1
            return Resp()
        lf.urllib.request.urlopen = ok
        calls["n"] = 0
        a, b = REAL_GH_GET("pulls?state=open"), REAL_GH_GET("pulls?state=open")
        assert a == b == [{"number": 1}] and calls["n"] == 1, f"urlopen calls={calls['n']}"
    finally:
        lf.urllib.request.urlopen = real_urlopen


# ── B3: fan-out batch reaches a final status; errors scrubbed ────────────────────────────
class FakeTable:
    def __init__(self, items=None):
        self.items = {i["id"]: i for i in (items or [])}
        self.updates = []

    def update_item(self, **kw):
        self.updates.append(kw)
        item = self.items.setdefault(kw["Key"]["id"], {"id": kw["Key"]["id"]})
        vals = kw.get("ExpressionAttributeValues", {})
        if ":s" in vals:
            item["sessions"] = item.get("sessions", []) + vals[":s"]
        if ":f" in vals:
            item["status"] = vals[":f"]
        return {"Attributes": dict(item)} if kw.get("ReturnValues") == "ALL_NEW" else {}

    def scan(self, **kw):
        ids = sorted(self.items)
        start = ids.index(kw["ExclusiveStartKey"]["id"]) + 1 if "ExclusiveStartKey" in kw else 0
        page = ids[start:start + 2]
        if "Limit" in kw:
            page = ids[start:start + kw["Limit"]]
        out = {"Items": [self.items[i] for i in page]}
        if start + len(page) < len(ids):
            out["LastEvaluatedKey"] = {"id": page[-1]}
        return out


@test
def batch_closes_when_last_session_ends_and_errors_are_scrubbed():
    tbl = FakeTable([{"id": "b1", "concurrency": 2, "status": "running", "sessions": []}])
    lf.runs_tbl = tbl

    class D:
        n = 0

        def invoke_harness(self, **kw):
            D.n += 1
            if D.n == 2:
                raise Exception("AccessDenied for arn:aws:sts::123456789012:assumed-role/X/Y")
            return {"stream": []}
    lf.data = D()
    lf.run_one_session("b1", 0, "https://x")
    lf.run_one_session("b1", 1, "https://x")
    item = tbl.items["b1"]
    assert item.get("status") == "error", item.get("status")
    assert "123456789012" not in json.dumps(item) and "arn:aws" not in json.dumps(item), item["sessions"][1]["status"]


@test
def runs_list_pages_past_cache_items_and_derives_old_status():
    items = [{"id": f"cost-cache-{k}"} for k in range(30)] + [
        {"id": "zz-batch", "concurrency": 1, "status": "running", "startedAt": "2026-08-25",
         "sessions": [{"status": "done"}]}]
    lf.runs_tbl = FakeTable(items)
    runs = lf.list_runs()
    assert [r["id"] for r in runs] == ["zz-batch"], [r["id"] for r in runs]
    assert runs[0]["status"] == "done", runs[0]["status"]


# ── B3: pipeline stages match SHA-pinned / renamed steps; cap read from workflow ─────────
@test
def pipeline_stages_match_pinned_steps_and_cap_from_workflow():
    wf = "env:\n  MAX_FIX_ROUNDS: '15'\n"
    steps = [{"name": n, "status": "completed", "conclusion": "success",
              "started_at": "2026-10-30T07:06:10Z", "completed_at": "2026-10-30T07:06:20Z"} for n in [
        "Run actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
        "Run actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
        "Run pip install boto3==1.43.120",
        "Run aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a",
        "Deploy frontend for this branch", "Run UI QA Agent", "Count prior fix rounds",
        "Comment on PR", "Fail check if the QA report is not clean"]]

    def gh(path):
        if path.startswith("actions/workflows/"):
            return {"workflow_runs": [{"id": 1, "run_number": 239, "status": "completed", "conclusion": "success",
                                       "event": "pull_request", "head_branch": "b", "created_at": "x", "updated_at": "y", "html_url": "u"}]}
        if path.startswith("actions/runs/"):
            return {"jobs": [{"steps": steps}]}
        if path.startswith("contents/"):
            return {"content": base64.b64encode(wf.encode()).decode()}
        return []
    lf._gh_get = gh
    if hasattr(lf, "_ROUNDS_CACHE"):
        lf._ROUNDS_CACHE.clear()
    d = lf.pipeline_detail()
    st = {s["key"]: s["status"] for s in d["stages"]}
    assert st["setup"] == "completed" and st["auth"] == "completed" and st["gate"] == "completed", st
    assert d["maxFixRounds"] == 15, d["maxFixRounds"]


# ── writes / reads limited to the watched harnesses ──────────────────────────────────────
@test
def foreign_harness_write_refused():
    lf.ctl = FakeCtl()
    code, b = call("POST", "/api/skill", headers={"authorization": "Bearer good"},
                   body={"id": "SomeOtherHarness-ZZZZZZZZZZ", "skill": {"git": {"url": "u", "path": "p"}}})
    assert code == 403, (code, b)
    assert not hasattr(lf.ctl, "updated"), "update_harness was called for a foreign harness"


@test
def foreign_harness_read_refused():
    lf.ctl = FakeCtl()
    code, b = call("GET", "/api/harness", qs={"id": "SomeOtherHarness-ZZZZZZZZZZ"})
    assert code == 404, code


# ── cost-by-tag uses the harnesses' tag key ──────────────────────────────────────────────
@test
def cost_by_tag_groups_by_agent_type():
    seen = {}

    class CE:
        def get_cost_and_usage(self, **kw):
            seen.update(kw)
            return {"ResultsByTime": []}
    lf.ce = CE()
    lf._billed_by_tag(30)
    keys = [g["Key"] for g in seen["GroupBy"] if g["Type"] == "TAG"]
    assert keys == ["agent-type"], keys


# ── unhandled errors don't leak details ──────────────────────────────────────────────────
@test
def unhandled_error_is_generic():
    def boom(*a, **k):
        raise Exception("secret detail arn:aws:iam::123456789012:role/X")
    lf.pipeline_detail = boom
    code, b = call("GET", "/api/pipeline")
    assert code == 500 and "arn:aws" not in json.dumps(b) and "secret detail" not in json.dumps(b), b


w = max(len(n) for n, _, _ in results)
for n, r, why in results:
    print(f"  {r}  {n:<{w}}  {why}")
fails = sum(1 for _, r, _ in results if r == "FAIL")
print(f"{len(results) - fails}/{len(results)} passed")
sys.exit(1 if fails else 0)
