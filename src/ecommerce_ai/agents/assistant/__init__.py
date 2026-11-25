"""Assistant: the customer chat agent (US-05).

One LangGraph graph per posted message (guard input → agent ⇄ tools → gatekeeper → finalize)
answers from the read-only tools only, releasing gatekept text segment by segment as typed
``StreamEvent`` objects; the API only frames them as SSE. ``model.py`` holds the domain types,
``ports.py`` the outside world it reaches (model, tools, conversation store, checkpointer).
"""
