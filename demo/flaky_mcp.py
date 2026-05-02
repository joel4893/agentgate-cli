from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import uvicorn
import uuid
import asyncio
import os

app = FastAPI()

# Fail the first N tools/call attempts, then succeed
FAIL_FIRST_N = int(os.getenv("FLAKY_FAIL_FIRST_N", "2"))
state = {"failures": 0}
system_state = {"status": "stable"}

@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body = await request.json()
    method = body.get("method")

    # Add a toggle for the demo to simulate environment drift
    if method == "demo/toggle_drift":
        system_state["status"] = "unstable"
        return {"success": True, "new_status": "unstable"}

    if method == "status":
        return JSONResponse(content={"jsonrpc": "2.0", "id": body.get("id"), "result": {"system_status": system_state["status"]}})

    if method == "initialize":
        headers = {"mcp-session-id": str(uuid.uuid4())}
        return JSONResponse(content={"jsonrpc": "2.0", "id": body.get("id"), "result": {}}, headers=headers)

    # notifications/initialized -> just ack
    if method == "notifications/initialized":
        return JSONResponse(content={"jsonrpc": "2.0", "result": {}})

    # tools/call -> flaky behavior
    if method == "tools/call":
        tool_name = body.get("params", {}).get("name")
        if tool_name == "get_status":
             return JSONResponse(content={"jsonrpc": "2.0", "id": body.get("id"), "result": {"system_status": system_state["status"]}})

        # simulate transient failure for first N attempts
        if state["failures"] < FAIL_FIRST_N:
            state["failures"] += 1
            # return 502 to simulate upstream transient error
            return JSONResponse(
                content={"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32000, "message": "Simulated transient failure"}},
                status_code=502,
                headers={"Content-Type": "application/json"}
            )

        # success path: return a JSON-RPC result matching AgentGate expectations
        params = body.get("params", {})
        arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
        if not isinstance(arguments, dict):
            arguments = {}
        # For fetch-like tools, return content in expected shape
        result = {
            "content": [{"type": "text", "text": f"Simulated fetch content for {arguments.get('url', '<no-url>')} (after {state['failures']} failures)"}],
            "source": "flaky_mcp",
            "is_fallback": False,
        }
        return JSONResponse(content={"jsonrpc": "2.0", "id": body.get("id"), "result": result})

    # default: method not supported
    return Response(content="Method not supported", status_code=400)

if __name__ == "__main__":
    port = int(os.getenv("FLAKY_MCP_PORT", "9000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
