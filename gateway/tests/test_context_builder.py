"""
Unit tests for the bounded Context Builder.
Verifies turn grouping, boundary adherence, character limits, and context status classification.
"""

import unittest

from app.context.builder import ContextBuilder
from app.context.contracts import ContextStatus
from app.context.policy import ContextPolicy
from app.core.contracts import ChatMessage


class TestContextBuilder(unittest.TestCase):
    """Test suite for ContextBuilder context reconstruction."""

    def setUp(self):
        self.default_policy = ContextPolicy(
            max_context_messages=50,
            max_context_chars=65536,
        )
        self.builder = ContextBuilder(self.default_policy)

    def test_single_turn_context_construction(self):
        """Constructs context for a fresh query with no history."""
        result = self.builder.build_context(
            current_query="What is SingleFlight?",
            history=[],
            system_prompt="You are a helpful caching assistant.",
        )

        self.assertEqual(result.status, ContextStatus.COMPLETE)
        self.assertEqual(len(result.messages), 2)
        self.assertEqual(result.messages[0].role, "system")
        self.assertEqual(result.messages[0].content, "You are a helpful caching assistant.")
        self.assertEqual(result.messages[1].role, "user")
        self.assertEqual(result.messages[1].content, "What is SingleFlight?")
        self.assertEqual(result.omitted_messages_count, 0)

    def test_complete_history_fits_within_budget(self):
        """Chronological multi-turn history preserved when budget is sufficient."""
        history = [
            ChatMessage(role="user", content="Turn 1 User"),
            ChatMessage(role="assistant", content="Turn 1 Assistant"),
            ChatMessage(role="user", content="Turn 2 User"),
            ChatMessage(role="assistant", content="Turn 2 Assistant"),
        ]

        result = self.builder.build_context(
            current_query="Turn 3 User",
            history=history,
        )

        self.assertEqual(result.status, ContextStatus.COMPLETE)
        self.assertEqual(len(result.messages), 5)
        self.assertEqual([m.content for m in result.messages], [
            "Turn 1 User",
            "Turn 1 Assistant",
            "Turn 2 User",
            "Turn 2 Assistant",
            "Turn 3 User",
        ])
        self.assertEqual(result.omitted_messages_count, 0)

    def test_compacted_context_when_message_limit_exceeded(self):
        """Omitted older turns when message count exceeds policy limit."""
        # Policy allows max 4 messages total: 1 current query + at most 1 turn (2 messages) + 1 spare
        policy = ContextPolicy(max_context_messages=4, max_context_chars=10000)
        builder = ContextBuilder(policy)

        history = [
            ChatMessage(role="user", content="Old 1"),
            ChatMessage(role="assistant", content="Old 1 Resp"),
            ChatMessage(role="user", content="Old 2"),
            ChatMessage(role="assistant", content="Old 2 Resp"),
            ChatMessage(role="user", content="Recent"),
            ChatMessage(role="assistant", content="Recent Resp"),
        ]

        result = builder.build_context(
            current_query="Latest Query",
            history=history,
        )

        self.assertEqual(result.status, ContextStatus.COMPACTED)
        # Should include [Recent, Recent Resp, Latest Query] (3 messages <= 4)
        # Old 1, Old 1 Resp, Old 2, Old 2 Resp are omitted (4 messages)
        self.assertEqual(len(result.messages), 3)
        self.assertEqual([m.content for m in result.messages], [
            "Recent",
            "Recent Resp",
            "Latest Query",
        ])
        self.assertEqual(result.omitted_messages_count, 4)
        self.assertIn("Omitted 4 older message(s)", result.reason)

    def test_compacted_context_when_character_limit_exceeded(self):
        """Preserves turn pairs and compacts older content when character budget is constrained."""
        # 100 characters max
        policy = ContextPolicy(max_context_messages=20, max_context_chars=100)
        builder = ContextBuilder(policy)

        # 30 chars for current query
        current_query = "A" * 30

        # Turn 1: 50 chars total
        # Turn 2: 40 chars total
        # Base: 30 chars. Remaining: 70 chars.
        # Turn 2 (40 chars) fits (70 - 40 = 30 rem). Turn 1 (50 chars) does not fit (50 > 30).
        history = [
            ChatMessage(role="user", content="1" * 25),
            ChatMessage(role="assistant", content="1" * 25),
            ChatMessage(role="user", content="2" * 20),
            ChatMessage(role="assistant", content="2" * 20),
        ]

        result = builder.build_context(
            current_query=current_query,
            history=history,
        )

        self.assertEqual(result.status, ContextStatus.COMPACTED)
        self.assertEqual(len(result.messages), 3)
        self.assertEqual(result.messages[0].content, "2" * 20)
        self.assertEqual(result.messages[1].content, "2" * 20)
        self.assertEqual(result.messages[2].content, current_query)
        self.assertEqual(result.omitted_messages_count, 2)

    def test_turn_boundary_integrity_not_splitting_pairs(self):
        """Never includes half of a user-assistant turn pair."""
        # If budget can fit the assistant message but not the preceding user message,
        # neither should be included.
        policy = ContextPolicy(max_context_messages=10, max_context_chars=80)
        builder = ContextBuilder(policy)

        current_query = "U_new" * 4  # 20 chars
        # Remaining chars: 60
        # Turn 1: User (40 chars) + Assistant (30 chars) = 70 chars (cannot fit both!)
        history = [
            ChatMessage(role="user", content="U" * 40),
            ChatMessage(role="assistant", content="A" * 30),
        ]

        result = builder.build_context(
            current_query=current_query,
            history=history,
        )

        self.assertEqual(result.status, ContextStatus.COMPACTED)
        # Should only contain current query; Turn 1 should not be partially included!
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].content, current_query)
        self.assertEqual(result.omitted_messages_count, 2)

    def test_error_when_current_query_exceeds_character_budget(self):
        """Raises ValueError without silently truncating the current query."""
        policy = ContextPolicy(max_context_messages=10, max_context_chars=50)
        builder = ContextBuilder(policy)

        with self.assertRaises(ValueError) as ctx:
            builder.build_context(
                current_query="X" * 60,
                history=[],
            )

        self.assertIn("exceed maximum context character budget", str(ctx.exception))

    def test_incomplete_status_when_flagged(self):
        """Marks status as incomplete when history cannot be verified."""
        result = self.builder.build_context(
            current_query="Verify status",
            history=[ChatMessage(role="user", content="Q1"), ChatMessage(role="assistant", content="A1")],
            is_history_incomplete=True,
        )

        self.assertEqual(result.status, ContextStatus.INCOMPLETE)
        self.assertIn("could not be reliably verified", result.reason)


if __name__ == "__main__":
    unittest.main()
