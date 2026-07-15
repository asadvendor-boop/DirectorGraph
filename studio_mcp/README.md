# DirectorGraph Studio MCP

This optional MCP server exposes deterministic production operations to Qwen-Agent or another MCP client:

- Shot Contract validation
- Render-budget estimation
- Edit-decision-list compilation
- Semantic impact analysis for partial rerendering

Run from the repository root:

```bash
python -m pip install -e studio_mcp
python studio_mcp/server.py
```

Use `qwen-agent-mcp.json` as the MCP client configuration. The core web application does not require this optional process; it is included to demonstrate clean separation between reasoning agents and deterministic production skills.
