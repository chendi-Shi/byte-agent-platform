"""Minimal MCP stdio server (tools-only subset, protocol 2025-11-25)."""
import json
import sys

PROTOCOL = "2025-11-25"


def handle(request, tools):
    identity = request.get("id")
    if "id" not in request:
        return None
    response = {"jsonrpc": "2.0", "id": identity}
    method = request.get("method")
    params = request.get("params", {})
    if request.get("jsonrpc") != "2.0" or not isinstance(params, dict):
        response["error"] = {"code": -32600, "message": "Invalid request"}
    elif method == "initialize":
        response["result"] = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                              "serverInfo": {"name": "byte-agent-tools", "version": "0.1.0"}}
    elif method == "ping":
        response["result"] = {}
    elif method == "tools/list":
        response["result"] = {"tools": [t.spec() for t in tools.values()]}
    elif method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in tools:
            response["error"] = {"code": -32602, "message": "Unknown tool"}
        else:
            try:
                result = tools[name].call(params.get("arguments", {}))
                response["result"] = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                                      "structuredContent": result, "isError": False}
            except Exception as exc:
                response["result"] = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    else:
        response["error"] = {"code": -32601, "message": "Method not found"}
    return response


def serve(tools):
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be object")
            response = handle(request, tools)
        except (ValueError, TypeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON request"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
