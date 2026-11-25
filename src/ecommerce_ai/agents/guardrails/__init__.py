"""Guardrails: deterministic, model-independent guards around the chat assistant (US-07).

Pure decisions only (no I/O): input normalisation and rules, tool-result sanitising, the tool
guard, canary and leak checks on answers, per-user quotas and cooldown. ``events.py`` holds the
audit event a guard emits and the ports (event store, user usage, rate window) the guards reach
the outside world through; ``infrastructure`` implements them.
"""
