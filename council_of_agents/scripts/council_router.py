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
    # One bounded recovery model for provider outages and unrepaired invalid
    # structured output. Must use a DIFFERENT provider than the role primary;
    # validated at startup against the shared recovery resolver (see
    # council_recovery). Separate from context_fallbacks on purpose.
    recovery_fallbacks: list[dict] = Field(default_factory=list)

class EscalationConfig(BaseModel):
    max_loops: int = 3
    conflict_threshold: float = 0.7

class CouncilConfig(BaseModel):
    roles: Dict[str, ModelConfig]
    escalation: EscalationConfig = EscalationConfig()

_PARENT_ROLES = {
    "perspective_analyzer": "manager",
    "completeness_auditor": "manager",
    "debate_response": "strategist",
    "chair_arbitration": "chair",
}

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
            self._validate_recovery()

    def _validate_recovery(self):
        """Fail startup on an invalid recovery routing instead of failing a live run."""
        try:
            from council_of_agents.scripts.council_recovery import validate_recovery_config
        except Exception:
            return
        errors = validate_recovery_config(self._path, required_roles=("chair", "strategist", "perspective_analyzer", "manager", "implementer", "completeness_auditor"))
        if errors:
            raise ValueError("invalid council recovery configuration:\n" + "\n".join(errors))

    def get(self) -> CouncilConfig:
        self._reload()
        return self._config

    def effective_overrides(self, role: str, full_overrides: Optional[dict] = None) -> dict:
        """Resolve effective role overrides, inheriting from companion/parent role if omitted."""
        if not full_overrides or not isinstance(full_overrides, dict):
            return {}
        if "model" in full_overrides or "endpoint_url" in full_overrides:
            return full_overrides
        own = full_overrides.get(role)
        if isinstance(own, dict) and own:
            return own
        parent_role = _PARENT_ROLES.get(role)
        if parent_role:
            parent_override = full_overrides.get(parent_role)
            if isinstance(parent_override, dict) and parent_override:
                return parent_override
        return {}

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
