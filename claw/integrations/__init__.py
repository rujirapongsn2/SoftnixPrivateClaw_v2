"""Built-in MCP servers for connector presets (ported from softnix-agenticclaw).

Each module is a self-contained FastMCP stdio server launched via
``python -m claw.integrations.<name>_mcp_server`` by a connector preset.
Configuration is read from environment variables supplied by the connector.
"""
