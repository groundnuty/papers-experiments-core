"""A stand-in for the Onedata MCP server's four knowledge tools, for the outage check's tests: a real MCP stdio server.

    python outage_tools_server.py --relay http://127.0.0.1:<port>

Models: an MCP server over stdio with the four tools the outage check calls (`get_file_distribution`,
`list_space_providers`, `schedule_file_replication`, `get_transfer`), the allow-list the launcher sets
(`ONEDATA_MCP_TOOLS`) and a tool that answers an error. Each call is relayed, as JSON over HTTP, to the test process
(`outage_support.ToolRelay`), which answers it from the fakes: `{"result": ...}` (returned as JSON text), `{"error":
text}` (raised, so the client sees a tool error with that wording) or `{"hang": true}` (the tool never returns). Does
not model: the real server's arguments, wording or answers, which the live run measures. Every name is invented.
"""

import argparse
import asyncio
import json
import os
import urllib.request

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

DESCRIPTIONS = {
    "get_file_distribution": "Show which providers hold which blocks of a file.",
    "list_space_providers": "List the providers that support a space.",
    "schedule_file_replication": "Schedule a replication of a file to a provider.",
    "get_transfer": "Show a transfer's state.",
}


def build(relay: str) -> MCPServer:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # straight to the test's own loopback

    def relayed(tool: str, arguments: dict[str, str]) -> dict:
        body = json.dumps({"tool": tool, "arguments": arguments}).encode()
        request = urllib.request.Request(relay, data=body, headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=600) as answer:
            return json.loads(answer.read())

    async def call(tool: str, **arguments: str) -> str:
        reply = await asyncio.to_thread(relayed, tool, arguments)
        if reply.get("hang"):  # the server stays responsive; only this call never returns
            await asyncio.sleep(3600)
        if "error" in reply:  # a ToolError keeps its wording; anything else the server would hide
            raise ToolError(reply["error"])
        return json.dumps(reply["result"])

    async def get_file_distribution(file_id: str) -> str:
        return await call("get_file_distribution", file_id=file_id)

    async def list_space_providers(space_id: str) -> str:
        return await call("list_space_providers", space_id=space_id)

    async def schedule_file_replication(file_id: str, provider_id: str) -> str:
        return await call("schedule_file_replication", file_id=file_id, provider_id=provider_id)

    async def get_transfer(transfer_id: str) -> str:
        return await call("get_transfer", transfer_id=transfer_id)

    tools = (get_file_distribution, list_space_providers, schedule_file_replication, get_transfer)
    functions = {function.__name__: function for function in tools}
    allowed = [t for t in os.environ.get("ONEDATA_MCP_TOOLS", ",".join(DESCRIPTIONS)).split(",") if t]
    server = MCPServer("outage-tools")
    for name in allowed:
        server.tool(name=name, description=DESCRIPTIONS[name])(functions[name])
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--relay", required=True)
    build(parser.parse_args().relay).run("stdio")
