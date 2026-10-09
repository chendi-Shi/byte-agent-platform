"""Official SDK stdio client and a synchronous Runtime tool adapter.

Only explicitly allowed tool names are adapted. Remote schemas and descriptions
are untrusted; readOnlyHint alone is not authorization to expose a remote tool.
"""
import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
import hashlib
import json
import threading


class MCPToolError(ValueError):
    pass


class MCPClient:
    def __init__(self, command, args=(), env=None, timeout=30):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.command, self.args, self.env, self.timeout = command, list(args), env, timeout
        self.session = None
        self.initialization = None
        self._stack = None

    async def __aenter__(self):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise RuntimeError("MCP client requires: pip install 'byte-agent-platform[mcp]'") from exc
        stack = AsyncExitStack()
        self._stack = stack
        try:
            read, write = await stack.enter_async_context(stdio_client(
                StdioServerParameters(command=self.command, args=self.args, env=self.env)))
            self.session = await stack.enter_async_context(ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=self.timeout)))
            self.initialization = await self.session.initialize()
            return self
        except BaseException:
            await stack.aclose()
            self.session = None
            raise

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._stack.__aexit__(exc_type, exc, tb)
        finally:
            self.session = None

    def _connected(self):
        if self.session is None:
            raise RuntimeError("MCP client is not connected")

    async def list_tools(self):
        self._connected()
        from mcp.types import PaginatedRequestParams
        tools, cursor, seen = [], None, set()
        while True:
            page = await self.session.list_tools(params=PaginatedRequestParams(cursor=cursor))
            tools.extend(tool.model_dump(by_alias=True, exclude_none=True) for tool in page.tools)
            cursor = page.nextCursor
            if not cursor:
                return tools
            if cursor in seen:
                raise MCPToolError("server repeated pagination cursor")
            seen.add(cursor)

    async def call_tool(self, name, arguments):
        self._connected()
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        response = await self.session.call_tool(name, arguments=arguments)
        if response.isError:
            raise MCPToolError("; ".join(getattr(item, "text", "") for item in response.content))
        if response.structuredContent is not None:
            result = response.structuredContent
        else:
            text = "\n".join(getattr(item, "text", "") for item in response.content)
            try:
                result = json.loads(text)
            except ValueError:
                result = {"text": text, "trust": "untrusted_evidence"}
        if len(json.dumps(result, ensure_ascii=False)) > 24000:
            raise MCPToolError("tool output too large")
        return result


class RemoteTool:
    def __init__(self, client, schema, revision):
        self.client, self.schema, self.revision = client, schema, revision
        self.name = schema["name"]

    def spec(self):
        # Runtime/OpenAI tools use only these fields, not SDK annotations.
        return {key: self.schema[key] for key in ("name", "description", "inputSchema") if key in self.schema}

    def call(self, arguments):
        return self.client.call_tool(self.name, arguments)


class SyncMCPClient:
    """Keep all SDK task groups in one thread/task; bridge sync Runtime calls.

    ``revision`` must identify the server's actual data snapshot for durable
    Runtime fingerprints. A transport command or schema cannot identify its data.
    """
    def __init__(self, command, args=(), env=None, timeout=30):
        self.options = (command, args, env, timeout)
        self.timeout = timeout
        self._ready = threading.Event()
        self._startup_error = None
        self._thread = None
        self._loop = None
        self._requests = None

    async def _serve(self):
        self._loop = asyncio.get_running_loop()
        self._requests = asyncio.Queue()
        try:
            async with MCPClient(*self.options) as client:
                self._schemas = await client.list_tools()
                self._ready.set()
                while True:
                    operation, arguments, future = await self._requests.get()
                    try:
                        if operation == "close":
                            future.set_result(None)
                            break
                        result = await client.call_tool(*arguments)
                        future.set_result(result)
                    except Exception as exc:
                        future.set_exception(exc)
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()

    def __enter__(self):
        self._thread = threading.Thread(target=lambda: asyncio.run(self._serve()), daemon=True)
        self._thread.start()
        if not self._ready.wait(self.timeout + 5):
            raise TimeoutError("MCP initialization timed out")
        if self._startup_error:
            raise self._startup_error
        return self

    def _request(self, operation, arguments):
        from concurrent.futures import Future
        if self._loop is None or self._thread is None or not self._thread.is_alive():
            raise RuntimeError("MCP client is closed")
        future = Future()
        self._loop.call_soon_threadsafe(self._requests.put_nowait, (operation, arguments, future))
        return future.result(self.timeout + 5)

    def call_tool(self, name, arguments):
        return self._request("call", (name, arguments))

    def tools(self, allowed, *, revision):
        if not revision:
            raise ValueError("provide the data snapshot revision")
        schemas = {item["name"]: item for item in self._schemas}
        if set(allowed) - schemas.keys():
            raise ValueError("allowed tool is absent from discovery")
        identity = hashlib.sha256((revision + json.dumps(self._schemas, sort_keys=True)).encode()).hexdigest()
        return {name: RemoteTool(self, schemas[name], identity) for name in allowed}

    def __exit__(self, exc_type, exc, tb):
        self._request("close", ())
        self._thread.join(self.timeout + 5)
        if self._thread.is_alive():
            raise TimeoutError("MCP shutdown timed out")
