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
    assert "lastHeartbeatText" in JS
    assert "const keepPinnedToBottom" in JS
    assert "if (keepPinnedToBottom) el.scrollTop = el.scrollHeight" in JS
    assert "_renderRunSummary(state)" in JS


def test_streamed_council_events_do_not_rebuild_open_aggregates():
    assert "scheduleRender(state, eventType)" in JS
    assert "ui.scheduleRender(s, ev)" in JS
    assert "eventType === 'thought_delta' && ledger.querySelector('[data-live-stream]')" in JS
    log_section = JS.split("Captain's log: record every meaningful event", 1)[1].split("// A committed thought", 1)[0]
    assert "'tool_progress'" not in log_section


def test_run_events_dedupe_tool_and_live_update_paths():
    assert "function sameFileOperation(left, right)" in JS
    assert "currentEntry.files.some(existing => sameFileOperation(existing, op))" in JS
    assert "a.endsWith(`/${b}`)" in JS


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
    assert ('<section class="council-center" aria-label="Council workspace">' in panel or
            '<section class="council-workspace" aria-label="Council workspace">' in panel)
    assert '<main class="council-center">' not in panel


def test_execution_stream_cockpit_invariants():
    # Canonical role mapping with full names for tooltips
    assert "perspective_analyzer: { tag: 'PERS', cls: 'strat', name: 'Perspective Analyzer' }" in JS
    assert "chair: { tag: 'CHAIR', cls: 'chair', name: 'Chairperson' }" in JS
    assert "const _resolveRole = (agent) => _roleMap[String(agent || '').toLowerCase()]" in JS
    assert 'title="${_esc(role.name || role.tag)}"' in JS

    # Real file diffing uses state.fileVersions
    assert "state.fileVersions" in JS
    assert "computeLineDiff(orig, curr)" in JS
    assert "diffMemo" in JS

    # Monotonic step numbering
    assert "let rowIndex = 1;" in JS
    assert "const _nextStep = () => String(rowIndex++).padStart(2, '0');" in JS

    # Vector SVG status & tool icons
    assert "const _statusIcon = (status) =>" in JS
    assert "viewBox=\"0 0 16 16\"" in JS
    assert "st-icon--done" in JS
    assert "st-icon--running" in JS

    # No thread delegation disruption in stream blocks
    assert "b.type === 'handoff'" not in JS

    # Cockpit styling and high-contrast borders
    assert ".ghost-editor-stream .active-cockpit-box" in CSS
    assert "animation: cockpit-spin" in CSS
    assert "@keyframes cockpit-spin" in CSS
    assert ".ghost-editor-stream .payload-diff" in CSS
    assert "content-visibility: auto" not in CSS.split(".ghost-editor-stream .row {", 1)[1].split("}", 1)[0]
    assert "color-mix(in srgb, var(--fg) 14%, transparent)" in CSS
    assert ".ghost-handoff" not in CSS


