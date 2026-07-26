import json
from pathlib import Path


def test_council_roles_use_configured_provider_defaults():
    path = Path(__file__).resolve().parents[1] / "council_of_agents/config/models.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    roles = config["roles"]
    zen = "https://opencode.ai/zen/v1/chat/completions"
    nvidia = "https://integrate.api.nvidia.com/v1/chat/completions"

    expected = {
        "chair": (zen, "nemotron-3-ultra-free"),
        "strategist": (nvidia, "nvidia/nemotron-3-super-120b-a12b"),
        "perspective_analyzer": (zen, "nemotron-3-ultra-free"),
        "manager": (nvidia, "nvidia/nemotron-3-super-120b-a12b"),
        "implementer": (zen, "nemotron-3-ultra-free"),
        "completeness_auditor": (zen, "mimo-v2.5-free"),
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
