"""Agents: LangGraph graphs, nodes, state models and versioned prompts.

Nodes reach the outside world only through tools or ``typing.Protocol`` ports implemented in
``infrastructure``; they never contain HTTP, Kafka or SQL code. The service's domain decisions
(triage cause, suggested action, gatekeeper verdict) live here (constitution I and III).
"""
