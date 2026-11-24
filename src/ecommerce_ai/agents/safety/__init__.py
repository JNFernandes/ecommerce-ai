"""Safety: pure, deterministic output rules shared by every agent's gatekeeper (constitution III).

``rules.py`` holds the checks (promises, contact details and URLs, facts not grounded in the
evidence, internal names, HTML) and the ``Evidence`` they compare against. Each agent's gatekeeper
(triage, assistant) composes them with its own limits and messages. No I/O, no LLM calls.
"""
