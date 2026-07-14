"""Retry with AWS-style exponential backoff and full jitter."""
import asyncio
import logging
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional
from council_of_agents.scripts.context_tracker import ContextBudgetExceededError

logger = logging.getLogger(__name__)

class ErrorClass(str, Enum):
    TRANSIENT = "transient"
    THROTTLING = "throttling"
    SCHEMA = "schema"
    TERMINAL = "terminal"

TRANSIENT_EXCEPTIONS = (asyncio.TimeoutError, ConnectionError, ConnectionResetError, OSError)
TRANSIENT_KW = ["timeout", "timed out", "connection reset", "502", "503", "504"]
THROTTLING_KW = ["rate limit", "throttl", "429", "too many requests"]
TERMINAL_KW = ["unauthorized", "forbidden", "401", "403", "invalid api key", "404"]

class SchemaValidationError(Exception):
    def __init__(self, msg, raw_text="", validation_error=""):
        super().__init__(msg)
        self.raw_text = raw_text
        self.validation_error = validation_error

def classify_error(error):
    if isinstance(error, ContextBudgetExceededError):
        return ErrorClass.TERMINAL
    if isinstance(error, SchemaValidationError):
        return ErrorClass.SCHEMA
    if isinstance(error, TRANSIENT_EXCEPTIONS):
        return ErrorClass.TRANSIENT
    s = str(error).lower()
    for kw in TERMINAL_KW:
        if kw in s:
            return ErrorClass.TERMINAL
    for kw in THROTTLING_KW:
        if kw in s:
            return ErrorClass.THROTTLING
    for kw in TRANSIENT_KW:
        if kw in s:
            return ErrorClass.TRANSIENT
    return ErrorClass.TRANSIENT

_BASE = {"transient": 50, "throttling": 1000, "schema": 200, "terminal": 0}
_MAX_RETRIES = {"transient": 3, "throttling": 2, "schema": 2, "terminal": 0}
CAP_MS = 20000

def compute_backoff(error_class, attempt):
    base = _BASE.get(error_class.value, 50)
    if base == 0:
        return 0.0
    return random.random() * min(CAP_MS, base * 2 ** attempt) / 1000

@dataclass
class RetryState:
    attempt: int = 0
    last_error: Optional[Exception] = None
    last_class: Optional[ErrorClass] = None
    total_delay_ms: float = 0
    errors: list = field(default_factory=list)

    def record(self, error, cls, delay):
        self.attempt += 1
        self.last_error = error
        self.last_class = cls
        self.total_delay_ms += delay * 1000
        self.errors.append({"attempt": self.attempt, "class": cls.value,
                            "error": str(error)[:200], "delay_s": delay})

async def retry_with_backoff(operation, role, max_retries=None, on_retry=None):
    state = RetryState()
    while True:
        try:
            result = await operation()
            return result, state
        except Exception as error:
            cls = classify_error(error)
            limit = (
                0 if cls == ErrorClass.TERMINAL
                else max_retries if max_retries is not None
                else _MAX_RETRIES.get(cls.value, 1)
            )
            if state.attempt >= limit:
                raise
            delay = compute_backoff(cls, state.attempt)
            state.record(error, cls, delay)
            logger.info("[%s] retry %d/%d after %.2fs (%s): %s",
                        role, state.attempt, limit, delay, cls.value, str(error)[:100])
            if on_retry:
                await on_retry(state)
            await asyncio.sleep(delay)
