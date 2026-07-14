"""Doom loop detection for council orchestrator.
G2/G8: Supports debate mode to allow intentional debate rounds.
"""
import hashlib
import logging
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DoomLoopDetector:
    _output_history: dict = field(default_factory=dict)
    _output_max: int = 5
    _error_patterns: Counter = field(default_factory=Counter)
    _error_threshold: int = 3
    _revision_count: int = 0
    _revision_max: int = 5
    _consecutive_identical: dict = field(default_factory=dict)
    _stuck_threshold: int = 2
    _mode: str = "pipeline"  # "pipeline" or "debate" (G2/G8)

    def set_mode(self, mode):
        self._mode = mode

    def _hash(self, text):
        return hashlib.sha256(text.strip().encode()).hexdigest()[:16]

    def check_output_loop(self, agent, output):
        if not output or not output.strip():
            return None
        h = self._hash(output)
        if agent not in self._output_history:
            self._output_history[agent] = deque(maxlen=self._output_max)
        hist = self._output_history[agent]

        # G8: In debate mode, allow more repeats
        threshold = self._stuck_threshold + 3 if self._mode == "debate" else self._stuck_threshold

        if hist and hist[-1] == h:
            self._consecutive_identical[agent] = self._consecutive_identical.get(agent, 0) + 1
            if self._consecutive_identical[agent] >= threshold:
                return f"Agent '{agent}' produced identical output {self._consecutive_identical[agent]+1} times"
        else:
            self._consecutive_identical[agent] = 0
        hist.append(h)
        counts = Counter(hist)
        mc, c = counts.most_common(1)[0]
        if c >= 3 and len(hist) >= 3:
            return f"Agent '{agent}' output cycling: {c} times in {len(hist)} attempts"
        return None

    def check_error_loop(self, task_id, error):
        if not error:
            return None
        sig = f"{task_id}:{error.strip().lower()[:100]}"
        self._error_patterns[sig] += 1
        if self._error_patterns[sig] >= self._error_threshold:
            return f"Task '{task_id}' same error {self._error_patterns[sig]} times: {error[:80]}"
        return None

    def check_revision_loop(self):
        self._revision_count += 1
        limit = self._revision_max + 3 if self._mode == "debate" else self._revision_max
        if self._revision_count >= limit:
            return f"Revision loop exceeded {limit} iterations"
        return None

    def reset_revision_count(self):
        self._revision_count = 0

    def get_loop_break_message(self, reason):
        return (f"LOOP DETECTED: {reason}\n\n"
                "STOP. Accept best result so far, OR state what blocks progress, OR report FAILED.\n"
                "Do NOT retry the same approach.")
