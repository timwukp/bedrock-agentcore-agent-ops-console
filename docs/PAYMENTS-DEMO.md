# AgentCore Payments — demo architecture (design only, NOT executed)

> **Verdict first (research 2026-08-25, verified against AWS docs).**
> AgentCore Payments moves **real money to external merchants**: agents pay for paid APIs,
> MCP servers, and paywalled content over the x402 protocol or the Machine Payments Protocol
> (MPP), settled **on-chain in USDC** through one of exactly two supported wallet connectors
> (`CoinbaseCDP`, `StripePrivy`). There is **no sandbox/mock mode**.
> It is therefore **NOT an internal chargeback mechanism**. Internal cost visibility and
> cost-center chargeback live in the dashboard's **Cost tab** (cost-allocation tags +
> Cost Explorer + CloudWatch estimates) — see the
> [Cost telemetry section of the README](../README.md#cost-telemetry--where-the-numbers-come-from).
> Keep the two permanently separated in any demo narrative.

Docs: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/payments-how-it-works.html

## Demo scenario (the use case Payments was designed for)

During a QA run, the UI-test agent needs a **paid external capability** — e.g. a premium
accessibility-audit API, a screenshot-diff service, or a paid MCP tool discovered via the
**Coinbase x402 Bazaar** integration in AgentCore Gateway. The merchant answers
`HTTP 402 Payment Required`; AgentCore Payments checks the session spend limit, signs the
payment with the wallet held by the connector, retries with the `X-PAYMENT` header, and the
merchant settles on-chain and returns the content.

Narrative pairing for a demo: **Cost tab = "what our agents spend" (internal visibility);
Payments = "agents autonomously buying external capabilities under a budget" (agent economy)**.

## Architecture

1. **PaymentManager** — top-level resource (AWS_IAM authorizer + IAM role); provisions a
   workload identity in AgentCore Identity.
2. **PaymentConnector** — `CoinbaseCDP` with **Quick create** (recommended): create with
   `provisionMode=QUICK_CREATE`, empty credential list → connector returns an
   `authorizationUrl` (**valid ~10 minutes** — authorize immediately or re-create), then the
   service provisions the credential provider itself. Alternative: `StripePrivy`
   (manual provisioning only — bring your own Privy app credentials).
3. **Payment instrument** — end-user crypto wallet, starts at **0 USDC**; a human funds it
   via the Coinbase wallet hub (card / Apple Pay / ACH / crypto) and **explicitly grants**
   the agent transact permission.
4. **Payment session** — per-interaction context with `maxSpendAmount` + expiry; requests
   beyond the limit are denied.
5. **Gateway target** — register the paid tool/API as a Gateway target (or use the x402
   Bazaar integration) so the agent reaches it through MCP.
6. **Harness wiring** — attach to a **COPY of the UI-test harness** (never the production
   one), so demo spend and behavior stay isolated.

## Safety rails (non-negotiable)

- Fund the wallet with **single-digit dollars** only.
- `maxSpendAmount` set per session (e.g. $0.50) + short session expiry.
- Human-in-the-loop confirmation step before the agent commits a payment (inline function).
- Credentials only via PaymentCredentialProvider / Token Vault (never inline); tight IAM.
- Audit: AgentCore Observability covers the payment lifecycle (transaction success rates,
  spending patterns); review after every demo. Know the revocation path: the wallet hub can
  revoke the agent's permission at any time; delete the session/connector after the demo.
- Never end-to-end test casually — these are real payment rails.

## Future execution checklist (requires an explicit go-ahead — real money)

1. `CreatePaymentManager` (AWS_IAM authorizer + scoped role)
2. `CreatePaymentConnector` type=CoinbaseCDP, provisionMode=QUICK_CREATE → open
   `authorizationUrl` within 10 min → wait for `READY`
3. Create instrument + fund minimal USDC via wallet hub; grant agent permission
4. Register one cheap paid x402 tool as a Gateway target (or pick one from x402 Bazaar)
5. Clone the UI-test harness → attach Gateway + Payments wiring
6. Invoke once with a session `maxSpendAmount`; verify the 402 → sign → retry → 200 flow
7. Verify the transaction in Observability; then revoke permission / clean up

## Optional dashboard surface (phase 2 of the demo)

A read-only **Payments** panel in the dashboard: wallet balance, session spend limit,
recent transactions (from `ListPaymentSessions` / Observability). Read-only by design —
payment mutations stay out of the public dashboard.
