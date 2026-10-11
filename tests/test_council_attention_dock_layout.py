from pathlib import Path
import subprocess

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "js" / "council" / "council.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_html_attention_dock_sibling_placement():
    """Verify council-attention-dock is placed between council-log-header and council-log-toolbar."""
    assert 'id="council-log-header"' in HTML
    assert 'id="council-attention-dock"' in HTML
    assert 'id="council-log-toolbar"' in HTML

    # Verify DOM ordering: header -> dock -> toolbar -> captains-log
    header_idx = HTML.index('id="council-log-header"')
    dock_idx = HTML.index('id="council-attention-dock"')
    toolbar_idx = HTML.index('id="council-log-toolbar"')
    log_idx = HTML.index('id="council-captains-log"')

    assert header_idx < dock_idx < toolbar_idx < log_idx
    assert '<div class="council-attention-dock" id="council-attention-dock" hidden></div>' in HTML


def test_css_attention_dock_and_zero_overlap_geometry():
    """Verify CSS layout geometry eliminates sticky overlap artifacts and enforces solid containment."""
    # Dock styling
    assert ".council-attention-dock" in CSS
    assert ".council-attention-dock:empty" in CSS
    dock_block = CSS.split(".council-attention-dock {", 1)[1].split("}", 1)[0]
    assert "flex-shrink: 0" in dock_block
    assert "position: relative" in dock_block
    assert "background: var(--council-surface, var(--panel))" in dock_block

    # Shelf styling: relative positioning, solid background, NO sticky overlap
    shelf_block = CSS.split(".audit-attention-shelf {", 1)[1].split("}", 1)[0]
    assert "position: relative" in shelf_block
    assert "margin: 0" in shelf_block
    assert "position: sticky" not in shelf_block
    assert "top: 0" not in shelf_block
    assert "backdrop-filter" not in shelf_block
    assert "-webkit-backdrop-filter" not in shelf_block
    assert "cubic-bezier(0.32, 0.72, 0, 1)" in shelf_block

    # Collapsible button styling & Kowalski spring easing
    assert ".audit-attention-toggle-btn" in CSS
    assert "cubic-bezier(0.32, 0.72, 0, 1)" in CSS
    assert ".audit-attention-toggle-btn:active" in CSS
    btn_active_block = CSS.split(".audit-attention-toggle-btn:active {", 1)[1].split("}", 1)[0]
    assert "scale(0.97)" in btn_active_block


def test_attention_dock_lifecycle_and_scroll_isolation_simulation():
    """Node simulation verifying zero errors hide dock, errors render to dock outside scroll,
    and collapse toggle functions cleanly."""
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
                return [];
            },
            querySelector(sel) { return null; },
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
                fns.forEach(f => f({ target: target || this }));
            },
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
    eval(cleanJs + '; global.CouncilUI = CouncilUI;');

    const ui = new CouncilUI({}, {});

    // Case 1: Clean session with 0 errors
    const stateClean = {
        sessionId: 'sess-clean',
        status: 'COMPLETE',
        log: [
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:00:00Z', extra: { tool: 'bash', command: 'ls' } },
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-09T01:00:01Z', exit_code: 0, extra: { tool: 'bash', output: 'ok' } }
        ]
    };

    ui._renderCaptainsLog(stateClean);
    const dockEl = document.getElementById('council-attention-dock');
    const logEl = document.getElementById('council-captains-log');

    if (!dockEl.hidden) {
        throw new Error('Dock must be hidden when 0 errors exist, but hidden=' + dockEl.hidden);
    }
    if (dockEl.innerHTML !== '') {
        throw new Error('Dock innerHTML must be empty when 0 errors exist');
    }
    if (logEl.innerHTML.includes('council-attention-shelf')) {
        throw new Error('Scrollable log body must NOT contain attention shelf');
    }

    // Case 2: Session with 2 errors
    const stateErrors = {
        sessionId: 'sess-err',
        status: 'FAILED',
        log: [
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:00:00Z', extra: { tool: 'bash', command: 'pytest' } },
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-09T01:00:01Z', exit_code: 1, text: 'AssertionError: fail', extra: { tool: 'bash', output: 'fail' } },
            { event: 'error', text: 'OutOfMemory', ts: '2026-10-09T01:00:02Z' }
        ]
    };

    ui._renderCaptainsLog(stateErrors);

    if (dockEl.hidden) {
        throw new Error('Dock must be unhidden when visible errors exist');
    }
    if (!dockEl.innerHTML.includes('council-attention-shelf')) {
        throw new Error('Dock must contain #council-attention-shelf');
    }
    if (!dockEl.innerHTML.includes('2 failures')) {
        throw new Error('Dock must display badge with 2 failures');
    }
    // Crucial: #council-captains-log must NOT contain the attention shelf!
    if (logEl.innerHTML.includes('council-attention-shelf')) {
        throw new Error('Attention shelf was injected inside scroll container! Must be in sibling dock only.');
    }
    if (!logEl.innerHTML.includes('audit-row--error')) {
        throw new Error('Scrollable log container must contain error rows in its sealed viewport');
    }

    // Scroll simulation: when logEl scrolls (scrollTop > 0), rows are inside logEl,
    // dock is completely separate outside scroll container
    logEl.scrollTop = 150;
    if (logEl.scrollTop !== 150) throw new Error('Scroll simulation failed');

    // Case 3: Collapsible ergonomics toggle [▼ Collapse] -> [▶ Show]
    if (!dockEl.innerHTML.includes('[▼ Collapse]')) {
        throw new Error('Expanded shelf must show [▼ Collapse] toggle button');
    }

    // Simulate clicking toggle button
    const mockToggleBtn = {
        closest(sel) {
            if (sel === '.audit-attention-toggle-btn') return this;
            return null;
        }
    };
    dockEl.dispatch('click', mockToggleBtn);

    if (!ui._attentionShelfCollapsed) {
        throw new Error('_attentionShelfCollapsed must be true after toggle click');
    }
    if (!dockEl.innerHTML.includes('[▶ Show]')) {
        throw new Error('Collapsed shelf must show [▶ Show] toggle button');
    }
    if (!dockEl.innerHTML.includes('audit-attention-shelf--collapsed')) {
        throw new Error('Collapsed shelf must have class audit-attention-shelf--collapsed');
    }
    if (!dockEl.innerHTML.includes('audit-attention-list" hidden')) {
        throw new Error('Collapsed shelf list must have hidden attribute');
    }

    // Click again to expand
    dockEl.dispatch('click', mockToggleBtn);
    if (ui._attentionShelfCollapsed) {
        throw new Error('_attentionShelfCollapsed must be false after second click');
    }
    if (!dockEl.innerHTML.includes('[▼ Collapse]')) {
        throw new Error('Expanded shelf must show [▼ Collapse] again');
    }

    // Case 4: Stderr peek drawer toggle inside attention shelf
    const mockPeekBtn = {
        dataset: { peekId: 'shelf-aud-1' },
        textContent: '[Peek Stderr]',
        closest(sel) { return sel === '.audit-btn-peek' ? this : null; }
    };
    const drawerEl = document.getElementById('peek-shelf-aud-1');
    drawerEl.hidden = true;
    dockEl.dispatch('click', mockPeekBtn);
    if (drawerEl.hidden !== false || mockPeekBtn.textContent !== '[Hide Stderr]') {
        throw new Error('Peek drawer in shelf failed to open on click: hidden=' + drawerEl.hidden + ' text=' + mockPeekBtn.textContent);
    }
    dockEl.dispatch('click', mockPeekBtn);
    if (drawerEl.hidden !== true || mockPeekBtn.textContent !== '[Peek Stderr]') {
        throw new Error('Peek drawer in shelf failed to close on second click: hidden=' + drawerEl.hidden + ' text=' + mockPeekBtn.textContent);
    }

    // Case 5: Dynamic error resolution during session (new state with 0 errors immediately hides dock and clears content)
    const stateResolved = {
        sessionId: 'sess-err',
        status: 'IN_PROGRESS',
        log: [
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:00:00Z', extra: { tool: 'bash', command: 'pytest' } },
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-09T01:00:01Z', exit_code: 0, text: 'ok', extra: { tool: 'bash', output: 'ok' } }
        ]
    };
    ui._renderCaptainsLog(stateResolved);
    if (!dockEl.hidden || dockEl.innerHTML !== '') {
        throw new Error('Dynamic resolution failed: dock must re-hide and empty when errors clear');
    }

    // Re-render errors to test search and facet filtering on shelf
    ui._renderCaptainsLog(stateErrors);
    if (dockEl.hidden) throw new Error('Dock should re-open when errors are present');

    // Filter by COMMANDS facet: should show 1 failure (the bash pytest failure), excluding OutOfMemory
    ui._logFilter = 'COMMANDS';
    ui._renderCaptainsLog(stateErrors);
    if (!dockEl.innerHTML.includes('1 failure')) {
        throw new Error('COMMANDS filter on shelf should show 1 failure, got: ' + dockEl.innerHTML);
    }

    // Filter by non-matching search: should hide dock
    ui._logFilter = 'ALL';
    ui._logSearchQuery = 'nonexistent-query-xyz';
    ui._renderCaptainsLog(stateErrors);
    if (!dockEl.hidden || dockEl.innerHTML !== '') {
        throw new Error('Non-matching search query should hide attention dock');
    }

    // Clear search: dock reappears
    ui._logSearchQuery = '';
    ui._renderCaptainsLog(stateErrors);
    if (dockEl.hidden || !dockEl.innerHTML.includes('2 failures')) {
        throw new Error('Clearing search should restore attention dock');
    }

    // Case 6: Reset cleans up dock and collapsed state
    ui.reset();
    if (dockEl.hidden !== true || dockEl.innerHTML !== '') {
        throw new Error('ui.reset() must re-hide and empty council-attention-dock');
    }
    if (ui._attentionShelfCollapsed !== false) {
        throw new Error('ui.reset() must reset _attentionShelfCollapsed to false');
    }

    // Case 7: Session with status FAILED but empty/clean log correctly populates Attention Dock
    // and does not leave a phantom hidden dock when error count is 1
    const stateFailedEmptyLog = {
        sessionId: 'sess-failed-clean',
        status: 'FAILED',
        error: 'Execution halted: container terminated unexpectedly',
        log: [
            { event: 'tool_start', agent: 'implementer', ts: '2026-10-09T01:00:00Z', extra: { tool: 'bash', command: 'ls' } },
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-09T01:00:01Z', exit_code: 0, extra: { tool: 'bash', output: 'ok' } }
        ]
    };
    ui._renderCaptainsLog(stateFailedEmptyLog);
    if (dockEl.hidden) {
        throw new Error('Attention dock must not be hidden when session status is FAILED');
    }
    if (!dockEl.innerHTML.includes('1 failure')) {
        throw new Error('Attention dock must display 1 failure for FAILED session with clean log');
    }
    if (!dockEl.innerHTML.includes('container terminated unexpectedly')) {
        throw new Error('Attention dock must display failure error detail');
    }
    if (!logEl.innerHTML.includes('audit-row--error')) {
        throw new Error('Ledger rows must display error row for FAILED session');
    }

    console.log('DOCK_LIFECYCLE_OK');
    process.exit(0);
    """

    proc = subprocess.run(["node"], input=node_script, capture_output=True, text=True, encoding="utf-8", check=True)
    assert "DOCK_LIFECYCLE_OK" in proc.stdout


def test_attention_dock_absent_fallback_simulation():
    """Verify graceful fallback renders inline when council-attention-dock is missing."""
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
            classList: { classes: new Set(), add() {}, remove() {}, contains() { return false; }, toggle() {} },
            querySelectorAll() { return []; }, querySelector() { return null; },
            addEventListener(ev, fn) {
                if (!this._listeners) this._listeners = {};
                if (!this._listeners[ev]) this._listeners[ev] = [];
                this._listeners[ev].push(fn);
            },
            dispatch(ev, target) {
                const fns = this._listeners?.[ev] || [];
                fns.forEach(f => f({ target: target || this }));
            },
            scrollHeight: 500, scrollTop: 0, clientHeight: 300
        };
    }

    // Explicitly return null for council-attention-dock (mocking legacy DOM lacking the dock)
    global.document = {
        getElementById(id) {
            if (id === 'council-attention-dock') return null;
            if (!elements[id]) elements[id] = mockElement(id);
            return elements[id];
        },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {}
    };

    const cleanJs = jsContent
        .replace(/^import\s+.*$/gm, '')
        .replace(/^export\s+default\s+.*$/gm, '')
        .replace(/export\s+/g, '');
    eval(cleanJs + '; global.CouncilUI = CouncilUI;');

    const ui = new CouncilUI({ subscribe: () => {} }, {});

    const stateErrors = {
        sessionId: 'sess-fallback',
        status: 'FAILED',
        subscribe: () => {},
        log: [
            { event: 'tool_output', agent: 'implementer', ts: '2026-10-09T01:00:01Z', exit_code: 1, text: 'fail', extra: { tool: 'bash', output: 'fail' } }
        ]
    };

    ui._renderCaptainsLog(stateErrors);
    const logEl = document.getElementById('council-captains-log');

    // In fallback mode when dock is absent, shelf must still be rendered inline inside logEl
    if (!logEl.innerHTML.includes('council-attention-shelf')) {
        throw new Error('Fallback failed: attention shelf should be rendered inline when dock is absent');
    }
    if (!logEl.innerHTML.includes('[▼ Collapse]')) {
        throw new Error('Fallback shelf must initially show [▼ Collapse]');
    }

    // Wire listeners on logEl (simulating CouncilUI.wireListeners fallback)
    ui.wireListeners({});

    // Simulate clicking toggle button inside logEl
    const mockFallbackToggle = {
        closest(sel) {
            if (sel === '.audit-attention-toggle-btn') return this;
            return null;
        }
    };
    logEl.dispatch('click', mockFallbackToggle);

    if (!ui._attentionShelfCollapsed) {
        throw new Error('Fallback collapse toggle failed to set _attentionShelfCollapsed');
    }
    if (!logEl.innerHTML.includes('[▶ Show]')) {
        throw new Error('Fallback shelf should re-render with [▶ Show] when collapsed');
    }
    if (!logEl.innerHTML.includes('audit-attention-shelf--collapsed')) {
        throw new Error('Fallback shelf should have audit-attention-shelf--collapsed class');
    }

    console.log('FALLBACK_OK');
    process.exit(0);
    """

    proc = subprocess.run(["node"], input=node_script, capture_output=True, text=True, encoding="utf-8", check=True)
    assert "FALLBACK_OK" in proc.stdout


def test_attention_dock_memoization_error_objects_and_null_resilience():
    """Verify dock memoization prevents DOM thrashing, error objects are extracted, and catch handles null state."""
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
            classList: { classes: new Set(), add(c) { this.classes.add(c); }, remove(c) { this.classes.delete(c); }, contains(c) { return this.classes.has(c); }, toggle() {} },
            querySelectorAll() { return []; },
            querySelector(sel) {
                if (sel.includes('data-entry-id')) {
                    return { scrollIntoViewCalled: false, scrollIntoView() { this.scrollIntoViewCalled = true; } };
                }
                return null;
            },
            addEventListener(ev, fn) {
                if (!this._listeners) this._listeners = {};
                if (!this._listeners[ev]) this._listeners[ev] = [];
                this._listeners[ev].push(fn);
            },
            dispatch(ev, target) {
                const fns = this._listeners?.[ev] || [];
                fns.forEach(f => f({ target: target || this }));
            },
            scrollHeight: 500, scrollTop: 0, clientHeight: 300
        };
    }

    global.document = {
        getElementById(id) {
            if (!elements[id]) elements[id] = mockElement(id);
            return elements[id];
        },
        querySelector() { return null; },
        querySelectorAll() { return []; }
    };

    const cleanJs = jsContent
        .replace(/^import\s+.*$/gm, '')
        .replace(/^export\s+default\s+.*$/gm, '')
        .replace(/export\s+/g, '');
    eval(cleanJs + '; global.CouncilUI = CouncilUI;');

    const ui = new CouncilUI({}, {});
    const dockEl = document.getElementById('council-attention-dock');
    const logEl = document.getElementById('council-captains-log');

    // 1. Error object extraction in state.status === 'FAILED'
    const stateObjError = {
        sessionId: 'sess-obj-err',
        status: 'FAILED',
        error: { message: 'Out of memory: worker killed (SIGKILL)' },
        log: []
    };
    ui._renderCaptainsLog(stateObjError);
    if (!dockEl.innerHTML.includes('Out of memory: worker killed (SIGKILL)')) {
        throw new Error('Attention dock failed to extract message from state.error object');
    }

    // 2. Signature caching / memoization check
    let setCounter = 0;
    const origInnerHTMLDescriptor = Object.getOwnPropertyDescriptor(dockEl, 'innerHTML');
    let currentHtml = dockEl.innerHTML;
    Object.defineProperty(dockEl, 'innerHTML', {
        get() { return currentHtml; },
        set(val) { setCounter++; currentHtml = val; },
        configurable: true
    });

    setCounter = 0;
    // Re-rendering with identical state should NOT reassign dockEl.innerHTML
    ui._renderCaptainsLog(stateObjError);
    if (setCounter !== 0) {
        throw new Error('dockEl.innerHTML was reassigned even though errors and collapse state did not change! (DOM thrashing)');
    }

    // Changing search or state DOES update
    ui._logSearchQuery = 'nonexistent-term';
    ui._renderCaptainsLog(stateObjError);
    if (setCounter === 0) {
        throw new Error('dockEl.innerHTML should update when filtered errors change');
    }

    // 3. Null state catch block safety: rendering null when getter throws must not throw uncaught TypeError
    const corruptNullState = null;
    try {
        ui._renderCaptainsLog(corruptNullState);
    } catch (e) {
        throw new Error('ui._renderCaptainsLog(null) threw uncaught exception: ' + e.message);
    }

    // 4. Click on attention item triggers scrollIntoView on matching row
    ui._logSearchQuery = '';
    ui._renderCaptainsLog(stateObjError);

    let scrolled = false;
    logEl.querySelector = function(sel) {
        if (sel === '[data-entry-id="aud-sess-fail"]') {
            return {
                scrollIntoView() { scrolled = true; }
            };
        }
        return null;
    };

    const mockItemTarget = {
        dataset: { entryId: 'aud-sess-fail' },
        closest(sel) {
            if (sel === '.audit-attention-item') return this;
            if (sel === 'button') return null;
            return null;
        }
    };
    dockEl.dispatch('click', mockItemTarget);
    if (!scrolled) {
        throw new Error('Clicking .audit-attention-item failed to trigger scrollIntoView on ledger row');
    }

    console.log('MEMOIZATION_AND_NULL_SAFETY_OK');
    process.exit(0);
    """

    proc = subprocess.run(["node"], input=node_script, capture_output=True, text=True, encoding="utf-8", check=True)
    assert "MEMOIZATION_AND_NULL_SAFETY_OK" in proc.stdout
