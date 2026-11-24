"""Triage intake: turn each consumed failure event into one triage case per order.

Holds the domain decisions of the intake (cause derivation and precedence) and the ports it
needs to persist cases; it knows nothing about Kafka or SQL (constitution I).
"""
