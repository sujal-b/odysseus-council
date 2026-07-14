"""
context_tracker.py

Lightweight per-council-session context budget tracker.

ContextTracker is instantiated once at the start of CouncilOrchestrator.run()
and passed (optionally) to every _invoke_agent_safe call so each agent leg
can record how many tokens it consumed.  The tracker has NO database or network
I/O — it is a plain in-memory object that lives for the lifetime of a single
council run.

Design constraints:
- Zero external dependencies (besides estimate_tokens from src.model_context).
- Thread-safe enough for async/await usage in a single event loop (no locking
  needed; Python's GIL protects simple attribute writes from the one coroutine
  that ever touches this object).
- Never raises — all public methods silently clamp or skip bad inputs so
  callers can fire-and-forget without try/except wrappers.
"""

from __future__ import annotations

import logging
import time
from uuid import uuid4
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Token threshold at which we warn that the session is approaching its budget.
_WARN_PCT = 0.80  # warn at 80% of budget used


class ContextBudgetExceededError(RuntimeError):
    """Raised before an LLM attempt when its reservation cannot fit."""


@dataclass
class AgentUsageRecord:
    """A single completed agent leg's token usage."""
    role: str
    input_tokens: int
    output_tokens: int
    timestamp: float = field(default_factory=time.time)


class ContextTracker:
    """Tracks token consumption across all agent legs in one council run.

    Usage::

        tracker = ContextTracker(session_id="abc", budget_tokens=50_000)
        tracker.record("chair",       input_tokens=800,  output_tokens=120)
        tracker.record("strategist",  input_tokens=2400, output_tokens=800)
        tracker.record("implementer", input_tokens=8000, output_tokens=3000)
        print(tracker.get_usage_summary())
        if tracker.over_budget():
            ...

    The ``compact_count`` field is incremented by the progressive tool-output
    replacement logic in agent_loop.py (via :meth:`increment_compact_count`)
    so the session-level stats remain accurate without the tracker needing to
    know about agent_loop internals.
    """

    def __init__(
        self,
        session_id: str,
        budget_tokens: int = 0,
        protected_reserve_tokens: int = 0,
    ) -> None:
        self.session_id = session_id
        # Hard budget: 0 means "unlimited" (no enforcement, just tracking).
        self.budget_tokens = max(0, int(budget_tokens or 0))
        self.protected_reserve_tokens = max(
            0, min(self.budget_tokens, int(protected_reserve_tokens or 0))
        ) if self.budget_tokens else 0
        self.compact_count: int = 0
        self._records: List[AgentUsageRecord] = []
        self._reservations: Dict[str, int] = {}
        self._warned = False

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(
        self,
        role: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """Record one agent leg's token consumption.

        Silently skips non-positive values rather than raising so callers
        don't need defensive coding around estimate_tokens failures.
        """
        input_tokens = max(0, int(input_tokens or 0))
        output_tokens = max(0, int(output_tokens or 0))
        if not role:
            return
        rec = AgentUsageRecord(
            role=role,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        self._records.append(rec)
        self._maybe_warn()

    def increment_compact_count(self) -> None:
        """Called by agent_loop's progressive replacement logic when it compacts messages."""
        self.compact_count += 1
        logger.debug(
            "[context-tracker] session=%s compact_count now %d",
            self.session_id,
            self.compact_count,
        )

    def reserve(
        self,
        role: str,
        *,
        input_tokens: int,
        output_tokens: int,
        allow_protected: bool = False,
    ) -> str | None:
        """Atomically reserve capacity for one real LLM attempt."""
        requested = max(0, int(input_tokens or 0)) + max(0, int(output_tokens or 0))
        if self.budget_tokens > 0:
            protected = 0 if allow_protected else self.protected_reserve_tokens
            usable_limit = max(0, self.budget_tokens - protected)
            projected = self.total_tokens + self.reserved_tokens + requested
            if projected > usable_limit:
                return None
        reservation_id = f"reservation-{uuid4().hex}"
        self._reservations[reservation_id] = requested
        self._maybe_warn()
        return reservation_id

    def release(self, reservation_id: str | None) -> None:
        if reservation_id:
            self._reservations.pop(reservation_id, None)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self._records)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self._records)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def reserved_tokens(self) -> int:
        return sum(self._reservations.values())

    def over_budget(self) -> bool:
        """Return True if the hard budget (input + output tokens combined) has been exceeded.

        Always False when budget_tokens == 0 (unlimited).
        """
        if self.budget_tokens <= 0:
            return False
        return self.total_tokens >= self.budget_tokens

    def budget_remaining(self) -> int:
        """Remaining token budget (total = input + output); -1 if unlimited."""
        if self.budget_tokens <= 0:
            return -1
        return max(0, self.budget_tokens - self.total_tokens - self.reserved_tokens)

    def available_for(self, *, allow_protected: bool = False) -> int:
        if self.budget_tokens <= 0:
            return -1
        protected = 0 if allow_protected else self.protected_reserve_tokens
        return max(
            0,
            self.budget_tokens - protected - self.total_tokens - self.reserved_tokens,
        )

    def get_usage_summary(self) -> Dict:
        """Return a serialisable summary dict for logging / session persistence."""
        by_role: Dict[str, Dict] = {}
        for rec in self._records:
            if rec.role not in by_role:
                by_role[rec.role] = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
            by_role[rec.role]["input_tokens"]  += rec.input_tokens
            by_role[rec.role]["output_tokens"] += rec.output_tokens
            by_role[rec.role]["calls"]         += 1

        return {
            "session_id":          self.session_id,
            "budget_tokens":       self.budget_tokens,
            "total_input_tokens":  self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens":        self.total_tokens,
            "budget_remaining":    self.budget_remaining(),
            "over_budget":         self.over_budget(),
            "reserved_tokens":     self.reserved_tokens,
            "protected_reserve_tokens": self.protected_reserve_tokens,
            "compact_count":       self.compact_count,
            "by_role":             by_role,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_warn(self) -> None:
        """Emit a single log warning when we cross the soft 80 % threshold."""
        if self._warned or self.budget_tokens <= 0:
            return
        used_pct = (self.total_tokens + self.reserved_tokens) / self.budget_tokens
        if used_pct >= _WARN_PCT:
            self._warned = True
            logger.warning(
                "[context-tracker] session=%s has used %.0f%% of token budget "
                "(%d / %d tokens). Consider increasing budget or enabling "
                "progressive tool-output compaction.",
                self.session_id,
                used_pct * 100,
                self.total_tokens + self.reserved_tokens,
                self.budget_tokens,
            )
