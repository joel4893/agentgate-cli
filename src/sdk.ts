export type JsonObject = Record<string, unknown>;

export type AgentGateOptions = {
  apiKey: string;
  baseUrl?: string;
};

export type ExecutionContext = {
  env?: string;
  workflowId?: string;
  workflowRunId?: string;
  userId?: string;
  workspaceId?: string;
  resources?: JsonObject;
  labels?: JsonObject;
  traceId?: string;
};

export type CallOptions = {
  tool: string;
  params: JsonObject;
  agentId?: string;
  policy?: JsonObject;
  executionState?: JsonObject;
  action?: string;
  failurePolicy?: JsonObject;
  entityResolution?: JsonObject;
  context?: ExecutionContext;
  idempotencyKey?: string;
};

export type ConnectOptions = {
  saas: string;
  ownerEmail?: string;
  name?: string;
  approvalSlackWebhookUrl?: string;
  approvalEmail?: string;
};

export type VerifyStateOptions = {
  intent: string;
  requiredFields: string[];
  assumedState?: JsonObject;
  currentState?: JsonObject;
  stateRefetch?: JsonObject;
  conditions?: JsonObject;
  threshold?: string;
  onMismatch?: string;
  params?: JsonObject;
  agentId?: string;
};

export class AgentGate {
  private readonly apiKey: string;
  private readonly baseUrl: string;

  constructor(options: AgentGateOptions) {
    if (!options.apiKey || !options.apiKey.trim()) {
      throw new Error("AgentGate requires a non-empty API key");
    }
    this.apiKey = options.apiKey.trim();
    this.baseUrl = (options.baseUrl ?? "http://localhost:8000").replace(/\/+$/, "");
  }

  static fromEnv(): AgentGate {
    const apiKey = process.env.TRACE_API_KEY ?? process.env.AGENTGATE_API_KEY;
    const baseUrl = process.env.TRACE_BASE_URL ?? process.env.AGENTGATE_BASE_URL;
    if (!apiKey) {
      throw new Error("Set TRACE_API_KEY or AGENTGATE_API_KEY, or pass { apiKey } to AgentGate");
    }
    return new AgentGate({ apiKey, baseUrl });
  }

  approvalDashboardUrl(apiKeyInQuery = false): string {
    return `${this.baseUrl}/dashboard/approvals${apiKeyInQuery ? `?api_key=${encodeURIComponent(this.apiKey)}` : ""}`;
  }

  private headers(): HeadersInit {
    return {
      "Content-Type": "application/json",
      "X-API-Key": this.apiKey,
    };
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        ...this.headers(),
        ...(init.headers ?? {}),
      },
    });
    if (!response.ok) {
      let detail: unknown;
      try {
        detail = await response.json();
      } catch {
        detail = await response.text();
      }
      throw new Error(`AgentGate error ${response.status}: ${JSON.stringify(detail)}`);
    }
    return response.json() as Promise<T>;
  }

  call(options: CallOptions): Promise<JsonObject> {
    return this.request<JsonObject>("/call", {
      method: "POST",
      body: JSON.stringify({
        tool: options.tool,
        params: options.params,
        agent_id: options.agentId ?? "default_agent",
        ...(options.policy ? { policy: options.policy } : {}),
        ...(options.executionState ? { execution_state: options.executionState } : {}),
        ...(options.action ? { action: options.action } : {}),
        ...(options.failurePolicy ? { failure_policy: options.failurePolicy } : {}),
        ...(options.entityResolution ? { entity_resolution: options.entityResolution } : {}),
        ...(options.context ? { context: normalizeContext(options.context) } : {}),
        ...(options.idempotencyKey ? { idempotency_key: options.idempotencyKey } : {}),
      }),
    });
  }

  listTools(): Promise<JsonObject> {
    return this.request<JsonObject>("/tools");
  }

  discover(query: string, limit = 10): Promise<JsonObject> {
    const params = new URLSearchParams({ q: query, limit: String(limit) });
    return this.request<JsonObject>(`/discover?${params.toString()}`);
  }

  connect(options: ConnectOptions): Promise<JsonObject> {
    return this.request<JsonObject>(`/connect/${encodeURIComponent(options.saas)}`, {
      method: "POST",
      body: JSON.stringify({
        owner_email: options.ownerEmail ?? "owner@example.com",
        ...(options.name ? { name: options.name } : {}),
        ...(options.approvalSlackWebhookUrl ? { approval_slack_webhook_url: options.approvalSlackWebhookUrl } : {}),
        ...(options.approvalEmail ? { approval_email: options.approvalEmail } : {}),
      }),
    });
  }

  listApprovals(): Promise<JsonObject> {
    return this.request<JsonObject>("/approvals");
  }

  getApproval(approvalId: string): Promise<JsonObject> {
    return this.request<JsonObject>(`/approvals/${encodeURIComponent(approvalId)}`);
  }

  approve(
    approvalId: string,
    body: { reviewedBy?: string; note?: string; executionState?: JsonObject; state?: JsonObject } = {},
  ): Promise<JsonObject> {
    return this.request<JsonObject>(`/approvals/${encodeURIComponent(approvalId)}/approve`, {
      method: "POST",
      body: JSON.stringify({
        reviewed_by: body.reviewedBy ?? "human",
        note: body.note ?? "",
        ...(body.executionState ? { execution_state: body.executionState } : {}),
        ...(body.state ? { state: body.state } : {}),
      }),
    });
  }

  reject(approvalId: string, reviewedBy = "human", note = ""): Promise<JsonObject> {
    return this.request<JsonObject>(`/approvals/${encodeURIComponent(approvalId)}/reject`, {
      method: "POST",
      body: JSON.stringify({ reviewed_by: reviewedBy, note }),
    });
  }

  verifyState(options: VerifyStateOptions): Promise<JsonObject> {
    return this.request<JsonObject>("/state/verify", {
      method: "POST",
      body: JSON.stringify({
        intent: options.intent,
        required_fields: options.requiredFields,
        threshold: options.threshold ?? "strict",
        on_mismatch: options.onMismatch ?? "abort",
        agent_id: options.agentId ?? "default_agent",
        ...(options.assumedState ? { assumed_state: options.assumedState } : {}),
        ...(options.currentState ? { current_state: options.currentState } : {}),
        ...(options.stateRefetch ? { state_refetch: options.stateRefetch } : {}),
        ...(options.conditions ? { conditions: options.conditions } : {}),
        ...(options.params ? { params: options.params } : {}),
      }),
    });
  }

  reconcile(action: JsonObject | string): Promise<JsonObject> {
    return this.request<JsonObject>("/outcomes/reconcile", {
      method: "POST",
      body: JSON.stringify(typeof action === "string" ? { action } : action),
    });
  }

  traces(limit = 100): Promise<JsonObject> {
    return this.request<JsonObject>(`/traces?${new URLSearchParams({ limit: String(limit) }).toString()}`);
  }

  async exportTraces(format = "json", limit = 100): Promise<JsonObject | string> {
    const params = new URLSearchParams({ format, limit: String(limit) });
    const response = await fetch(`${this.baseUrl}/traces/export?${params.toString()}`, {
      headers: this.headers(),
    });
    if (!response.ok) {
      throw new Error(`AgentGate error ${response.status}: ${await response.text()}`);
    }
    return format === "jsonl" ? response.text() : response.json();
  }
}

function normalizeContext(context: ExecutionContext): JsonObject {
  return {
    ...(context.env ? { env: context.env } : {}),
    ...(context.workflowId ? { workflow_id: context.workflowId } : {}),
    ...(context.workflowRunId ? { workflow_run_id: context.workflowRunId } : {}),
    ...(context.userId ? { user_id: context.userId } : {}),
    ...(context.workspaceId ? { workspace_id: context.workspaceId } : {}),
    ...(context.resources ? { resources: context.resources } : {}),
    ...(context.labels ? { labels: context.labels } : {}),
    ...(context.traceId ? { trace_id: context.traceId } : {}),
  };
}
