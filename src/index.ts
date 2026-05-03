#!/usr/bin/env node

import { spawn, spawnSync } from "node:child_process";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const API_KEY = "ag_live_demo_key_123";
const BASE_URL = process.env.AGENTGATE_BASE_URL ?? "http://localhost:8000";
const PACKAGE_ROOT = path.resolve(__dirname, "..");

type CommandResult = {
  code: number | null;
};

function printHelp(): void {
  console.log(`
AgentGate CLI

Usage:
  agentgate demo      Start the local AgentGate stack and run the guided demo
  agentgate dev       Start the local AgentGate stack in the foreground
  agentgate down      Stop the local Docker demo stack
  agentgate doctor    Check local prerequisites
  agentgate help      Show this help

Examples:
  npx agentgate demo
  npx agentgate down
`);
}

function commandExists(command: string): boolean {
  const result = spawnSync(command, ["--version"], { stdio: "ignore" });
  return result.status === 0;
}

function run(
  command: string,
  args: string[],
  options: { detached?: boolean; cwd?: string } = {},
): Promise<CommandResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: "inherit",
      shell: process.platform === "win32",
      detached: options.detached ?? false,
      cwd: options.cwd,
    });

    child.on("error", reject);
    child.on("close", (code) => resolve({ code }));
  });
}

function runQuiet(command: string, args: string[]): CommandResult {
  const result = spawnSync(command, args, {
    stdio: "ignore",
    shell: process.platform === "win32",
  });
  return { code: result.status };
}

async function waitForGateway(timeoutMs = 60000): Promise<void> {
  const started = Date.now();
  const url = `${BASE_URL}/tools`;

  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url, {
        headers: { "X-API-Key": API_KEY },
      });
      if (response.ok) {
        return;
      }
    } catch {
      // Service is still booting.
    }
    await delay(1000);
  }

  throw new Error(`AgentGate did not become ready at ${BASE_URL} within ${Math.round(timeoutMs / 1000)}s`);
}

async function doctor(): Promise<number> {
  console.log("Checking local Gate demo requirements...\n");

  const checks = [
    ["Docker", commandExists("docker")],
    ["Docker Compose", runQuiet("docker", ["compose", "version"]).code === 0],
    ["Node.js", commandExists("node")],
  ] as const;

  for (const [name, ok] of checks) {
    console.log(`${ok ? "OK " : "NO "} ${name}`);
  }

  const failed = checks.filter(([, ok]) => !ok);
  if (failed.length) {
    console.log("\nInstall the missing tools, then run `npx agentgate demo` again.");
    return 1;
  }

  console.log("\nAll set. Running the demo now.");
  return 0;
}

async function dev(): Promise<number> {
  console.log("Starting AgentGate demo stack in the foreground...\n");
  const result = await run("docker", ["compose", "up", "--build"], { cwd: PACKAGE_ROOT });
  return result.code ?? 1;
}

async function down(): Promise<number> {
  console.log("Stopping AgentGate demo stack...\n");
  const result = await run("docker", ["compose", "down", "--remove-orphans"], { cwd: PACKAGE_ROOT });
  return result.code ?? 1;
}

async function demo(): Promise<number> {
  const doctorCode = await doctor();
  if (doctorCode !== 0) {
    return doctorCode;
  }

  console.log("\nStarting AgentGate demo stack in the background...\n");
  await run("docker", ["compose", "down", "--remove-orphans"], { cwd: PACKAGE_ROOT });
  const up = await run("docker", ["compose", "up", "--build", "-d"], { cwd: PACKAGE_ROOT });
  if (up.code !== 0) {
    return up.code ?? 1;
  }

  console.log("\nWaiting for AgentGate to become ready...");
  await waitForGateway();

  console.log("\nRunning guided demo...\n");
  const demoPath = path.join(__dirname, "demo.js");
  const result = await run(process.execPath, [demoPath], { cwd: PACKAGE_ROOT });

  console.log(`\nApproval dashboard: ${BASE_URL}/dashboard/approvals?api_key=${API_KEY}`);
  console.log(`Traces: ${BASE_URL}/traces`);
  console.log("\nStop the stack with: npx agentgate down");

  return result.code ?? 1;
}

async function main(): Promise<void> {
  const command = process.argv[2] ?? "help";

  try {
    let code = 0;
    if (command === "help" || command === "--help" || command === "-h") {
      printHelp();
    } else if (command === "doctor") {
      code = await doctor();
    } else if (command === "dev") {
      code = await dev();
    } else if (command === "down") {
      code = await down();
    } else if (command === "demo") {
      code = await demo();
    } else {
      console.error(`Unknown command: ${command}`);
      printHelp();
      code = 1;
    }
    process.exitCode = code;
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  }
}

void main();
