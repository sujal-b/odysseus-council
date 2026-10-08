from pathlib import Path
import subprocess

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "js" / "council" / "council.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_html_audit_ledger_header_and_toolbar():
    assert 'id="council-log-header"' in HTML
    assert 'id="council-log-summary"' in HTML
    assert 'id="council-log-toolbar"' in HTML
    assert 'data-filter="ALL"' in HTML
    assert 'data-filter="ERRORS"' in HTML
    assert 'data-filter="COMMANDS"' in HTML
    assert 'id="council-log-search"' in HTML


def test_css_audit_ledger_and_tool_drawer_styles():
    assert ".ctx-tab-badge--warning" in CSS
    assert ".audit-ledger-container" in CSS
    assert ".audit-row" in CSS
    assert ".audit-row--error" in CSS
    assert ".audit-col-time" in CSS
    assert ".audit-col-agent" in CSS
    assert ".audit-col-action" in CSS
    assert ".audit-col-target" in CSS
    assert ".audit-col-status" in CSS
    assert ".audit-btn-peek" in CSS
    assert ".audit-btn-copy" in CSS
    assert ".audit-peek-drawer" in CSS
    assert ".ghost-tool-card.open" in CSS
    assert ".ghost-tool-card.is-running" in CSS
    assert ".tool-duration-badge" in CSS


def test_no_redundant_directive_box_in_log():
    # Directive box must not be emitted inside _renderCaptainsLog
    log_render_method = JS.split("_renderCaptainsLog(state) {", 1)[1].split("_openSkillInDocument(idx) {", 1)[0]
    assert "log-directive-box" not in log_render_method


def test_attention_required_badge_and_summary_node_simulation():
    node_script = r"""
    const fs = require('fs');
    const jsContent = fs.readFileSync('static/js/council/council.js', 'utf8');

    global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
    global.cancelAnimationFrame = (id) => clearTimeout(id);
    global.window = global;

    // Minimal browser mock environment
    let elements = {};
    function mockElement(id) {
        return {
            id,
            textContent: '',
            innerHTML: '',
            title: '',
            hidden: false,
            style: {},
            dataset: {},
            classList: {
                classes: new Set(),
                add(c) { this.classes.add(c); },
                remove(c) { this.classes.delete(c); },
                contains(c) { return this.classes.has(c); },
                toggle(c, v) { if (v !== undefined) { if (v) this.add(c); else this.remove(c); } else { if (this.contains(c)) this.remove(c); else this.add(c); } }
            },
            querySelectorAll() { return []; },
            querySelector() { return null; },
            addEventListener() {},
            scrollHeight: 500,
            scrollTop: 0,
            clientHeight: 500
        };
    }

    global.document = {
        getElementById(id) {
            if (!elements[id]) elements[id] = mockElement(id);
            return elements[id];
        },
        querySelector(sel) {
            return { dataset: { tab: 'files' }, style: {}, classList: { contains: () => false, add: () => {}, remove: () => {} } };
        },
        querySelectorAll() { return []; }
    };

    // Extract CouncilUI
    const cleanJs = jsContent
        .replace(/^import\s+.*$/gm, '')
        .replace(/^export\s+default\s+.*$/gm, '')
        .replace(/export\s+/g, '');
    eval(cleanJs + '; global.CouncilUI = CouncilUI;');

    const ui = new CouncilUI({}, {});

    // State with 2 errors and total 5 events
    const stateWithErrors = {
        sessionId: 'sess-123456789012',
        log: [
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:00:00Z', extra: { tool: 'bash', command: 'pytest' } },
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-09T01:00:01Z', exit_code: 1, text: 'AssertionError: test failed', extra: { tool: 'bash', output: 'AssertionError: test failed' } },
            { event: 'plan_created', agent: 'strategist', ts: '2026-10-09T01:00:02Z', extra: { dag: { nodes: [{ id: 'T1' }] } } },
            { event: 'task_status_update', agent: 'implementer', ts: '2026-10-09T01:00:03Z', extra: { task_id: 'T1', task_status: 'FAILED' }, text: 'Task failed' },
            { event: 'complete', status: 'COMPLETE', ts: '2026-10-09T01:00:04Z' }
        ]
    };

    ui._renderCaptainsLog(stateWithErrors);

    const summaryEl = document.getElementById('council-log-summary');
    if (!summaryEl.textContent.includes('5 events') || !summaryEl.textContent.includes('2 errors')) {
        throw new Error('Summary count mismatch: ' + summaryEl.textContent);
    }

    const badgeEl = document.getElementById('ctx-log-badge');
    if (badgeEl.textContent !== '! 2' || !badgeEl.classList.contains('ctx-tab-badge--warning') || badgeEl.hidden) {
        throw new Error('Badge error attention model failed: ' + badgeEl.textContent + ' hidden=' + badgeEl.hidden);
    }

    // State with 0 errors
    const stateClean = {
        sessionId: 'sess-clean',
        log: [
            { event: 'plan_created', agent: 'strategist', ts: '2026-10-09T01:00:00Z', extra: { dag: { nodes: [{ id: 'T1' }] } } },
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:00:01Z', extra: { tool: 'bash', command: 'pytest' } },
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-09T01:00:02Z', exit_code: 0, extra: { tool: 'bash', output: 'passed' } }
        ]
    };

    ui._renderCaptainsLog(stateClean);
    if (badgeEl.classList.contains('ctx-tab-badge--warning')) {
        throw new Error('Clean session should not have warning style on badge');
    }

    console.log('BADGE_AND_SUMMARY_OK');
    process.exit(0);
    """

    proc = subprocess.run(["node"], input=node_script, capture_output=True, text=True, encoding="utf-8", check=True)
    assert "BADGE_AND_SUMMARY_OK" in proc.stdout


def test_audit_ledger_rendering_and_facets():
    node_script = r"""
    const fs = require('fs');
    const jsContent = fs.readFileSync('static/js/council/council.js', 'utf8');

    global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
    global.cancelAnimationFrame = (id) => clearTimeout(id);
    global.window = global;

    let elements = {};
    function mockElement(id) {
        return {
            id,
            textContent: '',
            innerHTML: '',
            title: '',
            hidden: false,
            style: {},
            dataset: {},
            classList: {
                classes: new Set(),
                add(c) { this.classes.add(c); },
                remove(c) { this.classes.delete(c); },
                contains(c) { return this.classes.has(c); },
                toggle(c, v) { if (v !== undefined) { if (v) this.add(c); else this.remove(c); } else { if (this.contains(c)) this.remove(c); else this.add(c); } }
            },
            querySelectorAll() { return []; },
            querySelector() { return null; },
            addEventListener() {},
            scrollHeight: 500,
            scrollTop: 0,
            clientHeight: 500
        };
    }

    global.document = {
        getElementById(id) {
            if (!elements[id]) elements[id] = mockElement(id);
            return elements[id];
        },
        querySelector(sel) {
            return { dataset: { tab: 'files' }, style: {}, classList: { contains: () => false, add: () => {}, remove: () => {} } };
        },
        querySelectorAll() { return []; }
    };

    const cleanJs = jsContent
        .replace(/^import\s+.*$/gm, '')
        .replace(/^export\s+default\s+.*$/gm, '')
        .replace(/export\s+/g, '');
    eval(cleanJs + '; global.CouncilUI = CouncilUI;');
    const ui = new CouncilUI({}, {});

    const state = {
        sessionId: 'sess-audit',
        log: [
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:10:00Z', extra: { tool: 'bash', command: 'pytest tests/' } },
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-09T01:10:01Z', exit_code: 1, text: 'AssertionError: test failed', extra: { tool: 'bash', output: 'pytest failed: 1 error' } },
            { event: 'plan_created', agent: 'strategist', ts: '2026-10-09T01:10:02Z', extra: { dag: { nodes: [{ id: 'T1' }] } } },
            { event: 'code_update', agent: 'implementer', ts: '2026-10-09T01:10:03Z', file_path: 'src/main.py' }
        ]
    };

    // 1. ALL facet
    ui._logFilter = 'ALL';
    ui._renderCaptainsLog(state);
    let html = document.getElementById('council-captains-log').innerHTML;
    if (!html.includes('audit-row') || !html.includes('audit-row--error')) {
        throw new Error('Audit rows missing in ALL render');
    }
    if (!html.includes('[Peek Stderr]') || !html.includes('[Copy Command]')) {
        throw new Error('Error row actions missing: ' + html);
    }
    if (!html.includes('log-file-chip')) {
        throw new Error('File chip missing in code_update row');
    }

    // 2. ERRORS facet
    ui._logFilter = 'ERRORS';
    ui._renderCaptainsLog(state);
    html = document.getElementById('council-captains-log').innerHTML;
    if (!html.includes('audit-row--error') || html.includes('PLAN')) {
        throw new Error('ERRORS facet did not isolate failed rows');
    }

    // 3. COMMANDS facet
    ui._logFilter = 'COMMANDS';
    ui._renderCaptainsLog(state);
    html = document.getElementById('council-captains-log').innerHTML;
    if (!html.includes('pytest') || html.includes('PLAN')) {
        throw new Error('COMMANDS facet did not isolate tool commands');
    }

    // 4. Search query filter
    ui._logFilter = 'ALL';
    ui._logSearchQuery = 'main.py';
    ui._renderCaptainsLog(state);
    html = document.getElementById('council-captains-log').innerHTML;
    if (!html.includes('main.py') || html.includes('pytest')) {
        throw new Error('Search query filter failed');
    }

    console.log('AUDIT_LEDGER_OK');
    process.exit(0);
    """

    proc = subprocess.run(["node"], input=node_script, capture_output=True, text=True, encoding="utf-8", check=True)
    assert "AUDIT_LEDGER_OK" in proc.stdout


def test_live_tool_drawer_auto_collapsing():
    node_script = r"""
    const fs = require('fs');
    const jsContent = fs.readFileSync('static/js/council/council.js', 'utf8');

    global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
    global.cancelAnimationFrame = (id) => clearTimeout(id);
    global.window = global;

    let elements = {};
    function mockElement(id) {
        return {
            id,
            textContent: '',
            innerHTML: '',
            title: '',
            hidden: false,
            style: {},
            dataset: {},
            classList: {
                classes: new Set(),
                add(c) { this.classes.add(c); },
                remove(c) { this.classes.delete(c); },
                contains(c) { return this.classes.has(c); },
                toggle(c, v) { if (v !== undefined) { if (v) this.add(c); else this.remove(c); } else { if (this.contains(c)) this.remove(c); else this.add(c); } }
            },
            querySelectorAll() { return []; },
            querySelector() { return null; },
            addEventListener() {},
            scrollHeight: 500,
            scrollTop: 0,
            clientHeight: 500
        };
    }

    global.document = {
        getElementById(id) {
            if (!elements[id]) elements[id] = mockElement(id);
            return elements[id];
        },
        querySelector(sel) {
            return { dataset: { tab: 'files' }, style: {}, classList: { contains: () => false, add: () => {}, remove: () => {} } };
        },
        querySelectorAll() { return []; }
    };

    const cleanJs = jsContent
        .replace(/^import\s+.*$/gm, '')
        .replace(/^export\s+default\s+.*$/gm, '')
        .replace(/export\s+/g, '');
    eval(cleanJs + '; global.CouncilUI = CouncilUI;');
    const ui = new CouncilUI({}, {});

    // State 1: Running tool
    const stateRunning = {
        sessionId: 'sess-stream',
        status: 'IN_PROGRESS',
        log: [
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:20:00.000Z', extra: { tool: 'bash', command: 'pytest -v' } }
        ]
    };

    ui._renderGhostEditor(stateRunning);
    let ledgerHtml = document.getElementById('council-ghost-stream-ledger').innerHTML;

    // Running tool must be expanded (open) and marked is-running with live indicator
    if (!ledgerHtml.includes('ghost-tool-card open is-running')) {
        throw new Error('Running tool drawer must be expanded open by default: ' + ledgerHtml);
    }
    if (!ledgerHtml.includes('ghost-tool-running-indicator')) {
        throw new Error('Running tool drawer must show running activity indicator');
    }

    // State 2: Tool completes with duration
    const stateFinished = {
        sessionId: 'sess-stream',
        status: 'COMPLETE',
        log: [
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:20:00.000Z', extra: { tool: 'bash', command: 'pytest -v' } },
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-09T01:20:00.420Z', exit_code: 0, extra: { tool: 'bash', output: '5 passed' } }
        ]
    };

    ui._renderGhostEditor(stateFinished);
    ledgerHtml = document.getElementById('council-ghost-stream-ledger').innerHTML;

    // Finished tool drawer must auto-minimize into single-line summary with duration
    if (ledgerHtml.includes('ghost-tool-card open')) {
        throw new Error('Finished tool drawer must auto-minimize (collapse open class): ' + ledgerHtml);
    }
    if (!ledgerHtml.includes('tool-duration-badge') || !ledgerHtml.includes('420ms')) {
        throw new Error('Finished tool drawer must display duration badge (420ms): ' + ledgerHtml);
    }

    // State 3: User manually toggles finished drawer open
    const toolKey = Array.from(ledgerHtml.match(/data-tool-key="([^"]+)"/))[1];
    ui._manuallyOpenedTools.add(toolKey);
    ui._renderGhostEditor(stateFinished);
    ledgerHtml = document.getElementById('council-ghost-stream-ledger').innerHTML;

    if (!ledgerHtml.includes('ghost-tool-card open')) {
        throw new Error('Manually opened tool drawer must stay open across renders: ' + ledgerHtml);
    }

    console.log('LIVE_TOOL_DRAWER_OK');
    process.exit(0);
    """

    proc = subprocess.run(["node"], input=node_script, capture_output=True, text=True, encoding="utf-8", check=True)
    assert "LIVE_TOOL_DRAWER_OK" in proc.stdout


def test_unread_badge_accuracy_across_tabs_and_reset_cleanup():
    node_script = r"""
    const fs = require('fs');
    const jsContent = fs.readFileSync('static/js/council/council.js', 'utf8');

    global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
    global.cancelAnimationFrame = (id) => clearTimeout(id);
    global.window = global;

    let elements = {};
    let currentActiveTab = 'files';
    function mockElement(id) {
        return {
            id, textContent: '', innerHTML: '', title: '', hidden: false, style: {}, dataset: {},
            classList: {
                classes: new Set(),
                add(c) { this.classes.add(c); },
                remove(c) { this.classes.delete(c); },
                contains(c) { return this.classes.has(c); },
                toggle(c, v) { if (v !== undefined) { if (v) this.add(c); else this.remove(c); } else { if (this.contains(c)) this.remove(c); else this.add(c); } }
            },
            querySelectorAll() { return []; },
            querySelector() { return null; },
            addEventListener() {},
            setAttribute() {},
            getAttribute() { return null; },
            removeAttribute() {},
            scrollHeight: 500, scrollTop: 0, clientHeight: 500
        };
    }

    global.document = {
        getElementById(id) {
            if (!elements[id]) elements[id] = mockElement(id);
            return elements[id];
        },
        querySelector(sel) {
            if (sel === '.council-ctx-tab.active') {
                return { dataset: { tab: currentActiveTab }, classList: { add() {}, remove() {}, contains: () => false } };
            }
            return { dataset: { tab: 'files' }, style: {}, classList: { contains: () => false, add: () => {}, remove: () => {} } };
        },
        querySelectorAll() { return []; }
    };

    const cleanJs = jsContent
        .replace(/^import\s+.*$/gm, '')
        .replace(/^export\s+default\s+.*$/gm, '')
        .replace(/export\s+/g, '');
    eval(cleanJs + '; global.CouncilUI = CouncilUI;');
    const ui = new CouncilUI({}, {});

    // 1. Initial render with 1 completed phase (clean), user is on 'files' tab
    const stateClean1 = {
        sessionId: 'sess-run-1',
        log: [
            { event: 'plan_created', agent: 'strategist', ts: '2026-10-09T01:00:00Z', extra: { dag: { nodes: [{ id: 'T1' }] } } }
        ]
    };
    ui._renderCaptainsLog(stateClean1);
    const badge = document.getElementById('ctx-log-badge');

    // Badge must be visible with unread count 1 immediately on the very first render (not lagging)
    if (badge.hidden || badge.textContent !== '1') {
        throw new Error('Initial clean render must show unread badge 1 immediately, got: hidden=' + badge.hidden + ' text=' + badge.textContent);
    }

    // 2. User switches to 'log' tab
    currentActiveTab = 'log';
    ui._activateCtxTab('log');
    ui._renderCaptainsLog(stateClean1);
    if (!badge.hidden) {
        throw new Error('Badge must be hidden while active on log tab');
    }

    // 3. User switches back to 'files' tab without new events
    currentActiveTab = 'files';
    ui._activateCtxTab('files');
    ui._renderCaptainsLog(stateClean1);
    if (!badge.hidden) {
        throw new Error('Badge must remain hidden when returning to files if no new phases arrived');
    }

    // 4. A 2nd phase arrives while on 'files' tab
    const stateClean2 = {
        sessionId: 'sess-run-1',
        log: [
            ...stateClean1.log,
            { event: 'complete', status: 'COMPLETE', ts: '2026-10-09T01:00:05Z' }
        ]
    };
    ui._renderCaptainsLog(stateClean2);
    if (badge.hidden || badge.textContent !== '1') {
        throw new Error('Badge must show exactly 1 new unread phase, got: ' + badge.textContent);
    }

    // 5. An error arrives
    const stateWithError = {
        sessionId: 'sess-run-1',
        log: [
            ...stateClean2.log,
            { event: 'error', text: 'Fatal runtime failure', ts: '2026-10-09T01:00:10Z' }
        ]
    };
    ui._renderCaptainsLog(stateWithError);
    if (badge.hidden || badge.textContent !== '! 1' || !badge.classList.contains('ctx-tab-badge--warning')) {
        throw new Error('Badge must prioritize error attention with warning style: ' + badge.textContent);
    }

    // 6. User calls ui.reset() for next session
    ui._manuallyOpenedTools.add('tool:foo');
    ui._peekDrawers.add('aud-1');
    ui._logSearchQuery = 'foo';
    ui.reset();

    if (ui._manuallyOpenedTools.size !== 0 || ui._peekDrawers.size !== 0 || ui._logSearchQuery !== '') {
        throw new Error('ui.reset() failed to clear tracking sets and search query');
    }
    if (ui._currentPhaseCount !== 0 || ui._lastViewedPhaseCount !== 0) {
        throw new Error('ui.reset() failed to reset phase counters: curr=' + ui._currentPhaseCount + ' last=' + ui._lastViewedPhaseCount);
    }
    if (!badge.hidden || badge.textContent !== '') {
        throw new Error('ui.reset() failed to hide and clear badge');
    }

    // 7. Verify subsequent session 2 correctly displays unread badge without stale count suppression
    const stateSession2 = {
        sessionId: 'sess-run-2',
        log: [
            { event: 'plan_created', agent: 'strategist', ts: '2026-10-09T02:00:00Z', extra: { dag: { nodes: [{ id: 'T2' }] } } }
        ]
    };
    ui._renderCaptainsLog(stateSession2);
    if (badge.hidden || badge.textContent !== '1') {
        throw new Error('Session 2 must show unread badge 1 after reset, got: hidden=' + badge.hidden + ' text=' + badge.textContent);
    }

    console.log('UNREAD_AND_RESET_OK');
    process.exit(0);
    """

    proc = subprocess.run(["node"], input=node_script, capture_output=True, text=True, encoding="utf-8", check=True)
    assert "UNREAD_AND_RESET_OK" in proc.stdout


def test_multi_tool_stream_live_accordion_and_burst_resilience():
    node_script = r"""
    const fs = require('fs');
    const jsContent = fs.readFileSync('static/js/council/council.js', 'utf8');

    global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
    global.cancelAnimationFrame = (id) => clearTimeout(id);
    global.window = global;

    let elements = {};
    function mockElement(id) {
        return {
            id, textContent: '', innerHTML: '', title: '', hidden: false, style: {}, dataset: {},
            classList: {
                classes: new Set(),
                add(c) { this.classes.add(c); },
                remove(c) { this.classes.delete(c); },
                contains(c) { return this.classes.has(c); },
                toggle(c, v) {}
            },
            querySelectorAll() { return []; },
            querySelector() { return null; },
            addEventListener() {},
            scrollHeight: 500, scrollTop: 0, clientHeight: 500
        };
    }

    global.document = {
        getElementById(id) {
            if (!elements[id]) elements[id] = mockElement(id);
            return elements[id];
        },
        querySelector(sel) {
            return { dataset: { tab: 'files' }, style: {}, classList: { contains: () => false, add: () => {}, remove: () => {} } };
        },
        querySelectorAll() { return []; }
    };

    const cleanJs = jsContent
        .replace(/^import\s+.*$/gm, '')
        .replace(/^export\s+default\s+.*$/gm, '')
        .replace(/export\s+/g, '');
    eval(cleanJs + '; global.CouncilUI = CouncilUI;');
    const ui = new CouncilUI({}, {});

    // Step 1: Tool 1 starts running
    const state1 = {
        sessionId: 'sess-multi',
        status: 'IN_PROGRESS',
        log: [
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:30:00.000Z', extra: { tool: 'bash', command: 'pytest tests/test_one.py' } }
        ]
    };
    ui._renderGhostEditor(state1);
    let html = document.getElementById('council-ghost-stream-ledger').innerHTML;
    if (!html.includes('ghost-tool-card open is-running') || !html.includes('ghost-tool-running-indicator')) {
        throw new Error('Active running tool 1 must be open with live indicator: ' + html);
    }

    // Step 2: Tool 1 finishes, Tool 2 starts running
    const state2 = {
        sessionId: 'sess-multi',
        status: 'IN_PROGRESS',
        log: [
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:30:00.000Z', extra: { tool: 'bash', command: 'pytest tests/test_one.py' } },
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-09T01:30:00.350Z', exit_code: 0, extra: { tool: 'bash', output: '1 passed', duration_ms: 350 } },
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:30:00.400Z', extra: { tool: 'bash', command: 'pytest tests/test_two.py' } }
        ]
    };
    ui._renderGhostEditor(state2);
    html = document.getElementById('council-ghost-stream-ledger').innerHTML;

    // In Codex / Antigravity style:
    // Tool 1 must auto-minimize to a crisp 1-line summary with 350ms duration badge
    if (!html.includes('test_one.py') || !html.includes('350ms')) {
        throw new Error('Tool 1 must auto-minimize into single-line summary with duration badge');
    }
    // Tool 2 must be open as the live active tool with live running indicator
    if (!html.includes('ghost-tool-card open is-running') || !html.includes('test_two.py')) {
        throw new Error('Tool 2 must be expanded as the live tool drawer, NOT swallowed into closed burst: ' + html);
    }

    // Step 3: Tool 2 completes
    const state3 = {
        sessionId: 'sess-multi',
        status: 'COMPLETE',
        log: [
            ...state2.log,
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-09T01:30:00.800Z', exit_code: 0, extra: { tool: 'bash', output: '2 passed', duration_ms: 400 } }
        ]
    };
    ui._renderGhostEditor(state3);
    html = document.getElementById('council-ghost-stream-ledger').innerHTML;

    // Both tools completed -> no active running indicator
    if (html.includes('ghost-tool-running-indicator')) {
        throw new Error('Completed tools must not display running indicator');
    }

    console.log('MULTI_TOOL_STREAM_OK');
    process.exit(0);
    """

    proc = subprocess.run(["node"], input=node_script, capture_output=True, text=True, encoding="utf-8", check=True)
    assert "MULTI_TOOL_STREAM_OK" in proc.stdout


def test_calm_audit_ledger_hard_stress_test_and_fallback_recovery():
    node_script = r"""
    const fs = require('fs');
    const jsContent = fs.readFileSync('static/js/council/council.js', 'utf8');

    global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
    global.cancelAnimationFrame = (id) => clearTimeout(id);
    global.window = global;

    let elements = {};
    function mockElement(id) {
        return {
            id, textContent: '', innerHTML: '', title: '', hidden: false, style: {}, dataset: {},
            classList: {
                classes: new Set(),
                add(c) { this.classes.add(c); },
                remove(c) { this.classes.delete(c); },
                contains(c) { return this.classes.has(c); },
                toggle(c, v) {}
            },
            querySelectorAll() { return []; },
            querySelector() { return null; },
            addEventListener() {},
            scrollHeight: 500, scrollTop: 0, clientHeight: 500
        };
    }

    global.document = {
        getElementById(id) {
            if (!elements[id]) elements[id] = mockElement(id);
            return elements[id];
        },
        querySelector(sel) {
            return { dataset: { tab: 'files' }, style: {}, classList: { contains: () => false, add: () => {}, remove: () => {} } };
        },
        querySelectorAll() { return []; }
    };

    const cleanJs = jsContent
        .replace(/^import\s+.*$/gm, '')
        .replace(/^export\s+default\s+.*$/gm, '')
        .replace(/export\s+/g, '');
    eval(cleanJs + '; global.CouncilUI = CouncilUI;');
    const ui = new CouncilUI({}, {});

    // Build a 500-event stress test log with malformed and uncertain edge cases
    const stressLog = [];
    stressLog.push(null); // null item
    stressLog.push({ event: 'thought_delta', text: 'ignore me' }); // noise item
    stressLog.push({ event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:00:00Z', extra: { tool: 'bash', command: 'npm test' } });
    // tool_output with string exit_code "1" and extra.error
    stressLog.push({ event: 'tool_output', agent: 'implementer', ts: '2026-10-09T01:00:01Z', exit_code: '1', extra: { tool: 'bash', output: 'SyntaxError: unexpected token', error: 'Command failed' } });
    // error event with no command (stderr only)
    stressLog.push({ event: 'error', text: 'Resource exhausted: out of memory', ts: '2026-10-09T01:00:02Z' });
    // tool_output with string exit_code "0" (must NOT be treated as error!)
    stressLog.push({ event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:00:03Z', extra: { tool: 'bash', command: 'git status' } });
    stressLog.push({ event: 'tool_output', agent: 'implementer', ts: '2026-10-09T01:00:04Z', exit_code: '0', extra: { tool: 'bash', output: 'working tree clean' } });

    // Add 200 normal events
    for (let i = 0; i < 200; i++) {
        stressLog.push({
            event: 'code_update',
            agent: 'implementer',
            ts: '2026-10-09T01:00:05Z',
            file_path: 'src/file_' + i + '.py'
        });
    }

    const stressState = {
        sessionId: 'stress-sess-999',
        status: 'FAILED',
        log: stressLog
    };

    ui._renderCaptainsLog(stressState);

    const summaryEl = document.getElementById('council-log-summary');
    const badgeEl = document.getElementById('ctx-log-badge');

    // 2 errors exist (npm test fail + error event). git status "0" is clean.
    if (!summaryEl.textContent.includes('2 errors')) {
        throw new Error('Summary must reflect exactly 2 errors under stress test, got: ' + summaryEl.textContent);
    }
    if (badgeEl.textContent !== '! 2' || !badgeEl.classList.contains('ctx-tab-badge--warning')) {
        throw new Error('Badge must show ! 2 with warning class under stress test, got: ' + badgeEl.textContent);
    }

    // Verify ERRORS facet matches exactly 2 error rows
    ui._logFilter = 'ERRORS';
    ui._renderCaptainsLog(stressState);
    const html = document.getElementById('council-captains-log').innerHTML;
    const errorRowCount = (html.match(/audit-row--error/g) || []).length;
    if (errorRowCount !== 2) {
        throw new Error('ERRORS facet must isolate exactly 2 error rows, got: ' + errorRowCount);
    }

    // Verify [Copy Command] on tool error vs [Copy Error] on system error
    if (!html.includes('[Copy Command]')) {
        throw new Error('npm test error row must offer [Copy Command]');
    }
    if (!html.includes('[Copy Error]')) {
        throw new Error('Resource exhausted error row without command must offer [Copy Error]');
    }

    console.log('STRESS_TEST_AND_RECOVERY_OK');
    process.exit(0);
    """

    proc = subprocess.run(["node"], input=node_script, capture_output=True, text=True, encoding="utf-8", check=True)
    assert "STRESS_TEST_AND_RECOVERY_OK" in proc.stdout


def test_extreme_uncertain_edge_cases_and_system_wide_recovery():
    node_script = r"""
    const fs = require('fs');
    const jsContent = fs.readFileSync('static/js/council/council.js', 'utf8');

    global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
    global.cancelAnimationFrame = (id) => clearTimeout(id);
    global.window = global;

    let elements = {};
    function mockElement(id) {
        return {
            id, textContent: '', innerHTML: '', title: '', hidden: false, style: {}, dataset: {}, value: '',
            classList: {
                classes: new Set(),
                add(c) { this.classes.add(c); },
                remove(c) { this.classes.delete(c); },
                contains(c) { return this.classes.has(c); },
                toggle(c, v) { if (v !== undefined) { if (v) this.add(c); else this.remove(c); } else { if (this.contains(c)) this.remove(c); else this.add(c); } }
            },
            querySelectorAll() { return []; },
            querySelector() { return null; },
            addEventListener() {},
            setAttribute() {},
            getAttribute() { return null; },
            removeAttribute() {},
            scrollHeight: 500, scrollTop: 0, clientHeight: 500
        };
    }

    global.document = {
        getElementById(id) {
            if (!elements[id]) elements[id] = mockElement(id);
            return elements[id];
        },
        querySelector(sel) {
            return { dataset: { tab: 'files' }, style: {}, classList: { contains: () => false, add: () => {}, remove: () => {} } };
        },
        querySelectorAll() { return []; }
    };

    const cleanJs = jsContent
        .replace(/^import\s+.*$/gm, '')
        .replace(/^export\s+default\s+.*$/gm, '')
        .replace(/export\s+/g, '');
    eval(cleanJs + '; global.CouncilUI = CouncilUI;');
    const ui = new CouncilUI({}, {});

    // 1. Scenario: state.log is completely null / undefined / non-array object
    const nullLogState = { sessionId: 'null-log-sess', status: 'IN_PROGRESS', log: null };
    const undefinedLogState = { sessionId: 'undef-log-sess', status: 'IN_PROGRESS' };
    const objectLogState = { sessionId: 'obj-log-sess', status: 'IN_PROGRESS', log: { invalid: true } };

    // These must NEVER throw uncaught exceptions
    ui._renderGhostEditor(nullLogState);
    ui._renderCaptainsLog(nullLogState);
    if (ui._estimatedTokens(nullLogState) !== 0) throw new Error('Estimated tokens on null log must be 0');

    ui._renderGhostEditor(undefinedLogState);
    ui._renderCaptainsLog(undefinedLogState);
    if (ui._estimatedTokens(undefinedLogState) !== 0) throw new Error('Estimated tokens on undefined log must be 0');

    ui._renderGhostEditor(objectLogState);
    ui._renderCaptainsLog(objectLogState);
    if (ui._estimatedTokens(objectLogState) !== 0) throw new Error('Estimated tokens on object log must be 0');

    // 2. Scenario: Non-string error text (numeric 500, boolean false, JSON object error)
    const nonStringErrorState = {
        sessionId: 'non-string-sess',
        status: 'FAILED',
        log: [
            { event: 'error', text: 500, ts: '2026-10-09T01:00:00Z' },
            { event: 'tool_start', agent: 'implementer', extra: { task_id: 42, tool: 'bash', command: 'run' }, ts: '2026-10-09T01:00:01Z' },
            { event: 'tool_output', agent: 'implementer', exit_code: 1, text: { error: 'disk full', code: 'ENOSPC' }, extra: { task_id: 42, tool: 'bash' }, ts: '2026-10-09T01:00:02Z' }
        ]
    };

    // Must cleanly parse without toLowerCase crashing on numbers/objects
    ui._renderCaptainsLog(nonStringErrorState);
    let logHtml = document.getElementById('council-captains-log').innerHTML;
    if (!logHtml.includes('500') || !logHtml.includes('disk full')) {
        throw new Error('Non-string error text was not safely serialized into audit ledger');
    }

    // 3. Scenario: Search query filter with numeric target/action
    ui._logSearchQuery = '42';
    ui._renderCaptainsLog(nonStringErrorState);
    logHtml = document.getElementById('council-captains-log').innerHTML;
    if (!logHtml.includes('42')) {
        throw new Error('Search filter on numeric field failed');
    }

    // 4. Scenario: Reset clears both search bars
    document.getElementById('council-log-search').value = 'search query';
    document.getElementById('council-log-search-inline').value = 'inline search query';
    ui.reset();
    if (document.getElementById('council-log-search').value !== '' || document.getElementById('council-log-search-inline').value !== '') {
        throw new Error('CouncilUI.reset failed to clear both search inputs');
    }

    // 5. Scenario: Execution stream auto-recovery under intentional corrupt state
    const corruptState = {
        sessionId: 'corrupt-sess',
        status: 'IN_PROGRESS',
        thoughts: 'streaming...',
        log: [{
            get event() { throw new Error('Simulated event property access failure'); }
        }]
    };

    ui._renderGhostEditor(corruptState);
    const ledgerHtml = document.getElementById('council-ghost-stream-ledger').innerHTML;
    if (!ledgerHtml.includes('Execution stream active')) {
        throw new Error('Stream auto-recovery did not engage on corrupt stream payload');
    }

    console.log('EXTREME_EDGE_CASES_AND_RECOVERY_OK');
    process.exit(0);
    """

    proc = subprocess.run(["node"], input=node_script, capture_output=True, text=True, encoding="utf-8", check=True)
    assert "EXTREME_EDGE_CASES_AND_RECOVERY_OK" in proc.stdout

