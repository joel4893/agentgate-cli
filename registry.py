# registry.py - Agent-readable capability registry

import os
from typing import Any, Dict, List


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "fetch.url": {
        "name": "Web Fetch to Clean Markdown",
        "capability": "web.fetch",
        # Allow demo override via env var DEMO_FETCH_MCP_URL
        "mcp_url": os.getenv("DEMO_FETCH_MCP_URL", "https://remote.mcpservers.org/fetch/mcp"),
        "tool_name": "fetch",
        "description": "Fetch a public URL and return clean text/markdown content for agent research.",
        "auth_note": "Public - no auth required",
        "risk_level": "low",
        "approval_required": False,
        "tags": ["web", "research", "fetch", "markdown", "browser"],
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Public URL to fetch.",
                },
                "max_length": {
                    "type": "integer",
                    "description": "Maximum number of characters to return.",
                    "default": 5000,
                },
                "start_index": {
                    "type": "integer",
                    "description": "Character offset to start reading from.",
                    "default": 0,
                },
            },
            "required": ["url"],
        },
        "examples": [
            {
                "description": "Fetch a homepage for research.",
                "params": {"url": "https://github.com", "max_length": 1200},
            }
        ],
    },
    "github.create_issue": {
        "name": "GitHub Create Issue",
        "capability": "issue.create",
        "mcp_url": os.getenv("DEMO_GITHUB_MCP_URL", "http://mock-github-mcp:9001/mcp"),
        "tool_name": "create_issue",
        "description": "Create a GitHub issue in a repository.",
        "auth_note": "Requires GitHub PAT or delegated GitHub auth",
        "risk_level": "medium",
        "approval_required": True,
        "tags": ["github", "issue", "developer-tools", "project-management"],
        "policy": {
            "intent": "prevent_stale_issue_creation",
            "state_refetch": {
                "tool": "system.status",
                "params": {}
            },
            "conditions": {"system_status": "stable"},
            "threshold": "strict"
        },
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/name format."},
                "title": {"type": "string", "description": "Issue title."},
                "body": {"type": "string", "description": "Issue body."},
            },
            "required": ["repo", "title"],
        },
        "examples": [
            {
                "description": "Create a bug report.",
                "params": {
                    "repo": "acme/app",
                    "title": "Login fails on expired sessions",
                    "body": "Steps to reproduce...",
                },
            }
        ],
    },
    "system.status": {
        "name": "System Status Checker",
        "mcp_url": os.getenv("DEMO_FETCH_MCP_URL", "http://flaky-mcp:9000/mcp"),
        "tool_name": "get_status",
        "description": "Returns current system stability status.",
        "risk_level": "low",
        "approval_required": False,
        "input_schema": {
            "type": "object",
            "properties": {}
        },
    },
    "slack.post_message": {
        "name": "Slack Post Message",
        "capability": "message.send",
        "mcp_url": os.getenv("DEMO_SLACK_MCP_URL", "http://mock-saas-mcp:9002/mcp"),
        "tool_name": "slack_post_message",
        "description": "Send a message to a Slack channel.",
        "auth_note": "Requires Slack token",
        "risk_level": "medium",
        "approval_required": True,
        "tags": ["slack", "messaging", "notification", "chat"],
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Slack channel ID or name."},
                "text": {"type": "string", "description": "Message text."},
            },
            "required": ["channel", "text"],
        },
        "examples": [
            {
                "description": "Notify a support channel.",
                "params": {"channel": "#support", "text": "New high-priority ticket created."},
            }
        ],
    },
    "notion.create_page": {
        "name": "Notion Create Page",
        "capability": "document.create",
        "mcp_url": "",
        "tool_name": "create_page",
        "description": "Create a page in Notion.",
        "auth_note": "Requires Notion integration token",
        "risk_level": "medium",
        "approval_required": True,
        "tags": ["notion", "document", "wiki", "knowledge-base"],
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_id": {"type": "string", "description": "Parent page or database ID."},
                "title": {"type": "string", "description": "Page title."},
                "content": {"type": "string", "description": "Page content."},
            },
            "required": ["parent_id", "title"],
        },
        "examples": [
            {
                "description": "Create a research note.",
                "params": {"parent_id": "notion-parent-id", "title": "Research summary"},
            }
        ],
    },
    "linear.create_issue": {
        "name": "Linear Create Issue",
        "capability": "issue.create",
        "mcp_url": os.getenv("DEMO_LINEAR_MCP_URL", "http://mock-saas-mcp:9002/mcp"),
        "tool_name": "linear_create_issue",
        "description": "Create an issue in Linear.",
        "auth_note": "Requires Linear API key",
        "risk_level": "medium",
        "approval_required": False,
        "tags": ["linear", "issue", "project-management", "task"],
        "input_schema": {
            "type": "object",
            "properties": {
                "team_id": {"type": "string", "description": "Linear team ID."},
                "title": {"type": "string", "description": "Issue title."},
                "description": {"type": "string", "description": "Issue description."},
            },
            "required": ["team_id", "title"],
        },
        "examples": [
            {
                "description": "Create an engineering task.",
                "params": {"team_id": "team-id", "title": "Fix login callback race"},
            }
        ],
    },
    "vercel.deployment_status": {
        "name": "Vercel Deployment Status",
        "capability": "deployment.status.read",
        "mcp_url": os.getenv("DEMO_VERCEL_MCP_URL", "http://mock-saas-mcp:9002/mcp"),
        "tool_name": "vercel_get_deployment",
        "description": "Read the current status of a Vercel deployment.",
        "auth_note": "Requires Vercel token",
        "risk_level": "low",
        "approval_required": False,
        "tags": ["vercel", "deployment", "status", "release"],
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Vercel project ID or slug."},
                "deployment_id": {"type": "string", "description": "Vercel deployment ID."},
            },
            "required": ["project_id", "deployment_id"],
        },
        "examples": [
            {
                "description": "Check a production deployment before announcement.",
                "params": {"project_id": "web", "deployment_id": "dep_demo_123"},
            }
        ],
    },
}


def get_target(tool_name: str):
    return TOOL_REGISTRY.get(tool_name)


def list_tools() -> List[Dict[str, Any]]:
    return [tool_card(tool_id, config) for tool_id, config in TOOL_REGISTRY.items()]


def tool_card(tool_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": tool_id,
        "name": config["name"],
        "capability": config.get("capability"),
        "description": config.get("description"),
        "tags": config.get("tags", []),
        "auth_note": config.get("auth_note"),
        "risk_level": config.get("risk_level", "unknown"),
        "approval_required": bool(config.get("approval_required", False)),
        "configured": bool(config.get("mcp_url")),
        "input_schema": config.get("input_schema", {}),
        "examples": config.get("examples", []),
    }


def discover_tools(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    terms = {term.lower() for term in query.split() if term.strip()}
    scored_tools = []

    for tool_id, config in TOOL_REGISTRY.items():
        haystack_parts = [
            tool_id,
            config.get("name", ""),
            config.get("capability", ""),
            config.get("description", ""),
            config.get("auth_note", ""),
            " ".join(config.get("tags", [])),
        ]
        haystack = " ".join(haystack_parts).lower()
        score = sum(1 for term in terms if term in haystack)
        if not query or score:
            scored_tools.append((score, tool_card(tool_id, config)))

    scored_tools.sort(key=lambda item: (item[0], item[1]["configured"]), reverse=True)
    return [tool for _, tool in scored_tools[:limit]]
