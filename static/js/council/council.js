// Council of Agents — ES module
// Targets the DOM structure introduced in index.html (diamond compass layout).
import markdownModule from '../markdown.js';

/* ─── State ─────────────────────────────────────────────────────── */
class CouncilState {
  constructor() {
    this.sessionId     = null;
    this.status        = 'PENDING';
    this.complexity    = null;
    this.activeAgent   = null;
    this.activeAgentSince = null;     // ms epoch when the current agent started working
    this.activeTool    = null;
    this.thoughts      = '';
    this.lastCode      = '';
    this.lastFile      = '';
    this.generatedFiles = {};
    this.selectedFile  = '';
    this.log           = [];          // raw events for Captain's Log
    this.chairBrief    = null;        // first chair message
    this.pendingReview = false;
    this.pendingReviewRequiresOverride = false;
    this.pendingPermission = null;
    this.pendingDecision = null;      // new: critical decision gate
    this.completeness    = null;      // new: completeness metric (e.g. 67)
    this.completenessCriteria = [];   // new: criteria with met/gap status
    this.roleOverrides = {};
    this.dag           = null;
    this.showDAG       = false;
    this.showThinking  = false;
    this.workspace     = '';
    this.route         = null;
    this.action        = null;
    this.target        = null;
    this.successSkills = [];
    this.selfReflections = [];
    this.contextBudget   = 0;
    this.compactCount    = 0;
    this.actualTokens    = 0;
    this.lastResponseTime = null;
    this.lastHeartbeatText = '';
    this._listeners    = new Set();
  }

  update(data) {
    if (!data || typeof data !== 'object') return;

    this.lastResponseTime = Date.now();

    if (data.event === 'heartbeat') {
      if (data.status) this.status = String(data.status);
      if (data.text) this.lastHeartbeatText = String(data.text);
      this._listeners.forEach(fn => {
        try { fn(this, 'heartbeat'); } catch(e) { console.error('[Council] Listener render error:', e); }
      });
      return;
    }
    this.lastHeartbeatText = '';

    if (data.context_budget !== undefined) this.contextBudget = Number(data.context_budget);
    if (data.compact_count !== undefined) this.compactCount = Number(data.compact_count);

    if (data.event === 'thought_delta') {
      if (data.text && typeof data.text === 'string') {
        this.thoughts += data.text;
        const el = document.getElementById('council-ghost-editor');
        const ledger = document.getElementById('council-ghost-stream-ledger');
        if (ledger) {
          // If streaming thought, we notify listeners with the event type
          this._listeners.forEach(fn => {
            try { fn(this, 'thought_delta'); } catch(e) { console.error('[Council] Listener render error:', e); }
          });
        } else if (el) {
          el.textContent = this.thoughts;
          el.scrollTop = el.scrollHeight;
        }
      }
      return;
    }

    if (data.status)     this.status      = String(data.status);
    if (data.complexity) this.complexity  = String(data.complexity);
    if (data.agent) {
      const a = String(data.agent);
      // Reset the elapsed timer whenever the working agent changes so the
      // ghost-editor badge reads "<AGENT> · <STATUS> · <elapsed-on-this-agent>".
      // Also flush the streamed-thought buffer on handoff: it is a single
      // cross-agent accumulator, and the live ghost block attributes the whole
      // buffer to the *current* agent — without this reset, the previous
      // agent's text is duplicated into the next agent's container.
      if (a !== this.activeAgent) {
        this.activeAgentSince = Date.now();
        this.thoughts = '';
      }
      this.activeAgent = a;
    }

    if (data.event === 'tool_start') {
      this.activeTool = typeof data.extra?.tool === 'string' ? data.extra.tool : '';
    } else if (data.event === 'tool_output') {
      this.activeTool = null;
    }

    // Ghost editor accumulates streamed thoughts
    if (data.text && typeof data.text === 'string' && data.event === 'active_agent')
      this.thoughts += data.text + '\n';

    // Code panel
    if (data.event === 'code_update' || (data.event === 'task_status_update' && data.code)) {
      this.lastCode = typeof data.code === 'string' ? data.code : '';
      this.lastFile = typeof data.file_path === 'string' ? data.file_path : '';
      if (this.lastFile) {
        this.generatedFiles[this.lastFile] = this.lastCode;
        this.selectedFile = this.lastFile;
      }
    }

    // Captain's log: record every meaningful event
    if (['thought', 'active_agent', 'log', 'log_append', 'status_changed', 'code_update', 'complete', 'error', 'dag_update', 'task_status_update', 'tool_start', 'tool_output', 'permission_request', 'context_recovery', 'context_manifest', 'diagnostic_update', 'completeness_update', 'verification_update', 'checkpoint_update', 'metrics_update'].includes(data.event)) {
      // Capture Chair's Brief separately for the card at the top
      if (data.agent === 'chair' && data.event === 'status_changed' && !this.chairBrief) {
        this.chairBrief = { ts: data.timestamp || new Date().toISOString(), text: typeof data.text === 'string' ? data.text : '' };
      }
      
      let logText = typeof data.text === 'string' ? data.text : '';
      if (data.event === 'task_status_update' && data.extra?.task_status === 'DONE') {
        const codeStr = typeof data.code === 'string' ? data.code : '';
        const lineCount = codeStr ? codeStr.split('\n').length : 0;
        logText = `Completed ${data.extra?.task_id || 'task'}: Created/Updated ${data.file_path || 'output.txt'} (${lineCount} lines written)`;
      }

      const sanitized = sanitizeLogEntry(data, logText);
      if (sanitized) {
        this.log.push(sanitized);
      }
    }

    // A committed thought/decision is now a permanent ledger block (rendered
    // from state.log). Clear the live stream buffer so the streaming block
    // stops re-rendering the same text for the same agent. The buffer refills
    // from the next agent's thought_delta events.
    if (data.event === 'thought' || (data.event === 'status_changed' && data.agent === 'chair')) {
      this.thoughts = '';
    }

    // Route/action/target from Chair (DIRECT vs PIPELINE)
    if (data.event === 'status_changed' && data.extra?.route) {
      this.route = data.extra.route;
      this.action = data.extra.action || null;
      this.target = data.extra.target || null;
    }

    // Route update from fallback events
    if (data.event === 'log' && data.extra?.route) {
      this.route = data.extra.route;
    }

    // Self-reflection
    if (data.event === 'self_reflection' && data.text) {
      this.selfReflections.push({
        ts: data.timestamp || new Date().toISOString(),
        text: data.text
      });
    }

    if (data.event === 'metrics_update' && data.extra) {
      if (data.extra.context_budget !== undefined) this.contextBudget = Number(data.extra.context_budget);
      if (data.extra.compact_count !== undefined) this.compactCount = Number(data.extra.compact_count);
      if (data.extra.total_tokens !== undefined) this.actualTokens = Number(data.extra.total_tokens) || 0;
    }

    // Success skill
    if (data.event === 'success_skill' && data.text) {
      this.successSkills.push({
        ts: data.timestamp || new Date().toISOString(),
        text: data.text,
        name: data.extra?.skill_name || data.skill_name || 'skill'
      });
    }

    // Manager gate
    if (data.event === 'review_required') {
      this.pendingReview = true;
      this.pendingReviewRequiresOverride = Boolean(
        data.extra?.requires_override ||
        (data.extra?.manager_verdict && data.extra.manager_verdict !== 'APPROVED')
      );
    } else if (data.status && data.status !== 'BLOCKED') {
      this.pendingReview = false;
      this.pendingReviewRequiresOverride = false;
    }

    // Permission request gate
    if (data.event === 'permission_request') {
      this.pendingPermission = {
        permissionId: data.extra?.permission_id || data.permission_id,
        action: data.extra?.action || data.action,
        target: data.extra?.target || data.target,
      };
    } else if (data.status && data.status !== 'BLOCKED' && data.event !== 'heartbeat') {
      // Only clear when the run has actually resumed past the BLOCKED gate.
      // Do NOT clear on review_required (manager gate) — separate bar — or on
      // transient heartbeat pulses (thinking), which must not wipe a still-
      // pending permission request.
      this.pendingPermission = null;
    }

    // Decision required gate (critical decision from user)
    if (data.event === 'decision_required') {
      this.pendingDecision = {
        question: data.extra?.question || data.text || '',
        options: data.extra?.options || ['Proceed'],
        criterionId: data.extra?.criterion_id || ''
      };
    } else if (data.status && data.status !== 'BLOCKED' && data.event !== 'heartbeat') {
      this.pendingDecision = null;
    }

    // Completeness metric updates
    if (data.event === 'completeness_update') {
      this.completeness = data.extra?.completeness ?? null;
      this.completenessCriteria = data.extra?.criteria || [];
    }

    if (data.event === 'dag_update' && data.extra?.dag) {
      const dag = data.extra.dag;
      this.dag = (dag && typeof dag === 'object') ? {
        nodes: Array.isArray(dag.nodes) ? dag.nodes : [],
        edges: Array.isArray(dag.edges) ? dag.edges : []
      } : null;
      this.showDAG = true;
    }
    if (data.event === 'task_status_update' && data.extra?.dag) {
      const dag = data.extra.dag;
      this.dag = (dag && typeof dag === 'object') ? {
        nodes: Array.isArray(dag.nodes) ? dag.nodes : [],
        edges: Array.isArray(dag.edges) ? dag.edges : []
      } : null;
    }

    this._listeners.forEach(fn => {
      try { fn(this); } catch(e) { console.error('[Council] Listener render error:', e); }
    });
  }

  subscribe(fn)   { this._listeners.add(fn); }
  unsubscribe(fn) { this._listeners.delete(fn); }
}

/* ─── Session (API + SSE) ────────────────────────────────────────── */
class CouncilSession {
  constructor(state) {
    this._state = state;
    this._es    = null;
  }

  async start(prompt) {
    const res = await fetch('/api/council/session', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        prompt,
        role_overrides: this._state.roleOverrides,
        workspace: this._state.workspace || undefined,
        context_budget: this._state.contextBudget || undefined
      }),
    });
    if (!res.ok) throw new Error(await res.text());
    const { session_id } = await res.json();
    this._state.sessionId = session_id;
    this._state.lastResponseTime = Date.now();

    this._es = new EventSource(`/api/council/stream/${session_id}`);
    this._es.addEventListener('council_event', e => {
      this._state.update(JSON.parse(e.data));
    });
    this._es.onerror = () => {
      this._state.update({ event: 'error', status: 'FAILED', text: 'Connection lost.' });
      this._es.close();
    };
    this._es.onmessage = e => { if (e.data === '[DONE]') this._es.close(); };
  }

  async respond(choice, notes = '') {
    if (!this._state.sessionId) return;
    const res = await fetch(`/api/council/session/${this._state.sessionId}/respond`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ choice, notes }),
    });
    if (!res.ok) throw new Error(await res.text());
  }

  async respondPermission(choice, permissionId, target, persistLevel = 'once') {
    if (!this._state.sessionId) return;
    const res = await fetch(`/api/council/session/${this._state.sessionId}/respond`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ choice, permission_id: permissionId, target, persist_level: persistLevel }),
    });
    if (!res.ok) throw new Error(await res.text());
  }

  async respondDecision(answer) {
    if (!this._state.sessionId) return;
    await fetch(`/api/council/session/${this._state.sessionId}/respond`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ choice: 'decision', answer }),
    });
  }

  async updateRoleOverride(role, config) {
    this._state.roleOverrides[role] = config;
    if (!this._state.sessionId) return;
    await fetch(`/api/council/session/${this._state.sessionId}/role/${role}`, {
      method:  'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(config),
    });
  }

  async load(sessionId) {
    if (this._state.sessionId === sessionId && this._es && this._es.readyState !== EventSource.CLOSED) {
      this._state.update({});
      return;
    }
    this.close();
    this._state.sessionId = sessionId;
    try {
      const res = await fetch(`/api/council/session/${sessionId}`);
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();

      // Reset state first
      this._state.lastResponseTime = Date.now();
      this._state.thoughts = '';
      this._state.activeTool = null;
      this._state.lastCode = '';
      this._state.lastFile = '';
      this._state.generatedFiles = {};
      this._state.selectedFile = '';
      this._state.log = [];
      this._state.chairBrief = null;
      this._state.complexity = data.complexity || null;
      this._state.status = data.status || 'PENDING';
      this._state.roleOverrides = data.role_overrides || {};
      // Restore workspace from backend
      if (data.workspace) {
        this._state.workspace = data.workspace;
        const wsInput = document.getElementById('council-workspace-input');
        if (wsInput) wsInput.value = data.workspace;
        try { localStorage.setItem('councilWorkspace', data.workspace); } catch {}
      }
      this._state.route = data.route || data.extra?.route || null;
      this._state.action = data.action || data.extra?.action || null;
      this._state.target = data.target || data.extra?.target || null;
      this._state.successSkills = data.success_skills || [];
      this._state.selfReflections = data.self_reflections || [];
      this._state.contextBudget = Number(data.context_budget || 0);
      const budgetInput = document.getElementById('council-budget-input');
      if (budgetInput) budgetInput.value = data.context_budget || '';
      this._state.compactCount = Number(data.compact_count || 0);
      this._state.pendingReview = (this._state.status === 'BLOCKED');
      this._state.pendingReviewRequiresOverride = false;
      this._state.pendingPermission = null;
      this._state.pendingDecision = null;
      this._state.completeness = null;
      this._state.completenessCriteria = [];
      const persistedGate = data.pending_gate && typeof data.pending_gate === 'object'
        ? data.pending_gate : null;
      if (persistedGate?.kind === 'permission') {
        this._state.pendingReview = false;
        this._state.pendingPermission = {
          permissionId: persistedGate.permission_id,
          action: persistedGate.action,
          target: persistedGate.target,
        };
      } else if (persistedGate?.kind === 'review') {
        this._state.pendingReview = true;
        this._state.pendingReviewRequiresOverride = Boolean(
          persistedGate.requires_override ||
          (persistedGate.manager_verdict && persistedGate.manager_verdict !== 'APPROVED')
        );
      } else if (persistedGate?.kind === 'decision') {
        this._state.pendingReview = false;
        this._state.pendingDecision = {
          question: persistedGate.question || '',
          options: persistedGate.options || ['Proceed'],
          criterionId: persistedGate.criterion_id || '',
        };
      }
      if (!persistedGate && Array.isArray(data.log)) {
        // Find the last permission_request in the log and check whether it was
        // ever resolved. A permission is resolved once the run moved past the
        // BLOCKED gate after it: a later non-BLOCKED status change, or a
        // code_update for the same target (the write actually happened).
        const lastPermIdx = (() => {
          for (let i = data.log.length - 1; i >= 0; i--) {
            if (data.log[i] && data.log[i].event === 'permission_request') return i;
          }
          return -1;
        })();
        if (lastPermIdx !== -1) {
          const permEvt = data.log[lastPermIdx];
          const permId = permEvt.extra?.permission_id || permEvt.permission_id;
          const permTarget = permEvt.extra?.target || permEvt.target;
          const resolved = data.log.slice(lastPermIdx + 1).some(e => {
            if (!e) return false;
            if (e.status && e.status !== 'BLOCKED') return true;
            if (e.event === 'code_update') {
              const fp = e.file_path || e.extra?.target;
              return !permTarget || !fp || fp === permTarget;
            }
            return false;
          });
          if (!resolved) {
            this._state.pendingPermission = {
              permissionId: permId,
              action: permEvt.extra?.action || permEvt.action,
              target: permTarget,
            };
          }
        }

        // Find the last decision_required in the log and check whether it was
        // ever resolved. A decision is resolved once the run moved past the
        // BLOCKED gate after it: a later non-BLOCKED status change.
        const lastDecIdx = (() => {
          for (let i = data.log.length - 1; i >= 0; i--) {
            if (data.log[i] && data.log[i].event === 'decision_required') return i;
          }
          return -1;
        })();
        if (lastDecIdx !== -1) {
          const decEvt = data.log[lastDecIdx];
          const decResolved = data.log.slice(lastDecIdx + 1).some(e => {
            if (!e) return false;
            if (e.status && e.status !== 'BLOCKED') return true;
            return false;
          });
          if (!decResolved) {
            this._state.pendingDecision = {
              question: decEvt.extra?.question || decEvt.text || '',
              options: decEvt.extra?.options || ['Proceed'],
              criterionId: decEvt.extra?.criterion_id || decEvt.extra?.id || ''
            };
          }
        }
      }

      // Restore log
      if (Array.isArray(data.log)) {
        data.log.forEach(item => {
          if (!item) return;
          
          // Extract Chair brief
          if (item.agent === 'chair' && item.event === 'status_changed' && !this._state.chairBrief) {
            this._state.chairBrief = {
              ts: typeof item.timestamp === 'string' ? item.timestamp : (item.ts || new Date().toISOString()),
              text: typeof item.text === 'string' ? item.text : ''
            };
          }
          // Accumulate thoughts for Ghost Editor
          if ((item.event === 'thought' || item.event === 'active_agent') && typeof item.text === 'string') {
            this._state.thoughts += item.text + '\n';
          }
          
          let logText = typeof item.text === 'string' ? item.text : '';
          if (item.event === 'task_status_update' && item.extra && item.extra.task_status === 'DONE') {
            const codeStr = typeof item.code === 'string' ? item.code : '';
            const lineCount = codeStr ? codeStr.split('\n').length : 0;
            const taskId = typeof item.extra.task_id === 'string' ? item.extra.task_id : 'task';
            const filePath = typeof item.file_path === 'string' ? item.file_path : 'output.txt';
            logText = `Completed ${taskId}: Created/Updated ${filePath} (${lineCount} lines written)`;
          }

          // Restore last code block
          if (item.event === 'code_update' || (item.event === 'task_status_update' && item.code)) {
            this._state.lastCode = typeof item.code === 'string' ? item.code : '';
            this._state.lastFile = typeof item.file_path === 'string' ? item.file_path : '';
            if (this._state.lastFile) {
              this._state.generatedFiles[this._state.lastFile] = this._state.lastCode;
              this._state.selectedFile = this._state.lastFile;
            }
          }
          if (item.event === 'thought_delta' || item.event === 'tool_progress') return;

          // Sanitize before pushing
          const sanitized = sanitizeLogEntry(item, logText);
          if (sanitized) {
            this._state.log.push(sanitized);
          }
        });
      }

      // Set active agent based on status
      this._state.activeAgentSince = null;
      if (this._state.status === 'COMPLETE' || this._state.status === 'FAILED') {
        this._state.activeAgent = null;
      } else if (data.active_agent) {
        this._state.activeAgent = data.active_agent;
      }

      // After a refresh we don't know exactly when the agent started, so anchor
      // the elapsed timer to the most recent log event — for a stalled step that
      // equals "time since the agent last made progress", which is what matters.
      if ((this._state.status === 'IN_PROGRESS' || this._state.status === 'BLOCKED')
          && this._state.activeAgent && Array.isArray(data.log) && data.log.length) {
        const last = data.log[data.log.length - 1];
        const t = last && last.timestamp ? Date.parse(last.timestamp) : NaN;
        this._state.activeAgentSince = Number.isFinite(t) ? t : Date.now();
      }

      if (data.dag) {
        this._state.dag = data.dag;
        this._state.showDAG = true;
      }

      this._state.update({}); // trigger render

      // If still in progress/blocked, reconnect SSE stream to listen for real-time updates
      if (this._state.status === 'IN_PROGRESS' || this._state.status === 'BLOCKED') {
        this._es = new EventSource(`/api/council/stream/${sessionId}`);
        this._es.addEventListener('council_event', e => {
          this._state.update(JSON.parse(e.data));
        });
        this._es.onerror = () => {
          this._state.update({ event: 'error', status: 'FAILED', text: 'Connection lost.' });
          this._es.close();
        };
        this._es.onmessage = e => { if (e.data === '[DONE]') this._es.close(); };
      }
    } catch (err) {
      console.error('[Council] Load failed, resetting state:', err);
      this._state.thoughts = '';
      this._state.lastCode = '';
      this._state.lastFile = '';
      this._state.generatedFiles = {};
      this._state.selectedFile = '';
      this._state.log = [];
      this._state.chairBrief = null;
      this._state.complexity = null;
      this._state.status = 'FAILED';
      this._state.roleOverrides = {};
      this._state.pendingReview = false;
      this._state.pendingReviewRequiresOverride = false;
      this._state.pendingDecision = null;
      this._state.completeness = null;
      this._state.completenessCriteria = [];
      this._state.activeAgent = null;
      this._state.dag = null;
      this._state.showDAG = false;
      this._state.showThinking = false;
      this._state.route = null;
      this._state.action = null;
      this._state.target = null;
      this._state.successSkills = [];
      this._state.selfReflections = [];
      this._state.update({});
    }
  }

  close() {
    this._es?.close();
    // Clear blocking UI state when SSE closes (session switch, deletion, etc.)
    if (this._state.pendingPermission) {
      this._state.pendingPermission = null;
    }
    if (this._state.pendingReview) {
      this._state.pendingReview = false;
    }
    this._state.pendingReviewRequiresOverride = false;
  }
}

/* ─── UI ─────────────────────────────────────────────────────────── */
class CouncilUI {
  constructor(state, session) {
    this._state   = state;
    this._session = session;
    this._logFilter = 'ALL';
    this._prevRoute = null;
    this._transitionTimer = null;
    this._particles = [];
    this._particleRaf = null;
    this._prevCompassKey = null;
    // Tracks which burst groups the user has manually expanded.
    // Keys are stable for the lifetime of a logical task burst. The item count
    // is deliberately excluded because it changes while a burst is running.
    this._openBurstKeys = new Set();
    this._burstStatus = new Map();
    // Handle for the pending scroll-to-bottom rAF. Cancelled and replaced
    // on every render so only one rAF is ever queued at a time, preventing
    // accumulation at high event rates.
    this._scrollRaf = null;
    this._renderRaf = null;
    this._queuedState = null;
    this._queuedEventType = null;
  }

  /* ── Particle engine: dots flowing along the active edge ── */
  _startParticles(edgeEl) {
    this._stopParticles();
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    const svg = document.querySelector('.compass-svg');
    if (!svg || !edgeEl) return;

    const x1 = +edgeEl.getAttribute('x1'), y1 = +edgeEl.getAttribute('y1');
    const x2 = +edgeEl.getAttribute('x2'), y2 = +edgeEl.getAttribute('y2');
    const PARTICLE_COUNT = 3;
    const DURATION = 1600; // ms per full traversal

    for (let i = 0; i < PARTICLE_COUNT; i++) {
      const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c.setAttribute('r', '1.8');
      c.setAttribute('filter', 'url(#compass-glow)');
      c.classList.add('compass-particle', 'active');
      svg.appendChild(c);
      this._particles.push({ el: c, offset: i / PARTICLE_COUNT, start: performance.now() });
    }

    const animate = (now) => {
      this._particles.forEach(p => {
        const t = ((now - p.start) / DURATION + p.offset) % 1;
        p.el.setAttribute('cx', x1 + (x2 - x1) * t);
        p.el.setAttribute('cy', y1 + (y2 - y1) * t);
      });
      this._particleRaf = requestAnimationFrame(animate);
    };
    this._particleRaf = requestAnimationFrame(animate);
  }

  _stopParticles() {
    if (this._particleRaf) { cancelAnimationFrame(this._particleRaf); this._particleRaf = null; }
    this._particles.forEach(p => p.el.remove());
    this._particles = [];
  }

  reset() {
    this._stopParticles();
    clearTimeout(this._transitionTimer);
    this._transitionTimer = null;
    this._prevRoute = null;
    this._prevCompassKey = null;
    // Cancel any in-flight scroll rAF from the previous session so it cannot
    // fire against the freshly-cleared ledger after innerHTML is replaced.
    if (this._scrollRaf) { cancelAnimationFrame(this._scrollRaf); this._scrollRaf = null; }
    // Clear per-session open burst state so the new run starts fully collapsed.
    this._openBurstKeys.clear();
    this._burstStatus.clear();
    if (this._renderRaf) { cancelAnimationFrame(this._renderRaf); this._renderRaf = null; }
    this._queuedState = null;
    this._queuedEventType = null;
    this._state.sessionId = null;
    this._state.thoughts = '';
    this._state.activeTool = null;
    this._state.lastCode = '';
    this._state.lastFile = '';
    this._state.generatedFiles = {};
    this._state.selectedFile = '';
    this._state.log = [];
    this._state.chairBrief = null;
    this._state.complexity = null;
    this._state.status = 'PENDING';
    this._state.route = null;
    this._state.pendingReview = false;
    this._state.pendingReviewRequiresOverride = false;
    this._state.pendingPermission = null;
    this._state.pendingDecision = null;
    this._state.completeness = null;
    this._state.completenessCriteria = [];
    this._state.activeAgent = null;
    this._state.dag = null;
    this._state.showDAG = false;
    this._state.showThinking = false;
    this._state.roleOverrides = {};
    this._state.successSkills = [];
    this._state.selfReflections = [];
    this._state.actualTokens = 0;
    this._logFilter = 'ALL';
    // Reset animation-tracking cursors so next run starts fresh
    this._lastRenderedCount = 0;
    this._lastLogFilter = 'ALL';
    this.render(this._state);
  }

  scheduleRender(state, eventType) {
    const lightweight = type => type === 'thought_delta' || type === 'tool_progress' || type === 'heartbeat';
    this._queuedState = state;
    if (!this._queuedEventType || !lightweight(eventType) || lightweight(this._queuedEventType)) {
      this._queuedEventType = eventType;
    }
    if (this._renderRaf) return;
    this._renderRaf = requestAnimationFrame(() => {
      const nextState = this._queuedState;
      const nextEventType = this._queuedEventType;
      this._renderRaf = null;
      this._queuedState = null;
      this._queuedEventType = null;
      this.render(nextState, nextEventType);
    });
  }

  render(state, eventType) {
    if (eventType === 'thought_delta') {
      this._renderGhostEditor(state, eventType);
      return;
    }
    if (eventType === 'tool_progress') return;
    this._renderRunSummary(state);
    if (eventType === 'heartbeat') {
      this._renderGhostEditor(state, eventType);
      return;
    }
    this._renderCompass(state);
    this._renderGhostEditor(state, eventType);
    this._renderDAGView(state);
    this._renderCodePanel(state);
    this._renderCaptainsLog(state);
    this._renderReviewBar(state);
    this._renderPermissionBar(state);
    this._renderDecisionBar(state);
    this._renderComplexityBadge(state);
  }

  _estimatedTokens(state) {
    if (state.actualTokens > 0) return state.actualTokens;
    let chars = 0;
    state.log.forEach(event => {
      if (typeof event?.text === 'string') chars += event.text.length;
      if (typeof event?.code === 'string') chars += event.code.length;
    });
    return chars ? Math.round(chars / 3.8) : 0;
  }

  _renderRunSummary(state) {
    const statusEl = document.getElementById('council-run-status');
    const stageEl = document.getElementById('council-run-stage');
    const routeEl = document.getElementById('council-run-route');
    const tasksEl = document.getElementById('council-run-tasks');
    const budgetEl = document.getElementById('council-run-budget');
    const compactsEl = document.getElementById('council-run-compacts');
    const progressText = document.getElementById('council-run-progress-text');
    const progressBar = document.getElementById('council-run-progress-bar');
    const stopBtn = document.getElementById('council-run-cancel-btn');
    if (!statusEl) return;

    const labels = {
      PENDING: 'Ready', IN_PROGRESS: 'Running', BLOCKED: 'Needs input',
      COMPLETE: 'Verified complete', FAILED: 'Failed', CANCELLED: 'Cancelled'
    };
    const status = String(state.status || 'PENDING').toUpperCase();
    statusEl.textContent = labels[status] || status.toLowerCase();
    statusEl.dataset.status = status;
    const agent = state.activeAgent ? state.activeAgent.charAt(0).toUpperCase() + state.activeAgent.slice(1) : '';
    stageEl.textContent = state.activeTool
      ? `${agent || 'Agent'} · ${state.activeTool}`
      : (agent ? `${agent} active` : 'Waiting for a task');
    routeEl.textContent = state.route || '--';

    const nodes = Array.isArray(state.dag?.nodes) ? state.dag.nodes : [];
    const done = nodes.filter(node => node.status === 'DONE').length;
    tasksEl.textContent = `${done} / ${nodes.length}`;

    const criteria = Array.isArray(state.completenessCriteria) ? state.completenessCriteria : [];
    const verified = criteria.filter(item => item && (item.met === true || String(item.status || '').toLowerCase() === 'verified')).length;
    let ratio = criteria.length ? verified / criteria.length : 0;
    if (!criteria.length && Number.isFinite(Number(state.completeness))) {
      const raw = Number(state.completeness);
      ratio = Math.max(0, Math.min(1, raw > 1 ? raw / 100 : raw));
      progressText.textContent = `${Math.round(ratio * 100)}%`;
    } else {
      progressText.textContent = `${verified} / ${criteria.length}`;
    }
    progressBar.style.width = `${Math.round(ratio * 100)}%`;

    const tokens = this._estimatedTokens(state);
    budgetEl.textContent = state.contextBudget > 0
      ? `${tokens.toLocaleString()} / ${state.contextBudget.toLocaleString()}`
      : tokens.toLocaleString();
    budgetEl.title = state.contextBudget > 0 ? 'Estimated tokens / configured budget' : 'Estimated tokens';
    compactsEl.textContent = String(state.compactCount || 0);
    const running = status === 'IN_PROGRESS' || status === 'BLOCKED';
    stopBtn.disabled = !running;
    document.getElementById('chat-container')?.setAttribute('aria-busy', running ? 'true' : 'false');
  }

  /* Compass: cinematic morph between DIRECT and PIPELINE modes */
  _renderCompass(state) {
    const isDirect = state.route === 'DIRECT';
    const isPipeline = state.route === 'PIPELINE';
    const isRunning = state.status === 'IN_PROGRESS' || state.status === 'BLOCKED';
    const isTerminal = state.status === 'COMPLETE' || state.status === 'FAILED';

    // Dirty check — skip if compass-relevant state unchanged
    const compassKey = `${state.route}|${state.status}|${state.activeAgent}`;
    if (compassKey === this._prevCompassKey) return;
    this._prevCompassKey = compassKey;

    const wrap = document.getElementById('compass-wrap');
    if (!wrap) return;

    // Detect mode switch → trigger cinematic transition
    const prevRoute = this._prevRoute;
    if (prevRoute !== state.route && state.route) {
      wrap.classList.add('compass-transitioning');
      clearTimeout(this._transitionTimer);
      this._transitionTimer = setTimeout(() => {  // 600ms: longest child transition (0.5s node transform + 0.1s edge delay)
        wrap.classList.remove('compass-transitioning');
      }, 600);
    }
    this._prevRoute = state.route;

    // Mode classes
    wrap.classList.toggle('compass-direct', isDirect);
    wrap.classList.toggle('compass-completed', isTerminal && !!state.route);

    // Idle ambient state
    const isIdle = !state.route || (state.status === 'PENDING');
    wrap.classList.toggle('compass-idle', isIdle && !isTerminal);

    // Active nodes
    document.querySelectorAll('.compass-node').forEach(node => {
      const role = node.dataset.role;
      if (isDirect && role !== 'chair' && role !== 'implementer') {
        node.classList.remove('active');
      } else {
        node.classList.toggle('active', role === state.activeAgent);
      }
    });

    // Edges
    const edges = {
      chair: 'compass-edge-chair-strat',
      strategist: 'compass-edge-strat-manager',
      manager: 'compass-edge-manager-impl',
      implementer: 'compass-edge-impl-chair'
    };
    Object.keys(edges).forEach(role => {
      const edgeEl = document.getElementById(edges[role]);
      if (!edgeEl) return;
      if (isDirect) {
        edgeEl.classList.remove('compass-edge-active');
        edgeEl.classList.remove('pipeline-flow');
      } else {
        edgeEl.classList.toggle('compass-edge-active', state.activeAgent === role);
        edgeEl.classList.toggle('pipeline-flow', isPipeline && isRunning);
      }
    });

    // Particles (pipeline flow) — JS-driven edge traversal
    const activeEdgeId = state.activeAgent ? edges[state.activeAgent] : null;
    const activeEdgeEl = activeEdgeId ? document.getElementById(activeEdgeId) : null;
    if (isPipeline && isRunning && activeEdgeEl) {
      if (!this._particleRaf) this._startParticles(activeEdgeEl);
    } else {
      this._stopParticles();
    }

    // Active label
    const label = document.getElementById('compass-active-label');
    if (label) {
      label.className = 'compass-active-label';
      if (state.status === 'COMPLETE' && state.route) {
        label.textContent = state.route === 'DIRECT' ? 'Direct Complete' : 'Pipeline Complete';
        label.classList.add('label-complete');
      } else if (state.status === 'FAILED' && state.route) {
        label.textContent = state.route === 'DIRECT' ? 'Direct Failed' : 'Pipeline Failed';
        label.classList.add('label-failed');
      } else if (isDirect) {
        label.textContent = 'Direct Path';
        label.classList.add('label-direct');
      } else if (isPipeline && isRunning) {
        label.textContent = state.activeAgent
          ? state.activeAgent.charAt(0).toUpperCase() + state.activeAgent.slice(1)
          : 'Pipeline';
        label.classList.add('label-pipeline');
      } else if (state.activeAgent) {
        label.textContent = state.activeAgent.charAt(0).toUpperCase() + state.activeAgent.slice(1);
      } else {
        label.textContent = '';
      }
    }
  }

  /* Helper to extract file paths from task description */
  _extractPaths(text) {
    const regex = /`?([\w\-]+\/[\w\-\.]+\.\w+)`?/g;
    const paths = [];
    let match;
    while ((match = regex.exec(text)) !== null) {
      let path = match[1];
      if (!paths.includes(path)) {
        paths.push(path);
      }
    }
    return paths;
  }

  /* Helper to clean thinking text and strip JSON formatting noise, supporting partial streaming */
  _cleanThinkingText(text, agent) {
    if (!text) return '';
    let clean = text.trim();

    // 1. If strategist, strip the tasks block
    if (agent === 'strategist') {
      clean = clean.replace(/```tasks[\s\S]*?```/gi, '');
      clean = clean.replace(/```json\s*\[[\s\S]*?\]\s*```/gi, '');
      clean = clean.replace(/^\s*\[[\s\S]*?\]\s*$/g, '');
    }

    // 2. Try target keys first (reason, summary, notes, thought, thinking)
    const targetKeys = ['reason', 'summary', 'notes', 'thought', 'thinking'];
    for (const key of targetKeys) {
      const regex = new RegExp(`"${key}"\\s*:\\s*"`, 'i');
      const match = regex.exec(clean);
      if (match) {
        const startIndex = match.index + match[0].length;
        const remainder = clean.slice(startIndex);
        let endIdx = -1;
        let escaped = false;
        for (let i = 0; i < remainder.length; i++) {
          if (escaped) {
            escaped = false;
          } else if (remainder[i] === '\\') {
            escaped = true;
          } else if (remainder[i] === '"') {
            endIdx = i;
            break;
          }
        }
        let val = endIdx !== -1 ? remainder.slice(0, endIdx) : remainder;
        return val.replace(/\\"/g, '"').replace(/\\n/g, '\n').replace(/\\t/g, '\t').trim();
      }
    }

    // 3. Try to parse as raw JSON if it starts with { and doesn't match above keys
    if (clean.includes('```json')) {
      try {
        const jsonStr = clean.split('```json')[1].split('```')[0].trim();
        const data = JSON.parse(jsonStr);
        const keys = ['reason', 'summary', 'notes', 'thought', 'thinking', 't', 'description'];
        for (const k of keys) {
          if (data[k] && typeof data[k] === 'string') {
            return data[k];
          }
        }
      } catch (e) {}
    } else if (clean.startsWith('{')) {
      try {
        const data = JSON.parse(clean);
        const keys = ['reason', 'summary', 'notes', 'thought', 'thinking', 't', 'description'];
        for (const k of keys) {
          if (data[k] && typeof data[k] === 'string') {
            return data[k];
          }
        }
      } catch (e) {}
    }

    // 4. Strip leftover JSON formatting characters if we fail parsing (e.g. while streaming)
    if (clean.startsWith('{')) {
      clean = clean.replace(/^\{\s*"complexity"\s*:\s*"[^"]*",?\s*/gi, '');
      clean = clean.replace(/^\{\s*"verdict"\s*:\s*"[^"]*",?\s*/gi, '');
      clean = clean.replace(/^\{\s*"status"\s*:\s*"[^"]*",?\s*/gi, '');
      
      clean = clean.replace(/^\s*"[^"]+"\s*:\s*"[^"]*",?\s*/g, '');
      clean = clean.replace(/^\s*"[^"]+"\s*:\s*\[[\s\S]*?\],?\s*/g, '');

      for (const key of targetKeys) {
        const r = new RegExp(`^\\s*"${key}"\\s*:\\s*"`, 'i');
        clean = clean.replace(r, '');
      }

      if (clean.startsWith('{')) {
        clean = clean.slice(1).trim();
      }
    }

    // Strip general wrapper noise
    clean = clean.replace(/^```(json|tasks)?\s*/i, '');
    clean = clean.replace(/```$/, '');
    clean = clean.trim();

    // Clean up trailing quotes, commas, braces
    if (clean.endsWith('}')) {
      clean = clean.slice(0, -1).trim();
    }
    if (clean.endsWith('"') && (clean.match(/"/g) || []).length % 2 !== 0) {
      clean = clean.slice(0, -1);
    }
    if (clean.endsWith(',')) {
      clean = clean.slice(0, -1).trim();
    }

    return clean;
  }

  /* Helper to compile file stats from state.log code_update events */
  _getFileStats(name, state) {
    const updates = state.log.filter(e => e && e.event === 'code_update' && e.file_path === name);
    if (updates.length > 0) {
      const latest = updates[updates.length - 1];
      const latestLines = latest.code.split('\n');
      const lineCount = latestLines.length;

      if (updates.length > 1) {
        const prev = updates[updates.length - 2];
        const prevLines = prev.code.split('\n');
        const diff = lineCount - prevLines.length;
        let added = 0;
        let removed = 0;
        if (diff > 0) {
          added = diff;
          removed = 0;
        } else {
          added = 0;
          removed = Math.abs(diff);
        }
        return { lines: lineCount, added: added || 1, removed: removed || 0 };
      }
      return { lines: lineCount, added: lineCount, removed: 0 };
    }
    return { lines: 35, added: 12, removed: 2 };
  }

  /* Helper to compile implementer file actions from log tool calls and completed DAG tasks */
  _compileImplementerFiles(state) {
    const filesList = [];
    const seenFiles = new Set();

    // 1. Scan tool calls for read_file
    state.log.forEach(e => {
      if (e && e.agent === 'implementer' && (e.event === 'tool_start' || e.event === 'tool_output')) {
        const tool = (e.extra?.tool || e.tool || '').toLowerCase();
        if (tool === 'read_file') {
          const cmd = e.extra?.command || e.command || '';
          const pathMatch = /path:\s*["']([^"']+)["']/.exec(cmd);
          if (pathMatch) {
            const filepath = pathMatch[1];
            if (!seenFiles.has(filepath)) {
              seenFiles.add(filepath);
              filesList.push({ name: filepath, action: 'analyzed' });
            }
          }
        }
      }
    });

    // 2. Scan DAG nodes for DONE tasks and parse their output JSON
    if (state.dag && Array.isArray(state.dag.nodes)) {
      state.dag.nodes.forEach(node => {
        if (node.status === 'DONE' && node.output) {
          try {
            let cleanOutput = node.output.trim();
            if (cleanOutput.includes('```json')) {
              cleanOutput = cleanOutput.split('```json')[1].split('```')[0].trim();
            } else if (cleanOutput.startsWith('```')) {
              cleanOutput = cleanOutput.split('```')[1].split('```')[0].trim();
            }
            if (cleanOutput.startsWith('{')) {
              const data = JSON.parse(cleanOutput);
              if (Array.isArray(data.files_created)) {
                data.files_created.forEach(file => {
                  seenFiles.add(file);
                  const idx = filesList.findIndex(f => f.name === file);
                  if (idx !== -1) filesList.splice(idx, 1);
                  filesList.push({ name: file, action: 'created' });
                });
              }
              if (Array.isArray(data.files_modified)) {
                data.files_modified.forEach(file => {
                  seenFiles.add(file);
                  const idx = filesList.findIndex(f => f.name === file);
                  if (idx !== -1) filesList.splice(idx, 1);
                  filesList.push({ name: file, action: 'edited' });
                });
              }
            }
          } catch (err) {
            // ignore JSON parse error
          }
        }
      });
    }

    // 3. Attach computed stats
    return filesList.map(f => {
      const stats = this._getFileStats(f.name, state);
      return {
        name: f.name,
        action: f.action,
        lines: stats.lines,
        added: stats.added,
        removed: stats.removed
      };
    });
  }

  /* Helper to parse Manager verdict, summary, and issues */
  _parseManagerReview(event, log) {
    let rawJson = '';
    if (event.extra && event.extra.manager_review) {
      rawJson = event.extra.manager_review;
    } else {
      const reqEvent = log.find(evt => evt && evt.event === 'review_required' && evt.extra && evt.extra.manager_review);
      if (reqEvent) {
        rawJson = reqEvent.extra.manager_review;
      } else {
        const thoughtEvent = log.find(evt => evt && evt.event === 'thought' && evt.agent === 'manager' && evt.extra && evt.extra.manager_review);
        if (thoughtEvent) {
          rawJson = thoughtEvent.extra.manager_review;
        }
      }
    }

    let data = null;
    if (rawJson) {
      try {
        let clean = rawJson.trim();
        if (clean.includes('```json')) {
          clean = clean.split('```json')[1].split('```')[0].trim();
        } else if (clean.startsWith('```')) {
          clean = clean.split('```')[1].split('```')[0].trim();
        }
        if (clean.startsWith('{')) {
          data = JSON.parse(clean);
        }
      } catch (e) {
        console.warn("Failed to parse manager_review JSON:", e);
      }
    }

    const verdict = data?.verdict || event.status || 'APPROVED';
    const summary = data?.summary || event.text || '';
    const issues = data?.issues || [];

    return { verdict, summary, issues };
  }

  /* Active-agent badge text, including elapsed-on-agent while running. */
  _ghostBadgeText(state) {
    const statusWord = _statusWord(state.status).toUpperCase();
    let base;
    if (state.activeAgent) {
      const toolStr = state.activeTool ? ` (Tool: ${state.activeTool})` : '';
      base = `${state.activeAgent.toUpperCase()}${toolStr} · ${statusWord}`;
    } else {
      base = statusWord;
    }
    const running = state.status === 'IN_PROGRESS' || state.status === 'BLOCKED';
    if (running && state.activeAgent && state.activeAgentSince) {
      base += ` · ${_fmtElapsed(Date.now() - state.activeAgentSince)}`;
    }
    if (running && state.lastHeartbeatText) base += ` · ${state.lastHeartbeatText}`;
    return base;
  }

  /* Ghost Editor: stream text, toggle cursor blink */
  _renderGhostEditor(state, eventType) {
    const el = document.getElementById('council-ghost-editor');
    const ledger = document.getElementById('council-ghost-stream-ledger');
    if (!el) return;
    if (!ledger) {
      el.textContent = state.thoughts;
      el.scrollTop = el.scrollHeight;
      return;
    }

    // Toggle idle state
    const running = state.status === 'IN_PROGRESS' || state.status === 'BLOCKED';
    el.classList.toggle('idle', !running);

    // Update status label (Active Agent Badge)
    const agentEl = document.getElementById('council-ghost-agent');
    const pulseDot = document.querySelector('#council-ghost-status .council-pulse-dot');
    if (agentEl) {
      agentEl.textContent = this._ghostBadgeText(state);

      // Update color and animation based on status
      if (state.status === 'FAILED') {
        agentEl.style.color = 'var(--sys, #e05858)';
        if (pulseDot) {
          pulseDot.style.background = 'var(--sys, #e05858)';
          pulseDot.style.boxShadow = '0 0 6px var(--sys, #e05858)';
          pulseDot.style.animation = 'none'; // Stop pulsing
        }
      } else if (state.status === 'COMPLETE') {
        agentEl.style.color = 'var(--pass, #4eb870)';
        if (pulseDot) {
          pulseDot.style.background = 'var(--pass, #4eb870)';
          pulseDot.style.boxShadow = '0 0 6px var(--pass, #4eb870)';
          pulseDot.style.animation = 'none'; // Stop pulsing
        }
      } else if (state.status === 'CANCELLED') {
        agentEl.style.color = 'var(--muted, #5c4e42)';
        if (pulseDot) {
          pulseDot.style.background = 'var(--muted, #5c4e42)';
          pulseDot.style.boxShadow = 'none';
          pulseDot.style.animation = 'none'; // Stop pulsing
        }
      } else if (state.status === 'BLOCKED') {
        agentEl.style.color = 'var(--warn, #df8e45)';
        if (pulseDot) {
          pulseDot.style.background = 'var(--warn, #df8e45)';
          pulseDot.style.boxShadow = '0 0 6px var(--warn, #df8e45)';
          pulseDot.style.animation = 'cc-node-pulse 2s ease-in-out infinite';
        }
      } else { // IN_PROGRESS, PENDING, or others
        agentEl.style.color = '';
        if (pulseDot) {
          pulseDot.style.background = '';
          pulseDot.style.boxShadow = '';
          pulseDot.style.animation = '';
        }
      }
    }

    // Once the live card exists, streamed tokens only change liveness state.
    // Rebuilding expanded burst bodies for every token is needless.
    if (eventType === 'thought_delta' && ledger.querySelector('[data-live-stream]')) return;

    // Parse blocks sequentially
    const blocks = [];
    let lastAgent = null;

    // Helper: parse JSON args from command string (declared here so it is
    // available in the block-building loop below AND the render section).
    const _parseArgs = (cmd) => {
      if (!cmd || typeof cmd !== 'string') return {};
      const trimmed = cmd.trim();
      if (!trimmed.startsWith('{')) return {};
      try { const p = JSON.parse(trimmed); return (p && typeof p === 'object' && !Array.isArray(p)) ? p : {}; } catch(e) { return {}; }
    };

    state.log.forEach((e, eventIndex) => {
      if (!e) return;
      const agent = e.agent || 'system';

      // Thread Delegation (Handoff)
      if (agent !== 'system' && lastAgent && lastAgent !== agent && lastAgent !== 'system') {
        blocks.push({ type: 'handoff', from: lastAgent, to: agent });
      }
      if (agent !== 'system') { lastAgent = agent; }

      // Block Type Matching - same logic as before up to pushing blocks
      if (e.event === 'status_changed' && agent === 'chair') {
        blocks.push({
          type: 'chair',
          complexity: e.complexity || 'SIMPLE',
          reason: compactAgentActivity('chair')
        });
      }
      else if (e.event === 'thought') {
        let outcome = '';
        if (agent === 'strategist') {
          outcome = 'Plan updated';
        } else if (agent === 'manager') {
          const review = this._parseManagerReview(e, state.log);
          outcome = `Review ${String(review.verdict || 'received').toLowerCase()}`;
        }
        blocks.push({
          type: 'think',
          agent: agent,
          text: compactAgentActivity(agent),
          outcome: outcome
        });
      }
      else if (e.event === 'dag_update' || e.event === 'task_status_update') {
        const tasks = (e.extra?.dag?.nodes || []).map(n => ({
          i: n.id,
          t: n.summary || compactTaskLabel(n.description, n.id),
          dp: n.depends_on || []
        }));
        let lastStrat = [...blocks].reverse().find(b => b.type === 'strat');
        if (lastStrat) { lastStrat.tasks = tasks; }
        else { blocks.push({ type: 'strat', tasks: tasks }); }

        const doneNodes = (e.extra?.dag?.nodes || []).filter(n => n.status === 'DONE');
        if (doneNodes.length > 0) {
          const files = this._compileImplementerFiles(state);
          let lastImpl = [...blocks].reverse().find(b => b.type === 'impl');
          if (lastImpl) { lastImpl.files = files; }
          else { blocks.push({ type: 'impl', files: files }); }
        }
      }
      else if (e.event === 'review_required') {
        const review = this._parseManagerReview(e, state.log);
        blocks.push({
          type: 'manager_output',
          verdict: review.verdict,
          summary: review.summary,
          issues: review.issues
        });
      }
      else if (e.event === 'tool_start') {
        const cmd = typeof e.extra?.command === 'string' ? e.extra.command : '';
        // Prefer the structured args dict emitted by the backend (new); fall back
        // to parsing the command string for backward compat with old log replays.
        const args = (e.extra?.args && typeof e.extra.args === 'object' && !Array.isArray(e.extra.args))
          ? e.extra.args : _parseArgs(cmd);
        blocks.push({
          type: 'tool_call',
          tool: typeof e.extra?.tool === 'string' ? e.extra.tool : '',
          command: cmd,
          args: args,
          status: 'RUNNING',
          taskId: typeof e.extra?.task_id === 'string' ? e.extra.task_id : '',
          sourceIndex: eventIndex,
          agent: agent
        });
      }
      else if (e.event === 'tool_output') {
        const toolName = typeof e.extra?.tool === 'string' ? e.extra.tool : '';
        const taskId = typeof e.extra?.task_id === 'string' ? e.extra.task_id : '';
        let lastTool = [...blocks].reverse().find(b =>
          b.type === 'tool_call' && b.tool === toolName && b.taskId === taskId
        );
        if (lastTool && lastTool.status === 'RUNNING') {
          lastTool.status = e.exit_code === 0 || e.exit_code === null ? 'SUCCESS' : 'FAILED';
          lastTool.output = typeof e.extra?.output === 'string' ? e.extra.output : '';
        } else {
          const cmd = typeof e.extra?.command === 'string' ? e.extra.command : '';
          const args = (e.extra?.args && typeof e.extra.args === 'object' && !Array.isArray(e.extra.args))
            ? e.extra.args : _parseArgs(cmd);
          blocks.push({
            type: 'tool_call',
            tool: toolName,
            command: cmd,
            args: args,
            status: e.exit_code === 0 || e.exit_code === null ? 'SUCCESS' : 'FAILED',
            output: typeof e.extra?.output === 'string' ? e.extra.output : '',
            taskId: taskId,
            sourceIndex: eventIndex,
            agent: agent
          });
        }
      }
      // code_update events populate state.generatedFiles (Implementor Output tabs);
      // no separate ghost-editor stream block needed.
      else if (e.event === 'error') {
        blocks.push({
          type: 'sys',
          code: compactFailureCode(e.code, e.text),
          msg: compactFailureReason(e.text, 'execution issue'),
          detail: '',
          count: 1
        });
      }
    });

    // ── Burst-group post-process ────────────────────────────────────────────
    // Collapse any run of ≥2 consecutive tool_call blocks into a single
    // burst_group block so the stream stays readable.
    // Wrapped in try/catch: a malformed log entry must never blank the
    // entire execution stream; fall through to raw blocks on failure.
    let finalBlocks = blocks;
    try {
      const _burstCheckpoint = (items) => {
        const counts = new Map();
        items.forEach(item => {
          const intent = compactToolIntent(item.tool, item.args || {}, item.command);
          counts.set(intent, (counts.get(intent) || 0) + 1);
        });
        return [...counts.entries()]
          .map(([intent, count]) => count > 1 ? `${intent} ×${count}` : intent)
          .join(' · ') || `${items.length} actions`;
      };

      const processedBlocks = [];
      const _burstScope = (item) => `${item.taskId || ''}|${item.agent || ''}`;
      let bi = 0;
      while (bi < blocks.length) {
        if (blocks[bi].type === 'tool_call') {
          let bj = bi + 1;
          const scope = _burstScope(blocks[bi]);
          while (
            bj < blocks.length &&
            blocks[bj].type === 'tool_call' &&
            _burstScope(blocks[bj]) === scope
          ) bj++;
          const run = blocks.slice(bi, bj);
          if (run.length >= 2) {
            const isRunning  = run.some(r => r.status === 'RUNNING');
            const hasFailure = run.some(r => r.status === 'FAILED');
            processedBlocks.push({
              type: 'burst_group',
              items: run,
              running: isRunning,
              hasFailure,
              checkpoint: _burstCheckpoint(run),
              taskId: run[0].taskId || '',
              agent: run[0].agent || 'implementer',
              sourceIndex: run[0].sourceIndex
            });
          } else {
            processedBlocks.push(...run);
          }
          bi = bj;
        } else {
          processedBlocks.push(blocks[bi]);
          bi++;
        }
      }
      const compactedBlocks = [];
      processedBlocks.forEach(item => {
        if (item.type === 'sys') {
          const previous = compactedBlocks[compactedBlocks.length - 1];
          if (previous && previous.type === 'sys' && previous.code === item.code && previous.msg === item.msg) {
            previous.count = (previous.count || 1) + (item.count || 1);
            return;
          }
        }
        compactedBlocks.push(item);
      });
      finalBlocks = compactedBlocks;
    } catch (burstErr) {
      console.warn('[Council] Burst-group aggregation failed, using raw blocks:', burstErr);
      finalBlocks = blocks; // safe fallback: raw individual tool cards
    }
    // ────────────────────────────────────────────────────────────────────────

    // Handle active live streaming thought/code delta (SAME LOGIC)
    if (running && state.activeAgent && state.thoughts) {
      if (lastAgent && lastAgent !== state.activeAgent && lastAgent !== 'system') {
        blocks.push({ type: 'handoff', from: lastAgent, to: state.activeAgent });
      }
      if (state.activeAgent === 'chair') {
        blocks.push({
          type: 'chair', complexity: state.complexity || 'PENDING',
          reason: compactAgentActivity('chair'), streaming: true
        });
      } else if (state.activeAgent === 'strategist') {
        blocks.push({
          type: 'think', agent: 'strategist',
          text: compactAgentActivity('strategist'), streaming: true
        });
      } else if (state.activeAgent === 'implementer') {
        blocks.push({
          type: 'think', agent: 'implementer',
          text: compactAgentActivity('implementer', state.activeTool), streaming: true
        });
      } else if (state.activeAgent === 'manager') {
        blocks.push({
          type: 'think', agent: 'manager',
          text: compactAgentActivity('manager'), streaming: true
        });
      }
    }

    // RENDER blocks to HTML using NEW card-based design
    let html = '';
    let totalChars = 0;

    // Open burst keys come from the UI instance Set — updated on user click
    // in wireListeners. This avoids a DOM querySelectorAll on every
    // thought_delta render (which fires at ~114 tok/s during streaming).
    const _openBursts = this._openBurstKeys;
    const _seenBurstKeys = new Set();

    // Snapshot scroll state before innerHTML wipes the DOM.
    // _wasAtBottom: auto-pin to bottom when user hasn't scrolled up.
    // _savedScrollTop: restore their exact position when they have scrolled up,
    // so innerHTML replacement doesn't snap the viewport to position 0.
    const _atBottom = (ledger.scrollHeight - ledger.scrollTop - ledger.clientHeight) < 40;
    const _savedScrollTop = _atBottom ? 0 : ledger.scrollTop;
    // Helper: section header HTML
    const _sectionHeader = (label, color) => `
      <div class="ghost-section-header">
        <span class="ghost-section-dot" style="background:${color}"></span>
        ${label}
      </div>`;

    // _parseArgs is declared above the block-building loop (see line ~886).

    // Helper: keep file identity useful without leaking directory context.
    const _shortPath = (p) => {
      if (!p) return '';
      const parts = String(p).replace(/\\/g, '/').split('/');
      return parts[parts.length - 1] || '';
    };

    // Helper: build collapsed summary text. This is deliberately semantic:
    // the primary stream must never expose raw commands, JSON, quotes, or
    // absolute paths merely because a tool emitted them.
    const _toolSummary = (tool, args, cmd) => {
      return compactToolIntent(tool, args, cmd);
    };

    // Helper: build expanded body HTML
    const _toolBody = (tool, args, cmd, output) => {
      const t = (tool || '').toLowerCase();
      let html = '';
      const scrub = value => String(value || '')
        .replace(/\b[A-Za-z]:[\\/][^\s,;]+/g, '<file>')
        .replace(/\b(?:projects|workspace|data|assets|src)[\\/][^\s,;]+/gi, '<file>')
        .replace(/```/g, '')
        .replace(/["'`{}\[\]]/g, '')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 280);
      const rawPath = args.path || (typeof cmd === 'string' ? cmd.split('\n')[0] : '');
      const fileName = _shortPath(rawPath);
      const detailLabel = t === 'read_file' ? 'Source read'
        : (t === 'write_file' || t === 'edit_file') ? 'Files updated'
        : t === 'bash' || t === 'python' ? _toolSummary(t, args, cmd)
        : _toolSummary(t, args, cmd);
      html += '<div class="ghost-tool-section-label">details</div><div class="ghost-tool-args">';
      html += '<div class="ghost-tool-arg"><span class="ghost-tool-arg-key">operation</span><span class="ghost-tool-arg-val">' + _esc(detailLabel) + '</span></div>';
      if (fileName) {
        html += '<div class="ghost-tool-arg"><span class="ghost-tool-arg-key">file</span><span class="ghost-tool-arg-val">' + _esc(fileName) + '</span></div>';
      }
      html += '</div>';
      if (output && output.trim()) {
        html += '<div class="ghost-tool-section-label">result</div>';
        html += '<pre class="ghost-tool-output">' + _esc(scrub(output)) + '</pre>';
      }

      return html;
    };

    // Helper: tool category metadata
    const _toolKind = (toolName) => {
      const t = (toolName || '').toLowerCase();
      if (t === 'bash' || t === 'python') return { icon: '⚡', label: 'Command Execution', dot: '#df8e45' };
      if (t === 'write_file' || t === 'edit_file') return { icon: '📄', label: 'File Write', dot: '#a67cff' };
      if (t === 'read_file' || t === 'ls' || t === 'grep' || t === 'glob') return { icon: '🔍', label: 'File Read', dot: '#4eb870' };
      return { icon: '🛠', label: 'Tool Invocation', dot: '#50a0df' };
    };

    // Helper: render tool call card
    // showHeader: whether to emit the section header (suppressed for consecutive same-category cards)
    const _renderToolCard = (b, showHeader) => {
      const kind = _toolKind(b.tool);
      const args = b.args || {};
      const statusColor = b.status === 'SUCCESS' ? 'var(--impl)' : b.status === 'FAILED' ? 'var(--fail)' : '#50a0df';
      const statusText = b.status === 'SUCCESS' ? '✓ SUCCESS' : b.status === 'FAILED' ? '✗ FAILED' : b.status;
      const summary = _toolSummary(b.tool, args, b.command);
      const body = _toolBody(b.tool, args, b.command, b.output);

      return `
        ${showHeader ? _sectionHeader(kind.label, kind.dot) : ''}
        <div class="ghost-tool-card">
          <div class="ghost-tool-header">
            <span class="ghost-tool-icon">${kind.icon}</span>
            <span class="ghost-tool-name">${_esc(b.tool)}</span>
            <span class="ghost-tool-summary" title="${_esc(summary)}">${_esc(summary)}</span>
            <span class="ghost-tool-status" style="color:${statusColor}">${statusText}</span>
            <span class="ghost-tool-chevron">▶</span>
          </div>
          ${body ? '<div class="ghost-tool-body">' + body + '</div>' : ''}
        </div>`;
    };

    // Tracks the kind.label of the last rendered tool card to suppress
    // repeated section headers for consecutive same-category tool calls.
    let lastToolKindLabel = null;

    finalBlocks.forEach((b) => {
      if (b.text) totalChars += b.text.length;
      if (b.code) totalChars += b.code.length;

      if (b.type === 'handoff') {
        let fromColor = '#a67cff';
        let toColor = '#a67cff';
        if (b.from === 'chair') fromColor = 'var(--chair)';
        else if (b.from === 'strategist') fromColor = 'var(--strat)';
        else if (b.from === 'implementer') fromColor = 'var(--impl)';
        else if (b.from === 'manager') fromColor = 'var(--mgr)';
        if (b.to === 'chair') toColor = 'var(--chair)';
        else if (b.to === 'strategist') toColor = 'var(--strat)';
        else if (b.to === 'implementer') toColor = 'var(--impl)';
        else if (b.to === 'manager') toColor = 'var(--mgr)';
        html += `
          <div class="ghost-handoff">
            <div class="ghost-handoff-line"></div>
            <span class="ghost-handoff-text">
              ⇄ THREAD DELEGATION:
              <span style="color: ${fromColor}; font-weight: bold;">${_esc(b.from.toUpperCase())}</span>
              ➔
              <span style="color: ${toColor}; font-weight: bold;">${_esc(b.to.toUpperCase())}</span>
            </span>
            <div class="ghost-handoff-line"></div>
          </div>`;
      } else {
        // Any non-handoff, non-tool_call block breaks consecutive tool grouping.
        // burst_group also resets the label because it encapsulates tool cards.
        if (b.type !== 'tool_call') lastToolKindLabel = null;
        // Open card container for all non-handoff blocks
        html += `<div class="ghost-stream-entry${b.streaming ? ' ghost-stream-entry--live' : ''}"${b.streaming ? ' data-live-stream' : ''}>`;

        if (b.type === 'chair') {
          const cl = b.complexity === 'COMPLEX' ? 'var(--fail)' : b.complexity === 'MEDIUM' ? 'var(--warn)' : 'var(--pass)';
          html += `
          ${_sectionHeader('Chair Evaluation', 'var(--chair)')}
          <div style="margin-bottom:4px">
            <span style="font-size:9px;font-family:var(--font);background:var(--bg-highlight,#1c1510);padding:2px 6px;border-radius:3px;color:${cl};border:1px solid var(--border)">${_esc(b.complexity)}</span>
          </div>
          <div class="ghost-md ghost-chair-text">${_ghostMd(b.reason)}${b.streaming ? '<span class="ghost-typing-cursor"></span>' : ''}</div>`;
        }
        else if (b.type === 'think') {
          html += `
          ${_sectionHeader('System Thought', 'var(--think)')}
          <div class="ghost-md ghost-think-text">${_ghostMd(b.text)}${b.streaming ? '<span class="ghost-typing-cursor"></span>' : ''}</div>
          ${b.outcome ? `<p style="font-size:10px;color:var(--think);margin:4px 0 0 0">→ ${_esc(b.outcome)}</p>` : ''}`;
        }
        else if (b.type === 'strat') {
          const _taskCount = b.tasks.length;
          const _waves = executionWaveCount(b.tasks);
          const _planSummary = `${_taskCount} task${_taskCount === 1 ? '' : 's'} planned · ${_waves} execution wave${_waves === 1 ? '' : 's'}`;
          const _labels = b.tasks.map(t => `${t.i} ${t.t}`).join(' · ');
          html += `
          ${_sectionHeader('Strategy Execution', 'var(--strat)')}
          <div class="ghost-plan-summary">${_esc(_planSummary)}</div>
          <div class="ghost-plan-labels">${_esc(_labels.slice(0, 220))}</div>`;
        }
        else if (b.type === 'impl') {
          html += `
          ${_sectionHeader('Implementation', 'var(--impl)')}
          <div style="display:flex;flex-direction:column;gap:3px">
            ${b.files.map(f => `
              <div style="font-size:10px;font-family:var(--font);display:flex;gap:8px;align-items:center">
                ${f.action === 'created' ? `
                  <span style="color:var(--impl);flex-shrink:0;font-weight:600">CREATED</span>
                  <span style="color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0" title="${_esc(f.name)}">${_esc(f.name)}</span>
                  <span style="color:var(--muted);flex-shrink:0">${f.lines} lines</span>
                ` : ''}
                ${f.action === 'analyzed' ? `
                  <span style="color:var(--strat);flex-shrink:0;font-weight:600">ANALYZED</span>
                  <span style="color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0" title="${_esc(f.name)}">${_esc(f.name)}</span>
                  <span style="color:var(--muted);flex-shrink:0">${f.lines} lines</span>
                ` : ''}
                ${f.action === 'edited' ? `
                  <span style="color:var(--warn);flex-shrink:0;font-weight:600">EDITED</span>
                  <span style="color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0" title="${_esc(f.name)}">${_esc(f.name)}</span>
                  <span style="color:var(--impl);flex-shrink:0">+${f.added}</span>
                  <span style="color:var(--fail);flex-shrink:0">-${f.removed || 0}</span>
                ` : ''}
              </div>
            `).join('')}
            ${b.files.length === 0 ? '<span style="color:var(--muted);font-size:10px">—</span>' : ''}
          </div>`;
        }
        else if (b.type === 'manager_output') {
          const m = b.verdict === 'APPROVED'
            ? { c: 'var(--impl)', b: 'rgba(78,184,112,0.1)' }
            : b.verdict === 'REVISE'
            ? { c: 'var(--warn)', b: 'rgba(217,119,6,0.1)' }
            : { c: 'var(--fail)', b: 'rgba(220,38,38,0.1)' };
          const sevOrder = { critical: 0, warning: 1, info: 2 };
          const sevColor = { critical: 'var(--fail)', warning: 'var(--warn)', info: 'var(--strat)' };
          const grouped = b.issues.reduce((acc, iss) => {
            (acc[iss.severity] = acc[iss.severity] || []).push(iss);
            return acc;
          }, {});
          const sorted = Object.keys(grouped).sort((a, b) => (sevOrder[a] ?? 99) - (sevOrder[b] ?? 99));
          const issueCount = b.issues.length;
          const criticalCount = (grouped.critical || []).length;
          const issueThemes = b.issues
            .slice(0, 3)
            .map(iss => compactIssueTheme(iss))
            .filter((value, index, values) => values.indexOf(value) === index)
            .join(' · ');
          html += `
          ${_sectionHeader('Manager Review', 'var(--muted)')}
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <span style="padding:2px 7px;border-radius:3px;font-size:9px;font-weight:600;color:${m.c};background:${m.b};border:1px solid ${m.c}33;flex-shrink:0;letter-spacing:.08em">${_esc(b.verdict)}</span>
            <span style="font-size:10px;color:var(--dim);flex:1">${_esc(issueCount ? `${issueCount} issue${issueCount === 1 ? '' : 's'}${criticalCount ? ` · ${criticalCount} critical` : ''}` : 'No issues reported')}</span>
          </div>
          ${issueThemes ? `<div class="ghost-review-themes">${_esc(issueThemes)}</div>` : ''}`;
        }
        else if (b.type === 'burst_group') {
          // Stable key for this group — used to restore open state across re-renders.
          const _burstKey = [
            'burst',
            encodeURIComponent(this._state.sessionId || 'session'),
            encodeURIComponent(b.agent || 'implementer'),
            encodeURIComponent(b.taskId || 'phase'),
            String(Number.isFinite(b.sourceIndex) ? b.sourceIndex : 0)
          ].join(':');
          _seenBurstKeys.add(_burstKey);
          const _wasRunning = this._burstStatus.get(_burstKey);
          // A user-controlled burst stays open while tools are arriving. Once
          // the logical burst becomes terminal, collapse it exactly once.
          if (_wasRunning === true && !b.running) {
            _openBursts.delete(_burstKey);
          }
          this._burstStatus.set(_burstKey, b.running);
          const _isOpen   = _openBursts.has(_burstKey);
          // Icon mosaic: up to 3 unique icons from items
          const _burstIcons = [...new Set(b.items.map(x => _toolKind(x.tool).icon))].slice(0, 3).join('');
          const _total = b.items.length;
          // Status indicator colour
          const _bsc = b.running ? '#50a0df' : b.hasFailure ? 'var(--fail)' : 'var(--impl)';
          const _bss = b.running ? '●' : b.hasFailure ? '✗' : '✓';
          // Build individual item rows for the body
          const _itemsHtml = b.items.map(item => {
            const _ik  = _toolKind(item.tool);
            const _is  = _toolSummary(item.tool, item.args || {}, item.command);
            const _isc = item.status === 'SUCCESS' ? 'var(--impl)' : item.status === 'FAILED' ? 'var(--fail)' : '#50a0df';
            const _isi = item.status === 'SUCCESS' ? '✓' : item.status === 'FAILED' ? '✗' : '◌';
            return `<div class="burst-item">
              <span class="burst-item-status" style="color:${_isc}">${_isi}</span>
              <span class="burst-item-icon">${_ik.icon}</span>
              <span class="burst-item-tool">${_esc(item.tool)}</span>
              <span class="burst-item-summary">${_esc(_is)}</span>
            </div>`;
          }).join('');
          // Checkpoint label — glow-word animation when running.
          // Words are wrapped in <span class="bw"> with staggered animation-delay
          // so the glow travels left→right. Each word's peak is spread evenly
          // across one full cycle; minimum gap is 0.28s so short sentences still
          // look like a relay rather than a simultaneous flash.
          let _cpHtml;
          if (b.running) {
            // checkpoint may contain safe HTML (<code>) from the bash branch;
            // split on whitespace tokens only, preserve the inner HTML.
            const _cpWords = b.checkpoint.split(' ').filter(w => w.length > 0);
            if (_cpWords.length === 0) {
              _cpHtml = '';
            } else {
              const _minGap   = 0.28;  // seconds between word peaks
              const _wordDur  = Math.max(1.6, _cpWords.length * _minGap * 2);
              const _peakGap  = _cpWords.length > 1
                ? Math.max(_minGap, (_wordDur * 0.85) / (_cpWords.length - 1))
                : 0;
              _cpHtml = _cpWords.map((w, idx) =>
                `<span class="bw" style="animation-duration:${_wordDur.toFixed(2)}s;animation-delay:${(idx * _peakGap).toFixed(2)}s">${w}</span>`
              ).join(' ');
            }
          } else {
            // Not running: checkpoint may contain safe <code> HTML; emit as-is
            // (it was already escaped/sanitised in _burstCheckpoint).
            _cpHtml = b.checkpoint;
          }
          html += `
            <div class="ghost-burst-group${b.running ? ' burst-running' : ''}${_isOpen ? ' burst-open' : ''}"
                 data-burst-open="${_isOpen ? '1' : '0'}"
                 data-burst-key="${_esc(_burstKey)}">
              <div class="ghost-burst-header">
                <span class="ghost-burst-icons">${_burstIcons}</span>
                <span class="ghost-burst-checkpoint">${_cpHtml}</span>
                <span class="ghost-burst-count">${_total}</span>
                <span class="ghost-burst-status" style="color:${_bsc}">${_bss}</span>
                <span class="ghost-burst-chevron">▶</span>
              </div>
              <div class="ghost-burst-body">${_itemsHtml}</div>
            </div>`;
        }
        else if (b.type === 'tool_call') {
          const _kind = _toolKind(b.tool);
          const _showHeader = _kind.label !== lastToolKindLabel;
          lastToolKindLabel = _kind.label;
          html += _renderToolCard(b, _showHeader);
        }
        // code_view blocks removed — Implementor Output column already shows file content.
        else if (b.type === 'sys') {
          const attemptLabel = b.count > 1 ? ` · ${b.count} attempts` : '';
          html += `
          <div style="border-left:3px solid var(--sys);background:rgba(239,68,68,0.06);padding:8px 10px;margin:0 0 0 -12px;border-radius:0 4px 4px 0">
            <div style="font-size:9px;color:var(--sys);font-weight:700;letter-spacing:.06em">${_esc(b.code)}</div>
            <div style="font-size:10px;color:var(--dim);margin-top:2px">${_esc(b.msg + attemptLabel)}</div>
            ${b.detail ? `<div style="font-size:9px;color:var(--muted);font-style:italic;margin-top:2px">${_esc(b.detail)}</div>` : ''}
          </div>`;
        }

        html += `</div>`; // Close ghost-stream-entry
      }
    });

    // A session can contain many short bursts. Prune state for bursts that
    // are no longer represented in the current log so the UI state remains
    // bounded without affecting open bursts that are still visible.
    for (const key of this._burstStatus.keys()) {
      if (!_seenBurstKeys.has(key)) this._burstStatus.delete(key);
    }
    for (const key of _openBursts) {
      if (!_seenBurstKeys.has(key)) _openBursts.delete(key);
    }

    // Append completeness indicator if available
    if (state.completeness !== null) {
      const pct = Math.round(state.completeness);
      let critHtml = '';
      if (Array.isArray(state.completenessCriteria) && state.completenessCriteria.length > 0) {
        critHtml = '<div style="font-size:9px;color:var(--dim);margin-top:4px;margin-left:12px">';
        state.completenessCriteria.forEach(c => {
          const met = c.met ? '✓' : '○';
          const metColor = c.met ? 'var(--pass)' : 'var(--warn)';
          critHtml += `<div style="color:${metColor};margin:2px 0"><span>${met}</span> ${_esc(c.name || '')}</div>`;
        });
        critHtml += '</div>';
      }
      html += `
        <div class="ghost-completeness-bar">
          <div style="font-size:10px;font-weight:600;color:var(--text);margin-bottom:4px">Completeness: ${pct}%</div>
          <div style="background:var(--border);height:6px;border-radius:3px;overflow:hidden">
            <div style="background:var(--pass);height:100%;width:${pct}%;transition:width 0.3s ease"></div>
          </div>
          ${critHtml}
        </div>`;
    }

    ledger.innerHTML = html;

    // ── Scroll management ────────────────────────────────────────────────────
    // Cancel any rAF queued by the previous render so we never accumulate
    // pending scroll callbacks (at 114 tok/s this would grow unboundedly).
    if (this._scrollRaf) { cancelAnimationFrame(this._scrollRaf); this._scrollRaf = null; }
    if (_atBottom) {
      // Pin to bottom. rAF lets the browser lay out the new HTML first so
      // scrollHeight is correct before we read it.
      this._scrollRaf = requestAnimationFrame(() => {
        ledger.scrollTop = ledger.scrollHeight;
        this._scrollRaf = null;
      });
    } else {
      // Restore the user's reading position. innerHTML resets scrollTop to 0,
      // so we have to put it back immediately (no rAF needed — no layout read).
      ledger.scrollTop = _savedScrollTop;
    }
    // ─────────────────────────────────────────────────────────────────────────

    // Update bottom status footer
    const speedEl = document.getElementById('council-stream-speed');
    const bufferEl = document.getElementById('council-stream-buffer');
    if (bufferEl) bufferEl.textContent = `~${(totalChars / 1000).toFixed(1)}k chars`;
    if (speedEl) speedEl.textContent = running ? '114 tok/s' : '0 tok/s';
  }

  _renderDAGView(state) {
    const container = document.getElementById('council-dag-view');
    const ghostEl   = document.getElementById('council-ghost-editor');
    const titleEl   = document.getElementById('council-ghost-title');
    const toggleBtn = document.getElementById('council-thinking-toggle');

    if (!container) return;

    if (state.showDAG && state.dag) {
      if (toggleBtn) {
        toggleBtn.hidden = false;
        toggleBtn.textContent = state.showThinking ? 'Hide thinking ▴' : 'Show thinking ▾';
      }

      if (toggleBtn && !toggleBtn._wired) {
        toggleBtn._wired = true;
        toggleBtn.addEventListener('click', () => {
          state.showThinking = !state.showThinking;
          state.update({});
        });
      }

      if (state.showThinking) {
        if (ghostEl) ghostEl.style.display = '';
        container.style.display = 'none';
        if (titleEl) titleEl.textContent = 'Execution stream';
      } else {
        if (ghostEl) ghostEl.style.display = 'none';
        container.style.display = '';
        if (titleEl) titleEl.textContent = 'Task Graph';
        this._renderDAGSVG(container, state.dag);
      }
    } else {
      container.style.display = 'none';
      container.innerHTML = '';
      if (ghostEl) ghostEl.style.display = '';
      if (titleEl) titleEl.textContent = 'Execution stream';
      if (toggleBtn) toggleBtn.hidden = true;
    }
  }

  _renderDAGSVG(container, dag) {
    const nodes = (dag && Array.isArray(dag.nodes)) ? dag.nodes : [];
    if (!nodes.length) {
      container.innerHTML = '<div style="color:var(--fg);opacity:.4;text-align:center;padding:40px;">No tasks</div>';
      return;
    }

    const edges = (dag && Array.isArray(dag.edges)) ? dag.edges : [];

    const inDeg = {};
    const children = {};
    nodes.forEach(n => { inDeg[n.id] = 0; children[n.id] = []; });
    edges.forEach(e => {
      if (inDeg[e.to] !== undefined) inDeg[e.to]++;
      if (children[e.from]) children[e.from].push(e.to);
    });

    const layers = {};
    const queue = nodes.filter(n => inDeg[n.id] === 0).map(n => n.id);
    queue.forEach(id => { layers[id] = 0; });

    const q = [...queue];
    while (q.length) {
      const id = q.shift();
      (children[id] || []).forEach(child => {
        inDeg[child]--;
        layers[child] = Math.max(layers[child] || 0, (layers[id] || 0) + 1);
        if (inDeg[child] === 0) q.push(child);
      });
    }

    const layerGroups = {};
    nodes.forEach(n => {
      const layer = layers[n.id] || 0;
      if (!layerGroups[layer]) layerGroups[layer] = [];
      layerGroups[layer].push(n);
    });

    const layerKeys = Object.keys(layerGroups).map(Number).sort((a, b) => a - b);

    const nodeW = 160, nodeH = 48, padX = 32, padY = 24, marginX = 24, marginY = 20;
    const maxLayerWidth = Math.max(...layerKeys.map(k => layerGroups[k].length));
    const svgW = Math.max(300, maxLayerWidth * (nodeW + padX) + marginX * 2);
    const svgH = Math.max(200, layerKeys.length * (nodeH + padY) + marginY * 2);

    const positions = {};
    layerKeys.forEach((layerIdx, row) => {
      const group = layerGroups[layerIdx];
      const totalW = group.length * (nodeW + padX) - padX;
      const startX = (svgW - totalW) / 2;
      group.forEach((node, col) => {
        positions[node.id] = {
          x: startX + col * (nodeW + padX),
          y: marginY + row * (nodeH + padY),
        };
      });
    });

    const statusClass = (s) => {
      const m = { PENDING: 'pending', IN_PROGRESS: 'in-progress', DONE: 'done', FAILED: 'failed', BLOCKED: 'blocked', RETRYING: 'retrying' };
      return 'dag-node--' + (m[s] || 'pending');
    };

    let svg = `<svg class="dag-svg" width="${svgW}" height="${svgH}" viewBox="0 0 ${svgW} ${svgH}">`;
    svg += `<defs><marker id="dag-arrow" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6" fill="none" stroke="var(--border)" stroke-width="1.5"/>
    </marker></defs>`;

    edges.forEach(e => {
      const from = positions[e.from];
      const to = positions[e.to];
      if (!from || !to) return;
      const fx = from.x + nodeW / 2, fy = from.y + nodeH;
      const tx = to.x + nodeW / 2, ty = to.y;
      const midY = (fy + ty) / 2;
      const isDone = nodes.find(n => n.id === e.from)?.status === 'DONE';
      svg += `<path class="dag-edge${isDone ? ' dag-edge--done' : ''}" d="M${fx},${fy} C${fx},${midY} ${tx},${midY} ${tx},${ty}"/>`;
    });

    nodes.forEach(n => {
      if (!n || !n.id) return;
      const p = positions[n.id];
      if (!p) return;
      const desc = typeof n.description === 'string' ? n.description : (n.description != null ? String(n.description) : '');
      const truncDesc = desc.length > 40 ? desc.slice(0, 37) + '…' : desc;
      svg += `<g class="dag-node ${statusClass(n.status)}" transform="translate(${p.x},${p.y})">
        <rect class="dag-node-rect" width="${nodeW}" height="${nodeH}"/>
        <text class="dag-node-id" x="${nodeW/2}" y="16">${_esc(n.id)}</text>
        <text class="dag-node-desc" x="${nodeW/2}" y="34">${_esc(truncDesc)}</text>
      </g>`;
    });

    svg += '</svg>';
    container.innerHTML = svg;
  }

  /* Code panel */
  _renderCodePanel(state) {
    const el  = document.getElementById('council-code-panel');
    const linenosEl = document.getElementById('council-code-linenos');
    const btn = document.getElementById('council-promote-btn');
    const headerContainer = document.getElementById('council-code-header-container');

    const files = Object.keys(state.generatedFiles || {});

    // Determine selected file
    let selectedFile = state.selectedFile || state.lastFile;
    if (files.length > 0) {
      if (!selectedFile || !state.generatedFiles[selectedFile]) {
        selectedFile = files[0];
      }
    }

    if (el) {
      if (files.length > 0) {
        // SUCCESS / POPULATED STATE
        if (linenosEl) linenosEl.style.display = 'block';
        el.style.padding = '16px';
        
        const safeCode = (selectedFile && state.generatedFiles[selectedFile] !== undefined)
          ? state.generatedFiles[selectedFile]
          : (typeof state.lastCode === 'string' ? state.lastCode : '');
        
        el.textContent = safeCode;
        if (linenosEl) {
          const lines = safeCode ? safeCode.split('\n') : [];
          const lineCount = Math.max(lines.length, 1);
          let linenosHtml = '';
          for (let i = 1; i <= lineCount; i++) {
            linenosHtml += `<div>${i}</div>`;
          }
          linenosEl.innerHTML = linenosHtml;
        }
      } else {
        // NO FILES - DETECT OTHER STATE MACHINE STATES
        if (linenosEl) linenosEl.style.display = 'none';
        el.style.padding = '0'; // Let the state container take full layout
        
        if (state.status === 'FAILED') {
          // ERROR STATE
          let errorMessage = 'An unknown error occurred during execution.';
          if (Array.isArray(state.log)) {
            const errEv = [...state.log].reverse().find(e => e && (e.event === 'error' || e.type === 'error' || e.status === 'FAILED' || e.text?.toLowerCase().includes('failed') || e.text?.toLowerCase().includes('error')));
            if (errEv && errEv.text) {
              errorMessage = errEv.text;
            }
          }
          el.innerHTML = `
            <div class="implementer-state-container error-state">
              <svg class="error-icon" viewBox="0 0 24 24" width="36" height="36"><path fill="currentColor" d="M12 2C6.47 2 2 6.47 2 12s4.47 10 10 10 10-4.47 10-10S17.53 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>
              <div class="error-title">Execution Failed</div>
              <div class="error-message">${_esc(errorMessage)}</div>
              <div class="error-hint">Check the Captain's Log on the right for full trace details.</div>
            </div>
          `;
        } else if (state.status === 'IN_PROGRESS' || state.activeAgent === 'implementer') {
          // LOADING STATE
          el.innerHTML = `
            <div class="implementer-state-container loading-state">
              <div class="skeleton-header">
                <div class="skeleton-bar pulsing" style="width: 40%"></div>
              </div>
              <div class="skeleton-body">
                <div class="skeleton-line pulsing" style="width: 85%"></div>
                <div class="skeleton-line pulsing" style="width: 70%"></div>
                <div class="skeleton-line pulsing" style="width: 90%"></div>
                <div class="skeleton-line pulsing" style="width: 55%"></div>
                <div class="skeleton-line pulsing" style="width: 75%"></div>
                <div class="skeleton-line pulsing" style="width: 40%"></div>
              </div>
            </div>
          `;
        } else if (state.status === 'PENDING') {
          // IDLE STATE
          el.innerHTML = `
            <div class="implementer-state-container idle-state">
              <div class="terminal-shell">
                <span class="terminal-prompt">odysseus@implementer:~$</span>
                <span class="terminal-cursor">_</span>
              </div>
            </div>
          `;
        } else {
          // EMPTY STATE (Orchestrator complete but no files output)
          el.innerHTML = `
            <div class="implementer-state-container empty-state">
              <svg class="empty-icon" viewBox="0 0 24 24" width="36" height="36"><path fill="currentColor" d="M13 9h5.5L13 3.5V9M6 2h8l6 6v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4c0-1.11.89-2 2-2m0 18h12v-2H6v2m0-4h12v-2H6v2m0-4h8V8H6v2Z"/></svg>
              <div class="empty-title">No Code Output</div>
              <div class="empty-subtitle">The implementation task executed, but did not generate or edit any workspace files.</div>
            </div>
          `;
        }
      }
    }

    if (headerContainer) {
      if (files.length > 0) {
        // Build tabbed interface!
        let tabsHtml = '<div class="council-code-tabs">';
        
        // Sort files to keep consistent tab ordering (alphabetical)
        const sortedFiles = [...files].sort();
        
        sortedFiles.forEach(filepath => {
          const filename = filepath.split(/[/\\]/).pop(); // case-sensitive Filename.ext
          const isActive = (filepath === selectedFile);
          tabsHtml += `<button type="button" class="council-code-tab${isActive ? ' active' : ''}" data-filepath="${_esc(filepath)}" title="${_esc(filepath)}">${_esc(filename)}</button>`;
        });
        tabsHtml += '</div>';
        
        headerContainer.innerHTML = tabsHtml;
        
        // Add event listeners to tabs
        const tabs = headerContainer.querySelectorAll('.council-code-tab');
        tabs.forEach(tab => {
          tab.addEventListener('click', () => {
            const path = tab.getAttribute('data-filepath');
            state.selectedFile = path;
            state.lastFile = path;
            state.lastCode = state.generatedFiles[path] || '';
            this.render(state);
          });
        });
      } else {
        // No generated files yet, show original title logic
        let titleText = 'Changes';
        if (state.status === 'FAILED') {
          titleText = 'Execution Failed';
        } else if (state.status === 'IN_PROGRESS' || state.activeAgent === 'implementer') {
          const activeNode = state.dag?.nodes?.find(n => n.status === 'IN_PROGRESS');
          titleText = activeNode 
            ? `Writing: ${activeNode.id} ...`
            : 'Executing task...';
        }
        headerContainer.innerHTML = `<span class="council-pane-title" id="council-code-title">${_esc(titleText)}</span>`;
      }
    }

    if (btn) {
      const code = (selectedFile && state.generatedFiles[selectedFile] !== undefined)
        ? state.generatedFiles[selectedFile]
        : state.lastCode;
      btn.hidden = !code;
    }
  }

  /* Captain's Log: Chair Brief card + timeline entries */
  _renderCaptainsLog(state) {
    const el = document.getElementById('council-captains-log');
    if (!el) return;
    const keepPinnedToBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48;

    // 1. Update status dot color and class
    const dotEl = document.getElementById('council-log-status-dot');
    if (dotEl) {
      dotEl.dataset.status = String(state.status || 'PENDING').toUpperCase();
    }

    // 2. Update session ID display
    const sidEl = document.getElementById('council-log-session-id');
    if (sidEl) {
      sidEl.textContent = `ID: ${state.sessionId || 'PENDING'}`;
    }

    // 3. Update footer button text and state
    const revertBtn = document.getElementById('council-revert-btn');
    if (revertBtn) {
      if (state.status === 'IN_PROGRESS' || state.status === 'BLOCKED') {
        revertBtn.textContent = 'Stop run';
        revertBtn.disabled = false;
      } else if (state.status === 'COMPLETE') {
        revertBtn.textContent = 'Restart from last brief';
        revertBtn.disabled = false;
      } else {
        revertBtn.textContent = 'Stop run';
        revertBtn.disabled = true;
      }
    }

    // 4. Update footer token display
    const tokens = this._estimatedTokens(state);
    const tokensEl = document.getElementById('council-log-tokens');
    if (tokensEl) {
      let suffix = '';
      if (state.contextBudget > 0) {
        const budgetPercent = Math.min(100, Math.round((tokens / state.contextBudget) * 100));
        suffix = ` / ${state.contextBudget.toLocaleString()} (${budgetPercent}%)`;
      }
      if (state.compactCount > 0) {
        suffix += ` · ${state.compactCount} compacts`;
      }
      tokensEl.textContent = tokens.toLocaleString() + ' tokens' + suffix;
    }

    let html = '';

    // Directive Context Box (Chair Brief)
    if (state.chairBrief) {
      const text = _esc(state.chairBrief.text || '');
      html += `
        <div class="log-directive-box">
          <p class="log-directive-text">
            <span class="log-directive-label">DIRECTIVE:</span> ${text}
          </p>
        </div>`;
    }

    // Filter selector bar — segmented pill control
    const filter = this._logFilter || 'ALL';
    html += `
      <div class="log-filters-pill-group">
        <div class="log-filters-pill-track">
          <button class="log-filter-pill ${filter === 'ALL'         ? 'active' : ''}" data-filter="ALL">ALL</button>
          <button class="log-filter-pill ${filter === 'STRATEGIST'  ? 'active' : ''}" data-filter="STRATEGIST">PLAN</button>
          <button class="log-filter-pill ${filter === 'IMPLEMENTER' ? 'active' : ''}" data-filter="IMPLEMENTER">BUILD</button>
          <button class="log-filter-pill ${filter === 'MANAGER'     ? 'active' : ''}" data-filter="MANAGER">REVIEW</button>
        </div>
      </div>`;

    // Cache task attempts by taskId to avoid quadratic getTaskAttempts calls in the loop
    const attemptsByTask = {};
    state.log.forEach(e => {
      if (!e) return;
      if (e.event === 'task_status_update' && e.extra?.task_id) {
        const taskId = e.extra.task_id;
        if (!attemptsByTask[taskId]) {
          attemptsByTask[taskId] = [];
        }
        const status = e.extra?.task_status;
        if (status === 'FAILED') {
          attemptsByTask[taskId].push({ status: 'REJECTED', note: 'Compiler/Error' });
        } else if (status === 'DONE') {
          attemptsByTask[taskId].push({ status: 'APPROVED', note: '' });
        }
      }
    });

    // Process and group timeline entries
    const grouped = [];
    let currentEntry = null;

    state.log.forEach(e => {
      if (!e) return;
      if (e.event === 'thought_delta' || e.event === 'tool_progress') return;
      if (e.agent === 'chair' && e.event === 'status_changed') return;

      const agent = typeof e.agent === 'string' ? e.agent : 'system';
      const time = typeof e.ts === 'string' ? e.ts.slice(11, 19) : new Date().toTimeString().slice(0, 8);
      const taskId = e.extra && typeof e.extra.task_id === 'string' ? e.extra.task_id : undefined;
      const previousEvent = currentEntry?.rawEvents?.[currentEntry.rawEvents.length - 1];
      const repeatedFailure = e.event === 'error' && previousEvent?.event === 'error'
        && compactFailureReason(e.text, 'execution issue') === compactFailureReason(previousEvent.text, 'execution issue');
      
      const isNewPhase = !currentEntry || 
                         currentEntry.agent !== agent || 
                         (taskId && currentEntry.taskId !== taskId) ||
                         e.event === 'complete' || 
                         (e.event === 'error' && !repeatedFailure);

      if (isNewPhase) {
        if (currentEntry) {
          grouped.push(currentEntry);
        }
        currentEntry = {
          id: 'grp-' + Math.random().toString(36).substr(2, 9),
          time: time,
          agent: agent,
          title: '',
          subtitle: '',
          taskId: taskId,
          files: [],
          attempts: [],
          rawEvents: [e]
        };
      } else {
        currentEntry.rawEvents.push(e);
      }

      // Populate files from this event
      const ops = parseFileOperations(e);
      if (ops) {
        ops.forEach(op => {
          if (!currentEntry.files.some(existing => sameFileOperation(existing, op))) {
            currentEntry.files.push(op);
          }
        });
      }

      // If it's a task status update, accumulate attempts
      if (e.event === 'task_status_update' && taskId) {
        currentEntry.attempts = attemptsByTask[taskId] || [];
      }
    });

    if (currentEntry) {
      grouped.push(currentEntry);
    }

    // Title / Subtitle resolution
    grouped.forEach(grp => {
      const first = grp.rawEvents[0];
      const last = grp.rawEvents[grp.rawEvents.length - 1];

      // Exclude low-level tool events from setting high-level timeline subtitles
      const nonToolEvs = grp.rawEvents.filter(ev => !['tool_start', 'tool_output', 'tool_progress'].includes(ev.event));
      const firstNonTool = nonToolEvs.length ? nonToolEvs[0] : null;
      const lastNonTool = nonToolEvs.length ? nonToolEvs[nonToolEvs.length - 1] : null;

      if (grp.agent === 'chair') {
        grp.title = state.route === 'DIRECT' ? 'Direct Assessment' : 'Pipeline Instantiated';
        grp.subtitle = compactAgentActivity('chair');
      } else if (grp.agent === 'strategist') {
        grp.title = 'PLAN · Dependency routing';
        const planEvent = [...grp.rawEvents].reverse().find(ev => ev.extra?.dag?.nodes);
        const planNodes = planEvent?.extra?.dag?.nodes || [];
        const compactPlan = planNodes.map(n => ({
          i: n.id,
          t: n.summary || compactTaskLabel(n.description, n.id),
          dp: n.depends_on || []
        }));
        const planCount = compactPlan.length;
        const planWaves = executionWaveCount(compactPlan);
        grp.subtitle = planCount
          ? `${planCount} task${planCount === 1 ? '' : 's'} planned · ${planWaves} execution wave${planWaves === 1 ? '' : 's'}`
          : 'Constructing execution plan';
      } else if (grp.agent === 'implementer') {
        if (grp.taskId) {
          const statusEv = [...grp.rawEvents].reverse().find(ev => ev.event === 'task_status_update') || lastNonTool;
          const node = statusEv?.extra?.dag?.nodes?.find(n => String(n.id) === String(grp.taskId));
          const label = statusEv?.extra?.task_label || node?.summary || compactTaskLabel(node?.description, grp.taskId);
          const taskState = String(statusEv?.extra?.task_status || statusEv?.status || '').toUpperCase();
          grp.title = `BUILD · ${grp.taskId} ${label}`;
          if (taskState === 'DONE') {
            const fileCount = grp.files.length || (statusEv?.file_path ? 1 : 0);
            grp.subtitle = `Approved · ${fileCount || 1} file${fileCount === 1 ? '' : 's'} updated`;
          } else if (taskState === 'FAILED' || statusEv?.status === 'FAILED') {
            grp.subtitle = `Rejected · ${compactFailureReason(statusEv?.text, 'execution issue')}`;
          } else {
            grp.subtitle = `Running · ${label}`;
          }
        } else {
          grp.title = 'BUILD · Code updates';
          grp.subtitle = 'Writing implementation changes';
        }
      } else if (grp.agent === 'manager') {
        const errorEvent = grp.rawEvents.find(ev => ev.event === 'error' || ev.status === 'FAILED');
        const errorCount = grp.rawEvents.filter(ev => ev.event === 'error').length;
        const attemptSuffix = errorCount > 1 ? ` · ${errorCount} attempts` : '';
        const permissionEvent = grp.rawEvents.find(ev => ev.event === 'permission_request');
        const recoveryEvent = grp.rawEvents.find(ev => ev.event === 'context_recovery');
        if (errorEvent) {
          grp.title = errorEvent.extra?.error_kind === 'context_overflow'
            ? 'Context Recovery Failed'
            : 'Manager Failed';
          grp.subtitle = `Blocked · ${compactFailureReason(errorEvent.text, 'review failed')}${attemptSuffix}`;
        } else if (permissionEvent) {
          grp.title = 'Permission Required';
          grp.subtitle = 'Waiting · permission required';
        } else if (recoveryEvent) {
          grp.title = 'Context Recovery';
          grp.subtitle = 'Recovering · larger context review';
        } else {
          grp.title = 'Approval Gate Blocked';
          grp.subtitle = 'Waiting · manager approval required';
        }
      } else {
        const checkEvent = lastNonTool || last;
        const errorCount = grp.rawEvents.filter(ev => ev.event === 'error').length;
        const attemptSuffix = errorCount > 1 ? ` · ${errorCount} attempts` : '';
        if (checkEvent.event === 'complete') {
          const prefix = state.route === 'DIRECT' ? 'Direct' : 'Pipeline';
          grp.title = checkEvent.status === 'COMPLETE' ? `${prefix} Complete` : `${prefix} Failed`;
          grp.subtitle = checkEvent.status === 'COMPLETE' ? 'Done · verified result available' : 'Blocked · execution stopped';
        } else if (checkEvent.event === 'error') {
          const prefix = state.route === 'DIRECT' ? 'Direct' : 'Pipeline';
          grp.title = `${prefix} Intercept Error`;
          grp.subtitle = `Blocked · ${compactFailureReason(checkEvent.text, 'execution issue')}${attemptSuffix}`;
        } else {
          grp.title = 'SYSTEM · Council update';
          grp.subtitle = '';
        }
      }
    });

    // Apply Filter
    const filteredEntries = filter === 'ALL' 
      ? grouped 
      : grouped.filter(g => String(g.agent || 'system').toUpperCase() === filter);

    // Animation cursor: only cards BEYOND the previously rendered count are new.
    // When the user switches filter tabs, reset the cursor so the newly visible
    // set animates in once (intentional and expected for a tab switch).
    if (this._lastLogFilter !== filter) {
      this._lastLogFilter = filter;
      this._lastRenderedCount = 0;
    }
    const animStartIdx = this._lastRenderedCount || 0;
    this._lastRenderedCount = filteredEntries.length;

    if (filteredEntries.length) {
      html += '<div class="log-timeline-container">';
      try {
        filteredEntries.forEach((g, idx) => {
          const isLast = idx === filteredEntries.length - 1;
          const agentKey = String(g.agent || 'system').toLowerCase();
          const agentUpper = agentKey.toUpperCase();
          // Only truly new cards (beyond previous render count) get the entry animation.
          // Existing cards get --existing which has animation:none — prevents all cards
          // from re-animating on every SSE event during an active run.
          const cardClass = idx >= animStartIdx ? 'log-timeline-card' : 'log-timeline-card log-timeline-card--existing';

          // Role icon initials map
          const iconMap = { chair: 'CH', strategist: 'ST', implementer: 'IM', manager: 'MG', system: 'SY' };
          const initials = iconMap[agentKey] || agentUpper.slice(0, 2);

          // File chips HTML
          let filesHtml = '';
          if (g.files && g.files.length) {
            const chipClass = op => {
              if (op === 'WRITE') return 'log-file-chip log-file-chip--write';
              if (op === 'NEW')   return 'log-file-chip log-file-chip--new';
              return 'log-file-chip log-file-chip--read';
            };
            const basename = p => {
              const s = String(p || '');
              const last = s.replace(/\\/g, '/').split('/').pop();
              return last || s;
            };
            filesHtml = `
              <div class="log-chips-container">
                ${g.files.map(file => `
                  <button class="${chipClass(file.op)}" data-path="${_esc(file.path)}" title="${_esc(file.path)}">
                    <span class="log-chip-op">${_esc(file.op)}</span>
                    <span class="log-chip-name">${_esc(basename(file.path))}</span>
                  </button>`).join('')}
              </div>`;
          }

          // Attempts HTML (unchanged logic, new structure)
          let attemptsHtml = '';
          if (g.attempts && g.attempts.length) {
            attemptsHtml = `
              <div class="log-attempts-container">
                ${g.attempts.map((att, attIdx) => {
                  const statusColor = att.status === 'APPROVED' ? 'var(--council-success)' : 'var(--council-danger)';
                  return `
                    <div class="log-attempt-item">
                      <span class="log-attempt-prefix">Attempt ${attIdx + 1} —</span>
                      <span class="log-attempt-status" style="color:${statusColor}">
                        ${_esc(att.status)} ${att.status === 'APPROVED' ? '✓' : ''}
                      </span>
                      ${att.note ? `<span class="log-attempt-note">by ${_esc(att.note)}</span>` : ''}
                    </div>`;
                }).join('')}
              </div>`;
          }

          html += `
            <div class="log-timeline-row--v2">
              <div class="log-timeline-track--v2">
                <div class="log-role-icon" data-agent="${_esc(agentKey)}">${_esc(initials)}</div>
                ${!isLast ? '<div class="log-timeline-line--v2"></div>' : ''}
              </div>
              <div class="${cardClass}" data-agent="${_esc(agentKey)}">
                <div class="log-entry-meta">
                  <span class="log-entry-time">${_esc(g.time)}</span>
                  <span class="log-entry-agent-v2">
                    <span class="log-entry-agent-dot"></span>${agentUpper}
                  </span>
                </div>
                <div class="log-entry-title">${_esc(g.title)}</div>
                ${g.subtitle ? `<div class="log-entry-subtitle">${_esc(g.subtitle)}</div>` : ''}
                ${attemptsHtml}
                ${filesHtml}
              </div>
            </div>`;
        });
      } catch (renderErr) {
        // Fallback: plain-text render so sidebar never goes blank
        console.warn('[Council] _renderCaptainsLog v2 error — falling back to plain render:', renderErr);
        filteredEntries.forEach(g => {
          const agentUpper = String(g.agent || 'system').toUpperCase();
          html += `<div class="log-timeline-row"><div class="log-timeline-track"><div class="log-timeline-dot"></div></div><div class="log-timeline-content"><div class="log-entry-meta"><span class="log-entry-time">${_esc(g.time)}</span><span class="log-entry-agent">${agentUpper}</span></div><div class="log-entry-title">${_esc(g.title)}</div>${g.subtitle ? `<div class="log-entry-subtitle">${_esc(g.subtitle)}</div>` : ''}</div></div>`;
        });
      }
      html += '</div>';
    } else {
      html += '<div style="color:var(--log-text-muted);font-size:11px;text-align:center;margin-top:40px">— No events in this category —</div>';
    }

    // Skills & Reflections section
    const hasSkills = state.successSkills && state.successSkills.length > 0;
    const hasReflections = state.selfReflections && state.selfReflections.length > 0;
    if (hasSkills || hasReflections) {
      html += '<div class="log-skills-section">';
      if (hasSkills) {
        html += '<div class="log-skills-header">GENERATED SKILLS</div>';
        state.successSkills.forEach((skill, i) => {
          html += `
            <div class="log-skill-item">
              <span class="log-skill-name">${_esc(skill.name || 'Skill')}</span>
              <button class="log-skill-view-btn" data-skill-idx="${i}">View</button>
            </div>`;
        });
      }
      if (hasReflections) {
        html += '<div class="log-skills-header" style="margin-top:16px">SELF-REFLECTIONS</div>';
        state.selfReflections.forEach((ref, i) => {
          let lesson = ref.text;
          try {
            const parsed = JSON.parse(ref.text);
            lesson = parsed.lesson || lesson;
          } catch(e) {}
          html += `
            <div class="log-skill-item">
              <span class="log-skill-name">${_esc(String(lesson).slice(0, 120))}</span>
              <button class="log-reflection-view-btn" data-reflection-idx="${i}">View</button>
            </div>`;
        });
      }
      html += '</div>';
    }

    el.innerHTML = html;

    // Wire skills/reflection view buttons after DOM insertion
    el.querySelectorAll('.log-skill-view-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const idx = parseInt(e.target.dataset.skillIdx);
        this._openSkillInDocument(idx);
      });
    });
    el.querySelectorAll('.log-reflection-view-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const idx = parseInt(e.target.dataset.reflectionIdx);
        this._openReflectionInDocument(idx);
      });
    });

    if (keepPinnedToBottom) el.scrollTop = el.scrollHeight;
  }

  /* Open skill/reflection content in the right-side document panel */
  _openSkillInDocument(idx) {
    const skill = this._state.successSkills[idx];
    if (!skill) return;
    this._openInDocumentPanel(skill.name || 'Skill', skill.text || '');
  }

  _openReflectionInDocument(idx) {
    const ref = this._state.selfReflections[idx];
    if (!ref) return;
    const title = 'Self-Reflection';
    this._openInDocumentPanel(title, ref.text || '');
  }

  _openInDocumentPanel(title, content) {
    if (window.documentModule) {
      window.documentModule.openPanel();
      window.documentModule.streamDocOpen(String(title).slice(0, 100), 'markdown');
      window.documentModule.streamDocDelta(content || '');
      window.documentModule.streamDocFinalize();
    } else {
      console.warn('[Council] documentModule not available — cannot open skill/reflection in document panel');
    }
  }

  /* Review bar: show/hide + DIRECT skip indicator */
  _renderReviewBar(state) {
    const bar = document.getElementById('council-review-bar');
    if (!bar) return;

    const label = document.getElementById('council-review-text');
    const body = document.getElementById('review-plan-body');
    const actions = bar.querySelector('.review-actions');

    // DIRECT path completed — show muted skip indicator
    if (state.route === 'DIRECT' && state.status === 'COMPLETE') {
      bar.hidden = false;
      bar.className = 'review-bar review-bar-skipped';
      if (label) label.textContent = '⚡ APPROVAL GATE SKIPPED (DIRECT PATH)';
      if (body) body.textContent = '';
      if (actions) actions.style.display = 'none';
      return;
    }

    bar.classList.remove('review-bar-skipped');
    bar.hidden = !state.pendingReview;
    const approveButton = document.getElementById('council-approve-btn');
    const overrideButton = document.getElementById('council-override-btn');
    if (approveButton) approveButton.hidden = Boolean(state.pendingReviewRequiresOverride);
    if (overrideButton) {
      overrideButton.textContent = state.pendingReviewRequiresOverride
        ? 'Override Manager'
        : 'Override';
    }
    const planEvent = state.log.find(e => e.event === 'review_required');
    const planText = planEvent?.extra?.plan || '';
    if (body) {
      body.textContent = planText ? 'Plan preview: ' + planText.replace(/[\r\n]+/g, ' ').slice(0, 150) + '...' : '';
    }
    if (actions) actions.style.display = '';
  }

  /* Permission request bar: show/hide */
  _renderPermissionBar(state) {
    const bar = document.getElementById('council-permission-bar');
    if (!bar) return;
    bar.hidden = !state.pendingPermission;
    if (state.pendingPermission) {
      const label = document.getElementById('council-permission-label');
      const cmd = document.getElementById('council-permission-cmd');
      if (label) {
        label.textContent = `Security Permission Request: ${state.pendingPermission.action}`;
      }
      if (cmd) {
        cmd.textContent = state.pendingPermission.target;
      }
    }
  }

  /* Decision required bar: show/hide with option buttons */
  _renderDecisionBar(state) {
    const bar = document.getElementById('council-decision-bar');
    if (!bar) return;
    bar.hidden = !state.pendingDecision;
    if (state.pendingDecision) {
      const label = document.getElementById('council-decision-label');
      const body = document.getElementById('council-decision-body');
      if (label) {
        label.textContent = state.pendingDecision.question;
      }
      if (body) {
        const options = state.pendingDecision.options || ['Proceed'];
        let btnHtml = '';
        options.forEach((opt) => {
          btnHtml += `<button class="decision-btn" data-option="${_esc(opt)}">${_esc(opt)}</button>`;
        });
        body.innerHTML = btnHtml;
        // Wire up click handlers
        body.querySelectorAll('.decision-btn').forEach(btn => {
          btn.addEventListener('click', () => {
            const opt = btn.getAttribute('data-option');
            this._session.respondDecision(opt);
          });
        });
      }
    }
  }

  /* Complexity badge in left sidebar */
  _renderComplexityBadge(state) {
    const el = document.getElementById('council-complexity-badge');
    if (!el) return;
    if (state.complexity) {
      el.textContent = `${state.complexity} · ${_agentCount(state.complexity)} agents`;
      el.style.opacity = '';
    } else if (state.status === 'COMPLETE') {
      el.textContent = 'complete';
      el.style.opacity = '';
    } else {
      el.textContent = '';
      el.style.opacity = '0';
    }
  }

  /* Role-config popover — opens on diamond node click */
  openRolePopover(role, anchorEl) {
    Promise.all([
      fetch('/api/council/models').then(r => r.json()),
      fetch('/api/models').then(r => r.json()).catch(() => []),
    ]).then(([cfg, modelsResp]) => {
      const endpoints = Array.isArray(modelsResp) ? modelsResp : (modelsResp.items || []);
      const popover = document.getElementById('council-role-popover');
      if (!popover) return;

      // Make visible first to measure dimensions accurately
      popover.hidden = false;

      const rect = anchorEl.getBoundingClientRect();
      const viewportWidth = window.innerWidth;
      const viewportHeight = window.innerHeight;
      const popoverWidth = popover.offsetWidth || 220;
      const popoverHeight = popover.offsetHeight || 180;

      let top = rect.bottom + 8;
      let left = rect.left;

      // Adjust horizontally to stay inside viewport
      if (left + popoverWidth > viewportWidth - 16) {
        left = viewportWidth - popoverWidth - 16;
      }
      if (left < 16) {
        left = 16;
      }

      // Adjust vertically to stay inside viewport
      if (top + popoverHeight > viewportHeight - 16) {
        const topOption = rect.top - popoverHeight - 8;
        if (topOption > 16) {
          top = topOption;
        } else {
          top = viewportHeight - popoverHeight - 16;
        }
      }
      if (top < 16) {
        top = 16;
      }

      popover.style.top  = `${top}px`;
      popover.style.left = `${left}px`;

      // Merge: live override → models.json default → empty.
      // This ensures the popover pre-selects the configured model even when
      // no per-session override exists (prevents defaulting to first list item).
      const cfgDefault = (cfg.roles && cfg.roles[role]) || {};
      const override   = this._state.roleOverrides[role] || {};
      const current    = { ...cfgDefault, ...override };
      const modelSel  = document.getElementById('popover-model-select');
      const epSel     = document.getElementById('popover-endpoint-select');
      const tempRange = document.getElementById('popover-temp-range');
      const tempVal   = document.getElementById('popover-temp-val');

      if (epSel) {
        epSel.innerHTML = endpoints
          .map(ep => `<option value="${_esc(ep.url)}"${ep.url === current.endpoint_url ? ' selected' : ''}>${_esc(ep.endpoint_name || ep.url)}</option>`)
          .join('') || '<option value="">— no endpoints loaded —</option>';
      }

      const updateModelDropdown = () => {
        const selectedUrl = epSel ? epSel.value : '';
        const selectedEp = endpoints.find(ep => ep.url === selectedUrl) || null;
        if (modelSel) {
          const models = selectedEp ? (selectedEp.models || []) : [];
          modelSel.innerHTML = models
            .map(m => `<option value="${_esc(m)}"${m === current.model ? ' selected' : ''}>${_esc(m)}</option>`)
            .join('') || '<option value="">— no models —</option>';
        }
      };

      if (epSel) {
        epSel.onchange = updateModelDropdown;
      }
      updateModelDropdown();

      if (tempRange && current.temperature != null) {
        tempRange.value = current.temperature;
        if (tempVal) tempVal.textContent = current.temperature;
      }
      if (tempRange) {
        tempRange.oninput = () => { if (tempVal) tempVal.textContent = tempRange.value; };
      }

      const refreshBtn = document.getElementById('popover-refresh-btn');
      if (refreshBtn) {
        refreshBtn.onclick = () => {
          const selectedUrl = epSel ? epSel.value : '';
          const selectedEp = endpoints.find(ep => ep.url === selectedUrl) || null;
          if (!selectedEp || !selectedEp.endpoint_id) {
            if (window.uiModule?.showError) {
              window.uiModule.showError('No active provider endpoint found to refresh.');
            } else {
              alert('No active provider endpoint found to refresh.');
            }
            return;
          }

          const originalText = refreshBtn.textContent;
          refreshBtn.textContent = 'Refreshing...';
          refreshBtn.disabled = true;

          fetch(`/api/model-endpoints/${selectedEp.endpoint_id}/models?refresh=true`)
            .then(res => {
              if (!res.ok) throw new Error(`HTTP ${res.status}`);
              return fetch('/api/models');
            })
            .then(res => {
              if (!res.ok) throw new Error(`HTTP ${res.status}`);
              return res.json();
            })
            .then(modelsResp => {
              const newEndpoints = Array.isArray(modelsResp) ? modelsResp : (modelsResp.items || []);
              endpoints.length = 0;
              endpoints.push(...newEndpoints);
              updateModelDropdown();
              if (window.uiModule?.showToast) {
                window.uiModule.showToast('Provider models refreshed');
              }
            })
            .catch(err => {
              console.error('[Council] Provider refresh failed:', err);
              if (window.uiModule?.showError) {
                window.uiModule.showError('Failed to refresh models: ' + err.message);
              } else {
                alert('Failed to refresh models: ' + err.message);
              }
            })
            .finally(() => {
              refreshBtn.textContent = originalText;
              refreshBtn.disabled = false;
            });
        };
      }

      const saveBtn = document.getElementById('popover-save-btn');
      if (saveBtn) {
        saveBtn.onclick = () => {
          this._session.updateRoleOverride(role, {
            model:        modelSel?.value || "",
            endpoint_url: epSel?.value || "",
            temperature:  tempRange ? parseFloat(tempRange.value) : undefined,
          });
          popover.hidden = true;
        };
      }
      popover.hidden = false;
    });
  }

  /* Wire all interactive listeners */
  wireListeners(session) {
    // Compass node tactile interactions
    document.querySelectorAll('.compass-node').forEach(node => {
      const body = node.querySelector('.compass-node-body');
      if (!body) return;

      // Hover: handled by CSS :hover, but we add class for JS coordination
      node.addEventListener('mouseenter', () => {
        node.classList.add('compass-hover');
      });
      node.addEventListener('mouseleave', () => {
        node.classList.remove('compass-hover');
        body.classList.remove('compass-pressing');
      });

      // Tactile press
      node.addEventListener('mousedown', () => {
        body.classList.add('compass-pressing');
      });
      node.addEventListener('mouseup', () => {
        setTimeout(() => body.classList.remove('compass-pressing'), 80); // 80ms: matches .compass-pressing transition-duration (style.css)
      });

      // Click → open role popover
      node.addEventListener('click', e => {
        e.stopPropagation();
        this.openRolePopover(node.dataset.role, node);
      });
    });

    // Click events delegation (Dismiss popover + Retry/Skip tasks + Expand/Collapse cards)
    document.addEventListener('click', e => {
      const p = document.getElementById('council-role-popover');
      if (p && !p.contains(e.target) && !e.target.closest('.compass-node')) p.hidden = true;

      if (e.target.classList.contains('btn-retry-task')) {
        const taskId = e.target.dataset.task;
        session.respond('retry_task', taskId);
      }
      if (e.target.classList.contains('btn-skip-task')) {
        const taskId = e.target.dataset.task;
        session.respond('skip_task', taskId);
      }

      // Expand/collapse Chair/Manager summary text (.tx)
      const txEl = e.target.closest('.tx');
      if (txEl && txEl.getAttribute('data-full')) {
        const isExpanded = txEl.classList.toggle('exp');
        const fullText = txEl.getAttribute('data-full');
        const len = parseInt(txEl.getAttribute('data-len') || '70');
        const truncateText = (s, n = 70) => s.length > n ? s.slice(0, n) + "..." : s;
        if (isExpanded) {
          txEl.textContent = fullText;
        } else {
          txEl.textContent = truncateText(fullText, len);
        }
      }

      // Expand/collapse Strat task description (.tk)
      const tkEl = e.target.closest('.tk');
      if (tkEl && !e.target.classList.contains('tk-id')) {
        const tkdEl = tkEl.querySelector('.tk-d');
        if (tkdEl && tkdEl.getAttribute('data-full')) {
          const isExpanded = tkdEl.classList.toggle('exp');
          const fullText = tkdEl.getAttribute('data-full');
          const len = parseInt(tkdEl.getAttribute('data-len') || '60');
          const truncateText = (s, n = 60) => s.length > n ? s.slice(0, n) + "..." : s;
          if (isExpanded) {
            tkdEl.textContent = fullText;
          } else {
            tkdEl.textContent = truncateText(fullText, len);
          }
        }
      }

      // Expand/collapse Think line (.think-line)
      const thinkEl = e.target.closest('.think-line');
      if (thinkEl) {
        thinkEl.classList.toggle('exp');
      }

      // Expand/collapse Tool call card (.ghost-tool-header)
      const toolHeader = e.target.closest('.ghost-tool-header');
      if (toolHeader) {
        const card = toolHeader.closest('.ghost-tool-card');
        if (card) card.classList.toggle('open');
      }

      // Expand/collapse Burst group (.ghost-burst-header)
      const burstHeader = e.target.closest('.ghost-burst-header');
      if (burstHeader) {
        const group = burstHeader.closest('.ghost-burst-group');
        if (group) {
          const key    = group.getAttribute('data-burst-key') || '';
          const isOpen = group.getAttribute('data-burst-open') === '1';
          const nowOpen = !isOpen;
          group.setAttribute('data-burst-open', nowOpen ? '1' : '0');
          group.classList.toggle('burst-open', nowOpen);
          // Mirror state into the instance Set so the next innerHTML
          // re-render can restore the open state without a DOM query.
          if (key) {
            if (nowOpen) this._openBurstKeys.add(key);
            else         this._openBurstKeys.delete(key);
          }
        }
      }

      // Expand/collapse Strat extra tasks list
      const toggleBtn = e.target.closest('.strat-toggle-btn');
      if (toggleBtn) {
        const container = toggleBtn.previousElementSibling;
        if (container && container.classList.contains('strat-more-container')) {
          const isExpanded = container.style.display === 'flex';
          container.style.display = isExpanded ? 'none' : 'flex';
          toggleBtn.textContent = isExpanded 
            ? `+ Show ${container.querySelectorAll('.tk').length} more tasks ▾` 
            : `- Hide extra tasks ▴`;
        }
      }
    });

    // Review bar actions
    document.getElementById('council-approve-btn')?.addEventListener('click',
      () => session.respond('approve').catch(err => console.error('[Council] approval rejected:', err)));
    document.getElementById('council-override-btn')?.addEventListener('click',
      () => session.respond('override').catch(err => console.error('[Council] override rejected:', err)));

    // Permission bar actions
    const answerPermission = async (choice, persistLevel = 'once') => {
      const perm = this._state.pendingPermission;
      if (!perm) return;
      try {
        await session.respondPermission(choice, perm.permissionId, perm.target, persistLevel);
        this._state.pendingPermission = null;
        this._renderPermissionBar(this._state);
      } catch (err) {
        // Keep the gate visible when the server rejects a stale or interrupted
        // response.  Clearing it optimistically was the refresh/reconnect bug.
        console.error('[Council] permission response failed:', err);
        this._state.update({
          event: 'log',
          status: 'BLOCKED',
          text: 'Permission response failed; the request remains pending.',
          agent: 'system'
        });
      }
    };
    document.getElementById('council-perm-allow-btn')?.addEventListener('click',
      () => answerPermission('allow', 'once'));
    document.getElementById('council-perm-project-btn')?.addEventListener('click',
      () => answerPermission('allow', 'project'));
    document.getElementById('council-perm-global-btn')?.addEventListener('click',
      () => answerPermission('allow', 'global'));
    document.getElementById('council-perm-deny-btn')?.addEventListener('click',
      () => answerPermission('deny'));

    // Cancel (✕ in log header) — cancels the running session
    document.getElementById('council-cancel-btn')?.addEventListener('click',
      () => session.respond('cancel'));
    document.getElementById('council-run-cancel-btn')?.addEventListener('click',
      () => session.respond('cancel'));

    // Revert & Restart from Delta / Halt Pipeline dual behavior
    document.getElementById('council-revert-btn')?.addEventListener('click', () => {
      const state = this._state;
      if (state.status === 'IN_PROGRESS' || state.status === 'BLOCKED') {
        session.respond('cancel');
      } else if (state.status === 'COMPLETE') {
        const textarea = document.getElementById('message');
        if (textarea && state.log.length) {
          const brief = state.chairBrief?.text || '';
          if (brief) textarea.value = brief;
        }
      }
    });

    // Wire log filters (pill) + file chip clipboard — single delegated listener, no double-bind
    document.getElementById('council-captains-log')?.addEventListener('click', e => {
      // Pill tab filter
      const pill = e.target.closest('.log-filter-pill');
      if (pill) {
        this._logFilter = pill.dataset.filter || 'ALL';
        this.render(this._state);
        return;
      }
      // Legacy filter btn (kept for backward safety during any cached page load)
      const legacyBtn = e.target.closest('.log-filter-btn');
      if (legacyBtn) {
        this._logFilter = legacyBtn.dataset.filter || 'ALL';
        this.render(this._state);
        return;
      }
      // File chip — copy full path to clipboard
      const chip = e.target.closest('.log-file-chip');
      if (chip) {
        const path = chip.dataset.path;
        if (path && navigator.clipboard) {
          // Read path before async — the chip node may be orphaned by the next
          // innerHTML re-render before the .then() callback fires.
          const pathSnapshot = path;
          navigator.clipboard.writeText(pathSnapshot).catch(() => {/* insecure context — silent */});
          // Visual feedback: brief opacity pulse on the chip itself (if still mounted)
          // Uses requestAnimationFrame to stay in the current paint frame.
          const target = chip;
          target.style.opacity = '0.5';
          setTimeout(() => { if (target.isConnected) target.style.opacity = ''; }, 600);
        }
      }
    });

    // Promote to Canvas
    document.getElementById('council-promote-btn')?.addEventListener('click', async () => {
      const state = this._state;
      if (!state.lastCode) return;
      try {
        const res = await fetch('/api/document', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({
            session_id: state.sessionId,
            title:    state.lastFile || 'Council Output',
            content:  state.lastCode,
            language: 'auto',
          }),
        });
        if (res.ok) {
          const btn = document.getElementById('council-promote-btn');
          if (btn) { btn.textContent = '✓ Promoted'; setTimeout(() => { btn.textContent = 'Promote to Canvas ↗'; }, 2500); }
        }
      } catch(e) { console.error('[Council] promote failed:', e); }
    });

    // Enable button based on active/complete state
    this._state.subscribe(s => {
      const revert = document.getElementById('council-revert-btn');
      if (revert) {
        revert.disabled = (s.status !== 'IN_PROGRESS' && s.status !== 'BLOCKED' && s.status !== 'COMPLETE');
      }
    });
  }
}

/* ─── Helpers ────────────────────────────────────────────────────── */
function cleanLogText(text, agent = '') {
  if (!text) return '';
  let clean = text.trim();

  // Strip markdown tasks block
  clean = clean.replace(/```tasks[\s\S]*?```/gi, '');
  clean = clean.replace(/```json\s*\[[\s\S]*?\]\s*```/gi, '');
  clean = clean.replace(/^\s*\[[\s\S]*?\]\s*$/g, '');

  // Try parsing json
  try {
    let jsonStr = clean;
    if (jsonStr.includes('```json')) {
      jsonStr = jsonStr.split('```json')[1].split('```')[0].trim();
    } else if (jsonStr.includes('```')) {
      jsonStr = jsonStr.split('```')[1].split('```')[0].trim();
    }
    if (jsonStr.startsWith('{') && jsonStr.endsWith('}')) {
      const data = JSON.parse(jsonStr);
      const keys = ['reason', 'summary', 'notes', 'thought', 'thinking', 'text'];
      for (const k of keys) {
        if (data[k] && typeof data[k] === 'string') {
          return data[k].trim();
        }
      }
    }
  } catch(e) {}

  // Try target keys via regex
  const targetKeys = ['reason', 'summary', 'notes', 'thought', 'thinking', 'text'];
  for (const key of targetKeys) {
    const regex = new RegExp(`"${key}"\\s*:\\s*"`, 'i');
    const match = regex.exec(clean);
    if (match) {
      const startIndex = match.index + match[0].length;
      const remainder = clean.slice(startIndex);
      let endIdx = -1;
      let escaped = false;
      for (let i = 0; i < remainder.length; i++) {
        if (escaped) {
          escaped = false;
        } else if (remainder[i] === '\\') {
          escaped = true;
        } else if (remainder[i] === '"') {
          endIdx = i;
          break;
        }
      }
      let val = endIdx !== -1 ? remainder.slice(0, endIdx) : remainder;
      return val.replace(/\\"/g, '"').replace(/\\n/g, '\n').trim();
    }
  }

  // Strip general wrapper noise
  clean = clean.replace(/^```(json|tasks)?\s*/i, '');
  clean = clean.replace(/```$/, '');
  clean = clean.trim();
  if (clean.startsWith('{') && clean.endsWith('}')) {
    clean = clean.slice(1, -1).trim();
  }

  return clean.replace(/\s+/g, ' ').trim();
}

// Deterministic, display-only task labels. The implementation prompt remains
// available to the agent, but the primary UI gets a short noun instead of a
// copied prompt fragment. This intentionally avoids another model call.
function compactTaskLabel(description, taskId = '') {
  let clean = String(description || '')
    .replace(/`[^`]*`/g, ' ')
    .replace(/\b[A-Za-z]:[\\/][^\s,;)]*/g, ' ')
    .replace(/\b(?:projects|workspace|data|assets|src)\/[^\s,;)]*/gi, ' ')
    .replace(/\([^)]*\)/g, ' ')
    .replace(/\[[^\]]*\]/g, ' ')
    .replace(/["'“”‘’]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const lower = clean.toLowerCase();
  const rules = [
    [/flight|airline|route|simulation/, 'Flights'],
    [/three\.js|threejs|globe|earth|webgl|sphere/, 'Globe'],
    [/style\.css|stylesheet|glassmorphism|dark theme|typography|responsive/, 'Theme'],
    [/server|http\.server|cors|mime type/, 'Server'],
    [/readme|documentation|setup instructions/, 'Docs'],
    [/test|verification|validate|checks?/, 'Checks'],
    [/app\.js|orchestrat|animation loop|domcontentloaded/, 'App'],
    [/index\.html|stats panel|sidebar|interface|ui/, 'Interface'],
    [/scaffold|skeleton|project root|directories|structure/, 'Scaffold']
  ];
  for (const [pattern, label] of rules) {
    if (pattern.test(lower)) return label;
  }
  const phrase = clean
    .replace(/^(create|build|implement|add|define|set up|write|update|wire|configure|serve|document|test)\s+/i, '')
    .replace(/\b(with|containing|including|that|which)\b[\s\S]*$/i, '')
    .trim();
  if (phrase) {
    const words = phrase.split(/\s+/).filter(Boolean).slice(0, 2);
    if (words.length) return words.map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
  }
  return taskId ? 'Task' : 'Work';
}

function compactFailureReason(text, fallback = 'execution issue') {
  const lower = String(text || '').toLowerCase();
  if (/timeout|timed out/.test(lower)) return 'timeout';
  if (/permission|denied|forbidden/.test(lower)) return 'permission required';
  if (/verification|compiler|syntax|test/.test(lower)) return 'verification failed';
  if (/context|token|overflow/.test(lower)) return 'context limit reached';
  return fallback;
}

function compactFailureCode(code, text) {
  const lower = `${String(code || '')} ${String(text || '')}`.toLowerCase();
  if (/timeout|timed out|busy/.test(lower)) return 'TIMEOUT';
  if (/permission|denied|forbidden/.test(lower)) return 'PERMISSION';
  if (/context|token|overflow/.test(lower)) return 'CONTEXT';
  if (/verification|compiler|syntax|test/.test(lower)) return 'CHECK';
  return 'ERROR';
}

function compactToolIntent(tool, args = {}, cmd = '') {
  const t = String(tool || '').toLowerCase();
  if (t === 'glob' || t === 'grep' || t === 'ls') return 'Inspect workspace';
  if (t === 'read_file') return 'Read source';
  if (t === 'write_file' || t === 'edit_file') return 'Update files';
  if (t === 'bash' || t === 'python') {
    const raw = String(args?.command || args?.code || cmd || '').toLowerCase();
    if (/test|pytest|check|lint|compile|verify/.test(raw)) return 'Run checks';
    if (/install|build|bundle/.test(raw)) return 'Build project';
    return 'Run command';
  }
  return 'Run operation';
}

function compactIssueTheme(issue) {
  const text = String(issue?.description || issue?.suggestion || '').toLowerCase();
  if (/memory|leak|unbounded|object creation/.test(text)) return 'memory lifecycle';
  if (/security|sri|integrity|csp|xss|bind address|expos/.test(text)) return 'security hardening';
  if (/performance|fps|pixel ratio|latency|render/.test(text)) return 'render performance';
  if (/test|coverage|validation/.test(text)) return 'test coverage';
  if (/maintain|global namespace|magic number|config/.test(text)) return 'maintainability';
  return 'implementation detail';
}

function executionWaveCount(tasks) {
  const byId = new Map((tasks || []).map(t => [String(t.i), t]));
  const memo = new Map();
  const depth = (id, trail = new Set()) => {
    if (memo.has(id)) return memo.get(id);
    if (trail.has(id)) return 0;
    const node = byId.get(id);
    const deps = Array.isArray(node?.dp) ? node.dp : [];
    const nextTrail = new Set(trail).add(id);
    const value = deps.length ? 1 + Math.max(...deps.map(dep => depth(String(dep), nextTrail))) : 1;
    memo.set(id, value);
    return value;
  };
  return Math.max(1, ...Array.from(byId.keys()).map(id => depth(id)));
}

function compactAgentActivity(agent, tool = '') {
  const role = String(agent || 'agent').toLowerCase();
  if (tool) return `${role.charAt(0).toUpperCase() + role.slice(1)} is using a tool`;
  if (role === 'strategist') return 'Constructing execution plan';
  if (role === 'manager') return 'Reviewing execution plan';
  if (role === 'implementer') return 'Executing assigned task';
  if (role === 'chair') return 'Assessing request';
  return 'Council is working';
}

const COMPACT_LOG_EVENTS = new Set([
  'thought', 'active_agent', 'error', 'complete', 'dag_update',
  'plan_created', 'task_status_update', 'tool_start', 'tool_output'
]);

function sanitizeLogEntry(data, logText) {
  if (!data || typeof data !== 'object') return null;
  const presentation = data.extra?.presentation;
  const visibleText = COMPACT_LOG_EVENTS.has(data.event) && presentation?.summary
    ? String(presentation.summary)
    : (typeof logText === 'string' ? logText : '');
  return {
    ts: typeof data.timestamp === 'string' ? data.timestamp : (typeof data.ts === 'string' ? data.ts : new Date().toISOString()),
    agent: typeof data.agent === 'string' ? data.agent : 'system',
    text: visibleText,
    event: typeof data.event === 'string' ? data.event : 'log',
    status: typeof data.status === 'string' ? data.status : null,
    code: typeof data.code === 'string' ? data.code : null,
    file_path: typeof data.file_path === 'string' ? data.file_path : null,
    exit_code: typeof data.exit_code === 'number' ? data.exit_code : (typeof data.extra?.exit_code === 'number' ? data.extra.exit_code : null),
    extra: data.extra && typeof data.extra === 'object' ? { ...data.extra } : {}
  };
}

function _esc(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// Render streamed LLM thought text as structured markdown (headers, lists,
// tables, code, paragraphs) instead of raw escaped text. Mirrors chat.js'
// streaming path; falls back to escaped text if the parser throws on a
// partial chunk. Used for the ghost editor "System Thought" / chair blocks.
function _ghostMd(text) {
  const s = String(text || '');
  if (!s) return '';
  try { return markdownModule.mdToHtml(markdownModule.squashOutsideCode(s)); }
  catch (e) { return _esc(s); }
}

function sameFileOperation(left, right) {
  if (!left || !right || left.op !== right.op) return false;
  const normalize = path => String(path || '').replace(/\\/g, '/').replace(/^\.\//, '').replace(/\/+$/, '');
  const a = normalize(left.path);
  const b = normalize(right.path);
  return a === b || (b.includes('/') && a.endsWith(`/${b}`)) || (a.includes('/') && b.endsWith(`/${a}`));
}

function parseFileOperations(item) {
  if (!item) return null;
  if (item.event === 'code_update' && item.file_path) {
    return [{ op: 'WRITE', path: String(item.file_path) }];
  }
  let tool = item.event?.startsWith('tool_') ? item.extra?.tool : null;
  if (!tool && item.event === 'task_status_update') {
    if (item.file_path) {
      return [{ op: 'WRITE', path: String(item.file_path) }];
    }
  }
  if (!tool) return null;
  
  // Safe extraction and string coercion of command
  let rawCommand = item.extra?.command;
  let command = typeof rawCommand === 'string' ? rawCommand : (rawCommand != null ? String(rawCommand) : '');
  let path = '';

  // Prefer structured args.path if available (emitted by backend C-2 fix); more reliable than parsing command string.
  if (item.extra?.args && typeof item.extra.args.path === 'string' && item.extra.args.path) {
    path = item.extra.args.path;
  }

  if (!path && command.trim().startsWith('{')) {
    try {
      let parsed = JSON.parse(command);
      if (parsed && typeof parsed === 'object') {
        let rawPath = parsed.path || parsed.TargetFile || parsed.AbsolutePath || parsed.Target || '';
        path = typeof rawPath === 'string' ? rawPath : String(rawPath);
      }
    } catch(e) {}
  }

  if (!path) {
    if (tool === 'write_file' || tool === 'read_file') {
      let lines = command.split('\n');
      if (lines.length > 0) path = lines[0].trim();
    }
  }
  
  if (path) {
    let cleanPath = String(path).replace(/\\/g, '/');
    let parts = cleanPath.split('/');
    let displayPath = parts.length > 1 ? parts.slice(-2).join('/') : parts[0];
    
    let op = 'READ';
    if (['write_file', 'write_to_file', 'replace_file_content', 'multi_replace_file_content', 'edit_file'].includes(tool)) {
      op = 'WRITE';
    }
    return [{ op, path: displayPath }];
  }
  return null;
}

function getTaskAttempts(taskId, log) {
  let attempts = [];
  let taskEvents = log.filter(e => e.event === 'task_status_update' && e.extra?.task_id === taskId);
  
  taskEvents.forEach((e, idx) => {
    let status = e.extra?.task_status;
    if (status === 'FAILED') {
      attempts.push({ status: 'REJECTED', note: 'Compiler/Error' });
    } else if (status === 'DONE') {
      attempts.push({ status: 'APPROVED', note: '' });
    }
  });
  
  return attempts;
}
function _statusWord(status) {
  const map = { IN_PROGRESS: 'working', BLOCKED: 'waiting', COMPLETE: 'done', FAILED: 'error', PENDING: 'idle' };
  return map[status] || status.toLowerCase();
}
function _fmtElapsed(ms) {
  if (!Number.isFinite(ms) || ms < 0) ms = 0;
  const secs = Math.floor(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  return `${mins}m ${secs % 60}s`;
}
function _agentCount(complexity) {
  return { SIMPLE: 2, MEDIUM: 3, COMPLEX: 4 }[complexity] || 3;
}

function initResizers() {
  try {
    const resizerCodeGhost = document.getElementById('council-resizer-code-ghost');
    const resizerCenterLog = document.getElementById('council-resizer-center-log');

    if (resizerCodeGhost) {
      setupResizer(resizerCodeGhost, (dx) => {
        const leftPane = document.querySelector('.council-code-pane');
        const rightPane = document.querySelector('.council-ghost-pane');
        if (!leftPane || !rightPane) return;
        
        const parentWidth = leftPane.parentElement.clientWidth;
        if (!parentWidth) return;
        
        const currentRightWidth = rightPane.getBoundingClientRect().width;
        const newRightWidth = Math.max(150, Math.min(parentWidth - 150, currentRightWidth - dx));
        const rightPercent = (newRightWidth / parentWidth) * 100;
        
        rightPane.style.flex = `0 0 ${rightPercent}%`;
      });
    }

    if (resizerCenterLog) {
      setupResizer(resizerCenterLog, (dx) => {
        const leftPane = document.querySelector('.council-center');
        const rightPane = document.querySelector('.council-log-sidebar');
        if (!leftPane || !rightPane) return;

        const parentWidth = leftPane.parentElement.clientWidth;
        if (!parentWidth) return;
        
        const currentRightWidth = rightPane.getBoundingClientRect().width;
        const newRightWidth = Math.max(200, Math.min(parentWidth - 200, currentRightWidth - dx));
        const rightPercent = (newRightWidth / parentWidth) * 100;
        
        rightPane.style.flex = `0 0 ${rightPercent}%`;
      });
    }
  } catch (err) {
    console.error('[Council] Failed to initialize resizers:', err);
  }
}

function setupResizer(resizer, onDrag) {
  let startX;

  resizer.addEventListener('mousedown', (e) => {
    e.preventDefault();
    startX = e.clientX;
    resizer.classList.add('resizing');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    function onMouseMove(moveEvent) {
      try {
        const dx = moveEvent.clientX - startX;
        startX = moveEvent.clientX;
        onDrag(dx);
      } catch (err) {
        console.error('[Council] Drag handling error:', err);
      }
    }

    function onMouseUp() {
      resizer.classList.remove('resizing');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
    }

    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  });
}

/* ─── Entry point ────────────────────────────────────────────────── */
let _initialized = false;

export function init() {
  if (_initialized) return;
  _initialized = true;

  const state   = new CouncilState();
  const session = new CouncilSession(state);
  const ui      = new CouncilUI(state, session);

  // Single subscription: any state change → full re-render
  state.subscribe((s, ev) => ui.scheduleRender(s, ev));

  ui.wireListeners(session);

  // Setup resizers
  initResizers();

  // Initial render to set correct idle state
  ui.render(state);

  // Restore workspace input from localStorage
  const wsInput = document.getElementById('council-workspace-input');
  if (wsInput) {
    try {
      const saved = localStorage.getItem('councilWorkspace');
      if (saved) wsInput.value = saved;
    } catch {}
    wsInput.addEventListener('change', () => {
      try { localStorage.setItem('councilWorkspace', wsInput.value.trim()); } catch {}
    });
  }

  // Restore budget input from localStorage
  const budgetInput = document.getElementById('council-budget-input');
  if (budgetInput) {
    try {
      const saved = localStorage.getItem('councilBudget');
      if (saved) budgetInput.value = saved;
    } catch {}
    budgetInput.addEventListener('change', () => {
      try { localStorage.setItem('councilBudget', budgetInput.value.trim()); } catch {}
    });
  }

  // Setup 1s timer to update LAST RESPONSE indicator
  setInterval(() => {
    // Keep the active-agent elapsed timer ticking even when no events arrive,
    // so a long-running step reads as "working · 2m14s" rather than frozen.
    if (state.status === 'IN_PROGRESS' || state.status === 'BLOCKED') {
      const agentEl = document.getElementById('council-ghost-agent');
      if (agentEl && state.activeAgent && state.activeAgentSince) {
        agentEl.textContent = ui._ghostBadgeText(state);
      }
    }

    const el = document.getElementById('council-last-response');
    if (!el) return;
    if (!state.lastResponseTime || state.status === 'PENDING' || state.status === 'COMPLETE' || state.status === 'FAILED' || state.status === 'CANCELLED') {
      el.textContent = '—';
      el.style.color = 'var(--log-text-dim)';
      return;
    }
    const diff = Math.floor((Date.now() - state.lastResponseTime) / 1000);
    if (diff < 5) {
      el.textContent = 'just now';
      el.style.color = 'var(--log-text-dim)';
    } else if (diff < 60) {
      el.textContent = `${diff}s ago`;
      el.style.color = 'var(--log-text-dim)';
    } else {
      const mins = Math.floor(diff / 60);
      const secs = diff % 60;
      el.textContent = `${mins}m ${secs}s ago`;
      if (diff >= 120) {
        el.style.color = 'var(--sys, #e05858)';
      } else {
        el.style.color = 'var(--warn, #df8e45)';
      }
    }
  }, 1000);

  // Expose on window so other modules can switch/load sessions
  window.councilController = {
    state, session, ui,
    startFromInput(text) {
      if (!text) return;
      const wsEl = document.getElementById('council-workspace-input');
      state.workspace = wsEl ? wsEl.value.trim() : '';
      try { localStorage.setItem('councilWorkspace', state.workspace); } catch {}
      
      const budgetEl = document.getElementById('council-budget-input');
      const rawBudget = budgetEl ? budgetEl.value.trim() : '';
      state.contextBudget = rawBudget ? Number(rawBudget) : 0;
      try { localStorage.setItem('councilBudget', rawBudget); } catch {}

      state.thoughts   = '';
      state.activeTool = null;
      state.lastCode   = '';
      state.lastFile   = '';
      state.generatedFiles = {};
      state.selectedFile = '';
      state.log        = [];
      state.chairBrief = null;
      state.complexity = null;
      state.status     = 'IN_PROGRESS';
      state.activeAgent = null;
      state.activeAgentSince = Date.now();
      state.route      = null;
      state.pendingReview = false;
      state.pendingPermission = null;
      state.dag = null;
      state.showDAG = false;
      state.showThinking = false;
      ui.render(state);
      session.start(text).then(() => {
        if (window.sessionModule) {
          window.sessionModule.loadSessions().then(() => {
            window.sessionModule.selectSession(state.sessionId);
          });
        }
      }).catch(e => console.error('[Council] start failed:', e));
    }
  };
}

export default { init };
