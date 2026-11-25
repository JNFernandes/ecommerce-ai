"""Mutating tools and their declarative approval policy (US-06, constitution III, ADR 0002).

A mutating tool is never served on ``/mcp`` and never runs through ``ToolInvoker.invoke``: the
chat graph pauses for the customer's approval of the exact call first. Tools here only wrap their
endpoint; the decision of when a call may be proposed lives in ``agents/assistant``.
"""
