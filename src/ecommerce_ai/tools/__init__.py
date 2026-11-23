"""Tools: the MCP server and its tool definitions.

Each tool wraps exactly one endpoint documented in the owning service's README, is on the
allowlist, has a typed input schema and a timeout, and calls the platform with the caller's own
token (constitution II, ADR 0001). Tools contain no prompts, LLM calls or business decisions.
"""
