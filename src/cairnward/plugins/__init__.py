"""The plugin contract (ADR-0017): a plugin is an MCP server that gets no capability a
protection depends on. It asks a core service, the service enforces the protection, and the
sandbox makes sure it can't go around the services."""
