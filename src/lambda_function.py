#!/usr/bin/env python3
"""
Agent CI/CD Admin — AWS Lambda handler (Function URL).

Deployed, always-on version of the local admin backend. One Lambda serves both the
dashboard HTML (GET /) and the JSON API (GET/POST /api/*) via a Function URL, so there's
no server to keep running on a laptop.

Differences from the local app.py, and why:
  - GitHub data comes from the PUBLIC REST API via urllib (no `gh` CLI on Lambda).
  - The QA fan-out uses Lambda self async-invoke instead of background threads (a Lambda
    freezes after it responds, so threads wouldn't finish). Each async invocation runs one
    QA session; state is kept in a DynamoDB table so the dashboard can poll it.
  - Mutations (skill edit, fan-out) require a bearer token (env ADMIN_TOKEN) so a public
    Function URL can't be driven by strangers. Read endpoints are open (demo-friendly).

Env: AWS_REGION, ACCOUNT_ID (optional), TARGET_REPO, QA_BUCKET, UI_HARNESS, BUGFIX_HARNESS,
     TARGET_URL, ADMIN_TOKEN, RUNS_TABLE, GITHUB_TOKEN (optional, lifts rate limit).
"""
import json
import os
import re
import secrets
import urllib.request
import urllib.error
from datetime import datetime, timezone

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID") or boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
TARGET_REPO = os.environ.get("TARGET_REPO", "")  # e.g. "org/repo" — the repo your QA workflow runs in
QA_BUCKET = os.environ.get("QA_BUCKET", "")  # S3 bucket holding QA reports + screenshots
UI_HARNESS = os.environ.get("UI_HARNESS", "")  # your UI-test harness id, e.g. MyUiTestHarness-AbC123
BUGFIX_HARNESS = os.environ.get("BUGFIX_HARNESS", "")  # your bug-fix harness id
UI_HARNESS_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:harness/{UI_HARNESS}"
TARGET_URL = os.environ.get("TARGET_URL", "")  # the site your QA agent tests
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
RUNS_TABLE = os.environ.get("RUNS_TABLE", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SECRET_ID = os.environ.get("LOGIN_SECRET_ID", "")  # Secrets Manager secret with test-site login creds
SELF_FUNCTION = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")

COGNITO_POOL_ID = os.environ.get("COGNITO_POOL_ID", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")

ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
data = boto3.client("bedrock-agentcore", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
cognito = boto3.client("cognito-idp", region_name=REGION)
_INTRO_CACHE = None  # warm-container cache for intro/player.html (S3 fetch once per container)
_ddb = boto3.resource("dynamodb", region_name=REGION)
runs_tbl = _ddb.Table(RUNS_TABLE) if RUNS_TABLE else None


# ── GitHub public REST (no gh CLI) ────────────────────────────────────────────
def _gh_get(path: str):
    url = f"https://api.github.com/repos/{TARGET_REPO}/{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "agent-admin"})
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def pipeline_status():
    runs_j = _gh_get("actions/runs?per_page=8") or {}
    prs_j = _gh_get("pulls?state=open&per_page=10") or []
    runs = [{"name": r["name"], "conclusion": r.get("conclusion"), "status": r.get("status"),
             "event": r.get("event"), "headBranch": r.get("head_branch"),
             "createdAt": r.get("created_at")} for r in runs_j.get("workflow_runs", [])]
    prs = [{"number": p["number"], "title": p["title"], "headRefName": p["head"]["ref"],
            "createdAt": p["created_at"]} for p in (prs_j if isinstance(prs_j, list) else [])]
    return {"runs": runs, "prs": prs}


# ── live pipeline diagram (/api/pipeline) ─────────────────────────────────────
PIPE_WORKFLOW = os.environ.get("PIPE_WORKFLOW", "ui-qa-agent.yml")
MAX_FIX_ROUNDS = int(os.environ.get("MAX_FIX_ROUNDS", "3"))
# logical stage -> exact GitHub step names (from .github/workflows/ui-qa-agent.yml)
PIPE_STAGES = [
    ("setup", "Checkout / Setup", ["Run actions/checkout@v4", "Run actions/setup-python@v5",
                                   "Run pip install boto3", "Run actions/setup-node@v4"]),
    ("auth", "OIDC auth", ["Run aws-actions/configure-aws-credentials@v4"]),
    ("deploy", "Deploy frontend", ["Deploy frontend for this branch"]),
    ("qa", "UI QA Agent", ["Run UI QA Agent"]),
    ("rounds", "Count fix rounds", ["Count prior fix rounds"]),
    ("bugfix", "Bug-Fix Agent", ["Run Bug-Fix Agent and push fix"]),
    ("comment", "PR comment", ["Comment on PR"]),
    ("gate", "Pass / fail check", ["Fail check if unresolved blocking findings remain"]),
]


def pipeline_detail():
    """Latest run of the QA workflow + its steps mapped onto the logical pipeline stages
    that the animated diagram renders. Read-only; public GitHub API."""
    runs_j = _gh_get(f"actions/workflows/{PIPE_WORKFLOW}/runs?per_page=1") or {}
    runs = runs_j.get("workflow_runs", [])
    if not runs:
        return {"run": None, "stages": [], "maxFixRounds": MAX_FIX_ROUNDS, "fixRounds": 0}
    r = runs[0]
    run = {"id": r["id"], "number": r.get("run_number"), "status": r.get("status"),
           "conclusion": r.get("conclusion"), "event": r.get("event"),
           "branch": r.get("head_branch"), "createdAt": r.get("created_at"),
           "updatedAt": r.get("updated_at"), "url": r.get("html_url")}
    steps = []
    for j in (_gh_get(f"actions/runs/{r['id']}/jobs") or {}).get("jobs", []):
        steps += j.get("steps", [])
    by_name = {s.get("name"): s for s in steps}
    stages = []
    for key, label, names in PIPE_STAGES:
        ss = [by_name[n] for n in names if n in by_name]
        if not ss:  # run still queued — steps not reported yet
            stages.append({"key": key, "label": label, "status": "pending", "conclusion": None,
                           "started_at": None, "completed_at": None})
            continue
        concs = [s.get("conclusion") for s in ss]
        stats = [s.get("status") for s in ss]
        if any(st == "in_progress" for st in stats):
            status, conc = "in_progress", None
        elif all(st == "completed" for st in stats):
            status = "completed"
            conc = ("failure" if "failure" in concs else
                    "cancelled" if "cancelled" in concs else
                    "skipped" if all(c == "skipped" for c in concs) else "success")
        else:
            status, conc = "pending", None
        starts = [s.get("started_at") for s in ss if s.get("started_at")]
        ends = [s.get("completed_at") for s in ss if s.get("completed_at")]
        stages.append({"key": key, "label": label, "status": status, "conclusion": conc,
                       "started_at": min(starts) if starts else None,
                       "completed_at": max(ends) if (ends and status == "completed") else None})
    # fix rounds used on this branch: count auto-fix marker commits (loop-guard telemetry)
    fix_rounds = 0
    try:
        commits = _gh_get(f"commits?sha={run['branch']}&per_page=30") or []
        fix_rounds = sum(1 for c in commits
                         if "chore(agent): auto-fix round" in (c.get("commit", {}).get("message", "")))
    except Exception:
        pass
    return {"run": run, "stages": stages, "fixRounds": fix_rounds, "maxFixRounds": MAX_FIX_ROUNDS}


# ── AgentCore data ────────────────────────────────────────────────────────────
def list_runtimes():
    out = []
    for r in ctl.list_agent_runtimes().get("agentRuntimes", []):
        out.append({"name": r["agentRuntimeName"], "id": r.get("agentRuntimeId"),
                    "status": r["status"], "version": r.get("agentRuntimeVersion"),
                    "managed": r["agentRuntimeName"].startswith("harness_"),
                    "updatedAt": str(r.get("lastUpdatedAt", ""))})
    return out


def list_harnesses():
    return [{"name": h["harnessName"], "id": h["harnessId"], "status": h["status"],
             "version": h.get("harnessVersion")}
            for h in ctl.list_harnesses().get("harnesses", [])]


def get_harness_detail(hid):
    h = ctl.get_harness(harnessId=hid)["harness"]
    return {"name": h["harnessName"], "id": h["harnessId"], "status": h["status"],
            "model": h.get("model", {}).get("bedrockModelConfig", {}).get("modelId"),
            "skills": h.get("skills", []), "allowedTools": h.get("allowedTools"),
            "maxIterations": h.get("maxIterations"), "timeoutSeconds": h.get("timeoutSeconds")}


def _open_pr_numbers():
    """PR numbers still open on the target repo. None (not empty set) on API failure so the
    caller can tell 'GitHub unreachable' apart from 'no open PRs'."""
    prs = _gh_get("pulls?state=open&per_page=50")
    if not isinstance(prs, list):
        return None
    return {p.get("number") for p in prs}


def latest_qa_run(prefix=None):
    """Serve the NEWEST *relevant* QA report. With no explicit prefix, scan top-level prefixes
    (run-latest/, pr-<n>/, ...) and pick the one whose report was modified last — but skip
    pr-<n>/ prefixes whose PR is no longer open: once a PR is approved+merged (or closed) its
    findings are history, and the operator's focus belongs on PRs that still need attention.
    Merged-PR reports stay in S3 and remain reachable via an explicit ?prefix=pr-<n>."""
    report, shots, updated = None, [], None
    if not prefix:
        best = None
        open_prs = _open_pr_numbers()
        try:
            for cp in s3.list_objects_v2(Bucket=QA_BUCKET, Delimiter="/").get("CommonPrefixes", []):
                cand = cp["Prefix"].rstrip("/")
                m = re.fullmatch(r"pr-(\d+)", cand)
                # Skip closed/merged PRs; if GitHub is unreachable (open_prs None), keep
                # everything rather than blanking the panel on a rate-limit blip.
                if m and open_prs is not None and int(m.group(1)) not in open_prs:
                    continue
                try:
                    h = s3.head_object(Bucket=QA_BUCKET, Key=f"{cand}/test-report-latest.json")
                    if best is None or h["LastModified"] > best[1]:
                        best = (cand, h["LastModified"])
                except Exception:
                    continue
        except Exception:
            pass
        prefix = best[0] if best else "run-latest"
    try:
        obj = s3.get_object(Bucket=QA_BUCKET, Key=f"{prefix}/test-report-latest.json")
        report = json.loads(obj["Body"].read())
        updated = str(obj.get("LastModified", ""))
    except Exception:
        pass
    try:
        for o in s3.list_objects_v2(Bucket=QA_BUCKET, Prefix=f"{prefix}/screenshots/").get("Contents", []):
            shots.append({"key": o["Key"],
                          "url": s3.generate_presigned_url("get_object",
                                 Params={"Bucket": QA_BUCKET, "Key": o["Key"]}, ExpiresIn=3600)})
    except Exception:
        pass
    return {"report": report, "screenshots": shots, "prefix": prefix, "updated": updated}


def list_runs():
    if not runs_tbl:
        return []
    try:
        items = [i for i in runs_tbl.scan(Limit=25).get("Items", [])
                 if not str(i.get("id", "")).startswith("opt-")]
        items.sort(key=lambda x: x.get("startedAt", ""), reverse=True)
        return items[:10]
    except Exception:
        return []


# ── mutations ─────────────────────────────────────────────────────────────────
def update_skill(hid, skill):
    ctl.update_harness(harnessId=hid, skills=[skill], clientToken=secrets.token_hex(20))
    return {"ok": True, "skills": [skill]}


def update_limits(hid, max_iterations=None, timeout=None):
    kwargs = {"harnessId": hid, "clientToken": secrets.token_hex(20)}
    if max_iterations is not None:
        kwargs["maxIterations"] = int(max_iterations)
    if timeout is not None:
        kwargs["timeoutSeconds"] = int(timeout)
    ctl.update_harness(**kwargs)
    return {"ok": True}


def start_qa_fanout(concurrency, url, now_iso):
    """Fan out N QA sessions by async-invoking THIS Lambda N times (mode=session)."""
    concurrency = max(1, min(int(concurrency), 10))
    batch_id = secrets.token_hex(6)
    batch = {"id": batch_id, "startedAt": now_iso, "concurrency": concurrency,
             "url": url, "sessions": [], "status": "running"}
    if runs_tbl:
        runs_tbl.put_item(Item=batch)
    if SELF_FUNCTION:
        for i in range(concurrency):
            lam.invoke(FunctionName=SELF_FUNCTION, InvocationType="Event",
                       Payload=json.dumps({"mode": "session", "batch_id": batch_id,
                                           "i": i, "url": url}).encode())
    return batch


def run_one_session(batch_id, i, url):
    """Async worker: one QA smoke session against the target; record result to DynamoDB."""
    sid = f"admin-qa-{batch_id}-{i}-{secrets.token_hex(8)}"
    status = "done"
    try:
        resp = data.invoke_harness(
            harnessArn=UI_HARNESS_ARN, runtimeSessionId=sid, actorId="admin-panel",
            messages=[{"role": "user", "content": [{"text":
                f"Smoke test {url}: log in (creds from Secrets Manager {SECRET_ID}), visit each "
                f"page once, report one-line PASS/FAIL per page. Session #{i}."}]}])
        for _ in resp["stream"]:
            pass
    except Exception as e:
        status = f"error: {str(e)[:120]}"
    if runs_tbl:
        try:
            runs_tbl.update_item(
                Key={"id": batch_id},
                UpdateExpression="SET sessions = list_append(if_not_exists(sessions, :e), :s)",
                ExpressionAttributeValues={":s": [{"i": i, "session": sid, "status": status}], ":e": []})
        except Exception:
            pass



# ── observability / evaluations / optimizations ───────────────────────────────
cw = boto3.client("cloudwatch", region_name=REGION)
logsc = boto3.client("logs", region_name=REGION)
brt = boto3.client("bedrock-runtime", region_name=REGION)
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "global.anthropic.claude-sonnet-4-6")
EVAL_RESULTS_LG_PREFIX = "/aws/bedrock-agentcore/evaluations/results/"


def observability(hours=24):
    """Per-harness runtime metrics from the AWS/Bedrock-AgentCore service namespace, plus
    account-level gen_ai token usage EMF metrics emitted into the DEFAULT log groups."""
    from datetime import timedelta
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=int(hours))
    watched = {f"harness_{h.split('-')[0]}" for h in (UI_HARNESS, BUGFIX_HARNESS) if h}
    runtimes = {r["agentRuntimeName"]: r for r in ctl.list_agent_runtimes().get("agentRuntimes", [])
                if r["agentRuntimeName"] in watched}
    queries, order = [], []
    for i, (rname, r) in enumerate(runtimes.items()):
        arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/{r['agentRuntimeId']}"
        dims = [{"Name": "Name", "Value": f"{rname}::DEFAULT"},
                {"Name": "Operation", "Value": "InvokeAgentRuntime"},
                {"Name": "Resource", "Value": arn}]
        for j, (metric, stat) in enumerate([("Invocations", "Sum"), ("Latency", "Average"),
                                            ("Sessions", "Sum"), ("SystemErrors", "Sum"),
                                            ("UserErrors", "Sum"), ("Throttles", "Sum")]):
            queries.append({"Id": f"m{i}_{j}", "MetricStat": {
                "Metric": {"Namespace": "AWS/Bedrock-AgentCore", "MetricName": metric, "Dimensions": dims},
                "Period": 3600 * int(hours), "Stat": stat}, "ReturnData": True})
            order.append((rname, metric, stat))
    # token usage: EMF gen_ai.client.token.usage — dims vary, list then sum input/output
    tok_metrics = cw.list_metrics(Namespace="bedrock-agentcore",
                                  MetricName="gen_ai.client.token.usage").get("Metrics", [])
    for k, m in enumerate(tok_metrics):
        ttype = next((d["Value"] for d in m["Dimensions"] if d["Name"] == "gen_ai.token.type"), "?")
        queries.append({"Id": f"t{k}", "MetricStat": {
            "Metric": m, "Period": 3600 * int(hours), "Stat": "Sum"}, "ReturnData": True})
        order.append(("_tokens", ttype, "Sum"))
    out = {rn: {} for rn in runtimes}
    tokens = {"input": 0, "output": 0}
    if queries:
        res = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
        for q, (owner, metric, stat) in zip(res["MetricDataResults"], order):
            val = sum(q["Values"]) if stat == "Sum" else (sum(q["Values"]) / len(q["Values"]) if q["Values"] else 0)
            if owner == "_tokens":
                tokens[metric] = tokens.get(metric, 0) + val
            else:
                out[owner][metric] = round(val, 1)
    for rn in out:
        inv = out[rn].get("Invocations", 0)
        errs = out[rn].get("SystemErrors", 0) + out[rn].get("UserErrors", 0)
        out[rn]["ErrorRatePct"] = round(100.0 * errs / inv, 1) if inv else 0.0
    # daily invocation + latency series for the trend chart (1 bucket/day)
    dq, dorder = [], []
    for i, (rname, r) in enumerate(runtimes.items()):
        arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/{r['agentRuntimeId']}"
        dims = [{"Name": "Name", "Value": f"{rname}::DEFAULT"},
                {"Name": "Operation", "Value": "InvokeAgentRuntime"},
                {"Name": "Resource", "Value": arn}]
        dq.append({"Id": f"d{i}", "MetricStat": {
            "Metric": {"Namespace": "AWS/Bedrock-AgentCore", "MetricName": "Invocations",
                       "Dimensions": dims}, "Period": 86400, "Stat": "Sum"}, "ReturnData": True})
        dorder.append(rname)
    if dq:
        dres = cw.get_metric_data(MetricDataQueries=dq, StartTime=start, EndTime=end,
                                  ScanBy="TimestampAscending")
        for q, rname in zip(dres["MetricDataResults"], dorder):
            out[rname]["daily"] = [
                {"d": t.strftime("%m-%d"), "v": v}
                for t, v in zip(q["Timestamps"], q["Values"])]
    return {"windowHours": int(hours), "harnesses": out,
            "tokens": {k: int(v) for k, v in tokens.items()},
            "tokenScope": "account-wide agent runtimes (EMF gen_ai.client.token.usage)",
            "source": "AWS/Bedrock-AgentCore service metrics + DEFAULT log group EMF"}


def evaluations():
    """Online evaluation configs (verified SDK ops) + recent scores from the results log group."""
    cfgs = ctl.list_online_evaluation_configs().get("onlineEvaluationConfigs", [])
    out = []
    for c0 in cfgs:
        cid = c0.get("onlineEvaluationConfigId")
        item = {"id": cid, "name": c0.get("onlineEvaluationConfigName"),
                "status": str(c0.get("status")), "executionStatus": str(c0.get("executionStatus", ""))}
        try:
            d = ctl.get_online_evaluation_config(onlineEvaluationConfigId=cid)
            cfg = d.get("onlineEvaluationConfig", d)
            item["evaluators"] = [e.get("evaluatorId") for e in cfg.get("evaluators", [])]
            item["insights"] = [i.get("insightId", "").replace("Builtin.Insight.", "")
                                for i in cfg.get("insights", [])]
            item["frequencies"] = (cfg.get("clusteringConfig") or {}).get("frequencies", [])
            item["sampling"] = cfg.get("rule", {}).get("samplingConfig", {}).get("samplingPercentage")
            item["logGroups"] = cfg.get("dataSourceConfig", {}).get("cloudWatchLogs", {}).get("logGroupNames", [])
            item["status"] = str(cfg.get("status", item["status"]))
        except Exception as e:
            item["detailError"] = str(e)[:150]
        # recent scores from results log group (honest: empty until evaluator has scored traffic)
        scores = []
        try:
            ev = logsc.filter_log_events(logGroupName=EVAL_RESULTS_LG_PREFIX + cid, limit=50)
            for e0 in ev.get("events", []):
                try:
                    j = json.loads(e0["message"])
                    scores.append(j)
                except Exception:
                    pass
            if not scores:
                item["scoresNote"] = "evaluator ACTIVE — awaiting scored traffic (runs on next harness invocation)"
        except logsc.exceptions.ResourceNotFoundException:
            item["scoresNote"] = "no results log group yet — evaluator has not scored any traffic"
        except Exception as e:
            item["scoresNote"] = f"results read error: {str(e)[:120]}"
        item["recentScores"] = scores[-20:]
        # aggregate per evaluator — tolerant extraction, the preview result schema may drift
        agg = {}
        for j in scores:
            att = j.get("attributes") or {}
            name = (att.get("gen_ai.evaluation.name") or j.get("evaluatorId") or j.get("evaluator")
                    or j.get("evaluatorName") or j.get("metricName") or "")
            if name == "gen_ai.evaluation.result":
                name = att.get("gen_ai.evaluation.name", "")
            val = att.get("gen_ai.evaluation.score.value")
            val = float(val) if isinstance(val, (int, float)) else None
            for k in ("score", "value", "result", "metricValue"):
                if val is not None:
                    break
                v = j.get(k)
                if isinstance(v, (int, float)):
                    val = float(v)
                    break
                if isinstance(v, dict):
                    for kk in ("value", "score"):
                        if isinstance(v.get(kk), (int, float)):
                            val = float(v[kk])
                            break
                if val is not None:
                    break
            if name and val is not None:
                a = agg.setdefault(str(name).replace("Builtin.", ""), {"n": 0, "sum": 0.0})
                a["n"] += 1
                a["sum"] += val
        item["scoreStats"] = {k: {"count": v["n"], "avg": round(v["sum"] / v["n"], 3)}
                              for k, v in agg.items() if v["n"]}
        out.append(item)
    return {"configs": out,
            "note": "Online evaluations score live harness traces (preview). Scores also surface in "
                    "CloudWatch → AgentCore Observability."}


def list_batch_evaluations():
    """Batch evaluations via the DATA-plane SDK (StartBatchEvaluation etc. — these ops
    live on bedrock-agentcore, NOT the control plane)."""
    try:
        out = []
        for be in data.list_batch_evaluations().get("batchEvaluations", [])[:10]:
            if str(be.get("batchEvaluationName", "")).startswith(("ui_qa_insights_", "ui_qa_ins_rpt")):
                continue
            item = {"id": be.get("batchEvaluationId"), "name": be.get("batchEvaluationName"),
                    "status": str(be.get("status")), "createdAt": str(be.get("createdAt", ""))}
            try:
                d = data.get_batch_evaluation(batchEvaluationId=item["id"])
                bd = d.get("batchEvaluation", d)
                res = bd.get("evaluationResults", {})
                item["sessions"] = {"total": res.get("totalNumberOfSessions", 0),
                                    "completed": res.get("numberOfSessionsCompleted", 0),
                                    "failed": res.get("numberOfSessionsFailed", 0)}
                item["evaluatorSummaries"] = [
                    {"evaluator": e0.get("evaluatorId", "").replace("Builtin.", ""),
                     "avg": (e0.get("statistics") or {}).get("averageScore"),
                     "evaluated": e0.get("totalEvaluated", 0), "failed": e0.get("totalFailed", 0)}
                    for e0 in res.get("evaluatorSummaries", [])]
            except Exception:
                pass
            out.append(item)
        out.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        return out
    except Exception as e:
        return {"error": str(e)[:200]}


def start_insights_report():
    """On-demand insights report ("Create custom report"): StartBatchEvaluation with
    insights= over recent sampled sessions. Daily scheduled reports also appear in the console."""
    sids = []
    SPANS_SINCE = os.environ.get("SPANS_SINCE", "")  # ISO ts when OTEL_TRACES_SAMPLER=always_on was enabled; sessions before it can never score
    if runs_tbl:
        items = [i for i in runs_tbl.scan(Limit=25).get("Items", [])
                 if not str(i.get("id", "")).startswith("opt-")
                 and (not SPANS_SINCE or str(i.get("startedAt", "")) >= SPANS_SINCE)]
        items.sort(key=lambda x: x.get("startedAt", ""), reverse=True)
        for b in items[:3]:
            sids += [s["session"] for s in b.get("sessions", []) if s.get("status") == "done"]
    if not sids:
        return {"error": "no sampled sessions to analyze — run a QA fan-out first"}
    rt = next((r for r in ctl.list_agent_runtimes().get("agentRuntimes", [])
               if r["agentRuntimeName"] == f"harness_{UI_HARNESS.split(chr(45))[0]}"), None)
    lg = f"/aws/bedrock-agentcore/runtimes/{rt['agentRuntimeId']}-DEFAULT" if rt else ""
    r = data.start_batch_evaluation(
        batchEvaluationName="ui_qa_insights_" + secrets.token_hex(3),
        insights=[{"insightId": "Builtin.Insight.FailureAnalysis"},
                  {"insightId": "Builtin.Insight.UserIntent"},
                  {"insightId": "Builtin.Insight.ExecutionSummary"}],
        dataSourceConfig={"cloudWatchLogs": {
            "serviceNames": [f"harness_{UI_HARNESS.split(chr(45))[0]}.DEFAULT"],
            "logGroupNames": [lg],
            "filterConfig": {"sessionIds": sids[:20]}}},
        clientToken=secrets.token_hex(20),
        description="On-demand insights report from the admin dashboard")
    return {"id": r.get("batchEvaluationId"), "status": str(r.get("status")), "sessions": len(sids[:20])}


def get_insights_report(bid):
    d = data.get_batch_evaluation(batchEvaluationId=bid)
    be = d.get("batchEvaluation", d)
    return {"id": bid, "status": str(be.get("status")),
            "failures": (be.get("failureAnalysisResult") or {}).get("failures", []),
            "intents": (be.get("userIntentResult") or {}).get("userIntents", []),
            "summaries": (be.get("executionSummaryResult") or {}).get("executionSummaries", [])}


def list_insights_reports():
    out = []
    try:
        for be in data.list_batch_evaluations().get("batchEvaluations", []):
            if str(be.get("batchEvaluationName", "")).startswith("ui_qa_insights_") or                str(be.get("batchEvaluationName", "")).startswith("ui_qa_ins_rpt"):
                out.append({"id": be.get("batchEvaluationId"),
                            "name": be.get("batchEvaluationName"),
                            "status": str(be.get("status")),
                            "createdAt": str(be.get("createdAt", ""))})
        out.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    except Exception:
        pass
    return out[:5]


def start_batch_eval():
    """Score the most recent QA fan-out sessions offline (minutes, not the online
    evaluator's async cadence). Requires OTEL_TRACES_SAMPLER=always_on on the harness."""
    sids = []
    # sessions before tracing was enabled (OTEL_TRACES_SAMPLER=always_on, 2026-07-23) have no
    # span documents and always fail evaluation — exclude them
    SPANS_SINCE = os.environ.get("SPANS_SINCE", "")  # ISO ts when OTEL_TRACES_SAMPLER=always_on was enabled; sessions before it can never score
    if runs_tbl:
        items = [i for i in runs_tbl.scan(Limit=25).get("Items", [])
                 if not str(i.get("id", "")).startswith("opt-")
                 and (not SPANS_SINCE or str(i.get("startedAt", "")) >= SPANS_SINCE)]
        items.sort(key=lambda x: x.get("startedAt", ""), reverse=True)
        for b in items[:3]:
            sids += [s["session"] for s in b.get("sessions", []) if s.get("status") == "done"]
    if not sids:
        return {"error": "no completed fan-out sessions to score — run a QA fan-out first"}
    rt = next((r for r in ctl.list_agent_runtimes().get("agentRuntimes", [])
               if r["agentRuntimeName"] == f"harness_{UI_HARNESS.split(chr(45))[0]}"), None)
    lg = f"/aws/bedrock-agentcore/runtimes/{rt['agentRuntimeId']}-DEFAULT" if rt else ""
    r = data.start_batch_evaluation(
        batchEvaluationName="ui_qa_dash_" + secrets.token_hex(3),
        evaluators=[{"evaluatorId": "Builtin.Correctness"},
                    {"evaluatorId": "Builtin.GoalSuccessRate"},
                    {"evaluatorId": "Builtin.ToolSelectionAccuracy"}],
        dataSourceConfig={"cloudWatchLogs": {
            "serviceNames": [f"harness_{UI_HARNESS.split(chr(45))[0]}.DEFAULT"],
            "logGroupNames": [lg],
            "filterConfig": {"sessionIds": sids[:20]}}},
        clientToken=secrets.token_hex(20),
        description="Started from the admin dashboard")
    return {"id": r.get("batchEvaluationId"), "status": str(r.get("status")), "sessions": len(sids[:20])}


def list_native_recommendations():
    """AWS-native Optimizations Recommendations (data-plane SDK — StartRecommendation etc.)."""
    try:
        out = []
        for rec in data.list_recommendations().get("recommendationSummaries", [])[:10]:
            item = {"id": rec.get("recommendationId"), "name": rec.get("name"),
                    "status": str(rec.get("status")), "type": rec.get("type"),
                    "createdAt": str(rec.get("createdAt", ""))}
            if item["status"] == "COMPLETED":
                try:
                    d = data.get_recommendation(recommendationId=item["id"])
                    rr = d.get("recommendation", d).get("recommendationResult", {})
                    spr = rr.get("systemPromptRecommendationResult", {})
                    item["recommendedPrompt"] = spr.get("recommendedSystemPrompt", "")
                    item["explanation"] = (spr.get("explanation") or "")[:400]
                except Exception:
                    pass
            out.append(item)
        out.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        return out
    except Exception as e:
        return {"error": str(e)[:200]}


def start_native_recommendation(now_iso):
    """Kick off an AWS-native system-prompt recommendation from the last 12h of traces."""
    from datetime import timedelta
    h = ctl.get_harness(harnessId=UI_HARNESS)["harness"]
    cur = (h.get("systemPrompt") or [{}])[0].get("text", "")
    rt = next((r for r in ctl.list_agent_runtimes().get("agentRuntimes", [])
               if r["agentRuntimeName"] == f"harness_{UI_HARNESS.split(chr(45))[0]}"), None)
    lg_arn = (f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:"
              f"/aws/bedrock-agentcore/runtimes/{rt['agentRuntimeId']}-DEFAULT")
    end = datetime.now(timezone.utc)
    r = data.start_recommendation(
        name="ui_qa_rec_" + secrets.token_hex(3),
        type="SYSTEM_PROMPT_RECOMMENDATION",     # enum, not "SYSTEM_PROMPT"
        recommendationConfig={"systemPromptRecommendationConfig": {
            "systemPrompt": {"text": cur},
            "agentTraces": {"cloudwatchLogs": {
                "logGroupArns": [lg_arn],
                "serviceNames": [f"harness_{UI_HARNESS.split(chr(45))[0]}.DEFAULT"],
                "startTime": end - __import__("datetime").timedelta(hours=12), "endTime": end}},
            "evaluationConfig": {"evaluators": [   # max ONE evaluator
                {"evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate"}]}}},
        clientToken=secrets.token_hex(20))
    return {"id": r.get("recommendationId"), "status": str(r.get("status"))}


def apply_native_recommendation(rec_id, now_iso):
    d = data.get_recommendation(recommendationId=rec_id)
    rec = d.get("recommendation", d)
    spr = rec.get("recommendationResult", {}).get("systemPromptRecommendationResult", {})
    prompt = spr.get("recommendedSystemPrompt", "")
    if not prompt:
        return {"error": "recommendation has no completed prompt"}
    ctl.update_harness(harnessId=UI_HARNESS, systemPrompt=[{"text": prompt}],
                       clientToken=secrets.token_hex(20))
    return {"ok": True, "applied": rec_id}


def list_optimizations():
    if not runs_tbl:
        return []
    try:
        items = [i for i in runs_tbl.scan(Limit=50).get("Items", []) if str(i.get("id", "")).startswith("opt-")]
        items.sort(key=lambda x: x.get("startedAt", ""), reverse=True)
        return items[:10]
    except Exception:
        return []


def enqueue_optimization(now_iso):
    """API Gateway caps integrations at 30s; Bedrock generation takes longer. Enqueue a
    placeholder and self-invoke async (same pattern as the QA fan-out worker)."""
    opt_id = "opt-" + secrets.token_hex(5)
    if runs_tbl:
        runs_tbl.put_item(Item={"id": opt_id, "startedAt": now_iso, "status": "generating",
                                "harnessId": UI_HARNESS})
    if SELF_FUNCTION:
        lam.invoke(FunctionName=SELF_FUNCTION, InvocationType="Event",
                   Payload=json.dumps({"mode": "optimize", "opt_id": opt_id,
                                       "now_iso": now_iso}).encode())
    return {"id": opt_id, "status": "generating",
            "note": "generation runs async (~30s); refresh the Optimizations panel"}


def generate_optimization(now_iso, opt_id=None):
    """Honest Optimizations flow (SDK has ZERO optimization ops — console-only preview):
    use QA findings + eval state + current system prompt to draft an improved prompt via
    Bedrock, store for human review, apply through the existing UpdateHarness path."""
    h = ctl.get_harness(harnessId=UI_HARNESS)["harness"]
    cur = h.get("systemPrompt") or []
    cur_text = cur[0].get("text", "") if cur else ""
    qa = latest_qa_run().get("report") or {}
    findings = qa.get("findings", [])[:10]
    evs = evaluations()["configs"]
    ev_summary = "; ".join(f"{e['name']}={e['status']} scores={len(e.get('recentScores', []))}" for e in evs) or "none"
    prompt = (
        "You are optimizing the system prompt of a UI-testing agent (AgentCore harness).\n"
        f"CURRENT SYSTEM PROMPT:\n{cur_text}\n\n"
        f"RECENT QA FINDINGS (what it caught — JSON): {json.dumps(findings, default=str)[:3000]}\n"
        f"ONLINE EVALUATIONS: {ev_summary}\n\n"
        "Known weaknesses: (1) the agent sometimes ends its final answer in prose instead of the required "
        "strict JSON report, breaking the CI parser; (2) finding field names drift between runs "
        "(title/observed vs summary/evidence).\n"
        "Propose ONE improved system prompt that keeps current strengths, enforces a strict JSON output "
        "contract with fixed field names (id, page, severity, summary, evidence, suspected_source), and "
        "improves evidence quality. Reply as JSON: "
        '{"proposed_prompt": "...", "rationale": "...", "expected_improvements": ["..."]}')
    resp = brt.converse(modelId=JUDGE_MODEL,
                        messages=[{"role": "user", "content": [{"text": prompt}]}],
                        inferenceConfig={"maxTokens": 4000, "temperature": 0.2})
    txt = resp["output"]["message"]["content"][0]["text"].strip()
    # strip markdown code fences the model sometimes wraps around JSON
    if txt.startswith("```"):
        txt = txt.split("\n", 1)[1] if "\n" in txt else txt
        if txt.rstrip().endswith("```"):
            txt = txt.rstrip()[:-3]
    try:
        start = txt.index("{"); end = txt.rindex("}") + 1
        rec = json.loads(txt[start:end])
    except Exception:
        rec = {"proposed_prompt": txt, "rationale": "model returned non-JSON; raw text kept", "expected_improvements": []}
    item = {"id": opt_id or ("opt-" + secrets.token_hex(5)), "startedAt": now_iso, "status": "proposed",
            "harnessId": UI_HARNESS, "currentPrompt": cur_text,
            "proposedPrompt": rec.get("proposed_prompt", ""), "rationale": rec.get("rationale", ""),
            "expectedImprovements": rec.get("expected_improvements", []), "model": JUDGE_MODEL}
    if runs_tbl:
        runs_tbl.put_item(Item=item)
    return item


def apply_optimization(opt_id, now_iso):
    if not runs_tbl:
        return {"error": "no runs table"}
    item = runs_tbl.get_item(Key={"id": opt_id}).get("Item")
    if not item or not item.get("proposedPrompt"):
        return {"error": "recommendation not found"}
    ctl.update_harness(harnessId=item.get("harnessId", UI_HARNESS),
                       systemPrompt=[{"text": item["proposedPrompt"]}],
                       clientToken=secrets.token_hex(20))
    runs_tbl.update_item(Key={"id": opt_id},
                         UpdateExpression="SET #s = :s, appliedAt = :t",
                         ExpressionAttributeNames={"#s": "status"},
                         ExpressionAttributeValues={":s": "applied", ":t": now_iso})
    return {"ok": True, "applied": opt_id, "harnessId": item.get("harnessId", UI_HARNESS)}


# ── HTTP glue (Function URL event → response) ─────────────────────────────────
def _resp(code, body, ctype="application/json"):
    return {"statusCode": code,
            "headers": {"content-type": ctype, "access-control-allow-origin": "*",
                        "access-control-allow-methods": "GET,POST,OPTIONS",
                        "access-control-allow-headers": "content-type,authorization"},
            "body": body if isinstance(body, str) else json.dumps(body, default=str)}


def cognito_login(username, password):
    """Exchange username/password for a Cognito access token (USER_PASSWORD_AUTH)."""
    if not COGNITO_CLIENT_ID:
        return {"error": "Cognito not configured"}
    try:
        r = cognito.initiate_auth(ClientId=COGNITO_CLIENT_ID, AuthFlow="USER_PASSWORD_AUTH",
                                  AuthParameters={"USERNAME": username, "PASSWORD": password})
        a = r["AuthenticationResult"]
        return {"accessToken": a["AccessToken"], "expiresIn": a["ExpiresIn"],
                "refreshToken": a.get("RefreshToken", "")}
    except cognito.exceptions.NotAuthorizedException:
        return {"error": "invalid username or password"}
    except Exception as e:
        return {"error": str(e)[:200]}


def _authed(headers):
    """Cognito access token in Authorization: Bearer (validated server-side via GetUser,
    which checks signature/expiry/revocation). x-admin-token remains as a
    break-glass fallback only while ADMIN_TOKEN is set."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    auth = h.get("authorization", "")
    if auth.startswith("Bearer ") and COGNITO_POOL_ID:
        try:
            cognito.get_user(AccessToken=auth[7:].strip())
            return True
        except Exception:
            return False
    tok = h.get("x-admin-token", "")
    return bool(ADMIN_TOKEN) and tok.strip() == ADMIN_TOKEN


def handler(event, context):
    # Async self-invocation path (fan-out worker)
    if isinstance(event, dict) and event.get("mode") == "session":
        run_one_session(event["batch_id"], event["i"], event.get("url", TARGET_URL))
        return {"ok": True}
    if isinstance(event, dict) and event.get("mode") == "optimize":
        try:
            generate_optimization(event.get("now_iso", ""), event.get("opt_id"))
        except Exception as e:
            if runs_tbl and event.get("opt_id"):
                runs_tbl.update_item(Key={"id": event["opt_id"]},
                    UpdateExpression="SET #s = :s, error_msg = :e",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": "error", ":e": str(e)[:300]})
        return {"ok": True}

    rc = (event.get("requestContext") or {}).get("http") or {}
    method = rc.get("method", "GET")
    path = rc.get("path", "/")
    headers = event.get("headers") or {}
    qs = event.get("queryStringParameters") or {}

    if method == "OPTIONS":
        return _resp(204, "")

    # Serve the dashboard
    if method == "GET" and path in ("/", "/index.html"):
        return _resp(200, FRONTEND_HTML, "text/html; charset=utf-8")

    # Introduction tab: multilingual use-case player. HTML lives in S3 (intro/player.html,
    # ~1MB — too big to inline next to FRONTEND_HTML); audio streams via presigned redirect
    # so the bucket stays private.
    if method == "GET" and path == "/intro":
        global _INTRO_CACHE
        try:
            if _INTRO_CACHE is None:
                _INTRO_CACHE = s3.get_object(Bucket=QA_BUCKET, Key="intro/player.html")["Body"].read().decode()
            return _resp(200, _INTRO_CACHE, "text/html; charset=utf-8")
        except Exception as e:
            return _resp(404, {"error": f"intro player not found: {e}"})
    if method == "GET" and path.startswith("/intro/audio/"):
        # /intro/audio/<lang>/<scene>.mp3 -> 302 to presigned S3 URL (1h)
        rel = path[len("/intro/"):]
        if not re.fullmatch(r"audio/[a-z]{2,3}/[a-z0-9-]+\.mp3", rel):
            return _resp(400, {"error": "bad audio path"})
        url = s3.generate_presigned_url("get_object",
              Params={"Bucket": QA_BUCKET, "Key": f"intro/{rel}"}, ExpiresIn=3600)
        return {"statusCode": 302, "headers": {"location": url,
                "access-control-allow-origin": "*", "cache-control": "private, max-age=3000"}, "body": ""}

    try:
        if method == "GET" and path == "/api/overview":
            return _resp(200, {"runtimes": list_runtimes(), "harnesses": list_harnesses(),
                               "pipeline": pipeline_status(), "runs": list_runs()})
        if method == "GET" and path == "/api/pipeline":
            return _resp(200, pipeline_detail())
        if method == "GET" and path == "/api/harness":
            return _resp(200, get_harness_detail(qs.get("id", UI_HARNESS)))
        if method == "GET" and path == "/api/qa-latest":
            return _resp(200, latest_qa_run(qs.get("prefix")))
        if method == "GET" and path == "/api/observability":
            return _resp(200, observability(qs.get("hours", 24)))
        if method == "GET" and path == "/api/evaluations":
            return _resp(200, evaluations())
        if method == "GET" and path == "/api/insights-reports":
            return _resp(200, {"reports": list_insights_reports()})
        if method == "GET" and path == "/api/insights-report":
            return _resp(200, get_insights_report(qs.get("id", "")))
        if method == "GET" and path == "/api/batch-evals":
            return _resp(200, {"batchEvaluations": list_batch_evaluations()})
        if method == "GET" and path == "/api/optimizations":
            return _resp(200, {"recommendations": list_optimizations(),
                               "native": list_native_recommendations()})

        # public login route
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        if method == "POST" and path == "/api/login":
            return _resp(200, cognito_login(str(body.get("username", "")), str(body.get("password", ""))))
        if method == "POST" and path == "/api/skill":
            if not _authed(headers):
                return _resp(401, {"error": "unauthorized"})
            return _resp(200, update_skill(body["id"], body["skill"]))
        if method == "POST" and path == "/api/limits":
            if not _authed(headers):
                return _resp(401, {"error": "unauthorized"})
            return _resp(200, update_limits(body["id"], body.get("maxIterations"), body.get("timeout")))
        if method == "POST" and path == "/api/insights-report":
            if not _authed(headers):
                return _resp(401, {"error": "unauthorized"})
            return _resp(200, start_insights_report())
        if method == "POST" and path == "/api/batch-eval":
            if not _authed(headers):
                return _resp(401, {"error": "unauthorized"})
            return _resp(200, start_batch_eval())
        if method == "POST" and path == "/api/native-rec":
            if not _authed(headers):
                return _resp(401, {"error": "unauthorized"})
            now = datetime.now(timezone.utc).isoformat()
            return _resp(200, start_native_recommendation(now))
        if method == "POST" and path == "/api/native-rec/apply":
            if not _authed(headers):
                return _resp(401, {"error": "unauthorized"})
            now = datetime.now(timezone.utc).isoformat()
            return _resp(200, apply_native_recommendation(body.get("id", ""), now))
        if method == "POST" and path == "/api/optimize":
            if not _authed(headers):
                return _resp(401, {"error": "unauthorized"})
            now = datetime.now(timezone.utc).isoformat()
            return _resp(200, enqueue_optimization(now))
        if method == "POST" and path == "/api/optimize/apply":
            if not _authed(headers):
                return _resp(401, {"error": "unauthorized"})
            now = datetime.now(timezone.utc).isoformat()
            return _resp(200, apply_optimization(body.get("id", ""), now))
        if method == "POST" and path == "/api/qa-run":
            if not _authed(headers):
                return _resp(401, {"error": "unauthorized"})
            now = datetime.now(timezone.utc).isoformat()
            return _resp(200, start_qa_fanout(body.get("concurrency", 1), body.get("url", TARGET_URL), now))
        return _resp(404, {"error": "not found", "path": path})
    except Exception as e:
        return _resp(500, {"error": str(e)})


# The dashboard HTML is injected at package time (see build).
FRONTEND_HTML = '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8" />\n<meta name="viewport" content="width=device-width, initial-scale=1" />\n<title>Agent CI/CD Admin</title>\n<style>\n  :root {\n    --bg:#0b1020; --panel:#141b33; --panel2:#1b2444; --line:#26305a;\n    --text:#e7ecff; --muted:#8592c0; --accent:#5b8cff; --green:#3ecf8e;\n    --amber:#f5b342; --red:#ff6b6b; --crit:#ff4d6d;\n  }\n  * { box-sizing:border-box; }\n  body { margin:0; font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;\n         background:var(--bg); color:var(--text); }\n  header { display:flex; align-items:center; gap:12px; padding:16px 24px;\n           border-bottom:1px solid var(--line); background:var(--panel); }\n  header .logo { width:28px; height:28px; border-radius:7px;\n                 background:linear-gradient(135deg,var(--accent),#9d5bff); }\n  header h1 { font-size:16px; margin:0; font-weight:650; }\n  header .sub { color:var(--muted); font-size:12px; }\n  header .spacer { flex:1; }\n  button { font:inherit; cursor:pointer; border:1px solid var(--line);\n           background:var(--panel2); color:var(--text); border-radius:8px; padding:7px 13px; }\n  button:hover { border-color:var(--accent); }\n  button.primary { background:var(--accent); border-color:var(--accent); color:#001; font-weight:600; }\n  .wrap { padding:20px 24px; display:grid; gap:16px;\n          grid-template-columns:repeat(12,1fr); max-width:1400px; margin:0 auto; }\n  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px; }\n  .card h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em;\n             color:var(--muted); margin:0 0 12px; font-weight:600; }\n  .col-4 { grid-column:span 4; } .col-6 { grid-column:span 6; }\n  .col-8 { grid-column:span 8; } .col-12 { grid-column:span 12; }\n  @media (max-width:900px){ .col-4,.col-6,.col-8 { grid-column:span 12; } }\n  .row { display:flex; align-items:center; justify-content:space-between; gap:10px;\n         padding:8px 0; border-bottom:1px solid var(--line); }\n  .row:last-child { border-bottom:0; }\n  .pill { font-size:11px; padding:2px 9px; border-radius:20px; font-weight:600; }\n  .ok { background:rgba(62,207,142,.15); color:var(--green); }\n  .warn { background:rgba(245,179,66,.15); color:var(--amber); }\n  .err { background:rgba(255,107,107,.15); color:var(--red); }\n  .mono { font-family:ui-monospace,Menlo,monospace; font-size:12px; color:var(--muted); }\n  .sev { font-size:11px; font-weight:700; padding:2px 8px; border-radius:6px; }\n  .CRITICAL { background:var(--crit); color:#fff; } .HIGH { background:var(--red); color:#fff; }\n  .MEDIUM { background:var(--amber); color:#221; } .LOW { background:var(--panel2); color:var(--muted); }\n  table { width:100%; border-collapse:collapse; }\n  td,th { text-align:left; padding:7px 6px; border-bottom:1px solid var(--line); vertical-align:top; }\n  th { color:var(--muted); font-size:11px; text-transform:uppercase; }\n  .shots { display:grid; grid-template-columns:repeat(auto-fill,minmax(120px,1fr)); gap:8px; }\n  .shots img { width:100%; border-radius:6px; border:1px solid var(--line); cursor:zoom-in; }\n  input,textarea { font:inherit; background:var(--bg); color:var(--text);\n                   border:1px solid var(--line); border-radius:7px; padding:7px; width:100%; }\n  label { display:block; color:var(--muted); font-size:12px; margin:8px 0 3px; }\n  .flex { display:flex; gap:8px; align-items:center; }\n  .stat { font-size:26px; font-weight:700; } .stat small { font-size:12px; color:var(--muted); font-weight:400; }\n  dialog { background:var(--panel); color:var(--text); border:1px solid var(--line);\n           border-radius:12px; max-width:90vw; }\n  dialog img { max-width:86vw; max-height:80vh; border-radius:8px; }\n  nav.tabs { display:flex; gap:6px; padding:10px 24px 0; background:var(--panel);\n             border-bottom:1px solid var(--line); }\n  nav.tabs button { border:1px solid var(--line); border-bottom:none;\n                    border-radius:8px 8px 0 0; background:transparent; color:var(--muted);\n                    padding:8px 16px; }\n  nav.tabs button.active { background:var(--bg); color:var(--text);\n                           border-color:var(--accent); font-weight:600; }\n  .obsgrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-bottom:14px; }\n  .tile { background:var(--panel2); border:1px solid var(--line); border-radius:10px; padding:10px 12px; }\n  .tile .tlabel { font-size:11px; color:var(--muted); display:flex; align-items:center; gap:6px; }\n  .tile .tvalue { font-size:26px; font-weight:650; margin:2px 0; }\n  .tile .tsub { font-size:11px; color:var(--muted); }\n  .dot { width:8px; height:8px; border-radius:50%; display:inline-block; }\n  .chartbox { background:var(--panel2); border:1px solid var(--line); border-radius:10px; padding:12px; }\n  .chartbox h3 { font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); margin:0 0 8px; font-weight:600; }\n  .legend { display:flex; gap:14px; font-size:11px; color:var(--muted); margin-top:6px; }\n  .legend span { display:flex; align-items:center; gap:5px; }\n  .meter { height:10px; border-radius:5px; background:rgba(62,207,142,.15); overflow:hidden; margin-top:6px; }\n  .meter i { display:block; height:100%; border-radius:5px; }\n  .flow { width:100%; height:auto; display:block; }\n  .flow .node rect { fill:#1b2444; stroke:#26305a; stroke-width:1.5; rx:10; transition:all .2s; }\n  .flow .node text { fill:#e7ecff; font:600 13px system-ui,-apple-system,sans-serif; }\n  .flow .node .nsub { fill:#8592c0; font:400 10.5px system-ui,-apple-system,sans-serif; }\n  .flow .node { cursor:pointer; }\n  .flow .node:hover rect, .flow .node.lit rect { stroke:#5b8cff; stroke-width:2; fill:#20294f; }\n  .flow .wire { stroke:#3a4577; stroke-width:2; fill:none; stroke-dasharray:6 5; animation:flowdash 1.2s linear infinite; }\n  .flow .arrow { fill:#3a4577; }\n  @keyframes flowdash { to { stroke-dashoffset:-11; } }\n  .flow .grp rect { fill:none; stroke:#26305a; stroke-dasharray:3 3; rx:12; }\n  .flow .grp text { fill:#8592c0; font:600 11px system-ui,-apple-system,sans-serif; letter-spacing:.05em; }\n  @keyframes cardflash { 0%{ box-shadow:0 0 0 2px #5b8cff; } 100%{ box-shadow:none; } }\n  .flash { animation:cardflash 1.6s ease-out 1; }\n  .flow .pulse { animation:pipepulse 1.1s ease-in-out infinite; }\n  @keyframes pipepulse { 0%,100%{ opacity:1; } 50%{ opacity:.25; } }\n  .flow .looplbl { fill:#8592c0; font:600 11px system-ui,-apple-system,sans-serif; letter-spacing:.04em; }\n  #pipeDetail { margin-top:12px; background:var(--panel2); border:1px solid var(--line);\n                border-radius:10px; padding:12px 14px; }\n  #pipeDetail h3 { margin:0 0 6px; font-size:13px; font-weight:650; display:flex; align-items:center; gap:8px; }\n  .pipelinks { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }\n  .pipelinks a { font-size:11px; font-weight:600; color:var(--accent); text-decoration:none;\n                 border:1px solid var(--line); border-radius:7px; padding:3px 10px;\n                 background:var(--bg); white-space:nowrap; }\n  .pipelinks a:hover { border-color:var(--accent); }\n  .obsrow2 { display:grid; grid-template-columns:2fr 1fr; gap:10px; }\n  @media (max-width:900px){ .obsrow2 { grid-template-columns:1fr; } }\n  .archdiag { min-height:60px; }\n  .archdiag svg { width:100%; height:auto; display:block; border-radius:10px; }\n  .archlinks a { text-decoration:none; }\n  .archnote li { margin:6px 0; color:var(--text); }\n  .archnote li b { font-weight:650; }\n</style>\n</head>\n<body>\n<header>\n  <div class="logo"></div>\n  <div>\n    <h1>Agent CI/CD Admin</h1>\n    <div class="sub">Human control panel for the AgentCore UI-QA pipeline</div>\n  </div>\n  <div class="spacer"></div>\n  <span id="clock" class="sub"></span>\n  <button id="loginBtn" onclick="signIn()">Sign in</button>\n  <button onclick="refresh()">↻ Refresh</button>\n</header>\n\n<nav class="tabs">\n  <button data-tab="intro" onclick="showTab(\'intro\')">Introduction</button>\n  <button data-tab="architecture" onclick="showTab(\'architecture\')">Architecture</button>\n  <button data-tab="pipeline" class="active" onclick="showTab(\'pipeline\')">Pipeline</button>\n  <button data-tab="obs" onclick="showTab(\'obs\')">Observability</button>\n  <button data-tab="evals" onclick="showTab(\'evals\')">Evaluations</button>\n  <button data-tab="opts" onclick="showTab(\'opts\')">Optimizations</button>\n</nav>\n\n<div class="wrap">\n  <!-- KPIs -->\n  <div data-tab-panel="all" class="card col-4"><h2>AgentCore Runtimes</h2>\n    <div class="stat" id="kpiRuntimes">–</div><small>managed + standalone, all READY</small></div>\n  <div data-tab-panel="all" class="card col-4"><h2>Latest QA — Findings</h2>\n    <div class="stat" id="kpiFindings">–</div><small id="kpiOverall">last exploratory run</small></div>\n  <div data-tab-panel="all" class="card col-4"><h2>Pipeline</h2>\n    <div class="stat" id="kpiPipeline">–</div><small>recent GitHub Actions runs</small></div>\n\n  <!-- Introduction: narrated use-case animation (multilingual) -->\n  <div data-tab-panel="intro" class="card col-12">\n    <h2>Introduction \u00b7 narrated tour (~5 min)</h2>\n    <div class="sub" style="margin-bottom:10px">What this pipeline is, why it exists, and how it\'s built on AgentCore \u2014\n      animated with multilingual voice-over (pick the language on the player\'s start screen).</div>\n    <div id="introWrap" style="position:relative;width:100%;aspect-ratio:16/10;background:#0b1020;border-radius:12px;overflow:hidden">\n      <iframe id="introFrame" title="Use-case introduction video" allow="autoplay"\n              style="position:absolute;inset:0;width:100%;height:100%;border:0"></iframe>\n    </div>\n  </div>\n\n  <!-- Architecture -->\n  <div data-tab-panel="architecture" class="card col-12"><h2>Solution architecture</h2>\n    <div style="font-size:15px;font-weight:650;margin-bottom:6px">The agentic UI-QA CI/CD loop this console operates</div>\n    <div class="sub" style="max-width:880px">This console is the human control plane for an autonomous QA loop: GitHub Actions invokes AgentCore harnesses that explore the deployed site, file findings, patch the code and re-test. The full build guide, live configs and these diagrams are documented in the best-practices repo.</div>\n    <div class="flex archlinks" style="margin-top:12px;flex-wrap:wrap">\n      <a href="https://github.com/timwukp/Harness-agentic-AI-agent-best-practices-and-use-case" target="_blank" rel="noopener"><button class="primary">Best-practices repo — Harness agentic AI agents ↗</button></a>\n      <a href="https://github.com/timwukp/Claude-code-on-AWS-Bedrock-Token-monitoring-alarm-system/pull/30" target="_blank" rel="noopener"><button>Live demo PR (goal-driven QA loop) ↗</button></a>\n      <a href="https://github.com/timwukp/bedrock-agentcore-agent-ops-console" target="_blank" rel="noopener"><button>Ops console repo (this dashboard) ↗</button></a>\n    </div>\n  </div>\n\n  <div data-tab-panel="architecture" class="card col-12"><h2>High-level — the QA ↔ Bug-Fix loop</h2>\n    <div id="archHigh" class="archdiag"><span class="sub">loading diagram…</span></div>\n    <div class="sub" style="margin-top:8px">Five verified stages: developer push → Actions on PR → UI-Test agent explores the live deploy → Bug-Fix agent patches → auto-fix commit re-triggers the pipeline (the green loop-back wire).</div>\n  </div>\n\n  <div data-tab-panel="architecture" class="card col-12"><h2>Low-level — inside the harnesses (drawn from live configs)</h2>\n    <div id="archLow" class="archdiag"><span class="sub">loading diagram…</span></div>\n    <div class="sub" style="margin-top:8px">Both agents are declarative AgentCore harnesses — model, tools (Browser / Code Interpreter), memory strategies and git-pinned skills are JSON config, with zero orchestration code to maintain.</div>\n  </div>\n\n  <div data-tab-panel="architecture" class="card col-12"><h2>Ops console — how this dashboard monitors the loop</h2>\n    <div id="archOps" class="archdiag"><span class="sub">loading diagram…</span></div>\n    <div class="sub" style="margin-top:8px">The console itself: a single Lambda behind HTTP API Gateway that reads the AgentCore control &amp; data planes, CloudWatch metrics/spans, S3 QA reports and the GitHub API — the same loop shown above, observed from the operator\'s seat.</div>\n  </div>\n\n  <div data-tab-panel="architecture" class="card col-12"><h2>How to read this · demo narration</h2>\n    <ul class="archnote" style="margin:0;padding-left:20px">\n      <li><b>High-level</b> — the 5-stage loop; the green wire looping back is the agent feedback loop: an auto-fix commit pushes to the PR branch and re-runs deploy → QA until findings clear (bounded by MAX_FIX_ROUNDS).</li>\n      <li><b>Low-level</b> — each harness\'s live config: its model, tools (AgentCore Browser / Code Interpreter), memory strategies (episodic · semantic · summarization) and git-pinned skills. The Bug-Fix harness is the least-privilege variant: Code Interpreter only, no browser.</li>\n      <li><b>Model</b> — the model shown in the diagram is whatever the repo last published; the live model is a one-line UpdateHarness config change (see Harness Controls on the Pipeline tab for the current value).</li>\n      <li><b>Ops console</b> — the third diagram is this dashboard\'s own plumbing: a Lambda behind API Gateway reading the AgentCore control/data planes, CloudWatch metrics &amp; spans, S3 QA reports and the GitHub API.</li>\n      <li><b>Diagrams are fetched live</b> from the repo\'s main branch when this tab opens — update the SVG on GitHub and this page reflects it on next load; the animated wires show the direction data flows.</li>\n    </ul>\n  </div>\n\n  <!-- Live pipeline diagram -->\n  <div data-tab-panel="pipeline" class="card col-12"><h2>Live pipeline · ui-qa-agent.yml · click a stage</h2>\n    <div id="pipeHead" class="sub" style="margin:0 0 10px">loading latest run…</div>\n    <div id="pipeViz"></div>\n    <div id="pipeDetail" style="display:none"></div>\n  </div>\n\n  <!-- Runtimes + Harnesses -->\n  <div data-tab-panel="pipeline" class="card col-6"><h2>Runtimes & Harnesses</h2><div id="runtimes"></div></div>\n\n  <!-- Pipeline -->\n  <div data-tab-panel="pipeline" class="card col-6"><h2>CI/CD — Actions & PRs</h2><div id="pipeline"></div></div>\n\n  <!-- QA findings -->\n  <div data-tab-panel="pipeline" class="card col-8"><h2>Latest QA Findings</h2><div id="findings"></div></div>\n\n  <!-- Controls: skill + concurrency -->\n  <div data-tab-panel="pipeline" class="card col-4"><h2>Harness Controls</h2>\n    <div class="mono" id="hName">–</div>\n    <label>Skill git path</label>\n    <input id="skillPath" placeholder="app/ui-test-agent/skills/ui-testing" />\n    <div class="flex" style="margin-top:8px">\n      <button onclick="saveSkill()">Save skill</button>\n      <span id="skillMsg" class="sub"></span>\n    </div>\n    <label style="margin-top:14px">Concurrent QA sessions</label>\n    <div class="flex">\n      <input id="conc" type="number" min="1" max="10" value="3" style="width:80px" />\n      <button class="primary" onclick="fanout()">▶ Run QA fan-out</button>\n    </div>\n    <div id="runMsg" class="sub" style="margin-top:8px"></div>\n  </div>\n\n  <!-- Observability -->\n  <div data-tab-panel="obs" class="card col-12"><h2>Observability · 7d</h2><div id="obs"><span class="sub">loading…</span></div></div>\n\n  <!-- Evaluations -->\n  <div data-tab-panel="evals" class="card col-12"><h2>Evaluations (online)</h2><div id="evals"><span class="sub">loading…</span></div></div>\n\n  <!-- Optimizations: how-it-works flow -->\n  <div data-tab-panel="opts" class="card col-12"><h2>How it works · click a step</h2>\n  <svg class="flow" viewBox="0 0 1160 210" role="img" aria-label="Optimizations flow">\n    <defs><marker id="ah" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 Z" class="arrow"/></marker></defs>\n    <g class="node" onclick="flowGo(1)"><rect x="10" y="60" width="190" height="66"/>\n      <text x="105" y="87" text-anchor="middle">1 · Insights</text>\n      <text x="105" y="105" text-anchor="middle" class="nsub">failures · intents · summaries</text></g>\n    <path class="wire" d="M200,93 H247" marker-end="url(#ah)"/>\n    <g class="node" onclick="flowGo(2)"><rect x="252" y="60" width="200" height="66"/>\n      <text x="352" y="87" text-anchor="middle">2 · Recommendation</text>\n      <text x="352" y="105" text-anchor="middle" class="nsub">better prompt / tool descriptions</text></g>\n    <g class="grp"><rect x="500" y="14" width="420" height="150"/><text x="520" y="36">GATEWAYS</text></g>\n    <path class="wire" d="M452,93 H495" marker-end="url(#ah)"/>\n    <g class="node" onclick="flowGo(3)"><rect x="520" y="50" width="180" height="56"/>\n      <text x="610" y="73" text-anchor="middle">Configuration bundle</text>\n      <text x="610" y="90" text-anchor="middle" class="nsub">override defaults</text></g>\n    <g class="node" onclick="flowGo(3)"><rect x="720" y="50" width="180" height="56"/>\n      <text x="810" y="73" text-anchor="middle">Rules</text>\n      <text x="810" y="90" text-anchor="middle" class="nsub">conditional execution</text></g>\n    <path class="wire" d="M920,89 H967" marker-end="url(#ah)"/>\n    <g class="node" onclick="flowGo(3)"><rect x="972" y="56" width="178" height="66"/>\n      <text x="1061" y="83" text-anchor="middle">Dynamic routing</text>\n      <text x="1061" y="101" text-anchor="middle" class="nsub">shift traffic to the winner</text></g>\n    <path class="wire" d="M452,110 C480,175 560,185 600,185 H720" marker-end="url(#ah)"/>\n    <g class="node" onclick="flowGo(4)"><rect x="725" y="158" width="175" height="46"/>\n      <text x="812" y="178" text-anchor="middle">A/B test</text>\n      <text x="812" y="194" text-anchor="middle" class="nsub">validate improvements</text></g>\n    <path class="wire" d="M900,181 C960,181 1020,160 1050,128" marker-end="url(#ah)"/>\n  </svg>\n  </div>\n\n  <!-- Optimizations: insights / recommendations / drafts -->\n  <div data-tab-panel="opts" class="card col-12"><h2>Step 1 — Insights · AI session analysis</h2>\n    <div class="row" style="border-bottom:none;padding:2px 0 6px">\n      <span id="insCfg" class="sub">loading…</span>\n      <span style="white-space:nowrap"><button class="primary" onclick="runInsightsReport()" style="padding:4px 12px;font-size:12px">▶ Generate report now</button> <span id="insMsg" class="sub"></span></span>\n    </div>\n    <div id="insReports"><span class="sub">loading…</span></div>\n  </div>\n\n  <div data-tab-panel="opts" class="card col-6"><h2>Step 2 — Recommendations · AWS native</h2>\n    <div id="natRecs"><span class="sub">loading…</span></div>\n    <div class="flex" style="margin-top:10px">\n      <button class="primary" onclick="genNativeRec()">⚙ Generate from traces</button>\n      <span id="optMsg" class="sub"></span>\n    </div>\n    <div class="sub" style="margin-top:6px">Steps 3–4 (roadmap): native A/B via CreateABTest SDK needs a Gateway front — this harness deploys winners directly via apply. <a href="https://console.aws.amazon.com/bedrock-agentcore/home?region=us-east-1" target="_blank" style="color:var(--accent)">Console</a></div>\n  </div>\n\n  <div data-tab-panel="opts" class="card col-6"><h2>Prompt drafts · Bedrock-assisted</h2>\n    <div id="drafts"><span class="sub">loading…</span></div>\n    <div class="flex" style="margin-top:10px">\n      <button onclick="genOpt()">✨ Draft new prompt</button>\n      <span id="draftMsg" class="sub"></span>\n    </div>\n  </div>\n\n  <!-- Screenshots -->\n  <div data-tab-panel="pipeline" class="card col-12"><h2>QA Screenshots (latest run)</h2><div class="shots" id="shots"></div></div>\n\n  <!-- Fan-out runs -->\n  <div data-tab-panel="pipeline" class="card col-12"><h2>Fan-out Runs</h2><div id="runs"><span class="sub">none yet</span></div></div>\n</div>\n\n<dialog id="imgModal" onclick="this.close()"><img id="modalImg" /></dialog>\n\n<script>\nconst API = "";  // same-origin (Lambda Function URL)\nconst HID = "__UI_HARNESS__";  // replaced at deploy time\n// admin token for mutations: from URL hash #token=... (kept out of history/logs)\nlet SESSION = JSON.parse(localStorage.getItem("adminSession")||"null");\nif (SESSION && SESSION.exp && Date.now() > SESSION.exp) { SESSION = null; localStorage.removeItem("adminSession"); }\nfunction authHeaders(){ return SESSION ? {"authorization": "Bearer "+SESSION.token} : {}; }\nfunction setAuthUi(){\n  const b = $("loginBtn");\n  if (b) b.textContent = SESSION ? ("admin · sign out") : "Sign in";\n}\nfunction signOut(){ SESSION = null; localStorage.removeItem("adminSession"); setAuthUi(); }\nasync function signIn(){\n  if (SESSION) { if (confirm("Sign out?")) signOut(); return true; }\n  const u = prompt("Admin username:", "admin"); if (!u) return false;\n  const p = prompt("Password:"); if (!p) return false;\n  const r = await fetch(API+"/api/login",{method:"POST",headers:{"content-type":"application/json"},\n    body:JSON.stringify({username:u.trim(), password:p})}).then(x=>x.json()).catch(e=>({error:String(e)}));\n  if (r.accessToken) {\n    SESSION = {token:r.accessToken, exp: Date.now()+(r.expiresIn-60)*1000, user:u.trim()};\n    localStorage.setItem("adminSession", JSON.stringify(SESSION)); setAuthUi(); return true;\n  }\n  alert("Login failed: "+(r.error||"unknown")); return false;\n}\nasync function ensureToken(){\n  if (SESSION) return true;\n  return await signIn();\n}\nconst $ = (id) => document.getElementById(id);\nconst badge = (s) => s==="READY"||s==="success"||s==="complete" ? "ok"\n                   : s==="running"||s==="in_progress" ? "warn"\n                   : (s==="failure"||s==="error") ? "err" : "warn";\n\nsetInterval(()=>$("clock").textContent=new Date().toLocaleTimeString(),1000);\n\nasync function refresh(){\n  const o = await fetch(API+"/api/overview").then(r=>r.json());\n  $("kpiRuntimes").textContent = o.runtimes.length;\n  $("kpiPipeline").textContent = o.pipeline.runs.length;\n\n  $("runtimes").innerHTML = o.harnesses.map(h=>\n    `<div class="row"><span>${h.name} <span class="mono">v${h.version}</span></span>\n     <span class="pill ${badge(h.status)}">${h.status}</span></div>`).join("")\n    + o.runtimes.filter(r=>r.managed).map(r=>\n    `<div class="row"><span class="mono">↳ ${r.name}</span>\n     <span class="pill ${badge(r.status)}">${r.status}</span></div>`).join("");\n\n  $("pipeline").innerHTML =\n    o.pipeline.runs.slice(0,6).map(r=>\n    `<div class="row"><span>${r.name} <span class="mono">[${r.event}] ${r.headBranch||""}</span></span>\n     <span class="pill ${badge(r.conclusion||r.status)}">${r.conclusion||r.status}</span></div>`).join("")\n    + (o.pipeline.prs.length ? o.pipeline.prs.map(p=>\n    `<div class="row"><span>PR #${p.number} ${p.title}</span><span class="pill warn">open</span></div>`).join("")\n    : `<div class="row"><span class="sub">no open PRs</span></div>`);\n\n  try { renderRuns(o.runs); } catch(e) { console.error(e); }\n  loadHarness().catch(console.error); loadQa().catch(console.error);\n  const _t = localStorage.getItem("adminTab")||"pipeline";\n  if (_t==="obs") loadObs(); if (_t==="evals") loadEvals(); if (_t==="opts") loadOpts();\n}\n\nasync function loadHarness(){\n  const h = await fetch(API+"/api/harness?id="+HID).then(r=>r.json());\n  $("hName").textContent = h.name+"  ·  "+(h.model||"");\n  const gitPath = (h.skills&&h.skills[0]&&h.skills[0].git&&h.skills[0].git.path)||"";\n  $("skillPath").value = gitPath;\n}\n\nasync function loadQa(){\n  const d = await fetch(API+"/api/qa-latest").then(r=>r.json());\n  const rep = d.report||{findings:[]};\n  window._qaRep = rep;\n  const f = rep.findings||[];\n  $("kpiFindings").textContent = f.length;\n  $("kpiOverall").textContent = "overall: "+(rep.overall||(rep.report_metadata||{}).overall_result||"n/a")+(d.prefix?" · "+d.prefix:"");\n  $("findings").innerHTML = f.length ? `<table><tr><th>Sev</th><th>Page</th><th>Finding</th></tr>`+\n    f.map(x=>`<tr><td><span class="sev ${x.severity}">${x.severity}</span></td>\n      <td>${x.page||""}</td><td>${x.summary||x.title||""}<div class="sub">${x.evidence||x.observed||""}</div></td></tr>`).join("")\n    +`</table>` : `<span class="sub">no findings</span>`;\n  $("shots").innerHTML = (d.screenshots||[]).map(s=>\n    `<img src="${s.url}" onclick="zoom(\'${s.url}\')" title="${s.key}"/>`).join("")\n    || `<span class="sub">no screenshots</span>`;\n}\n\nconst OBS_COLORS = ["#5b8cff","#d95926"];\nconst OBS_NAMES = {};  // optional friendly names, e.g. {harness_MyUiTest:"UI Test"}\nfunction fmtN(n){ n=+n||0; return n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?(n/1e3).toFixed(1)+"K":String(Math.round(n)); }\nfunction roundTopBar(x,y,w,h,r){ if(h<=0) return ""; r=Math.min(r,w/2,h);\n  return `M${x},${y+h} L${x},${y+r} Q${x},${y} ${x+r},${y} L${x+w-r},${y} Q${x+w},${y} ${x+w},${y+r} L${x+w},${y+h} Z`; }\nfunction dailyChart(H){\n  const days=[...new Set(H.flatMap(([n,m])=>(m.daily||[]).map(d=>d.d)))].sort();\n  if(!days.length) return `<div class="sub">no daily series in window</div>`;\n  const byH=H.map(([n,m])=>{const map={};(m.daily||[]).forEach(d=>map[d.d]=d.v);return days.map(d=>map[d]||0);});\n  const W=620,Ht=170,padL=36,padB=20,padT=12,cw=(W-padL-8)/days.length;\n  const max=Math.max(1,...byH.flat()); const step=max<=4?1:Math.ceil(max/4);\n  const top=Math.ceil(max/step)*step;\n  const ticks=[]; for(let v=0;v<=top;v+=step) ticks.push(v);\n  const y=v=>padT+(Ht-padT-padB)*(1-v/(ticks[ticks.length-1]||1));\n  const bw=Math.min(24,(cw-8)/byH.length-2);\n  let g=ticks.map(v=>`<line x1="${padL}" y1="${y(v)}" x2="${W-4}" y2="${y(v)}" stroke="#26305a" stroke-width="1"/>`+\n    `<text x="${padL-6}" y="${y(v)+3.5}" font-size="10" fill="#8592c0" text-anchor="end">${fmtN(v)}</text>`).join("");\n  let bars="";\n  byH.forEach((vals,si)=>{ vals.forEach((v,di)=>{\n    const x=padL+di*cw+(cw-byH.length*(bw+2))/2+si*(bw+2);\n    const isLast=di===days.length-1;\n    bars+=`<path d="${roundTopBar(x,y(v),bw,y(0)-y(v),4)}" fill="${OBS_COLORS[si]}"><title>${OBS_NAMES[H[si][0]]||H[si][0]} · ${days[di]}: ${v} invocations</title></path>`;\n    if(isLast&&v>0) bars+=`<text x="${x+bw/2}" y="${y(v)-4}" font-size="10" fill="#e7ecff" text-anchor="middle">${fmtN(v)}</text>`;\n  });});\n  const xl=days.map((d,di)=>`<text x="${padL+di*cw+cw/2}" y="${Ht-6}" font-size="10" fill="#8592c0" text-anchor="middle">${d}</text>`).join("");\n  return `<svg viewBox="0 0 ${W} ${Ht}" style="width:100%;height:auto" role="img" aria-label="Daily invocations by harness">${g}${bars}${xl}</svg>`;\n}\nfunction latencyBars(H){\n  const max=Math.max(1,...H.map(([n,m])=>m.Latency||0));\n  return H.map(([n,m],i)=>{const v=m.Latency||0,w=Math.max(2,100*v/max);\n    return `<div style="margin:7px 0"><div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted)">`+\n      `<span><span class="dot" style="background:${OBS_COLORS[i]}"></span> ${OBS_NAMES[n]||n}</span><span style="color:var(--text)">${fmtN(v)} ms</span></div>`+\n      `<svg viewBox="0 0 100 8" preserveAspectRatio="none" style="width:100%;height:8px;margin-top:3px"><rect x="0" y="0" width="100" height="8" rx="4" fill="#26305a"/>`+\n      `<rect x="0" y="0" width="${w}" height="8" rx="4" fill="${OBS_COLORS[i]}"><title>${OBS_NAMES[n]||n}: ${Math.round(v)} ms avg</title></rect></svg></div>`;}).join("");\n}\nasync function loadObs(){\n  try {\n    const o = await fetch(API+"/api/observability?hours=168").then(r=>r.json());\n    const H = Object.entries(o.harnesses||{});\n    const t = o.tokens||{};\n    const totErr = H.reduce((a,[n,m])=>a+(m.SystemErrors||0)+(m.UserErrors||0),0);\n    const totInv = H.reduce((a,[n,m])=>a+(m.Invocations||0),0);\n    const errPct = totInv? (100*totErr/totInv) : 0;\n    const errColor = errPct===0 ? "#3ecf8e" : errPct<5 ? "#f5b342" : "#ff6b6b";\n    let tiles = H.map(([n,m],i)=>`<div class="tile"><div class="tlabel"><span class="dot" style="background:${OBS_COLORS[i]}"></span>${OBS_NAMES[n]||n} invocations</div>`+\n      `<div class="tvalue">${fmtN(m.Invocations||0)}</div><div class="tsub">${fmtN(m.Sessions||0)} sessions · ${fmtN(m.Latency||0)} ms avg</div></div>`).join("");\n    tiles += `<div class="tile"><div class="tlabel">Tokens in / out</div><div class="tvalue">${fmtN(t.input||0)}</div><div class="tsub">out ${fmtN(t.output||0)}</div></div>`;\n    tiles += `<div class="tile"><div class="tlabel">Error rate</div><div class="tvalue" style="display:flex;align-items:center;gap:8px">${errPct.toFixed(1)}%`+\n      `<span style="font-size:12px;color:${errColor}">${errPct===0?"✓ healthy":totErr+" errors"}</span></div>`+\n      `<div class="meter"><i style="width:${Math.min(100,errPct*10)}%;background:${errColor}"></i></div></div>`;\n    const legend = `<div class="legend">${H.map(([n],i)=>`<span><span class="dot" style="background:${OBS_COLORS[i]}"></span>${OBS_NAMES[n]||n}</span>`).join("")}</div>`;\n    $("obs").innerHTML = `<div class="obsgrid">${tiles}</div>`+\n      `<div class="obsrow2"><div class="chartbox"><h3>Daily invocations · 7d</h3>${dailyChart(H)}${legend}</div>`+\n      `<div class="chartbox"><h3>Avg latency</h3>${latencyBars(H)}<div class="sub" style="margin-top:10px">${o.source||""}</div></div></div>`;\n  } catch(e){ $("obs").innerHTML = `<span class="sub">error: ${e}</span>`; }\n}\n\nconst EVAL_DESC = {Correctness:"factually accurate responses", GoalSuccessRate:"achieves the user goal",\n  ToolSelectionAccuracy:"picks the right tool"};\nfunction scoreArc(pct, color){\n  const r=34, cx=44, cy=44, C=2*Math.PI*r, off=C*(1-Math.max(0,Math.min(1,pct)));\n  return `<svg viewBox="0 0 88 88" style="width:88px;height:88px">`+\n    `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#26305a" stroke-width="8"/>`+\n    `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${color}" stroke-width="8" stroke-linecap="round"`+\n    ` stroke-dasharray="${C}" stroke-dashoffset="${off}" transform="rotate(-90 ${cx} ${cy})"/>`+\n    `<text x="${cx}" y="${cy+5}" font-size="18" font-weight="650" fill="#e7ecff" text-anchor="middle">${Math.round(pct*100)}</text></svg>`;\n}\nfunction evalGauges(c){\n  const st = c.scoreStats||{};\n  return (c.evaluators||[]).map(ev=>{\n    const k = ev.replace("Builtin.","");\n    const a = st[k];\n    const has = a && a.count>0;\n    const pct = has ? a.avg : 0;\n    const color = !has ? "#26305a" : pct>=0.8 ? "#3ecf8e" : pct>=0.5 ? "#f5b342" : "#ff6b6b";\n    return `<div class="tile" style="display:flex;align-items:center;gap:14px">`+\n      (has ? scoreArc(pct,color)\n           : `<svg viewBox="0 0 88 88" style="width:88px;height:88px"><circle cx="44" cy="44" r="34" fill="none" stroke="#26305a" stroke-width="8" stroke-dasharray="4 7"/><text x="44" y="49" font-size="15" fill="#8592c0" text-anchor="middle">—</text></svg>`)+\n      `<div><div style="font-weight:600">${k}</div>`+\n      `<div class="tsub">${EVAL_DESC[k]||""}</div>`+\n      `<div class="tsub" style="margin-top:4px">${has ? a.count+" scored traces · avg "+(pct*100).toFixed(0)+"%" : "awaiting traffic"}</div></div></div>`;\n  }).join("");\n}\nasync function loadEvals(){\n  try {\n    const d = await fetch(API+"/api/evaluations").then(r=>r.json());\n    const cs = d.configs||[];\n    const evalCs = cs.filter(c=>!(c.insights||[]).length);\n    window._insightCs = cs.filter(c=>(c.insights||[]).length);\n    if (!evalCs.length) { $("evals").innerHTML = `<span class="sub">no eval configs</span>`; return; }\n    $("evals").innerHTML = evalCs.map(c=>{\n      const anyScores = c.scoreStats && Object.keys(c.scoreStats).length;\n      if ((c.insights||[]).length) {\n        const chips = c.insights.map(i=>`<span class="pill warn" style="margin-right:6px">${i.replace(/([A-Z])/g," $1").trim()}</span>`).join("");\n        return `<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">`+\n          `<div><span style="font-weight:600">${c.name}</span> <span class="mono">AI insights · ${(c.frequencies||[]).join("/").toLowerCase()||"daily"} · ${c.sampling||0}% sampling</span></div>`+\n          `<span class="pill ${badge(c.status==="ACTIVE"?"READY":c.status)}">${c.status}</span></div>`+\n          `<div style="margin-bottom:6px">${chips}</div>`+\n          `<div class="sub">failure root-causes · user intents · execution summaries — reports cluster ${(c.frequencies||["DAILY"]).join("/").toLowerCase()}; results in CloudWatch → AgentCore Observability</div>`;\n      }\n      return `<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">`+\n        `<div><span style="font-weight:600">${c.name}</span>`+\n        ` <span class="mono">${c.sampling||0}% sampling · ${(c.logGroups||[]).length} log group(s)</span></div>`+\n        `<span class="pill ${badge(c.status==="ACTIVE"?"READY":c.status)}">${c.status}</span></div>`+\n        `<div class="obsgrid" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))">${evalGauges(c)}</div>`+\n        (anyScores ? "" : `<div class="sub" style="margin-top:8px">⏳ ${c.scoresNote||"awaiting scored traffic"} — run a QA fan-out to generate traces; scores land in CloudWatch → AgentCore Observability and appear here.</div>`);\n    }).join(`<hr style="border:none;border-top:1px solid var(--line);margin:14px 0">`)\n      + `<hr style="border:none;border-top:1px solid var(--line);margin:14px 0">`\n      + `<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">`\n      + `<span style="font-weight:600">Batch evaluations <span class="mono">offline scoring · minutes</span></span>`\n      + `<span><button class="primary" onclick="runBatchEval()" style="padding:4px 12px;font-size:12px">▶ Score recent QA sessions</button> <span id="beMsg" class="sub"></span></span></div>`\n      + `<div id="batchEvals"><span class="sub">loading…</span></div>`;\n    loadBatchEvals();\n  } catch(e){ $("evals").innerHTML = `<span class="sub">error: ${e}</span>`; }\n}\n\nasync function loadBatchEvals(){\n  try {\n    const d = await fetch(API+"/api/batch-evals").then(r=>r.json());\n    const bs = d.batchEvaluations||[];\n    if (bs.error) { $("batchEvals").innerHTML = `<span class="sub">${bs.error}</span>`; return; }\n    $("batchEvals").innerHTML = bs.length ? bs.slice(0,5).map(b=>{\n      const gauges = (b.evaluatorSummaries||[]).filter(e=>e.avg!=null).map(e=>{\n        const pct = e.avg, color = pct>=0.8?"#3ecf8e":pct>=0.5?"#f5b342":"#ff6b6b";\n        return `<div class="tile" style="display:flex;align-items:center;gap:12px">${scoreArc(pct,color)}`+\n          `<div><div style="font-weight:600">${e.evaluator}</div>`+\n          `<div class="tsub">${e.evaluated} evaluated · avg ${(pct*100).toFixed(0)}%</div></div></div>`;}).join("");\n      return `<div style="display:flex;align-items:center;justify-content:space-between;margin:6px 0">`+\n        `<span class="mono">${b.name} · ${b.id}</span>`+\n        `<span class="pill ${b.status==="COMPLETED"?"ok":b.status.startsWith("COMPLETED")?"warn":b.status==="FAILED"?"err":"warn"}">${b.status}</span></div>`+\n        (gauges?`<div class="obsgrid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">${gauges}</div>`:\n         `<div class="sub">sessions: ${(b.sessions||{}).completed||0}/${(b.sessions||{}).total||0} scored</div>`);\n    }).join("") : `<span class="sub">none yet — click "Score recent QA sessions"</span>`;\n  } catch(e){ $("batchEvals").innerHTML = `<span class="sub">error: ${e}</span>`; }\n}\n\nasync function runBatchEval(){\n  if (!await ensureToken()) { $("beMsg").textContent = "sign-in required"; return; }\n  $("beMsg").textContent = "starting…";\n  const r = await fetch(API+"/api/batch-eval",{method:"POST",headers:{"content-type":"application/json",...authHeaders()},body:"{}"}).then(x=>x.json());\n  if (r.id) {\n    $("beMsg").textContent = "⏳ "+r.id+" scoring "+r.sessions+" session(s)…";\n    let n=0; const t=setInterval(async()=>{ n++; await loadBatchEvals();\n      const d = await fetch(API+"/api/batch-evals").then(x=>x.json());\n      const me = (d.batchEvaluations||[]).find(x=>x.id===r.id);\n      if ((me && me.status!=="PENDING" && me.status!=="IN_PROGRESS") || n>30) { clearInterval(t); $("beMsg").textContent = me?me.status:"done"; }\n    }, 10000);\n  } else { $("beMsg").textContent = "error: "+(r.error||"").slice(0,100); }\n}\n\nfunction flowGo(step){\n  const cards = document.querySelectorAll(\'[data-tab-panel="opts"]\');\n  // cards: [0]=flow, [1]=insights, [2]=native recs, [3]=drafts\n  if (step===4) { window.open("https://console.aws.amazon.com/bedrock-agentcore/home?region=us-east-1","_blank"); return; }\n  const target = step===1 ? cards[1] : step===2 ? cards[2] : cards[2];\n  if (step===3) { alert("Gateways · configuration bundles · dynamic routing apply to Gateway-fronted agents. This harness deploys winners directly via UpdateHarness (the apply buttons in step 2)."); }\n  if (target) { target.scrollIntoView({behavior:"smooth", block:"start"});\n    target.classList.remove("flash"); void target.offsetWidth; target.classList.add("flash"); }\n}\nfunction trunc(s,n){ s=s||""; if (s.length<=n) return s; const i=s.lastIndexOf(" ",n); return s.slice(0, i>40?i:n)+"…"; }\nasync function loadOpts(){\n  try {\n    const d = await fetch(API+"/api/optimizations").then(r=>r.json());\n    try {\n      const ed = await fetch(API+"/api/evaluations").then(r=>r.json());\n      const ics = (ed.configs||[]).filter(c=>(c.insights||[]).length);\n      $("insCfg").innerHTML = ics.length ? ics.map(c=>\n        `<span style="font-weight:600;color:var(--text)">${c.name}</span> <span class="pill ${badge(c.status==="ACTIVE"?"READY":c.status)}">${c.status}</span>`+\n        ` <span class="mono">${(c.frequencies||["DAILY"]).join("/").toLowerCase()} · ${c.sampling||0}% sampling</span>`+\n        c.insights.map(i=>`<span class="pill warn" style="margin-left:6px">${i.replace(/([A-Z])/g," $1").trim()}</span>`).join("")).join("<br>")\n        : `no insights config yet`;\n    } catch(e){}\n    const nat = Array.isArray(d.native) ? d.native : [];\n    $("natRecs").innerHTML = nat.length ? nat.map(n=>\n      `<div class="row"><span>${n.name} <span class="mono">${n.type==="SYSTEM_PROMPT_RECOMMENDATION"?"system prompt":"tool descriptions"}</span>`+\n      `<div class="sub">${trunc(n.explanation||"",170)}</div></span>`+\n      `<span style="white-space:nowrap"><span class="pill ${n.status==="COMPLETED"?"ok":n.status==="FAILED"?"err":"warn"}">${n.status}</span>`+\n      (n.status==="COMPLETED"&&n.recommendedPrompt?` <button onclick="applyNativeRec(\'${n.id}\')" style="padding:2px 10px;font-size:11px">apply</button>`:``)+\n      `</span></div>`).join("")\n      : `<span class="sub">none yet — generate one from recent traces</span>`;\n    const rs = d.recommendations||[];\n    $("drafts").innerHTML = rs.length ? rs.map(r=>\n      `<div class="row"><span><span class="mono">${r.id}</span>`+\n      `<div class="sub">${trunc(r.rationale||"",170)}</div></span>`+\n      `<span style="white-space:nowrap"><span class="pill ${r.status==="applied"?"ok":"warn"}">${r.status}</span>`+\n      (r.status==="proposed"?` <button onclick="applyOpt(\'${r.id}\')" style="padding:2px 10px;font-size:11px">apply</button>`:``)+\n      `</span></div>`).join("")\n      : `<span class="sub">none yet — draft one from QA findings + eval data</span>`;\n    loadInsightsReports();\n  } catch(e){ $("natRecs").innerHTML = `<span class="sub">error: ${e}</span>`; }\n}\n\nasync function loadInsightsReports(){\n  try {\n    const d = await fetch(API+"/api/insights-reports").then(r=>r.json());\n    const rs = d.reports||[];\n    if (!rs.length) { $("insReports").innerHTML = `<div class="sub">no reports yet — the daily schedule runs automatically, or generate one now</div>`; return; }\n    const done = rs.find(r=>r.status==="COMPLETED");\n    let head = `<div class="sub" style="margin:2px 0 8px">reports: `+rs.slice(0,4).map(r=>\n      `<span class="mono">${r.name}</span> <span class="pill ${r.status==="COMPLETED"?"ok":r.status==="FAILED"?"err":"warn"}">${r.status}</span>`).join(" · ")+`</div>`;\n    let cols = "";\n    if (done) {\n      const rep = await fetch(API+"/api/insights-report?id="+done.id).then(r=>r.json());\n      const col = (title, items) => `<div class="chartbox"><h3>${title}</h3>`+\n        ((items&&items.length) ? items.slice(0,4).map(x=>\n          `<div style="padding:8px 0;border-bottom:1px solid var(--line)">`+\n          `<div style="display:flex;justify-content:space-between;gap:8px"><span style="font-weight:600;font-size:13px">${x.name}</span>`+\n          `<span class="pill warn" style="white-space:nowrap">${x.affectedSessionCount} session${x.affectedSessionCount>1?"s":""}</span></div>`+\n          `<div class="sub" style="margin-top:3px">${trunc(x.description||"",180)}</div></div>`).join("")\n         : `<div class="sub">none found</div>`)+`</div>`;\n      cols = `<div class="obsgrid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">`+\n        col("Failure clusters", rep.failures)+col("User intents", rep.intents)+col("Execution summaries", rep.summaries)+`</div>`;\n    }\n    $("insReports").innerHTML = head + cols;\n  } catch(e){ $("insReports").innerHTML = `<span class="sub">report error: ${e}</span>`; }\n}\n\nasync function runInsightsReport(){\n  if (!await ensureToken()) { $("insMsg").textContent = "sign-in required"; return; }\n  $("insMsg").textContent = "starting…";\n  const r = await fetch(API+"/api/insights-report",{method:"POST",headers:{"content-type":"application/json",...authHeaders()},body:"{}"}).then(x=>x.json());\n  $("insMsg").textContent = r.id ? "⏳ analyzing "+r.sessions+" session(s) (~2 min)" : "error: "+(r.error||"").slice(0,80);\n  if (r.id) setTimeout(loadInsightsReports, 90000);\n  loadInsightsReports();\n}\n\nasync function genNativeRec(){\n  if (!await ensureToken()) { $("optMsg").textContent = "sign-in required"; return; }\n  $("optMsg").textContent = "starting native recommendation… (~10 min)";\n  const r = await fetch(API+"/api/native-rec",{method:"POST",headers:{"content-type":"application/json",...authHeaders()},body:"{}"}).then(x=>x.json());\n  $("optMsg").textContent = r.id ? "⏳ "+r.id+" "+r.status+" — refresh in ~10 min" : "error: "+(r.error||"").slice(0,100);\n  loadOpts();\n}\n\nasync function applyNativeRec(id){\n  if (!await ensureToken()) return;\n  if (!confirm("Apply the AWS-recommended prompt to the live harness?")) return;\n  const r = await fetch(API+"/api/native-rec/apply",{method:"POST",headers:{"content-type":"application/json",...authHeaders()},body:JSON.stringify({id})}).then(x=>x.json());\n  alert(r.ok ? "Applied — harness updating" : "Error: "+(r.error||"unknown"));\n  loadOpts();\n}\n\nasync function genOpt(){\n  if (!await ensureToken()) { $("draftMsg").textContent = "cancelled — sign-in required"; return; }\n  $("draftMsg").textContent="generating… (~20s)";\n  try {\n    const r = await fetch(API+"/api/optimize",{method:"POST",headers:{"content-type":"application/json",...authHeaders()},body:"{}"}).then(r=>r.json());\n    if (r.id) {\n      $("draftMsg").textContent = "⏳ "+r.id+" generating…";\n      let n = 0;\n      const t = setInterval(async () => {\n        n++; await loadOpts();\n        const d = await fetch(API+"/api/optimizations").then(x=>x.json());\n        const me = (d.recommendations||[]).find(x=>x.id===r.id);\n        if ((me && me.status!=="generating") || n>20) {\n          clearInterval(t);\n          $("draftMsg").textContent = me && me.status==="proposed" ? "✓ "+r.id+" proposed" : (me && me.error_msg ? "error: "+me.error_msg.slice(0,80) : "✓ done");\n        }\n      }, 5000);\n    } else {\n      if (r.error==="unauthorized") { signOut();\n        $("draftMsg").textContent = "session expired — click again to sign in"; }\n      else $("draftMsg").textContent = "error: "+(r.error||JSON.stringify(r)).slice(0,80);\n    }\n  } catch(e){ $("draftMsg").textContent = "error: "+e; }\n  loadOpts();\n}\n\nasync function applyOpt(id){\n  if (!await ensureToken()) return;\n  if (!confirm("Apply this prompt to the live harness via UpdateHarness?")) return;\n  const r = await fetch(API+"/api/optimize/apply",{method:"POST",headers:{"content-type":"application/json",...authHeaders()},body:JSON.stringify({id})}).then(r=>r.json());\n  alert(r.ok ? "Applied — harness updating" : "Error: "+(r.error||"unknown"));\n  loadOpts();\n}\n\nfunction renderRuns(runs){\n  $("runs").innerHTML = (runs&&runs.length) ? runs.map(b=>\n    `<div class="row"><span class="mono">${b.id} · ${b.concurrency||"?"}× · ${new Date(b.startedAt).toLocaleTimeString()}</span>\n     <span class="pill ${badge(b.status)}">${b.status} (${(b.sessions||[]).length}/${b.concurrency||"?"})</span></div>`).join("")\n    : `<span class="sub">none yet</span>`;\n}\n\nfunction zoom(u){ $("modalImg").src=u; $("imgModal").showModal(); }\n\nasync function saveSkill(){\n  if (!await ensureToken()) { $("skillMsg").textContent = "sign-in required"; return; }\n  $("skillMsg").textContent="saving…";\n  const skill = {git:{url:"__SKILL_REPO_URL__",  // your harness skill repo\n                      path:$("skillPath").value}};\n  const r = await fetch(API+"/api/skill",{method:"POST",headers:{"content-type":"application/json",...authHeaders()},\n    body:JSON.stringify({id:HID, skill})}).then(r=>r.json());\n  $("skillMsg").textContent = r.ok ? "✓ updated (harness updating)" : ("error: "+(r.error||""));\n}\n\nasync function fanout(){\n  if (!await ensureToken()) { $("runMsg").textContent = "sign-in required"; return; }\n  const c = Number($("conc").value);\n  $("runMsg").textContent="launching "+c+" concurrent QA session(s)…";\n  const r = await fetch(API+"/api/qa-run",{method:"POST",headers:{"content-type":"application/json",...authHeaders()},\n    body:JSON.stringify({concurrency:c})}).then(r=>r.json());\n  $("runMsg").textContent="batch "+r.id+" started; poll with Refresh.";\n  refresh();\n}\n\n// ── Live pipeline diagram (ui-qa-agent.yml, latest run) ─────────────────────\nconst PIPE_ICONS = {setup:"⚙", auth:"⛨", deploy:"⬆", qa:"☑", rounds:"⟳",\n                    bugfix:"⚒", comment:"✉", gate:"⚑"};\nconst PIPE_SUBS  = {setup:"checkout · python · node", auth:"OIDC → AWS role",\n                    deploy:"build → S3 → CDN", qa:"explore · reconcile",\n                    rounds:"loop guard", bugfix:"patch → push",\n                    comment:"post results", gate:"pass / fail"};\nlet PIPE_TIMER = null, PIPE_SEL = null;\nfunction pipeColor(st){\n  if (st.status==="in_progress") return "#f5b342";\n  if (st.conclusion==="success") return "#3ecf8e";\n  if (st.conclusion==="failure") return "#ff6b6b";\n  if (st.conclusion==="cancelled") return "#ff6b6b";\n  return "#5a6491";\n}\nfunction fmtDur(a,b){\n  if (!a) return "";\n  const ms = (b?new Date(b):new Date()) - new Date(a);\n  if (ms<0||isNaN(ms)) return "";\n  const s = Math.round(ms/1000);\n  return s<60 ? s+"s" : Math.floor(s/60)+"m "+(s%60)+"s";\n}\nfunction esc(s){ return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }\nfunction renderPipe(d){\n  const stages = d.stages||[];\n  const NW=152, GAP=24, Y=58, H=78, STEP=NW+GAP;\n  const W = stages.length*STEP-GAP+20, VH=236;\n  let svg = `<svg class="flow" viewBox="0 0 ${W} ${VH}" role="img" aria-label="Live CI/CD pipeline">`+\n    `<defs><marker id="pah" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 Z" class="arrow"/></marker></defs>`;\n  const xs = stages.map((_,i)=>10+i*STEP);\n  stages.forEach((st,i)=>{\n    const x = xs[i], c = pipeColor(st), live = st.status==="in_progress";\n    if (i<stages.length-1)\n      svg += `<path class="wire" d="M${x+NW},${Y+H/2} H${x+STEP-5}" marker-end="url(#pah)"/>`;\n    svg += `<g class="node${PIPE_SEL===i?" lit":""}" onclick="pipeClick(${i})">`+\n      `<rect x="${x}" y="${Y}" width="${NW}" height="${H}" style="stroke:${c}"/>`+\n      `<circle cx="${x+16}" cy="${Y+16}" r="5" fill="${c}"${live?\' class="pulse"\':\'\'}/>`+\n      `<text x="${x+NW/2}" y="${Y+30}" text-anchor="middle" style="font-size:16px">${PIPE_ICONS[st.key]||""}</text>`+\n      `<text x="${x+NW/2}" y="${Y+50}" text-anchor="middle" style="font-size:12px">${esc(st.label)}</text>`+\n      `<text x="${x+NW/2}" y="${Y+66}" text-anchor="middle" class="nsub">${PIPE_SUBS[st.key]||""}</text></g>`;\n  });\n  // loop-back: Bug-Fix → Deploy (auto-fix commit re-triggers the workflow)\n  const bi = stages.findIndex(s=>s.key==="bugfix"), di = stages.findIndex(s=>s.key==="deploy");\n  if (bi>=0 && di>=0){\n    const bx = xs[bi]+NW/2, dx = xs[di]+NW/2;\n    const loopC = (stages[bi].conclusion==="success") ? "#f5b342" : "#3a4577";\n    svg += `<path class="wire" style="stroke:${loopC}" d="M${bx},${Y+H} V${Y+H+52} H${dx} V${Y+H+8}" marker-end="url(#pah)"/>`+\n      `<text class="looplbl" x="${(bx+dx)/2}" y="${Y+H+46}" text-anchor="middle">auto-fix commit → push → re-runs pipeline (agent feedback loop)</text>`;\n  }\n  svg += `</svg>`;\n  $("pipeViz").innerHTML = svg;\n  const r = d.run;\n  $("pipeHead").innerHTML = r ?\n    `run #${r.number} · <span class="mono">[${esc(r.event)}] ${esc(r.branch)}</span> · `+\n    `<span class="pill ${badge(r.conclusion||r.status)}">${r.conclusion||r.status}</span> · `+\n    `fix rounds ${d.fixRounds??0}/${d.maxFixRounds??3} · `+\n    `<a href="${esc(r.url)}" target="_blank" style="color:var(--accent)">view on GitHub</a>`+\n    ` <span class="mono">· ${new Date(r.createdAt).toLocaleString()}</span>`\n    : `no runs of ui-qa-agent.yml yet`;\n}\n// "learn more / see the actual config" links per stage — all public, non-sensitive.\nconst PIPE_REPO = "https://github.com/timwukp/Claude-code-on-AWS-Bedrock-Token-monitoring-alarm-system";\nconst PIPE_WF   = PIPE_REPO+"/blob/main/.github/workflows/ui-qa-agent.yml";\nconst PIPE_LINKS = {\n  setup:   [["workflow yml ↗", PIPE_WF]],\n  auth:    [["workflow: configure-aws-credentials ↗", PIPE_WF],\n            ["GitHub OIDC docs ↗", "https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services"]],\n  deploy:  [["workflow yml (deploy step) ↗", PIPE_WF]],\n  qa:      [["qa_agent.py ↗", PIPE_REPO+"/blob/main/ci-agent/qa_agent.py"],\n            ["harness anatomy (best-practices repo) ↗", "https://github.com/timwukp/Harness-agentic-AI-agent-best-practices-and-use-case"]],\n  rounds:  [["workflow yml (round counter) ↗", PIPE_WF]],\n  bugfix:  [["bugfix_agent.py ↗", PIPE_REPO+"/blob/main/ci-agent/bugfix_agent.py"]],\n  comment: [["real PR comment (PR #30) ↗", PIPE_REPO+"/pull/30"]],\n  gate:    [["workflow yml (gate step) ↗", PIPE_WF]],\n};\nfunction pipeStageLinks(key){\n  const ls = PIPE_LINKS[key]||[]; if (!ls.length) return "";\n  return `<div class="pipelinks">`+ls.map(([t,u])=>\n    `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(t)}</a>`).join("")+`</div>`;\n}\nfunction pipeStageDetail(st, d){\n  const rep = window._qaRep||{}, f = rep.findings||[];\n  switch(st.key){\n    case "setup": return "Checks out the PR branch (full history for the fix-round counter), sets up Python 3.12 + Node 20 and installs the ci-agent deps (boto3).";\n    case "auth": return "Exchanges the GitHub OIDC token for short-lived AWS credentials (assume-role) — no long-lived keys in the repo. On the AWS side an IAM role (GitHubActionsUITestRole) trusts GitHub\'s OIDC provider (token.actions.githubusercontent.com), scoped to this repository.";\n    case "deploy": return `Deploys branch <b>${esc((d.run||{}).branch||"?")}</b> to the live dev site: npm build (VITE env) → aws s3 sync → CloudFront invalidation (waits for completion) — so QA tests THIS code, closing the fix→re-test loop.`;\n    case "qa": return `AgentCore UI-QA harness logs into the live site, explores 5 pages and re-verifies prior findings (FIXED / STILL_FAILING).<br>Latest report: <b>${f.length} finding(s)</b> · overall <b>${esc(rep.overall||(rep.report_metadata||{}).overall_result||"n/a")}</b>.`;\n    case "rounds": return `Counts prior <span class="mono">chore(agent): auto-fix round</span> commits on the branch to guard the loop. Used <b>${d.fixRounds??0}</b> of <b>${d.maxFixRounds??3}</b> (MAX_FIX_ROUNDS).`;\n    case "bugfix": return `Runs only when QA found blocking bugs and rounds &lt; MAX_FIX_ROUNDS: Bug-Fix harness root-causes findings against the repo source, patches, and pushes an auto-fix commit — which re-triggers this whole workflow (the loop-back wire). Rounds used: <b>${d.fixRounds??0}/${d.maxFixRounds??3}</b>.`;\n    case "comment": return "Posts the findings table, reconciliation of prior findings and the Bug-Fix summary as a PR comment. Skipped on manual (workflow_dispatch) runs.";\n    case "gate": return "Fails the check only if blocking findings remain after MAX_FIX_ROUNDS auto-fix rounds — skipped means the run needed no human escalation.";\n  }\n  return "";\n}\nfunction pipeClick(i){\n  const d = window._pipeData; if (!d) return;\n  const st = (d.stages||[])[i]; if (!st) return;\n  PIPE_SEL = i; renderPipe(d);\n  const c = pipeColor(st);\n  const dur = fmtDur(st.started_at, st.completed_at);\n  $("pipeDetail").style.display = "";\n  $("pipeDetail").innerHTML =\n    `<h3><span style="color:${c}">${PIPE_ICONS[st.key]||""}</span> ${esc(st.label)} `+\n    `<span class="pill" style="background:${c}22;color:${c}">${st.conclusion||st.status||"pending"}</span></h3>`+\n    `<div class="mono">started: ${st.started_at?new Date(st.started_at).toLocaleString():"—"} · `+\n    `completed: ${st.completed_at?new Date(st.completed_at).toLocaleString():(st.status==="in_progress"?"running…":"—")}`+\n    (dur?` · duration: ${dur}${st.completed_at?"":" so far"}`:"")+`</div>`+\n    `<div style="margin-top:8px">${pipeStageDetail(st, d)}</div>`+\n    pipeStageLinks(st.key);\n}\n// ?stage=<key> URL param (mirrors ?tab=): auto-open a stage\'s detail panel on load.\nlet PIPE_URL_STAGE = new URLSearchParams(location.search).get("stage");\nasync function loadPipe(){\n  try {\n    const d = await fetch(API+"/api/pipeline").then(r=>r.json());\n    window._pipeData = d;\n    renderPipe(d);\n    if (PIPE_SEL==null && PIPE_URL_STAGE){\n      const i = (d.stages||[]).findIndex(s=>s.key===PIPE_URL_STAGE);\n      PIPE_URL_STAGE = null;\n      if (i>=0) pipeClick(i);\n    }\n    if (PIPE_SEL!=null) pipeClick(PIPE_SEL);\n  } catch(e){\n    $("pipeHead").textContent = "pipeline error: "+e;\n  }\n}\nfunction pipePoll(on){\n  if (PIPE_TIMER) { clearInterval(PIPE_TIMER); PIPE_TIMER = null; }\n  if (on) PIPE_TIMER = setInterval(()=>{ loadPipe().catch(()=>{}); }, 30000);\n}\n\n// ── Architecture tab: fetch + inline the animated SVG diagrams ──────────────\n// The diagrams live in the best-practices repo and are fetched at tab-open time\n// (thin client: update the SVG on GitHub and the dashboard shows it on next load).\n// <img src> would block the CSS wire animations in some browsers, so we inline the\n// SVG text — inside a Shadow DOM so its <style> rules (.card/.mono/.wire) and ids\n// (#ah) can\'t clash with the page\'s own.\nconst ARCH_REPO = "https://github.com/timwukp/Harness-agentic-AI-agent-best-practices-and-use-case";\nconst ARCH_RAW = "https://raw.githubusercontent.com/timwukp/Harness-agentic-AI-agent-best-practices-and-use-case/main/docs/";\nconst OPS_REPO = "https://github.com/timwukp/bedrock-agentcore-agent-ops-console";\nconst OPS_RAW = "https://raw.githubusercontent.com/timwukp/bedrock-agentcore-agent-ops-console/main/docs/";\nlet ARCH_LOADED = false;  // lazy: only fetch when the tab first opens, then cached in the DOM\nasync function inlineSvg(elId, rawBase, repo, file){\n  const el = $(elId);\n  try {\n    const r = await fetch(rawBase+file, {cache:"no-cache"});\n    if (!r.ok) throw new Error("HTTP "+r.status);\n    let svg = await r.text();\n    svg = svg.replace(/<script[\\s\\S]*?<\\/script\\s*>/gi, "");  // hygiene: never inline scripts\n    if (svg.indexOf("<svg") < 0) throw new Error("not an SVG");\n    const root = el.shadowRoot || el.attachShadow({mode:"open"});\n    root.innerHTML = "<style>:host{display:block}svg{width:100%;height:auto;display:block;border-radius:10px}</style>"+svg;\n    el.classList.remove("archdiag");\n  } catch(e){\n    el.innerHTML = `<span class="sub">diagram unavailable (${String(e.message||e).slice(0,80)}) — </span>`+\n      `<a href="${repo}/blob/main/docs/${file}" target="_blank" rel="noopener" style="color:var(--accent)">View on GitHub ↗</a>`;\n  }\n}\nasync function loadArch(){\n  if (ARCH_LOADED) return;\n  ARCH_LOADED = true;\n  try {\n    await Promise.all([inlineSvg("archHigh",ARCH_RAW,ARCH_REPO,"architecture-high-level.svg"),\n                       inlineSvg("archLow",ARCH_RAW,ARCH_REPO,"architecture-low-level.svg"),\n                       inlineSvg("archOps",OPS_RAW,OPS_REPO,"architecture.svg")]);\n  } catch(e){ console.error(e); }\n}\n\nfunction showTab(t){\n  document.querySelectorAll("[data-tab-panel]").forEach(el=>{\n    el.style.display = (el.dataset.tabPanel===t || el.dataset.tabPanel==="all") ? "" : "none"; });\n  document.querySelectorAll("nav.tabs button").forEach(b=>b.classList.toggle("active", b.dataset.tab===t));\n  localStorage.setItem("adminTab", t);\n  if (t==="intro") { const f=document.getElementById("introFrame"); if (f && !f.src) f.src = API + "/intro"; }\n  if (t==="architecture") { try { loadArch(); } catch(e){ console.error(e); } }\n  if (t==="pipeline") { try { loadPipe(); } catch(e){ console.error(e); } }\n  try { pipePoll(t==="pipeline"); } catch(e){ console.error(e); }\n  if (t==="obs") loadObs();\n  if (t==="evals") loadEvals();\n  if (t==="opts") loadOpts();\n}\n\n// tab on load: ?tab= URL param (bookmarkable/demo-able) > localStorage > pipeline default\nconst KNOWN_TABS = ["intro","architecture","pipeline","obs","evals","opts"];\nconst _urlTab = new URLSearchParams(location.search).get("tab");\nconst _memTab = localStorage.getItem("adminTab");\nshowTab(KNOWN_TABS.includes(_urlTab) ? _urlTab\n      : KNOWN_TABS.includes(_memTab) ? _memTab : "pipeline");\nsetAuthUi();\nrefresh();\n</script>\n</body>\n</html>\n'
