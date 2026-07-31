"""Canonical prompt parity regression.

Fragments are the runtime truth; the monolithic ``{role}.md`` files are
generated fallbacks. Every role registered in ``fragments.json`` must compose
to exactly its monolithic fallback, and fallback mode must be identical to
fragment mode.
"""

import hashlib
import json
from pathlib import Path

import pytest

from council_of_agents.scripts import prompt_composer
from council_of_agents.scripts.prompt_composer import PromptComposer

ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = ROOT / "council_of_agents" / "prompts"

# Promoted P2.5 identity (see data/council_agent_evals/phase-a/live-canary/P2.5-chair-generalization).
CHAIR_MONOLITH_SHA256 = "cccbdd9494c86170f4b6a5e45c9e33c8caac3ae8d170adfcef3a6019ffc8a7bf"
CHAIR_PROMPT_VERSION = "846d4e43c45021ac"

# Promoted role monolith hashes. Strategist is P2.6 (repository-grounding
# candidate, promoted after the task-7 planning gate; see
# data/council_agent_evals/phase-a/prompts/P2.6/parent.json for the 3/3 gate
# evidence under data/council_agent_evals/phase-a/task6-gates/). The rest are
# P2.5 promoted (data/council_agent_evals/phase-a/prompts/P2.5/parent.json).
STRATEGIST_MONOLITH_SHA256 = "99d0ee788b4ff7257ac82e3e4953b028a01d2b3213600128ca83aa407c179e80"
PERSPECTIVE_MONOLITH_SHA256 = "e6b7dc408fb94137bbb24799a5e9447ca3c8f1538a83b9d7def15c0f136d6c3a"
MANAGER_MONOLITH_SHA256 = "bf83ae80ceeb90df8608f5413e580568f2cd1f391258c8b9e5247524f8e88ef6"
OPTION_COUNT_INSTRUCTION = (
    "Provide exactly 2, 3, or 4 options\u2014never fewer and never more. "
    "Merge related alternatives when necessary."
)

# SHA-256 of every non-Chair fragment referenced by fragments.json compositions.
# Promotion touched only the Chair; these hashes must not change.
NON_CHAIR_FRAGMENT_SHA256 = {
    "implementer/examples.md": "4c40b284101d630902cfa9d7b9cd9851d5cf0a2e104bd513c080db3d46049bb5",
    "implementer/identity.md": "da1cde1e9c9088560ab5bdf7a6212f0b4b6eb3d45aaccfde74fcbe905455ee66",
    "implementer/instructions.md": "005c4ddb666939a7bbb2769b22a99fe0c47bedb17bbefab21294b4252dff791d",
    "implementer/output_format.md": "95d59b95c8c9afc7987920fb5998d94549b429755e6519ec012ab9b66c904a26",
    "implementer_direct/execution_flow.md": "8ae71ec5a37c95aefb77c1d7fadde2837bcece23cf9bbea288f9ad2b2f415878",
    "implementer_direct/identity.md": "de99b2833fd8f6fccd16c30251cd88caa4cb4ee0fe156e4f7805db713f6d92af",
    "implementer_direct/instructions.md": "5c08b09792062e1f5f9053b324f45b29340a5585c5b8d361d93dc342e83395bd",
    "implementer_direct/output_format.md": "289096dbad2c24512f0b1c76fadd429f8e8ce32f51d8bc98cdddb7f8e4d2964e",
    "implementer_direct/permission_handling.md": "41687be03e54f0bddf38b6dc681fd4c3799d6d2d5f289fa74ce922f59e09482a",
    "implementer_direct/tool_selection.md": "2f9765d9efb15ed6db7fa8039cb4e359c07f7abef2baab372d89081ef237598a",
    "manager/examples.md": "d337b42308bc5b0b43a214460ae5a55a81776ee43faae94687b83ba9dcce1c48",
    "manager/identity.md": "0c10d3741475ee59585c480aa98be7e6a2b4060d6f41b9f70a2440eb884f3c68",
    "manager/instructions.md": "b1385856cbba6890c74eb43943dac563f96937e4f5b611c446c171d5914192b6",
    "manager/output_format.md": "9478eb20b6bd086b1578128696a522a0d080f4776b9450de34540e1a099b5f25",
    "manager/verdict_guidance.md": "679717a8747d8b7a44006003236de743a1379877d84c9926d518c9d98ee1f289",
    "perspective_analyzer/identity.md": "34edc2331213515282cca2d22b8ebf921d3bd3ed5b41ea61900fedfe37499458",
    "perspective_analyzer/instructions.md": "39b54fbb166b48cf81e89e9fcdcb0965d4a1ae686868518add9368a7db74f155",
    "perspective_analyzer/output_format.md": "e72b8ab30a8c52a2bf59c2b7a16b6a44c4f1bff8cfd78a115b2f784e0713b895",
    "shared/code_quality.md": "3421d6278874c67ad41f37680783c2f5d18b1d7ba57b5863749e396ae256e9ad",
    "shared/context_efficiency.md": "a986ae1279ade52ab33eb0d52d286e283842e9592d95261523cab3cbb0531633",
    "shared/default_to_action.md": "eb6e4d061a20bc7cf725718f68c0281581f06ce16425de7d2c122ebb81ef6010",
    "shared/investigate_before.md": "910ad954f5264e2f56fe7353c1ff232a1d374693b256f5c5ee0a011bead5c006",
    "shared/parallel_tools.md": "aaff96a374dd72455db4d1d8a705494c5b5a400d63a545a0027f02c53364a8d5",
    "shared/self_verification.md": "4a2ec14e816505916586d0d24f8caf10ac6a2a0a148fdee4b19281a90cdbba41",
    "shared/tool_selection.md": "0e7df46ae66dab56a3391e31c9acbdb7293784128631bb31bd9c37d37f1388df",
    "strategist/examples.md": "de155354a681a26701f7b84b3bc460ac200f89f6033bb826af2071a236a08710",
    "strategist/identity.md": "9a27f2788d1c151624d650061d03aaeb3ca08b74421cea4da0fbd6264ee0f136",
    "strategist/instructions.md": "7f88cc7eb8537f85f0d6aa93acd2de4b701644246e296af5cb3a36ec687cc951",
    "strategist/output_format.md": "b52db7472f818b843d1b1b48c2c33fcd213d23156ce39b888a1d9cf6664d580d",
    "validator/examples.md": "454dfaf9b69f03d2b5585ea40197fbb5db39816d5cbcf2c067225213726f6148",
    "validator/identity.md": "b7324ea1adf4db84027b67caccf96b350fabf249f89e73ac3a556f74594d2339",
    "validator/instructions.md": "cf5c45c436a656c33bbf0d965691b2373dbdb0284aab8b547b3130257faecf0c",
    "validator/output_format.md": "d49fdc4d353294ff75c48c046198c06652b8869dfb065d3ef5aaa25dbd6a38ba",
    "validator/verdict_definitions.md": "b64dcbde640c7b160ace3d3f400ce6c120f59dfcf7e04d1e4ced7940c45c53e4",
    "validator_task/examples.md": "4bf8d789f479abeb1738f01c3b5f995fdc68ec1d09bee5288ebe36a2ef0cd83e",
    "validator_task/identity.md": "e741f1cd61c950e13573b49817f91e938183b7052365cec3e81ca1319bb522aa",
    "validator_task/instructions.md": "fe2c173bf22ccd2f2d958738eef17c40a3903034dafca91ee1b81b0ea0ca8b76",
    "validator_task/issue_format.md": "98a723bc610d9bc856f313678d7a3067a7e517457dfe92a45f15069f6f20e7d0",
    "validator_task/output_format.md": "3ffa4f7afdd0424646e7a3ffb6559cf1bad441f326b34f6c6a57136fe0f52a5b",
}


def _registry() -> dict:
    return json.loads((PROMPTS_DIR / "fragments.json").read_text(encoding="utf-8"))


def _roles() -> list:
    return sorted(_registry()["compositions"])


def _fragment_paths(role: str) -> list:
    reg = _registry()
    return [
        reg["fragments"].get(key, key if key.endswith(".md") else f"{key}.md")
        for key in reg["compositions"][role]
    ]


@pytest.mark.parametrize("role", _roles())
def test_registered_fragments_exist_and_are_nonempty(role):
    """Never silently certify an incomplete composition."""
    missing = []
    empty = []
    for rel in _fragment_paths(role):
        path = PROMPTS_DIR / "fragments" / rel
        if not path.exists():
            missing.append(rel)
        elif not path.read_text(encoding="utf-8").strip():
            empty.append(rel)
    assert not missing, f"{role}: missing registered fragments: {missing}"
    assert not empty, f"{role}: empty registered fragments: {empty}"


@pytest.mark.parametrize("role", _roles())
def test_composed_prompt_equals_monolithic_fallback(role):
    composer = PromptComposer(PROMPTS_DIR)
    composed = composer.compose(role)
    assert composed, f"{role}: composed prompt is empty"
    monolith = composer._load_monolithic(role)
    assert monolith, f"{role}: monolithic fallback missing"
    assert composed == monolith, f"{role}: composed prompt != monolithic fallback"


@pytest.mark.parametrize("role", _roles())
def test_fragment_and_fallback_modes_produce_identical_prompts(role, monkeypatch):
    composer = PromptComposer(PROMPTS_DIR)
    composed = composer.compose(role)
    monkeypatch.setattr(prompt_composer, "USE_FRAGMENTS", False)
    fallback = PromptComposer(PROMPTS_DIR).compose(role)
    assert composed == fallback, f"{role}: fragment mode != fallback mode"


def test_chair_monolith_matches_promoted_p2_5_hash():
    raw = (PROMPTS_DIR / "chair.md").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CHAIR_MONOLITH_SHA256


def test_chair_composed_prompt_version():
    composed = PromptComposer(PROMPTS_DIR).compose("chair")
    version = hashlib.sha256(composed.encode("utf-8")).hexdigest()[:16]
    assert version == CHAIR_PROMPT_VERSION


def test_chair_option_count_instruction_occurs_once():
    composed = PromptComposer(PROMPTS_DIR).compose("chair")
    assert composed.count(OPTION_COUNT_INSTRUCTION) == 1


def test_strategist_monolith_matches_p2_6_hash():
    raw = (PROMPTS_DIR / "strategist.md").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == STRATEGIST_MONOLITH_SHA256


def test_perspective_analyzer_monolith_matches_p2_5_hash():
    raw = (PROMPTS_DIR / "perspective_analyzer.md").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PERSPECTIVE_MONOLITH_SHA256


def test_manager_monolith_matches_p2_5_hash():
    raw = (PROMPTS_DIR / "manager.md").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == MANAGER_MONOLITH_SHA256


@pytest.mark.parametrize("fragment", sorted(NON_CHAIR_FRAGMENT_SHA256))
def test_non_chair_fragment_hashes_unchanged(fragment):
    raw = (PROMPTS_DIR / "fragments" / fragment).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == NON_CHAIR_FRAGMENT_SHA256[fragment]
