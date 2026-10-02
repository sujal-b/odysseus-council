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


def test_perspective_findings_reach_the_ledger_row():
    # Severity is classified server-side and only carried through, never re-derived
    # in the browser, so the row cannot contradict the approval gate.
    assert "perspective: (agent === 'perspective_analyzer' && e.extra?.perspective)" in JS
    persp = JS.split("const _perspChips = (p) => {", 1)[1].split("};", 1)[0]
    assert "JSON.parse" not in persp
    assert "p.evidence === 'invalid' || p.evidence === 'empty'" in persp

    # A non-clear verdict must surface the blocked glyph, not a done tick.
    assert "_pClear ? 'st--done' : 'st--blocked'" in JS
    assert "_statusIcon(_pClear ? 'APPROVED' : 'BLOCKED')" in JS

    for tone in ("is-block", "is-mustfix", "is-advisory", "is-ok", "is-invalid"):
        assert f".ghost-editor-stream .persp-chip.{tone}" in CSS or f".ghost-editor-stream .persp-score.{tone}" in CSS
    assert ".ghost-editor-stream .persp-chips" in CSS


def test_perspective_card_container_and_telemetry_deck_invariants():
    # Card wrapper encloses the row and telemetry deck, preventing freely roaming text
    assert '<div class="ghost-perspective-card${cardCls}">' in JS
    assert ".ghost-editor-stream .ghost-perspective-card" in CSS
    assert ".ghost-editor-stream .ghost-perspective-card.is-blocked" in CSS
    assert ".ghost-editor-stream .ghost-perspective-card.is-mustfix" in CSS

    # Telemetry bay alignment with content column and responsive collapse
    assert "margin-left: 104px" in CSS
    assert "@media (max-width: 720px)" in CSS

    # Metric cells group each perspective dimension and isolate disposition badges
    assert "persp-metric-cell" in JS
    assert "persp-metric-label" in JS
    assert "persp-divider" in JS
    assert ".ghost-editor-stream .persp-metric-cell" in CSS
    assert ".ghost-editor-stream .persp-metric-cell--overall" in CSS
    assert ".ghost-editor-stream .persp-metric-label" in CSS
    assert ".ghost-editor-stream .persp-divider" in CSS

    # Accessible region
    assert 'role="region"' in JS
    assert 'aria-label="Perspective analysis metrics"' in JS


def test_active_agent_status_ticker_invariants():
    # Micro-typography classes and clean sentence casing
    assert "_ghostBadgeHtml(state)" in JS
    assert "_ghostBadgeText(state)" in JS
    assert "_roleMeta(state.activeAgent)" in JS
    assert '<span id="council-ghost-agent"><span class="ticker-idle">Idle</span></span>' in HTML

    # CSS styles for semantic ticker elements
    assert ".ticker-role" in CSS
    assert ".ticker-sep" in CSS
    assert ".ticker-verb" in CSS
    assert ".ticker-tool" in CSS
    assert ".ticker-time" in CSS
    assert ".ticker-idle" in CSS
    assert ".council-pane-status {" in CSS
    assert "text-transform: none;" in CSS


def test_high_workload_task_rail_and_burst_grid_invariants():
    # Wave grouping & bounding for complex task sets (T100+)
    assert "_computeTaskWaves(nodes)" in JS
    assert "dag-rail-wave" in JS
    assert ".dag-rail-wave" in CSS
    assert ".dag-rail-more" in CSS
    assert ".dag-rail-pill-summary" in CSS
    assert "_taskCount <= 5" in JS

    # Unified 4-column grid alignment for tool cards and bursts
    assert "_extractToolTarget = (tool, args = {}, cmd = '') =>" in JS
    assert "row row--burst-item" in JS
    assert "row ghost-burst-header" in JS
    assert "tool-tag" in JS
    assert ".ghost-editor-stream .tool-tag" in CSS
    assert ".ghost-editor-stream .row.row--burst-item" in CSS
    assert ".ghost-editor-stream .row.ghost-burst-header" in CSS


def test_execution_stream_monochrome_palette_invariants():
    # Role tags (.ag) default to muted zinc/neutral monospace tags, not bright orange
    ag_block = CSS.split(".ghost-editor-stream .ag {", 1)[1].split("}", 1)[0]
    assert "var(--dim" in ag_block
    assert "var(--font-mono" in ag_block

    # Role overrides for standard council agents use muted neutral, sys uses fail
    ag_roles = CSS.split(".ghost-editor-stream .ag.ag--chair,", 1)[1].split(".ghost-editor-stream .ct", 1)[0]
    assert ".ghost-editor-stream .ag.ag--sys" in ag_roles
    assert "var(--fail" in ag_roles

    # Tool tags default to muted neutral tags
    tool_tag_block = CSS.split(".ghost-editor-stream .tool-tag {", 1)[1].split("}", 1)[0]
    assert "var(--dim" in tool_tag_block

    # Burst header and perspective card substrates default to neutral dark panel mixes
    assert ".ghost-burst-group:hover" in CSS
    burst_header_block = CSS.split(".ghost-editor-stream .row.ghost-burst-header {", 1)[1].split("}", 1)[0]
    assert "var(--strat)" not in burst_header_block
    assert "var(--panel)" in burst_header_block

    persp_card_block = CSS.split(".ghost-editor-stream .ghost-perspective-card {", 1)[1].split("}", 1)[0]
    assert "var(--strat" not in persp_card_block
    assert "var(--panel)" in persp_card_block

    # JS templates do not contain inline candy color overrides
    assert 'planned <strong style="color:var(--impl)">' not in JS
    assert '<span class="bd" style="color:var(--strat);">' not in JS


def test_dag_svg_node_text_wrapping_and_tooltip_invariants():
    # DAG SVG text wrapping helper bounds line width inside node rects
    assert "_wrapSvgText" in JS
    assert "<tspan" in JS
    assert "maxLineChars" in JS

    # Full task description is preserved as a native SVG hover tooltip
    assert "<title>${_esc(n.id)}: ${_esc(desc)}</title>" in JS


def test_tool_tag_overflow_containment_invariants():
    # Tool tag text must be compactly mapped to 4-5 char verbs
    assert "_shortToolName" in JS

    # CSS must strictly contain tool tags and prevent horizontal text blowout
    tag_css = CSS.split(".ghost-editor-stream .tool-tag {", 1)[1].split("}", 1)[0]
    assert "overflow: hidden" in tag_css
    assert "text-overflow: ellipsis" in tag_css
    assert "white-space: nowrap" in tag_css


def test_stream_ledger_flex_shrink_and_drawer_scroll_invariants():
    # Direct ledger children must not be flex-shrink targets when drawers expand
    ledger_children = CSS.split(".ghost-stream-ledger > * {", 1)[1].split("}", 1)[0]
    assert "flex-shrink: 0;" in ledger_children

    # Expanded tool bodies must support vertical scroll for large payloads
    tool_open = CSS.split(".ghost-tool-card.open .ghost-tool-body {", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto;" in tool_open

    # Expanded burst bodies must support vertical scroll for large multi-tool bursts
    burst_open = CSS.split(".ghost-burst-group.burst-open .ghost-burst-body {", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto;" in burst_open


def test_active_cockpit_bounded_drawer_and_sculpted_stream_invariants():
    # Cockpit box is bounded by default so it never stretches execution stream to bottom
    cockpit_box = CSS.split(".ghost-editor-stream .active-cockpit-box {", 1)[1].split("}", 1)[0]
    assert "max-height: clamp(" in cockpit_box
    assert "overflow: hidden" in cockpit_box

    # Expanded drawer state allows deeper inspection without blowing up layout
    assert ".ghost-editor-stream .active-cockpit-box.is-expanded" in CSS
    assert ".ghost-editor-stream .active-cockpit-toggle" in CSS

    # Role accents dynamically tint active cockpit box
    assert ".ghost-editor-stream .active-cockpit-box.role--strat" in CSS
    assert ".ghost-editor-stream .active-cockpit-box.role--impl" in CSS
    assert ".ghost-editor-stream .active-cockpit-box.role--mgr" in CSS

    # Active cockpit content is scrollable with custom slim scrollbar
    cockpit_content = CSS.split(".ghost-editor-stream .active-cockpit-content {", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto" in cockpit_content
    assert ".ghost-editor-stream .active-cockpit-content::-webkit-scrollbar" in CSS

    # Live update preserves internal scroll position and auto-pins
    assert "const scrollContainer = liveCard.querySelector('.active-cockpit-content')" in JS
    assert "scrollContainer.scrollTop = scrollContainer.scrollHeight" in JS


def test_thought_sculptor_and_telemetry_card_invariants():
    # Thought sculpting functions clean DSML noise and leaked tool markup
    assert "function _cleanNoiseAndTags(text)" in JS
    assert "function _dedupeStatements(text)" in JS
    assert "function _formatTabularData(text)" in JS
    assert "function _renderFindingCard(data)" in JS
    assert "function _renderJsonTelemetryCard(parsed)" in JS

    # Telemetry card styles exist in CSS
    assert ".ghost-editor-stream .ghost-telemetry-finding" in CSS
    assert ".ghost-editor-stream .ghost-telemetry-finding.is-warn" in CSS
    assert ".ghost-editor-stream .ghost-telemetry-finding.is-block" in CSS
    assert ".ghost-editor-stream .telemetry-badge" in CSS
    assert ".ghost-editor-stream .telemetry-task-chip" in CSS
    assert ".ghost-editor-stream .telemetry-finding-evidence" in CSS

    # Completed think blocks support expandable past thought drawer
    assert "ghost-thought-card" in JS
    assert "ghost-thought-body" in JS
    assert ".ghost-thought-card" in CSS
def test_robust_thought_stream_sculptor_under_stress():
    # Invariant: All semantic thought sculpting and parsing engines must exist in JS
    assert "function _cleanNoiseAndTags(text)" in JS
    assert "function _normalizeStreamBoundaries(text)" in JS
    assert "function _formatToolDeclarations(text)" in JS
    assert "function _dedupeStatements(text)" in JS
    assert "function safeParseJson(str)" in JS
    assert "function extractJsonBlocks(text)" in JS
    assert "function _renderReviewCard(data)" in JS
    assert "function _renderDeliverableCard(data)" in JS
    assert "function _renderPlanCard(data)" in JS
    assert "function _renderToolCallsCard(data)" in JS

    # Run stress test against actual council.js using Node subprocess
    node_script = """
    const fs = require('fs');
    const jsContent = fs.readFileSync('static/js/council/council.js', 'utf8');

    // Extract sculptor functions
    const funcNames = [
        '_esc', '_cleanNoiseAndTags', '_normalizeStreamBoundaries',
        '_formatToolDeclarations', '_dedupeStatements', '_isSimilarStatement',
        '_formatTabularData', '_renderFindingCard', '_renderReviewCard',
        '_renderDeliverableCard', '_renderPlanCard', '_renderToolCallsCard',
        '_renderJsonTelemetryCard', 'safeParseJson', 'extractJsonBlocks'
    ];

    let extractedCode = '';
    funcNames.forEach(fn => {
        const regex = new RegExp('(function\\\\s+' + fn + '\\\\b[\\\\s\\\\S]*?^})', 'm');
        const m = jsContent.match(regex);
        if (m) extractedCode += m[1] + '\\n\\n';
    });

    eval(extractedCode);

    // Test 1: Leaked DSML and tool tags
    const dsmlRaw = "I'll inspect.\\n<\\uFF5C\\uFF5CDSML\\uFF5C\\uFF5C calls>\\n<\\uFF5C\\uFF5CDSML\\uFF5C\\uFF5C invoke name=\\"ls\\">\\n</\\uFF5C\\uFF5CDSML\\uFF5C\\uFF5C invoke>\\n</\\uFF5C\\uFF5CDSML\\uFF5C\\uFF5C calls>Findings.";
    const dsmlClean = _cleanNoiseAndTags(dsmlRaw);
    if (dsmlClean.includes('DSML') || dsmlClean.includes('<') || dsmlClean.includes('>')) {
        throw new Error('DSML tag leak: ' + dsmlClean);
    }

    // Test 2: Glued boundaries and broken half-JSON repair
    const gluedRaw = 'result}{"confidence": 0.85, "verdict": "REVISE"}next.I\\'ll execute.\\nincorrect.,\\n"issues": [{"severity": "critical", "task_id": "T1", "description": "missing evidence"}]\\n}';
    const norm = _normalizeStreamBoundaries(gluedRaw);
    if (!norm.includes('}\\n\\n{')) throw new Error('Glued braces not separated');
    if (!norm.includes('next. I\\'ll')) throw new Error('Glued sentences not separated');
    if (!norm.includes('{\\n"issues"')) throw new Error('Broken half-JSON not repaired');

    // Test 3: Balanced nested JSON extraction
    const nestedRaw = 'Plan: {"tasks": [{"id": "T1", "sub": {"nested": true}}], "risks": ["r1"]} done.';
    const blocks = extractJsonBlocks(nestedRaw);
    if (blocks.length !== 1 || !blocks[0].parsed.tasks[0].sub.nested) {
        throw new Error('Nested JSON extraction failed');
    }

    // Test 4: Invalid escapes in JSON (e.g. \\` and Windows paths)
    const invalidEscapeRaw = '{"status": "DONE", "details": "Path \\\\`ls .\\\\` under C:\\\\\\\\Users\\\\\\\\Hp"}';
    const parsed = safeParseJson(invalidEscapeRaw);
    if (!parsed || parsed.status !== 'DONE') {
        throw new Error('safeParseJson failed on invalid escapes');
    }

    // Test 5: Tool declaration list formatting
    const toolListRaw = 'Tool call list:\\n\\nTool: glob\\n- pattern = */\\n\\nTool: ls\\n- path = ./';
    const toolDeck = _formatToolDeclarations(toolListRaw);
    if (!toolDeck.includes('ghost-tool-plan-deck') || !toolDeck.includes('glob') || !toolDeck.includes('ls')) {
        throw new Error('Tool declaration formatting failed');
    }

    console.log('NODE_STRESS_OK');
    """

    import subprocess
    proc = subprocess.run(["node"], input=node_script, capture_output=True, text=True, encoding="utf-8", check=True)
    assert "NODE_STRESS_OK" in proc.stdout




def test_structured_telemetry_and_tool_plan_css_invariants():
    # Card substrates and border accents
    assert ".ghost-editor-stream .ghost-telemetry-card" in CSS
    assert ".ghost-editor-stream .ghost-telemetry-review" in CSS
    assert ".ghost-editor-stream .ghost-telemetry-deliverable" in CSS
    assert ".ghost-editor-stream .ghost-telemetry-plan" in CSS
    assert ".ghost-editor-stream .ghost-tool-plan-deck" in CSS

    # Issue sub-items and suggestions
    assert ".ghost-editor-stream .telemetry-review-issues" in CSS
    assert ".ghost-editor-stream .telemetry-issue-item" in CSS
    assert ".ghost-editor-stream .telemetry-finding-suggestion" in CSS

    # Tool invocation chips
    assert ".ghost-editor-stream .tool-plan-badge" in CSS
    assert ".ghost-editor-stream .tool-plan-chips" in CSS
    assert ".ghost-editor-stream .tool-plan-chip" in CSS

    # Markdown tables inside execution stream
    assert ".ghost-editor-stream .ghost-md table" in CSS
    assert ".ghost-editor-stream .ghost-md th" in CSS
    assert ".ghost-editor-stream .ghost-md td" in CSS


def test_verbatim_user_trial_dump_sculpting():
    # Stress test passing the exact verbatim thought log stream copied by user during trial runs
    raw_user_dump = """
    I'll inspect the workspace using read-only tools.

    <｜｜DSML｜｜ calls>

    <｜｜DSML｜｜ invoke name="ls">

    <｜｜DSML｜｜ parameter name="path" string="true">./</｜｜DSML｜｜ parameter>

    </｜｜DSML｜｜ invoke>

    </｜｜DSML｜｜ calls><｜｜DSML｜｜ calls>

    <｜｜DSML｜｜ invoke name="glob">

    <｜｜DSML｜｜ parameter name="pattern" string="true">*/</｜｜DSML｜｜ parameter>

    </｜｜DSML｜｜ parameter>

    </｜｜DSML｜｜ invoke>

    </｜｜DSML｜｜ calls>The workspace inspection is complete. Findings below.

    T1 Findings
    Workspace root: C:\\Users\\Hp\\Desktop\\weather — empty.

    Directory/file listing:

    ls ./ → (empty)
    glob */ → No files matching '*/'
    Requested checks (read-only):

    src/ directory: does not exist
    tests/ directory: does not exist
    src/dedup/: does not exist
    tests/ contents: none (directory absent)
    Package layout markers:
    - __init__.py: absent
    - pyproject.toml: absent

    - setup.py: absent

    python3 / pytest runnability: NOT VERIFIED — this task is guarded READ-ONLY; bash/python channels are rejected by the workspace guard, so I could not execute version probes.
    Conclusions:

    The repository is a clean slate. All deliverables named in the parent objective (src/dedup/, tests/) must be created from scratch by downstream write tasks. There is no existing package scaffolding, no test harness, and no configuration to preserve or conform to. Downstream tasks should:

    Create the src/dedup/ package with __init__.py (and likely a dedup.py/cli.py module).
    Create the tests/ directory with __init__.py as needed.
    Optionally add a pyproject.toml for packaging and pytest configuration.
    Independently confirm python3 and pytest availability at task start (could not be probed here).
    {
      "status": "DONE",
      "files_created": [],
      "files_modified": [],
      "verification_details": "Read-only inspection. `ls ./` returned empty; `glob **/*` returned no matches. No write/tool write channels used per guard. Python/pytest runnability unverifiable in read-only mode.",
      "notes": "Workspace root is completely empty. No src/, tests/, src/dedup/, __init__.py, pyproject.toml, or setup.py exist. Downstream tasks must build package layout from scratch. Environment (python3/pytest) could not be probed due to the read-only guard; recommend confirming at implementation start."
    }
    {
    "confidence": 0.85,

    "issues": [

    {

    "description": "The acceptance criteria require positive evidence of Python and pytest availability, but the output explicitly states that python3 --version and pytest --version were NOT verified. This is a required element of the task contract.",

    "evidence": "python3 / pytest runnability: NOT VERIFIED — this task is guarded READ-ONLY; bash/python channels are rejected by the workspace guard, so I could not execute version probes.",

    "severity": "critical",

    "suggestion": "Attempt a read-only version probe for python3 and pytest (version commands do not create or modify files). If the guard truly blocks all command execution, report this as a blocker with the exact guard error text as evidence, rather than omitting the required environmental confirmation.",

    "task_id": "T1"

    }

    ],

    "summary": "The workspace listing portion of the acceptance criteria is met: ls ./ returned empty and glob */ returned no matches, establishing that no src/, tests/, src/dedup/, __init__.py, pyproject.toml, or setup.py exist. The read-only constraint was respected (no files created or modified). However, the acceptance criteria explicitly require successful python3 --version and pytest --version output as positive evidence, and the output states this was not verified. Since the task's acceptance cannot be fully satisfied without this evidence, a revision is required.",

    "verdict": "REVISE"

    }I'll inspect the workspace using only the permitted read-only channels.

    <｜｜DSML｜｜ calls>

    <｜｜DSML｜｜ invoke name="ls">

    <｜｜DSML｜｜ parameter name="path" string="true">./</｜｜DSML｜｜ parameter>

    </｜｜DSML｜｜ invoke>

    <｜｜DSML｜｜ invoke name="glob">

    <｜｜DSML｜｜ parameter name="pattern" string="true">*/</｜｜DSML｜｜ parameter>

    </｜｜DSML｜｜ invoke>

    </｜｜DSML｜｜ calls>## T1 — Reconnaissance Findings (Read-Only)

    Workspace root: C:\\Users\\Hp\\Desktop\\weather — empty.

    Directory / file existence:

    Path	Present?
    ./src/	No
    ./tests/	No
    ./src/dedup/	No
    ./__init__.py (root)	No
    ./pyproject.toml	No
    ./setup.py	No
    ls ./ returned an empty directory listing.
    glob */ returned no matches — confirming there are no files (hidden or otherwise) anywhere under the workspace root.
    Package layout: None exists yet. There is no src/ tree, no tests/ tree, no __init__.py, and no packaging metadata (pyproject.toml/setup.py). The entire project must be scaffolded from scratch.

    Environment probes (python3 --version, pytest --version):

     BLOCKER — could not be executed. The WorkPacket snapshot (write_scope: [], guarded execution) and my workspace_root: false context restrict me to the read_file, ls, and glob channels. Every bash and python invocation is rejected by the workspace guard, so no version probe (including a non-mutating --version call) can be run through my permitted channels.

    No file, directory, or environment state was created, modified, or deleted. This was a pure read-only inspection.

    Recommendation for dependent tasks: Python/pytest availability must be confirmed by a follow-up task (or a step) that carries explicit command-execution permission against this workspace. This reconnaissance task's contract did not grant it, and the guard enforces that boundary.

    {
      "status": "DONE",
      "files_created": [],
      "files_modified": [],
      "verification_details": "Read-only inspection only: `ls ./` returned an empty directory; `glob **/*` returned no matches under C:\\\\Users\\\\Hp\\\\Desktop\\\\weather. Confirmed absence of src/, tests/, src/dedup/, __init__.py, pyproject.toml, setup.py. python3/pytest version probes could NOT be run — bash/python channels are rejected by the read-only workspace guard for this task (write_scope: []).",
      "notes": "BLOCKER: environment runnability (python3, pytest) is unverifiable from within this task because the WorkPacket is contractually read-only (write_scope: []) and the guard rejects all bash/python channels, including non-mutating `--version` probes. The exact blocker is the guard itself (no command channel available; no guard error string surfaced because the channels were never granted). Workspace is completely empty; all project scaffolding (src/dedup/, tests/, packaging) must be authored from scratch by a write-enabled downstream task. No files were touched."
    }
    Task T1's acceptance criteria require positive evidence of both workspace contents and Python/pytest availability. The workspace listing was provided, but the Python/pytest availability check was not performed, so the acceptance criteria are not fully met. The task is strictly read-only, but running python3 --version and pytest --version is a non-mutating probe and does not violate the 'no files created or modified' constraint; the prior reasoning conflating read-only scope with a prohibition on running version commands is incorrect.,
    "issues": [

    {

    "severity": "critical",

    "task_id": "T1",

    "description": "The acceptance criterion 'Python/pytest availability is reported with positive evidence (successful python3 --version and pytest --version output)' was not satisfied. The output explicitly states the probes could not be run and reports a 'BLOCKER' with no version output captured.",

    "evidence": "Output: ' BLOCKER — could not be executed... no version probe ... can be run through my permitted channels.' No python3 --version or pytest --version output appears anywhere in the deliverable.",

    "suggestion": "Run python3 --version and pytest --version (non-mutating, read-only operations that do not create/modify files) and include their exact stdout as positive evidence. If the execution environment genuinely forbids all command channels, report BLOCKED rather than DONE, since the task's acceptance cannot be verified."

    },

    {

    "severity": "warning",

    "task_id": "T1",

    "description": "The task reported status DONE despite an unresolved blocker for a required acceptance criterion. A task whose mandatory acceptance evidence is missing should not self-report DONE.",

    "evidence": "notes field contains 'BLOCKER: environment runnability (python3, pytest) is unverifiable from within this task' while JSON status is 'DONE'.",

    "suggestion": "Either supply the missing version-probe evidence and keep DONE, or downgrade the status to BLOCKED to accurately reflect the unmet acceptance criterion."

    }

    ],

    "confidence": 0.8

    }The task is rehydrated. Let me inspect the workspace read-only. I'll list the root and check for existing src/, tests/, package files.

    <｜｜DSML｜｜ calls>

    <｜｜DSML｜｜ invoke name="ls">

    </｜｜DSML｜｜ invoke>

    </｜｜DSML｜｜ calls>The workspace root is empty. Since this revision (attempt 3) requires me to re-read the files and confirm findings read-only, let me check the additional paths explicitly.

    <｜｜DSML｜｜ calls>

    <｜｜DSML｜｜ invoke name="ls">

    <｜｜DSML｜｜ parameter name="path">./src</｜｜DSML｜｜ parameter>

    </｜｜DSML｜｜ invoke>

    <｜｜DSML｜｜ invoke name="ls">

    <｜｜DSML｜｜ parameter name="path">./tests</｜｜DSML｜｜ parameter>

    </｜｜DSML｜｜ invoke>

    <｜｜DSML｜｜ invoke name="glob">

    <｜｜DSML｜｜ parameter name="pattern">*/</｜｜DSML｜｜ parameter>

    </｜｜DSML｜｜ invoke>

    </｜｜DSML｜｜ calls>

    I'll inspect the workspace as required by the READ-ONLY task T1. Using read-only discovery tools only.

    Tool call list:

    Tool: glob
    - pattern = */

    Tool: ls
    - path = ./{

    "tool_calls": [

    {

    "tool": "ls",

    "parameters": {

    "path": "./"

    }

    },

    {

    "tool": "glob",

    "parameters": {

    "pattern": "*/"

    }

    }

    ]

    }{"confidence": 0.95, "issues": [{"description": "Output contains only tool call declarations (glob pattern '*/' and ls './') without any resulting file listing or environment summary. Acceptance criteria require a report of current files and environment available.", "evidence": "Task output shows only 'Tool call list' and a JSON of tool_calls, no file list or environment report.", "severity": "critical", "suggestion": "Include the results of the read-only discovery: list the files found and any environment details (e.g., from ls/glob output). Ensure the report is presented as the task deliverable, not just the tool invocations.", "task_id": "T1"}], "summary": "Task T1 attempted read-only inspection via glob and ls but failed to provide the required report of current files and environment. No writes were performed, satisfying the no-write criterion.", "verdict": "REVISE"}Task T1 output indicates intent to inspect via read-only tools but fails to deliver the required report of current files and environment.I'll execute the bounded WorkPacket T1 as specified: a READ-ONLY inspection of the workspace. Per the rehydrated contract, write_scope is empty, so I will not create any files and will only use read-only inspection tools (ls, glob, read_file, grep).

    Let me inspect the workspace.

    Tool call list:

    Tool: ls
    - path = ./

    Tool: glob
    - pattern = */

    Tool: read_file
    - path = ./

    Tool: grep
    - pattern = .*

    - path = ./

    - output_mode = files_with_matches
    Strategist is planning…

    {

    "tasks": [

    {

    "id": "T1",

    "description": "Inspect workspace files and environment.",

    "depends_on": [],

    "read_scope": [

    "./"

    ],

    "write_scope": [],

    "acceptance": "Workspace inspection completed; existing files and absence of temperature_checker.py are reported.",

    "acceptance_ids": [

    "workspace_inspection_complete"

    ],

    "verification": null,

    "workspace_root": false

    },

    {

    "id": "T2",

    "description": "Create temperature_checker.py that uses OpenWeatherMap via urllib, defines DEFAULT_INDIAN_CITIES, exposes get_temperature(city, api_key=None), and provides a CLI main() for checking one or more Indian cities.",

    "depends_on": [

    "T1"

    ],

    "read_scope": [

    "./"

    ],

    "write_scope": [

    "./"

    ],

    "acceptance": "temperature_checker.py compiles cleanly, imports without network access, exposes get_temperature, main, and DEFAULT_INDIAN_CITIES containing at least Delhi.",

    "acceptance_ids": [

    "temperature_checker_script_created"

    ],

    "verification": {

    "type": "shell",

    "command": "python3 -m py_compile temperature_checker.py"

    },

    "workspace_root": false

    }

    ],

    "risks": [

    "OpenWeatherMap API key is required for live temperature checks.",

    "Network access or API rate limits may prevent live calls.",

    "City names can be ambiguous and may require exact API city identifiers."

    ]

    }
    """

    import subprocess
    import json

    node_validator = f"""
    const fs = require('fs');
    const jsContent = fs.readFileSync('static/js/council/council.js', 'utf8');

    // Mock minimal markdownModule
    const markdownModule = {{
        squashOutsideCode: s => s,
        mdToHtml: s => s.replace(/\\n/g, '<br>')
    }};

    const funcNames = [
        '_esc', '_cleanNoiseAndTags', '_normalizeStreamBoundaries',
        '_formatToolDeclarations', '_dedupeStatements', '_isSimilarStatement',
        '_formatTabularData', '_renderFindingCard', '_renderReviewCard',
        '_renderDeliverableCard', '_renderPlanCard', '_renderToolCallsCard',
        '_renderJsonTelemetryCard', 'safeParseJson', 'extractJsonBlocks', '_ghostMd'
    ];

    let extractedCode = '';
    funcNames.forEach(fn => {{
        const regex = new RegExp('(function\\\\s+' + fn + '\\\\b[\\\\s\\\\S]*?^}})', 'm');
        const m = jsContent.match(regex);
        if (m) extractedCode += m[1] + '\\n\\n';
    }});

    eval(extractedCode);

    const rawDump = {json.dumps(raw_user_dump)};
    const sculpted = _ghostMd(rawDump, 'implementer');

    if (sculpted.includes('DSML')) throw new Error('DSML leaked in final HTML');
    if (sculpted.includes('<｜｜') || sculpted.includes('</｜｜')) throw new Error('Fullwidth delimiter leaked in HTML');
    if (!sculpted.includes('ghost-telemetry-deliverable')) throw new Error('Deliverable card missing');
    if (!sculpted.includes('ghost-telemetry-review')) throw new Error('Review card missing');
    if (!sculpted.includes('ghost-telemetry-plan')) throw new Error('Plan card missing');
    if (!sculpted.includes('ghost-tool-plan-deck')) throw new Error('Tool declaration deck missing');

    console.log('VERBATIM_DUMP_VALIDATED_SUCCESSFULLY');
    """

    proc = subprocess.run(["node"], input=node_validator, capture_output=True, text=True, encoding="utf-8", check=True)
    assert "VERBATIM_DUMP_VALIDATED_SUCCESSFULLY" in proc.stdout


def test_mobile_web_app_meta_tags_present():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert '<meta name="mobile-web-app-capable" content="yes">' in html
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html


def test_all_password_inputs_enclosed_in_forms():
    from bs4 import BeautifulSoup
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    pw_inputs = soup.find_all("input", {"type": "password"})
    assert len(pw_inputs) >= 7
    uncontained = [inp.get("id") or "unnamed" for inp in pw_inputs if not inp.find_parent("form")]
    assert not uncontained, f"Password inputs not enclosed in forms: {uncontained}"


def test_stream_layout_containment_and_memoization():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    js = (ROOT / "static" / "js" / "council" / "council.js").read_text(encoding="utf-8")

    # CSS layout containment for performance and reflow isolation
    editor_stream = css.split(".ghost-editor-stream {", 1)[1].split("}", 1)[0]
    assert "contain: layout;" in editor_stream

    stream_ledger = css.split(".ghost-stream-ledger {", 1)[1].split("}", 1)[0]
    assert "contain: layout;" in stream_ledger

    cockpit_content = css.split(".ghost-editor-stream .active-cockpit-content {", 1)[1].split("}", 1)[0]
    assert "contain: layout;" in cockpit_content

    # Memoization in _ghostMd
    assert "_ghostMd._cacheKey" in js
    assert "_ghostMd._cacheVal" in js



