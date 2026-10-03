import json
from pathlib import Path


def test_council_roles_use_configured_provider_defaults():
    path = Path(__file__).resolve().parents[1] / "council_of_agents/config/models.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    roles = config["roles"]
    nilovr = "https://api.nilovr.com/v1/chat/completions"

    expected = {
        "chair": (nilovr, "hy3"),
        "strategist": (nilovr, "hy3"),
        "perspective_analyzer": (nilovr, "hy3"),
        "manager": (nilovr, "hy3"),
        "implementer": (nilovr, "hy3"),
        "completeness_auditor": (nilovr, "hy3"),
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
        assert config["roles"][role]["endpoint_url"].startswith("https://api.nilovr.com/")


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

