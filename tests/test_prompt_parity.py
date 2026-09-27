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

# Promoted identity hashes
CHAIR_MONOLITH_SHA256 = "851aafd4401c59d814fe91cbee5eaae7049361389adbb9755d68ba1cd42faa99"
CHAIR_PROMPT_VERSION = "851aafd4401c59d8"

# Promoted role monolith hashes.
STRATEGIST_MONOLITH_SHA256 = "d6825e4d8b10b7c90d10d384cf488186f2668b6f8bfdbd727e0515d60bf1be4a"
PERSPECTIVE_MONOLITH_SHA256 = "3f736c4f0b35d99e58a9a7e1777876be769bcd23c0a718a1df19a85b065230fc"
MANAGER_MONOLITH_SHA256 = "e6aec8b65d5bd816b2e1449917a8fc79aa8f62c76f5aafc0ab4a6f2fdbde2741"
OPTION_COUNT_INSTRUCTION = "`options` lists 2-4 concrete choices."

# SHA-256 of every non-Chair fragment referenced by fragments.json compositions.
NON_CHAIR_FRAGMENT_SHA256 = {
    "implementer/examples.md": "b3d48267fcd2fb1d73a32adcf13da786d9bac07ef43cd9fddff3d439e211c717",
    "implementer/identity.md": "51ad5b3f9bb91257534dcac0da380a019f6b3e12badde4b8d4346fb300b5da24",
    "implementer/instructions.md": "eb8f81a5139b1f496b3ffba4e2675a6406c0a4dd807c74f5c81a591a2a823bda",
    "implementer/output_format.md": "25625e356cb62095a5937960ea3d3aefd7bea04ef55c5a3687f8842e637c438b",
    "implementer_direct/identity.md": "2cbc9039b7dbdead708617c425d17328f1d1a8b067ee3d0c0a22bd1c20d242a7",
    "implementer_direct/instructions.md": "06bd5b1338c9ae52a5c8263616f239a25245e5a3df442763335d91d8d8ad33b5",
    "implementer_direct/output_format.md": "5eeab0dfef8271bf7661ac05dc12fc3656b8d40d37ec0d47fffeb61447d4af5a",
    "implementer_direct/tool_selection.md": "0f11da7fc330c11b11090ff97eeb3fe568851d9c8b979bb4080195668feb1e05",
    "manager/examples.md": "1c42bdc6b3c829690b8dbaac516a4a3da7f863234c18fc8ad5d41170bb7399c6",
    "manager/identity.md": "9f9acf68b7ec967fbc7df6cc291df3871ba9b632733d0d0945e4ffaad10b66d7",
    "manager/instructions.md": "53f9e1a4eb5253cbe90d8ce8b1ed7711f1170837e748c3befe0095c8fc7da79f",
    "manager/output_format.md": "9f5909cf6a69e257241fbf096042c793a260157116dd5df26562fe0305c3c23d",
    "manager/verdict_guidance.md": "c27aa26729291bd766ebbe1a6678e5998682a38a69abcca7651eba205270a02d",
    "perspective_analyzer/identity.md": "c99d881a2e4d3c6ad9131e602cdec9da00b46912f379001200141ad958496db6",
    "perspective_analyzer/instructions.md": "16fc0d054ac3f141effebff38063ff2aa7ab4419005e6d82a86e94f94b425634",
    "perspective_analyzer/output_format.md": "ca3fad0ba0f88b313034c8762f9fe20e8be1c35c123095a6293394eb70a1f427",
    "shared/code_quality.md": "3421d6278874c67ad41f37680783c2f5d18b1d7ba57b5863749e396ae256e9ad",
    "shared/self_verification.md": "4a2ec14e816505916586d0d24f8caf10ac6a2a0a148fdee4b19281a90cdbba41",
    "strategist/examples.md": "a1dcbb225260242d8f0dc5baeefd2260ac2e5c801a1e0f76d790976167a470e8",
    "strategist/identity.md": "7045cabe879ad7ff7a3bfc0c7c619cbf137f9bfe9a85bebe332d8ccb2638d940",
    "strategist/instructions.md": "9b186164d83e60481875f7a8d6b918603abfff2e5ed0dd2f7335c5ecfff4f03e",
    "strategist/output_format.md": "7657a6f45936f597e09a8515057956de4cd519f53ccc5704a9b25bd8ffa404e4",
    "validator/examples.md": "454dfaf9b69f03d2b5585ea40197fbb5db39816d5cbcf2c067225213726f6148",
    "validator/identity.md": "b7324ea1adf4db84027b67caccf96b350fabf249f89e73ac3a556f74594d2339",
    "validator/instructions.md": "cf5c45c436a656c33bbf0d965691b2373dbdb0284aab8b547b3130257faecf0c",
    "validator/output_format.md": "d49fdc4d353294ff75c48c046198c06652b8869dfb065d3ef5aaa25dbd6a38ba",
    "validator/verdict_definitions.md": "b64dcbde640c7b160ace3d3f400ce6c120f59dfcf7e04d1e4ced7940c45c53e4",
    "validator_task/examples.md": "bd55dc21cebb4788b53992e92764436624b5773e4c5b63141d2c1e6120e029e6",
    "validator_task/identity.md": "f0e3ccc80460ea181e0248a7771c2e9a7c58ed6f9214316a36523cbf322a19ba",
    "validator_task/instructions.md": "246b64e4fc297165ced45ece87336c24a0f660bead65c3fcee84efc1348cacea",
    "validator_task/output_format.md": "6785f1386c622228420d7bd4c63cbc805e1b1d17b5af66fe4eebe47a5862a9e3"
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
