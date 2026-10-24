"""RepoPilot's sandboxed tools as an MCP server (stdio).

Any MCP client (Claude Code, an IDE, another agent) can open a local directory as a
workspace inside a hardened sandbox and then use the same policy-checked tools the RepoPilot
agent uses: read_file, search_code, replace_text, apply_patch, run_tests, git_diff, ...
Nothing runs on the host; writes go through the same ToolPolicy. Only directories under the
allowed roots can be opened (REPOPILOT_MCP_ROOTS, default: the directory the server started
in), so a prompt-injected client cannot copy, say, ~/.ssh into a sandbox and read it back.

Run: `repopilot mcp`  (e.g. `claude mcp add repopilot -- repopilot mcp`)
"""

import json
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server

from repopilot.github.workspace import SANDBOX_IMAGE_PREFIXES
from repopilot.sandbox_client import SandboxClient
from repopilot.tools.builtin import build_registry
from repopilot.tools.policy import ToolPolicy
from repopilot.tools.registry import ToolContext, ToolRegistry

OPEN_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Absolute path of a local repository to open."},
        "image": {
            "type": "string",
            "default": "repopilot-sandbox-base",
            "description": "Sandbox image (repopilot-sandbox-* or repopilot-bench-*).",
        },
        "test_command": {"type": "array", "items": {"type": "string"}, "default": ["pytest", "-q"]},
    },
    "required": ["path"],
}
CLOSE_SCHEMA = {
    "type": "object",
    "properties": {"workspace_id": {"type": "string"}},
    "required": ["workspace_id"],
}


class WorkspaceTools:
    """The MCP-independent core: workspaces are sandboxes; tools dispatch through the registry."""

    def __init__(
        self,
        sandbox: SandboxClient,
        registry: ToolRegistry | None = None,
        *,
        roots: list[Path] | None = None,
    ):
        self.sandbox = sandbox
        self.registry = registry or build_registry()
        self.workspaces: dict[str, ToolContext] = {}
        self.roots = [r.resolve() for r in (roots or [Path.cwd()])]

    def tool_schemas(self) -> list[tuple[str, str, dict]]:
        tools = [
            (
                "open_workspace",
                (
                    "Copy a local repository into a new isolated sandbox. Returns a "
                    "workspace_id for the other tools."
                ),
                OPEN_SCHEMA,
            ),
            ("close_workspace", "Destroy a workspace's sandbox.", CLOSE_SCHEMA),
        ]
        for spec in self.registry.specs():
            schema = json.loads(json.dumps(spec.parameters))
            schema.setdefault("properties", {})["workspace_id"] = {"type": "string"}
            schema["required"] = ["workspace_id", *schema.get("required", [])]
            tools.append((spec.name, spec.description, schema))
        return tools

    async def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """(text, is_error) for one tool call."""
        if name == "open_workspace":
            return await self._open(arguments)
        if name == "close_workspace":
            ctx = self.workspaces.pop(str(arguments.get("workspace_id")), None)
            if ctx is None:
                return "unknown workspace_id", True
            await self.sandbox.destroy(ctx.sandbox_id)
            return "closed", False
        args = dict(arguments)
        ctx = self.workspaces.get(str(args.pop("workspace_id", "")))
        if ctx is None:
            return "unknown workspace_id; call open_workspace first", True
        outcome = await self.registry.dispatch(ctx, name, json.dumps(args))
        return outcome.for_model(), not outcome.ok

    async def _open(self, arguments: dict) -> tuple[str, bool]:
        path = Path(str(arguments.get("path", ""))).expanduser()
        if not path.is_absolute() or not path.is_dir():
            return f"{path} is not an absolute path to a directory", True
        path = path.resolve()  # symlinks and .. resolved before the containment check
        if not any(path.is_relative_to(root) for root in self.roots):
            allowed = ", ".join(map(str, self.roots))
            return f"{path} is outside the allowed roots ({allowed}); see REPOPILOT_MCP_ROOTS", True
        image = str(arguments.get("image") or "repopilot-sandbox-base")
        if not image.startswith(SANDBOX_IMAGE_PREFIXES):
            return f"image {image!r} is not a sandbox image", True
        sandbox_id = await self.sandbox.create(image)
        try:
            await self.sandbox.upload_dir(sandbox_id, path)
        except Exception:
            await self.sandbox.destroy(sandbox_id)
            raise
        self.workspaces[sandbox_id] = ToolContext(
            sandbox=self.sandbox,
            sandbox_id=sandbox_id,
            policy=ToolPolicy(),
            test_command=list(arguments.get("test_command") or ["pytest", "-q"]),
        )
        return json.dumps({"workspace_id": sandbox_id}), False

    async def close_all(self) -> None:
        for ctx in list(self.workspaces.values()):
            await self.sandbox.destroy(ctx.sandbox_id)
        self.workspaces.clear()


def build_server(core: WorkspaceTools) -> Server:
    async def list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=name, description=description, input_schema=schema)
                for name, description, schema in core.tool_schemas()
            ]
        )

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        text, is_error = await core.call(params.name, dict(params.arguments or {}))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], is_error=is_error
        )

    return Server(
        "repopilot",
        version="0.1.0",
        instructions="Open a repository with open_workspace, then use the returned "
        "workspace_id with the other tools. Everything runs in a sandbox.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve_stdio(core: WorkspaceTools) -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(core)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await core.close_all()
