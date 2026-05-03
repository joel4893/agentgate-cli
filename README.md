# Gate CLI

Gate is the TypeScript CLI for AgentGate: turn APIs and MCP servers into agent-ready tools in minutes.

This repo is a focused technical preview of the AgentGate runtime. It gives you one command to run a local demo of what happens between an AI agent requesting an action and that action actually executing: schema checks, scoped execution, retries, approval checkpoints, state revalidation, and traces.

## What You Will See

The demo runs a local AgentGate gateway plus two mock tools:

- a flaky MCP tool that fails several times before succeeding
- a mock GitHub MCP tool that represents a real-world write action

Then `gate demo` shows three moments:

1. An agent calls a flaky tool with one simple SDK call.
2. AgentGate retries behind the scenes so the agent code stays clean.
3. A risky action pauses for approval, then AgentGate revalidates live state before execution.

## Quick Start

Clone the repo:

```bash
git clone https://github.com/joel4893/agentgate-cli.git
cd agentgate-cli
```

Install and build the TypeScript CLI:

```bash
npm install
npm run build
```

Run the guided demo:

```bash
npm exec -- gate demo
```

That command starts the local Docker stack, waits for AgentGate to become ready, and runs the demo script.

## CLI Commands

```bash
npm exec -- gate demo      # start stack in background and run the guided demo
npm exec -- gate dev       # start stack in the foreground
npm exec -- gate down      # stop the local stack
npm exec -- gate doctor    # check Docker, Docker Compose, and Node.js
```

The TypeScript SDK is exported from the package:

```ts
import { AgentGate } from "@agentgate/cli";

const gate = new AgentGate({
  apiKey: "ag_live_demo_key_123",
  baseUrl: "http://localhost:8000",
});

const result = await gate.call({
  tool: "fetch.url",
  params: { url: "https://agentgate.dev" },
  agentId: "research_agent",
  context: { env: "dev", workflowId: "demo" },
});
```

## The Core Idea

Calling a tool directly gives you a raw request and response.

Calling a tool through AgentGate turns it into an execution transaction:

```ts
import { AgentGate } from "@agentgate/cli";

const gate = new AgentGate({
  apiKey: "ag_live_demo_key_123",
  baseUrl: "http://localhost:8000",
});

const result = await gate.call({
  tool: "github.create_issue",
  params: {
    repo: "acme/app",
    title: "Fix login callback race",
    body: "Found by an agent",
  },
  agentId: "dev_agent",
  context: { env: "prod", workflowId: "issue_triage" },
});
```

In the gap between `gate.call(...)` and the actual tool action, AgentGate can:

- identify the agent and execution context
- validate the tool input schema
- check scoped permissions
- evaluate policy
- retry flaky tools
- pause risky actions for approval
- revalidate state before approved actions execute
- write structured traces

## Useful URLs

With `docker compose up --build` running:

- AgentGate API: `http://localhost:8000`
- Approval dashboard: `http://localhost:8000/dashboard/approvals?api_key=ag_live_demo_key_123`
- Tool discovery: `http://localhost:8000/tools`
- Traces: `http://localhost:8000/traces`

For authenticated API calls, use:

```bash
X-API-Key: ag_live_demo_key_123
```

Example:

```bash
curl http://localhost:8000/tools \
  -H "X-API-Key: ag_live_demo_key_123"
```

## Python Fallback

If you want to run the pieces manually:

```bash
docker compose up --build
```

In another terminal:

```bash
python -m pip install -r requirements.txt
python demo.py
```

If `python` points to Python 2 on your machine, use:

```bash
python3 -m pip install -r requirements.txt
python3 demo.py
```

Expected story:

- Act 1: AgentGate handles flaky tool retries.
- Act 2: AgentGate pauses a risky GitHub issue creation.
- Act 3: AgentGate detects state drift and prevents stale execution.

## Why This Exists

Agents should not operate software through brittle browser flows or one-off glue code.

They need machine-readable execution interfaces: APIs, MCPs, CLIs, schemas, scoped permissions, policies, and logs.

AgentGate is the agent-first execution layer for that world.

## Status

This is a local technical preview for developers. It is intentionally small so you can understand the runtime in a few minutes.

The hosted onboarding flow, packaged CLI, and production credential management are separate product surfaces.
