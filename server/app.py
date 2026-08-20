"""The MCP server. Four read tools, one endpoint, no session state.

Run it locally from the repository root:

    uvicorn server.app:app

and the endpoint is http://127.0.0.1:8000/mcp.

Three runtime decisions, all of them made here so that step 5 is a deployment rather than a
redesign:

``stateless_http=True``. MCP has been stateless since the 2026-07-28 revision — no
``initialize`` handshake, no ``Mcp-Session-Id``, every request self-describing — so any
request can land on any instance. The old objection to running MCP on Lambda was session
affinity, and it no longer exists. Scale to zero is the natural shape now rather than a
compromise, which is worth saying out loud because plenty of MCP-on-AWS material still works
around a constraint that was removed.

``json_response=True``. One JSON body per response instead of an SSE stream. API Gateway's
HTTP API in front of a Lambda has nothing useful to do with a stream.

DNS rebinding protection off, explicitly, and this is the line that saves an hour of
debugging at step 5. The check exists to protect servers listening on loopback from browsers
on the same machine, which is not this situation. ``streamable_http_app()`` defaults to
``host="127.0.0.1"`` and arms the protection unless a ``TransportSecuritySettings`` is
passed, and API Gateway forwards its own ``Host`` header — so left on, every deployed
request comes back ``421 Misdirected Request``.

The import to watch: ``MCPServer`` from ``mcp.server.mcpserver``. ``FastMCP`` from
``mcp.server.fastmcp`` is the v1 line and speaks the old handshake.
"""

from __future__ import annotations

import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from server import toollog
from server.tools import TOOLS

INSTRUCTIONS = """
The delivery and margin data layer for a professional services firm: clients, engagements,
SOW line items, people, forty thousand time entries and their invoices.

Start with list_engagements. It returns burn and health on every row, so the decision about
what to look at properly comes from one call rather than a fan-out. Then pull
get_engagement_burn and get_financials only for engagements over 70% burn or not in the
green band.

Every figure comes from these tools. Do not compute a percentage from other numbers in a
response; if a figure is needed that no tool returns, say so rather than deriving it.

Two things the tools tell you that you must not override. get_engagement_burn returns
projection_confidence with a reason, and a low-confidence projection is reported with that
reason attached rather than acted on. get_time_summary returns weekly reporting coverage,
and a week flagged firm_wide_gap is a filing artifact: exclude it from every run rate and
projection, report it as a data note, and never describe it as a delivery slowdown.
""".strip()


mcp = MCPServer(
    "bizdata",
    title="BizData delivery and margin review",
    instructions=INSTRUCTIONS,
    version="0.3.0",
)

for tool in TOOLS:
    mcp.add_tool(tool)


# stdout locally; at step 5 the Lambda's stdout is CloudWatch Logs and this line is what
# lands there, unchanged and still one JSON object per line.
toollog.configure(sys.stdout)


app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
