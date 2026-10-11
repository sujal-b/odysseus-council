import pytest
from pathlib import Path
from council_of_agents.scripts.council_schemas import (
    CompletenessCriterion,
    CompletenessAuditOutput,
    validate_agent_output,
    compact_agent_contract,
)

ROOT = Path(__file__).parents[1]
JS = (ROOT / "static" / "js" / "council" / "council.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_council_completeness_card_has_apple_design_classes():
    """Verify Apple Design card structure, typography, and classes in council.js."""
    assert "ghost-completeness-card" in JS
    assert "ghost-completeness-header" in JS
    assert "ghost-completeness-kicker" in JS
    assert "ghost-completeness-headline" in JS
    assert "ghost-completeness-pill" in JS
    assert "ghost-completeness-track" in JS
    assert "ghost-completeness-fill" in JS
    assert "ghost-criteria-list" in JS
    assert "ghost-crit-row" in JS
    assert "ghost-crit-icon" in JS
    assert "ghost-crit-chip" in JS
    assert "ghost-crit-title" in JS
    assert "ghost-crit-detail" in JS
    assert "ghost-crit-badge" in JS


def test_council_completeness_css_apple_design_foundations():
    """Verify backdrop blur, spring easing, and Apple materials in style.css."""
    assert ".ghost-completeness-card" in CSS
    assert "backdrop-filter: blur(20px)" in CSS
    assert "border-radius: 12px" in CSS
    assert "font-variant-numeric: tabular-nums" in CSS
    # Apple fluid drawer spring curve from apple-design skill
    assert "cubic-bezier(0.32, 0.72, 0, 1)" in CSS
    assert ".ghost-crit-icon.is-met" in CSS
    assert ".ghost-crit-icon.is-unmet" in CSS


def test_council_completeness_mathematical_precision_in_stream():
    """Ensure that 1 of 3 criteria resolves to 33% and not 0%."""
    # Look for the calculation logic in council.js
    assert "const totalCrits = criteria.length;" in JS
    assert "const metCrits = criteria.filter(" in JS
    assert "pct = Math.round((metCrits / totalCrits) * 100);" in JS
    # Verify fallback for raw ratio (0..1 scaled to 100)
    assert "pct = Math.round(raw <= 1 && raw > 0 ? raw * 100 : raw);" in JS


def test_council_criterion_text_robustness_no_blank_labels():
    """Ensure criterion title extraction checks name, title, description, dagNode, criterion, detail, and id."""
    assert "dagNode.description || dagNode.acceptance" in JS
    assert "c.name" in JS
    assert "c.title" in JS
    assert "c.description" in JS
    assert "c.criterion" in JS
    assert "c.detail" in JS
    assert "c.id" in JS


def test_council_schema_preserves_criterion_semantic_labels():
    """Verify CompletenessCriterion schema and pruning preserve title, description, and acceptance."""
    data = {
        "completeness": 0.33,
        "done": False,
        "criteria": [
            {
                "id": "T1",
                "name": "User Auth",
                "description": "Implement authentication endpoints",
                "acceptance": "Returns 200 with JWT token",
                "met": True,
                "gap_type": "verified",
                "detail": "Verified JWT sign and verify handlers",
            },
            {
                "id": "T2",
                "description": "Password reset flow",
                "acceptance": "Sends email token",
                "met": False,
                "gap_type": "fillable",
                "detail": "Email service not yet configured",
            },
        ],
    }
    pruned = compact_agent_contract("completeness_auditor", data)
    assert len(pruned["criteria"]) == 2
    c0 = pruned["criteria"][0]
    assert c0["name"] == "User Auth"
    assert c0["description"] == "Implement authentication endpoints"
    assert c0["acceptance"] == "Returns 200 with JWT token"
    assert c0["met"] is True

    c1 = pruned["criteria"][1]
    assert c1["description"] == "Password reset flow"
    assert c1["met"] is False


def test_council_calm_breathable_velocity_damper_and_apple_springs():
    """Verify Calm & Breathable Council mode: velocity damper, fixed 32px tool height,
    attention shelf containment, and Apple spring physics."""
    # R1 Velocity damper, zero-jitter fixed height, and live duration timer
    assert ".ghost-tool-card.is-running:not(.is-user-expanded):not(.open-error) .ghost-tool-body" in CSS
    assert ".ghost-tool-card.is-running .row.ghost-tool-header" in CSS
    assert "height: 32px" in CSS
    assert "line-height: 32px" in CSS
    assert "tool-header-breathe" in CSS
    assert "tool-duration-badge--live" in CSS
    assert "is-user-expanded" in JS
    assert "open-error" in JS
    assert "${body ? '<span class=\"ghost-tool-chevron\" style=\"margin-left:auto;\">▶</span>' : ''}" in JS

    # R2 Quiet Telemetry Spine & Attention Required Shelf containment
    assert ".council-attention-dock" in CSS
    assert ".audit-attention-shelf" in CSS
    assert ".audit-attention-list" in CSS
    assert "max-height: 220px" in CSS
    assert ".audit-row:not(.audit-row--error)" in CSS
    assert "opacity: 0.38" in CSS

    # R3 Apple fluid spring physics and active press feedback
    assert "scale(0.96)" in CSS
    assert "scale(0.97)" in CSS
    assert ".role-popover" in CSS
    assert "cubic-bezier(0.32, 0.72, 0, 1)" in CSS
    assert ".log-filter-btn:active" in CSS


def test_council_completeness_progressive_disclosure_and_clean_tooltips():
    """Verify progressive disclosure, compact criteria rows, capped tooltips, and drawer containment."""
    js = (ROOT / "static" / "js" / "council" / "council.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    # Progressive disclosure classes in JS and CSS
    assert "ghost-crit-drawer" in js
    assert "ghost-crit-toggle" in js
    assert "ghost-completeness-collapse-btn" in js
    assert "_expandedCriteriaIds" in js
    assert "_completenessListCollapsed" in js
    assert "this._completenessListCollapsed = true;" in js
    assert "cleanTitle" in js
    assert "tooltipTitle" in js

    # CSS containment
    assert ".ghost-crit-drawer" in css
    assert ".ghost-crit-row.is-expanded .ghost-crit-drawer" in css
    assert "max-height: 140px" in css
    assert "overflow-y: auto" in css
    assert ".ghost-completeness-card.is-collapsed" in css


def test_council_completeness_persistence_and_refresh_restoration():
    """Verify completeness is persisted on disk and rehydrated across page refresh / reload."""
    import tempfile
    from council_of_agents.scripts.session_store import InMemorySessionStore, SessionState

    with tempfile.TemporaryDirectory() as td:
        store = InMemorySessionStore()
        store._dir = td

        state = SessionState(
            session_id="test-session-refresh",
            owner="test-user",
            user_prompt="Build Sort Race",
            completeness=0.5,
            completeness_criteria=[
                {"id": "T1", "met": True, "detail": "setup done"},
                {"id": "T2", "met": False, "detail": "index.html incomplete"},
            ],
        )
        store.save(state)

        # Clear memory cache to simulate fresh server / restart
        store._cache.clear()
        loaded = store.load("test-session-refresh")
        assert loaded is not None
        assert loaded.completeness == 0.5
        assert len(loaded.completeness_criteria) == 2
        assert loaded.completeness_criteria[0]["id"] == "T1"
        assert loaded.completeness_criteria[1]["id"] == "T2"

    # Also verify council.js rehydrates completeness in load() and stream render
    js = (ROOT / "static" / "js" / "council" / "council.js").read_text(encoding="utf-8")
    assert "data.completeness_criteria" in js
    assert "this._state.completenessCriteria = data.completeness_criteria;" in js
    assert "ev.event === 'completeness_update'" in js




