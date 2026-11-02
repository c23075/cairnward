"""A stdio MCP server written with the official MCP Python SDK, for conformance tests:
Cairnward's upstream client must work with servers built on the reference
implementation, not only with the hand-written fake."""

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def list_tools(ctx, params):  # type: ignore[no-untyped-def]
    return types.ListToolsResult(tools=[
        types.Tool(name="shout", description="Upper-case the text.",
                   input_schema={"type": "object", "properties": {"text": {"type": "string"}},
                                 "required": ["text"]},
                   annotations=types.ToolAnnotations(read_only_hint=True)),
    ])  # fmt: skip


async def call_tool(ctx, params):  # type: ignore[no-untyped-def]
    if params.name != "shout":
        raise ValueError(f"no tool {params.name}")
    text = str((params.arguments or {}).get("text", ""))
    return types.CallToolResult(content=[types.TextContent(type="text", text=text.upper())],
                                structured_content={"shouted": text.upper()})  # fmt: skip


server = Server("sdk-fake", version="2.0", on_list_tools=list_tools, on_call_tool=call_tool)


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
