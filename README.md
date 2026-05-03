# AgentGate

Run one command. Watch your agent fail... and still succeed.

```bash
npx agentgate demo
```

Most agents break in production because:

- APIs fail
- retries loop
- state changes mid-execution

AgentGate sits between your agent and tools and handles it.

Agents are good at reasoning.
They are bad at execution.

AgentGate fixes execution.

## Get Started

```bash
git clone https://github.com/joel4893/agentgate-cli.git
cd agentgate-cli
npm install
npm run demo
```

That is it.

The demo starts a local AgentGate gateway, runs mock tools, and shows:

1. Agent calls tool.
2. Tool fails.
3. AgentGate retries and recovers.
4. Risky action pauses for approval.
5. State changes before approval.
6. AgentGate revalidates before execution.
7. Every tool call is traced.

Once the package is published, this should become:

```bash
npx agentgate demo
```

## What To Notice

Direct tool calls give you a raw request and response.

AgentGate turns each agent action into an execution transaction:

- identify the agent and execution context
- validate the tool input schema
- check scoped permissions
- evaluate policy
- retry flaky tools
- pause risky actions for approval
- revalidate state before approved actions execute
- write structured traces

## Want This In Your Setup?

Plug in your API.

AgentGate is designed to turn APIs, MCP servers, and internal workflows into agent-ready tools.

The current demo uses mock tools. The intended next step is:

```bash
agentgate wrap openapi ./openapi.yaml
agentgate deploy
```

## TypeScript SDK

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

## CLI Commands

```bash
npm run demo      # build CLI, start stack, run the guided demo
npm run dev       # build CLI and start the stack in the foreground
npm run down      # stop the local stack
npm run doctor    # check Docker, Docker Compose, and Node.js
```

If you want to call the local binary directly:

```bash
npm run build
npm exec -- gate demo
```

## Useful URLs

With the demo running:

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

## Core Call Shape

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

The hosted onboarding flow, published npm package, and production credential management are separate product surfaces.
