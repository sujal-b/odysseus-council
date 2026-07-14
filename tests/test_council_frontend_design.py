from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "js" / "council" / "council.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_council_console_has_responsive_run_summary():
    assert '<meta name="viewport" content="width=device-width, initial-scale=1"' in HTML
    for element_id in (
        "council-run-status", "council-run-stage", "council-run-progress-bar",
        "council-run-route", "council-run-tasks", "council-run-budget",
        "council-run-compacts", "council-run-cancel-btn",
    ):
        assert f'id="{element_id}"' in HTML
    assert "@media (max-width: 720px)" in CSS


def test_role_controls_are_keyboard_native_and_labeled():
    for role in ("chair", "strategist", "implementer", "manager"):
        assert f'<button type="button" class="compass-node" data-role="{role}"' in HTML
        assert f'aria-label="Configure {role.capitalize()}"' in HTML
    assert ".compass-node:focus-visible" in CSS


def test_renderer_avoids_heartbeat_full_renders_and_forced_log_scroll():
    assert "if (eventType === 'heartbeat')" in JS
    assert "const keepPinnedToBottom" in JS
    assert "if (keepPinnedToBottom) el.scrollTop = el.scrollHeight" in JS
    assert "_renderRunSummary(state)" in JS


def test_dynamic_code_tab_markup_is_escaped():
    assert 'data-filepath="${_esc(filepath)}"' in JS
    assert '${_esc(filename)}</button>' in JS
    assert '${_esc(titleText)}</span>' in JS


def test_console_is_theme_native_and_reduced_motion_safe():
    console_css = CSS.split("Council developer console v3", 1)[1]
    assert "--council-surface:" in console_css
    assert "@media (prefers-reduced-motion: reduce)" in console_css
    assert ".compass-particle { display: none !important; }" in console_css
    assert "#0c0907" not in console_css


def test_council_workspace_does_not_nest_a_main_landmark():
    panel = HTML.split('<div id="council-panel"', 1)[1].split("</div>\n\n    <!-- Unified chat input bar", 1)[0]
    assert '<section class="council-center" aria-label="Council workspace">' in panel
    assert '<main class="council-center">' not in panel
