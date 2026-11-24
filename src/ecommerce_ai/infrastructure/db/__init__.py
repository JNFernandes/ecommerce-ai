"""PostgreSQL access: async engine, table definitions, migrations and repositories.

Nothing here connects at import or at application startup; the first query opens the first
connection (constitution IV). Repositories implement the ports declared by ``agents``.
"""
