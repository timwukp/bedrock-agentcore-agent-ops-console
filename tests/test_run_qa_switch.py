"""Run QA kill switch and harness qualifier (src/run_qa_spec.json). Offline: every AWS client is a fake.
Usage: python3 tests/test_run_qa_switch.py [<worktree>]  (default: this repo). Exits 1 if any test fails."""
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

WT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
TARGET = "https://d0example.cloudfront.net"
os.environ.update({"ACCOUNT_ID": "123456789012", "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1",
                   "UI_HARNESS": "UITestAgentHarness-AAAAAAAAAA", "BUGFIX_HARNESS": "BugFixAgentHarness-BBBBBBBBBB",
                   "RUNS_TABLE": "AgentAdminRuns", "QA_BUCKET": "test-qa-bucket", "COGNITO_POOL_ID": "us-east-1_test",
                   "TARGET_REPO": "owner/repo", "AWS_LAMBDA_FUNCTION_NAME": "agent-cicd-admin",
                   "TARGET_URL": TARGET, "LOGIN_SECRET_ID": "app/login-creds-viewer"})
SPEC = json.loads((WT / "src/run_qa_spec.json").read_text())
for k in (SPEC["enable"]["env"], SPEC["qualifier"]["env"]):
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
        for k in (SPEC["enable"]["env"], SPEC["qualifier"]["env"]):
            os.environ.pop(k, None)
    return fn


class FakeCognito:
    class exceptions:
        NotAuthorizedException = type("NotAuthorizedException", (Exception,), {})

    def get_user(self, AccessToken):
        if AccessToken != "good":
            raise Exception("bad token")
        return {"Username": "admin"}


class FakeLam:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(json.loads(kw["Payload"]))


class FakeTable:
    def __init__(self, items=None):
        self.items = {i["id"]: i for i in (items or [])}

    def put_item(self, Item):
        self.items[Item["id"]] = dict(Item)

    def update_item(self, **kw):
        item = self.items.setdefault(kw["Key"]["id"], {"id": kw["Key"]["id"]})
        vals = kw.get("ExpressionAttributeValues", {})
        if ":s" in vals:
            item["sessions"] = item.get("sessions", []) + vals[":s"]
        return {"Attributes": dict(item)} if kw.get("ReturnValues") == "ALL_NEW" else {}

    def scan(self, **kw):
        return {"Items": list(self.items.values())}


class FakeData:
    def __init__(self):
        self.invocations = []

    def invoke_harness(self, **kw):
        self.invocations.append(kw)
        return {"stream": iter(())}


def call(method, path, body=None, headers=None):
    ev = {"requestContext": {"http": {"method": method, "path": path}}, "headers": headers or {},
          "queryStringParameters": {}, "body": json.dumps(body) if body is not None else None}
    r = lf.handler(ev, None)
    return r["statusCode"], json.loads(r["body"]) if r["body"] else None


AUTH = {"authorization": "Bearer good"}
lf.cognito = FakeCognito()


def fresh():
    lf.lam, lf.runs_tbl, lf.data = FakeLam(), FakeTable([{"id": "b1", "concurrency": 1, "sessions": []}]), FakeData()


@test
def code_reads_the_shipped_spec():
    assert getattr(lf, "RUN_QA_SPEC", None) == SPEC, "lambda_function.py does not load src/run_qa_spec.json"


@test
def enabled_by_default_per_spec():
    fresh()
    assert SPEC["enable"]["default"] is True
    code, b = call("POST", "/api/qa-run", {"concurrency": 1}, AUTH)
    assert code == 200 and len(lf.lam.calls) == 1, (code, b)


@test
def disabled_refuses_before_any_session():
    for v in SPEC["enable"]["falseValues"] + ["FALSE", " Off "]:
        fresh()
        os.environ[SPEC["enable"]["env"]] = v
        code, b = call("POST", "/api/qa-run", {"concurrency": 1}, AUTH)
        assert code == SPEC["disabled"]["status"] and b.get("error") == SPEC["disabled"]["error"], (v, code, b)
        assert lf.lam.calls == [] and set(lf.runs_tbl.items) == {"b1"}, f"{v!r}: a batch was started"


@test
def disabled_worker_never_invokes_the_harness():
    fresh()
    os.environ[SPEC["enable"]["env"]] = "false"
    lf.run_one_session("b1", 0, TARGET)
    s = lf.runs_tbl.items["b1"]["sessions"][0]
    assert lf.data.invocations == [], "invoke_harness was called while disabled"
    assert s["status"] == SPEC["disabled"]["workerStatus"], s["status"]


@test
def disabled_still_requires_sign_in_first():
    fresh()
    os.environ[SPEC["enable"]["env"]] = "false"
    code, _ = call("POST", "/api/qa-run", {"concurrency": 1})
    assert code == 401, code


@test
def qualifier_absent_when_unset():
    fresh()
    lf.run_one_session("b1", 0, TARGET)
    assert lf.data.invocations and "qualifier" not in lf.data.invocations[0], list(lf.data.invocations[0])


@test
def qualifier_sent_when_set():
    fresh()
    os.environ[SPEC["qualifier"]["env"]] = "ci"
    lf.run_one_session("b1", 0, TARGET)
    assert lf.data.invocations and lf.data.invocations[0].get("qualifier") == "ci", lf.data.invocations


@test
def every_invoke_harness_call_goes_through_the_qualifier_helper():
    src = (WT / "src/lambda_function.py").read_text()
    raw = re.findall(r"data\.invoke_harness\(", src)
    assert raw and len(raw) == len(re.findall(r"data\.invoke_harness\(\*\*_with_qualifier\(", src)), \
        "an invoke_harness call bypasses _with_qualifier"


@test
def deploy_paths_ship_the_spec():
    sh = (WT / "deploy/deploy.sh").read_text()
    stack = (WT / "cdk/stack.py").read_text()
    assert re.search(r'cp [^\n]*src/run_qa_spec\.json[^\n]*"\$BUILD/"', sh), "deploy.sh does not copy run_qa_spec.json"
    assert "run_qa_spec.json /asset-output/" in stack, "cdk/stack.py bundling does not copy run_qa_spec.json"


w = max(len(n) for n, _, _ in results)
for n, r, why in results:
    print(f"  {r}  {n:<{w}}  {why}")
fails = sum(1 for _, r, _ in results if r == "FAIL")
print(f"{len(results) - fails}/{len(results)} passed")
sys.exit(1 if fails else 0)
