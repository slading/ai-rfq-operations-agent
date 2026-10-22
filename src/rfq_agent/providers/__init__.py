"""Concrete LLM provider adapters.

Project 04 authorises exactly one: :mod:`rfq_agent.providers.groq`, the live
adapter behind the provider-neutral :class:`~rfq_agent.contracts.llm.LLMProvider`
port. Every other module depends on the port, never on this package.
"""

from __future__ import annotations

from rfq_agent.providers.groq import GroqProvider, HttpRequest, HttpResponse, HttpTransport

__all__ = ["GroqProvider", "HttpRequest", "HttpResponse", "HttpTransport"]
