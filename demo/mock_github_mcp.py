from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import uvicorn
import uuid
import os
import json

app = FastAPI()

@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body = await request.json()
    method = body.get("method")
    request_id = body.get("id")

    if method == "initialize":
        headers = {"mcp-session-id": str(uuid.uuid4())}
        return JSONResponse(content={"jsonrpc": "2.0", "id": request_id, "result": {}}, headers=headers)

    if method == "notifications/initialized":
        return JSONResponse(content={"jsonrpc": "2.0", "result": {}})

    if method == "tools/call":
        tool_name = body.get("params", {}).get("name")
        args = body.get("params", {}).get("arguments", {})

        if tool_name == "create_issue":
            print(f"--- Mock GitHub MCP: Received request to create issue ---")
            print(f"  Repo: {args.get('repo')}")
            print(f"  Title: {args.get('title')}")
            print(f"  Body: {args.get('body')}")
            print(f"-------------------------------------------------------")
            # If we reached here, the gate didn't block us
            return JSONResponse(content={"jsonrpc": "2.0", "id": request_id, "result": {"issue_id": "gh_12345", "status": "created"}})
        
        return Response(content=f"Tool '{tool_name}' not supported by Mock GitHub MCP", status_code=400)

    return Response(content=f"Method '{method}' not supported", status_code=400)

if __name__ == "__main__":
    port = int(os.getenv("MOCK_GITHUB_MCP_PORT", "9001"))
    uvicorn.run(app, host="0.0.0.0", port=port)