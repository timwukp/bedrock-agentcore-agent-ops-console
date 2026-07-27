# Security Policy

This is a personal open-source project built against Amazon Bedrock AgentCore **preview** APIs. It
is not an official AWS product and carries no warranty (see [LICENSE](LICENSE)). There are no
released versions — `main` is the only supported branch, and security fixes land there.

## Reporting a vulnerability

Please use **[GitHub private vulnerability reporting](https://github.com/timwukp/bedrock-agentcore-agent-ops-console/security/advisories/new)**
(Security → Report a vulnerability). It's enabled on this repo and keeps the report private until a
fix ships. Please don't open a public issue for anything exploitable.

Include the endpoint or panel involved, what an attacker controls, and what they gain. This is a
side project — expect a first response within about a week. If a report is valid I'll fix it on a
branch and credit you in the PR unless you'd rather not be named; if I disagree that it's a
vulnerability I'll say why rather than leave it sitting.

## Threat model

Understanding this is more useful than a checklist, because the design deliberately splits reads
from writes:

- **Read endpoints are public and unauthenticated on purpose.** Anyone who knows the API Gateway
  URL can `GET` the dashboard, QA findings, pipeline state, CloudWatch metrics and evaluation
  scores. That is the intended design for an always-on status page, not an oversight. If that
  doesn't suit you, put the API behind an authorizer, CloudFront + WAF, or private endpoint.
- **All write endpoints require authentication.** Every `POST` route except `/api/login` —
  `/api/qa-run`, `/api/skill`, `/api/limits`, `/api/batch-eval`, `/api/insights-report`,
  `/api/native-rec`, `/api/native-rec/apply`, `/api/optimize`, `/api/optimize/apply` — requires a
  Cognito access token, validated server-side per request via `cognito-idp:GetUser` (which checks
  signature, expiry and revocation — not a local JWT decode). No token, or an invalid one, gets
  `401`. `POST /api/login` is necessarily public: it's the route that issues the token.
- **The data the dashboard renders is untrusted.** QA findings are written by an LLM describing a
  site under test; branch names, PR titles and workflow names come from the GitHub API and can be
  influenced by any contributor; prompt recommendations are model output. All of it is treated as
  hostile input.
- **What is out of scope:** the AWS credentials and IAM role you deploy with, the harnesses and
  runtimes themselves, and the site your QA agent tests. Review
  [`deploy/iam-policy.json`](deploy/iam-policy.json) before deploying — the Lambda role can call
  AgentCore control/data planes, read CloudWatch and S3, and invoke Bedrock.

## Controls in this codebase

**Output encoding (the primary XSS control).** Every value that reaches the DOM is escaped at the
sink. Three helpers, used according to context, because context determines what's dangerous:

| Helper | Use for | Why not just `esc()` |
|---|---|---|
| `esc()` | text and quoted attribute values | escapes `& < > " '` |
| `jstr()` | values inside an inline handler, e.g. `onclick="applyOpt(${jstr(r.id)})"` | `JSON.stringify` first, so a value containing a quote can't break out of the JS call |
| `safeUrl()` | anything reaching `src` / `href` | escaping does **not** neutralise `javascript:` — there's no escapable character in it — so URLs are allow-listed to `http(s)`/relative and anything else becomes `#` |

Values that land in a CSS class (finding `severity`) are constrained to a known vocabulary rather
than escaped, and numeric fields are coerced with `+x||0`.

**No token at rest.** The Cognito access token is held in an in-memory variable only, never
`localStorage`/`sessionStorage`, so there is nothing on disk for injected script to read and
replay. The cost is that a page reload requires signing in again; read-only browsing needs no
sign-in at all.

**Response headers** on every response: `Content-Security-Policy`, `X-Content-Type-Options:
nosniff`, `X-Frame-Options: SAMEORIGIN` (not `DENY` — the Introduction tab iframes same-origin
`/intro`), `Referrer-Policy`, HSTS. JSON responses also get `Cache-Control: no-store` because they
carry presigned URLs and live run state; HTML stays cacheable.

**No CORS grant by default.** The dashboard is served same-origin by the same Lambda
(`const API = ""`) and auth is a Bearer token rather than cookies, so no cross-origin grant is
needed. Set `ALLOWED_ORIGIN` to an explicit origin — never `*` — only if you host the UI elsewhere.

## Known limitations

Documented rather than hidden, so you can judge them for your own environment:

- **CSP still requires `'unsafe-inline'`.** The page is one self-contained document with an inline
  `<script>` block and 27 `onclick=` attributes. CSP therefore limits exfiltration to a foreign
  host but does **not** block script execution — output encoding above is the real control.
  Removing `'unsafe-inline'` means externalising the script and moving to `addEventListener`.
- **Authorization checks the token, not the identity.** `_authed()` accepts any valid access token
  from the configured pool without comparing the username against an expected admin. This is safe
  only while the pool holds exactly one account: `deploy.sh` and the CDK both set
  `AllowAdminCreateUserOnly` and enable no sign-up or federation flow. **If you add a second user
  to that pool — even a read-only one — fix this first**, or that user gains every write endpoint.
- **`ADMIN_TOKEN` is a break-glass bypass.** If set, a matching `x-admin-token` header authenticates
  writes without Cognito. It has no expiry, no rotation and no per-caller audit trail. Leave it
  unset unless you need it (unset ⇒ the fallback is inert), and treat it as a credential if you do.
- **`deploy/deploy.sh` applies weaker Cognito/API settings than `cdk/stack.py`.** The CDK path sets
  a 20-char password minimum, Cognito Plus-tier threat protection, and a 20 rps / 40 burst throttle
  on the API stage. The shell script — the path the README shows — creates a 12-char minimum, no
  threat protection, and **no throttle on `POST /api/login`**, the one unauthenticated write route.
  If you deploy with the script, consider applying those three yourself.
- **No MFA on the Cognito pool** by default. For a single-admin pool, Plus-tier threat protection
  (which blocks credential-stuffing with breached passwords) is arguably the higher-value control,
  but enable MFA if your exposure warrants it.
- **Editing a live user pool by CLI is hazardous.** `aws cognito-idp update-user-pool` has PUT
  semantics, not PATCH: any field you omit reverts to its default. A single-field call to change the
  tier will silently re-open self-signup by resetting `AllowAdminCreateUserOnly`. Always read the
  current config, merge your change in, send the whole thing, and read it back to confirm.

## Operational notes

- The `admin` password is generated at deploy time into Secrets Manager
  (`agent-admin/dashboard-login`) and never printed. Rotate it in both Cognito and the secret.
- `GITHUB_TOKEN` is optional and only lifts GitHub API rate limits. A read-only token is enough —
  the dashboard never writes to GitHub.
- Presigned S3 URLs for QA screenshots and intro audio are read-only `get_object` URLs scoped to a
  single key in `QA_BUCKET`, expiring in 1 hour.
- The `/intro/audio/` route validates its path against `audio/[a-z]{2,3}/[a-z0-9-]+\.mp3` before
  presigning. The QA report `?prefix=` parameter has no such allow-list — it is passed to S3 as a
  *list prefix*, which cannot address keys outside the bucket, so `?prefix=../../etc` simply matches
  nothing. It is a structural property of prefix listing rather than an input check.
