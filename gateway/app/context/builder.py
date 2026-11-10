"""
Bounded Context Builder for IntelliCache Gateway.
Reconstructs effective LLM conversation messages under strict character and turn count limits.
"""

from typing import List, Optional

from .contracts import ContextBuildResult, ContextStatus
from .policy import ContextPolicy
from ..core.contracts import ChatMessage


class ContextBuilder:
    """
    Constructs provider-neutral effective message sequences for LLM generation.
    Enforces turn boundary preservation, chronological ordering, and context budget limits.
    """

    def __init__(self, policy: Optional[ContextPolicy] = None):
        self.policy = policy or ContextPolicy()

    def build_context(
        self,
        current_query: str,
        history: Optional[List[ChatMessage]] = None,
        system_prompt: Optional[str] = None,
        is_history_incomplete: bool = False,
    ) -> ContextBuildResult:
        """
        Reconstructs the effective context sequence for a generation request.

        1. Ensures the current user query and optional system prompt are included.
        2. Validates that current query + system prompt do not exceed the character budget.
        3. Scans eligible completed historical messages from newest to oldest in complete turn pairs.
        4. Classifies context status as complete, compacted, or incomplete.
        """
        history_messages = history or []

        # 1. Base messages required for the turn: optional system prompt + current user query
        base_messages: List[ChatMessage] = []
        base_chars = 0

        if system_prompt and system_prompt.strip():
            clean_sys = system_prompt.strip()
            base_messages.append(ChatMessage(role="system", content=clean_sys))
            base_chars += len(clean_sys)

        clean_query = (current_query or "").strip()
        if not clean_query:
            raise ValueError("Current query cannot be empty when building conversation context.")

        current_user_msg = ChatMessage(role="user", content=clean_query)
        base_chars += len(clean_query)

        # 2. Budget verification: current query and system prompt must fit within limits
        if base_chars > self.policy.max_context_chars:
            raise ValueError(
                f"Current query and system prompt ({base_chars} chars) exceed maximum context "
                f"character budget of {self.policy.max_context_chars} characters."
            )

        if len(base_messages) + 1 > self.policy.max_context_messages:
            raise ValueError(
                f"Base turn messages exceed maximum context message limit of "
                f"{self.policy.max_context_messages}."
            )

        rem_chars = self.policy.max_context_chars - base_chars
        rem_msgs = self.policy.max_context_messages - (len(base_messages) + 1)

        # 3. Group history into complete turn units to preserve boundaries
        turns: List[List[ChatMessage]] = self._group_into_turns(history_messages)

        # 4. Pack historical turns from newest to oldest
        selected_turns: List[List[ChatMessage]] = []
        omitted_turns_count = 0
        omitted_messages_count = 0

        for turn in reversed(turns):
            turn_chars = sum(len(m.content) for m in turn)
            turn_msg_count = len(turn)

            if turn_chars <= rem_chars and turn_msg_count <= rem_msgs:
                selected_turns.append(turn)
                rem_chars -= turn_chars
                rem_msgs -= turn_msg_count
            else:
                # Turn cannot fit without truncation or breaking turn boundaries.
                # Stop including older turns to preserve continuous recent context window.
                break

        # Total omitted messages count includes all older turns not selected
        total_historical_msgs = len(history_messages)
        included_historical_msgs = sum(len(t) for t in selected_turns)
        omitted_messages_count = total_historical_msgs - included_historical_msgs

        # 5. Restore chronological order of historical messages
        final_history: List[ChatMessage] = []
        for turn in reversed(selected_turns):
            final_history.extend(turn)

        # 6. Assemble complete message array: [system (if any)] + [history] + [current_user]
        effective_messages: List[ChatMessage] = []
        if base_messages:
            effective_messages.extend(base_messages)
        effective_messages.extend(final_history)
        effective_messages.append(current_user_msg)

        total_characters = sum(len(m.content) for m in effective_messages)
        total_messages = len(effective_messages)

        # 7. Classify context state
        status: ContextStatus
        reason: Optional[str] = None

        if is_history_incomplete:
            status = ContextStatus.INCOMPLETE
            reason = "Conversation history could not be reliably verified or reconstructed."
        elif omitted_messages_count > 0:
            status = ContextStatus.COMPACTED
            reason = (
                f"Omitted {omitted_messages_count} older message(s) to adhere to context boundaries "
                f"({self.policy.max_context_messages} messages, {self.policy.max_context_chars} chars)."
            )
        else:
            status = ContextStatus.COMPLETE

        return ContextBuildResult(
            messages=effective_messages,
            status=status,
            total_messages=total_messages,
            total_characters=total_characters,
            omitted_messages_count=omitted_messages_count,
            reason=reason,
        )

    def _group_into_turns(self, messages: List[ChatMessage]) -> List[List[ChatMessage]]:
        """
        Groups sequential messages into atomic turn pairs (e.g. user + assistant).
        Guarantees that a user query and its assistant response are never split.
        """
        turns: List[List[ChatMessage]] = []
        i = 0
        n = len(messages)

        while i < n:
            msg = messages[i]
            if msg.role == "user" and i + 1 < n and messages[i + 1].role == "assistant":
                # Complete user + assistant turn pair
                turns.append([msg, messages[i + 1]])
                i += 2
            else:
                # Standalone message (e.g. system message or lone turn)
                turns.append([msg])
                i += 1

        return turns
