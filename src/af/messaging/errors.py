"""Sanitized messaging boundary failures."""


class InvalidMessageEnvelope(ValueError):
    """A message envelope or its wire representation is invalid."""


class InvalidMessageSchema(ValueError):
    """A schema, upcaster, subscription, or local-bus contract is invalid."""


class UnsupportedMessageSchema(LookupError):
    """A message type or schema version has no eligible registered contract."""


class MessageUpcastError(RuntimeError):
    """A registered payload upcaster failed without exposing message content."""


class LocalEventListenerError(RuntimeError):
    """A local event listener failed without exposing message content."""
