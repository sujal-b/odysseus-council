import json, os
from typing import Dict, Optional
from pydantic import BaseModel, Field

class ModelConfig(BaseModel):
    endpoint_url: str
    model: str
    temperature: float = 1.0
    max_tokens: int = 4096
    # One bounded recovery model used only when the primary model rejects the
    # request for context size.  It is deliberately separate from ordinary
    # provider retries so a context failure is not replayed against the same
    # window.
    context_fallbacks: list[dict] = Field(default_factory=list)

class EscalationConfig(BaseModel):
    max_loops: int = 3
    conflict_threshold: float = 0.7
    verifier_enabled: bool = True
    max_retries_per_task: int = 2

class CouncilConfig(BaseModel):
    roles: Dict[str, ModelConfig]
    escalation: EscalationConfig = EscalationConfig()

class CouncilRouter:
    def __init__(self, config_path: str):
        self._path = config_path
        self._mtime = 0.0
        self._config: Optional[CouncilConfig] = None
        self._reload()

    def _reload(self):
        mtime = os.path.getmtime(self._path)
        if mtime != self._mtime:
            with open(self._path, encoding="utf-8") as f:
                self._config = CouncilConfig(**json.load(f))
            self._mtime = mtime

    def get(self) -> CouncilConfig:
        self._reload()
        return self._config

    def role_config(self, role: str, overrides: dict = None) -> ModelConfig:
        roles = self.get().roles
        if role not in roles:
            # Graceful fallback mapping for debate/arbitration roles
            if role == "debate_response":
                fallback = "strategist"
            elif role == "perspective_analyzer":
                fallback = "manager"
            elif role == "chair_arbitration":
                fallback = "chair"
            else:
                fallback = "chair"
            base = roles[fallback].model_copy()
        else:
            base = roles[role].model_copy()

        for k, v in (overrides or {}).items():
            if hasattr(base, k) and v:
                setattr(base, k, v)
        return base
