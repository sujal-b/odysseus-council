from pathlib import Path
import subprocess

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "js" / "council" / "council.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_pipeline_atlas_html_and_css_invariants():
    """Verify HTML and CSS contracts for Pipeline Atlas header, tabs, and spine styling."""
    # 1. Header & Navigation layout
    assert 'class="council-ctx-tabbar"' in HTML
    assert 'class="council-ctx-tabs-group"' in HTML
    assert 'id="ctx-tab-files"' in HTML
    assert 'id="ctx-tab-log"' in HTML
    assert 'id="ctx-tab-debug"' in HTML
    assert 'id="council-ctx-tab-tooltip"' in HTML
    assert 'id="council-log-session-id"' in HTML
    assert 'id="council-log-copy-btn"' in HTML
    assert 'id="council-cancel-btn"' in HTML

    # Ensure tooltip is docked to the right (after the spacer)
    spacer_idx = HTML.index('class="council-ctx-tabbar-spacer"')
    tooltip_idx = HTML.index('id="council-ctx-tab-tooltip"')
    session_group_idx = HTML.index('class="council-ctx-session-group"')
    assert spacer_idx < tooltip_idx < session_group_idx

    # Ensure no fourth tab was added (exactly 3 contextual view tabs)
    tab_buttons = [line for line in HTML.splitlines() if '<button' in line and 'class="council-ctx-tab' in line]
    assert len(tab_buttons) == 3

    # 2. Pipeline Atlas container in Log tab
    assert 'id="ctx-body-log"' in HTML
    assert 'id="council-pipeline-header"' in HTML
    assert 'id="council-pipeline-phase-count"' in HTML
    assert 'id="council-pipeline-summary"' in HTML
    assert 'id="council-pipeline-atlas"' in HTML
    assert 'id="council-pipeline-footer"' in HTML
    assert 'id="council-revert-btn"' in HTML

    # 3. CSS invariants
    # Zero dead-space tabs
    tabbar_block = CSS.split(".council-ctx-tabbar {", 1)[1].split("}", 1)[0]
    assert "gap: 0" in tabbar_block

    tabs_group_block = CSS.split(".council-ctx-tabs-group {", 1)[1].split("}", 1)[0]
    assert "gap: 0" in tabs_group_block

    # Tooltip right alignment styling
    tooltip_block = CSS.split(".council-ctx-tab-tooltip {", 1)[1].split("}", 1)[0]
    assert "margin-right: 12px" in tooltip_block
    assert "white-space: nowrap" in tooltip_block

    # Restrained 2px active indicator
    tab_active_block = CSS.split(".council-ctx-tab.active {", 1)[1].split("}", 1)[0]
    assert "border-bottom-color: var(--council-copper)" in tab_active_block

    # Text shimmer and animation
    assert "@keyframes pipeline-text-shimmer" in CSS
    assert "@keyframes pipeline-spin" in CSS
    assert ".pipeline-agent-name--active" in CSS
    assert ".pipeline-agent-name-text" in CSS
    assert ".pipeline-loader-icon" in CSS
    assert ".pipeline-pause-icon" in CSS
    assert ".pipeline-phase--blocked" in CSS
    assert ".pipeline-phase--failed" in CSS

    # Reduced motion safety
    reduced_motion = CSS.split("@media (prefers-reduced-motion: reduce) {", 1)[1].split("}", 2)[0]
    assert "animation: none" in reduced_motion

    # Restrained failed ops styling (quiet neutral, not alarm red)
    failed_metric_block = CSS.split(".pipeline-metric-failed {", 1)[1].split("}", 1)[0]
    assert "var(--dim" in failed_metric_block or "#888" in failed_metric_block
    assert "#ef4444" not in failed_metric_block
    assert "var(--council-danger)" not in failed_metric_block

    # Connectors and drawers
    assert ".pipeline-spine-connector" in CSS
    assert ".pipeline-spine-connector--active" in CSS
    assert ".pipeline-phase-drawer" in CSS
    assert ".pipeline-task-branch-list" in CSS
    assert ".pipeline-output-item" in CSS
    assert ".pipeline-outputs-overflow-btn" in CSS
    assert ".pipeline-stage-history-link" in CSS
    assert ".stream-block-focus-highlight" in CSS


def test_pipeline_atlas_node_simulation():
    """Verify runtime behavior of Pipeline Atlas: metrics separation, dynamic spine,
    repeated invocations, 4-phase ceiling, auto/user expansion, and 2-output ceiling."""
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
            querySelectorAll(sel) {
                const results = [];
                if (this._children) {
                    this._children.forEach(c => {
                        if (c.matches && c.matches(sel)) results.push(c);
                        if (c.querySelectorAll) results.push(...c.querySelectorAll(sel));
                    });
                }
                return results;
            },
            querySelector(sel) {
                const all = this.querySelectorAll(sel);
                return all.length ? all[0] : null;
            },
            addEventListener(ev, fn) {
                if (!this._listeners) this._listeners = {};
                if (!this._listeners[ev]) this._listeners[ev] = [];
                this._listeners[ev].push(fn);
            },
            setAttribute(k, v) {},
            getAttribute(k) { return null; },
            removeAttribute(k) {},
            dispatch(ev, target) {
                const fns = this._listeners?.[ev] || [];
                fns.forEach(f => f({ target: target || this, preventDefault() {} }));
            },
            scrollIntoViewCalled: false,
            scrollIntoView() { this.scrollIntoViewCalled = true; },
            scrollHeight: 500,
            scrollTop: 0,
            clientHeight: 300
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
    eval(cleanJs + '; global.CouncilUI = CouncilUI; global._derivePipelinePhases = _derivePipelinePhases;');

    const ui = new CouncilUI({}, {});

    // ─────────────────────────────────────────────────────────────
    // TEST 1: Global Pipeline Summary & Metric Separation
    // ─────────────────────────────────────────────────────────────
    const stateComplex = {
        sessionId: 'sess-complex-12345678',
        status: 'IN_PROGRESS',
        activeAgent: 'completeness_auditor',
        route: 'COUNCIL',
        complexity: 'Medium Complexity',
        dag: {
            nodes: [
                { id: 'T1', label: 'Lint & Schema validation', status: 'DONE' },
                { id: 'T2', label: 'Atlas Sidebar Component', status: 'IN_PROGRESS' },
                { id: 'T3', label: 'Theme Tokens', status: 'PENDING' }
            ]
        },
        log: [
            // Phase 1: Chair
            { event: 'status_changed', agent: 'chair', ts: '2026-10-11T10:00:00Z' },
            { event: 'brief_created', agent: 'chair', ts: '2026-10-11T10:00:02Z' },

            // Phase 2: Strategist
            { event: 'plan_created', agent: 'strategist', ts: '2026-10-11T10:00:03Z', extra: { dag: { nodes: [{ id: 'T1' }, { id: 'T2' }, { id: 'T3' }] } } },

            // Phase 3: Implementer (Work Stage 1)
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-11T10:00:10Z', extra: { tool: 'bash', command: 'pytest' } },
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-11T10:00:20Z', exit_code: 1, extra: { error: 'SyntaxError in test' } }, // 1 failed op
            { event: 'code_update', agent: 'implementer', file_path: 'static/js/council/council.js', ts: '2026-10-11T10:00:30Z' },
            { event: 'code_update', agent: 'implementer', file_path: 'static/style.css', ts: '2026-10-11T10:00:35Z' },
            { event: 'code_update', agent: 'implementer', file_path: 'static/index.html', ts: '2026-10-11T10:00:40Z' },
            { event: 'code_update', agent: 'implementer', file_path: 'tests/test_atlas.py', ts: '2026-10-11T10:00:45Z' }, // 4 unique files

            // Phase 4: Manager (Review Round 1 -> Requests revision)
            { event: 'review_required', agent: 'manager', ts: '2026-10-11T10:01:00Z', extra: { review: { verdict: 'REVISE', summary: '1 issue flagged: missing test' } } },

            // Phase 5: Implementer (Revision Fix)
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-11T10:01:10Z', extra: { tool: 'bash', command: 'pytest' } },
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-11T10:01:20Z', exit_code: 0 },
            { event: 'code_update', agent: 'implementer', file_path: 'tests/test_atlas.py', ts: '2026-10-11T10:01:25Z' }
            // Phase 6: Active Completeness Auditor
        ]
    };

    ui._renderCaptainsLog(stateComplex);

    const summaryEl = document.getElementById('council-pipeline-summary');
    const phaseCountEl = document.getElementById('council-pipeline-phase-count');
    const atlasEl = document.getElementById('council-pipeline-atlas');

    // Total phases should be 6
    if (!phaseCountEl.textContent.includes('6 PHASES')) {
        throw new Error('Expected 6 PHASES, got: ' + phaseCountEl.textContent);
    }

    // Summary line should contain files changed, outputs, failed op, and revision cycle
    if (!summaryEl.innerHTML.includes('4 files changed')) {
        throw new Error('Summary missing 4 files changed: ' + summaryEl.innerHTML);
    }
    if (!summaryEl.innerHTML.includes('output')) {
        throw new Error('Summary missing outputs: ' + summaryEl.innerHTML);
    }
    if (!summaryEl.innerHTML.includes('1 failed op') || !summaryEl.innerHTML.includes('pipeline-metric-failed')) {
        throw new Error('Summary missing restrained failed op: ' + summaryEl.innerHTML);
    }
    if (!summaryEl.innerHTML.includes('1 revision cycle')) {
        throw new Error('Summary missing revision cycle: ' + summaryEl.innerHTML);
    }

    // ─────────────────────────────────────────────────────────────
    // TEST 2: Dynamic Organic Spine & Contextual Suffixes
    // ─────────────────────────────────────────────────────────────
    // Earlier work must not be overwritten or collapsed into one:
    // Should have Implementer first run AND Implementer (Revision fix)
    if (!atlasEl.innerHTML.includes('Implementer')) {
        throw new Error('Spine missing Implementer');
    }
    if (!atlasEl.innerHTML.includes('Revision fix')) {
        throw new Error('Spine missing Implementer (Revision fix) contextual suffix');
    }
    if (!atlasEl.innerHTML.includes('Revision requested')) {
        throw new Error('Spine missing Manager (Revision requested) contextual suffix');
    }

    // Active agent visual treatment: Completeness Auditor is active
    if (!atlasEl.innerHTML.includes('pipeline-agent-name--active')) {
        throw new Error('Active agent missing pipeline-agent-name--active shimmer class');
    }
    if (!atlasEl.innerHTML.includes('pipeline-loader-icon')) {
        throw new Error('Active agent missing loader spinner icon');
    }
    if (!atlasEl.innerHTML.includes('Completeness Auditor')) {
        throw new Error('Spine missing active Completeness Auditor');
    }

    // ─────────────────────────────────────────────────────────────
    // TEST 3: 4-Phase Ceiling & Unified Progressive Disclosure
    // ─────────────────────────────────────────────────────────────
    // By default, showing 4 most recent phases out of 6
    if (!atlasEl.innerHTML.includes('Show earlier phases (2)')) {
        throw new Error('Ceiling button missing or incorrect earlier phases count: ' + atlasEl.innerHTML);
    }
    // Phase 0 (Chairperson) should NOT be in the visible spine
    if (atlasEl.innerHTML.includes('data-phase-id="phase-0"')) {
        throw new Error('Phase 0 (Chairperson) should be hidden by 4-phase ceiling');
    }
    // Phase 5 (active) must be in visible spine
    if (!atlasEl.innerHTML.includes('data-phase-id="phase-5"')) {
        throw new Error('Active Phase 5 must be in visible spine');
    }

    // Toggle earlier phases
    ui._pipelineShowAllHistory = true;
    ui._renderCaptainsLog(stateComplex);

    if (!atlasEl.innerHTML.includes('Show recent (4)')) {
        throw new Error('Expanded disclosure toggle must show "Show recent (4)"');
    }
    // Now Phase 0 (Chairperson) MUST be visible
    if (!atlasEl.innerHTML.includes('data-phase-id="phase-0"')) {
        throw new Error('Phase 0 must be revealed when disclosure is expanded');
    }

    // ─────────────────────────────────────────────────────────────
    // TEST 4: Expansion Ownership (Auto vs. User)
    // ─────────────────────────────────────────────────────────────
    // Active phase (phase-5) is auto-expanded
    if (!ui._isPhaseExpanded({ id: 'phase-5' })) {
        throw new Error('Active phase-5 must be auto-expanded');
    }
    // Phase 4 (Implementer fix) should NOT be auto-expanded
    if (ui._isPhaseExpanded({ id: 'phase-4' })) {
        throw new Error('Completed phase-4 must not be auto-expanded');
    }

    // Developer explicitly clicks Phase 1 (Strategist) to inspect DAG
    ui._onPhaseHeaderClick('phase-1');
    if (!ui._userExpandedPhaseIds.has('phase-1')) {
        throw new Error('Phase 1 must be registered in _userExpandedPhaseIds');
    }
    if (!ui._isPhaseExpanded({ id: 'phase-1' })) {
        throw new Error('Phase 1 must be expanded after explicit user click');
    }

    // Simulate active agent handoff to Completeness Auditor completed (run COMPLETE)
    const stateDone = {
        ...stateComplex,
        status: 'COMPLETE',
        activeAgent: null
    };
    ui._renderCaptainsLog(stateDone);

    // After completion, Phase 5 auto-collapses
    if (ui._isPhaseExpanded({ id: 'phase-5' })) {
        throw new Error('Phase 5 must collapse after execution finishes (auto-collapse)');
    }
    // But Phase 1 (user expanded) STAYS OPEN!
    if (!ui._isPhaseExpanded({ id: 'phase-1' })) {
        throw new Error('User-expanded Phase 1 must persist and NEVER collapse automatically');
    }

    // Developer explicitly collapses an active phase
    ui._onPhaseHeaderClick('phase-5'); // opens it
    ui._onPhaseHeaderClick('phase-5'); // collapses it explicitly
    if (!ui._userCollapsedPhaseIds.has('phase-5')) {
        throw new Error('Phase 5 must be registered in _userCollapsedPhaseIds');
    }

    // Re-render as active: user explicit collapse MUST win over auto-expansion
    const stateActiveAgain = { ...stateComplex, activeAgent: 'completeness_auditor', status: 'IN_PROGRESS' };
    ui._renderCaptainsLog(stateActiveAgain);
    if (ui._isPhaseExpanded({ id: 'phase-5' })) {
        throw new Error('User explicit collapse must prevent auto-expansion from forcing drawer open');
    }

    // ─────────────────────────────────────────────────────────────
    // TEST 5: Stage Output Preview Ceiling (At Most 2 Previews)
    // ─────────────────────────────────────────────────────────────
    // Phase 2 (Implementer first run) produced 4 files (council.js, style.css, index.html, test_atlas.py)
    // Drawer should show at most 2 file previews and "See all outputs (4) →"
    const derived = _derivePipelinePhases(stateComplex);
    const phase2 = derived.phases.find(p => p.id === 'phase-2');
    const drawerHtml = ui._renderPhaseDrawerContent(phase2);

    if (!drawerHtml.includes('See all outputs (4)')) {
        throw new Error('Expected "See all outputs (4)" overflow button in drawer with 4 outputs, got: ' + drawerHtml);
    }
    // Count preview items rendered
    const previewMatches = drawerHtml.match(/class="pipeline-output-item/g);
    if (!previewMatches || previewMatches.length !== 2) {
        throw new Error('Expected exactly 2 output preview items before overflow, got: ' + (previewMatches ? previewMatches.length : 0));
    }
    // Check format of preview item
    if (!drawerHtml.includes('Modified') || !drawerHtml.includes('&#x2197;')) {
        throw new Error('Output preview missing change badge or arrow: ' + drawerHtml);
    }

    // ─────────────────────────────────────────────────────────────
    // TEST 6: Zero Outputs Produced -> Omit Outputs Section Completely
    // ─────────────────────────────────────────────────────────────
    const emptyStagePhase = {
        id: 'phase-empty',
        agentKey: 'chair_arbitration',
        summaryInfo: { route: 'COUNCIL' },
        outputs: [],
        files: new Map(),
        tasks: new Map(),
        failedOps: 0
    };
    const emptyDrawer = ui._renderPhaseDrawerContent(emptyStagePhase);
    if (emptyDrawer.includes('pipeline-outputs-section') || emptyDrawer.includes('outputs')) {
        throw new Error('Drawer with 0 outputs must completely omit outputs section: ' + emptyDrawer);
    }

    // ─────────────────────────────────────────────────────────────
    // TEST 7: Execution History Link Present in Every Drawer
    // ─────────────────────────────────────────────────────────────
    if (!drawerHtml.includes('View execution history &rarr;') || !drawerHtml.includes('data-agent="implementer"')) {
        throw new Error('Drawer missing "View execution history →" link: ' + drawerHtml);
    }

    // ─────────────────────────────────────────────────────────────
    // TEST 8: Blocked / Gate Auto-Expansion & Pause Icon (Decision Gate)
    // ─────────────────────────────────────────────────────────────
    const stateBlocked = {
        sessionId: 'sess-blocked-1',
        status: 'BLOCKED',
        pendingDecision: { reason: 'Human decision required on schema migration' },
        log: [
            { event: 'brief_created', agent: 'chair', ts: '2026-10-11T11:00:00Z' },
            { event: 'decision_required', agent: 'chair', status: 'BLOCKED', ts: '2026-10-11T11:00:05Z' }
        ]
    };
    ui._renderCaptainsLog(stateBlocked);
    // Blocked phase must auto-expand
    if (!ui._isPhaseExpanded({ id: 'phase-0' })) {
        throw new Error('Blocked phase must be auto-expanded to display required decision gate');
    }
    const blockedAtlasHtml = document.getElementById('council-pipeline-atlas').innerHTML;
    if (!blockedAtlasHtml.includes('pipeline-pause-icon') || !blockedAtlasHtml.includes('&#x23F8;')) {
        throw new Error('Blocked phase must render pause icon: ' + blockedAtlasHtml);
    }
    if (!blockedAtlasHtml.includes('Human decision required on schema migration')) {
        throw new Error('Blocked drawer must display decision callout: ' + blockedAtlasHtml);
    }

    // ─────────────────────────────────────────────────────────────
    // TEST 9: Odysseus extra.manager_verdict Detection
    // ─────────────────────────────────────────────────────────────
    const stateOdysseusVerdict = {
        sessionId: 'sess-odysseus-1',
        status: 'IN_PROGRESS',
        log: [
            { event: 'plan_created', agent: 'strategist', ts: '2026-10-11T12:00:00Z' },
            {
                event: 'review_required',
                agent: 'manager',
                status: 'BLOCKED',
                ts: '2026-10-11T12:00:10Z',
                extra: { manager_verdict: 'REVISE', requires_override: true }
            }
        ]
    };
    const derivedOdysseus = _derivePipelinePhases(stateOdysseusVerdict);
    const mgrPhase = derivedOdysseus.phases.find(p => p.agentKey === 'manager');
    if (!mgrPhase || mgrPhase.summaryInfo.verdict !== 'REVISE') {
        throw new Error('Expected manager verdict REVISE from extra.manager_verdict, got: ' + mgrPhase?.summaryInfo?.verdict);
    }
    if (mgrPhase.labelSuffix !== '(Revision requested)') {
        throw new Error('Expected (Revision requested) label suffix, got: ' + mgrPhase.labelSuffix);
    }

    // ─────────────────────────────────────────────────────────────
    // TEST 10: Dynamic Completeness Auditor Acceptance & Criteria
    // ─────────────────────────────────────────────────────────────
    const stateIncompleteAudit = {
        sessionId: 'sess-audit-1',
        status: 'COMPLETE',
        completeness: 67,
        completenessCriteria: [
            { id: 'C1', met: true, description: 'Auth token support' },
            { id: 'C2', met: true, description: 'Rate limit handler' },
            { id: 'C3', met: false, description: 'Audit telemetry' }
        ],
        log: [
            { event: 'brief_created', agent: 'chair', ts: '2026-10-11T13:00:00Z' },
            { event: 'acceptance_audit', agent: 'completeness_auditor', ts: '2026-10-11T13:00:10Z' }
        ]
    };
    const derivedAudit = _derivePipelinePhases(stateIncompleteAudit);
    const auditPhase = derivedAudit.phases.find(p => p.agentKey === 'completeness_auditor');
    if (!auditPhase || auditPhase.summaryInfo.acceptance !== 67) {
        throw new Error('Expected acceptance 67%, got: ' + auditPhase?.summaryInfo?.acceptance);
    }
    if (auditPhase.summaryInfo.criteriaMet !== 2 || auditPhase.summaryInfo.criteriaTotal !== 3) {
        throw new Error(`Expected 2 of 3 criteria met, got: ${auditPhase.summaryInfo.criteriaMet} of ${auditPhase.summaryInfo.criteriaTotal}`);
    }

    // ─────────────────────────────────────────────────────────────
    // TEST 11: Multi-Invocation Execution History Jump
    // ─────────────────────────────────────────────────────────────
    // Mock ghost stream ledger with two distinct Implementer execution clusters
    const mockLedger = mockElement('council-ghost-stream-ledger');
    elements['council-ghost-stream-ledger'] = mockLedger;

    const blockImpl1 = mockElement('block-impl-1');
    blockImpl1.dataset.agent = 'implementer';
    blockImpl1._targetMark = 'FIRST_RUN';

    const blockMgr = mockElement('block-mgr');
    blockMgr.dataset.agent = 'manager';

    const blockImpl2 = mockElement('block-impl-2');
    blockImpl2.dataset.agent = 'implementer';
    blockImpl2._targetMark = 'REVISION_FIX';

    mockLedger._children = [blockImpl1, blockMgr, blockImpl2];
    mockLedger.querySelectorAll = (sel) => mockLedger._children;

    // Jumping to invocation 0 must scroll to FIRST_RUN block
    ui._scrollToStreamAgent('implementer', 0);
    if (!blockImpl1.scrollIntoViewCalled) {
        throw new Error('Invocation 0 did not scroll blockImpl1 into view');
    }

    // Jumping to invocation 1 must scroll to REVISION_FIX block
    blockImpl1.scrollIntoViewCalled = false;
    ui._scrollToStreamAgent('implementer', 1);
    if (!blockImpl2.scrollIntoViewCalled) {
        throw new Error('Invocation 1 did not scroll blockImpl2 into view');
    }

    // ─────────────────────────────────────────────────────────────
    // TEST 12: Tool Execution Active Duration & Waiting Duration
    // ─────────────────────────────────────────────────────────────
    const stateTimedTools = {
        sessionId: 'sess-tools-1',
        status: 'COMPLETE',
        log: [
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-11T14:00:00Z' },
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-11T14:00:15Z', durationMs: 15000 },
            { event: 'code_update', agent: 'implementer', file_path: 'app.py', ts: '2026-10-11T14:00:20Z' }
        ]
    };
    const derivedTimed = _derivePipelinePhases(stateTimedTools);
    const timedImplPhase = derivedTimed.phases.find(p => p.agentKey === 'implementer');
    if (!timedImplPhase || timedImplPhase.activeDuration !== 15) {
        throw new Error('Expected activeDuration 15s, got: ' + timedImplPhase?.activeDuration);
    }
    if (timedImplPhase.waitingDuration !== 5) {
        throw new Error('Expected waitingDuration 5s, got: ' + timedImplPhase?.waitingDuration);
    }

    console.log('PIPELINE_ATLAS_VERIFIED_SUCCESSFULLY');
    process.exit(0);
    """

    proc = subprocess.run(["node"], input=node_script, capture_output=True, text=True, encoding="utf-8", check=True)
    assert "PIPELINE_ATLAS_VERIFIED_SUCCESSFULLY" in proc.stdout
