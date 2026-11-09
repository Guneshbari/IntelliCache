"""
Context policy configuration controlling message count and character budgets.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextPolicy:
    """Configurable boundaries for conversation context construction."""
    max_context_messages: int = 50
    max_context_chars: int = 65536
