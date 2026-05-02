# sdk.py - Trace Python SDK with API Key

import os
from typing import Any, Dict

import httpx


DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_API_KEY = "ag_live_default1234567890"

class AgentGateError(Exception):
    """Base error for Trace/AgentGate SDK."""
    pass

class AuthenticationError(AgentGateError):
    """Raised when API key is invalid."""
    pass


class Trace:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL):
        if not api_key or not api_key.strip():
            raise ValueError("Trace requires a non-empty API key")

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.client = httpx.Client(timeout=120.0)

    def __enter__(self) -> "Trace":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @classmethod
    def from_env(cls, base_url: str = DEFAULT_BASE_URL) -> "Trace":
        # Prefer new TRACE_* env vars but fall back to AGENTGATE_* for compatibility
        env_base = os.getenv("TRACE_BASE_URL", os.getenv("AGENTGATE_BASE_URL", base_url))
        api_key = os.getenv("TRACE_API_KEY", os.getenv("AGENTGATE_API_KEY", DEFAULT_API_KEY))
        return cls(
            base_url=env_base,
            api_key=api_key,
        )

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
        }

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if response.status_code == 401:
            raise AuthenticationError("Authentication failed: Invalid or missing API key")
        if response.status_code != 200:
            try:
                error_detail = response.json()
            except ValueError:
                error_detail = response.text
            raise AgentGateError(f"AgentGate error {response.status_code}: {error_detail}")

    @staticmethod
    def _payload(
        tool: str,
        params: Dict[str, Any],
        agent_id: str,
        policy: Dict[str, Any] | None = None,
        execution_state: Dict[str, Any] | None = None,
        action: str | None = None,
        failure_policy: Dict[str, Any] | None = None,
        entity_resolution: Dict[str, Any] | None = None,
        context: Dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Dict[str, Any]:
        payload = {
            "tool": tool,
            "params": params,
            "agent_id": agent_id,
        }
        if policy:
            payload["policy"] = policy
        if execution_state is not None:
            payload["execution_state"] = execution_state
        if action is not None:
            payload["action"] = action
        if failure_policy is not None:
            payload["failure_policy"] = failure_policy
        if entity_resolution is not None:
            payload["entity_resolution"] = entity_resolution
        if context is not None:
            payload["context"] = context
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        return payload

    def call(
        self,
        tool: str,
        params: Dict[str, Any],
        agent_id: str = "default_agent",
        policy: Dict[str, Any] | None = None,
        execution_state: Dict[str, Any] | None = None,
        action: str | None = None,
        failure_policy: Dict[str, Any] | None = None,
        entity_resolution: Dict[str, Any] | None = None,
        context: Dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Dict[str, Any]:
        """Call a tool through the gateway."""
        try:
            response = self.client.post(
                f"{self.base_url}/call",
                json=self._payload(
                    tool,
                    params,
                    agent_id,
                    policy,
                    execution_state,
                    action,
                    failure_policy,
                    entity_resolution,
                    context,
                    idempotency_key,
                ),
                headers=self._headers(),
            )
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            raise AgentGateError(f"Network error connecting to AgentGate at {self.base_url}: {exc}")
        self._raise_for_error(response)

        return response.json()

    def list_tools(self) -> Dict[str, Any]:
        """List registered tools and capability cards."""
        response = self.client.get(f"{self.base_url}/tools", headers=self._headers())
        self._raise_for_error(response)
        return response.json()

    def to_openai_tools(self, query: str | None = None) -> list[dict[str, Any]]:
        """Format AgentGate tools for OpenAI's 'tools' parameter."""
        tools = self.discover(query)["tools"] if query else self.list_tools()["tools"]
        return [
            {
                "type": "function",
                "function": {
                    # OpenAI names must match ^[a-zA-Z0-9_-]+$
                    "name": t["id"].replace(".", "__").replace("/", "---"),
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]

    def to_anthropic_tools(self, query: str | None = None) -> list[dict[str, Any]]:
        """Format AgentGate tools for Anthropic's 'tools' parameter."""
        tools = self.discover(query)["tools"] if query else self.list_tools()["tools"]
        return [
            {
                "name": t["id"].replace(".", "__").replace("/", "---"),
                "description": t["description"],
                "input_schema": t["input_schema"],
            }
            for t in tools
        ]

    def resolve_tool_id(self, llm_tool_name: str) -> str:
        """Convert LLM-safe tool names back to AgentGate IDs."""
        return llm_tool_name.replace("---", "/").replace("__", ".")

    def discover(self, query: str, limit: int = 10) -> Dict[str, Any]:
        """Search for tools by natural-language capability."""
        response = self.client.get(
            f"{self.base_url}/discover",
            params={"q": query, "limit": limit},
            headers=self._headers(),
        )
        self._raise_for_error(response)
        return response.json()

    def connect(
        self,
        saas: str,
        owner_email: str = "owner@example.com",
        name: str | None = None,
        approval_slack_webhook_url: str | None = None,
        approval_email: str | None = None,
    ) -> Dict[str, Any]:
        """Connect a launch SaaS and get its hosted MCP gateway URL."""
        payload: Dict[str, Any] = {"owner_email": owner_email}
        if name:
            payload["name"] = name
        if approval_slack_webhook_url:
            payload["approval_slack_webhook_url"] = approval_slack_webhook_url
        if approval_email:
            payload["approval_email"] = approval_email
        response = self.client.post(
            f"{self.base_url}/connect/{saas}",
            json=payload,
            headers=self._headers(),
        )
        self._raise_for_error(response)
        return response.json()

    def approval_dashboard_url(self, api_key_in_query: bool = False) -> str:
        """Return the simple human approval dashboard URL."""
        suffix = f"?api_key={self.api_key}" if api_key_in_query else ""
        return f"{self.base_url}/dashboard/approvals{suffix}"

    def list_approvals(self) -> Dict[str, Any]:
        """List approvals and their current state."""
        response = self.client.get(f"{self.base_url}/approvals", headers=self._headers())
        self._raise_for_error(response)
        return response.json()

    def get_approval(self, approval_id: str) -> Dict[str, Any]:
        """Fetch one approval request."""
        response = self.client.get(f"{self.base_url}/approvals/{approval_id}", headers=self._headers())
        self._raise_for_error(response)
        return response.json()

    def approve(
        self,
        approval_id: str,
        reviewed_by: str = "human",
        note: str = "",
        execution_state: Dict[str, Any] | None = None,
        state: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Approve a pending action, revalidate policy, and execute if still allowed."""
        payload: Dict[str, Any] = {"reviewed_by": reviewed_by, "note": note}
        if execution_state is not None:
            payload["execution_state"] = execution_state
        elif state is not None:
            payload["state"] = state
        response = self.client.post(
            f"{self.base_url}/approvals/{approval_id}/approve",
            json=payload,
            headers=self._headers(),
        )
        self._raise_for_error(response)
        return response.json()

    def reject(self, approval_id: str, reviewed_by: str = "human", note: str = "") -> Dict[str, Any]:
        """Reject a pending action."""
        response = self.client.post(
            f"{self.base_url}/approvals/{approval_id}/reject",
            json={"reviewed_by": reviewed_by, "note": note},
            headers=self._headers(),
        )
        self._raise_for_error(response)
        return response.json()

    def traces(self, limit: int = 100) -> Dict[str, Any]:
        """List recent tool-call traces."""
        response = self.client.get(
            f"{self.base_url}/traces",
            params={"limit": limit},
            headers=self._headers(),
        )
        self._raise_for_error(response)
        return response.json()

    def export_traces(self, format: str = "json", limit: int = 100) -> Any:
        """Export traces as json, jsonl, langsmith, or helicone-compatible records."""
        response = self.client.get(
            f"{self.base_url}/traces/export",
            params={"format": format, "limit": limit},
            headers=self._headers(),
        )
        self._raise_for_error(response)
        if format == "jsonl":
            return response.text
        return response.json()

    def verify_state(
        self,
        intent: str,
        required_fields: list[str],
        assumed_state: Dict[str, Any] | None = None,
        current_state: Dict[str, Any] | None = None,
        state_refetch: Dict[str, Any] | None = None,
        conditions: Dict[str, Any] | None = None,
        on_mismatch: str = "abort",
    ) -> Dict[str, Any]:
        """Re-check critical state before execution."""
        payload: Dict[str, Any] = {
            "intent": intent,
            "required_fields": required_fields,
            "on_mismatch": on_mismatch,
        }
        if assumed_state is not None:
            payload["assumed_state"] = assumed_state
        if current_state is not None:
            payload["current_state"] = current_state
        if state_refetch is not None:
            payload["state_refetch"] = state_refetch
        if conditions is not None:
            payload["conditions"] = conditions
        response = self.client.post(f"{self.base_url}/state/verify", json=payload, headers=self._headers())
        self._raise_for_error(response)
        return response.json()

    def reconcile(self, action: Dict[str, Any] | str, **kwargs: Any) -> Dict[str, Any]:
        """Reconcile an unknown outcome before retrying an action."""
        payload: Dict[str, Any] = {"action": action}
        payload.update(kwargs)
        response = self.client.post(f"{self.base_url}/outcomes/reconcile", json=payload, headers=self._headers())
        self._raise_for_error(response)
        return response.json()

    async def acall(
        self,
        tool: str,
        params: Dict[str, Any],
        agent_id: str = "default_agent",
        policy: Dict[str, Any] | None = None,
        execution_state: Dict[str, Any] | None = None,
        action: str | None = None,
        failure_policy: Dict[str, Any] | None = None,
        entity_resolution: Dict[str, Any] | None = None,
        context: Dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Dict[str, Any]:
        """Async version."""
        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/call",
                    json=self._payload(
                        tool,
                        params,
                        agent_id,
                        policy,
                        execution_state,
                        action,
                        failure_policy,
                        entity_resolution,
                        context,
                        idempotency_key,
                    ),
                    headers=self._headers(),
                )
            except httpx.ConnectError:
                raise AgentGateError(f"Connection refused. Is AgentGate running at {self.base_url}?")
            self._raise_for_error(response)
            return response.json()

    async def alist_tools(self) -> Dict[str, Any]:
        """Async version of list_tools."""
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(f"{self.base_url}/tools", headers=self._headers())
            self._raise_for_error(response)
            return response.json()

    async def adiscover(self, query: str, limit: int = 10) -> Dict[str, Any]:
        """Async version of discover."""
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(
                f"{self.base_url}/discover",
                params={"q": query, "limit": limit},
                headers=self._headers(),
            )
            self._raise_for_error(response)
            return response.json()

    async def aconnect(
        self,
        saas: str,
        owner_email: str = "owner@example.com",
        name: str | None = None,
        approval_slack_webhook_url: str | None = None,
        approval_email: str | None = None,
    ) -> Dict[str, Any]:
        """Async version of connect."""
        payload: Dict[str, Any] = {"owner_email": owner_email}
        if name:
            payload["name"] = name
        if approval_slack_webhook_url:
            payload["approval_slack_webhook_url"] = approval_slack_webhook_url
        if approval_email:
            payload["approval_email"] = approval_email
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.base_url}/connect/{saas}",
                json=payload,
                headers=self._headers(),
            )
            self._raise_for_error(response)
            return response.json()

    async def alist_approvals(self) -> Dict[str, Any]:
        """Async version of list_approvals."""
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(f"{self.base_url}/approvals", headers=self._headers())
            self._raise_for_error(response)
            return response.json()

    async def aapprove(
        self,
        approval_id: str,
        reviewed_by: str = "human",
        note: str = "",
        execution_state: Dict[str, Any] | None = None,
        state: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Async version of approve."""
        payload: Dict[str, Any] = {"reviewed_by": reviewed_by, "note": note}
        if execution_state is not None:
            payload["execution_state"] = execution_state
        elif state is not None:
            payload["state"] = state
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.base_url}/approvals/{approval_id}/approve",
                json=payload,
                headers=self._headers(),
            )
            self._raise_for_error(response)
            return response.json()

    async def areject(self, approval_id: str, reviewed_by: str = "human", note: str = "") -> Dict[str, Any]:
        """Async version of reject."""
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.base_url}/approvals/{approval_id}/reject",
                json={"reviewed_by": reviewed_by, "note": note},
                headers=self._headers(),
            )
            self._raise_for_error(response)
            return response.json()

    async def atraces(self, limit: int = 100) -> Dict[str, Any]:
        """Async version of traces."""
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(
                f"{self.base_url}/traces",
                params={"limit": limit},
                headers=self._headers(),
            )
            self._raise_for_error(response)
            return response.json()

    async def aexport_traces(self, format: str = "json", limit: int = 100) -> Any:
        """Async version of export_traces."""
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(
                f"{self.base_url}/traces/export",
                params={"format": format, "limit": limit},
                headers=self._headers(),
            )
            self._raise_for_error(response)
            if format == "jsonl":
                return response.text
            return response.json()

    def close(self) -> None:
        self.client.close()


# Convenience instances. Prefer `trace`; provide `gate` as a compatibility alias.
trace = Trace.from_env()
gate = trace
