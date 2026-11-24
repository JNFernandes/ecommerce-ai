"""Triage: explain each settled triage case with a guarded LLM draft or a fallback template.

Holds the triage graph, its domain models, the deterministic gatekeeper, the fallback templates,
the worker loop and the ports it needs (model, token budget, case queue, clock). It never imports
messaging, HTTP, Kafka or SQL code (constitution I).
"""
