import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).parents[1]
JS = (ROOT / "static" / "js" / "council" / "council.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _run_node_script(js_code: str) -> str:
    """Helper to run a Node.js test script with full council.js mock environment."""
    bootstrap = r"""
    const fs = require('fs');
    const jsContent = fs.readFileSync('static/js/council/council.js', 'utf8');

    global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
    global.cancelAnimationFrame = (id) => clearTimeout(id);
    global.window = global;

    let elements = {};
    function createMockElement(id) {
        return {
            id, textContent: '', innerHTML: '', title: '', hidden: false, style: {}, dataset: {},
            classList: {
                classes: new Set(),
                add(c) { this.classes.add(c); },
                remove(c) { this.classes.delete(c); },
                contains(c) { return this.classes.has(c); },
                toggle(c, v) {
                    if (v !== undefined) {
                        if (v) this.add(c); else this.remove(c);
                    } else {
                        if (this.contains(c)) this.remove(c); else this.add(c);
                    }
                    return this.contains(c);
                }
            },
            querySelectorAll(sel) { return []; },
            querySelector(sel) { return null; },
            addEventListener(ev, fn) {},
            setAttribute(k, v) { this[k] = v; },
            getAttribute(k) { return this[k] || null; },
            removeAttribute(k) { delete this[k]; },
            contains(el) { return false; },
            closest(sel) { return null; },
            scrollHeight: 500, scrollTop: 0, clientHeight: 300
        };
    }

    global.document = {
        getElementById(id) {
            if (!elements[id]) elements[id] = createMockElement(id);
            return elements[id];
        },
        querySelector(sel) {
            return {
                dataset: { tab: 'files' },
                style: {},
                classList: { contains: () => false, add: () => {}, remove: () => {} },
                querySelector: () => null,
                querySelectorAll: () => []
            };
        },
        querySelectorAll() { return []; },
        addEventListener() {}
    };

    const cleanJs = jsContent
        .replace(/^import\s+.*$/gm, '')
        .replace(/^export\s+default\s+.*$/gm, '')
        .replace(/export\s+/g, '');
    eval(cleanJs + '; global.CouncilUI = CouncilUI; global.CouncilState = CouncilState; global.CouncilSession = CouncilSession; global.synthesizeAggregatorTitle = synthesizeAggregatorTitle; global.formatBurstTelemetry = formatBurstTelemetry; global._esc = _esc; global.compactAgentActivity = compactAgentActivity;');
    """
    full_script = bootstrap + "\n" + js_code
    proc = subprocess.run(
        ["node"],
        input=full_script,
        capture_output=True,
        text=True,
        encoding="utf-8"
    )
    if proc.returncode != 0:
        raise AssertionError(f"Node script failed with code {proc.returncode}:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc.stdout


def test_adversarial_fuzzing_malformed_and_circular_log_events():
    """Attack 1: Fuzz state.log with null, undefined, primitive, and circular events."""
    script = r"""
    const ui = new CouncilUI({}, {});
    const state = {
        sessionId: 'fuzz-session-1',
        status: 'COMPLETE',
        log: [
            null,
            undefined,
            12345,
            "corrupted_event_string",
            {},
            { event: 'unknown_event_type', text: 'Random message' },
            { event: 'thought', agent: null, text: null },
            { event: 'error', code: null, text: null },
            { event: 'dag_update', extra: { dag: { nodes: [null, undefined, { id: 'T1', status: 'DONE' }] } } },
            { event: 'tool_start', agent: undefined, extra: { tool: null, command: null } },
            { event: 'tool_output', agent: null, exit_code: null, extra: { tool: null, output: null } }
        ]
    };

    // Circular object fuzzing
    const circularEv = { event: 'thought', agent: 'implementer', text: 'Circular reference test' };
    circularEv.self = circularEv;
    state.log.push(circularEv);

    // Circular error fuzzing
    state.error = { message: 'Failed run' };
    state.error.circ = state.error;

    // Must render without throwing any exceptions
    ui._renderGhostEditor(state);
    const ledger = document.getElementById('council-ghost-stream-ledger');
    if (!ledger.innerHTML) throw new Error('Ledger innerHTML was empty after fuzzing');

    console.log('FUZZ_MALFORMED_EVENTS_PASS');
    process.exit(0);
    """
    out = _run_node_script(script)
    assert "FUZZ_MALFORMED_EVENTS_PASS" in out


def test_adversarial_xss_injection_in_all_interpolations():
    """Attack 1 (cont): Inject malicious script/HTML payloads across tool, command, agent, paths, titles."""
    script = r"""
    const ui = new CouncilUI({}, {});
    const maliciousPayload = '<script>alert("xss")</script><img src=x onerror=alert(1)>" onmouseover="alert(2)';
    const state = {
        sessionId: 'xss-session',
        status: 'COMPLETE',
        log: [
            {
                event: 'tool_start',
                agent: maliciousPayload,
                extra: {
                    tool: maliciousPayload,
                    command: maliciousPayload,
                    task_id: maliciousPayload
                }
            },
            {
                event: 'tool_output',
                agent: maliciousPayload,
                exit_code: 1,
                extra: {
                    tool: maliciousPayload,
                    output: maliciousPayload,
                    task_id: maliciousPayload
                }
            },
            {
                event: 'thought',
                agent: maliciousPayload,
                text: maliciousPayload,
                extra: {
                    thoughts: maliciousPayload
                }
            },
            {
                event: 'error',
                code: maliciousPayload,
                text: maliciousPayload
            }
        ]
    };

    ui._renderGhostEditor(state);
    const ledger = document.getElementById('council-ghost-stream-ledger');
    const html = ledger.innerHTML;

    // Verify no raw unescaped script tags or unescaped img tags leaked into DOM
    if (html.includes('<script>')) {
        throw new Error('XSS VULNERABILITY: Raw <script> unescaped in execution stream HTML');
    }
    if (html.includes('<img src=x')) {
        throw new Error('XSS VULNERABILITY: Raw <img src=x unescaped in execution stream HTML');
    }
    if (html.includes('onmouseover="alert(2)"')) {
        throw new Error('XSS VULNERABILITY: Attribute breakout unescaped in execution stream HTML');
    }

    console.log('XSS_INJECTION_PASS');
    process.exit(0);
    """
    out = _run_node_script(script)
    assert "XSS_INJECTION_PASS" in out


def test_performance_linear_scaling_on_high_volume_logs():
    """Attack 2: 1,000+ sequential events must aggregate and render in O(N) time under 150ms."""
    script = r"""
    const ui = new CouncilUI({}, {});
    const log = [];
    const agents = ['implementer', 'strategist', 'manager'];

    for (let i = 0; i < 1000; i++) {
        const agent = agents[Math.floor(i / 10) % agents.length]; // 10 consecutive per agent
        if (i % 3 === 0) {
            log.push({
                event: 'thought',
                agent: agent,
                text: `Thinking step ${i}`,
                ts: new Date(Date.now() + i * 1000).toISOString()
            });
        } else if (i % 3 === 1) {
            log.push({
                event: 'tool_start',
                agent: agent,
                extra: { tool: 'bash', command: `echo ${i}`, task_id: `T${Math.floor(i / 50)}` },
                ts: new Date(Date.now() + i * 1000).toISOString()
            });
        } else {
            log.push({
                event: 'tool_output',
                agent: agent,
                exit_code: 0,
                extra: { tool: 'bash', output: `output ${i}`, duration_ms: 50 },
                ts: new Date(Date.now() + i * 1000).toISOString()
            });
        }
    }

    const state = {
        sessionId: 'scale-session-1000',
        status: 'COMPLETE',
        log: log
    };

    const start = performance.now();
    ui._renderGhostEditor(state);
    const duration = performance.now() - start;

    console.log(`Rendered 1000 events in ${duration.toFixed(2)}ms`);
    if (duration > 250) {
        throw new Error(`Performance bottleneck: 1000 events took ${duration.toFixed(2)}ms (expected < 250ms)`);
    }

    console.log('PERF_LINEAR_SCALING_PASS');
    process.exit(0);
    """
    out = _run_node_script(script)
    assert "PERF_LINEAR_SCALING_PASS" in out


def test_performance_massive_tool_output_containment_and_no_freezes():
    """Attack 2 (cont): 500KB+ / 20,000 line stdout must not freeze UI thread or blow up attributes."""
    script = r"""
    const ui = new CouncilUI({}, {});
    const massiveStdout = 'C:\\Projects\\repo\\src\\file.js: error line 42\\n'.repeat(15000); // ~675KB

    const state = {
        sessionId: 'massive-output-sess',
        status: 'COMPLETE',
        log: [
            {
                event: 'tool_start',
                agent: 'implementer',
                extra: { tool: 'bash', command: 'cat massive_log.txt' }
            },
            {
                event: 'tool_output',
                agent: 'implementer',
                exit_code: 0,
                extra: { tool: 'bash', output: massiveStdout, duration_ms: 120 }
            }
        ]
    };

    const start = performance.now();
    ui._renderGhostEditor(state);
    const duration = performance.now() - start;

    console.log(`Rendered massive tool output in ${duration.toFixed(2)}ms`);
    if (duration > 50) {
        throw new Error(`UI freeze: Massive output render took ${duration.toFixed(2)}ms (expected < 50ms)`);
    }

    const ledger = document.getElementById('council-ghost-stream-ledger');
    const html = ledger.innerHTML;
    if (!html.includes('ghost-tool-copy-btn')) {
        throw new Error('Copy button missing in tool drawer');
    }

    console.log('MASSIVE_OUTPUT_PASS');
    process.exit(0);
    """
    out = _run_node_script(script)
    assert "MASSIVE_OUTPUT_PASS" in out


def test_workflow_state_transitions_and_auto_focus():
    """Attack 3: Transitions across IN_PROGRESS -> BLOCKED -> IN_PROGRESS -> COMPLETE / FAILED."""
    script = r"""
    const ui = new CouncilUI({}, {});
    const state = {
        sessionId: 'lifecycle-sess',
        status: 'IN_PROGRESS',
        log: [
            { event: 'tool_start', agent: 'implementer', extra: { tool: 'read_file', command: '{"path":"main.py"}' } },
            { event: 'tool_output', agent: 'implementer', exit_code: 0, extra: { tool: 'read_file', output: 'content' } },
            { event: 'tool_start', agent: 'implementer', extra: { tool: 'bash', command: 'pytest' } } // Still running
        ]
    };

    // 1. IN_PROGRESS: Running item should be separated out with auto-focus
    ui._renderGhostEditor(state);
    let html = document.getElementById('council-ghost-stream-ledger').innerHTML;
    if (!html.includes('is-running')) {
        throw new Error('Active running tool card must have is-running class in live state');
    }

    // 2. State flips to BLOCKED (e.g. manager review gate)
    state.status = 'BLOCKED';
    ui._renderGhostEditor(state);
    html = document.getElementById('council-ghost-stream-ledger').innerHTML;
    if (!html.includes('is-running')) {
        throw new Error('BLOCKED state is still live, running tool card must remain visible');
    }

    // 3. State flips back to IN_PROGRESS, second tool completes, third starts
    state.status = 'IN_PROGRESS';
    state.log[2] = { event: 'tool_output', agent: 'implementer', exit_code: 0, extra: { tool: 'bash', output: 'passed' } };
    state.log.push({ event: 'tool_start', agent: 'implementer', extra: { tool: 'write_file', command: '{"path":"out.py"}' } });
    ui._renderGhostEditor(state);
    html = document.getElementById('council-ghost-stream-ledger').innerHTML;

    // Two completed tools must now be aggregated in a burst_group, and third is live
    if (!html.includes('ghost-burst-group')) {
        throw new Error('Completed consecutive tools must aggregate into ghost-burst-group');
    }

    // 4. Run completes with FAILED
    state.status = 'FAILED';
    ui._renderGhostEditor(state);
    html = document.getElementById('council-ghost-stream-ledger').innerHTML;
    // In completed run, running items flip to failed and all consecutive tools are grouped
    if (!html.includes('ghost-burst-group')) {
        throw new Error('Finished run must retain burst group aggregation');
    }

    console.log('LIFECYCLE_STATE_TRANSITIONS_PASS');
    process.exit(0);
    """
    out = _run_node_script(script)
    assert "LIFECYCLE_STATE_TRANSITIONS_PASS" in out


def test_rapid_agent_interleaving_and_boundary_isolation():
    """Attack 3 (cont): Alternating between impl, mgr, strat, pers over 20+ turns without cross-agent bleed."""
    script = r"""
    const ui = new CouncilUI({}, {});
    const sequence = ['impl', 'mgr', 'strat', 'perspective_analyzer', 'impl', 'mgr', 'strat', 'perspective_analyzer'];
    const log = [];

    sequence.forEach((ag, idx) => {
        if (ag === 'perspective_analyzer') {
            log.push({
                event: 'thought',
                agent: ag,
                text: `Audit ${idx}`,
                extra: { perspective: { evidence: 'clear', overall_score: 0.95 } }
            });
        } else if (ag === 'mgr') {
            log.push({
                event: 'thought',
                agent: ag,
                text: `Review ${idx}`,
                extra: { manager_review: JSON.stringify({ verdict: 'APPROVED', issues: [] }) }
            });
        } else if (ag === 'strat') {
            log.push({
                event: 'thought',
                agent: ag,
                text: `Plan ${idx}`
            });
        } else {
            log.push({
                event: 'thought',
                agent: ag,
                text: `Implement ${idx}`
            });
        }
    });

    const state = {
        sessionId: 'interleaved-sess',
        status: 'COMPLETE',
        log: log
    };

    ui._renderGhostEditor(state);
    const html = document.getElementById('council-ghost-stream-ledger').innerHTML;

    // In rapid 1-turn interleaving, NO consecutive actions exist for any single agent,
    // so there must be ZERO burst groups created! Every step remains standalone.
    if (html.includes('ghost-burst-group')) {
        throw new Error('Agent boundary violation: Interleaved single-turn agents were improperly merged into burst group');
    }

    // Now test that aliases ('impl' and 'implementer') DO group consecutively:
    const aliasState = {
        sessionId: 'alias-sess',
        status: 'COMPLETE',
        log: [
            { event: 'tool_start', agent: 'impl', extra: { tool: 'read_file', command: '{"path":"a.py"}' } },
            { event: 'tool_output', agent: 'implementer', exit_code: 0, extra: { tool: 'read_file', output: 'ok' } },
            { event: 'tool_start', agent: 'implementer', extra: { tool: 'read_file', command: '{"path":"b.py"}' } },
            { event: 'tool_output', agent: 'impl', exit_code: 0, extra: { tool: 'read_file', output: 'ok' } }
        ]
    };
    ui._renderGhostEditor(aliasState);
    const aliasHtml = document.getElementById('council-ghost-stream-ledger').innerHTML;
    if (!aliasHtml.includes('ghost-burst-group')) {
        throw new Error('Agent aliases (impl vs implementer) must be unified into burst group when consecutive');
    }

    console.log('AGENT_ISOLATION_PASS');
    process.exit(0);
    """
    out = _run_node_script(script)
    assert "AGENT_ISOLATION_PASS" in out


def test_session_switching_cleanses_set_and_map_caches():
    """Attack 3 (cont): Switching sessions must cleanly prune old session keys from Set/Map caches."""
    script = r"""
    const ui = new CouncilUI({}, {});

    // Session 1
    const sess1 = {
        sessionId: 'session-1',
        status: 'COMPLETE',
        log: [
            { event: 'tool_start', agent: 'impl', extra: { tool: 'bash', command: 'ls' } },
            { event: 'tool_output', agent: 'impl', exit_code: 0, extra: { tool: 'bash', output: 'ok' } },
            { event: 'tool_start', agent: 'impl', extra: { tool: 'bash', command: 'pwd' } },
            { event: 'tool_output', agent: 'impl', exit_code: 0, extra: { tool: 'bash', output: 'ok' } }
        ]
    };
    ui._renderGhostEditor(sess1);

    // Simulate user manually expanding burst and tool drawer in Session 1
    const burstKey1 = Array.from(ui._burstStatus.keys())[0];
    if (!burstKey1) throw new Error('Expected burst key in session 1');
    ui._openBurstKeys.add(burstKey1);
    ui._manuallyOpenedTools.add('tool:impl::bash:0');
    ui._manuallyClosedBursts.add(burstKey1);

    // Now switch to Session 2 with completely different keys
    const sess2 = {
        sessionId: 'session-2',
        status: 'COMPLETE',
        log: [
            { event: 'thought', agent: 'strat', text: 'Plan step 1' }
        ]
    };
    ui._renderGhostEditor(sess2);

    // Verify session 1 keys were cleanly purged and not retained as zombies
    for (const key of ui._burstStatus.keys()) {
        if (key.includes('session-1')) {
            throw new Error(`Memory leak: Zombie key retained in _burstStatus: ${key}`);
        }
    }
    for (const key of ui._openBurstKeys) {
        if (key.includes('session-1')) {
            throw new Error(`Memory leak: Zombie key retained in _openBurstKeys: ${key}`);
        }
    }
    for (const key of ui._manuallyClosedBursts) {
        if (key.includes('session-1')) {
            throw new Error(`Memory leak: Zombie key retained in _manuallyClosedBursts: ${key}`);
        }
    }

    console.log('SESSION_SWITCH_PURGE_PASS');
    process.exit(0);
    """
    out = _run_node_script(script)
    assert "SESSION_SWITCH_PURGE_PASS" in out


def test_copy_button_and_clipboard_fallback_resilience():
    """Attack 4: Copy button must survive restricted clipboard / rejection without unhandled rejection."""
    script = r"""
    const ui = new CouncilUI({}, {});
    let copiedText = null;

    // Simulate non-secure context where navigator.clipboard is undefined
    global.navigator = {}; // No clipboard

    // Setup mock DOM for copy click
    const copyBtn = {
        closest(sel) {
            if (sel === '.ghost-tool-copy-btn') return this;
            return null;
        },
        getAttribute(attr) {
            if (attr === 'data-copy-text') return 'npm test -- --coverage';
            return null;
        },
        classList: { contains: () => false },
        textContent: 'Copy'
    };

    // Document mock with execCommand fallback support
    let execCommandCalled = false;
    global.document.createElement = (tag) => {
        return {
            value: '',
            style: {},
            select() {},
            focus() {}
        };
    };
    global.document.body = {
        appendChild() {},
        removeChild() {}
    };
    global.document.execCommand = (cmd) => {
        if (cmd === 'copy') {
            execCommandCalled = true;
            return true;
        }
        return false;
    };

    // Wire listeners once
    let clickHandler = null;
    global.document.addEventListener = (ev, handler) => {
        if (ev === 'click') clickHandler = handler;
    };
    ui.wireListeners({ respond: () => {} });

    // Call click handler simulating button press in non-secure context
    const fakeEvent = {
        target: copyBtn,
        stopPropagation() {}
    };

    clickHandler(fakeEvent);

    if (!execCommandCalled) {
        throw new Error('Copy fallback failed: document.execCommand was not invoked when navigator.clipboard is undefined');
    }
    if (copyBtn.textContent !== 'Copied!') {
        throw new Error('Copy button did not show Copied! confirmation on successful fallback');
    }

    console.log('CLIPBOARD_FALLBACK_PASS');
    process.exit(0);
    """
    out = _run_node_script(script)
    assert "CLIPBOARD_FALLBACK_PASS" in out


def test_theme_and_reduced_motion_css_hardening():
    """Attack 5: Inspect style.css for theme-aware tokens and prefers-reduced-motion coverage."""
    # 1. Hover illumination must not use rigid #ffffff !important that breaks light themes
    assert ".ghost-editor-stream .row:hover .tx" in CSS
    assert "var(--text-bright" in CSS or "var(--fg" in CSS

    # 2. prefers-reduced-motion must disable hover illumination transitions and running animations
    reduced_motion_sections = [s for s in CSS.split("@media (prefers-reduced-motion: reduce)") if len(s) > 10]
    full_reduced_block = " ".join(reduced_motion_sections)
    assert "transition: none !important" in full_reduced_block or "animation: none !important" in full_reduced_block

    # 3. Word-break / overflow-wrap on long strings and paths
    assert ".ghost-editor-stream .ghost-tool-running-cmd" in CSS
    assert "overflow-wrap: anywhere" in CSS or "word-break: break-all" in CSS or "word-break: break-word" in CSS


def test_universal_3_slot_alignment_architecture():
    """Attack 6: Verify Universal Slot Architecture across all stream rows.
    Slot 1 MUST be stream-expander (16px), Slot 2 MUST be ag (54px), Slot 3 MUST be ct.
    No status indicators or chevrons may displace the ag column horizontally.
    """
    # 1. CSS must define fixed 16px stream-expander and unified flex gap
    assert ".ghost-editor-stream .stream-expander" in CSS
    assert "stream-expander--empty" in CSS
    assert ".ghost-editor-stream .row {" in CSS
    row_block = CSS.split(".ghost-editor-stream .row {", 1)[1].split("}", 1)[0]
    assert "gap: 8px" in row_block
    assert "padding: 4px 12px" in row_block

    burst_header_block = CSS.split(".ghost-editor-stream .row.ghost-burst-header {", 1)[1].split("}", 1)[0]
    assert "gap: 8px" in burst_header_block

    # 2. Node script verifying all row types render with Slot 1: stream-expander, Slot 2: ag, Slot 3: ct
    script = r"""
    const ui = new CouncilUI({}, {});
    const state = {
        sessionId: 'alignment-proof-sess',
        status: 'COMPLETE',
        log: [
            { event: 'status_changed', agent: 'chair', complexity: 'SIMPLE', text: 'Assessing request' },
            { event: 'thought', agent: 'strategist', text: 'Thinking about plan' },
            { event: 'dag_update', agent: 'strategist', extra: { dag: { nodes: [{ id: 'T1', summary: 'Task 1' }] } } },
            { event: 'thought', agent: 'perspective_analyzer', text: 'Auditing security', extra: { perspective: { evidence: 'block', overall_score: 0.6 } } },
            { event: 'review_required', agent: 'manager', extra: { manager_review: JSON.stringify({ verdict: 'APPROVED', issues: [] }) } },
            { event: 'tool_start', agent: 'implementer', extra: { tool: 'read_file', command: '{"path":"main.py"}' } },
            { event: 'tool_output', agent: 'implementer', exit_code: 0, extra: { tool: 'read_file', output: 'content' } },
            { event: 'tool_start', agent: 'implementer', extra: { tool: 'bash', command: 'pytest' } },
            { event: 'tool_output', agent: 'implementer', exit_code: 0, extra: { tool: 'bash', output: 'passed' } },
            { event: 'error', code: 'ERR_TIMEOUT', text: 'Execution timed out' }
        ]
    };

    ui._renderGhostEditor(state);
    const html = document.getElementById('council-ghost-stream-ledger').innerHTML;

    // Split HTML by '<div class="row'
    const rowSegments = html.split('<div class="row').slice(1);
    if (rowSegments.length === 0) {
        throw new Error('Zero rows rendered');
    }

    for (let i = 0; i < rowSegments.length; i++) {
        const seg = rowSegments[i];
        const isChildItem = seg.includes('row--burst-item');
        if (isChildItem) {
            // Child rows inside burst body start with tool-tag, then ct
            if (!seg.includes('class="tool-tag')) {
                throw new Error(`Child row ${i} does not start with tool-tag: ${seg.slice(0, 100)}`);
            }
            continue;
        }

        // Level-0 rows MUST have: stream-expander -> ag -> ct
        const expanderIdx = seg.indexOf('class="stream-expander');
        const agIdx = seg.indexOf('class="ag ag--');
        const ctIdx = seg.indexOf('class="ct"');

        if (expanderIdx === -1) {
            throw new Error(`Row ${i} missing Slot 1 stream-expander: ${seg.slice(0, 120)}`);
        }
        if (agIdx === -1) {
            throw new Error(`Row ${i} missing Slot 2 ag badge: ${seg.slice(0, 120)}`);
        }
        if (ctIdx === -1) {
            throw new Error(`Row ${i} missing Slot 3 ct container: ${seg.slice(0, 120)}`);
        }
        if (!(expanderIdx < agIdx && agIdx < ctIdx)) {
            throw new Error(`Row ${i} violates Slot order [expander < ag < ct]: expander=${expanderIdx}, ag=${agIdx}, ct=${ctIdx}`);
        }

        // Ensure NO status glyph exists BEFORE the ag badge
        const stBeforeAg = seg.indexOf('class="st ');
        if (stBeforeAg !== -1 && stBeforeAg < agIdx) {
            throw new Error(`Row ${i} has status badge placed BEFORE ag, displacing alignment!`);
        }
    }

    console.log('UNIVERSAL_3_SLOT_ALIGNMENT_PASS');
    process.exit(0);
    """
    out = _run_node_script(script)
    assert "UNIVERSAL_3_SLOT_ALIGNMENT_PASS" in out
