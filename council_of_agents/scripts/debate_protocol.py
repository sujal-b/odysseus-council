"""Structured debate protocol for Council of Agents.

Bounded multi-round debate between Strategist and Manager
with confidence scoring and forced convergence.
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

MAX_DEBATE_ROUNDS = 3
CONFIDENCE_THRESHOLD = 0.7


@dataclass
class DebateRound:
    round_num: int
    strategist_reply: str = ""
    manager_reply: str = ""
    manager_confidence: float = 0.5
    convergence_achieved: bool = False
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class DebateResult:
    rounds_used: int
    final_verdict: str           # "APPROVED", "REVISED", "ARBITRATED"
    convergence_achieved: bool
    forced_convergence: bool
    final_plan: str
    all_rounds: list = field(default_factory=list)
    total_confidence: float = 0.0


class DebateProtocol:
    def __init__(self, max_rounds=MAX_DEBATE_ROUNDS,
                 confidence_threshold=CONFIDENCE_THRESHOLD):
        self.max_rounds = max_rounds
        self.confidence_threshold = confidence_threshold
        self.rounds: list[DebateRound] = []

    def should_continue(self, round_num, confidence):
        if confidence >= self.confidence_threshold:
            return False
        if round_num >= self.max_rounds:
            return False
        return True

    def needs_arbitration(self, round_num):
        return round_num >= self.max_rounds

    def extract_confidence(self, reply):
        # Delegate parsing to the schema validator's extraction helper
        from council_of_agents.scripts.council_schemas import validate_agent_output
        if not reply:
            return 0.0
        
        # Try JSON parsing via schema
        v = validate_agent_output("manager", reply)
        if v.success and v.data:
            return max(0.0, min(1.0, float(v.data.get("confidence", 0.5))))
            
        # Fallback string matching (ensuring we avoid false positives on negations)
        upper = reply.upper().strip()
        
        # Robust check to prevent "NOT APPROVED" from triggering approval
        has_approved = "APPROVED" in upper or "ACCEPT" in upper
        has_negation = "NOT" in upper or "CANNOT" in upper or "UNABLE" in upper or "FAILED" in upper or "BLOCKED" in upper
        
        if has_approved and not has_negation:
            return 0.85
        if "BLOCKED" in upper or "REVISE" in upper:
            return 0.2
        return 0.5

    def format_history(self):
        if not self.rounds:
            return ""
        lines = ["\n\n## Debate History"]
        for r in self.rounds:
            lines.append(f"\n### Round {r.round_num}")
            if r.strategist_reply:
                # Truncate content but keep tasks DAG block if exists
                strat_text = r.strategist_reply
                if len(strat_text) > 600:
                    strat_text = strat_text[:300] + "... [truncated] ...\n"
                    m = re.search(r'(```tasks\s*\n.*?```)', r.strategist_reply, re.DOTALL)
                    if m:
                        strat_text += f"\nReconstruction of Task DAG:\n{m.group(1)}"
                lines.append(f"**Strategist**: {strat_text}")
            if r.manager_reply:
                lines.append(f"**Manager** ({r.manager_confidence:.0%}): {r.manager_reply[:300]}")
        return "\n".join(lines)

    def build_result(self, final_verdict, final_plan, last_confidence=0.5):
        last = last_confidence
        if self.rounds:
            last = self.rounds[-1].manager_confidence
        return DebateResult(
            rounds_used=len(self.rounds),
            final_verdict=final_verdict,
            convergence_achieved=last >= self.confidence_threshold,
            forced_convergence=len(self.rounds) >= self.max_rounds and last < self.confidence_threshold,
            final_plan=final_plan,
            all_rounds=[{"round": r.round_num, "confidence": r.manager_confidence,
                         "converged": r.convergence_achieved, "ts": r.timestamp}
                        for r in self.rounds],
            total_confidence=last,
        )
