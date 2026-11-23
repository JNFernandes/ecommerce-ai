"""HTTP layer: FastAPI routers, middleware and request/response schemas.

Routers handle I/O only (authentication, validation, SSE framing) and call exactly one agent entry
point. They never contain prompts, graph logic or calls to other services (constitution I).
"""
