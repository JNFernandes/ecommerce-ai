"""LLM clients: adapters of the ``agents`` model ports to a provider SDK (Anthropic).

No prompts and no decisions live here: an adapter sends the rendered prompt it is given, enforces
the call's timeout and output limit, and maps the provider's answer and errors to the port.
"""
