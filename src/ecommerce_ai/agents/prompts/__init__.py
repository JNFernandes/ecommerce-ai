"""Versioned prompt files and their loader.

Each prompt lives in ``<name>.<version>.md`` with a ``# System`` and, for a templated prompt
(triage), a ``# User`` section (``load_prompt``; ``load_system_prompt`` for system-only ones). It is
immutable once released: a change is a new version file (constitution III, FR-013). Prompts are
never written inline in Python.
"""
