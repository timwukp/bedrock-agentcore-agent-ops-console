"""Run QA takes the shared UI harness lease and rotates the login around the harness call (src/run_qa_spec.json
'lease', 'rotation', 'order'; the lease rules are the vendored src/harness_lock_spec.json). Offline: all fakes.
Usage: python3 tests/test_run_qa_lease.py [<worktree>]  (default: this repo). Exits 1 if any test fails."""
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

WT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes import FakeBroker, FakeClientError, FakeS3Lease  # noqa: E402

TARGET = "https://d0example.cloudfront.net"
os.environ.update({"ACCOUNT_ID": "123456789012", "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1",
                   "UI_HARNESS": "UITestAgentHarness-AAAAAAAAAA", "BUGFIX_HARNESS": "BugFixAgentHarness-BBBBBBBBBB",
                   "RUNS_TABLE": "AgentAdminRuns", "QA_BUCKET": "test-qa-bucket", "COGNITO_POOL_ID": "us-east-1_test",
                   "TARGET_REPO": "owner/repo", "AWS_LAMBDA_FUNCTION_NAME": "agent-cicd-admin",
                   "TARGET_URL": TARGET, "LOGIN_SECRET_ID": "app/login-creds-viewer"})
SPEC = json.loads((WT / "src/run_qa_spec.json").read_text())
LOCK = json.loads((WT / "src/harness_lock_spec.json").read_text())
BROKER = json.loads((WT / "src/qa_reset_broker_spec.json").read_text())
ROT = SPEC["rotation"]
for k in (SPEC["enable"]["env"], SPEC["qualifier"]["env"], ROT["env"], ROT["brokerFunctionEnv"], ROT["secretEnv"]):
    os.environ.pop(k, None)
spec = importlib.util.spec_from_file_location("lf", WT / "src/lambda_function.py")
lf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lf)
results = []


def test(fn):
    try:
        fn()
        results.append((fn.__name__, "PASS", ""))
    except Exception as e:  # noqa: BLE001
        results.append((fn.__name__, "FAIL", f"{type(e).__name__}: {e}"[:170]))
    finally:
        os.environ.pop(ROT["env"], None)
    return fn


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeTable:
    def __init__(self, items=None):
        self.items = {i["id"]: i for i in (items or [])}

    def update_item(self, **kw):
        item = self.items.setdefault(kw["Key"]["id"], {"id": kw["Key"]["id"]})
        vals = kw.get("ExpressionAttributeValues", {})
        if ":s" in vals:
            item["sessions"] = item.get("sessions", []) + vals[":s"]
        return {"Attributes": dict(item)} if kw.get("ReturnValues") == "ALL_NEW" else {}


class FakeData:
    def __init__(self, log, raise_exc=None, on_stream=None):
        self.invocations, self.log, self.raise_exc, self.on_stream = [], log, raise_exc, on_stream

    def invoke_harness(self, **kw):
        self.invocations.append(kw)
        self.log.append(("invoke",))
        if self.raise_exc:
            raise self.raise_exc

        def stream():
            if self.on_stream:
                self.on_stream()
            return iter(())
        return {"stream": stream()}


def setup(answers=None, raise_exc=None, on_stream=None, preexisting=None):
    log = []
    s3 = FakeS3Lease()
    if preexisting is not None:
        s3.objects[LOCK["key"]] = (json.dumps(preexisting).encode(), '"e0"')
    orig_put, orig_del = s3.put_object, s3.delete_object

    def put(**kw):
        r = orig_put(**kw)
        log.append(("lease-put",))
        return r

    def delete(**kw):
        r = orig_del(**kw)
        log.append(("lease-delete",))
        return r
    s3.put_object, s3.delete_object = put, delete
    clock = Clock()
    lf.lock_s3, lf.broker_lambda, lf.LEASE_CLOCK = s3, FakeBroker(answers, log), clock
    lf.data = FakeData(log, raise_exc, on_stream)
    lf.runs_tbl = FakeTable([{"id": "b1", "concurrency": 1, "sessions": []}])
    return log, s3, clock


def run():
    lf.run_one_session("b1", 0, TARGET)
    return lf.runs_tbl.items["b1"]["sessions"][0]


def kinds(log):
    return [e[0] if e[0] != "rotate" else f"rotate:{'after' if e[1] else 'before'}" for e in log]


def lease_body(owner="ci:1", run_id="qa-ci-x", expires_in=600, clock=None):
    now = (clock or Clock())()
    f = lambda t: __import__("time").strftime(LOCK["timeFormat"], __import__("time").gmtime(int(t)))  # noqa: E731
    return {"owner": owner, "runId": run_id, "acquiredAt": f(now - 60), "expiresAt": f(now + expires_in)}


@test
def happy_path_follows_the_spec_order():
    log, s3, _ = setup()
    s = run()
    assert kinds(log) == ["lease-put", "rotate:before", "invoke", "rotate:after", "lease-delete"], kinds(log)
    assert s3.lease() is None, "lease left behind"
    reqs = lf.broker_lambda.requests
    assert [r["signOut"] for r in reqs] == [ROT["before"]["signOut"], ROT["after"]["signOut"]], reqs
    assert all(r["runId"] == s["session"] and r["secret"] == ROT["secretDefault"] and
               r["function"] == ROT["brokerFunctionDefault"] and r["action"] == "rotate" for r in reqs), reqs
    assert s["lease"] == "ok" and s["rotation"] == {"before": "ok", "after": "ok"}, (s.get("lease"), s.get("rotation"))


@test
def lease_owner_and_run_id_follow_the_spec():
    seen = {}
    log, s3, _ = setup(on_stream=lambda: seen.update(s3.lease()))
    s = run()
    assert seen["owner"] == SPEC["lease"]["owner"].format(batchId="b1") and seen["runId"] == s["session"], seen


@test
def busy_lease_refuses_before_any_rotation_or_invoke():
    log, s3, clock = setup(preexisting=lease_body())
    s = run()
    assert lf.broker_lambda.requests == [] and lf.data.invocations == [], kinds(log)
    assert s["status"] == SPEC["lease"]["busyStatus"] and s["lease"] == "busy", s
    assert s3.lease()["owner"] == "ci:1", "someone else's lease was touched"


@test
def expired_lease_is_taken_over():
    log, s3, clock = setup(preexisting=lease_body(expires_in=-LOCK["clockSkewSeconds"] - 5))
    s = run()
    assert lf.data.invocations and s["lease"] == "ok", (kinds(log), s)


@test
def broker_unreachable_before_means_no_harness_call_and_release():
    log, s3, _ = setup(answers=[FakeClientError("ServiceException", 500)])
    s = run()
    assert lf.data.invocations == [], "harness invoked without a fresh password"
    assert s["rotation"]["before"].startswith("failed") and s["status"].startswith("error"), s
    assert s3.lease() is None, "lease not released after a failed rotation"


@test
def broker_refusal_before_means_no_harness_call():
    log, s3, _ = setup(answers=[{"ok": False, "error": "lease-not-yours"}])
    s = run()
    assert lf.data.invocations == [] and "lease-not-yours" in s["rotation"]["before"], s


@test
def harness_failure_still_rotates_after_and_releases():
    log, s3, _ = setup(raise_exc=RuntimeError("stream broke"))
    s = run()
    assert "rotate:after" in kinds(log) and s3.lease() is None, kinds(log)
    assert s["rotation"]["after"] == "ok" and s["status"].startswith("error"), s


@test
def lease_lost_mid_run_skips_rotate_after_and_says_so():
    holder = {}

    def steal():  # the lease runs out while the harness streams, and another run takes it over
        holder["c"].t += LOCK["ttlSeconds"] + LOCK["clockSkewSeconds"] + 1
    log, s3, clock = setup(on_stream=steal)
    holder["c"] = clock
    s = run()
    assert "rotate:after" not in kinds(log), kinds(log)
    assert s["lease"] == "lost" and s["rotation"]["after"] == "skipped", s
    assert SPEC["lease"]["lostNote"] in s.get("note", ""), s.get("note")


@test
def lease_lost_before_the_harness_call_means_no_invoke():
    log, s3, clock = setup()
    real = lf.broker_lambda.invoke

    def slow_rotate(**kw):  # the lease runs out while the broker rotates
        clock.t += LOCK["ttlSeconds"] + LOCK["clockSkewSeconds"] + 1
        return real(**kw)
    lf.broker_lambda.invoke = slow_rotate
    s = run()
    assert lf.data.invocations == [], "harness invoked on a lost lease"
    assert s["lease"] == "lost" and "lost before the harness call" in s["status"], s


@test
def manual_mode_takes_the_lease_but_never_calls_the_broker():
    os.environ[ROT["env"]] = "manual"
    log, s3, _ = setup()
    s = run()
    assert lf.broker_lambda.requests == [] and lf.data.invocations and s3.lease() is None, kinds(log)
    assert s["rotation"] == {"before": "skipped", "after": "skipped"}, s["rotation"]


@test
def broker_request_matches_the_vendored_broker_spec():
    log, s3, _ = setup()
    run()
    req = {k: v for k, v in lf.broker_lambda.requests[0].items() if k != "function"}
    assert set(req) == set(BROKER["request"]), (set(req), set(BROKER["request"]))
    assert req["action"] == BROKER["request"]["action"]


@test
def vendored_files_match_their_pinned_hashes():
    pins = json.loads((WT / "src/VENDORED.json").read_text())
    for name, meta in pins["files"].items():
        got = hashlib.sha256((WT / "src" / name).read_bytes()).hexdigest()
        assert got == meta["sha256"], f"{name}: {got[:12]} != pinned {meta['sha256'][:12]} (edit upstream, then re-vendor)"


@test
def deploy_paths_ship_every_runtime_file():
    sh = (WT / "deploy/deploy.sh").read_text()
    stack = (WT / "cdk/stack.py").read_text()
    for f in ("run_qa_spec.json", "harness_lock.py", "harness_lock_spec.json", "qa_reset_broker_spec.json"):
        assert re.search(rf'cp [^\n]*src/{re.escape(f)}', sh) or re.search(r'cp [^\n]*src/\{[^}]*' + re.escape(f), sh) \
            or (f in sh and 'cp ' in sh and 'src/' in sh), f"deploy.sh does not ship {f}"
        assert f in stack, f"cdk/stack.py bundling does not ship {f}"


def rendered_policy():
    sh = (WT / "deploy/deploy.sh").read_text()
    m = re.search(r'^sed "s[/|][^\n]*?iam-policy\.json"', sh.replace("\\\n", " "), re.M)
    cmd = m.group(0).replace('"$(dirname "$0")/iam-policy.json"', str(WT / "deploy/iam-policy.json"))
    env = dict(os.environ, UI_HARNESS="UITestAgentHarness-CCCCCCCCCC", BUGFIX_HARNESS="",
               UI_ROLE_ARN="arn:aws:iam::123456789012:role/R", BUGFIX_ROLE_ARN="arn:aws:iam::123456789012:role/R",
               QA_BUCKET="qa-bucket", ACCOUNT_ID="123456789012", REGION="us-east-1",
               QA_BROKER_FUNCTION=ROT["brokerFunctionDefault"])
    return json.loads(subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, check=True).stdout)


@test
def iam_grants_only_the_lock_key_and_the_broker():
    pol = rendered_policy()
    grants = [(a, r) for s in pol["Statement"] for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
              for r in (s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]])]
    key = f"arn:aws:s3:::qa-bucket/{LOCK['key']}"
    for a in ("s3:PutObject", "s3:DeleteObject"):
        rs = {r for x, r in grants if x == a}
        assert rs == {key}, f"{a} granted on {rs}, expected only the lock key"
    fn = f"arn:aws:lambda:us-east-1:123456789012:function:{ROT['brokerFunctionDefault']}"
    assert fn in {r for x, r in grants if x == "lambda:InvokeFunction"}, "no invoke grant on the broker"


w = max(len(n) for n, _, _ in results)
for n, r, why in results:
    print(f"  {r}  {n:<{w}}  {why}")
fails = sum(1 for _, r, _ in results if r == "FAIL")
print(f"{len(results) - fails}/{len(results)} passed")
sys.exit(1 if fails else 0)
