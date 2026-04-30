# AgentGate

Give your agent real-world capabilities in one API.

AgentGate is the runtime API for agents to discover and call tools reliably. Point your agent at AgentGate and give it one interface for tool discovery, authentication, invocation, retries, approvals, and audit logs. No brittle MCP wiring. No one-off auth glue. No blind side effects.

## Why AgentGate

### Agents should discover tools, not hard-code integrations

Most agent stacks make you hand-pick a fixed set of tools before the agent starts. AgentGate exposes agent-readable capability cards through `/tools` and `/discover`, so an agent can search for the right capability, inspect schemas, understand risk, and call the tool through one runtime API.

### Real-world actions need a control plane

Fetching a webpage is low risk. Creating a GitHub issue, posting to Slack, editing a CRM, or sending an invoice is different. AgentGate routes risky calls through approval gates, freezes execution state, revalidates policy when a human approves, and records what happened.

### Tool calls should be reliable by default

Raw tool calls fail in boring ways: flaky MCP servers, missing session IDs, timeouts, inconsistent errors, auth drift. AgentGate sits between agents and tools with retries, structured errors, scoped API keys, direct HTTP fallback where useful, and JSON traces you can inspect.

### Tool providers should onboard like API providers

`agentgate wrap` turns PostgreSQL queries, OpenAPI services, and launch SaaS connectors into AgentGate-ready MCP wrappers. Each wrapper includes schemas, capability metadata, idempotency hints, retry hints, and a registration payload.

## Install

Run AgentGate from any project with `npx`:

```bash
npx @agentgate/cli wrap github
```

Or install it once:

```bash
npm install -g @agentgate/cli
agentgate --help
```

Other package managers work too:

```bash
yarn dlx @agentgate/cli wrap github
pnpm dlx @agentgate/cli wrap github
bunx @agentgate/cli wrap github
```

The npm package ships a dependency-free Node launcher for the same wrapper generator used by the Python CLI. It requires Python 3 on the machine running the command. Set `AGENTGATE_PYTHON=/path/to/python` if auto-detection needs a nudge.

From this repo checkout, test the local npm binary with:

```bash
npm exec -- agentgate --help
```

## Quick Start

### 1. Start AgentGate

Create and load a local API key:

```bash
source .env
```

Start the gateway:

```bash
python main.py
```

If port 8000 is busy:

```bash
PORT=8001 python main.py
```

### 2. Discover available tools

```python
from sdk import trace

tools = trace.discover("fetch a webpage for research")
print(tools["tools"][0]["id"])
```

Or use HTTP directly:

```bash
curl "http://localhost:8000/discover?q=web%20research" \
  -H "X-API-Key: $TRACE_API_KEY"
```

### 3. Call a tool

```python
from sdk import trace

result = trace.call(
    tool="fetch.url",
    params={"url": "https://github.com", "max_length": 1200},
    agent_id="research_agent_v1",
)

print(result["result"]["content"][0]["text"])
```

### 4. Require approval for risky work

```python
from sdk import trace

pending = trace.call(
    tool="fetch.url",
    params={"url": "https://github.com"},
    agent_id="research_agent_v1",
    policy={"approval": "required", "reason": "Demo approval gate"},
)

approval_id = pending["approval_id"]
result = trace.approve(approval_id, reviewed_by="joel")
print(result["status"])
```

That's it. Your agent can discover tools, call them through one gateway, and route sensitive actions through human review.

## What You Can Connect

AgentGate ships with wrapper generation for common launch paths:

| Command | What it creates |
| --- | --- |
| `agentgate wrap github` | GitHub MCP wrapper with approval-ready issue creation metadata |
| `agentgate wrap notion` | Notion MCP wrapper with AgentGate capability metadata |
| `agentgate wrap linear` | Linear MCP wrapper for issue creation workflows |
| `agentgate wrap postgresql --query "SELECT ..."` | Scoped PostgreSQL query tool with inferred input schema |
| `agentgate wrap billing-api --openapi openapi.json --base-url https://billing.example.com` | OpenAPI-backed wrapper for an HTTP service |

Or start from an existing MCP server and register its capability card with AgentGate.

## How It Works

```text
Agents discover tools       AgentGate runtime API          Tools and services
+------------------+        +----------------------+      +------------------+
| /tools           |        | Auth                 |      | MCP servers      |
| /discover        |------->| Retries              |----->| SaaS APIs        |
| /call            |        | Approval gates       |      | OpenAPI apps     |
| SDK / HTTP       |<-------| Audit traces         |<-----| PostgreSQL       |
+------------------+        +----------------------+      +------------------+
```

Define - Wrap a service with `agentgate wrap` or register a provider tool with a capability card, schema, risk level, and MCP URL.

Discover - Agents call `/tools` or `/discover` to find the right capability instead of hard-coding every integration.

Invoke - Agents call `/call` with tool params. AgentGate handles auth, retries, structured errors, and trace logging.

Approve - Policy can return `pending_approval`. A reviewer approves or rejects in the API or dashboard, then AgentGate revalidates state before executing.

## Build A Tool Wrapper

Create launch connector wrappers:

```bash
agentgate wrap github
agentgate wrap notion
agentgate wrap linear
```

Wrap a PostgreSQL query:

```bash
agentgate wrap postgresql \
  --query "SELECT * FROM invoices WHERE id = :invoice_id" \
  --name "invoice lookup"
```

Wrap an OpenAPI service:

```bash
agentgate wrap billing-api \
  --openapi openapi.json \
  --base-url https://billing.example.com
```

This creates a runnable MCP wrapper under `wrapped_tools/` with:

- capability metadata
- JSON schema validation
- structured JSON-RPC errors
- idempotency hints
- retry hints
- `agentgate.register.json` for provider onboarding

The Python entrypoint remains available for local development:

```bash
python agentify.py wrap github
```

## Runtime API

### Connect Hosted MCP Gateway

```python
from sdk import trace

connected = trace.connect(
    "github",
    owner_email="dev@acme.example",
    approval_email="ops@acme.example",
)

print(connected["gateway_url"])
print(connected["tools"][0]["key"])
```

The gateway returns a hosted endpoint such as `https://github.agentgate.dev` plus an MCP URL and preloaded launch-tool metadata.

### TypeScript SDK

```ts
import { Trace } from "./sdk";

const trace = new Trace({ apiKey: process.env.TRACE_API_KEY! });

const result = await trace.call({
  tool: "fetch.url",
  params: { url: "https://github.com" },
  agentId: "research_agent_v1",
});

const connected = await trace.connect({
  saas: "linear",
  ownerEmail: "dev@acme.example",
});
```

### HTTP

All runtime endpoints require `X-API-Key`.

List registered tools:

```bash
curl http://localhost:8000/tools \
  -H "X-API-Key: $TRACE_API_KEY"
```

Call a tool:

```bash
curl http://localhost:8000/call \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TRACE_API_KEY" \
  -d '{
    "tool": "fetch.url",
    "params": {"url": "https://github.com"},
    "agent_id": "research_agent_v1"
  }'
```

Connect a launch SaaS and get its hosted gateway:

```bash
curl -X POST http://localhost:8000/connect/github \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TRACE_API_KEY" \
  -d '{"owner_email": "dev@acme.example"}'
```

## Approval Gates

Approval checkpoints carry an execution snapshot: params, variables, tool outputs, action, policy contract, and timestamps. Approval revalidates policy against fresh state before execution and returns one of `executed`, `cancelled`, or `replan_required`.

```python
from sdk import trace

policy = {
    "rules": [
        {
            "when": "action == git_push and branch == main",
            "effect": "require_approval",
            "intent": "push_to_main",
            "allowed_action": "git_push",
            "reason": "Human approval required for pushes to main",
        }
    ]
}

pending = trace.call(
    tool="fetch.url",
    params={"url": "https://example.com/repo", "branch": "main"},
    agent_id="dev_agent_v1",
    action="git_push",
    policy=policy,
    execution_state={
        "variables": {"branch": "main"},
        "tool_outputs": {"diff_summary": {"files_changed": 3}},
    },
)
```

A domain policy can freeze intent-specific work:

```json
{
  "intent": "send_invoice_reminder",
  "condition": "invoice_status == overdue",
  "allowed_action": "send_email",
  "expires_at": "2026-04-30T15:00:00Z"
}
```

When the human approves, AgentGate can accept fresh state from the approver or run a configured `state_refetch` read tool. It then thaws the checkpoint, validates `condition`, checks `allowed_action` and `expires_at`, and decides whether to execute, cancel, or ask the agent to re-plan.

Open the approval dashboard:

```bash
open "http://localhost:8000/dashboard/approvals?api_key=$TRACE_API_KEY"
```

For approval notifications, set:

```bash
export APPROVAL_SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export APPROVAL_EMAIL_WEBHOOK_URL="https://email-webhook.example/send"
```

## CLI Commands

The current CLI focuses on wrapper generation:

```bash
agentgate wrap github
agentgate wrap notion
agentgate wrap linear
agentgate wrap postgresql --query "SELECT * FROM users WHERE id = :user_id"
agentgate wrap billing-api --openapi openapi.json --base-url http://localhost:8000
```

The generated wrapper can then be registered with AgentGate using its `agentgate.register.json` payload.

## Configuration

Local server environment variables:

| Variable | Description |
| --- | --- |
| `TRACE_API_KEYS` | Comma-separated full-access server API keys |
| `AGENTGATE_API_KEYS` | Backward-compatible server API key env var |
| `TRACE_API_KEY_SCOPES` | JSON scoped-token config for tool allowlists and scopes |
| `AGENTGATE_API_KEY_SCOPES` | Backward-compatible scoped-token env var |
| `TRACE_LOG_FILE` | Override JSON audit log filename |
| `AGENTGATE_LOG_FILE` | Backward-compatible audit log filename |
| `HOST` | Server bind host, default `0.0.0.0` |
| `PORT` | Server bind port, default `8000` |

Client environment variables:

| Variable | Description |
| --- | --- |
| `TRACE_API_KEY` | API key used by the Python and TypeScript SDKs |
| `AGENTGATE_API_KEY` | Backward-compatible client API key env var |
| `TRACE_BASE_URL` | SDK base URL override |
| `AGENTGATE_BASE_URL` | Backward-compatible SDK base URL override |

Scoped token example:

```bash
export TRACE_API_KEY_SCOPES='{
  "ag_scoped_fetch_read": {
    "scopes": ["tools:read", "tools:call", "traces:read"],
    "allowed_tools": ["fetch.url"],
    "read_only": true,
    "agent_id": "research_agent_v1"
  }
}'
```

Supported scopes today: `tools:read`, `tools:call`, `approvals:read`, `approvals:write`, `logs:read`, `traces:read`, and `providers:admin`.

## Observability

List recent tool-call traces:

```bash
curl http://localhost:8000/traces \
  -H "X-API-Key: $TRACE_API_KEY"
```

Export traces:

```bash
curl "http://localhost:8000/traces/export?format=langsmith" \
  -H "X-API-Key: $TRACE_API_KEY"
```

Local logs:

- JSON audit logs: `logs/trace.log`
- tool-call traces: `logs/tool_calls.jsonl`
- trace export formats: JSON, JSONL, LangSmith-shaped, and Helicone-shaped records

## Reliable Invocation Demo

There is a demo harness that starts a flaky MCP simulator which fails the first N `tools/call` attempts, then succeeds. It demonstrates AgentGate's retry behavior.

```bash
FLAKY_FAIL_FIRST_N=2 ./demo/run_demo.sh
```

The demo starts the simulator, starts the gateway, runs `test_agent.py`, prints logs, and cleans up processes.

## What Exists Today

- API-key authentication
- scoped API tokens with tool allowlists and read-only checks
- npm/npx CLI package with `agentgate wrap`
- `agentgate wrap` generator for OpenAPI, GitHub, Notion, Linear, and PostgreSQL MCP wrappers
- hosted gateway URL metadata for connected SaaS tools
- agent-readable tool registry
- `/tools` capability cards
- `/discover` capability search
- provider onboarding with registered tools available in discovery and calls
- `/call` reliable tool invocation
- policy-as-code `pending_approval` responses
- frozen execution checkpoints with variables and tool outputs
- `/approvals` approval queue
- `/dashboard/approvals` web dashboard for human review
- Slack and email-webhook approval notifications
- approve/reject plus thaw-time execute, cancel, or re-plan decisions
- MCP Streamable HTTP support
- direct HTTP fallback for `fetch.url`
- JSON audit logs in `logs/trace.log`
- tool-call traces in `logs/tool_calls.jsonl`
- `/traces/export` for JSON, JSONL, LangSmith-shaped, and Helicone-shaped records

## Direction

AgentGate is moving toward the runtime layer for real-world agent capabilities:

- scoped agent identities
- approval gates for risky actions
- provider onboarding and wrapper templates
- richer capability search
- usage metering and policy controls
- dashboard-grade observability
