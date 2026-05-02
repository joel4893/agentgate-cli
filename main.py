# main.py - AgentGate Gateway with API Key Auth

import json
import logging
import os
import re
import time
import uuid
import asyncio
import random
import ast
import secrets
import hashlib
import sqlite3
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from html import escape, unescape
from typing import Any, AsyncGenerator, Dict, Optional
from urllib.parse import parse_qs

import httpx
import structlog
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from registry import discover_tools as discover_registry_tools
from registry import get_target as get_registry_target
from registry import list_tools as list_registry_tool_cards

# ====================== LOGGING SETUP ======================
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILENAME = os.getenv("TRACE_LOG_FILE", os.getenv("AGENTGATE_LOG_FILE", "trace.log"))
TRACE_EVENTS_FILENAME = os.getenv("TRACE_EVENTS_FILE", "tool_calls.jsonl")
TRACE_EVENTS_PATH = os.path.join(LOG_DIR, TRACE_EVENTS_FILENAME)
logging.basicConfig(
    level=logging.INFO,
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, LOG_FILENAME), mode="a"),
        logging.StreamHandler(),
    ],
    format="%(message)s",
)

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()
DB_PATH = os.getenv("TRACE_DB", "trace.db")

# ====================== APP SETUP ======================
MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "User-Agent": "Trace/0.1.0",
    "mcp-protocol-version": MCP_PROTOCOL_VERSION,
}

FULL_ACCESS_SCOPES = ["*"]
READ_SCOPES = {"tools:read", "state:verify", "outcomes:reconcile", "approvals:read", "logs:read", "traces:read"}
WRITE_ACTION_WORDS = {
    "archive",
    "cancel",
    "create",
    "delete",
    "invite",
    "merge",
    "post",
    "publish",
    "push",
    "send",
    "submit",
    "update",
    "write",
}


def parse_token_policies(raw: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Parse scoped token config from JSON.

    Expected shape:
    {
      "ag_scoped_key": {
        "scopes": ["tools:read", "tools:call", "traces:read"],
        "allowed_tools": ["fetch.url"],
        "read_only": true,
        "agent_id": "research_agent_v1"
      }
    }
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("token_policy_parse_failed", error=str(exc))
        return {}
    if not isinstance(parsed, dict):
        logger.warning("token_policy_parse_failed", error="top-level value must be an object")
        return {}

    policies: Dict[str, Dict[str, Any]] = {}
    for token, policy in parsed.items():
        if not isinstance(token, str) or not token.strip():
            continue
        if not isinstance(policy, dict):
            logger.warning("token_policy_skipped", token_prefix=token[:8] + "...", error="policy must be an object")
            continue
        policies[token.strip()] = policy
    return policies

API_KEYS = {
    key.strip()
    for key in os.getenv(
        "TRACE_API_KEYS",
        os.getenv("AGENTGATE_API_KEYS", "ag_live_default1234567890"),
    ).split(",")
    if key.strip()
}
TOKEN_POLICIES = parse_token_policies(
    os.getenv("TRACE_API_KEY_SCOPES", os.getenv("AGENTGATE_API_KEY_SCOPES"))
)

def init_db():
    """Initialize persistence tables for tools and approvals."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dynamic_tools (
                key TEXT PRIMARY KEY,
                data TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_approvals (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.commit()

PENDING_APPROVALS: Dict[str, Dict[str, Any]] = {}
DYNAMIC_PROVIDERS: Dict[str, Dict[str, Any]] = {}
PROVIDER_KEYS: Dict[str, str] = {}
DYNAMIC_TOOLS: Dict[str, Dict[str, Any]] = {}
IDEMPOTENCY_RECORDS: Dict[str, Dict[str, Any]] = {}
HOSTED_GATEWAY_DOMAIN = os.getenv("AGENTGATE_GATEWAY_DOMAIN", "agentgate.dev").strip().strip(".")
PUBLIC_BASE_URL = os.getenv("AGENTGATE_PUBLIC_URL", "http://localhost:8000").rstrip("/")
APPROVAL_SLACK_WEBHOOK_URL = os.getenv("APPROVAL_SLACK_WEBHOOK_URL", "")
APPROVAL_EMAIL_WEBHOOK_URL = os.getenv("APPROVAL_EMAIL_WEBHOOK_URL", "")

# Retry configuration for MCP calls
RETRY_MAX = int(os.getenv("RETRY_MAX", "3"))
RETRY_BASE_MS = float(os.getenv("RETRY_BASE_MS", "200"))
RETRY_BACKOFF_FACTOR = float(os.getenv("RETRY_BACKOFF_FACTOR", "2.0"))
RETRY_JITTER_MS = float(os.getenv("RETRY_JITTER_MS", "100"))
FAILURE_POLICY_MAX_RETRIES = int(os.getenv("FAILURE_POLICY_MAX_RETRIES", "5"))

def load_persistence():
    """Load tools and approvals from SQLite into memory on startup."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute("SELECT key, data FROM dynamic_tools"):
                DYNAMIC_TOOLS[row["key"]] = json.loads(row["data"])
            for row in conn.execute("SELECT id, data FROM pending_approvals WHERE status = 'pending'"):
                PENDING_APPROVALS[row["id"]] = json.loads(row["data"])
        logger.info("persistence_loaded", tools=len(DYNAMIC_TOOLS), approvals=len(PENDING_APPROVALS))
    except Exception as exc:
        logger.warning("persistence_load_failed", error=str(exc))

def save_to_db(table: str, pk: str, data: Dict[str, Any], status: Optional[str] = None):
    with sqlite3.connect(DB_PATH) as conn:
        if table == "dynamic_tools":
            conn.execute("INSERT OR REPLACE INTO dynamic_tools (key, data) VALUES (?, ?)", (pk, json.dumps(data)))
        else:
            conn.execute("INSERT OR REPLACE INTO pending_approvals (id, status, data, created_at) VALUES (?, ?, ?, ?)", 
                         (pk, status or data.get("status", "pending"), json.dumps(data), data.get("created_at", time.time())))
        conn.commit()

# Shared HTTP Client
http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(90.0, connect=15.0),
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
    follow_redirects=True,
)


class UpstreamToolError(Exception):
    """An upstream MCP server rejected or failed a tool call."""


class PolicyEvaluationError(ValueError):
    """A policy condition could not be safely evaluated."""


_MISSING = object()
POLICY_CONTRACT_KEYS = {
    "intent",
    "condition",
    "conditions",
    "threshold",
    "allowed_action",
    "expires_at",
    "on_condition_failed",
    "state_refetch",
    "refetch",
    "reason",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_policy_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyEvaluationError(f"Invalid expires_at timestamp '{value}'") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_execution_state(raw_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if raw_state is None:
        raw_state = {}
    if not isinstance(raw_state, dict):
        raise HTTPException(status_code=400, detail="'execution_state' must be an object")

    variables = raw_state.get("variables", {})
    tool_outputs = raw_state.get("tool_outputs", {})
    state = raw_state.get("state", {})
    if variables is None:
        variables = {}
    if tool_outputs is None:
        tool_outputs = {}
    if state is None:
        state = {}
    if not isinstance(variables, dict):
        raise HTTPException(status_code=400, detail="'execution_state.variables' must be an object")
    if not isinstance(tool_outputs, dict):
        raise HTTPException(status_code=400, detail="'execution_state.tool_outputs' must be an object")
    if not isinstance(state, dict):
        raise HTTPException(status_code=400, detail="'execution_state.state' must be an object")

    direct_state = {
        key: value
        for key, value in raw_state.items()
        if key not in {"variables", "tool_outputs", "state"}
    }
    merged_state = {**deepcopy(state), **deepcopy(direct_state)}

    return {
        "variables": deepcopy(variables),
        "tool_outputs": deepcopy(tool_outputs),
        "state": merged_state,
    }


def merge_execution_states(base_state: Dict[str, Any], live_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = normalize_execution_state(base_state)
    live = normalize_execution_state(live_state)
    merged["variables"].update(live["variables"])
    merged["tool_outputs"].update(live["tool_outputs"])
    merged["state"].update(live["state"])
    return merged


def build_policy_context(
    params: Dict[str, Any],
    execution_state: Dict[str, Any],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    normalized_state = normalize_execution_state(execution_state)
    context: Dict[str, Any] = {
        "params": deepcopy(params),
        "variables": deepcopy(normalized_state["variables"]),
        "tool_outputs": deepcopy(normalized_state["tool_outputs"]),
        "state": deepcopy(normalized_state["state"]),
    }
    context.update(deepcopy(params))
    context.update(deepcopy(normalized_state["variables"]))
    context.update(deepcopy(normalized_state["state"]))
    if extra:
        context.update(deepcopy(extra))
    return context


def normalize_execution_context(raw_context: Any, agent_id: str, request_id: str) -> Dict[str, Any]:
    if raw_context is None:
        raw_context = {}
    if not isinstance(raw_context, dict):
        raise HTTPException(status_code=400, detail="'context' must be an object")

    env = raw_context.get("env", raw_context.get("environment", os.getenv("AGENTGATE_ENV", "dev")))
    context = {
        "env": str(env or "dev"),
        "agent_id": agent_id,
        "workflow_id": str(raw_context.get("workflow_id", raw_context.get("workflow", "")) or ""),
        "workflow_run_id": str(raw_context.get("workflow_run_id", raw_context.get("run_id", "")) or ""),
        "user_id": str(raw_context.get("user_id", raw_context.get("end_user_id", "")) or ""),
        "workspace_id": str(raw_context.get("workspace_id", raw_context.get("tenant_id", "")) or ""),
        "trace_id": str(raw_context.get("trace_id", request_id) or request_id),
        "request_id": request_id,
    }
    resources = raw_context.get("resources", {})
    if resources is None:
        resources = {}
    if not isinstance(resources, dict):
        raise HTTPException(status_code=400, detail="'context.resources' must be an object")
    context["resources"] = deepcopy(resources)

    labels = raw_context.get("labels", {})
    if labels is None:
        labels = {}
    if not isinstance(labels, dict):
        raise HTTPException(status_code=400, detail="'context.labels' must be an object")
    context["labels"] = deepcopy(labels)

    return context


def _matches_allowed(value: str, allowed: Any) -> bool:
    if allowed is None:
        return True
    if isinstance(allowed, str):
        allowed = [allowed]
    if not isinstance(allowed, list):
        return False
    normalized = {str(item) for item in allowed}
    return "*" in normalized or value in normalized


def validate_json_schema_value(value: Any, schema: Dict[str, Any], path: str) -> list[str]:
    if not isinstance(schema, dict) or not schema:
        return []

    errors: list[str] = []
    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        allowed_types = expected_type
    elif isinstance(expected_type, str):
        allowed_types = [expected_type]
    else:
        allowed_types = []

    def type_matches(expected: str, item: Any) -> bool:
        if expected == "object":
            return isinstance(item, dict)
        if expected == "array":
            return isinstance(item, list)
        if expected == "string":
            return isinstance(item, str)
        if expected == "integer":
            return isinstance(item, int) and not isinstance(item, bool)
        if expected == "number":
            return isinstance(item, (int, float)) and not isinstance(item, bool)
        if expected == "boolean":
            return isinstance(item, bool)
        if expected == "null":
            return item is None
        return True

    if allowed_types and not any(type_matches(item_type, value) for item_type in allowed_types):
        errors.append(f"{path} must be {', '.join(allowed_types)}")
        return errors

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']}")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    errors.append(f"{path}.{key} is required")
        if isinstance(properties, dict):
            for key, item in value.items():
                prop_schema = properties.get(key)
                if isinstance(prop_schema, dict):
                    errors.extend(validate_json_schema_value(item, prop_schema, f"{path}.{key}"))
        if schema.get("additionalProperties") is False and isinstance(properties, dict):
            extra_keys = sorted(set(value) - set(properties))
            for key in extra_keys:
                errors.append(f"{path}.{key} is not allowed")

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        item_schema = schema["items"]
        for index, item in enumerate(value):
            errors.extend(validate_json_schema_value(item, item_schema, f"{path}[{index}]"))

    return errors


def validate_tool_params(tool_key: str, target: Dict[str, Any], params: Dict[str, Any]) -> None:
    schema = target.get("input_schema") or {}
    if not schema:
        return
    if not isinstance(schema, dict):
        raise HTTPException(status_code=500, detail=f"Tool '{tool_key}' has invalid input_schema")
    errors = validate_json_schema_value(params, schema, "params")
    if errors:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Tool parameter validation failed",
                "tool": tool_key,
                "schema_errors": errors,
            },
        )


def params_resource_values(params: Dict[str, Any], execution_state: Dict[str, Any]) -> set[str]:
    values: set[str] = set()
    normalized_state = normalize_execution_state(execution_state)
    for source in (params, normalized_state["state"], normalized_state["variables"]):
        candidates: list[Dict[str, Any]] = []
        _collect_entity_id_candidates(source, ENTITY_ID_KEYS.union({"repo", "team_id", "project_id"}), path="", out=candidates)
        values.update(candidate["value"] for candidate in candidates)
    return values


def enforce_execution_scope(
    policy: Dict[str, Any],
    tool_key: str,
    target: Dict[str, Any],
    agent_id: str,
    execution_context: Dict[str, Any],
    params: Dict[str, Any],
    execution_state: Dict[str, Any],
) -> Dict[str, Any]:
    env = execution_context.get("env", "dev")
    effective_agent_id = str(policy.get("agent_id") or agent_id)

    if not _matches_allowed(env, policy.get("envs", policy.get("environments"))):
        raise HTTPException(status_code=403, detail=f"API key is not scoped for environment '{env}'")

    agents = policy.get("agents")
    if agents is not None and not _matches_allowed(effective_agent_id, agents):
        raise HTTPException(status_code=403, detail=f"API key is not scoped for agent '{effective_agent_id}'")

    workflow_id = execution_context.get("workflow_id", "")
    if policy.get("workflows") is not None and not _matches_allowed(workflow_id, policy.get("workflows")):
        raise HTTPException(status_code=403, detail=f"API key is not scoped for workflow '{workflow_id}'")

    allowed_actions = policy.get("allowed_actions") or policy.get("actions")
    if allowed_actions is not None and not allowed_action_matches(allowed_actions, tool_action_candidates(tool_key, target, None)):
        raise HTTPException(status_code=403, detail=f"API key is not scoped for action '{tool_key}'")

    allowed_resources = policy.get("resources") or policy.get("allowed_resources")
    resource_values = params_resource_values(params, execution_state).union(
        str(value) for value in execution_context.get("resources", {}).values()
    )
    if allowed_resources is not None and "*" not in set(allowed_resources if isinstance(allowed_resources, list) else [allowed_resources]):
        allowed = {str(item) for item in allowed_resources} if isinstance(allowed_resources, list) else {str(allowed_resources)}
        if resource_values and not resource_values.intersection(allowed):
            raise HTTPException(status_code=403, detail="API key is not scoped for the requested resource")

    return {
        "env": env,
        "agent_id": effective_agent_id,
        "workflow_id": workflow_id,
        "resource_values": sorted(resource_values),
    }


def resolve_provider_credential(
    target: Dict[str, Any],
    execution_context: Dict[str, Any],
    api_key: str,
) -> Dict[str, Any]:
    provider_id = target.get("provider_id") or target.get("provider")
    provider_slug = ""
    if provider_id and provider_id in DYNAMIC_PROVIDERS:
        provider_slug = DYNAMIC_PROVIDERS[provider_id].get("slug", "")
    provider_slug = provider_slug or str(target.get("provider_slug") or target.get("capability", "")).split(".")[0]
    env = execution_context.get("env", "dev")
    credential_id = target.get("credential_id") or f"{provider_slug or 'default'}:{env}"
    return {
        "provider_id": provider_id,
        "provider_slug": provider_slug,
        "credential_id": credential_id,
        "env": env,
        "source": "target" if target.get("credential_id") else "derived",
        "secret_exposed_to_agent": False,
        "api_key_prefix": api_key[:8] + "...",
    }


def idempotency_fingerprint(parts: Dict[str, Any]) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def resolve_idempotency(
    request: Request,
    body: Dict[str, Any],
    tool_key: str,
    target: Dict[str, Any],
    params: Dict[str, Any],
    execution_context: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    spec = target.get("idempotency") or {}
    if not isinstance(spec, dict) or not spec:
        return None
    mode = str(spec.get("mode", "automatic")).lower()
    if mode in {"none", "off", "false"}:
        return None
    caller_key = request.headers.get("Idempotency-Key") or body.get("idempotency_key")
    key_fields = spec.get("key_fields", [])
    if not isinstance(key_fields, list):
        key_fields = []
    selected = {field: resolve_context_path(params, str(field)) for field in key_fields if isinstance(field, str)}
    selected = {key: value for key, value in selected.items() if value is not _MISSING}
    raw_key = caller_key or idempotency_fingerprint(
        {
            "tool": tool_key,
            "params": selected or params,
            "env": execution_context.get("env"),
            "workflow_id": execution_context.get("workflow_id"),
        }
    )
    return {
        "key": f"{tool_key}:{raw_key}",
        "mode": mode,
        "caller_provided": bool(caller_key),
        "key_fields": key_fields,
        "fingerprint": raw_key,
    }


ENTITY_ID_KEYS = {
    "account_id",
    "company_id",
    "contact_id",
    "cust_id",
    "customer_id",
    "entity_id",
    "organization_id",
    "org_id",
    "user_id",
}
ENTITY_TYPE_ID_KEYS = {
    "account": {"account_id", "entity_id"},
    "company": {"company_id", "entity_id"},
    "contact": {"contact_id", "entity_id"},
    "customer": {"customer_id", "cust_id", "entity_id"},
    "invoice": {"invoice_id", "entity_id"},
    "organization": {"organization_id", "org_id", "entity_id"},
    "payment": {"payment_id", "entity_id"},
    "user": {"user_id", "entity_id"},
}


def normalize_entity_resolution(raw_entity: Any) -> Optional[Dict[str, Any]]:
    if raw_entity is None:
        return None
    if not isinstance(raw_entity, dict):
        raise HTTPException(status_code=400, detail="'entity_resolution' must be an object")

    entity_id = raw_entity.get("entity_id")
    source = raw_entity.get("source")
    resolved_at = raw_entity.get("resolved_at") or now_iso()
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise HTTPException(status_code=400, detail="'entity_resolution.entity_id' must be a non-empty string")
    if not isinstance(source, str) or not source.strip():
        raise HTTPException(status_code=400, detail="'entity_resolution.source' must be a non-empty string")
    if not isinstance(resolved_at, str) or not resolved_at.strip():
        raise HTTPException(status_code=400, detail="'entity_resolution.resolved_at' must be a non-empty string")
    try:
        parse_policy_datetime(resolved_at)
    except PolicyEvaluationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    normalized: Dict[str, Any] = {
        "entity_id": entity_id.strip(),
        "source": source.strip(),
        "resolved_at": resolved_at.strip(),
        "verified_at": now_iso(),
    }
    if "resolved_at" not in raw_entity:
        normalized["resolved_at_inferred"] = True

    for optional_key in ("entity_type", "display_name", "confidence", "expected_entity_id"):
        if optional_key in raw_entity:
            normalized[optional_key] = deepcopy(raw_entity[optional_key])
    return normalized


def _collect_entity_id_candidates(
    value: Any,
    allowed_keys: set[str],
    *,
    path: str,
    out: list[Dict[str, Any]],
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key in allowed_keys and isinstance(item, (str, int)):
                out.append({"path": child_path, "value": str(item)})
            _collect_entity_id_candidates(item, allowed_keys, path=child_path, out=out)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _collect_entity_id_candidates(item, allowed_keys, path=f"{path}[{index}]", out=out)


def verify_entity_resolution(
    entity_resolution: Optional[Dict[str, Any]],
    params: Dict[str, Any],
    execution_state: Dict[str, Any],
) -> Dict[str, Any]:
    if not entity_resolution:
        return {"required": False, "passed": True}

    entity_id = str(entity_resolution["entity_id"])
    entity_type = str(entity_resolution.get("entity_type", "")).strip().lower()
    allowed_keys = set(ENTITY_ID_KEYS)
    if entity_type:
        allowed_keys.update(ENTITY_TYPE_ID_KEYS.get(entity_type, {f"{entity_type}_id", "entity_id"}))

    candidates: list[Dict[str, Any]] = []
    _collect_entity_id_candidates(params, allowed_keys, path="params", out=candidates)
    normalized_state = normalize_execution_state(execution_state)
    _collect_entity_id_candidates(normalized_state["state"], allowed_keys, path="execution_state.state", out=candidates)
    _collect_entity_id_candidates(
        normalized_state["variables"],
        allowed_keys,
        path="execution_state.variables",
        out=candidates,
    )

    expected_entity_id = entity_resolution.get("expected_entity_id")
    if expected_entity_id is not None:
        candidates.append({"path": "entity_resolution.expected_entity_id", "value": str(expected_entity_id)})

    mismatches = [candidate for candidate in candidates if candidate["value"] != entity_id]
    matches = [candidate for candidate in candidates if candidate["value"] == entity_id]
    passed = not candidates or not mismatches
    return {
        "required": True,
        "passed": passed,
        "entity": deepcopy(entity_resolution),
        "candidate_paths": candidates,
        "matches": matches,
        "mismatches": [] if passed else mismatches,
        "reason": "entity_match" if passed and candidates else ("no_comparable_entity_field" if not candidates else "entity_mismatch"),
        "checked_at": now_iso(),
    }


def normalize_failure_policy(raw_policy: Any) -> Dict[str, Any]:
    if raw_policy is None:
        return {}
    if not isinstance(raw_policy, dict):
        raise HTTPException(status_code=400, detail="'failure_policy' must be an object")
    retry = raw_policy.get("retry", 0)
    if isinstance(retry, bool) or not isinstance(retry, int):
        raise HTTPException(status_code=400, detail="'failure_policy.retry' must be an integer")
    on_failure = str(raw_policy.get("on_failure", "error")).lower()
    if on_failure not in {"error", "escalate"}:
        raise HTTPException(status_code=400, detail="'failure_policy.on_failure' must be 'error' or 'escalate'")
    fallback = raw_policy.get("fallback")
    if fallback is not None and not isinstance(fallback, str):
        raise HTTPException(status_code=400, detail="'failure_policy.fallback' must be a string")

    normalized = deepcopy(raw_policy)
    normalized["retry"] = min(max(retry, 0), FAILURE_POLICY_MAX_RETRIES)
    normalized["requested_retry"] = retry
    normalized["on_failure"] = on_failure
    if fallback:
        normalized["fallback"] = fallback
    return normalized


def outcome_is_unknown(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return True
    return "unknown" in str(exc).lower()


def reconcile_outcome_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    action = payload.get("action")
    outcome = str(payload.get("outcome", "UNKNOWN")).upper()
    current_state = payload.get("current_state", {})
    conditions = payload.get("conditions", {})
    if not isinstance(action, (dict, str)):
        raise HTTPException(status_code=400, detail="'action' must be a string or object")
    if not isinstance(current_state, dict):
        raise HTTPException(status_code=400, detail="'current_state' must be an object")

    context = build_policy_context(
        payload.get("params", {}) if isinstance(payload.get("params", {}), dict) else {},
        {"state": current_state},
        {"action": action},
    )
    conditions_result = evaluate_contract_conditions(conditions, context)
    if outcome == "UNKNOWN" and conditions_result["passed"]:
        status = "succeeded"
        decision = "do_not_retry"
        reconciled_outcome = "already_succeeded"
    elif outcome == "UNKNOWN":
        status = "unresolved"
        decision = "retry_allowed"
        reconciled_outcome = "not_observed"
    else:
        status = "known"
        decision = "do_not_retry" if outcome in {"SUCCEEDED", "SUCCESS"} else "retry_allowed"
        reconciled_outcome = outcome.lower()

    return {
        "success": True,
        "status": status,
        "decision": decision,
        "outcome": reconciled_outcome,
        "action": deepcopy(action),
        "conditions": conditions_result,
        "checked_at": now_iso(),
    }


async def execute_with_failure_policy(
    tool_key: str,
    target: Dict[str, Any],
    params: Dict[str, Any],
    failure_policy: Dict[str, Any],
    *,
    api_key: str,
    agent_id: str,
    request_id: str,
    entity_resolution: Optional[Dict[str, Any]],
    execution_context: Optional[Dict[str, Any]] = None,
    credential: Optional[Dict[str, Any]] = None,
    idempotency: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not failure_policy:
        result, retry_count = await execute_tool_call(tool_key, target, params, request_id=request_id)
        return {"kind": "executed", "result": result, "meta": {"request_id": request_id, "retry_count": retry_count}}

    max_retries = failure_policy["retry"]
    attempts: list[Dict[str, Any]] = []
    last_exc: Optional[Exception] = None
    for attempt_index in range(max_retries + 1):
        try:
            result, retry_count = await execute_tool_call(tool_key, target, params, request_id=request_id)
            return {
                "kind": "executed",
                "result": result,
                "meta": {
                    "request_id": request_id,
                    "retry_count": attempt_index + retry_count,
                    "failure_policy": {
                        "retry_count": attempt_index,
                        "attempts": attempts
                        + [{"tool": tool_key, "attempt": attempt_index + 1, "status": "success"}],
                        "bounded": True,
                    },
                },
            }
        except Exception as exc:
            last_exc = exc
            attempts.append({"tool": tool_key, "attempt": attempt_index + 1, "status": "failed", "error": str(exc)})
            reconcile_payload = failure_policy.get("reconcile")
            if reconcile_payload and outcome_is_unknown(exc):
                reconciliation = reconcile_outcome_payload(reconcile_payload)
                if reconciliation["decision"] == "do_not_retry":
                    return {
                        "kind": "executed",
                        "result": {
                            "status": "outcome_reconciled",
                            "outcome": reconciliation["outcome"],
                            "reconciliation": reconciliation,
                        },
                        "meta": {
                            "request_id": request_id,
                            "retry_count": attempt_index,
                            "failure_policy": {
                                "retry_count": attempt_index,
                                "attempts": attempts,
                                "reconciled": True,
                                "reconciliation": reconciliation,
                            },
                        },
                    }

    fallback_tool = failure_policy.get("fallback")
    if fallback_tool:
        fallback_target = get_tool_target(fallback_tool)
        if not fallback_target:
            raise HTTPException(status_code=404, detail=f"Fallback tool '{fallback_tool}' not found")
        require_tool_access(api_key, fallback_tool, fallback_target)
        result, retry_count = await execute_tool_call(fallback_tool, fallback_target, params, request_id=request_id)
        return {
            "kind": "executed",
            "result": result,
            "meta": {
                "request_id": request_id,
                "retry_count": max_retries + retry_count,
                "fallback_used": fallback_tool,
                "failure_policy": {
                    "retry_count": max_retries,
                    "attempts": attempts + [{"tool": fallback_tool, "attempt": 1, "status": "success"}],
                    "fallback_used": fallback_tool,
                    "bounded": True,
                },
            },
        }

    if failure_policy.get("on_failure") == "escalate":
        reason = "failure_escalated"
        approval = create_pending_approval(
            tool_key,
            target,
            params,
            agent_id,
            api_key,
            reason,
            policy_contract={
                "intent": "tool_failure_escalation",
                "reason": reason,
                "failure_policy": deepcopy(failure_policy),
                "attempts": deepcopy(attempts),
            },
            execution_state={
                "state": {
                    "failure": str(last_exc) if last_exc else "unknown",
                    "attempts": deepcopy(attempts),
                }
            },
            request_id=request_id,
            policy_decision={"effect": "require_approval", "requires_approval": True, "reason": reason},
            entity_resolution=entity_resolution,
            execution_context=execution_context,
            credential=credential,
            idempotency=idempotency,
        )
        await send_approval_notifications(approval)
        return {
            "kind": "pending_approval",
            "approval": approval,
            "reason": reason,
            "failure_policy": {"retry_count": max_retries, "attempts": attempts, "bounded": True},
        }

    if last_exc:
        raise last_exc
    raise UpstreamToolError("Tool failed without a captured exception")


def parse_policy_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyEvaluationError(f"Invalid expires_at timestamp '{value}'") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def policy_node_path(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = policy_node_path(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def resolve_context_path(context: Dict[str, Any], path: str) -> Any:
    if path in context:
        return context[path]
    value: Any = context
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return _MISSING
    return value


def compare_policy_values(left: Any, op: ast.cmpop, right: Any) -> bool:
    try:
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.In):
            return left in right
        if isinstance(op, ast.NotIn):
            return left not in right
    except TypeError as exc:
        raise PolicyEvaluationError(f"Cannot compare {left!r} and {right!r}") from exc
    raise PolicyEvaluationError(f"Unsupported comparison operator '{op.__class__.__name__}'")


def eval_policy_ast(node: ast.AST, context: Dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return eval_policy_ast(node.body, context)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        lowered = node.id.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered in {"none", "null"}:
            return None
        resolved = resolve_context_path(context, node.id)
        return node.id if resolved is _MISSING else resolved
    if isinstance(node, ast.Attribute):
        path = policy_node_path(node)
        if not path:
            raise PolicyEvaluationError("Unsupported attribute expression")
        resolved = resolve_context_path(context, path)
        return path if resolved is _MISSING else resolved
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [eval_policy_ast(item, context) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(values)
        if isinstance(node, ast.Set):
            return set(values)
        return values
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(bool(eval_policy_ast(value, context)) for value in node.values)
        if isinstance(node.op, ast.Or):
            return any(bool(eval_policy_ast(value, context)) for value in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not bool(eval_policy_ast(node.operand, context))
    if isinstance(node, ast.Compare):
        left = eval_policy_ast(node.left, context)
        for op, comparator in zip(node.ops, node.comparators):
            right = eval_policy_ast(comparator, context)
            if not compare_policy_values(left, op, right):
                return False
            left = right
        return True
    raise PolicyEvaluationError(f"Unsupported policy expression '{node.__class__.__name__}'")


def evaluate_condition(condition: Optional[str], context: Dict[str, Any]) -> Dict[str, Any]:
    if not condition:
        return {"condition": condition, "passed": True}
    if not isinstance(condition, str):
        raise PolicyEvaluationError("Policy condition must be a string")
    try:
        parsed = ast.parse(condition, mode="eval")
    except SyntaxError as exc:
        raise PolicyEvaluationError(f"Invalid policy condition syntax: {exc.msg}") from exc
    return {"condition": condition, "passed": bool(eval_policy_ast(parsed, context))}


def safe_policy_value(value: Any) -> Any:
    if value is _MISSING:
        return None
    return safe_json_value(value)


def coerce_comparison_number(value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def compare_condition_expectation(actual: Any, expected: Any) -> bool:
    if isinstance(expected, str):
        stripped = expected.strip()
        for operator in (">=", "<=", "!=", "==", ">", "<"):
            if stripped.startswith(operator):
                raw_expected = stripped[len(operator):].strip()
                left = coerce_comparison_number(actual)
                right = coerce_comparison_number(raw_expected)
                try:
                    if operator == ">=":
                        return left >= right
                    if operator == "<=":
                        return left <= right
                    if operator == "!=":
                        return left != right
                    if operator == "==":
                        return left == right
                    if operator == ">":
                        return left > right
                    if operator == "<":
                        return left < right
                except TypeError as exc:
                    raise PolicyEvaluationError(f"Cannot compare {actual!r} and {raw_expected!r}") from exc
        return actual == expected
    return actual == expected


def evaluate_contract_conditions(conditions: Any, context: Dict[str, Any]) -> Dict[str, Any]:
    if not conditions:
        return {"conditions": conditions, "passed": True, "checks": []}
    if not isinstance(conditions, dict):
        raise PolicyEvaluationError("'conditions' must be an object")

    checks = []
    for raw_path, expected in conditions.items():
        if not isinstance(raw_path, str) or not raw_path:
            raise PolicyEvaluationError("'conditions' keys must be non-empty strings")
        actual = resolve_context_path(context, raw_path)
        passed = actual is not _MISSING and compare_condition_expectation(actual, expected)
        checks.append(
            {
                "path": raw_path,
                "expected": safe_policy_value(expected),
                "actual": safe_policy_value(actual),
                "passed": passed,
            }
        )
    return {
        "conditions": deepcopy(conditions),
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


def evaluate_conditional_drift(
    conditions: Any,
    frozen_context: Dict[str, Any],
    live_context: Dict[str, Any],
    threshold: Any,
) -> Dict[str, Any]:
    if not conditions:
        return {
            "threshold": threshold or "none",
            "exceeded": False,
            "changed": False,
            "checks": [],
            "evaluated_at": now_iso(),
        }
    if not isinstance(conditions, dict):
        raise PolicyEvaluationError("'conditions' must be an object")

    normalized_threshold = str(threshold or "strict").lower()
    if normalized_threshold in {"lenient", "condition", "conditions", "within_conditions"}:
        mode = "conditions"
    elif normalized_threshold in {"strict", "exact"}:
        mode = "strict"
    else:
        raise PolicyEvaluationError("'threshold' must be 'strict' or 'conditions'")

    checks = []
    for raw_path, expected in conditions.items():
        if not isinstance(raw_path, str) or not raw_path:
            raise PolicyEvaluationError("'conditions' keys must be non-empty strings")
        before = resolve_context_path(frozen_context, raw_path)
        after = resolve_context_path(live_context, raw_path)
        changed = before != after
        condition_passed = after is not _MISSING and compare_condition_expectation(after, expected)
        exceeded = not condition_passed or (mode == "strict" and changed)
        checks.append(
            {
                "path": raw_path,
                "expected": safe_policy_value(expected),
                "approval_value": safe_policy_value(before),
                "current_value": safe_policy_value(after),
                "changed": changed,
                "condition_passed": condition_passed,
                "exceeded": exceeded,
            }
        )

    return {
        "threshold": normalized_threshold,
        "mode": mode,
        "exceeded": any(check["exceeded"] for check in checks),
        "changed": any(check["changed"] for check in checks),
        "failed_conditions": [check["path"] for check in checks if not check["condition_passed"]],
        "changed_paths": [check["path"] for check in checks if check["changed"]],
        "checks": checks,
        "evaluated_at": now_iso(),
    }


def tool_action_candidates(tool_key: str, target: Dict[str, Any], explicit_action: Optional[str]) -> list[str]:
    candidates = [
        explicit_action,
        target.get("action"),
        target.get("capability"),
        target.get("tool_name"),
        target.get("mcp_tool"),
        tool_key,
    ]
    out: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, str) and candidate and candidate not in out:
            out.append(candidate)
    return out


def infer_action(tool_key: str, target: Dict[str, Any], explicit_action: Optional[str]) -> str:
    candidates = tool_action_candidates(tool_key, target, explicit_action)
    return candidates[0] if candidates else tool_key


def tool_looks_write_like(target: Dict[str, Any]) -> bool:
    if target.get("access") == "write":
        return True
    if target.get("approval_required"):
        return True
    action_parts = [
        target.get("capability", ""),
        target.get("tool_name", ""),
        target.get("mcp_tool", ""),
        target.get("name", ""),
    ]
    normalized = re.split(r"[^a-zA-Z0-9]+", " ".join(str(part) for part in action_parts).lower())
    return any(word in WRITE_ACTION_WORDS for word in normalized)


def allowed_action_matches(allowed_action: Any, candidates: list[str]) -> bool:
    if not allowed_action:
        return True
    if isinstance(allowed_action, str):
        allowed = [allowed_action]
    elif isinstance(allowed_action, list) and all(isinstance(item, str) for item in allowed_action):
        allowed = allowed_action
    else:
        raise PolicyEvaluationError("'allowed_action' must be a string or list of strings")
    return "*" in allowed or bool(set(allowed).intersection(candidates))


def policy_contract_from(policy: Dict[str, Any], matched_rule: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    contract = {key: deepcopy(policy[key]) for key in POLICY_CONTRACT_KEYS if key in policy}
    if "condition" not in contract and "approval_when" in policy:
        contract["condition"] = policy["approval_when"]
    if "condition" not in contract and "requires_approval_when" in policy:
        contract["condition"] = policy["requires_approval_when"]
    if matched_rule:
        for key in POLICY_CONTRACT_KEYS:
            if key in matched_rule:
                contract[key] = deepcopy(matched_rule[key])
        if "condition" not in matched_rule and "when" in matched_rule and "condition" not in contract:
            contract["condition"] = matched_rule["when"]
        contract["matched_rule"] = deepcopy(matched_rule)
    return contract


def is_conditional_approval_contract(policy: Dict[str, Any]) -> bool:
    return bool(
        policy.get("intent")
        and (policy.get("conditions") or policy.get("condition") or policy.get("expires_at"))
    )


def policy_decision_for_call(
    tool_key: str,
    target: Dict[str, Any],
    params: Dict[str, Any],
    agent_id: str,
    request_policy: Dict[str, Any],
    execution_state: Dict[str, Any],
    action: Optional[str],
    execution_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    action_value = infer_action(tool_key, target, action)
    context = build_policy_context(
        params,
        execution_state,
        {
            "tool": tool_key,
            "tool_name": target.get("tool_name") or target.get("mcp_tool") or tool_key.split(".")[-1],
            "capability": target.get("capability"),
            "risk_level": target.get("risk_level", "unknown"),
            "agent_id": agent_id,
            "action": action_value,
            "env": (execution_context or {}).get("env", "dev"),
            "workflow_id": (execution_context or {}).get("workflow_id", ""),
            "user_id": (execution_context or {}).get("user_id", ""),
            "workspace_id": (execution_context or {}).get("workspace_id", ""),
            "resources": (execution_context or {}).get("resources", {}),
        },
    )
    
    # Merge tool-specific registry policy with request-time overrides
    effective_policy = {**target.get("policy", {}), **request_policy}

    if effective_policy.get("approval") == "skip":
        return {
            "effect": "allow",
            "requires_approval": False,
            "reason": "Policy explicitly skipped approval",
            "policy": policy_contract_from(effective_policy),
        }

    if is_conditional_approval_contract(effective_policy):
        return {
            "effect": "require_approval",
            "requires_approval": True,
            "reason": effective_policy.get("reason") or f"{tool_key} requires conditional approval",
            "policy": policy_contract_from(effective_policy),
        }

    rules = effective_policy.get("rules", [])
    if rules and not isinstance(rules, list):
        raise HTTPException(status_code=400, detail="'policy.rules' must be a list")
    for rule in rules:
        if not isinstance(rule, dict):
            raise HTTPException(status_code=400, detail="Every policy rule must be an object")
        when = rule.get("when")
        try:
            matched = evaluate_condition(when, context)["passed"] if when else True
        except PolicyEvaluationError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid policy rule: {exc}") from exc
        if not matched:
            continue
        effect = str(rule.get("effect", "require_approval")).lower()
        if effect in {"deny", "block"}:
            return {
                "effect": "deny",
                "requires_approval": False,
                "reason": rule.get("reason", "Denied by policy"),
                "policy": policy_contract_from(request_policy, rule),
            }
        if effect in {"allow", "permit"}:
            return {
                "effect": "allow",
                "requires_approval": False,
                "reason": rule.get("reason", "Allowed by policy"),
                "policy": policy_contract_from(effective_policy, rule),
            }
        if effect in {"require_approval", "approval_required", "human_approval"}:
            return {
                "effect": "require_approval",
                "requires_approval": True,
                "reason": rule.get("reason") or effective_policy.get("reason") or f"{tool_key} requires approval",
                "policy": policy_contract_from(effective_policy, rule),
            }
        raise HTTPException(status_code=400, detail=f"Unsupported policy rule effect '{effect}'")

    approval_when = effective_policy.get("approval_when") or effective_policy.get("requires_approval_when")
    if approval_when:
        try:
            if evaluate_condition(approval_when, context)["passed"]:
                return {
                    "effect": "require_approval",
                    "requires_approval": True,
                    "reason": effective_policy.get("reason") or f"{tool_key} matched approval policy",
                    "policy": policy_contract_from(effective_policy),
                }
        except PolicyEvaluationError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid approval policy: {exc}") from exc

    if effective_policy.get("approval") == "required" or target.get("approval_required") or tool_looks_write_like(target):
        return {
            "effect": "require_approval",
            "requires_approval": True,
            "reason": effective_policy.get("reason") or f"{tool_key} requires approval",
            "policy": policy_contract_from(effective_policy),
        }

    return {
        "effect": "allow",
        "requires_approval": False,
        "reason": "Allowed",
        "policy": policy_contract_from(effective_policy),
    }

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    init_db()
    load_persistence()
    yield
    await http_client.aclose()


async def post_mcp_jsonrpc_with_retries(
    mcp_url: str,
    message: Dict[str, Any],
    session_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Post an MCP JSON-RPC message with retries, exponential backoff and jitter.

    Logs attempt counts and re-raises the last exception if all retries fail.
    """
    headers_base = dict(MCP_HEADERS)
    if session_id:
        headers_base["mcp-session-id"] = session_id
    if request_id:
        headers_base["mcp-request-id"] = request_id

    last_exc: Optional[Exception] = None
    for attempt in range(1, max(1, RETRY_MAX) + 1):
        # copy headers each attempt in case something mutates
        headers = dict(headers_base)
        try:
            # Use a fresh message id per attempt to avoid JSON-RPC dedup problems
            attempt_message = dict(message)
            if isinstance(attempt_message.get("id"), str):
                attempt_message["id"] = str(uuid.uuid4())

            response = await http_client.post(mcp_url, json=attempt_message, headers=headers)
            response.raise_for_status()
            parsed = parse_mcp_response(response)
            ensure_jsonrpc_success(parsed)

            if attempt > 1:
                logger.info(
                    "mcp_call_succeeded_after_retries",
                    mcp_url=mcp_url,
                    attempts=attempt,
                )
            return parsed, response.headers.get("mcp-session-id"), attempt

        except (httpx.HTTPError, json.JSONDecodeError, UpstreamToolError, ValueError) as exc:
            last_exc = exc

            # Log transient error and whether we'll retry
            will_retry = attempt < RETRY_MAX
            logger.warning(
                "mcp_call_failed",
                mcp_url=mcp_url,
                attempt=attempt,
                will_retry=will_retry,
                error=str(exc),
            )

            if not will_retry:
                # all attempts exhausted; re-raise the last exception
                raise

            # exponential backoff + jitter (ms -> seconds)
            backoff_ms = RETRY_BASE_MS * (RETRY_BACKOFF_FACTOR ** (attempt - 1))
            jitter = random.uniform(0, RETRY_JITTER_MS)
            sleep_s = max(0.0, (backoff_ms + jitter) / 1000.0)
            await asyncio.sleep(sleep_s)

    # If somehow loop exits without returning, raise last exception
    if last_exc:
        raise last_exc
    raise UpstreamToolError("MCP JSON-RPC failed without exception")
app = FastAPI(title="AgentGate - MCP Gateway", version="0.1.0", lifespan=lifespan)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "provider"


def hosted_gateway_url(slug: str) -> str:
    return f"https://{slug}.{HOSTED_GATEWAY_DOMAIN}"


def public_url(path: str) -> str:
    return f"{PUBLIC_BASE_URL}{path if path.startswith('/') else '/' + path}"


def create_provider_record(
    name: str,
    owner_email: str,
    *,
    slug: Optional[str] = None,
    saas: Optional[str] = None,
    approval_slack_webhook_url: Optional[str] = None,
    approval_email: Optional[str] = None,
) -> Dict[str, Any]:
    provider_id = f"prov_{secrets.token_hex(8)}"
    base_slug = slugify(slug or saas or name)
    candidate = base_slug
    suffix = 1
    while any(provider["slug"] == candidate for provider in DYNAMIC_PROVIDERS.values()):
        suffix += 1
        candidate = f"{base_slug}-{suffix}"
    onboarding_token = secrets.token_urlsafe(24)
    gateway_url = hosted_gateway_url(candidate)
    provider = {
        "id": provider_id,
        "slug": candidate,
        "name": name,
        "saas": saas or candidate,
        "owner_email": owner_email,
        "status": "connected",
        "onboarding_token": onboarding_token,
        "gateway_url": gateway_url,
        "mcp_url": f"{gateway_url}/mcp",
        "approval_slack_webhook_url": approval_slack_webhook_url,
        "approval_email": approval_email,
        "created_at": time.time(),
        "created_at_iso": now_iso(),
    }
    DYNAMIC_PROVIDERS[provider_id] = provider
    return provider


def verify_onboarding_token(provider_id: str, token: str) -> bool:
    provider = DYNAMIC_PROVIDERS.get(provider_id)
    return bool(provider and provider.get("onboarding_token") == token)


def create_provider_key(provider_id: str, name: str = "primary") -> Dict[str, Any]:
    if provider_id not in DYNAMIC_PROVIDERS:
        raise HTTPException(status_code=404, detail="Provider not found")
    raw_key = f"pk_{secrets.token_urlsafe(24)}"
    PROVIDER_KEYS[raw_key] = provider_id
    return {
        "key_id": f"pkey_{secrets.token_hex(8)}",
        "provider_key": raw_key,
        "name": name,
        "created_at": now_iso(),
    }


def verify_provider_key(provider_id: str, raw_key: Optional[str]) -> bool:
    return bool(raw_key and PROVIDER_KEYS.get(raw_key) == provider_id)


def dynamic_tool_card(tool_key: str, target: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": tool_key,
        "name": target["name"],
        "capability": target.get("capability"),
        "description": target.get("description"),
        "tags": target.get("tags", []),
        "auth_note": target.get("auth_note", "Scoped AgentGate token required"),
        "risk_level": target.get("risk_level", "unknown"),
        "approval_required": bool(target.get("approval_required", False)),
        "configured": bool(target.get("mcp_url")),
        "input_schema": target.get("input_schema", {}),
        "output_schema": target.get("output_schema", {}),
        "examples": target.get("examples", []),
        "retry": target.get("retry", {}),
        "idempotency": target.get("idempotency", {}),
        "provider_id": target.get("provider_id"),
        "gateway_url": target.get("gateway_url"),
    }


def get_tool_target(tool_key: str) -> Optional[Dict[str, Any]]:
    return DYNAMIC_TOOLS.get(tool_key) or get_registry_target(tool_key)


def list_all_tool_cards() -> list[Dict[str, Any]]:
    dynamic_cards = [dynamic_tool_card(key, target) for key, target in DYNAMIC_TOOLS.items()]
    return list_registry_tool_cards() + dynamic_cards


def insert_tool(
    provider_id: str,
    name: str,
    capability_card: Dict[str, Any],
    mcp_tool: Optional[str],
    mcp_url: Optional[str],
    approval_required: bool,
    risk_level: str,
) -> Dict[str, Any]:
    provider = DYNAMIC_PROVIDERS.get(provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    clean_name = slugify(name).replace("-", "_")
    tool_key = f"{provider_id}/{clean_name}"
    if tool_key in DYNAMIC_TOOLS:
        tool_key = f"{tool_key}_{secrets.token_hex(3)}"
    target = {
        "name": capability_card.get("name") or name,
        "capability": capability_card.get("capability") or f"{provider['slug']}.{clean_name}",
        "mcp_url": mcp_url or provider["mcp_url"],
        "tool_name": mcp_tool or clean_name,
        "description": capability_card.get("description") or f"Call {name} through {provider['name']}.",
        "auth_note": capability_card.get("auth_note", "Scoped AgentGate token required"),
        "risk_level": risk_level or capability_card.get("risk_level", "unknown"),
        "approval_required": bool(approval_required),
        "tags": capability_card.get("tags", ["connected", provider["slug"]]),
        "input_schema": capability_card.get("input_schema", {}),
        "output_schema": capability_card.get("output_schema", {}),
        "examples": capability_card.get("examples", []),
        "retry": capability_card.get("retry", {}),
        "idempotency": capability_card.get("idempotency", {}),
        "provider_id": provider_id,
        "gateway_url": provider["gateway_url"],
    }
    DYNAMIC_TOOLS[tool_key] = target
    save_to_db("dynamic_tools", tool_key, target)
    return {
        "id": f"tool_{secrets.token_hex(8)}",
        "key": tool_key,
        "gateway_url": provider["gateway_url"],
        "mcp_url": target["mcp_url"],
        "tool_name": target["tool_name"],
    }


def list_provider_tools(provider_id: str) -> list[Dict[str, Any]]:
    return [
        {"key": key, **dynamic_tool_card(key, target)}
        for key, target in DYNAMIC_TOOLS.items()
        if target.get("provider_id") == provider_id
    ]


def launch_connector_tools(saas: str) -> list[Dict[str, Any]]:
    normalized = slugify(saas)
    if normalized == "github":
        return [
            {
                "name": "create_issue",
                "capability_card": {
                    "name": "GitHub Create Issue",
                    "description": "Create a GitHub issue with approval-friendly inputs.",
                    "capability": "github.issue.create",
                    "tags": ["github", "issue", "launch"],
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "owner": {"type": "string"},
                            "repo": {"type": "string"},
                            "title": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["owner", "repo", "title"],
                    },
                    "retry": {"safe": False, "max_attempts": 1},
                    "idempotency": {"mode": "caller_provided", "key_fields": ["owner", "repo", "title"]},
                },
                "mcp_tool": "github_create_issue",
                "approval_required": True,
                "risk_level": "medium",
            }
        ]
    if normalized == "notion":
        return [
            {
                "name": "create_page",
                "capability_card": {
                    "name": "Notion Create Page",
                    "description": "Create a Notion page from structured title and content.",
                    "capability": "notion.page.create",
                    "tags": ["notion", "page", "launch"],
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "parent_id": {"type": "string"},
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["parent_id", "title"],
                    },
                    "retry": {"safe": False, "max_attempts": 1},
                    "idempotency": {"mode": "caller_provided", "key_fields": ["parent_id", "title"]},
                },
                "mcp_tool": "notion_create_page",
                "approval_required": True,
                "risk_level": "medium",
            }
        ]
    if normalized == "linear":
        return [
            {
                "name": "create_issue",
                "capability_card": {
                    "name": "Linear Create Issue",
                    "description": "Create a Linear issue in a scoped team.",
                    "capability": "linear.issue.create",
                    "tags": ["linear", "issue", "launch"],
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "team_id": {"type": "string"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["team_id", "title"],
                    },
                    "retry": {"safe": False, "max_attempts": 1},
                    "idempotency": {"mode": "caller_provided", "key_fields": ["team_id", "title"]},
                },
                "mcp_tool": "linear_create_issue",
                "approval_required": True,
                "risk_level": "medium",
            }
        ]
    return []

def normalized_allowed_tools(policy: Dict[str, Any]) -> Optional[set[str]]:
    allowed_tools = policy.get("allowed_tools") or policy.get("tools")
    if not allowed_tools:
        return None
    if isinstance(allowed_tools, str):
        allowed_tools = [allowed_tools]
    if not isinstance(allowed_tools, list):
        return set()
    return {str(tool) for tool in allowed_tools}


def filter_tools_for_token(api_key: str, tools: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    policy = token_policy_for(api_key)
    allowed = normalized_allowed_tools(policy)
    if not allowed or "*" in allowed:
        return tools
    return [tool for tool in tools if tool.get("id") in allowed]


def discover_all_tools(query: str, limit: int = 10) -> list[Dict[str, Any]]:
    registry_matches = discover_registry_tools(query, limit)
    registry_ids = {tool["id"] for tool in registry_matches}
    tool_cards = registry_matches + [tool for tool in list_all_tool_cards() if tool["id"] not in registry_ids]
    terms = {term.lower() for term in query.split() if term.strip()}
    scored_tools: list[tuple[int, Dict[str, Any]]] = []
    for card in tool_cards:
        haystack = " ".join(
            [
                str(card.get("id") or ""),
                str(card.get("name") or ""),
                str(card.get("capability") or ""),
                str(card.get("description") or ""),
                str(card.get("auth_note") or ""),
                " ".join(card.get("tags", [])),
            ]
        ).lower()
        score = sum(1 for term in terms if term in haystack)
        if not query or score:
            scored_tools.append((score, card))
    scored_tools.sort(key=lambda item: (item[0], item[1]["configured"]), reverse=True)

    seen: set[str] = set()
    combined: list[Dict[str, Any]] = []
    for _, card in scored_tools:
        if card["id"] in seen:
            continue
        seen.add(card["id"])
        combined.append(card)
        if len(combined) >= limit:
            break
    return combined

def token_policy_for(api_key: str) -> Dict[str, Any]:
    if api_key in TOKEN_POLICIES:
        policy = deepcopy(TOKEN_POLICIES[api_key])
        policy.setdefault("scopes", [])
        return policy
    return {"scopes": FULL_ACCESS_SCOPES, "allowed_tools": ["*"]}


def normalized_scopes(policy: Dict[str, Any]) -> set[str]:
    scopes = policy.get("scopes", [])
    if isinstance(scopes, str):
        scopes = [scopes]
    if not isinstance(scopes, list):
        return set()
    return {str(scope) for scope in scopes}


def require_scope(api_key: str, required_scope: str) -> Dict[str, Any]:
    policy = token_policy_for(api_key)
    scopes = normalized_scopes(policy)
    if "*" in scopes or required_scope in scopes:
        return policy
    if required_scope in READ_SCOPES and "read:*" in scopes:
        return policy
    if required_scope.startswith("approvals:") and "approvals:*" in scopes:
        return policy
    if required_scope.startswith("providers:") and "providers:*" in scopes:
        return policy
    if required_scope.startswith("tools:") and "tools:*" in scopes:
        return policy
    raise HTTPException(status_code=403, detail=f"API key is missing required scope '{required_scope}'")


def require_tool_access(api_key: str, tool_key: str, target: Dict[str, Any]) -> Dict[str, Any]:
    policy = require_scope(api_key, "tools:call")
    allowed = normalized_allowed_tools(policy)
    if allowed and "*" not in allowed:
        if tool_key not in allowed:
            raise HTTPException(status_code=403, detail=f"API key is not scoped for tool '{tool_key}'")
    if policy.get("read_only") and tool_looks_write_like(target):
        raise HTTPException(status_code=403, detail=f"API key is read-only and cannot call '{tool_key}'")
    return policy


def agent_metadata_for(api_key: str, request_agent_id: str) -> Dict[str, Any]:
    policy = token_policy_for(api_key)
    token_agent_id = policy.get("agent_id")
    metadata = policy.get("agent_metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "id": token_agent_id or request_agent_id,
        "requested_id": request_agent_id,
        "metadata": metadata,
    }


def verify_api_key(api_key: Optional[str]) -> str:
    if not api_key:
        logger.warning("auth_missing_key")
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    if api_key not in API_KEYS and api_key not in TOKEN_POLICIES:
        logger.warning("auth_invalid_key", key_prefix=api_key[:8] + "...")
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key


def parse_mcp_response(response: httpx.Response) -> Optional[Dict[str, Any]]:
    if not response.content:
        return None

    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        return response.json()

    event_data: list[str] = []
    for line in response.text.splitlines():
        if line.startswith("data:"):
            event_data.append(line.removeprefix("data:").strip())
        elif not line and event_data:
            payload = "\n".join(event_data)
            if payload != "[DONE]":
                return json.loads(payload)
            event_data = []

    if event_data:
        payload = "\n".join(event_data)
        if payload != "[DONE]":
            return json.loads(payload)

    raise UpstreamToolError("MCP server returned an event stream without a JSON data event")


def ensure_jsonrpc_success(message: Optional[Dict[str, Any]]) -> None:
    if message and "error" in message:
        raise UpstreamToolError(f"MCP JSON-RPC error: {message['error']}")

async def call_mcp_tool(mcp_url: str, tool_name: str, params: Dict[str, Any], request_id: Optional[str] = None) -> tuple[Dict[str, Any], int]:
    _, session_id, _ = await post_mcp_jsonrpc_with_retries(
        mcp_url,
        {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentgate", "version": "0.1.0"},
            },
            },
        request_id=request_id,
    )

    await post_mcp_jsonrpc_with_retries(
        mcp_url,
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        session_id=session_id,
        request_id=request_id,
    )

    result_message, _, attempts = await post_mcp_jsonrpc_with_retries(
        mcp_url,
        {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": params},
        },
        session_id=session_id,
        request_id=request_id,
    )
    if not result_message or "result" not in result_message:
        raise UpstreamToolError("MCP server returned no tool result")

    return result_message["result"], attempts


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|section|article|header|footer|li|h[1-6])>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def fetch_url_direct(params: Dict[str, Any]) -> Dict[str, Any]:
    url = params.get("url")
    if not isinstance(url, str) or not url:
        raise UpstreamToolError("fetch.url requires params.url")

    response = await http_client.get(
        url,
        headers={
            "Accept": "text/html, text/plain, application/json;q=0.9, */*;q=0.8",
            "User-Agent": "AgentGate/0.1.0",
        },
    )
    response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    text = html_to_text(response.text) if "text/html" in content_type else response.text.strip()
    start_index = max(int(params.get("start_index", 0)), 0)
    max_length = max(int(params.get("max_length", 5000)), 1)

    return {
        "content": [{"type": "text", "text": text[start_index:start_index + max_length]}],
        "is_fallback": True,
        "source": "direct_http",
    }


async def call_fetch_tool(mcp_url: str, tool_name: str, params: Dict[str, Any], request_id: Optional[str] = None) -> tuple[Dict[str, Any], int]:
    try:
        return await call_mcp_tool(mcp_url, tool_name, params, request_id=request_id)
    except (httpx.HTTPError, json.JSONDecodeError, UpstreamToolError, ValueError) as exc:
        logger.warning("fetch_mcp_failed_using_direct_fallback", error=str(exc))
        result = await fetch_url_direct(params)
        return result, 0


async def execute_tool_call(tool_key: str, target: Dict[str, Any], params: Dict[str, Any], request_id: Optional[str] = None) -> tuple[Dict[str, Any], int]:
    mcp_url = target.get("mcp_url")
    tool_name = target.get("tool_name") or target.get("mcp_tool") or tool_key.split(".")[-1]
    if not mcp_url:
        raise HTTPException(status_code=501, detail=f"Tool '{tool_key}' has no MCP URL configured")

    if tool_key == "fetch.url":
        return await call_fetch_tool(mcp_url, tool_name, params, request_id=request_id)
    result, attempts = await call_mcp_tool(mcp_url, tool_name, params, request_id=request_id)
    return result, attempts


async def refetch_execution_state_for_approval(
    approval: Dict[str, Any],
    approve_body: Dict[str, Any],
    request_id: Optional[str],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    if "execution_state" in approve_body:
        return normalize_execution_state(approve_body["execution_state"]), {"source": "approval_payload"}
    if "state" in approve_body:
        return normalize_execution_state(approve_body["state"]), {"source": "approval_payload"}

    policy = approval.get("policy", {})
    refetch_spec = policy.get("state_refetch") or policy.get("refetch")
    if not refetch_spec:
        return normalize_execution_state({}), {"source": "frozen_checkpoint"}
    if not isinstance(refetch_spec, dict):
        raise HTTPException(status_code=400, detail="'state_refetch' must be an object")

    refetch_tool = refetch_spec.get("tool")
    if not isinstance(refetch_tool, str) or not refetch_tool:
        raise HTTPException(status_code=400, detail="'state_refetch.tool' is required")
    refetch_params = refetch_spec.get("params", {})
    if not isinstance(refetch_params, dict):
        raise HTTPException(status_code=400, detail="'state_refetch.params' must be an object")
    refetch_target = get_tool_target(refetch_tool)
    if not refetch_target:
        raise HTTPException(status_code=404, detail=f"State refetch tool '{refetch_tool}' not found")
    if refetch_target.get("approval_required") and not refetch_spec.get("allow_approval_required"):
        raise HTTPException(status_code=400, detail="State refetch tools must be read-only")

    result, retry_count = await execute_tool_call(refetch_tool, refetch_target, refetch_params, request_id=request_id)
    if "as" in refetch_spec:
        state_payload = {"variables": {str(refetch_spec["as"]): result}}
    elif isinstance(result, dict):
        state_payload = {"state": result}
    else:
        state_payload = {"state": {"result": result}}
    return normalize_execution_state(state_payload), {
        "source": "state_refetch",
        "tool": refetch_tool,
        "retry_count": retry_count,
    }


def evaluate_policy_contract_for_checkpoint(
    approval: Dict[str, Any],
    checkpoint: Dict[str, Any],
    frozen_checkpoint: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    policy = approval.get("policy", {})
    frozen = frozen_checkpoint or checkpoint
    context = build_policy_context(
        checkpoint.get("params", {}),
        checkpoint.get("execution_state", {}),
        {
            "tool": checkpoint.get("tool"),
            "tool_name": checkpoint.get("tool_name"),
            "risk_level": checkpoint.get("risk_level"),
            "agent_id": checkpoint.get("agent_id"),
            "action": checkpoint.get("action"),
        },
    )
    frozen_context = build_policy_context(
        frozen.get("params", {}),
        frozen.get("execution_state", {}),
        {
            "tool": frozen.get("tool"),
            "tool_name": frozen.get("tool_name"),
            "risk_level": frozen.get("risk_level"),
            "agent_id": frozen.get("agent_id"),
            "action": frozen.get("action"),
        },
    )

    expires_at = policy.get("expires_at")
    if expires_at:
        if not isinstance(expires_at, str):
            raise PolicyEvaluationError("'expires_at' must be an ISO timestamp string")
        if utc_now() >= parse_policy_datetime(expires_at):
            return {
                "decision": "cancel",
                "passed": False,
                "reasons": [f"Policy expired at {expires_at}"],
                "evaluated_at": now_iso(),
            }

    allowed_action = policy.get("allowed_action")
    if allowed_action and not allowed_action_matches(allowed_action, checkpoint.get("action_candidates", [])):
        return {
            "decision": "cancel",
            "passed": False,
            "reasons": [f"Action '{checkpoint.get('action')}' is not allowed by policy"],
            "evaluated_at": now_iso(),
        }

    condition_result = evaluate_condition(policy.get("condition"), context)
    if not condition_result["passed"]:
        decision = "cancel" if str(policy.get("on_condition_failed", "replan")).lower() == "cancel" else "replan"
        return {
            "decision": decision,
            "passed": False,
            "condition": condition_result,
            "reasons": [f"Condition failed: {condition_result['condition']}"],
            "evaluated_at": now_iso(),
        }

    conditions_result = evaluate_contract_conditions(policy.get("conditions"), context)
    drift_result = evaluate_conditional_drift(
        policy.get("conditions"),
        frozen_context,
        context,
        policy.get("threshold"),
    )
    if not conditions_result["passed"] or drift_result["exceeded"]:
        return {
            "decision": "requeue",
            "passed": False,
            "condition": condition_result,
            "conditions": conditions_result,
            "drift": drift_result,
            "reasons": ["Conditional execution contract drift exceeded threshold"],
            "evaluated_at": now_iso(),
        }

    return {
        "decision": "execute",
        "passed": True,
        "condition": condition_result,
        "conditions": conditions_result,
        "drift": drift_result,
        "reasons": [],
        "evaluated_at": now_iso(),
    }


def create_pending_approval(
    tool_key: str,
    target: Dict[str, Any],
    params: Dict[str, Any],
    agent_id: str,
    api_key: str,
    reason: str,
    policy_contract: Optional[Dict[str, Any]] = None,
    execution_state: Optional[Dict[str, Any]] = None,
    action: Optional[str] = None,
    request_id: Optional[str] = None,
    policy_decision: Optional[Dict[str, Any]] = None,
    entity_resolution: Optional[Dict[str, Any]] = None,
    execution_context: Optional[Dict[str, Any]] = None,
    credential: Optional[Dict[str, Any]] = None,
    idempotency: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    approval_id = f"appr_{uuid.uuid4().hex[:12]}"
    tool_name = target.get("tool_name") or target.get("mcp_tool") or tool_key.split(".")[-1]
    action_value = infer_action(tool_key, target, action)
    normalized_state = normalize_execution_state(execution_state)
    checkpoint = {
        "id": f"freeze_{uuid.uuid4().hex[:12]}",
        "version": 1,
        "request_id": request_id,
        "frozen_at": now_iso(),
        "tool": tool_key,
        "tool_name": tool_name,
        "risk_level": target.get("risk_level", "unknown"),
        "action": action_value,
        "action_candidates": tool_action_candidates(tool_key, target, action),
        "params": deepcopy(params),
        "agent_id": agent_id,
        "execution_state": normalized_state,
        "entity_resolution": deepcopy(entity_resolution or {}),
        "execution_context": deepcopy(execution_context or {}),
        "credential": deepcopy(credential or {}),
        "idempotency": deepcopy(idempotency or {}),
    }
    approval = {
        "id": approval_id,
        "status": "pending",
        "tool": tool_key,
        "tool_name": tool_name,
        "risk_level": target.get("risk_level", "unknown"),
        "reason": reason,
        "params": deepcopy(params),
        "agent_id": agent_id,
        "policy": deepcopy(policy_contract or {}),
        "checkpoint": checkpoint,
        "policy_decision": deepcopy(policy_decision or {}),
        "entity_resolution": deepcopy(entity_resolution or {}),
        "execution_context": deepcopy(execution_context or {}),
        "credential": deepcopy(credential or {}),
        "idempotency": deepcopy(idempotency or {}),
        "dashboard_url": public_url(f"/dashboard/approvals/{approval_id}"),
        "api_key_prefix": api_key[:8] + "...",
        "created_at": time.time(),
        "created_at_iso": now_iso(),
    }
    PENDING_APPROVALS[approval_id] = approval
    save_to_db("pending_approvals", approval_id, approval, "pending")
    return approval


def error_response(status_code: int, tool: Optional[str], message: str, start_time: float) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "tool": tool,
            "error": message,
            "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
        },
    )


def safe_json_value(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


def record_tool_call_trace(
    *,
    status: str,
    tool: Optional[str],
    params: Optional[Dict[str, Any]],
    agent_id: Optional[str],
    api_key: Optional[str],
    latency_ms: float,
    request_id: Optional[str],
    result: Any = None,
    error: Optional[Dict[str, Any]] = None,
    retry_count: Optional[int] = None,
    approval_id: Optional[str] = None,
    decision: Optional[Dict[str, Any]] = None,
    entity_resolution: Optional[Dict[str, Any]] = None,
    execution_context: Optional[Dict[str, Any]] = None,
    credential: Optional[Dict[str, Any]] = None,
    idempotency: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    auth_policy = token_policy_for(api_key) if api_key else {"scopes": []}
    agent = agent_metadata_for(api_key, agent_id or "unknown") if api_key else {"id": agent_id or "unknown"}
    entry = {
        "timestamp": now_iso(),
        "event": "tool_call",
        "trace_id": request_id or f"trace_{uuid.uuid4().hex}",
        "span_id": f"span_{uuid.uuid4().hex[:16]}",
        "status": status,
        "tool": tool,
        "input": safe_json_value(params or {}),
        "output": safe_json_value(result),
        "error": error,
        "latency_ms": latency_ms,
        "agent": agent,
        "execution_context": safe_json_value(execution_context or {}),
        "entity_resolution": safe_json_value(entity_resolution) if entity_resolution else None,
        "credential": safe_json_value(credential) if credential else None,
        "auth": {
            "api_key_prefix": api_key[:8] + "..." if api_key else None,
            "scopes": sorted(normalized_scopes(auth_policy)),
            "read_only": bool(auth_policy.get("read_only", False)),
        },
        "meta": {
            "retry_count": retry_count,
            "approval_id": approval_id,
            "decision": decision,
            "idempotency": idempotency,
        },
    }
    try:
        trace_dir = os.path.dirname(TRACE_EVENTS_PATH)
        if trace_dir:
            os.makedirs(trace_dir, exist_ok=True)
        with open(TRACE_EVENTS_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception as exc:
        logger.warning("trace_event_write_failed", error=str(exc))
    return entry


async def send_approval_notifications(approval: Dict[str, Any]) -> None:
    provider_id = get_tool_target(approval["tool"]).get("provider_id") if get_tool_target(approval["tool"]) else None
    provider = DYNAMIC_PROVIDERS.get(provider_id or "", {})
    slack_url = provider.get("approval_slack_webhook_url") or APPROVAL_SLACK_WEBHOOK_URL
    email_url = APPROVAL_EMAIL_WEBHOOK_URL
    email_to = provider.get("approval_email")
    message = {
        "approval_id": approval["id"],
        "tool": approval["tool"],
        "agent_id": approval["agent_id"],
        "risk_level": approval.get("risk_level"),
        "reason": approval.get("reason"),
        "dashboard_url": approval.get("dashboard_url"),
        "created_at": approval.get("created_at_iso"),
    }
    if slack_url:
        try:
            await http_client.post(
                slack_url,
                json={
                    "text": f"AgentGate approval needed for {approval['tool']}",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"*AgentGate approval needed*\n"
                                    f"Tool: `{approval['tool']}`\n"
                                    f"Agent: `{approval['agent_id']}`\n"
                                    f"Reason: {approval.get('reason', '')}\n"
                                    f"Review: {approval.get('dashboard_url')}"
                                ),
                            },
                        }
                    ],
                    "agentgate": message,
                },
            )
            approval.setdefault("notifications", []).append({"channel": "slack", "status": "sent"})
        except Exception as exc:
            logger.warning("approval_slack_notification_failed", approval_id=approval["id"], error=str(exc))
            approval.setdefault("notifications", []).append({"channel": "slack", "status": "failed", "error": str(exc)})
    if email_url or email_to:
        payload = {
            "to": email_to,
            "subject": f"Approval needed: {approval['tool']}",
            "text": (
                f"AgentGate approval needed for {approval['tool']}.\n"
                f"Agent: {approval['agent_id']}\n"
                f"Reason: {approval.get('reason', '')}\n"
                f"Review: {approval.get('dashboard_url')}\n"
            ),
            "agentgate": message,
        }
        if email_url:
            try:
                await http_client.post(email_url, json=payload)
                approval.setdefault("notifications", []).append({"channel": "email", "status": "sent"})
            except Exception as exc:
                logger.warning("approval_email_notification_failed", approval_id=approval["id"], error=str(exc))
                approval.setdefault("notifications", []).append({"channel": "email", "status": "failed", "error": str(exc)})
        else:
            approval.setdefault("notifications", []).append({"channel": "email", "status": "configured", "to": email_to})


def read_trace_events(limit: int = 100) -> list[Dict[str, Any]]:
    safe_limit = min(max(int(limit), 1), 1000)
    if not os.path.exists(TRACE_EVENTS_PATH):
        return []
    records: list[Dict[str, Any]] = []
    with open(TRACE_EVENTS_PATH, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(records) >= safe_limit:
            break
    records.reverse()
    return records


def export_trace_events(events: list[Dict[str, Any]], export_format: str) -> Any:
    if export_format == "langsmith":
        return [
            {
                "id": event["trace_id"],
                "name": event.get("tool") or "agentgate.tool_call",
                "run_type": "tool",
                "start_time": event["timestamp"],
                "end_time": event["timestamp"],
                "inputs": event.get("input", {}),
                "outputs": event.get("output"),
                "error": event.get("error"),
                "extra": {
                    "latency_ms": event.get("latency_ms"),
                    "agent": event.get("agent"),
                    "auth": event.get("auth"),
                    "entity_resolution": event.get("entity_resolution"),
                    "meta": event.get("meta"),
                    "status": event.get("status"),
                },
            }
            for event in events
        ]
    if export_format == "helicone":
        return [
            {
                "provider": "agentgate",
                "request": {
                    "id": event["trace_id"],
                    "tool": event.get("tool"),
                    "inputs": event.get("input", {}),
                    "user": event.get("agent", {}).get("id"),
                    "created_at": event["timestamp"],
                },
                "response": {
                    "body": event.get("output"),
                    "error": event.get("error"),
                    "latency_ms": event.get("latency_ms"),
                    "status": event.get("status"),
                },
                "properties": {
                    "agentgate.scopes": ",".join(event.get("auth", {}).get("scopes", [])),
                    "agentgate.tool": event.get("tool") or "",
                    "agentgate.entity_id": (
                        event.get("entity_resolution", {}).get("entity", {}).get("entity_id")
                        if isinstance(event.get("entity_resolution"), dict)
                        else ""
                    ),
                },
            }
            for event in events
        ]
    return events


@app.get("/tools")
async def tools(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "tools:read")
    tools_list = filter_tools_for_token(api_key, list_all_tool_cards())
    logger.info("tools_listed", api_key=api_key[:8] + "...", count=len(tools_list))
    return JSONResponse(content={"success": True, "tools": tools_list})


@app.post("/providers")
async def create_provider_endpoint(request: Request, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "providers:admin")
    body = await request.json()
    name = body.get("name")
    owner_email = body.get("owner_email")
    if not name or not owner_email:
        raise HTTPException(status_code=400, detail="'name' and 'owner_email' are required")
    provider = create_provider_record(
        name,
        owner_email,
        slug=body.get("slug"),
        saas=body.get("saas"),
        approval_slack_webhook_url=body.get("approval_slack_webhook_url"),
        approval_email=body.get("approval_email"),
    )
    logger.info("provider_created", provider_id=provider["id"], gateway_url=provider["gateway_url"], api_key=api_key[:8] + "...")
    return JSONResponse(content={"success": True, "provider": provider})


@app.post("/connect/{saas}")
async def connect_saas_endpoint(
    saas: str,
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "providers:admin")
    body = await request.json() if request.headers.get("content-length") else {}
    provider = create_provider_record(
        body.get("name") or f"{saas.title()} Tools",
        body.get("owner_email", "owner@example.com"),
        slug=body.get("slug") or saas,
        saas=saas,
        approval_slack_webhook_url=body.get("approval_slack_webhook_url"),
        approval_email=body.get("approval_email"),
    )
    created_tools = []
    for tool_spec in launch_connector_tools(saas):
        created_tools.append(
            insert_tool(
                provider["id"],
                tool_spec["name"],
                tool_spec["capability_card"],
                tool_spec.get("mcp_tool"),
                body.get("mcp_url") or provider["mcp_url"],
                bool(tool_spec.get("approval_required", True)),
                tool_spec.get("risk_level", "medium"),
            )
        )
    return JSONResponse(
        content={
            "success": True,
            "provider": provider,
            "gateway_url": provider["gateway_url"],
            "mcp_url": provider["mcp_url"],
            "tools": created_tools,
        }
    )


@app.post("/providers/{provider_id}/keys")
async def create_provider_key_endpoint(
    provider_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    is_admin = False
    if x_api_key:
        try:
            api_key = verify_api_key(x_api_key)
            require_scope(api_key, "providers:admin")
            is_admin = True
        except HTTPException:
            is_admin = False
    if not is_admin:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing onboarding token")
        token = authorization.split(None, 1)[1].strip()
        if not verify_onboarding_token(provider_id, token):
            raise HTTPException(status_code=401, detail="Invalid onboarding token")
    body = await request.json() if request.headers.get("content-length") else {}
    return JSONResponse(content={"success": True, "key": create_provider_key(provider_id, body.get("name", "primary"))})


@app.post("/providers/{provider_id}/tools")
async def create_tool_endpoint(
    provider_id: str,
    request: Request,
    x_provider_key: Optional[str] = Header(None, alias="X-Provider-Key"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    is_admin = False
    if x_api_key:
        try:
            api_key = verify_api_key(x_api_key)
            require_scope(api_key, "providers:admin")
            is_admin = True
        except HTTPException:
            is_admin = False
    if not is_admin and not verify_provider_key(provider_id, x_provider_key):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Provider-Key")

    body = await request.json()
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="'name' is required")
    capability_card = body.get("capability_card") or {}
    if not isinstance(capability_card, dict):
        raise HTTPException(status_code=400, detail="'capability_card' must be an object")
    tool = insert_tool(
        provider_id,
        name,
        capability_card,
        body.get("mcp_tool"),
        body.get("mcp_url"),
        bool(body.get("approval_required", True)),
        body.get("risk_level", "unknown"),
    )
    logger.info("tool_registered", provider_id=provider_id, tool_key=tool["key"], tool_id=tool["id"])
    return JSONResponse(content={"success": True, "tool": tool})


@app.get("/providers/{provider_id}/tools")
async def list_provider_tools_endpoint(provider_id: str, x_provider_key: Optional[str] = Header(None, alias="X-Provider-Key"), x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    is_admin = False
    if x_api_key:
        try:
            api_key = verify_api_key(x_api_key)
            require_scope(api_key, "providers:admin")
            is_admin = True
        except HTTPException:
            is_admin = False
    if not is_admin and not verify_provider_key(provider_id, x_provider_key):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Provider-Key")
    return JSONResponse(content={"success": True, "tools": list_provider_tools(provider_id)})


@app.get("/providers/{provider_id}")
async def get_provider_endpoint(provider_id: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "providers:admin")
    provider = DYNAMIC_PROVIDERS.get(provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    return JSONResponse(content={"success": True, "provider": provider})


@app.get("/gateway/{provider_slug}")
async def hosted_gateway_metadata(provider_slug: str):
    provider = next((item for item in DYNAMIC_PROVIDERS.values() if item["slug"] == provider_slug), None)
    if not provider:
        raise HTTPException(status_code=404, detail="Gateway not found")
    return JSONResponse(
        content={
            "success": True,
            "name": provider["name"],
            "gateway_url": provider["gateway_url"],
            "mcp_url": provider["mcp_url"],
            "tools": list_provider_tools(provider["id"]),
        }
    )

@app.post("/mcp")
async def global_mcp_proxy(request: Request):
    """Optional: A global entry point for MCP clients to hit if 
    not using provider-specific subdomains."""
    # Implementation logic for routing based on auth or headers
    return JSONResponse({"jsonrpc": "2.0", "result": {"status": "active"}})


@app.get("/discover")
async def discover(
    q: str = "",
    limit: int = 10,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "tools:read")
    safe_limit = min(max(limit, 1), 50)
    matches = filter_tools_for_token(api_key, discover_all_tools(q, safe_limit))
    logger.info(
        "tools_discovered",
        api_key=api_key[:8] + "...",
        query=q,
        limit=safe_limit,
        count=len(matches),
    )
    return JSONResponse(content={"success": True, "query": q, "tools": matches})


@app.post("/state/verify")
async def verify_state_endpoint(request: Request, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "state:verify")
    body = await request.json()
    intent = body.get("intent")
    required_fields = body.get("required_fields", [])
    assumed_state = body.get("assumed_state", {})
    current_state = body.get("current_state")
    on_mismatch = str(body.get("on_mismatch", "abort")).lower()
    if not isinstance(intent, str) or not intent.strip():
        raise HTTPException(status_code=400, detail="'intent' must be a non-empty string")
    if not isinstance(required_fields, list) or not all(isinstance(item, str) for item in required_fields):
        raise HTTPException(status_code=400, detail="'required_fields' must be a list of strings")
    if not isinstance(assumed_state, dict):
        raise HTTPException(status_code=400, detail="'assumed_state' must be an object")

    refetch_meta = {"source": "current_state"}
    if current_state is None:
        state_refetch = body.get("state_refetch")
        if not isinstance(state_refetch, dict):
            raise HTTPException(status_code=400, detail="'current_state' or 'state_refetch' is required")
        refetch_tool = state_refetch.get("tool")
        refetch_params = state_refetch.get("params", {})
        if not isinstance(refetch_tool, str) or not refetch_tool:
            raise HTTPException(status_code=400, detail="'state_refetch.tool' must be a non-empty string")
        if not isinstance(refetch_params, dict):
            raise HTTPException(status_code=400, detail="'state_refetch.params' must be an object")
        refetch_target = get_tool_target(refetch_tool)
        if not refetch_target:
            raise HTTPException(status_code=404, detail=f"Tool '{refetch_tool}' not found")
        require_tool_access(api_key, refetch_tool, refetch_target)
        current_state, _retry_count = await execute_tool_call(
            refetch_tool,
            refetch_target,
            refetch_params,
            request_id=str(uuid.uuid4()),
        )
        refetch_meta = {"source": "state_refetch", "tool": refetch_tool}

    if not isinstance(current_state, dict):
        raise HTTPException(status_code=400, detail="Current state must be an object")

    mismatches = []
    for field in required_fields:
        assumed = resolve_context_path(assumed_state, field)
        current = resolve_context_path(current_state, field)
        if assumed is _MISSING or current is _MISSING or assumed != current:
            mismatches.append(
                {
                    "field": field,
                    "assumed": safe_policy_value(assumed),
                    "current": safe_policy_value(current),
                }
            )

    params = body.get("params", {}) if isinstance(body.get("params", {}), dict) else {}
    context = build_policy_context(params, {"state": current_state})
    conditions_result = evaluate_contract_conditions(body.get("conditions", {}), context)
    passed = not mismatches and conditions_result["passed"]
    decision = "execute" if passed else ("replan" if on_mismatch == "replan" else "abort")
    status = "verified" if passed else ("replan_required" if decision == "replan" else "blocked")
    return JSONResponse(
        content={
            "success": True,
            "status": status,
            "decision": decision,
            "intent": intent,
            "refetch": refetch_meta,
            "verification": {
                "passed": passed,
                "mismatches": mismatches,
                "conditions": conditions_result,
                "checked_at": now_iso(),
            },
        }
    )


@app.post("/outcomes/reconcile")
async def reconcile_outcome_endpoint(request: Request, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "outcomes:reconcile")
    body = await request.json()
    return JSONResponse(content=reconcile_outcome_payload(body))


@app.get("/approvals")
async def list_approvals(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "approvals:read")
    approvals = sorted(PENDING_APPROVALS.values(), key=lambda item: item["created_at"], reverse=True)
    logger.info("approvals_listed", api_key=api_key[:8] + "...", count=len(approvals))
    return JSONResponse(content={"success": True, "approvals": approvals})


@app.get("/approvals/{approval_id}")
async def get_approval(approval_id: str, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "approvals:read")
    approval = PENDING_APPROVALS.get(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval '{approval_id}' not found")

    logger.info("approval_read", api_key=api_key[:8] + "...", approval_id=approval_id)
    return JSONResponse(content={"success": True, "approval": approval})


@app.post("/approvals/{approval_id}/reject")
async def reject_approval(
    approval_id: str,
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "approvals:write")
    approval = PENDING_APPROVALS.get(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval '{approval_id}' not found")
    if approval["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Approval '{approval_id}' is already {approval['status']}")

    body = await request.json() if request.headers.get("content-length") else {}
    approval["status"] = "rejected"
    approval["reviewed_at"] = time.perf_counter()
    approval["reviewed_by"] = body.get("reviewed_by", "human")
    approval["review_note"] = body.get("note", "")

    logger.info(
        "approval_rejected",
        api_key=api_key[:8] + "...",
        approval_id=approval_id,
        reviewed_by=approval["reviewed_by"],
    )
    return JSONResponse(content={"success": True, "approval": approval})


@app.post("/approvals/{approval_id}/approve")
async def approve_approval(
    approval_id: str,
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    start_time = time.perf_counter()
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "approvals:write")
    approval = PENDING_APPROVALS.get(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"Approval '{approval_id}' not found")
    if approval["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Approval '{approval_id}' is already {approval['status']}")

    body = await request.json() if request.headers.get("content-length") else {}
    tool_key = approval["tool"]
    target = get_tool_target(tool_key)
    if not target:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_key}' not found")

    request_id = str(uuid.uuid4())
    approval["review_status"] = "validating"
    approval["reviewed_at"] = time.time()
    approval["reviewed_at_iso"] = now_iso()
    approval["reviewed_by"] = body.get("reviewed_by", "human")
    approval["review_note"] = body.get("note", "")

    logger.info(
        "approval_approved",
        api_key=api_key[:8] + "...",
        approval_id=approval_id,
        tool=tool_key,
        reviewed_by=approval["reviewed_by"],
    )

    try:
        frozen_checkpoint = approval.get("checkpoint") or {
            "tool": tool_key,
            "tool_name": approval.get("tool_name"),
            "risk_level": approval.get("risk_level"),
            "action": approval.get("tool_name") or tool_key,
            "action_candidates": tool_action_candidates(tool_key, target, None),
            "params": deepcopy(approval["params"]),
            "agent_id": approval.get("agent_id"),
            "execution_state": normalize_execution_state({}),
            "execution_context": approval.get("execution_context", {}),
            "credential": approval.get("credential", {}),
            "idempotency": approval.get("idempotency", {}),
        }
        execution_context = frozen_checkpoint.get("execution_context") or approval.get("execution_context") or {}
        credential = frozen_checkpoint.get("credential") or approval.get("credential") or resolve_provider_credential(
            target,
            execution_context,
            api_key,
        )
        idempotency = frozen_checkpoint.get("idempotency") or approval.get("idempotency")
        live_state, refetch_meta = await refetch_execution_state_for_approval(
            approval,
            body,
            request_id=request_id,
        )
        thawed_checkpoint = deepcopy(frozen_checkpoint)
        thawed_checkpoint["execution_state"] = merge_execution_states(
            frozen_checkpoint.get("execution_state", {}),
            live_state,
        )
        thawed_checkpoint["thawed_at"] = now_iso()
        thawed_checkpoint["refetch"] = refetch_meta
        approval_entity_record = approval.get("entity_resolution") or frozen_checkpoint.get("entity_resolution") or {}
        approval_entity = approval_entity_record.get("entity") if isinstance(approval_entity_record, dict) else None
        approval_entity_check = verify_entity_resolution(
            approval_entity,
            thawed_checkpoint["params"],
            thawed_checkpoint["execution_state"],
        )
        approval_entity_trace = approval_entity_check if approval_entity else None
        thawed_checkpoint["entity_resolution"] = approval_entity_check
        if not approval_entity_check["passed"]:
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            approval["status"] = "blocked"
            approval["review_status"] = "completed"
            approval["blocked_at"] = time.time()
            approval["blocked_at_iso"] = now_iso()
            approval["entity_resolution"] = approval_entity_check
            approval["block_reason"] = "Entity resolution mismatch"
            save_to_db("pending_approvals", approval_id, approval, approval["status"])
            record_tool_call_trace(
                status="blocked",
                tool=tool_key,
                params=thawed_checkpoint["params"],
                result={"status": "blocked", "reason": approval["block_reason"]},
                error={"status_code": 409, "message": approval["block_reason"]},
                agent_id=approval.get("agent_id"),
                api_key=api_key,
                latency_ms=latency_ms,
                request_id=request_id,
                approval_id=approval_id,
                entity_resolution=approval_entity_trace,
                execution_context=execution_context,
                credential=credential,
                idempotency=idempotency,
            )
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "status": "blocked",
                    "error": approval["block_reason"],
                    "entity_resolution": approval_entity_check,
                    "approval": approval,
                    "tool": tool_key,
                    "latency_ms": latency_ms,
                },
            )

        policy_evaluation = evaluate_policy_contract_for_checkpoint(
            approval,
            thawed_checkpoint,
            frozen_checkpoint=frozen_checkpoint,
        )
        approval["thawed_checkpoint"] = thawed_checkpoint
        approval["policy_evaluation"] = policy_evaluation

        decision = policy_evaluation["decision"]
        if decision == "requeue":
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            requeue_reason = "; ".join(policy_evaluation.get("reasons", [])) or "Conditional execution contract drifted"
            approval["status"] = "invalidated"
            approval["review_status"] = "completed"
            approval["invalidated_at"] = time.time()
            approval["invalidated_at_iso"] = now_iso()
            approval["requeue_reason"] = requeue_reason

            requeued_approval = create_pending_approval(
                tool_key=tool_key,
                target=target,
                params=thawed_checkpoint["params"],
                agent_id=approval.get("agent_id", "unknown"),
                api_key=api_key,
                reason=f"Re-queued after conditional drift: {requeue_reason}",
                policy_contract=approval.get("policy", {}),
                execution_state=thawed_checkpoint.get("execution_state", {}),
                action=thawed_checkpoint.get("action"),
                request_id=request_id,
                policy_decision=approval.get("policy_decision", {}),
                entity_resolution=approval_entity_trace,
                execution_context=execution_context,
                credential=credential,
                idempotency=idempotency,
            )
            requeued_approval["parent_approval_id"] = approval_id
            requeued_approval["requeue"] = {
                "reason": requeue_reason,
                "decision": policy_evaluation,
                "from_approval_id": approval_id,
            }
            approval["requeued_approval_id"] = requeued_approval["id"]
            save_to_db("pending_approvals", approval_id, approval, approval["status"])
            save_to_db("pending_approvals", requeued_approval["id"], requeued_approval, "pending")
            await send_approval_notifications(requeued_approval)
            record_tool_call_trace(
                status="requeued",
                tool=tool_key,
                params=thawed_checkpoint["params"],
                result={
                    "status": "requeued",
                    "approval_id": requeued_approval["id"],
                    "decision": policy_evaluation,
                },
                agent_id=approval.get("agent_id"),
                api_key=api_key,
                latency_ms=latency_ms,
                request_id=request_id,
                approval_id=approval_id,
                decision=policy_evaluation,
                entity_resolution=approval_entity_trace,
                execution_context=execution_context,
                credential=credential,
                idempotency=idempotency,
            )
            return JSONResponse(
                content={
                    "success": True,
                    "status": "requeued",
                    "requeue_reason": requeue_reason,
                    "approval_id": requeued_approval["id"],
                    "decision": policy_evaluation,
                    "approval": approval,
                    "requeued_approval": requeued_approval,
                    "tool": tool_key,
                    "latency_ms": latency_ms,
                }
            )

        if decision in {"cancel", "replan"}:
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            approval["status"] = "cancelled" if decision == "cancel" else "replan_required"
            approval["review_status"] = "completed"
            approval["completed_at"] = time.time()
            approval["completed_at_iso"] = now_iso()
            record_tool_call_trace(
                status=approval["status"],
                tool=tool_key,
                params=thawed_checkpoint["params"],
                result={"status": approval["status"], "decision": policy_evaluation},
                agent_id=approval.get("agent_id"),
                api_key=api_key,
                latency_ms=latency_ms,
                request_id=request_id,
                approval_id=approval_id,
                decision=policy_evaluation,
                entity_resolution=approval_entity_trace,
                execution_context=execution_context,
                credential=credential,
                idempotency=idempotency,
            )
            return JSONResponse(
                content={
                    "success": True,
                    "status": approval["status"],
                    "decision": policy_evaluation,
                    "approval": approval,
                    "tool": tool_key,
                    "latency_ms": latency_ms,
                }
            )

        approval["status"] = "approved"
        approval["approved_at"] = time.time()
        approval["approved_at_iso"] = now_iso()
        result, retry_count = await execute_tool_call(
            tool_key,
            target,
            thawed_checkpoint["params"],
            request_id=request_id,
        )
        if idempotency and idempotency.get("key"):
            IDEMPOTENCY_RECORDS[idempotency["key"]] = {
                "result": deepcopy(result),
                "meta": {
                    "request_id": request_id,
                    "retry_count": retry_count,
                    "approval_id": approval_id,
                    "idempotency": {**idempotency, "replayed": False},
                },
                "created_at": time.time(),
                "tool": tool_key,
            }
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        approval["status"] = "executed"
        approval["review_status"] = "completed"
        approval["executed_at"] = time.time()
        approval["executed_at_iso"] = now_iso()
        logger.info(
            "approval_executed",
            approval_id=approval_id,
            tool=tool_key,
            latency_ms=latency_ms,
            agent_id=approval["agent_id"],
        )
        record_tool_call_trace(
            status="executed",
            tool=tool_key,
            params=approval["params"],
            result=result,
            agent_id=approval.get("agent_id"),
            api_key=api_key,
            latency_ms=latency_ms,
            request_id=request_id,
            retry_count=retry_count,
            approval_id=approval_id,
            decision=policy_evaluation,
            entity_resolution=approval_entity_trace,
            execution_context=execution_context,
            credential=credential,
            idempotency={**idempotency, "replayed": False} if idempotency else None,
        )
        return JSONResponse(
            content={
                "success": True,
                "status": "executed",
                "approval": approval,
                "tool": tool_key,
                "result": result,
                "latency_ms": latency_ms,
                "decision": policy_evaluation,
                "meta": {
                    "request_id": request_id,
                    "retry_count": retry_count,
                    "idempotency": {**idempotency, "replayed": False} if idempotency else None,
                },
            }
        )
    except PolicyEvaluationError as exc:
        approval["status"] = "policy_failed"
        approval["review_status"] = "failed"
        approval["error"] = str(exc)
        return error_response(400, tool_key, f"Policy validation failed: {exc}", start_time)
    except HTTPException as exc:
        approval["status"] = "execution_failed" if approval.get("status") == "approved" else "policy_failed"
        approval["review_status"] = "failed"
        approval["error"] = str(exc.detail)
        raise
    except httpx.HTTPStatusError as exc:
        response_text = exc.response.text[:500]
        approval["status"] = "execution_failed"
        approval["review_status"] = "failed"
        approval["error"] = response_text
        return error_response(
            502,
            tool_key,
            f"Upstream MCP server returned HTTP {exc.response.status_code}: {response_text}",
            start_time,
        )
    except httpx.HTTPError as exc:
        approval["status"] = "execution_failed"
        approval["review_status"] = "failed"
        approval["error"] = str(exc)
        return error_response(502, tool_key, f"Could not reach upstream MCP server: {exc}", start_time)
    except (json.JSONDecodeError, UpstreamToolError, ValueError) as exc:
        approval["status"] = "execution_failed"
        approval["review_status"] = "failed"
        approval["error"] = str(exc)
        return error_response(502, tool_key, f"Invalid MCP response: {exc}", start_time)


@app.post("/call")
async def call_tool(request: Request, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    start_time = time.perf_counter()
    tool_key: Optional[str] = None
    params: Dict[str, Any] = {}
    agent_id = "unknown"
    api_key: Optional[str] = None
    request_id = str(uuid.uuid4())
    entity_resolution: Optional[Dict[str, Any]] = None
    entity_check: Optional[Dict[str, Any]] = None
    entity_trace: Optional[Dict[str, Any]] = None
    execution_context: Dict[str, Any] = {}
    credential: Optional[Dict[str, Any]] = None
    idempotency: Optional[Dict[str, Any]] = None

    try:
        api_key = verify_api_key(x_api_key)
        body = await request.json()
        tool_key = body.get("tool")
        params = body.get("params", {})
        agent_id = body.get("agent_id", "unknown")
        execution_context = normalize_execution_context(body.get("context"), agent_id, request_id)
        policy = body.get("policy", {})
        raw_execution_state = body.get("execution_state", body.get("state", {}))
        execution_state = normalize_execution_state(raw_execution_state)
        action = body.get("action")
        entity_resolution = normalize_entity_resolution(body.get("entity_resolution", body.get("entity")))
        failure_policy = normalize_failure_policy(body.get("failure_policy"))

        if not tool_key:
            raise HTTPException(status_code=400, detail="Missing 'tool' field")
        if not isinstance(params, dict):
            raise HTTPException(status_code=400, detail="'params' must be an object")
        if not isinstance(policy, dict):
            raise HTTPException(status_code=400, detail="'policy' must be an object")
        if action is not None and not isinstance(action, str):
            raise HTTPException(status_code=400, detail="'action' must be a string")

        entity_check = verify_entity_resolution(entity_resolution, params, execution_state)
        entity_trace = entity_check if entity_resolution else None
        if not entity_check["passed"]:
            logger.warning(
                "entity_resolution_blocked",
                tool=tool_key,
                agent_id=agent_id,
                entity_id=entity_resolution.get("entity_id") if entity_resolution else None,
                mismatches=entity_check.get("mismatches", []),
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Entity resolution mismatch",
                    "message": "Resolved entity does not match the action payload or execution state",
                    "entity_resolution": entity_check,
                },
            )

        target = get_tool_target(tool_key)
        if not target:
            raise HTTPException(status_code=404, detail=f"Tool '{tool_key}' not found")
        validate_tool_params(tool_key, target, params)
        token_policy = require_tool_access(api_key, tool_key, target)
        scope_decision = enforce_execution_scope(
            token_policy,
            tool_key,
            target,
            agent_id,
            execution_context,
            params,
            execution_state,
        )
        credential = resolve_provider_credential(target, execution_context, api_key)
        idempotency = resolve_idempotency(request, body, tool_key, target, params, execution_context)
        if idempotency and idempotency["key"] in IDEMPOTENCY_RECORDS:
            cached = deepcopy(IDEMPOTENCY_RECORDS[idempotency["key"]])
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            record_tool_call_trace(
                status="idempotent_replay",
                tool=tool_key,
                params=params,
                result=cached.get("result"),
                agent_id=agent_id,
                api_key=api_key,
                latency_ms=latency_ms,
                request_id=request_id,
                retry_count=0,
                entity_resolution=entity_trace,
                execution_context=execution_context,
                credential=credential,
                idempotency={**idempotency, "replayed": True},
            )
            return JSONResponse(
                content={
                    "success": True,
                    "status": "idempotent_replay",
                    "tool": tool_key,
                    "result": cached.get("result"),
                    "latency_ms": latency_ms,
                    "meta": {
                        **cached.get("meta", {}),
                        "request_id": request_id,
                        "idempotency": {**idempotency, "replayed": True},
                    },
                }
            )

        tool_name = target.get("tool_name") or target.get("mcp_tool") or tool_key.split(".")[-1]

        logger.info(
            "tool_call_start",
            tool=tool_key,
            upstream_tool=tool_name,
            agent_id=agent_id,
            api_key=api_key[:8] + "...",
            request_id=request_id,
            params_keys=list(params.keys()),
            env=execution_context.get("env"),
        )

        policy_decision = policy_decision_for_call(
            tool_key,
            target,
            params,
            agent_id,
            policy,
            execution_state,
            action,
            execution_context,
        )
        if policy_decision["effect"] == "deny":
            raise HTTPException(status_code=403, detail=policy_decision["reason"])

        if policy_decision["requires_approval"]:
            reason = policy_decision["reason"]
            approval = create_pending_approval(
                tool_key,
                target,
                params,
                agent_id,
                api_key,
                reason,
                policy_contract=policy_decision.get("policy", {}),
                execution_state=execution_state,
                action=action,
                request_id=request_id,
                policy_decision=policy_decision,
                entity_resolution=entity_trace,
                execution_context=execution_context,
                credential=credential,
                idempotency=idempotency,
            )
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.info(
                "tool_call_pending_approval",
                tool=tool_key,
                approval_id=approval["id"],
                risk_level=approval["risk_level"],
                policy_effect=policy_decision["effect"],
                latency_ms=latency_ms,
                agent_id=agent_id,
            )
            record_tool_call_trace(
                status="pending_approval",
                tool=tool_key,
                params=params,
                result={"status": "pending_approval", "approval_id": approval["id"]},
                agent_id=agent_id,
                api_key=api_key,
                latency_ms=latency_ms,
                request_id=request_id,
                approval_id=approval["id"],
                decision=policy_decision,
                entity_resolution=entity_trace,
                execution_context=execution_context,
                credential=credential,
                idempotency=idempotency,
            )
            await send_approval_notifications(approval)
            return JSONResponse(
                content={
                    "success": True,
                    "status": "pending_approval",
                    "tool": tool_key,
                    "approval_id": approval["id"],
                    "approval": approval,
                    "latency_ms": latency_ms,
                }
            )
        execution = await execute_with_failure_policy(
            tool_key,
            target,
            params,
            failure_policy,
            api_key=api_key,
            agent_id=agent_id,
            request_id=request_id,
            entity_resolution=entity_trace,
            execution_context=execution_context,
            credential=credential,
            idempotency=idempotency,
        )

        if execution["kind"] == "pending_approval":
            approval = execution["approval"]
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            record_tool_call_trace(
                status="pending_approval",
                tool=tool_key,
                params=params,
                result={"status": "pending_approval", "approval_id": approval["id"], "reason": execution["reason"]},
                agent_id=agent_id,
                api_key=api_key,
                latency_ms=latency_ms,
                request_id=request_id,
                approval_id=approval["id"],
                decision=approval.get("policy_decision"),
                entity_resolution=entity_trace,
                execution_context=execution_context,
                credential=credential,
                idempotency=idempotency,
            )
            return JSONResponse(
                content={
                    "success": True,
                    "status": "pending_approval",
                    "reason": execution["reason"],
                    "tool": tool_key,
                    "approval_id": approval["id"],
                    "approval": approval,
                    "failure_policy": execution["failure_policy"],
                    "latency_ms": latency_ms,
                }
            )

        result = execution["result"]
        meta = execution["meta"]
        retry_count = int(meta.get("retry_count", 0))
        if idempotency:
            meta["idempotency"] = {**idempotency, "replayed": False}
            IDEMPOTENCY_RECORDS[idempotency["key"]] = {
                "result": deepcopy(result),
                "meta": deepcopy(meta),
                "created_at": time.time(),
                "tool": tool_key,
                "scope": scope_decision,
            }

        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        logger.info("tool_call_success", tool=tool_key, latency_ms=latency_ms, agent_id=agent_id)
        record_tool_call_trace(
            status="success",
            tool=tool_key,
            params=params,
            result=result,
            agent_id=agent_id,
            api_key=api_key,
            latency_ms=latency_ms,
            request_id=request_id,
            retry_count=retry_count,
            entity_resolution=entity_trace,
            execution_context=execution_context,
            credential=credential,
            idempotency=meta.get("idempotency"),
        )

        return JSONResponse(
            content={
                "success": True,
                "tool": tool_key,
                "result": result,
                "latency_ms": latency_ms,
                "meta": meta,
            }
        )

    except HTTPException as exc:
        if api_key and tool_key:
            record_tool_call_trace(
                status="blocked" if exc.status_code == 409 and entity_check else "error",
                tool=tool_key,
                params=params,
                result=None,
                error={"status_code": exc.status_code, "message": str(exc.detail)},
                agent_id=agent_id,
                api_key=api_key,
                latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                request_id=request_id,
                entity_resolution=entity_trace or entity_resolution,
                execution_context=execution_context,
                credential=credential,
                idempotency=idempotency,
            )
        raise
    except httpx.HTTPStatusError as exc:
        response_text = exc.response.text[:500]
        logger.warning(
            "tool_call_upstream_status_error",
            tool=tool_key,
            status_code=exc.response.status_code,
            response=response_text,
        )
        if api_key:
            record_tool_call_trace(
                status="error",
                tool=tool_key,
                params=params,
                error={"status_code": 502, "message": response_text},
                agent_id=agent_id,
                api_key=api_key,
                latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                request_id=request_id,
                entity_resolution=entity_trace or entity_resolution,
                execution_context=execution_context,
                credential=credential,
                idempotency=idempotency,
            )
        return error_response(
            502,
            tool_key,
            f"Upstream MCP server returned HTTP {exc.response.status_code}: {response_text}",
            start_time,
        )
    except httpx.HTTPError as exc:
        logger.warning("tool_call_upstream_network_error", tool=tool_key, error=str(exc))
        if api_key:
            record_tool_call_trace(
                status="error",
                tool=tool_key,
                params=params,
                error={"status_code": 502, "message": str(exc)},
                agent_id=agent_id,
                api_key=api_key,
                latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                request_id=request_id,
                entity_resolution=entity_trace or entity_resolution,
                execution_context=execution_context,
                credential=credential,
                idempotency=idempotency,
            )
        return error_response(502, tool_key, f"Could not reach upstream MCP server: {exc}", start_time)
    except (json.JSONDecodeError, UpstreamToolError, ValueError) as exc:
        logger.warning("tool_call_upstream_protocol_error", tool=tool_key, error=str(exc))
        if api_key:
            record_tool_call_trace(
                status="error",
                tool=tool_key,
                params=params,
                error={"status_code": 502, "message": str(exc)},
                agent_id=agent_id,
                api_key=api_key,
                latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                request_id=request_id,
                entity_resolution=entity_trace or entity_resolution,
                execution_context=execution_context,
                credential=credential,
                idempotency=idempotency,
            )
        return error_response(502, tool_key, f"Invalid MCP response: {exc}", start_time)
    except Exception as exc:
        logger.exception("tool_call_exception", tool=tool_key)
        if api_key:
            record_tool_call_trace(
                status="error",
                tool=tool_key,
                params=params,
                error={"status_code": 500, "message": str(exc)},
                agent_id=agent_id,
                api_key=api_key,
                latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                request_id=request_id,
                entity_resolution=entity_trace or entity_resolution,
                execution_context=execution_context,
                credential=credential,
                idempotency=idempotency,
            )
        return error_response(500, tool_key, f"AgentGate internal error: {exc}", start_time)


@app.post("/call-tool")
async def call_tool_alias(request: Request, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """Alias endpoint for `/call` kept for backwards compatibility and clearer naming."""
    return await call_tool(request, x_api_key)


@app.get("/logs")
async def get_logs(limit: int = 100, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """Return the most recent structured log entries (JSON lines) from the log file.

    `limit` controls how many recent entries to return (max 1000).
    """
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "logs:read")
    try:
        safe_limit = min(max(int(limit), 1), 1000)
    except Exception:
        safe_limit = 100

    path = os.path.join(LOG_DIR, LOG_FILENAME)
    if not os.path.exists(path):
        logger.info("logs_fetched", api_key=api_key[:8] + "...", count=0)
        return JSONResponse(content={"success": True, "count": 0, "logs": []})

    # Read file and parse JSON lines from the end to get the most recent entries
    records = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except Exception:
                # Skip non-JSON lines
                continue
            records.append(entry)
            if len(records) >= safe_limit:
                break
        records.reverse()
    except Exception as exc:
        logger.warning("logs_read_error", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Could not read logs: {exc}")

    logger.info("logs_fetched", api_key=api_key[:8] + "...", count=len(records))
    return JSONResponse(content={"success": True, "count": len(records), "logs": records})


@app.get("/traces")
async def get_traces(limit: int = 100, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "traces:read")
    events = read_trace_events(limit)
    logger.info("traces_fetched", api_key=api_key[:8] + "...", count=len(events))
    return JSONResponse(content={"success": True, "count": len(events), "traces": events})


@app.get("/traces/export")
async def export_traces(
    format: str = "json",
    limit: int = 100,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    api_key = verify_api_key(x_api_key)
    require_scope(api_key, "traces:read")
    export_format = format.lower()
    if export_format not in {"json", "jsonl", "langsmith", "helicone"}:
        raise HTTPException(status_code=400, detail="format must be one of json, jsonl, langsmith, helicone")

    events = read_trace_events(limit)
    if export_format == "jsonl":
        body = "\n".join(json.dumps(event, sort_keys=True) for event in events)
        if body:
            body += "\n"
        return PlainTextResponse(content=body, media_type="application/x-ndjson")

    exported = export_trace_events(events, export_format)
    logger.info("traces_exported", api_key=api_key[:8] + "...", format=export_format, count=len(events))
    return JSONResponse(content={"success": True, "format": export_format, "count": len(events), "traces": exported})


@app.get("/", response_class=HTMLResponse)
async def root_welcome():
    """Simple landing page to avoid 404s when visiting the root URL."""
    return f"""
    <html>
        <head><title>AgentGate</title><style>body{{font-family:sans-serif;padding:40px;line-height:1.6;max-width:800px;margin:0 auto;}} code{{background:#eee;padding:2px 4px;border-radius:4px;}} .card{{border:1px solid #ddd;padding:20px;border-radius:8px;background:#f9f9f9;}}</style></head>
        <body>
            <h1>🛡️ AgentGate is Online</h1>
            <div class="card">
                <p>The gateway is running on <code>port 8000</code>. This is the <b>Control Plane</b> for your agents.</p>
                <p>To view pending requests, visit the <a href="/dashboard/approvals?api_key=ag_live_demo_key_123">Approvals Dashboard</a>.</p>
            </div>
        </body>
    </html>
    """


@app.get("/dashboard/approvals/{approval_id}", response_class=HTMLResponse)
async def approval_detail_dashboard(approval_id: str, api_key: Optional[str] = None):
    """Individual approval review page."""
    if not api_key:
        return "<h1>Authentication Required</h1><p>Please provide an <code>api_key</code>.</p>"
    
    try:
        verify_api_key(api_key)
        require_scope(api_key, "approvals:read")
    except HTTPException:
        return "<h1>Invalid API Key</h1>"

    approval = PENDING_APPROVALS.get(approval_id)
    if not approval:
        return f"<h1>Approval {escape(approval_id)} Not Found</h1><p>It may have been processed already.</p>"

    params_json = json.dumps(approval['params'], indent=2)
    
    return f"""
    <html>
        <head>
            <title>Review Approval {escape(approval_id)}</title>
            <style>
                body{{font-family:sans-serif;padding:40px;line-height:1.6;max-width:800px;margin:0 auto;}}
                pre{{background:#f4f4f4;padding:15px;border-radius:5px;overflow-x:auto;}}
                .actions{{margin-top:30px;}}
                button{{padding:10px 20px;font-size:16px;cursor:pointer;margin-right:10px;border:none;border-radius:4px;}}
                .approve{{background:#28a745;color:white;}}
                .reject{{background:#dc3545;color:white;}}
                .meta{{color:#666;font-size:0.9em;margin-bottom:20px;}}
            </style>
        </head>
        <body>
            <a href="/dashboard/approvals?api_key={escape(api_key)}">&larr; Back to List</a>
            <h1>Review Tool Call</h1>
            <div class="meta">
                ID: <code>{escape(approval_id)}</code><br>
                Agent: <b>{escape(approval['agent_id'])}</b><br>
                Risk Level: <span style="color:orange">{escape(approval['risk_level'])}</span>
            </div>
            
            <h3>Reason</h3>
            <p>{escape(approval['reason'])}</p>

            <h3>Tool: <code>{escape(approval['tool'])}</code></h3>
            <pre>{escape(params_json)}</pre>

            <div class="actions">
                <button class="approve" onclick="decision('approve')">Approve & Execute</button>
                <button class="reject" onclick="decision('reject')">Reject Call</button>
            </div>

            <script>
                async function decision(action) {{
                    const key = new URLSearchParams(window.location.search).get('api_key');
                    const resp = await fetch(`/approvals/{escape(approval_id)}/${{action}}`, {{
                        method: 'POST',
                        headers: {{'X-API-Key': key, 'Content-Type': 'application/json'}},
                        body: JSON.stringify({{reviewed_by: 'dashboard_user', note: 'Approved via detail dash'}})
                    }});
                    const result = await resp.json();
                    if(resp.ok) {{
                        alert('Status: ' + result.status);
                        window.location.href = '/dashboard/approvals?api_key=' + key;
                    }} else alert('Error: ' + JSON.stringify(result));
                }}
            </script>
        </body>
    </html>
    """


@app.get("/dashboard/approvals", response_class=HTMLResponse)
async def approvals_dashboard(api_key: Optional[str] = None):
    """Simple web dashboard to view and manage pending approvals."""
    if not api_key:
        return "<h1>Authentication Required</h1><p>Please provide an <code>api_key</code> query parameter.</p>"
    
    try:
        verify_api_key(api_key)
        require_scope(api_key, "approvals:read")
    except HTTPException:
        return "<h1>Invalid API Key</h1>"

    pending = [a for a in PENDING_APPROVALS.values() if a["status"] == "pending"]
    
    rows = ""
    for app_req in pending:
        rows += f"""
        <tr>
            <td>{escape(app_req['id'])}</td>
            <td>{escape(app_req['tool'])}</td>
            <td>{escape(app_req['agent_id'])}</td>
            <td>{escape(app_req['reason'])}</td>
            <td>
                <button onclick="decision('{app_req['id']}', 'approve')">Approve</button>
                <button onclick="decision('{app_req['id']}', 'reject')">Reject</button>
            </td>
        </tr>
        """

    return f"""
    <html>
        <head><title>AgentGate Approvals</title><style>body{{font-family:sans-serif;padding:20px;}} table{{width:100%;border-collapse:collapse;}} th,td{{text-align:left;padding:10px;border-bottom:1px solid #ddd;}} button{{cursor:pointer;margin-right:5px;}}</style></head>
        <body>
            <h1>AgentGate Pending Approvals</h1>
            <table>
                <tr><th>ID</th><th>Tool</th><th>Agent</th><th>Reason</th><th>Actions</th></tr>
                {rows or "<tr><td colspan='5'>No pending approvals</td></tr>"}
            </table>
            <script>
                async function decision(id, action) {{
                    const key = new URLSearchParams(window.location.search).get('api_key');
                    const resp = await fetch(`/approvals/${{id}}/${{action}}`, {{
                        method: 'POST',
                        headers: {{'X-API-Key': key, 'Content-Type': 'application/json'}},
                        body: JSON.stringify({{reviewed_by: 'dashboard_user'}})
                    }});
                    if(resp.ok) location.reload(); else alert('Error: ' + await resp.text());
                }}
            </script>
        </body>
    </html>
    """


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
