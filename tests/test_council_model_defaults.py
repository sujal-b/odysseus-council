import json
from pathlib import Path


def test_council_roles_use_configured_provider_defaults():
    path = Path(__file__).resolve().parents[1] / "council_of_agents/config/models.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    roles = config["roles"]
    zen = "https://opencode.ai/zen/v1/chat/completions"

    expected = {
        "chair": (zen, "mimo-v2.6-flash-free"),
        "strategist": (zen, "mimo-v2.6-flash-free"),
        "perspective_analyzer": (zen, "mimo-v2.6-flash-free"),
        "manager": (zen, "mimo-v2.6-flash-free"),
        "implementer": (zen, "mimo-v2.6-flash-free"),
        "completeness_auditor": (zen, "mimo-v2.6-flash-free"),
    }
    for role, (endpoint, model) in expected.items():
        assert roles[role]["endpoint_url"] == endpoint
        assert roles[role]["model"] == model


def test_auxiliary_council_roles_stay_on_zen_defaults():
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "council_of_agents/config/models.json")
        .read_text(encoding="utf-8")
    )
    for role in ("debate_response", "chair_arbitration"):
        assert config["roles"][role]["endpoint_url"].startswith("https://opencode.ai/zen/")


def test_effective_overrides_inheritance():
    from council_of_agents.scripts.council_router import CouncilRouter
    router = CouncilRouter(str(Path(__file__).resolve().parents[1] / "council_of_agents/config/models.json"))

    overrides = {
        "manager": {"model": "custom-mgr", "endpoint_url": "https://custom.api/v1"},
        "chair": {"model": "custom-chair"},
        "strategist": {"model": "custom-strat"},
    }
    assert router.effective_overrides("perspective_analyzer", overrides) == overrides["manager"]
    assert router.effective_overrides("completeness_auditor", overrides) == overrides["manager"]
    assert router.effective_overrides("debate_response", overrides) == overrides["strategist"]
    assert router.effective_overrides("chair_arbitration", overrides) == overrides["chair"]

    overrides_with_explicit = dict(overrides)
    overrides_with_explicit["perspective_analyzer"] = {"model": "custom-persp"}
    assert router.effective_overrides("perspective_analyzer", overrides_with_explicit) == {"model": "custom-persp"}

    assert router.effective_overrides("perspective_analyzer", {"model": "direct-model"}) == {"model": "direct-model"}
    assert router.effective_overrides("perspective_analyzer", {}) == {}
    assert router.effective_overrides("perspective_analyzer", None) == {}

