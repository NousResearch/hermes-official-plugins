## n8n plugin installed

Connect it to your n8n instance:

```bash
hermes n8n setup
hermes mcp login n8n-official
```

`setup` asks for the MCP Server URL from **n8n Settings > Instance-level MCP > Connect**
and adds it as the `n8n-official` MCP server. `hermes mcp login` opens n8n's sign-in in
your browser. Then start a new session (or run `/reload-mcp`).
