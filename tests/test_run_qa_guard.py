"""Run QA reads qa_tool_guard's own decisions from the harness stream (src/run_qa_spec.json 'guard'; the rule is the
vendored src/qa_tool_guard_spec.json 'consumers' entry; parity with token-monitoring ci-agent/qa_agent.py audit_guard).
Offline: all fakes. Usage: python3 tests/test_run_qa_guard.py [<worktree>]  (default: this repo). Exits 1 if any fails."""
import copy
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

WT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes import FakeBroker, FakeS3Lease  # noqa: E402

TARGET = "https://d0example.cloudfront.net"
os.environ.update({"ACCOUNT_ID": "123456789012", "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1",
                   "UI_HARNESS": "UITestAgentHarness-AAAAAAAAAA", "BUGFIX_HARNESS": "BugFixAgentHarness-BBBBBBBBBB",
                   "RUNS_TABLE": "AgentAdminRuns", "QA_BUCKET": "test-qa-bucket", "COGNITO_POOL_ID": "us-east-1_test",
                   "TARGET_REPO": "owner/repo", "AWS_LAMBDA_FUNCTION_NAME": "agent-cicd-admin",
                   "TARGET_URL": TARGET, "LOGIN_SECRET_ID": "app/login-creds-viewer"})
SPEC = json.loads((WT / "src/run_qa_spec.json").read_text())
G = SPEC["guard"]
GUARD_SPEC = json.loads((WT / "src" / G["spec"]).read_text())
STREAMS = json.loads((WT / "src/qa_tool_guard_stream_cases.json").read_text())
EXPECTED_ENV = G["expected"]["env"]
for k in (SPEC["enable"]["env"], SPEC["qualifier"]["env"], SPEC["rotation"]["env"], EXPECTED_ENV):
    os.environ.pop(k, None)
os.environ[SPEC["rotation"]["env"]] = "manual"  # these tests are about the stream, not the broker
spec = importlib.util.spec_from_file_location("lf", WT / "src/lambda_function.py")
lf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lf)
results = []
# Harness main c839d7c's SHA256SUMS. Written out on purpose: a re-vendor must change this line too.
UPSTREAM = {"qa_tool_guard_spec.json": "c67f8586859077f2501a3d959602c9f6462c8355bf217c9370277abf4c517a3c",
            "qa_tool_guard_stream_cases.json": "3211dbb068fb8bb4a7c2617c425fcc4298f27c8219e397acac3ef0c28ffbed7c"}
MARKER = "S3cr3t-Marker-Pw-guard"


def test(fn):
    try:
        fn()
        results.append((fn.__name__, "PASS", ""))
    except Exception as e:  # noqa: BLE001
        results.append((fn.__name__, "FAIL", f"{type(e).__name__}: {e}"[:170]))
    finally:
        os.environ.pop(EXPECTED_ENV, None)
    return fn


def replay(events):
    calls, hooks = [], []
    lf._collect_tool_calls(iter(copy.deepcopy(events)), calls, hooks)
    return lf.audit_guard(hooks), hooks, calls


# ── streams and the consumer rule ────────────────────────────────────────────────────────────────
@test
def every_fixture_stream_gives_its_expected_verdict():
    bad = []
    for case in STREAMS["cases"]:
        got, _, _ = replay(case["events"])
        if got != case["expect"]:
            bad.append(f"{case['id']}: {got} != {case['expect']}")
    assert not bad, bad
    assert len(STREAMS["cases"]) == 14, len(STREAMS["cases"])


@test
def all_three_verdicts_are_exercised():
    assert {c["expect"]["readonly"] for c in STREAMS["cases"]} == {"ok", "violated", "unobserved"}


@test
def hook_name_and_event_come_from_the_vendored_guard_spec():
    assert (lf.GUARD_NAME, lf.GUARD_EVENT) == (GUARD_SPEC["name"], GUARD_SPEC["event"]), (lf.GUARD_NAME, lf.GUARD_EVENT)
    assert lf.GUARD_RULE.pattern == G["rulePrefix"], lf.GUARD_RULE.pattern


@test
def only_a_well_formed_rule_prefix_is_counted_by_rule():
    reasons = {"B02: click on a write-type control": "B02", "S01: x": "S01", "B2: x": "?", "b02: x": "?",
               "B02 x": "?", "XB02: x": "?", "B123: x": "?", "": "?"}
    for reason, want in reasons.items():
        events = [{"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": "t", "name": "shell"}}}},
                  {"hookEvent": {"hookEventId": "h", "name": GUARD_SPEC["name"], "type": GUARD_SPEC["event"],
                                 "decision": "deny", "reason": reason}}]
        got = lf.audit_guard(events)["denies"]
        assert got == {want: 1}, (reason, got)


@test
def collector_keeps_only_hook_events_and_tool_use_starts():
    events = [{"messageStart": {"role": "assistant"}},
              {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
              {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": f"the password is {MARKER}"}}},
              {"contentBlockStop": {"contentBlockIndex": 0}},
              {"contentBlockStart": {"contentBlockIndex": 1, "start": {"toolUse": {"toolUseId": "t1", "name": "browser"}}}},
              {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": json.dumps(
                  {"browser_input": {"action": {"type": "type", "selector": "#password", "text": MARKER}}})}}}},
              {"contentBlockStop": {"contentBlockIndex": 1}},
              {"hookEvent": {"hookEventId": "h1", "name": "qa_tool_guard", "type": "before_tool_call",
                             "decision": "allow", "reason": "A00: allowed"}},
              {"messageStop": {"stopReason": "end_turn"}}]
    _, hooks, calls = replay(events)
    assert [list(e) for e in hooks] == [["contentBlockStart"], ["hookEvent"]], hooks
    assert MARKER not in json.dumps(hooks), "tool input or text leaked into hook_events"
    assert len(calls) == 1 and calls[0]["tool"] == "browser", calls  # audit_actions' input is unchanged


@test
def a_stream_cut_off_keeps_what_arrived():
    def stream():
        yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": "t1", "name": "browser"}}}}
        yield {"hookEvent": {"hookEventId": "h1", "name": "qa_tool_guard", "type": "before_tool_call",
                             "decision": "deny", "reason": "B02: click on a write-type control"}}
        raise ConnectionError("read timeout")
    calls, hooks = [], []
    try:
        lf._collect_tool_calls(stream(), calls, hooks)
    except ConnectionError:
        pass
    assert lf.audit_guard(hooks) == {"readonly": "violated", "toolCalls": 1, "guardDecisions": 1, "denies": {"B02": 1}}


@test
def collector_without_hook_list_behaves_as_before():
    calls = []
    lf._collect_tool_calls(iter(STREAMS["cases"][0]["events"]), calls)
    assert len(calls) == STREAMS["cases"][0]["expect"]["toolCalls"], calls


# ── the fold and the setting ─────────────────────────────────────────────────────────────────────
@test
def fold_rules_in_both_modes():
    v = lambda r: {"readonly": r}  # noqa: E731
    f = lf.fold_readonly
    for expected in (False, True):
        assert f("ok", v("violated"), expected) == "violated"
        assert f("unobserved", v("violated"), expected) == "violated"
        assert f("violated", v("ok"), expected) == "violated"
        assert f("ok", None, expected) == "ok"
    assert f("ok", v("unobserved"), False) == "ok", "a coverage gap must not count before the guard is expected"
    assert f("unobserved", v("ok"), False) == "unobserved", "not expected: audit_actions' verdict stands"
    assert f("ok", v("unobserved"), True) == "unobserved"
    assert f("unobserved", v("ok"), True) == "unobserved"
    assert f("ok", v("ok"), True) == "ok"


@test
def expected_setting_reads_the_spec_env_and_defaults_off():
    assert G["expected"]["default"] is False
    os.environ.pop(EXPECTED_ENV, None)
    assert lf._guard_expected() is False
    for raw, want in (("1", True), ("true", True), (" TRUE ", True), ("yes", False), ("false", False), ("", False)):
        os.environ[EXPECTED_ENV] = raw
        assert lf._guard_expected() is want, (raw, lf._guard_expected())


# ── run_one_session end to end ───────────────────────────────────────────────────────────────────
class FakeTable:
    def __init__(self):
        self.items = {"b1": {"id": "b1", "concurrency": 1, "sessions": []}}

    def update_item(self, **kw):
        item = self.items.setdefault(kw["Key"]["id"], {"id": kw["Key"]["id"]})
        vals = kw.get("ExpressionAttributeValues", {})
        if ":s" in vals:
            item["sessions"] = item.get("sessions", []) + vals[":s"]
        return {"Attributes": dict(item)} if kw.get("ReturnValues") == "ALL_NEW" else {}


class FakeData:
    def __init__(self, events):
        self.events = events

    def invoke_harness(self, **kw):
        return {"stream": iter(copy.deepcopy(self.events))}


def browser_call(i, action, hook=None):
    ev = [{"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": f"t{i}", "name": "browser"}}}},
          {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": json.dumps(
              {"browser_input": {"action": action}})}}}},
          {"contentBlockStop": {"contentBlockIndex": 0}}]
    if hook:
        ev.append({"hookEvent": {"hookEventId": f"h{i}", "name": "qa_tool_guard", "type": "before_tool_call", **hook}})
    return ev


def session(events, preexisting_lease=False):
    s3 = FakeS3Lease()
    if preexisting_lease:
        lock = json.loads((WT / "src/harness_lock_spec.json").read_text())
        import time
        fmt = lambda t: time.strftime(lock["timeFormat"], time.gmtime(int(t)))  # noqa: E731
        now = lf.LEASE_CLOCK()
        s3.objects[lock["key"]] = (json.dumps({"owner": "ci:1", "runId": "qa-ci-x", "acquiredAt": fmt(now - 60),
                                               "expiresAt": fmt(now + 600)}).encode(), '"e0"')
    lf.lock_s3, lf.broker_lambda = s3, FakeBroker(None, [])
    lf.data, lf.runs_tbl = FakeData(events), FakeTable()
    lf.run_one_session("b1", 0, TARGET)
    return lf.runs_tbl.items["b1"]["sessions"][0]


NAV = {"type": "navigate", "url": TARGET + "/"}
ALLOW = {"decision": "allow", "reason": "A00: allowed"}


@test
def session_records_guard_and_a_deny_makes_it_violated():
    s = session(browser_call(1, NAV, ALLOW) + browser_call(2, {"type": "click", "selector": "a[href='/cost']"},
                                                           {"decision": "deny", "reason": "B03: admin pass"}))
    assert s["guard"] == {"readonly": "violated", "toolCalls": 2, "guardDecisions": 2, "denies": {"B03": 1}}, s.get("guard")
    assert s["readonly"] == "violated", s["readonly"]


@test
def without_the_guard_expected_a_missing_hook_changes_nothing():
    s = session(browser_call(1, NAV))  # no hookEvent at all: the hook is not configured yet
    assert s["guard"]["readonly"] == "unobserved", s["guard"]
    assert s["readonly"] == "ok", "audit_actions' verdict must stand until QA_GUARD_EXPECTED is on"


@test
def with_the_guard_expected_a_missing_hook_is_unobserved():
    os.environ[EXPECTED_ENV] = "true"
    s = session(browser_call(1, NAV))
    assert s["readonly"] == "unobserved", s["readonly"]


@test
def with_the_guard_expected_full_coverage_is_ok():
    os.environ[EXPECTED_ENV] = "true"
    s = session(browser_call(1, NAV, ALLOW) + browser_call(2, {"type": "screenshot"}, ALLOW))
    assert s["guard"]["readonly"] == "ok" and s["readonly"] == "ok", (s["guard"], s["readonly"])


@test
def audit_actions_still_reddens_when_the_guard_allowed():
    s = session(browser_call(1, {"type": "click", "selector": "text=Delete"}, ALLOW))
    assert s["guard"]["readonly"] == "ok" and s["readonly"] == "violated", (s["guard"], s["readonly"])


@test
def a_session_that_never_reached_the_harness_records_no_guard():
    s = session(browser_call(1, NAV, ALLOW), preexisting_lease=True)
    assert s["lease"] == "busy", s.get("lease")
    assert "guard" not in s, s.get("guard")


@test
def recorded_guard_carries_no_tool_input():
    s = session(browser_call(1, {"type": "type", "selector": "#password", "text": MARKER}, ALLOW))
    assert MARKER not in json.dumps(s["guard"]), s["guard"]


# ── vendoring and packaging ──────────────────────────────────────────────────────────────────────
@test
def vendored_guard_files_match_their_pins_and_upstream():
    pins = json.loads((WT / "src/VENDORED.json").read_text())["guard"]["files"]
    assert set(pins) == set(UPSTREAM), set(pins)
    for name, meta in pins.items():
        got = hashlib.sha256((WT / "src" / name).read_bytes()).hexdigest()
        assert got == meta["sha256"] == UPSTREAM[name], f"{name}: {got[:12]} pinned {meta['sha256'][:12]}"


@test
def lease_pins_stay_a_separate_block():
    pins = json.loads((WT / "src/VENDORED.json").read_text())
    assert not set(pins["files"]) & set(UPSTREAM), "guard files belong in the 'guard' block, not the lease 'files'"


@test
def deploy_paths_ship_the_runtime_guard_spec():
    sh = (WT / "deploy/deploy.sh").read_text()
    stack = (WT / "cdk/stack.py").read_text()
    f = G["spec"]
    assert re.search(rf'cp [^\n]*src/{re.escape(f)}[^\n]*"\$BUILD/"', sh), f"deploy.sh does not ship {f}"
    assert re.search(rf'cp [^\n]*\b{re.escape(f)}\b[^\n]*/asset-output/', stack), f"cdk/stack.py bundling does not ship {f}"


w = max(len(n) for n, _, _ in results)
for n, r, why in results:
    print(f"  {r}  {n:<{w}}  {why}")
fails = sum(1 for _, r, _ in results if r == "FAIL")
print(f"{len(results) - fails}/{len(results)} passed")
sys.exit(1 if fails else 0)
