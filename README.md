# Agent CI/CD Admin Dashboard

A serverless, always-on **human control panel for Amazon Bedrock AgentCore agents running in CI/CD** —
monitor them, score them, analyze their failures, and optimize their prompts, all from one page.

Built for the pattern where AgentCore harnesses do autonomous UI-QA and bug-fixing inside GitHub
Actions ([companion best-practices repo](https://github.com/timwukp/Harness-agentic-AI-agent-best-practices-and-use-case)),
but the dashboard works with any AgentCore harness/runtime setup.

![Optimizations tab](docs/screenshots/optimizations-panel.png)

## What you get — five tabs

| Tab | What it shows | Backed by |
|---|---|---|
| **Pipeline** | Harness/runtime status, GitHub Actions runs & PRs, latest QA findings with severity + evidence, QA screenshots, **Run QA** smoke sessions (one per click by default, `QA_MAX_CONCURRENCY`) against an allow-listed URL, each checked against read-only rules | AgentCore control plane, GitHub API, S3 |
| **Observability** | Per-harness invocations / sessions / latency / error-rate stat tiles, 7-day daily-invocations column chart, token usage | `AWS/Bedrock-AgentCore` CloudWatch metrics + EMF `gen_ai.client.token.usage` |
| **Cost** | Month-to-date **billed** cost split into AgentCore platform / Bedrock inference / CloudWatch observability, 30-day daily-cost chart, near-real-time **estimated** per-harness runtime cost, per-fan-out-run cost attribution, optional per-cost-allocation-tag breakdown | Cost Explorer `GetCostAndUsage` + vended `CPUUsed-vCPUHours` / `MemoryUsed-GBHours` CloudWatch metrics — see [Cost telemetry](#cost-telemetry--where-the-numbers-come-from) |
| **Evaluations** | Online evaluation score gauges (Builtin.Correctness / GoalSuccessRate / ToolSelectionAccuracy) over the last `EVAL_WINDOW_DAYS` (default 90), aggregated server-side with CloudWatch Logs Insights and cached `EVAL_CACHE_TTL_S` (600 s), one-click **batch evaluation** of recent QA sessions (offline scoring in minutes) | AgentCore Evaluations (online configs + data-plane `StartBatchEvaluation`) |
| **Optimizations** | Animated clickable how-it-works flow, **AI Insights** (failure root-cause clusters, user intents, execution summaries — on-demand reports), **AWS-native prompt recommendations** (`StartRecommendation`) and Bedrock-drafted alternatives, one-click apply via `UpdateHarness` | AgentCore Optimizations (data-plane SDK) + Bedrock |

> **Currently disabled:** batch evaluation, AWS-native recommendations and on-demand insights
> reports are placeholder stubs in `src/lambda_function.py` (their buttons report "not available").
> The pinned boto3 exposes the underlying APIs, so restoring them is a follow-up. Their IAM grants
> (`BatchEval`, `NativeRecs` in `deploy/iam-policy.json`) were removed with the stubs; add them back
> when you restore the features.

Write actions (fan-out, apply, generate) are protected by **Cognito login** (8-hour access tokens,
server-side validation via `cognito-idp:GetUser`). Read-only views need no sign-in.

**Run QA** drives a browser that signs in to the site under test with `LOGIN_SECRET_ID`, so treat
it like the CI loop's QA pass:
- Point `LOGIN_SECRET_ID` at a **non-admin** login. A shared dev app should refuse that account's
  admin writes on the server.
- The prompt carries the same `SAFETY_RULES` as `ci-agent/qa_agent.py` in the CI repo: no
  Save/Delete-type clicks, no setting changes, no POST/PUT/PATCH/DELETE.
- Every browser action in the stream is checked against those rules. The batch shows
  `read-only: ok`, `violated` or `unobserved`. The check matches selectors and scripts, so a hit
  means a rule was broken and a miss proves nothing. Typed text and scripts are never stored.
- The URL must match `QA_URL_ALLOWLIST` (default: the origin of `TARGET_URL`). The agent types the
  test login into whatever page it is sent to.

## Tests

Offline, with every AWS and GitHub client replaced by a fake; CI runs them on each pull request
(`.github/workflows/tests.yml`):

```bash
pip install boto3==1.43.120
python3 tests/test_b1_b3.py && python3 tests/test_hardening.py && python3 tests/test_run_qa_switch.py
```

## Architecture

![Architecture](docs/architecture.svg)

Single Lambda, no build step, no framework — the dashboard HTML/JS/SVG lives inside
`src/lambda_function.py` and is served on `GET /`. HTTP API Gateway is used instead of a Lambda
Function URL because some org guardrails block public Function URLs.

Data sources read by the Lambda: AgentCore control & data planes, CloudWatch (metrics, Logs
Insights, eval scores, spans), **Cost Explorer** (billed cost, cached), S3 (QA reports &
screenshots), DynamoDB, Bedrock, and the **GitHub REST API** (Actions workflow runs, open PRs,
workflow jobs/steps, and branch commits — anonymous by default, `GITHUB_TOKEN` optional to lift
rate limits).

## Cost telemetry — where the numbers come from

The Cost tab (`GET /api/cost?days=N[&byTag=1]`, `GET /api/cost?batch=<id>`) aggregates **three
distinct telemetry sources**, each labeled in the UI as either `billed` (green) or `estimate`
(amber):

| Layer | Source of truth | Freshness | What it tells you |
|---|---|---|---|
| **1 · Billed** | Cost Explorer `ce:GetCostAndUsage` — `DAILY` granularity, `UnblendedCost`, grouped by `SERVICE` and classified into three families: `Amazon Bedrock AgentCore` (Runtime/Browser/Code Interpreter/Gateway/Memory platform charges), `Amazon Bedrock` (model inference — billed separately from AgentCore), `AmazonCloudWatch` (observability telemetry) | ~24 h billing lag; **account-wide** | The authoritative dollars — what actually lands on the invoice |
| **2 · Estimated (per harness)** | Vended billing-visibility CloudWatch metrics `CPUUsed-vCPUHours` and `MemoryUsed-GBHours` (`AWS/Bedrock-AgentCore` namespace, dims `Resource`=runtime ARN / `Service`=`AgentCore.Runtime` / `Name`=`harness_X::DEFAULT`) × AgentCore list prices, plus the account-wide EMF token metric `gen_ai.client.token.usage` × model token prices | Near-real-time — metrics **flush ~10–15 min after a session ends** | Which harness is spending, today, before billing data exists |
| **3 · Per-run attribution** | Fan-out batches record `startedAt`/`endedAt` in DynamoDB; the Lambda slices the layer-2 CPU/memory series (Period=60 s) inside each batch's `[start−60 s, end+120 s]` window and prices it | Same as layer 2 | Estimated cost of one QA fan-out run — the foundation for per-team / cost-center chargeback |

Honest-numbers caveats, also surfaced in the UI:

- Layer 1 is account-wide (SERVICE-level filtering cannot split multiple workloads sharing the
  account) — per-harness splits come from layer 2, per-cost-center splits from cost-allocation
  tags (below). The token metric in layer 2 has **no per-harness dimension**, so model-inference
  cost is an account-wide estimate.
- Layer 3 assumes the batch is the dominant traffic in its time window; overlapping runs are
  co-attributed. Cost Explorer stays authoritative for dollars.
- Cost Explorer charges **$0.01 per query** — responses are cached ~6 h (warm container +
  `cost-cache-*` items in the DynamoDB runs table), and the per-tag breakdown is a separate
  cached call behind a click.
- Unit prices are env-overridable so a price change never needs a code change:
  `PRICE_VCPU_HR` (default `0.0895`), `PRICE_GB_HR` (`0.00945`), `TOKEN_IN_PER_1K` (`0.003`),
  `TOKEN_OUT_PER_1K` (`0.015`), `COST_CACHE_TTL_S` (`21600`).

**Chargeback prerequisite (one-time):** tag your billable AgentCore resources and activate the
tags as cost allocation tags — only usage *after* activation carries tags in billing data:

```bash
aws bedrock-agentcore-control tag-resource --resource-arn <runtime-arn> \
    --tags CostCenter=<team>,agent-type=<agent-name>
# ~24-48h later, once the tag keys appear in billing data:
aws ce update-cost-allocation-tags-status --cost-allocation-tags-status \
    Status=Active,TagKey=CostCenter Status=Active,TagKey=agent-type
```

The Cost tab's "per-tag breakdown" then groups AgentCore spend by the `agent-type` tag (override with `COST_TAG_KEY`). For
**internal chargeback**, this tags + Cost Explorer path is the AWS-prescribed mechanism
(Well-Architected COST03-BP03) — do **not** reach for AgentCore Payments for that: Payments
moves real money to external merchants (x402/MPP, USDC wallets) and has no sandbox. See
[docs/PAYMENTS-DEMO.md](docs/PAYMENTS-DEMO.md) for what Payments *is* for and a safe demo design.

## Deploy

```bash
export UI_HARNESS=MyUiTestHarness-AbC123        # required: harness to monitor/optimize
export BUGFIX_HARNESS=MyBugFixHarness-XyZ789    # optional second harness
export TARGET_REPO=org/repo                     # CI repo shown in the Pipeline tab
export TARGET_URL=https://your-app.example.com  # site under test
export QA_BUCKET=your-qa-reports-bucket         # where QA reports land
export LOGIN_SECRET_ID=your-app/login-creds-viewer  # a NON-admin test-site login for Run QA
export SKILL_REPO_URL=https://github.com/org/skill-repo
./deploy/deploy.sh
```

The script creates (idempotently): DynamoDB table, IAM role, Cognito user pool + `admin` user
(password goes to Secrets Manager `agent-admin/dashboard-login`, never printed), the Lambda, and an
HTTP API Gateway. Re-run it to ship code updates.

Security defaults are overridable — they match `cdk/stack.py` and are documented in
[SECURITY.md](SECURITY.md):

| Variable | Default | Notes |
|---|---|---|
| `COGNITO_TIER` | `PLUS` | Enables threat protection (blocks sign-ins with breached credentials). A **paid** feature plan — set `ESSENTIALS` to opt out. |
| `PASSWORD_MIN_LENGTH` | `20` | Applied to newly created pools only. |
| `API_RATE_LIMIT` / `API_BURST_LIMIT` | `20` / `40` | Throttles the API stage, capping credential-grinding on `POST /api/login`. Re-applied on every run. |
| `QA_RUN_ENABLED` | `true` | Kill switch for **Run QA**. `false` (or `0`/`no`/`off`) refuses it with 409 before any batch or harness call; batches already queued stop at their worker. Use it until Run QA takes the shared run lease and per-run passwords (see `src/run_qa_spec.json`). |
| `HARNESS_QUALIFIER` | *(unset)* | Harness endpoint Run QA invokes. Unset = today's behaviour (no qualifier). Setting it also needs `InvokeHarness`/`InvokeAgentRuntime` on the endpoint ARN in `deploy/iam-policy.json`. |
| `QA_MAX_CONCURRENCY` | `1` | Most Run QA sessions one click starts. Concurrent sessions on one harness collide (one is cut off mid-run, another streams nothing). |
| `QA_URL_ALLOWLIST` | *(empty)* | Origins Run QA may test, `\|`-separated. Empty means only the origin of `TARGET_URL`. The full origin must match, so another port or a `user@` prefix is refused. |
| `PIPE_BASE_BRANCH` | `main` | Base for counting auto-fix rounds: commits between it and the run's head SHA (`compare/<base>...<sha>`), so a deleted branch still counts. |
| `BOTO3_VERSION` | `1.43.120` | boto3 vendored into the Lambda package. The runtime's built-in boto3 lacks the git skill type, `harnessVersion` and evaluation insights, which then show blank. |

## CRITICAL prerequisite: enable trace sampling

AgentCore harness runtimes default to `trace_sampled=False`. **Evaluations, batch scoring, insights,
and recommendations all read OTel span documents — with sampling off they silently produce nothing**
(online evals sit ACTIVE forever with zero scores; batch evals fail with "All N sessions failed").

```python
ctl.update_harness(harnessId=HID,
    environmentVariables={"OTEL_TRACES_SAMPLER": "always_on"},
    clientToken=secrets.token_hex(20))
```

Then set `SPANS_SINCE` (ISO timestamp of when you enabled it) so the dashboard excludes older,
unscoreable sessions from batch runs. Also confirm CloudWatch Transaction Search is enabled
(`aws xray get-trace-segment-destination` → `CloudWatchLogs/ACTIVE`).

## Hard-won field notes

This dashboard was built against the AgentCore **preview** APIs; the gotchas are documented as
reference files in the companion skill
([agent-skills-best-practice → agentcore-harness-builder](https://github.com/timwukp/agent-skills-best-practice/tree/main/skills/skills/agentcore-harness-builder/references)),
including: batch evaluation / recommendations / insights all live on the **data-plane** SDK client
(not control-plane); `StartBatchEvaluation` FAS caller permissions; evaluation-config name regex and
clientToken length; insight results returned as typed structures on `GetBatchEvaluation`; A/B test
anatomy (requires a Gateway front).

## Screenshots

| | |
|---|---|
| ![Observability](docs/screenshots/observability-panel.png) | ![Evaluations](docs/screenshots/evaluations-panel.png) |

## Roadmap

- **Native A/B testing** via Gateway: front the harness with an AgentCore Gateway, register
  control/variant runtimes as two `agentcoreRuntime` targets, `CreateABTest` with weighted split and
  per-variant online evaluation configs → statistical results (p-value, confidence intervals).
- Insights scheduled-report browser (daily reports already generate server-side).

## Author

**Tim WU** ([@timwukp](https://github.com/timwukp))

## Disclaimer

This is a personal open-source project, provided **"as is" without warranty of any kind** (see
[LICENSE](LICENSE)). It is **not an official AWS product** and is not affiliated with, endorsed by, or
supported by Amazon Web Services. It builds against Amazon Bedrock AgentCore **preview** APIs, which may
change without notice and break this project at any time. You are responsible for the AWS costs, security
posture (IAM permissions, Cognito configuration, exposed endpoints), and compliance of anything you deploy
from this repository — review the IAM policy and deployment scripts before running them in your account.

## License

[MIT](LICENSE)
