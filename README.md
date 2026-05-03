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
npx agentgate demo
```

That is it.

The demo starts a local AgentGate gateway, runs mock tools, and shows:

1. Agent checks a Vercel deployment.
2. The Vercel read fails once.
3. AgentGate retries and recovers.
4. Agent creates a Linear follow-up issue.
5. Agent prepares a Slack release update.
6. Slack posting pauses for approval.
7. AgentGate revalidates live deployment state before execution.
8. A stale Slack update gets requeued instead of blindly posting.
9. Every tool call is traced.

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
import { AgentGate } from "agentgate";

const gate = new AgentGate({
  apiKey: "ag_live_demo_key_123",
  baseUrl: "http://localhost:8000",
});

const result = await gate.call({
  tool: "vercel.deployment_status",
  params: {
    project_id: "agentgate-web",
    deployment_id: "dep_demo_123",
  },
  agentId: "release_agent",
  context: { env: "prod", workflowId: "release_coordination" },
});
```

## CLI Commands

```bash
npx agentgate demo # start the local stack and run the guided demo

npm run demo      # build CLI, start stack, run the guided demo
npm run dev       # build CLI and start the stack in the foreground
npm run down      # stop the local stack
npm run doctor    # check Docker, Docker Compose, and Node.js
```

If you want to call the local binary directly:

```bash
npm run build
npm exec -- agentgate demo
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
import { AgentGate } from "agentgate";

const gate = new AgentGate({
  apiKey: "ag_live_demo_key_123",
  baseUrl: "http://localhost:8000",
});

const result = await gate.call({
  tool: "linear.create_issue",
  params: {
    team_id: "ENG",
    title: "Verify release automation after deployment",
    description: "Found by an agent",
  },
  agentId: "release_agent",
  context: { env: "prod", workflowId: "release_coordination" },
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

- Act 1: AgentGate handles a flaky Vercel deployment read.
- Act 2: AgentGate creates a Linear issue through the same call interface.
- Act 3: AgentGate pauses a Slack post, approves it, and executes after state revalidation.
- Act 4: AgentGate detects Vercel state drift and prevents stale Slack execution.

## Why This Exists

Agents should not operate software through brittle browser flows or one-off glue code.

They need machine-readable execution interfaces: APIs, MCPs, CLIs, schemas, scoped permissions, policies, and logs.

AgentGate is the agent-first execution layer for that world.

## Status

This is a local technical preview for developers. It is intentionally small so you can understand the runtime in a few minutes.

The hosted onboarding flow, published npm package, and production credential management are separate product surfaces.
