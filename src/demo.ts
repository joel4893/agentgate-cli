#!/usr/bin/env node

import { AgentGate, JsonObject } from "./sdk";

const API_KEY = process.env.AGENTGATE_API_KEY ?? "ag_live_demo_key_123";
const BASE_URL = process.env.AGENTGATE_BASE_URL ?? "http://localhost:8000";
const FLAKY_API_URL = process.env.FLAKY_API_URL ?? "http://localhost:9000/mcp";

function line(): void {
  console.log("=".repeat(64));
}

function getString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function getObject(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

async function toggleDrift(): Promise<void> {
  await fetch(FLAKY_API_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ method: "demo/toggle_drift", params: {} }),
  });
}

async function run(): Promise<void> {
  const gate = new AgentGate({ apiKey: API_KEY, baseUrl: BASE_URL });

  console.log("");
  line();
  console.log("AGENTGATE DEMO: THE AGENT EXECUTION LAYER");
  line();

  console.log("\n[ACT 1] Invisible Reliability");
  console.log("Scenario: an agent calls a flaky tool with one SDK call.");

  const started = Date.now();
  const fetchResult = await gate.call({
    tool: "fetch.url",
    params: { url: "https://agentgate.dev" },
    agentId: "reliability_agent",
    context: { env: "dev", workflowId: "demo_research" },
  });
  console.log("SUCCESS: AgentGate handled backend retries.");
  console.log(`Latency: ${((Date.now() - started) / 1000).toFixed(2)}s`);
  console.log(`Tool result keys: ${Object.keys(getObject(fetchResult.result)).join(", ")}`);

  console.log("\n[ACT 2] Risky Action Checkpoint");
  console.log("Scenario: an agent tries to create a GitHub issue.");

  const pending = await gate.call({
    tool: "github.create_issue",
    params: {
      repo: "acme/corp",
      title: "Critical bug found by agent",
      body: "Demo issue created through AgentGate.",
    },
    agentId: "dev_agent",
    context: { env: "prod", workflowId: "issue_triage" },
  });

  const approvalId = getString(pending.approval_id);
  const approval = getObject(pending.approval);
  console.log(`PAUSED: ${pending.status}`);
  console.log(`Approval ID: ${approvalId}`);
  console.log(`Dashboard: ${getString(approval.dashboard_url, gate.approvalDashboardUrl(true))}`);

  console.log("\n[ACT 3] State Drift Revalidation");
  console.log("Scenario: state changes while a human is reviewing the request.");
  await toggleDrift();
  console.log("SIMULATED: system status changed before approval.");

  const approved = await gate.approve(approvalId, {
    reviewedBy: "demo_reviewer",
    note: "Looks good from the dashboard.",
  });

  console.log(`Approval result: ${approved.status}`);
  if (approved.status === "requeued") {
    console.log(`Reason: ${approved.requeue_reason}`);
    const decision = getObject(approved.decision);
    const drift = getObject(decision.drift);
    const checks = Array.isArray(drift.checks) ? drift.checks : [];
    for (const check of checks) {
      const item = getObject(check);
      if (item.changed || item.condition_passed === false) {
        console.log(`Drift: ${item.path} ${item.approval_value} -> ${item.current_value}`);
      }
    }
  }

  console.log("");
  line();
  console.log("SUMMARY");
  console.log("1. Agent code stays simple.");
  console.log("2. Tool execution gets retries, policy, approvals, and traces.");
  console.log("3. Approval is a revalidation boundary, not a blind resume.");
  line();
}

run().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
});
