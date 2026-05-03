#!/usr/bin/env node

import { AgentGate, JsonObject } from "./sdk";

const API_KEY = process.env.AGENTGATE_API_KEY ?? "ag_live_demo_key_123";
const BASE_URL = process.env.AGENTGATE_BASE_URL ?? "http://localhost:8000";
const MOCK_SAAS_URL = process.env.MOCK_SAAS_URL ?? "http://localhost:9002/mcp";

function line(): void {
  console.log("=".repeat(64));
}

function getString(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function getObject(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

async function postMock(method: "demo/reset" | "demo/toggle_deployment_drift"): Promise<void> {
  const response = await fetch(MOCK_SAAS_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ method, params: {} }),
  });
  if (!response.ok) {
    throw new Error(`Mock SaaS server did not accept ${method}: ${response.status}`);
  }
}

function releasePolicy(projectId: string, deploymentId: string): JsonObject {
  return {
    intent: "announce_successful_deployment",
    reason: "Release announcements only go out while the deployment is still ready",
    allowed_action: "slack_post_message",
    conditions: { deployment_status: "ready" },
    threshold: "strict",
    state_refetch: {
      tool: "vercel.deployment_status",
      params: {
        project_id: projectId,
        deployment_id: deploymentId,
      },
    },
  };
}

async function run(): Promise<void> {
  const gate = new AgentGate({ apiKey: API_KEY, baseUrl: BASE_URL });
  const projectId = "agentgate-web";
  const deploymentId = "dep_demo_123";

  console.log("");
  line();
  console.log("AGENTGATE DEMO: THE AGENT EXECUTION LAYER");
  line();

  await postMock("demo/reset");

  console.log("\n[ACT 1] Vercel Read With A Transient Failure");
  console.log("Scenario: an agent checks a deployment before announcing a release.");

  const started = Date.now();
  const deployment = await gate.call({
    tool: "vercel.deployment_status",
    params: { project_id: projectId, deployment_id: deploymentId },
    agentId: "release_agent",
    context: { env: "prod", workflowId: "release_coordination" },
  });
  const deploymentResult = getObject(deployment.result);
  const retryCount = getObject(deployment.meta).retry_count;
  console.log("SUCCESS: Vercel returned after AgentGate retried the flaky read.");
  console.log(`Deployment: ${getString(deploymentResult.deployment_status, "unknown")}`);
  console.log(`Retries observed: ${retryCount ?? "tracked in traces"}`);
  console.log(`Latency: ${((Date.now() - started) / 1000).toFixed(2)}s`);

  console.log("\n[ACT 2] Linear Issue Creation");
  console.log("Scenario: the agent creates the engineering follow-up without extra integration glue.");

  const issue = await gate.call({
    tool: "linear.create_issue",
    params: {
      team_id: "ENG",
      title: "Verify release automation after deployment",
      description: `Deployment ${deploymentId} is ${getString(deploymentResult.deployment_status, "unknown")}.`,
    },
    policy: { approval: "skip" },
    agentId: "release_agent",
    context: { env: "prod", workflowId: "release_coordination" },
  });
  const issueResult = getObject(issue.result);
  console.log(`CREATED: ${getString(issueResult.issue_id, "Linear issue")} ${getString(issueResult.url)}`);

  console.log("\n[ACT 3] Slack Approval Then Execution");
  console.log("Scenario: posting to Slack is paused, reviewed, revalidated, then executed.");

  const announcementText = `Release ${deploymentId} is ready. Follow-up: ${getString(issueResult.issue_id, "LIN-101")}`;
  const pending = await gate.call({
    tool: "slack.post_message",
    params: {
      channel: "#release",
      text: announcementText,
    },
    agentId: "release_agent",
    action: "slack_post_message",
    policy: releasePolicy(projectId, deploymentId),
    executionState: {
      state: {
        deployment_status: getString(deploymentResult.deployment_status, "ready"),
      },
      tool_outputs: {
        vercel: deploymentResult,
        linear: issueResult,
      },
    },
    context: { env: "prod", workflowId: "release_coordination" },
  });

  const approvalId = getString(pending.approval_id);
  const approval = getObject(pending.approval);
  console.log(`PAUSED: ${pending.status}`);
  console.log(`Approval ID: ${approvalId}`);
  console.log(`Dashboard: ${getString(approval.dashboard_url, gate.approvalDashboardUrl(true))}`);

  const approved = await gate.approve(approvalId, {
    reviewedBy: "demo_reviewer",
    note: "Release status is ready; send the update.",
  });
  console.log(`APPROVAL RESULT: ${approved.status}`);
  const executedResult = getObject(approved.result);
  if (Object.keys(executedResult).length) {
    console.log(`POSTED: ${getString(executedResult.message_id, "Slack message")}`);
  }

  console.log("\n[ACT 4] Drift Blocks A Stale Slack Action");
  console.log("Scenario: deployment state changes while a second Slack update waits for review.");

  const stalePending = await gate.call({
    tool: "slack.post_message",
    params: {
      channel: "#release",
      text: `Release ${deploymentId} is still ready.`,
    },
    agentId: "release_agent",
    action: "slack_post_message",
    policy: releasePolicy(projectId, deploymentId),
    executionState: {
      state: { deployment_status: "ready" },
    },
    context: { env: "prod", workflowId: "release_coordination" },
  });
  const staleApprovalId = getString(stalePending.approval_id);
  console.log(`PAUSED: ${staleApprovalId}`);

  await postMock("demo/toggle_deployment_drift");
  console.log("SIMULATED: Vercel deployment changed from ready to failed.");

  const staleApproved = await gate.approve(staleApprovalId, {
    reviewedBy: "demo_reviewer",
    note: "Trying to approve stale release update.",
  });

  console.log(`APPROVAL RESULT: ${staleApproved.status}`);
  if (staleApproved.status === "requeued") {
    console.log(`Reason: ${staleApproved.requeue_reason}`);
    const decision = getObject(staleApproved.decision);
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
  console.log("1. Agent code made normal Vercel, Linear, and Slack calls through one interface.");
  console.log("2. AgentGate recovered the flaky read and traced the whole workflow.");
  console.log("3. Slack execution paused, revalidated live deployment state, and blocked stale work.");
  line();
}

run().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
});
