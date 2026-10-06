"""Provider-neutral contracts: the interfaces later phases implement.

Nothing in this package imports a vendor SDK, touches the network or performs
I/O. Swapping Groq for another provider means writing one adapter against
:class:`rfq_agent.contracts.llm.LLMProvider` and changing configuration.
"""
