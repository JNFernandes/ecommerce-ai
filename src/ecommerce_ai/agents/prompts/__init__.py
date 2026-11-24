"""Versioned prompt files and their loader.

Each prompt lives in ``<name>.<version>.md`` with a ``# System`` and a ``# User`` section and is
immutable once released: a change is a new version file (constitution III, FR-013). Prompts are
never written inline in Python.
"""
