"""Atomic, attributed context reservations for one Council run.

The tracker is intentionally small. It does not perform compaction itself; it
protects the model window while the orchestrator/context broker decides what
to keep. Reservations are pessimistic and are reconciled after the call.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)
_WARN_PCT = 0.80


class ContextBudgetExceededError(RuntimeError):
    """Raised when a bounded LLM attempt cannot be safely reserved."""

    def __init__(self, message: str, *, metadata: Optional[dict] = None):
        super().__init__(message)
        self.metadata = dict(metadata or {})


@dataclass
class AgentUsageRecord:
    role: str
    input_tokens: int
    output_tokens: int
    tool_type: str = "llm"
    timestamp: float = field(default_factory=time.time)


@dataclass
class ContextReservation:
    reservation_id: str
    role: str
    tokens: int
    input_tokens: int
    output_tokens: int
    tool_type: str
    priority: str
    timestamp: float = field(default_factory=time.time)


class ContextTracker:
    """Thread-safe shared ceiling with optional role/tool quotas.

    Existing callers can keep using ``reserve(role, input_tokens=...,
    output_tokens=...)``. New callers may supply ``tool_type`` and ``priority``
    to get attributed accounting and quota enforcement.
    """

    PRIORITY_RANK = {
        "queued": 1,
        "new": 1,
        "agent": 2,
        "in_progress": 2,
        "recovery": 3,
        "active_recovery": 3,
        "verification": 4,
        "safety": 4,
    }

    def __init__(
        self,
        session_id: str,
        budget_tokens: int = 0,
        protected_reserve_tokens: int = 0,
        *,
        agent_quotas: Optional[Dict[str, int]] = None,
        tool_quotas: Optional[Dict[str, int]] = None,
    ) -> None:
        self.session_id = session_id
        self.budget_tokens = max(0, int(budget_tokens or 0))
        self.protected_reserve_tokens = (
            max(0, min(self.budget_tokens, int(protected_reserve_tokens or 0)))
            if self.budget_tokens else 0
        )
        self.agent_quotas = {
            str(k): max(0, int(v)) for k, v in (agent_quotas or {}).items() if int(v) > 0
        }
        self.tool_quotas = {
            str(k): max(0, int(v)) for k, v in (tool_quotas or {}).items() if int(v) > 0
        }
        self.compact_count = 0
        self._records: List[AgentUsageRecord] = []
        self._reservations: Dict[str, ContextReservation] = {}
        self._tool_usage: Dict[str, Dict] = {}
        self._lock = threading.RLock()
        self._warned = False

    def record(
        self,
        role: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        *,
        tool_type: str = "llm",
    ) -> None:
        if not role:
            return
        with self._lock:
            self._records.append(AgentUsageRecord(
                role=str(role),
                input_tokens=max(0, int(input_tokens or 0)),
                output_tokens=max(0, int(output_tokens or 0)),
                tool_type=str(tool_type or "llm"),
            ))
            self._maybe_warn_locked()

    def increment_compact_count(self) -> None:
        with self._lock:
            self.compact_count += 1

    def record_tool(
        self,
        role: str,
        tool_type: str,
        *,
        output_tokens: int = 0,
        output_chars: int = 0,
        truncated: bool = False,
    ) -> None:
        """Record tool-output pressure without double-counting LLM input.

        LLM call tokens and tool-output volume are separate ledgers: the former
        enforces the shared model ceiling, while the latter explains which tool
        consumed context and whether its result was capped.
        """
        key = str(tool_type or "unknown")
        with self._lock:
            entry = self._tool_usage.setdefault(key, {
                "calls": 0, "output_tokens": 0, "output_chars": 0,
                "truncated_calls": 0, "by_role": {},
            })
            entry["calls"] += 1
            entry["output_tokens"] += max(0, int(output_tokens or 0))
            entry["output_chars"] += max(0, int(output_chars or 0))
            entry["truncated_calls"] += 1 if truncated else 0
            role_entry = entry["by_role"].setdefault(str(role or "unknown"), {"calls": 0, "output_tokens": 0})
            role_entry["calls"] += 1
            role_entry["output_tokens"] += max(0, int(output_tokens or 0))

    def reserve(
        self,
        role: str,
        *,
        input_tokens: int,
        output_tokens: int,
        allow_protected: bool = False,
        tool_type: str = "llm",
        priority: str = "agent",
    ) -> str | None:
        role = str(role or "unknown")
        tool_type = str(tool_type or "llm")
        requested_input = max(0, int(input_tokens or 0))
        requested_output = max(0, int(output_tokens or 0))
        requested = requested_input + requested_output
        with self._lock:
            if self.budget_tokens > 0:
                protected = 0 if allow_protected else self.protected_reserve_tokens
                usable_limit = max(0, self.budget_tokens - protected)
                projected = self.total_tokens + self.reserved_tokens + requested
                if projected > usable_limit:
                    return None
            if self._quota_used_locked(role=role, tool_type=tool_type) + requested > self.agent_quotas.get(role, 0) > 0:
                return None
            if self._tool_quota_used_locked(tool_type) + requested > self.tool_quotas.get(tool_type, 0) > 0:
                return None
            reservation_id = f"reservation-{uuid4().hex}"
            self._reservations[reservation_id] = ContextReservation(
                reservation_id=reservation_id,
                role=role,
                tokens=requested,
                input_tokens=requested_input,
                output_tokens=requested_output,
                tool_type=tool_type,
                priority=priority,
            )
            self._maybe_warn_locked()
            return reservation_id

    def release(self, reservation_id: str | None) -> None:
        if reservation_id:
            with self._lock:
                self._reservations.pop(reservation_id, None)

    def reconcile(
        self,
        reservation_id: str | None,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """Release pessimistic capacity and record actual usage atomically."""
        if not reservation_id:
            return
        with self._lock:
            reservation = self._reservations.pop(reservation_id, None)
            if reservation is None:
                return
            self._records.append(AgentUsageRecord(
                role=reservation.role,
                input_tokens=max(0, int(input_tokens or 0)),
                output_tokens=max(0, int(output_tokens or 0)),
                tool_type=reservation.tool_type,
            ))
            self._maybe_warn_locked()

    @property
    def total_input_tokens(self) -> int:
        with self._lock:
            return sum(r.input_tokens for r in self._records)

    @property
    def total_output_tokens(self) -> int:
        with self._lock:
            return sum(r.output_tokens for r in self._records)

    @property
    def total_tokens(self) -> int:
        with self._lock:
            return sum(r.input_tokens + r.output_tokens for r in self._records)

    @property
    def reserved_tokens(self) -> int:
        with self._lock:
            return sum(r.tokens for r in self._reservations.values())

    def over_budget(self) -> bool:
        with self._lock:
            return self.budget_tokens > 0 and self.total_tokens >= self.budget_tokens

    def budget_remaining(self) -> int:
        with self._lock:
            if self.budget_tokens <= 0:
                return -1
            return max(0, self.budget_tokens - self.total_tokens - self.reserved_tokens)

    def available_for(self, *, allow_protected: bool = False) -> int:
        with self._lock:
            if self.budget_tokens <= 0:
                return -1
            protected = 0 if allow_protected else self.protected_reserve_tokens
            return max(0, self.budget_tokens - protected - self.total_tokens - self.reserved_tokens)

    def get_usage_summary(self) -> Dict:
        with self._lock:
            by_role: Dict[str, Dict] = {}
            by_tool: Dict[str, Dict] = {}
            for rec in self._records:
                role_entry = by_role.setdefault(rec.role, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
                role_entry["input_tokens"] += rec.input_tokens
                role_entry["output_tokens"] += rec.output_tokens
                role_entry["calls"] += 1
                tool_entry = by_tool.setdefault(rec.tool_type, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
                tool_entry["input_tokens"] += rec.input_tokens
                tool_entry["output_tokens"] += rec.output_tokens
                tool_entry["calls"] += 1
            return {
                "session_id": self.session_id,
                "budget_tokens": self.budget_tokens,
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "total_tokens": self.total_tokens,
                "budget_remaining": self.budget_remaining(),
                "over_budget": self.over_budget(),
                "reserved_tokens": self.reserved_tokens,
                "protected_reserve_tokens": self.protected_reserve_tokens,
                "compact_count": self.compact_count,
                "by_role": by_role,
                "by_tool": by_tool,
                "tool_usage": self._tool_usage,
                "active_reservations": len(self._reservations),
            }

    def _quota_used_locked(self, *, role: str, tool_type: str) -> int:
        return sum(
            r.input_tokens + r.output_tokens
            for r in self._records
            if r.role == role
        ) + sum(r.tokens for r in self._reservations.values() if r.role == role)

    def _tool_quota_used_locked(self, tool_type: str) -> int:
        return sum(
            r.input_tokens + r.output_tokens
            for r in self._records
            if r.tool_type == tool_type
        ) + sum(r.tokens for r in self._reservations.values() if r.tool_type == tool_type)

    def _maybe_warn_locked(self) -> None:
        if self._warned or self.budget_tokens <= 0:
            return
        used = self.total_tokens + self.reserved_tokens
        if used / self.budget_tokens >= _WARN_PCT:
            self._warned = True
            logger.warning(
                "[context-tracker] session=%s has used %.0f%% (%d/%d tokens)",
                self.session_id, used / self.budget_tokens * 100, used, self.budget_tokens,
            )
