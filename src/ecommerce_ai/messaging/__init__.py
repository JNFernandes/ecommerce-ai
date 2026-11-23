"""Messaging: Kafka consumers and producers, the dead-letter tables and the retry worker.

Consumers deserialize, validate and call one agent entry point; they contain no business logic
(constitution I, IV and V).
"""
