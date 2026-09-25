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
