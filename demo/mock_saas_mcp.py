from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import os
import uuid
import uvicorn


app = FastAPI()

state = {
    "deployment_status": "ready",
    "vercel_failures": 0,
    "linear_counter": 100,
    "slack_counter": 200,
}


def jsonrpc_result(request_id, result):
    return JSONResponse(content={"jsonrpc": "2.0", "id": request_id, "result": result})


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body = await request.json()
    method = body.get("method")
    request_id = body.get("id")

    if method == "demo/reset":
        state["deployment_status"] = "ready"
        state["vercel_failures"] = 0
        state["linear_counter"] = 100
        state["slack_counter"] = 200
        return {"success": True, "state": state}

    if method == "demo/toggle_deployment_drift":
        state["deployment_status"] = "failed"
        return {"success": True, "deployment_status": state["deployment_status"]}

    if method == "initialize":
        headers = {"mcp-session-id": str(uuid.uuid4())}
        return JSONResponse(content={"jsonrpc": "2.0", "id": request_id, "result": {}}, headers=headers)

    if method == "notifications/initialized":
        return JSONResponse(content={"jsonrpc": "2.0", "result": {}})

    if method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name") if isinstance(params, dict) else None
        args = params.get("arguments", {}) if isinstance(params, dict) else {}
        if not isinstance(args, dict):
            args = {}

        if tool_name == "vercel_get_deployment":
            fail_first = int(os.getenv("MOCK_VERCEL_FAIL_FIRST_N", "1"))
            if state["vercel_failures"] < fail_first:
                state["vercel_failures"] += 1
                return JSONResponse(
                    content={
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32000,
                            "message": "Vercel API transient 502 while reading deployment status",
                        },
                    },
                    status_code=502,
                )
            return jsonrpc_result(
                request_id,
                {
                    "provider": "vercel",
                    "project_id": args.get("project_id", "web"),
                    "deployment_id": args.get("deployment_id", "dep_demo_123"),
                    "deployment_status": state["deployment_status"],
                    "url": "https://agentgate-demo.vercel.app",
                },
            )

        if tool_name == "linear_create_issue":
            state["linear_counter"] += 1
            issue_id = f"LIN-{state['linear_counter']}"
            print("--- Mock Linear MCP: create issue ---")
            print(f"  Team: {args.get('team_id')}")
            print(f"  Title: {args.get('title')}")
            print("-------------------------------------")
            return jsonrpc_result(
                request_id,
                {
                    "provider": "linear",
                    "issue_id": issue_id,
                    "url": f"https://linear.app/acme/issue/{issue_id}",
                    "title": args.get("title"),
                    "status": "created",
                },
            )

        if tool_name == "slack_post_message":
            state["slack_counter"] += 1
            message_id = f"msg_{state['slack_counter']}"
            print("--- Mock Slack MCP: post message ---")
            print(f"  Channel: {args.get('channel')}")
            print(f"  Text: {args.get('text')}")
            print("------------------------------------")
            return jsonrpc_result(
                request_id,
                {
                    "provider": "slack",
                    "message_id": message_id,
                    "channel": args.get("channel"),
                    "status": "posted",
                },
            )

        return Response(content=f"Tool '{tool_name}' not supported by Mock SaaS MCP", status_code=400)

    return Response(content=f"Method '{method}' not supported", status_code=400)


if __name__ == "__main__":
    port = int(os.getenv("MOCK_SAAS_MCP_PORT", "9002"))
    uvicorn.run(app, host="0.0.0.0", port=port)
