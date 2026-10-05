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
    this.connectionState = 'disconnected'; // 'connected' | 'reconnecting' | 'disconnected'
    this.fileVersions = {};           // filepath -> { original: string, current: string }
    this.telemetry = null;
    this._listeners    = new Set();
  }

  update(data) {
    if (!data || typeof data !== 'object') return;

    // Timestamp hygiene: do not stamp lastResponseTime on empty or cosmetic updates.
    // Only stamp when data carries genuine progress, events, or heartbeats.
    const keys = Object.keys(data);
    const isCosmeticOrEmpty = keys.length === 0 || keys.every(k => (
      k === 'showThinking' || k === 'showDAG' || k === 'selectedFile' || k === 'connectionState'
    ));
    const hasGenuineProgress = Boolean(
      data.event ||
      data.status ||
      data.agent ||
      data.active_agent ||
      data.activeTool ||
      data.text ||
      data.code ||
      data.dag ||
      data.complexity ||
      data.completeness !== undefined
    );
    if (!isCosmeticOrEmpty && hasGenuineProgress) {
      this.lastResponseTime = Date.now();
    }

    if (data.connectionState) this.connectionState = String(data.connectionState);

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
        if (!this.fileVersions[this.lastFile]) {
          const original = data.original_code || data.extra?.original_code || (this.generatedFiles[this.lastFile] !== undefined ? this.generatedFiles[this.lastFile] : '');
          this.fileVersions[this.lastFile] = { original, current: this.lastCode };
        } else {
          this.fileVersions[this.lastFile].original = this.fileVersions[this.lastFile].current;
          this.fileVersions[this.lastFile].current = this.lastCode;
        }
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

/* ─── Telemetry Engine (Decoupled 250ms interval) ─────────────────── */
class CouncilTelemetry {
  constructor(state) {
    this._state = state;
    this._interval = null;
    this._tokenDeltas = []; // [{ time: number, tokens: number }]
    this._totalBytes = 0;
    this._speed = 0;
  }

  start() {
    if (this._interval) return;
    this._interval = setInterval(() => this.tick(), 250);
  }

  stop() {
    if (this._interval) {
      clearInterval(this._interval);
      this._interval = null;
    }
  }

  reset() {
    this._tokenDeltas = [];
    this._totalBytes = 0;
    this._speed = 0;
    this.updateUI();
  }

  recordDelta(text) {
    if (!text || typeof text !== 'string') return;
    const bytes = new TextEncoder().encode(text).length;
    this._totalBytes += bytes;
    // Estimate tokens from text (~3.8 chars per token)
    const tokens = Math.max(1, Math.round(text.length / 3.8));
    this._tokenDeltas.push({ time: Date.now(), tokens });
  }

  recordRawBytes(bytes) {
    if (typeof bytes === 'number' && bytes > 0) {
      this._totalBytes += bytes;
    }
  }

  tick() {
    const now = Date.now();
    const cutoff = now - 2000; // 2.0s rolling window
    this._tokenDeltas = this._tokenDeltas.filter(d => d.time >= cutoff);

    const isRunning = this._state.status === 'IN_PROGRESS' || this._state.status === 'BLOCKED';
    if (!isRunning || this._tokenDeltas.length === 0) {
      this._speed = 0;
    } else {
      const sumTokens = this._tokenDeltas.reduce((acc, d) => acc + d.tokens, 0);
      const oldest = this._tokenDeltas[0].time;
      const spanSec = Math.max(0.5, Math.min(2.0, (now - oldest) / 1000));
      this._speed = sumTokens / spanSec;
    }
    this.updateUI();
  }

  _formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
  }

  updateUI() {
    const speedEl = document.getElementById('council-stream-speed');
    const bufferEl = document.getElementById('council-stream-buffer');
    if (speedEl) {
      speedEl.textContent = `${Math.round(this._speed)} tok/s`;
    }
    if (bufferEl) {
      bufferEl.textContent = this._formatBytes(this._totalBytes);
    }
  }
}

/* ─── Session (API + SSE) ────────────────────────────────────────── */
class CouncilSession {
  constructor(state) {
    this._state = state;
    this._es    = null;
    this._councilHandler = null;
  }

  startStream(sessionId) {
    if (this._es) {
      if (this._councilHandler) {
        this._es.removeEventListener('council_event', this._councilHandler);
      }
      this._es.close();
      this._es = null;
    }
    this._state.connectionState = 'connecting';
    this._state.update({ event: 'connection_state', connectionState: 'connecting' });

    this._es = new EventSource(`/api/council/stream/${sessionId}`);

    this._es.onopen = () => {
      this._state.connectionState = 'connected';
      this._state.update({ event: 'connection_state', connectionState: 'connected' });
    };

    this._councilHandler = e => {
      this._state.connectionState = 'connected';
      try {
        const data = JSON.parse(e.data);
        if (this._state.telemetry) {
          const rawByteLen = new TextEncoder().encode(e.data).length;
          this._state.telemetry.recordRawBytes(rawByteLen);
          if (data.event === 'thought_delta' && data.text) {
            this._state.telemetry.recordDelta(data.text);
          }
        }
        this._state.update(data);
      } catch (err) {
        console.error('[Council] SSE parse error:', err);
      }
    };
    this._es.addEventListener('council_event', this._councilHandler);

    this._es.onerror = () => {
      // Let native EventSource retry automatically upon transient network drops.
      // Do not close the stream or mark status as FAILED!
      if (this._es && this._es.readyState === EventSource.CONNECTING) {
        this._state.connectionState = 'reconnecting';
        this._state.update({ event: 'connection_state', connectionState: 'reconnecting' });
      } else {
        this._state.connectionState = 'disconnected';
        this._state.update({ event: 'connection_state', connectionState: 'disconnected' });
      }
    };

    this._es.onmessage = e => {
      if (e.data === '[DONE]') {
        this._es.close();
        this._es = null;
        // The stream closed without a terminal event. A run that never left
        // IN_PROGRESS/BLOCKED was stopped (the worker emits its own CANCELLED
        // event, but it can be raced by the stream teardown), so settle the
        // state here rather than leaving the UI reporting a run that is over.
        const unfinished = this._state.status === 'IN_PROGRESS' || this._state.status === 'BLOCKED';
        this._state.update({
          event: 'connection_state',
          connectionState: 'disconnected',
          ...(unfinished ? { status: 'CANCELLED' } : {})
        });
      }
    };
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

    this.startStream(session_id);
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
      if (!res.ok) {
        if (res.status === 404) {
          console.warn(`[Council] Session ${sessionId} not found (404). Resetting to clean state.`);
          if (window.location.hash.replace('#', '') === sessionId) {
            history.replaceState(null, '', window.location.pathname + window.location.search);
          }
          try {
            if (localStorage.getItem('lastSessionId') === sessionId) {
              localStorage.removeItem('lastSessionId');
            }
          } catch {}
          if (window.sessionModule && window.sessionModule.getCurrentSessionId() === sessionId) {
            window.sessionModule.setCurrentSessionId(null);
          }
          this._state.sessionId = null;
          this._state.status = 'PENDING';
          this._state.update({});
          return;
        }
        throw new Error(await res.text());
      }
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
        this.startStream(sessionId);
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
    this._state.connectionState = 'disconnected';
    if (this._es) {
      if (this._councilHandler) {
        this._es.removeEventListener('council_event', this._councilHandler);
      }
      this._es.close();
      this._es = null;
    }
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
    // _compileImplementerFiles derives an immutable display snapshot from the
    // current log/DAG. Cache it between structural renders so repeated DAG
    // events do not rescan the entire session log inside the block-building
    // pass. reset() clears this explicitly at the session boundary.
    this._implementerFilesCache = null;
    this._autoFollow = true;
    this._dagOverlayOpen = false;
    this._activeThoughtExpanded = false;
    this._telemetry = new CouncilTelemetry(this._state);
    this._state.telemetry = this._telemetry;
    this._telemetry.start();
  }

  /* ── Particle engine: dots flowing along the active edge ── */
  // ponytail: simplify to CSS offset-path animation
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
    this._implementerFilesCache = null;
    this._telemetry?.reset();
    this._autoFollow = true;
    this._activeThoughtExpanded = false;
    this._state.fileVersions = {};
    if (this._dagOverlayOpen) {
      this._toggleDagOverlay(false);
    }
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

  // ponytail: pre-compute totalChars in state.update() to make this O(1)
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
    const meta = _roleMeta(state.activeAgent);
    if (status === 'FAILED') {
      stageEl.textContent = state.error ? `Error: ${state.error}` : 'Execution terminated with error';
    } else if (status === 'COMPLETE') {
      stageEl.textContent = 'All steps verified complete';
    } else if (status === 'CANCELLED') {
      stageEl.textContent = 'Run stopped by user';
    } else if (status === 'BLOCKED') {
      stageEl.textContent = `${meta.name || 'Manager'} › awaiting review`;
    } else {
      if (state.activeTool) {
        stageEl.textContent = `${meta.name} › running ${state.activeTool}`;
      } else if (state.activeAgent) {
        stageEl.textContent = `${meta.name} › thinking`;
      } else {
        stageEl.textContent = 'Waiting for a task';
      }
    }
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
    const isTerminal = state.status === 'COMPLETE' || state.status === 'FAILED' || state.status === 'CANCELLED';

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



  /* Helper to compile real file stats using computeLineDiff and state.fileVersions */
  _getFileStats(name, state, diffMemo = null) {
    if (!name) return { lines: 0, added: 0, removed: 0 };
    if (diffMemo && diffMemo.has(name)) return diffMemo.get(name);

    let stats = { lines: 0, added: 0, removed: 0 };

    // 1. Check state.fileVersions for real version history with flexible path resolution
    const fileVer = state.fileVersions
      ? (state.fileVersions[name] ||
         Object.entries(state.fileVersions).find(([k]) => k.endsWith(name) || name.endsWith(k))?.[1])
      : null;

    if (fileVer && fileVer.current != null) {
      const orig = fileVer.original != null ? String(fileVer.original) : '';
      const curr = String(fileVer.current);
      const currLines = curr.split('\n');
      if (orig && typeof computeLineDiff === 'function') {
        const diff = computeLineDiff(orig, curr);
        const added = diff.filter(d => d.type === 'add').length;
        const removed = diff.filter(d => d.type === 'del').length;
        stats = { lines: currLines.length, added, removed };
      } else {
        stats = { lines: currLines.length, added: currLines.length, removed: 0 };
      }
    } else {
      // 2. Check code_update events in state.log
      const log = Array.isArray(state.log) ? state.log : [];
      const updates = log.filter(e => e && e.event === 'code_update' && (e.file_path === name || (e.file_path && (e.file_path.endsWith(name) || name.endsWith(e.file_path)))));
      if (updates.length > 0) {
        const latest = updates[updates.length - 1];
        const latestCode = latest?.code != null ? String(latest.code) : '';
        const latestLines = latestCode.split('\n');

        if (updates.length > 1) {
          const prev = updates[updates.length - 2];
          const prevCode = prev?.code != null ? String(prev.code) : '';
          if (typeof computeLineDiff === 'function') {
            const diff = computeLineDiff(prevCode, latestCode);
            const added = diff.filter(d => d.type === 'add').length;
            const removed = diff.filter(d => d.type === 'del').length;
            stats = { lines: latestLines.length, added, removed };
          }
        } else {
          stats = { lines: latestLines.length, added: latestLines.length, removed: 0 };
        }
      }
    }

    if (diffMemo) diffMemo.set(name, stats);
    return stats;
  }

  /* Helper to compile implementer file actions from log tool calls and completed DAG tasks */
  _compileImplementerFiles(state) {
    const log = Array.isArray(state.log) ? state.log : [];
    const cache = this._implementerFilesCache;
    if (
      cache &&
      cache.state === state &&
      cache.log === log &&
      cache.logLength === log.length &&
      cache.dag === state.dag &&
      cache.generatedFiles === state.generatedFiles
    ) {
      return cache.files;
    }

    const filesList = [];
    const seenFiles = new Set();

    // 1. Scan tool calls for read_file
    log.forEach(e => {
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
            const blocks = (typeof extractJsonBlocks === 'function') ? extractJsonBlocks(cleanOutput) : [];
            let data = null;
            if (blocks.length > 0) {
              const target = blocks.find(b => b.parsed && (b.parsed.files_created || b.parsed.files_modified)) || blocks[0];
              data = target?.parsed;
            } else {
              const s = cleanOutput.indexOf('{');
              const e = cleanOutput.lastIndexOf('}');
              if (s !== -1 && e > s) {
                const sliced = cleanOutput.slice(s, e + 1);
                data = typeof safeParseJson === 'function' ? safeParseJson(sliced) : null;
              }
            }
            if (data && typeof data === 'object') {
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

    // 3. Attach computed stats and retain the immutable display snapshot.
    const files = filesList.map(f => {
      const stats = this._getFileStats(f.name, state);
      return {
        name: f.name,
        action: f.action,
        lines: stats.lines,
        added: stats.added,
        removed: stats.removed
      };
    });
    this._implementerFilesCache = {
      state,
      log,
      logLength: log.length,
      dag: state.dag,
      generatedFiles: state.generatedFiles,
      files
    };
    return files;
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
        // Extract JSON robustly: models can append tokens (<|im_end|>function),
        // include markdown fences, or output surrounding prose.
        const blocks = (typeof extractJsonBlocks === 'function') ? extractJsonBlocks(clean) : [];
        const target = blocks.find(b => b.parsed && b.parsed.verdict) || blocks[0];
        if (target && target.parsed && typeof target.parsed === 'object') {
          data = target.parsed;
        } else {
          const direct = typeof safeParseJson === 'function' ? safeParseJson(clean) : null;
          if (direct && typeof direct === 'object') {
            data = direct;
          } else {
            const start = clean.indexOf('{');
            const end = clean.lastIndexOf('}');
            if (start !== -1 && end > start) {
              const sliced = clean.slice(start, end + 1);
              data = typeof safeParseJson === 'function' ? safeParseJson(sliced) : JSON.parse(sliced);
            }
          }
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

  /* Active-agent badge plain text for accessibility, titles, and fallbacks */
  _ghostBadgeText(state) {
    const status = String(state?.status || 'PENDING').toUpperCase();
    if (status === 'COMPLETE') return 'Verified complete';
    if (status === 'FAILED') return 'Failed';
    if (status === 'CANCELLED') return 'Stopped';
    if (status === 'PENDING') return 'Idle';
    if (status === 'BLOCKED') {
      const meta = _roleMeta(state.activeAgent);
      return `${meta.name || 'Manager'} › awaiting review`;
    }
    const meta = _roleMeta(state.activeAgent);
    const roleName = meta.name || 'Council';
    let verb = 'thinking';
    if (state.activeTool) {
      verb = `running ${state.activeTool}`;
    }
    let text = `${roleName} › ${verb}`;
    if (state.activeAgent && state.activeAgentSince) {
      const elapsed = _fmtElapsed(Date.now() - state.activeAgentSince);
      if (elapsed) text += ` · ${elapsed}`;
    }
    if (state.lastHeartbeatText && state.lastHeartbeatText !== 'Waiting for model response') {
      text += ` (${state.lastHeartbeatText})`;
    }
    return text;
  }

  /* Active-agent badge semantic HTML with micro-typography */
  _ghostBadgeHtml(state) {
    const status = String(state?.status || 'PENDING').toUpperCase();
    if (status === 'COMPLETE') {
      return '<span class="ticker-complete">Verified complete</span>';
    }
    if (status === 'FAILED') {
      return '<span class="ticker-failed">Failed</span>';
    }
    if (status === 'CANCELLED') {
      return '<span class="ticker-cancelled">Stopped</span>';
    }
    if (status === 'PENDING') {
      return '<span class="ticker-idle">Idle</span>';
    }
    if (status === 'BLOCKED') {
      const meta = _roleMeta(state.activeAgent);
      return `<span class="ticker-role ticker-role--${meta.cls}">${_esc(meta.name || 'Manager')}</span>` +
             ` <span class="ticker-sep">›</span> ` +
             `<span class="ticker-verb ticker-verb--blocked">awaiting review</span>`;
    }
    const meta = _roleMeta(state.activeAgent);
    let verbHtml;
    if (state.activeTool) {
      verbHtml = `running <code class="ticker-tool">${_esc(state.activeTool)}</code>`;
    } else {
      verbHtml = 'thinking';
    }
    let html = `<span class="ticker-role ticker-role--${meta.cls}">${_esc(meta.name || 'Council')}</span>` +
               ` <span class="ticker-sep">›</span> ` +
               `<span class="ticker-verb">${verbHtml}</span>`;
    if (state.activeAgent && state.activeAgentSince) {
      const elapsed = _fmtElapsed(Date.now() - state.activeAgentSince);
      if (elapsed) {
        html += ` <span class="ticker-sep">·</span> ` +
                `<span class="ticker-time">${_esc(elapsed)}</span>`;
      }
    }
    return html;
  }

  /* Ghost Editor: stream text, toggle cursor blink */
  _renderGhostEditor(state, eventType) {
    const diffMemo = new Map();
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
      agentEl.innerHTML = this._ghostBadgeHtml(state);
      agentEl.title = this._ghostBadgeText(state);

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

    // Heartbeat carries liveness only: update active cockpit timer and bypass full ledger rebuild
    if (eventType === 'heartbeat') {
      const timerEl = ledger.querySelector('[data-live-stream] .active-cockpit-timer');
      if (timerEl && state.activeAgentSince) {
        timerEl.textContent = _fmtElapsed(Date.now() - state.activeAgentSince);
      }
      return;
    }

    // Once the live card exists, streamed tokens update the live card content in-place.
    // This provides lightning-fast streaming with zero full-ledger rebuilds.
    if (eventType === 'thought_delta' && ledger.querySelector('[data-live-stream]')) {
      const liveCard = ledger.querySelector('[data-live-stream]');
      if (liveCard) {
        const textEl = liveCard.querySelector('.ghost-chair-text, .ghost-think-text');
        if (textEl) {
          const scrollContainer = liveCard.querySelector('.active-cockpit-content') || textEl;
          const isAtBottom = (scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight) < 40;
          textEl.innerHTML = _ghostMd(state.thoughts, state.activeAgent) + '<span class="ghost-typing-cursor"></span>';
          if (isAtBottom) {
            scrollContainer.scrollTop = scrollContainer.scrollHeight;
          }
        }
        if (this._autoFollow !== false) {
          ledger.scrollTop = ledger.scrollHeight;
        }
        return;
      }
    }

    // Parse blocks sequentially
    const blocks = [];
    let lastAgent = null;
    let latestStratBlock = null;
    let latestImplBlock = null;
    const runningToolCallsByScope = new Map();
    const _toolScope = (agent, taskId, tool) => `${agent || 'system'}|${taskId || ''}|${tool || ''}`;

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

      // Track last working agent without emitting disruptive thread delegation rows
      if (agent !== 'system') { lastAgent = agent; }

      // Block Type Matching - same logic as before up to pushing blocks
      if (e.event === 'status_changed' && agent === 'chair') {
        const dur = (() => {
          if (e.extra?.duration_ms) return _fmtElapsed(e.extra.duration_ms);
          if (e.duration_ms) return _fmtElapsed(e.duration_ms);
          return '';
        })();
        blocks.push({
          type: 'chair',
          complexity: e.complexity || 'SIMPLE',
          reason: compactAgentActivity('chair'),
          duration: dur
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
        const dur = (() => {
          if (e.extra?.duration_ms) return _fmtElapsed(e.extra.duration_ms);
          if (e.duration_ms) return _fmtElapsed(e.duration_ms);
          const curTime = Date.parse(e.timestamp || e.ts);
          if (Number.isFinite(curTime)) {
            for (let prevIdx = eventIndex - 1; prevIdx >= 0; prevIdx--) {
              const pe = state.log[prevIdx];
              const pt = Date.parse(pe?.timestamp || pe?.ts);
              if (Number.isFinite(pt) && curTime >= pt) {
                const diff = curTime - pt;
                if (diff >= 200 && diff < 3600000) return _fmtElapsed(diff);
              }
            }
          }
          return '';
        })();
        blocks.push({
          type: 'think',
          agent: agent,
          text: compactAgentActivity(agent),
          thoughtText: typeof e.text === 'string' ? e.text : (typeof e.extra?.thoughts === 'string' ? e.extra.thoughts : ''),
          outcome: outcome,
          duration: dur,
          perspective: (agent === 'perspective_analyzer' && e.extra?.perspective) ? e.extra.perspective : null
        });
      }
      else if (e.event === 'dag_update' || e.event === 'task_status_update') {
        const tasks = (e.extra?.dag?.nodes || []).map(n => ({
          i: n.id,
          t: n.summary || compactTaskLabel(n.description, n.id),
          dp: n.depends_on || []
        }));
        if (latestStratBlock) {
          latestStratBlock.tasks = tasks;
        } else {
          latestStratBlock = { type: 'strat', tasks };
          blocks.push(latestStratBlock);
        }

        const doneNodes = (e.extra?.dag?.nodes || []).filter(n => n.status === 'DONE');
        if (doneNodes.length > 0) {
          const files = this._compileImplementerFiles(state);
          if (latestImplBlock) {
            latestImplBlock.files = files;
          } else {
            latestImplBlock = { type: 'impl', files };
            blocks.push(latestImplBlock);
          }
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
        const toolBlock = {
          type: 'tool_call',
          tool: typeof e.extra?.tool === 'string' ? e.extra.tool : '',
          command: cmd,
          args: args,
          status: 'RUNNING',
          taskId: typeof e.extra?.task_id === 'string' ? e.extra.task_id : '',
          sourceIndex: eventIndex,
          agent: agent
        };
        blocks.push(toolBlock);
        runningToolCallsByScope.set(_toolScope(agent, toolBlock.taskId, toolBlock.tool), toolBlock);
      }
      else if (e.event === 'tool_output') {
        const toolName = typeof e.extra?.tool === 'string' ? e.extra.tool : '';
        const taskId = typeof e.extra?.task_id === 'string' ? e.extra.task_id : '';
        const scope = _toolScope(agent, taskId, toolName);
        const lastTool = runningToolCallsByScope.get(scope);
        if (lastTool && lastTool.status === 'RUNNING') {
          lastTool.status = e.exit_code === 0 || e.exit_code === null ? 'SUCCESS' : 'FAILED';
          lastTool.output = typeof e.extra?.output === 'string' ? e.extra.output : '';
          runningToolCallsByScope.delete(scope);
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
        return formatBurstTelemetry(items, false).breakdown;
      };

      const processedBlocks = [];
      const _burstScope = (item) => `${item.taskId || ''}|${item.agent || ''}`;
      const live = state.status === 'IN_PROGRESS' || state.status === 'BLOCKED';
      if (!live) {
        blocks.forEach(b => {
          if (b.type === 'tool_call' && b.status === 'RUNNING') {
            b.status = 'FAILED';
          }
        });
      }
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
            const isRunning  = live && run.some(r => r.status === 'RUNNING');
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

    // Append the active block to the collection that is actually rendered.
    // finalBlocks is normally a compacted copy, not an alias of blocks; pushing
    // into blocks here orphaned the live card and kept the thought_delta guard
    // above from ever seeing [data-live-stream].
    if (running && state.activeAgent && state.thoughts) {
      const dur = state.activeAgentSince ? _fmtElapsed(Date.now() - state.activeAgentSince) : '';
      if (state.activeAgent === 'chair') {
        finalBlocks.push({
          type: 'chair', complexity: state.complexity || 'PENDING',
          reason: state.thoughts, streaming: true, duration: dur
        });
      } else {
        // Every non-chair role streams into the same live "think" row. An
        // explicit per-role list here silently dropped any role not yet added
        // (perspective_analyzer) from the execution stream while it worked.
        finalBlocks.push({
          type: 'think', agent: state.activeAgent,
          text: state.thoughts, streaming: true, duration: dur
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
    // Canonical role mapping table with full display names
    const _roleMap = {
      chair: { tag: 'CHAIR', cls: 'chair', name: 'Chairperson' },
      strategist: { tag: 'STRAT', cls: 'strat', name: 'Strategist' },
      strat: { tag: 'STRAT', cls: 'strat', name: 'Strategist' },
      implementer: { tag: 'IMPL', cls: 'impl', name: 'Implementer' },
      impl: { tag: 'IMPL', cls: 'impl', name: 'Implementer' },
      manager: { tag: 'MGR', cls: 'mgr', name: 'Manager' },
      mgr: { tag: 'MGR', cls: 'mgr', name: 'Manager' },
      perspective_analyzer: { tag: 'PERS', cls: 'strat', name: 'Perspective Analyzer' },
      completeness_auditor: { tag: 'AUDT', cls: 'mgr', name: 'Completeness Auditor' },
      chair_arbitration: { tag: 'ARBT', cls: 'chair', name: 'Chair Arbitration' },
      system: { tag: 'SYS', cls: 'sys', name: 'System' },
      sys: { tag: 'SYS', cls: 'sys', name: 'System' },
    };
    const _resolveRole = (agent) => _roleMap[String(agent || '').toLowerCase()] || {
      tag: String(agent || 'SYS').slice(0, 5).toUpperCase(),
      cls: 'sys',
      name: String(agent || 'System')
    };

    // _parseArgs is declared above the block-building loop (see line ~886).

    // Helper: keep file identity useful without leaking directory context.
    const _shortPath = (p) => {
      if (!p) return '';
      const parts = String(p).replace(/\\/g, '/').split('/');
      return parts[parts.length - 1] || '';
    };

    // Helper: extract concrete tool target argument (e.g. pattern, query, filename) cleanly
    const _extractToolTarget = (tool, args = {}, cmd = '') => {
      let a = args && typeof args === 'object' ? args : {};
      if ((!a || Object.keys(a).length === 0) && typeof cmd === 'string' && cmd.trim().startsWith('{')) {
        try {
          const parsed = JSON.parse(cmd.trim());
          if (parsed && typeof parsed === 'object') a = parsed;
        } catch {}
      }
      const t = String(tool || '').toLowerCase();
      if (t === 'glob') {
        const pat = a.pattern || a.glob || a.path || (typeof cmd === 'string' && !cmd.startsWith('{') ? cmd.trim() : '');
        return pat ? String(pat) : '';
      }
      if (t === 'grep') {
        const q = a.query || a.pattern || a.search || (typeof cmd === 'string' && !cmd.startsWith('{') ? cmd.trim() : '');
        const p = a.path ? ` in ${_shortPath(a.path)}` : '';
        return q ? `"${q}"${p}` : '';
      }
      if (t === 'ls' || t === 'list_dir') {
        return a.path || a.dir || '.';
      }
      if (t === 'read_file' || t === 'view_file' || t === 'edit_file' || t === 'write_file' || t === 'write_to_file') {
        const p = a.path || a.file || a.TargetFile || (typeof cmd === 'string' && !cmd.startsWith('{') ? cmd.trim() : '');
        return _shortPath(p);
      }
      if (t === 'bash' || t === 'python' || t === 'run_command') {
        const c = a.command || a.cmd || a.CommandLine || cmd || '';
        return typeof c === 'string' ? c.split('\n')[0].slice(0, 50) : '';
      }
      const generic = a.path || a.file || a.name || a.query || a.pattern || '';
      if (typeof generic === 'string' && generic && !generic.startsWith('{')) {
        return _shortPath(generic);
      }
      return '';
    };

    // Helper: compact tool badge verb to fit fixed 48px ledger column
    const _shortToolName = (tool) => {
      const t = String(tool || '').toLowerCase();
      if (t === 'write_file' || t === 'write_to_file') return 'write';
      if (t === 'read_file' || t === 'view_file') return 'read';
      if (t === 'edit_file' || t === 'replace_file_content') return 'edit';
      if (t === 'patch_file') return 'patch';
      if (t === 'run_command') return 'bash';
      if (t === 'list_dir') return 'ls';
      if (t.endsWith('_file')) return t.replace(/_file$/, '');
      return t;
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

    // Helper: crisp vector status icons (never deformed on Windows/macOS/Linux)
    const _statusIcon = (status) => {
      const s = String(status || '').toUpperCase();
      if (s === 'SUCCESS' || s === 'DONE' || s === 'COMPLETED' || s === 'APPROVED') {
        return `<svg class="st-icon st-icon--done" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 8.5l3 3 6-6"/></svg>`;
      }
      if (s === 'FAILED' || s === 'ERROR' || s === 'FAIL') {
        return `<svg class="st-icon st-icon--failed" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>`;
      }
      if (s === 'RUNNING' || s === 'IN_PROGRESS') {
        return `<svg class="st-icon st-icon--running" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="5.5" stroke-dasharray="24" stroke-dashoffset="8"/></svg>`;
      }
      if (s === 'BLOCKED' || s === 'REVISE' || s === 'WARN') {
        return `<svg class="st-icon st-icon--blocked" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M8 3.5v5.5M8 12.5h.01"/></svg>`;
      }
      return `<svg class="st-icon st-icon--pending" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="8" cy="8" r="4"/></svg>`;
    };

    // Severities are server-classified so this row can never contradict the approval gate.
    const _perspNum = (v) => {
      if (v === null || v === undefined || v === '') return null;
      const n = Number(v);
      return isFinite(n) ? n : null;
    };
    const _perspChips = (p) => {
      if (!p) return '';
      if (p.evidence === 'invalid' || p.evidence === 'empty') {
        return `<div class="persp-chips is-empty"><div class="persp-metric-cell is-invalid"><span class="persp-metric-label">STATUS</span><span class="persp-chip is-invalid">NO VALID ANALYSIS</span></div></div>`;
      }
      const sections = Array.isArray(p.sections) ? p.sections : [];
      if (!sections.length) return '';
      const overall = _perspNum(p.overall_score);
      const isBlock = p.evidence === 'block' || (overall !== null && overall < 0.6);
      const isMustFix = p.evidence === 'must_fix' || (overall !== null && overall < 0.8);
      const overallTone = isBlock ? 'is-block' : (isMustFix ? 'is-mustfix' : 'is-ok');
      const overallLabel = p.evidence === 'clear' ? 'PASSED' : (p.evidence === 'block' ? 'BLOCKED' : 'MUST_FIX');
      const head = overall !== null
        ? `<div class="persp-metric-cell persp-metric-cell--overall ${overallTone}">
            <span class="persp-metric-label">OVERALL</span>
            <span class="persp-score persp-overall ${overallTone}">${overall.toFixed(2)}</span>
            <span class="persp-chip ${overallTone}">${overallLabel}</span>
          </div>
          <span class="persp-divider" aria-hidden="true"></span>`
        : '';
      const body = sections.map(s => {
        const fullKey = String(s.key || '');
        const label = _esc((fullKey.length <= 4 ? fullKey : fullKey.slice(0, 4)).toUpperCase());
        const sc = _perspNum(s.score);
        const tone = s.block > 0 ? 'is-block' : (s.must_fix > 0 ? 'is-mustfix' : 'is-ok');
        const chips = [];
        if (s.block > 0) chips.push(`<span class="persp-chip is-block">${s.block} BLOCK</span>`);
        if (s.must_fix > 0) chips.push(`<span class="persp-chip is-mustfix">${s.must_fix} MUST_FIX</span>`);
        if (s.advisory > 0) chips.push(`<span class="persp-chip is-advisory">${s.advisory} ADVISORY</span>`);
        if (!chips.length) chips.push(`<span class="persp-chip is-ok">CLEAR</span>`);
        return `
          <div class="persp-metric-cell" data-perspective="${_esc(fullKey)}">
            <span class="persp-metric-label" title="${_esc(fullKey)}">${label}</span>
            <span class="persp-score ${tone}">${sc !== null ? sc.toFixed(2) : '--'}</span>
            <div class="persp-metric-badges">${chips.join('')}</div>
          </div>`;
      }).join('');
      return `<div class="persp-chips" role="region" aria-label="Perspective analysis metrics">${head}${body}</div>`;
    };

    // Helper: tool category metadata with clean vector SVG icons
    const _toolKind = (toolName) => {
      const t = (toolName || '').toLowerCase();
      if (t === 'bash' || t === 'python') return {
        icon: `<svg class="st-tool-icon" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 4.5l4 3.5-4 3.5M8.5 12.5h4.5"/></svg>`,
        label: 'Command Execution',
        dot: '#df8e45'
      };
      if (t === 'write_file' || t === 'edit_file') return {
        icon: `<svg class="st-tool-icon" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 2H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V6L9 2z"/><path d="M9 2v4h4"/></svg>`,
        label: 'File Write',
        dot: '#a67cff'
      };
      if (t === 'read_file' || t === 'ls' || t === 'grep' || t === 'glob') return {
        icon: `<svg class="st-tool-icon" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/></svg>`,
        label: 'File Read',
        dot: '#4eb870'
      };
      return {
        icon: `<svg class="st-tool-icon" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="M10 2a3 3 0 0 1 2.8 4.1L8 11 5 8l4.9-4.8A3 3 0 0 1 10 2z"/><path d="M2.5 13.5l3-3"/></svg>`,
        label: 'Tool Invocation',
        dot: '#50a0df'
      };
    };

    // Helper: render tool call card matching cockpit ledger row
    const _renderToolCard = (b, stepNum, memo) => {
      const args = b.args || {};
      const statusCls = b.status === 'SUCCESS' ? 'st--done' : b.status === 'FAILED' ? 'st--failed' : 'st--running';
      const target = _extractToolTarget(b.tool, args, b.command);
      const summary = _toolSummary(b.tool, args, b.command);
      const displayLabel = target || summary;
      const body = _toolBody(b.tool, args, b.command, b.output);

      let diffHtml = '';
      if (b.tool === 'write_file' || b.tool === 'edit_file') {
        const rawPath = args.path || args.file || (target && !target.includes(' ') ? target : '');
        const stats = this._getFileStats(rawPath, state, memo);
        if (stats.added > 0 || stats.removed > 0) {
          diffHtml = `<span class="payload-diff"><span class="diff-add">+${stats.added}</span> <span class="diff-del">-${stats.removed}</span> <span class="diff-lines-label">lines</span></span>`;
        } else if (args.content) {
          const lCount = String(args.content).split('\n').length;
          diffHtml = `<span class="payload-diff"><span class="diff-add">+${lCount}</span> <span class="diff-del">-0</span> <span class="diff-lines-label">lines</span></span>`;
        }
      }

      const role = _resolveRole(b.agent || 'impl');

      return `
        <div class="ghost-tool-card" style="border:none;margin:0;">
          <div class="row ghost-tool-header" tabindex="0">
            <span class="id">${stepNum}</span>
            <span class="st ${statusCls}" title="${_esc(b.status)}" aria-label="${_esc(b.status)}">${_statusIcon(b.status)}</span>
            <span class="ag ag--${role.cls}" title="${_esc(role.name || role.tag)}" aria-label="${_esc(role.name || role.tag)}">${role.tag}</span>
            <div class="ct">
              <span class="tool-tag tool-tag--${_esc(b.tool)}" title="${_esc(b.tool)}">${_esc(_shortToolName(b.tool))}</span>
              <span class="tx" title="${_esc(displayLabel)}">${_esc(displayLabel)}</span>
              ${diffHtml}
              ${body ? '<span class="ghost-tool-chevron" style="margin-left:auto;">▶</span>' : ''}
            </div>
          </div>
          ${body ? '<div class="ghost-tool-body">' + body + '</div>' : ''}
        </div>`;
    };

    let rowIndex = 1;
    const _nextStep = () => String(rowIndex++).padStart(2, '0');

    finalBlocks.forEach((b) => {
      if (b.text) totalChars += b.text.length;
      if (b.code) totalChars += b.code.length;

      if (b.streaming) {
        const stepNum = _nextStep();
        const role = _resolveRole(b.agent || (b.type === 'chair' ? 'chair' : 'impl'));
        const durText = b.duration ? `<span class="active-cockpit-timer">${_esc(b.duration)}</span>` : '';
        const titleText = b.type === 'chair' ? 'Chair Evaluation' : 'Thought streaming...';
        const isExp = this._activeThoughtExpanded ? 'is-expanded' : '';
        html += `
          <div class="row row--active" data-live-stream aria-live="off">
            <span class="id">${stepNum}</span>
            <div class="active-cockpit-box role--${role.cls} ${isExp}">
              <div class="active-cockpit-header">
                <span class="active-spinner" aria-hidden="true"></span>
                <span class="ag ag--${role.cls}" title="${_esc(role.name || role.tag)}" aria-label="${_esc(role.name || role.tag)}">${role.tag}</span>
                <span class="active-cockpit-title">${titleText}</span>
                ${durText}
                <button type="button" class="active-cockpit-toggle" title="Toggle drawer expansion (E)" aria-label="Toggle drawer expansion">
                  <span class="cockpit-toggle-label">${this._activeThoughtExpanded ? 'Collapse' : 'Expand'}</span>
                </button>
              </div>
              <div class="active-cockpit-content ghost-md ${b.type === 'chair' ? 'ghost-chair-text' : 'ghost-think-text'}">
                ${_ghostMd(b.reason || b.text || '', b.agent || 'chair')}<span class="ghost-typing-cursor"></span>
              </div>
              ${b.outcome ? `<p style="font-size:10px;color:var(--think);margin:4px 0 0 0">→ ${_esc(b.outcome)}</p>` : ''}
            </div>
          </div>`;
      } else if (b.type === 'chair') {
        const stepNum = _nextStep();
        const role = _resolveRole('chair');
        const cl = b.complexity === 'COMPLEX' ? 'var(--fail)' : b.complexity === 'MEDIUM' ? 'var(--warn)' : 'var(--muted)';
        const durBadge = b.duration ? `<span style="font-size:9px;color:var(--muted);background:var(--bg-highlight,#1c1510);padding:1px 5px;border-radius:3px;border:1px solid var(--border);margin-left:auto;">${_esc(b.duration)}</span>` : '';
        html += `
          <div class="row">
            <span class="id">${stepNum}</span>
            <span class="st st--done" title="Completed" aria-label="Completed">${_statusIcon('done')}</span>
            <span class="ag ag--${role.cls}" title="${_esc(role.name || role.tag)}" aria-label="${_esc(role.name || role.tag)}">${role.tag}</span>
            <div class="ct">
              <span>assessed <span class="bd" style="color:${cl}">${_esc(b.complexity)}</span> complexity</span>
              <span class="tx" title="${_esc(b.reason)}">${_esc(b.reason)}</span>
              ${durBadge}
            </div>
          </div>`;
      } else if (b.type === 'think') {
        const stepNum = _nextStep();
        const role = _resolveRole(b.agent);
        const durBadge = b.duration ? `<span style="font-size:9px;color:var(--muted);background:var(--bg-highlight,#1c1510);padding:1px 5px;border-radius:3px;border:1px solid var(--border);margin-left:auto;">${_esc(b.duration)}</span>` : '';
        const _p = b.perspective;
        const _pClear = !_p || _p.evidence === 'clear';
        const _pTitle = _pClear ? 'Analysis clear' : 'Blocked approval';
        const statusCell = _p
          ? `<span class="st ${_pClear ? 'st--done' : 'st--blocked'}" title="${_pTitle}" aria-label="${_pTitle}">${_statusIcon(_pClear ? 'APPROVED' : 'BLOCKED')}</span>`
          : `<span class="st st--done" title="Completed" aria-label="Completed">${_statusIcon('done')}</span>`;
        if (_p) {
          const cardCls = _p.evidence === 'block' ? ' is-blocked' : (_p.evidence === 'must_fix' ? ' is-mustfix' : '');
          const badgeColor = _pClear ? 'var(--dim)' : (_p.evidence === 'block' ? 'var(--fail)' : 'var(--warn)');
          const badgeBg = _pClear ? 'color-mix(in srgb, var(--fg) 6%, transparent)' : (_p.evidence === 'block' ? 'rgba(224,88,88,0.1)' : 'rgba(223,142,69,0.1)');
          const badgeLabel = _p.evidence === 'clear' ? 'AUDIT CLEAR' : (_p.evidence === 'block' ? 'AUDIT BLOCKED' : 'AUDIT FLAGGED');
          html += `
            <div class="ghost-perspective-card${cardCls}">
              <div class="row">
                <span class="id">${stepNum}</span>
                ${statusCell}
                <span class="ag ag--${role.cls}" title="${_esc(role.name || role.tag)}" aria-label="${_esc(role.name || role.tag)}">${role.tag}</span>
                <div class="ct">
                  <span class="bd" style="color:${badgeColor};background:${badgeBg}">${badgeLabel}</span>
                  <span class="tx" title="${_esc(b.text)}">${_esc(b.text)}</span>
                  ${b.outcome ? `<span style="color:var(--muted);font-size:10px">→ ${_esc(b.outcome)}</span>` : ''}
                  ${durBadge}
                </div>
              </div>
              ${_perspChips(_p)}
            </div>`;
        } else {
          const hasThought = Boolean(b.thoughtText && b.thoughtText.trim() && b.thoughtText !== b.text);
          html += `
            <div class="ghost-thought-card${hasThought ? ' has-drawer' : ''}">
              <div class="row ghost-thought-header" tabindex="0">
                <span class="id">${stepNum}</span>
                ${statusCell}
                <span class="ag ag--${role.cls}" title="${_esc(role.name || role.tag)}" aria-label="${_esc(role.name || role.tag)}">${role.tag}</span>
                <div class="ct">
                  <span class="tx" title="${_esc(b.text)}">${_esc(b.text)}</span>
                  ${b.outcome ? `<span style="color:var(--muted);font-size:10px">→ ${_esc(b.outcome)}</span>` : ''}
                  ${durBadge}
                  ${hasThought ? '<span class="ghost-tool-chevron" style="margin-left:auto;">▶</span>' : ''}
                </div>
              </div>
              ${hasThought ? `<div class="ghost-thought-body"><div class="ghost-md ghost-think-text">${_ghostMd(b.thoughtText, b.agent)}</div></div>` : ''}
            </div>`;
        }
      } else if (b.type === 'strat') {
        const stepNum = _nextStep();
        const role = _resolveRole('strat');
        const _taskCount = b.tasks.length;
        const _waves = executionWaveCount(b.tasks);
        const _allLabels = b.tasks.map(t => `${t.i} ${t.t}`).join(' · ');
        const _labels = _taskCount <= 5
          ? _allLabels
          : `${b.tasks.slice(0, 4).map(t => `${t.i} ${t.t}`).join(' · ')} · +${_taskCount - 4} more`;
        html += `
          <div class="row">
            <span class="id">${stepNum}</span>
            <span class="st st--done" title="Completed" aria-label="Completed">${_statusIcon('done')}</span>
            <span class="ag ag--${role.cls}" title="${_esc(role.name || role.tag)}" aria-label="${_esc(role.name || role.tag)}">${role.tag}</span>
            <div class="ct">
              <span>planned <strong>${_taskCount}</strong> task${_taskCount === 1 ? '' : 's'} in <strong>${_waves}</strong> wave${_waves === 1 ? '' : 's'}</span>
              <span class="tx" title="${_esc(_allLabels)}">${_esc(_labels)}</span>
            </div>
          </div>`;
      } else if (b.type === 'impl') {
        const role = _resolveRole('impl');
        if (b.files && b.files.length > 0) {
          b.files.forEach(f => {
            const stepNum = _nextStep();
            const diffSpan = (f.added || f.removed) ? `
              <span class="payload-diff">
                <span class="diff-add">+${f.added || 0}</span>
                <span class="diff-del">-${f.removed || 0}</span>
                <span class="diff-lines-label">lines</span>
              </span>` : (f.lines ? `<span style="color:var(--muted);font-size:10px">${f.lines} lines</span>` : '');
            html += `
              <div class="row">
                <span class="id">${stepNum}</span>
                <span class="st st--done" title="Completed" aria-label="Completed">${_statusIcon('done')}</span>
                <span class="ag ag--${role.cls}" title="${_esc(role.name || role.tag)}" aria-label="${_esc(role.name || role.tag)}">${role.tag}</span>
                <div class="ct">
                  <span class="bd">${_esc((f.action || 'updated').toUpperCase())}</span>
                  <span class="tx" title="${_esc(f.name)}">${_esc(f.name)}</span>
                  ${diffSpan}
                </div>
              </div>`;
          });
        } else {
          const stepNum = _nextStep();
          html += `
            <div class="row">
              <span class="id">${stepNum}</span>
              <span class="st st--done" title="Completed" aria-label="Completed">${_statusIcon('done')}</span>
              <span class="ag ag--${role.cls}" title="${_esc(role.name || role.tag)}" aria-label="${_esc(role.name || role.tag)}">${role.tag}</span>
              <div class="ct">
                <span class="tx" style="color:var(--muted)">Implementation step recorded</span>
              </div>
            </div>`;
        }
      } else if (b.type === 'manager_output') {
        const stepNum = _nextStep();
        const role = _resolveRole('mgr');
        const m = b.verdict === 'APPROVED'
          ? { c: 'var(--impl)', b: 'rgba(78,184,112,0.1)' }
          : b.verdict === 'REVISE'
          ? { c: 'var(--warn)', b: 'rgba(217,119,6,0.1)' }
          : { c: 'var(--fail)', b: 'rgba(220,38,38,0.1)' };
        const issueCount = b.issues ? b.issues.length : 0;
        const criticalCount = b.issues ? b.issues.filter(i => i.severity === 'critical').length : 0;
        const issueThemes = b.issues
          ? b.issues.slice(0, 3).map(iss => compactIssueTheme(iss)).filter((v, i, a) => a.indexOf(v) === i).join(' · ')
          : '';
        html += `
          <div class="row">
            <span class="id">${stepNum}</span>
            <span class="st ${b.verdict === 'APPROVED' ? 'st--done' : 'st--blocked'}" title="${_esc(b.verdict)}" aria-label="${_esc(b.verdict)}">${_statusIcon(b.verdict)}</span>
            <span class="ag ag--${role.cls}" title="${_esc(role.name || role.tag)}" aria-label="${_esc(role.name || role.tag)}">${role.tag}</span>
            <div class="ct">
              <span class="bd" style="color:${m.c};background:${m.b}">${_esc(b.verdict)}</span>
              <span class="tx" title="${_esc(issueThemes || 'Review complete')}">${_esc(issueCount ? `${issueCount} issue${issueCount === 1 ? '' : 's'}${criticalCount ? ` · ${criticalCount} critical` : ''}` : 'No issues reported')}</span>
              ${issueThemes ? `<span style="color:var(--muted);font-size:10px">(${_esc(issueThemes)})</span>` : ''}
            </div>
          </div>`;
      } else if (b.type === 'burst_group') {
        const stepNum = _nextStep();
        const _burstKey = [
          'burst',
          encodeURIComponent(this._state.sessionId || 'session'),
          encodeURIComponent(b.agent || 'implementer'),
          encodeURIComponent(b.taskId || 'phase'),
          String(Number.isFinite(b.sourceIndex) ? b.sourceIndex : 0)
        ].join(':');
        _seenBurstKeys.add(_burstKey);
        const _wasRunning = this._burstStatus.get(_burstKey);
        if (_wasRunning === true && !b.running) {
          if (!b.hasFailure) {
            _openBursts.delete(_burstKey);
          }
        }
        this._burstStatus.set(_burstKey, b.running);
        const _isOpen   = _openBursts.has(_burstKey);
        const _total = b.items.length;
        const role = _resolveRole(b.agent || 'impl');
        const statusCls = b.running ? 'st--running' : b.hasFailure ? 'st--failed' : 'st--done';
        const _bss = b.running ? _statusIcon('running') : b.hasFailure ? _statusIcon('failed') : _statusIcon('done');
        const _telemetry = formatBurstTelemetry(b.items, b.running);
        const _mutationCls = _telemetry.isMutation ? ' is-mutation' : '';

        const _itemsHtml = b.items.map(item => {
          const _target = _extractToolTarget(item.tool, item.args || {}, item.command);
          const _is = _toolSummary(item.tool, item.args || {}, item.command);
          const _itemStatusCls = item.status === 'SUCCESS' ? 'st--done' : item.status === 'FAILED' ? 'st--failed' : 'st--running';
          const _isi = _statusIcon(item.status);
          const _display = _target ? `${_target}` : _is;
          return `<div class="row row--burst-item" tabindex="0">
            <span class="id id--sub" style="color:var(--muted);opacity:0.35;font-size:9px;font-family:var(--font-mono, monospace);">··</span>
            <span class="st ${_itemStatusCls}">${_isi}</span>
            <span class="tool-tag tool-tag--${_esc(item.tool)}" title="${_esc(item.tool)}">${_esc(_shortToolName(item.tool))}</span>
            <div class="ct">
              <span class="tx tool-target" title="${_esc(_display)}">${_esc(_display)}</span>
            </div>
          </div>`;
        }).join('');

        let _cpHtml;
        const _burstBreakdownText = _telemetry.breakdown;
        if (b.running) {
          const _cpWords = (_burstBreakdownText || '').split(' ').filter(w => w.length > 0);
          if (_cpWords.length === 0) {
            _cpHtml = '';
          } else {
            const _minGap   = 0.28;
            const _wordDur  = Math.max(1.6, _cpWords.length * _minGap * 2);
            const _peakGap  = _cpWords.length > 1
              ? Math.max(_minGap, (_wordDur * 0.85) / (_cpWords.length - 1))
              : 0;
            _cpHtml = _cpWords.map((w, idx) =>
              `<span class="bw" style="animation-duration:${_wordDur.toFixed(2)}s;animation-delay:${(idx * _peakGap).toFixed(2)}s">${w}</span>`
            ).join(' ');
          }
        } else {
          _cpHtml = _burstBreakdownText || '';
        }

        html += `
          <div class="ghost-burst-group${b.running ? ' burst-running' : ''}${_isOpen ? ' burst-open' : ''}"
               data-burst-open="${_isOpen ? '1' : '0'}"
               data-burst-key="${_esc(_burstKey)}"
               style="margin: 1px 6px;">
            <div class="row ghost-burst-header" tabindex="0">
              <span class="id">${stepNum}</span>
              <span class="st ${statusCls}" title="${b.running ? 'Running' : b.hasFailure ? 'Failed' : 'Completed'}" aria-label="${b.running ? 'Running' : b.hasFailure ? 'Failed' : 'Completed'}">${_bss}</span>
              <span class="ag ag--${role.cls}" title="${_esc(role.name || role.tag)}" aria-label="${_esc(role.name || role.tag)}">${role.tag}</span>
              <div class="ct">
                <span class="burst-primary-label${_mutationCls}">${_esc(_telemetry.category)}</span>
                <span class="burst-divider-dot">·</span>
                <span class="tx burst-breakdown" title="${_esc(_burstBreakdownText)}">${_cpHtml || _esc(_burstBreakdownText)}</span>
                <span class="ghost-burst-count" style="margin-left:auto;">${_total}</span>
                <span class="ghost-burst-chevron" style="margin-left:6px;">${_isOpen ? '▼' : '▶'}</span>
              </div>
            </div>
            <div class="ghost-burst-body">${_itemsHtml}</div>
          </div>`;
      } else if (b.type === 'tool_call') {
        const stepNum = _nextStep();
        html += _renderToolCard(b, stepNum, diffMemo);
      } else if (b.type === 'sys') {
        const stepNum = _nextStep();
        const role = _resolveRole('sys');
        const attemptLabel = b.count > 1 ? ` · ${b.count} attempts` : '';
        html += `
          <div class="row row--sys-error">
            <span class="id">${stepNum}</span>
            <span class="st st--failed" title="Failed" aria-label="Failed">${_statusIcon('failed')}</span>
            <span class="ag ag--${role.cls}" title="${_esc(role.name || role.tag)}" aria-label="${_esc(role.name || role.tag)}">${role.tag}</span>
            <div class="ct">
              <span class="bd" style="color:var(--sys)">${_esc(b.code)}</span>
              <span class="tx" style="color:var(--dim)" title="${_esc(b.msg)}">${_esc(b.msg + attemptLabel)}</span>
              ${b.detail ? `<span style="color:var(--muted);font-style:italic;font-size:10px">${_esc(b.detail)}</span>` : ''}
            </div>
          </div>`;
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
    if (this._scrollRaf) { cancelAnimationFrame(this._scrollRaf); this._scrollRaf = null; }
    if (this._autoFollow && _atBottom) {
      this._scrollRaf = requestAnimationFrame(() => {
        ledger.scrollTop = ledger.scrollHeight;
        this._scrollRaf = null;
      });
    } else if (!_atBottom) {
      ledger.scrollTop = _savedScrollTop;
    }
    // ─────────────────────────────────────────────────────────────────────────

    // Update bottom status footer via decoupled telemetry engine
    if (this._telemetry) {
      this._telemetry.updateUI();
    }
    this._updateAutoFollowIndicator();
  }

  /* Panel 2 Identity (Decision 3): Execution stream permanently owns the body */
  _renderDAGView(state) {
    const container = document.getElementById('council-dag-view');
    const ghostEl   = document.getElementById('council-ghost-editor');
    const titleEl   = document.getElementById('council-ghost-title');
    const toggleBtn = document.getElementById('council-thinking-toggle');

    if (ghostEl) ghostEl.style.display = '';
    if (container) container.style.display = 'none';
    if (titleEl) titleEl.textContent = 'Execution stream';

    if (toggleBtn) {
      const hasDag = Boolean(state.dag && Array.isArray(state.dag.nodes) && state.dag.nodes.length > 0);
      toggleBtn.hidden = !hasDag;
      toggleBtn.textContent = 'Task Graph [G]';
      toggleBtn.title = 'Open full task dependency graph overlay (G)';
      if (!toggleBtn._wired) {
        toggleBtn._wired = true;
        toggleBtn.addEventListener('click', () => {
          this._toggleDagOverlay();
        });
      }
    }

    this._renderDAGRail(state);

    if (this._dagOverlayOpen && state.dag) {
      const overlayContainer = document.getElementById('council-dag-overlay-container');
      if (overlayContainer) {
        this._renderDAGSVG(overlayContainer, state.dag, true);
      }
    }
  }

  /* ~28px horizontal DAG task rail below pane header */
  _renderDAGRail(state) {
    const ghostPane = document.querySelector('.council-stream-pane, .council-ghost-pane');
    if (!ghostPane) return;

    let railEl = document.getElementById('council-dag-rail');
    if (!railEl) {
      railEl = document.createElement('div');
      railEl.id = 'council-dag-rail';
      railEl.className = 'council-dag-rail';
      const header = ghostPane.querySelector('.council-pane-header');
      if (header && header.nextSibling) {
        ghostPane.insertBefore(railEl, header.nextSibling);
      } else {
        ghostPane.prepend(railEl);
      }
      railEl.addEventListener('click', (e) => {
        // Clicking anywhere on the rail opens the full interactive SVG DAG overlay
        this._toggleDagOverlay(true);
      });
    }

    const nodes = (state.dag && Array.isArray(state.dag.nodes)) ? state.dag.nodes : [];
    if (!nodes.length) {
      railEl.style.display = 'none';
      return;
    }

    railEl.style.display = 'flex';
    const waveMap = _computeTaskWaves(nodes);
    const totalWaves = Math.max(1, ...Array.from(waveMap.values()));

    let activeWave = totalWaves;
    for (const n of nodes) {
      const s = String(n.status || 'PENDING').toUpperCase();
      if (s !== 'DONE') {
        activeWave = waveMap.get(String(n.id)) || 1;
        break;
      }
    }

    const renderPill = (n) => {
      const status = String(n.status || 'PENDING').toUpperCase();
      const statusCls = status === 'DONE' ? 'done' : (status === 'IN_PROGRESS' || status === 'RUNNING') ? 'in-progress' : status === 'FAILED' ? 'failed' : status === 'BLOCKED' ? 'blocked' : 'pending';
      const summary = n.summary || compactTaskLabel(n.description, n.id);
      return `
        <button type="button" class="council-dag-pill dag-pill--${statusCls}" data-task-id="${_esc(n.id)}" title="${_esc(n.id)}: ${_esc(n.description || '')}">
          <span class="dag-pill-dot"></span>
          <span class="dag-pill-id">${_esc(n.id)}</span>
          <span class="dag-pill-summary">${_esc(summary)}</span>
        </button>`;
    };

    let pillsHtml = '';
    if (nodes.length <= 5) {
      nodes.forEach(n => { pillsHtml += renderPill(n); });
    } else {
      const activeNodes = nodes.filter(n => (waveMap.get(String(n.id)) || 1) === activeWave);
      const doneBefore = nodes.filter(n => (waveMap.get(String(n.id)) || 1) < activeWave && String(n.status || '').toUpperCase() === 'DONE').length;
      const upcoming = nodes.filter(n => (waveMap.get(String(n.id)) || 1) > activeWave).length;

      pillsHtml += `<span class="dag-rail-wave" title="Active execution wave">Wave ${activeWave}/${totalWaves}</span>`;
      if (doneBefore > 0) {
        pillsHtml += `<span class="dag-rail-pill-summary" title="${doneBefore} tasks verified in prior waves">✓ ${doneBefore} done</span>`;
      }
      activeNodes.forEach(n => { pillsHtml += renderPill(n); });
      if (upcoming > 0) {
        pillsHtml += `<span class="dag-rail-more" title="${upcoming} tasks in upcoming waves">+${upcoming} more</span>`;
      }
    }
    railEl.innerHTML = pillsHtml;
  }

  /* Interactive DAG modal overlay with backdrop scrim */
  _toggleDagOverlay(forceState) {
    let overlay = document.getElementById('council-dag-overlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'council-dag-overlay';
      overlay.className = 'council-dag-overlay';
      overlay.style.display = 'none';
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-modal', 'true');
      overlay.setAttribute('aria-label', 'Task Dependency Graph');
      overlay.innerHTML = `
        <div class="council-dag-backdrop"></div>
        <div class="council-dag-modal">
          <div class="council-dag-modal-header">
            <div style="display:flex;align-items:center;gap:12px;">
              <span class="council-pane-title" style="font-weight:700;letter-spacing:.08em;">TASK DEPENDENCY GRAPH</span>
              <span style="font-size:10px;color:var(--muted);background:var(--bg);padding:2px 8px;border-radius:4px;border:1px solid var(--border);">Press [G] or [Esc] to close</span>
            </div>
            <button type="button" class="council-dag-modal-close" aria-label="Close task graph">&times;</button>
          </div>
          <div class="council-dag-modal-body scroll-container" id="council-dag-overlay-container"></div>
        </div>
      `;
      document.body.appendChild(overlay);

      overlay.querySelector('.council-dag-backdrop')?.addEventListener('click', () => this._toggleDagOverlay(false));
      overlay.querySelector('.council-dag-modal-close')?.addEventListener('click', () => this._toggleDagOverlay(false));
    }

    const isCurrentlyOpen = this._dagOverlayOpen;
    const shouldOpen = forceState !== undefined ? Boolean(forceState) : !isCurrentlyOpen;
    this._dagOverlayOpen = shouldOpen;

    if (shouldOpen) {
      overlay.style.display = 'flex';
      const container = document.getElementById('council-dag-overlay-container');
      if (container && this._state.dag) {
        this._renderDAGSVG(container, this._state.dag, true);
      }
    } else {
      overlay.style.display = 'none';
    }
  }

  _renderDAGSVG(container, dag, isOverlay = false) {
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

    const nodeW = isOverlay ? 220 : 160;
    const nodeH = isOverlay ? 64 : 50;
    const padX = isOverlay ? 44 : 32;
    const padY = isOverlay ? 36 : 24;
    const marginX = isOverlay ? 40 : 24;
    const marginY = isOverlay ? 36 : 20;
    const maxLayerWidth = Math.max(...layerKeys.map(k => layerGroups[k].length));
    const svgW = Math.max(isOverlay ? 600 : 300, maxLayerWidth * (nodeW + padX) + marginX * 2);
    const svgH = Math.max(isOverlay ? 360 : 200, layerKeys.length * (nodeH + padY) + marginY * 2);

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

    const _wrapSvgText = (text, maxLineChars = 26, maxLines = 2) => {
      if (!text) return [];
      const words = String(text).trim().split(/\s+/);
      const lines = [];
      let current = '';

      for (const w of words) {
        if (!w) continue;
        if (w.length > maxLineChars) {
          if (current) {
            lines.push(current);
            current = '';
            if (lines.length === maxLines) break;
          }
          lines.push(w.slice(0, maxLineChars - 1) + '…');
          if (lines.length === maxLines) break;
          continue;
        }
        const test = current ? `${current} ${w}` : w;
        if (test.length <= maxLineChars) {
          current = test;
        } else {
          if (current) lines.push(current);
          current = w;
          if (lines.length === maxLines) break;
        }
      }
      if (current && lines.length < maxLines) {
        lines.push(current);
      }

      if (lines.length === maxLines) {
        const joined = lines.join(' ');
        if (joined.length < String(text).trim().length) {
          let last = lines[maxLines - 1];
          if (!last.endsWith('…')) {
            last = (last.length > maxLineChars - 1 ? last.slice(0, maxLineChars - 2) : last) + '…';
            lines[maxLines - 1] = last;
          }
        }
      }
      return lines;
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
      const maxLineChars = isOverlay ? 26 : 18;
      const lines = _wrapSvgText(desc, maxLineChars, 2);
      let descHtml = '';
      if (lines.length === 1) {
        const yPos = isOverlay ? 40 : 34;
        descHtml = `<text class="dag-node-desc" x="${nodeW/2}" y="${yPos}">${_esc(lines[0])}</text>`;
      } else if (lines.length >= 2) {
        const startY = isOverlay ? 33 : 28;
        const lineSpacing = isOverlay ? 14 : 12;
        descHtml = `<text class="dag-node-desc" x="${nodeW/2}" y="${startY}">
          <tspan x="${nodeW/2}" dy="0">${_esc(lines[0])}</tspan>
          <tspan x="${nodeW/2}" dy="${lineSpacing}">${_esc(lines[1])}</tspan>
        </text>`;
      }
      svg += `<g class="dag-node ${statusClass(n.status)}" transform="translate(${p.x},${p.y})">
        <title>${_esc(n.id)}: ${_esc(desc)}</title>
        <rect class="dag-node-rect" width="${nodeW}" height="${nodeH}"/>
        <text class="dag-node-id" x="${nodeW/2}" y="${isOverlay ? 20 : 16}">${_esc(n.id)}</text>
        ${descHtml}
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

    // Snapshot scroll positions before DOM updates to prevent viewport jumping
    const savedCodeScrollTop = el ? el.scrollTop : 0;
    const savedLinenosScrollTop = linenosEl ? linenosEl.scrollTop : 0;

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
        // Auto-switch to Files tab on first output
        const filesTab = document.querySelector('.council-ctx-tab[data-tab="files"]');
        if (filesTab && !filesTab.classList.contains('active')) this._activateCtxTab('files');
        if (linenosEl) linenosEl.style.display = 'block';
        el.style.padding = '16px';
        
        const safeCode = (selectedFile && state.generatedFiles[selectedFile] !== undefined)
          ? state.generatedFiles[selectedFile]
          : (typeof state.lastCode === 'string' ? state.lastCode : '');

        const fileVer = state.fileVersions ? state.fileVersions[selectedFile] : null;
        const hasDiff = Boolean(
          fileVer &&
          fileVer.original !== undefined &&
          fileVer.original !== fileVer.current &&
          typeof fileVer.current === 'string'
        );

        el.classList.remove('has-state-container');
        el.style.padding = '';
        if (linenosEl) linenosEl.style.display = '';

        if (hasDiff) {
          const diffLines = computeLineDiff(fileVer.original || '', fileVer.current || '');
          let codeHtml = '';
          let linenosHtml = '';
          diffLines.forEach(item => {
            if (item.type === 'add') {
              codeHtml += `<div class="diff-line diff-line-add"><span class="diff-gutter" style="color:var(--impl,#4eb870);user-select:none;margin-right:6px;font-weight:700;">+</span>${_esc(item.text)}</div>`;
              linenosHtml += `<div class="diff-line-add" style="color:var(--impl,#4eb870);">${item.lineNum != null ? item.lineNum : '+'}</div>`;
            } else if (item.type === 'del') {
              codeHtml += `<div class="diff-line diff-line-del"><span class="diff-gutter" style="color:var(--fail,#e05858);user-select:none;margin-right:6px;font-weight:700;">-</span>${_esc(item.text)}</div>`;
              linenosHtml += `<div class="diff-line-del" style="color:var(--fail,#e05858);">-</div>`;
            } else {
              codeHtml += `<div class="diff-line diff-line-equal"><span class="diff-gutter" style="opacity:0.3;user-select:none;margin-right:6px;"> </span>${_esc(item.text)}</div>`;
              linenosHtml += `<div>${item.lineNum != null ? item.lineNum : ''}</div>`;
            }
          });
          el.innerHTML = codeHtml;
          if (linenosEl) linenosEl.innerHTML = linenosHtml;
        } else {
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
        }

        // Restore scroll positions to prevent jumping
        el.scrollTop = savedCodeScrollTop;
        if (linenosEl) linenosEl.scrollTop = savedLinenosScrollTop;

        if (linenosEl && !el._linenosWired) {
          el._linenosWired = true;
          el.addEventListener('scroll', () => {
            linenosEl.scrollTop = el.scrollTop;
          });
        }
      } else {
        // NO FILES - DETECT OTHER STATE MACHINE STATES
        if (linenosEl) linenosEl.style.display = 'none';
        el.classList.add('has-state-container');
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
          `.trim();
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
          `.trim();
        } else if (state.status === 'PENDING') {
          // IDLE STATE
          el.innerHTML = `
            <div class="implementer-state-container idle-state">
              <div class="terminal-shell">
                <span class="terminal-prompt">odysseus@implementer:~$</span>
                <span class="terminal-cursor">_</span>
              </div>
            </div>
          `.trim();
        } else {
          // EMPTY STATE (Orchestrator complete but no files output)
          el.innerHTML = `
            <div class="implementer-state-container empty-state">
              <svg class="empty-icon" viewBox="0 0 24 24" width="36" height="36"><path fill="currentColor" d="M13 9h5.5L13 3.5V9M6 2h8l6 6v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4c0-1.11.89-2 2-2m0 18h12v-2H6v2m0-4h12v-2H6v2m0-4h8V8H6v2Z"/></svg>
              <div class="empty-title">No Code Output</div>
              <div class="empty-subtitle">The implementation task executed, but did not generate or edit any workspace files.</div>
            </div>
          `.trim();
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
        headerContainer.addEventListener('click', e => {
          const tab = e.target.closest('.council-code-tab');
          if (!tab) return;
          const path = tab.getAttribute('data-filepath');
          state.selectedFile = path;
          state.lastFile = path;
          state.lastCode = state.generatedFiles[path] || '';
          this.render(state);
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
    const dotEl = null; // council-log-status-dot removed in Stream-First redesign
    // Update Log tab badge with event count
    const logBadge = document.getElementById('ctx-log-badge');
    const activeTab = document.querySelector('.council-ctx-tab.active');
    const logTabActive = activeTab?.dataset?.tab === 'log';
    if (logBadge) {
      const count = state.log ? state.log.length : 0;
      if (count > 0 && !logTabActive) {
        logBadge.textContent = count > 99 ? '99+' : String(count);
        logBadge.hidden = false;
      } else {
        logBadge.hidden = true;
      }
    }

    // 2. Update session ID display
    const sidEl = document.getElementById('council-log-session-id');
    if (sidEl) {
      const fullId = state.sessionId || 'PENDING';
      const shortId = fullId.length > 12 ? `${fullId.slice(0, 8)}…` : fullId;
      sidEl.textContent = `ID: ${shortId}`;
      sidEl.title = `Session ID: ${fullId} (click to copy)`;
      sidEl.style.cursor = 'pointer';
      if (!sidEl.dataset.hasCopyListener) {
        sidEl.dataset.hasCopyListener = 'true';
        sidEl.addEventListener('click', () => {
          if (state.sessionId) {
            navigator.clipboard?.writeText(state.sessionId);
            const orig = sidEl.textContent;
            sidEl.textContent = 'COPIED!';
            setTimeout(() => { sidEl.textContent = orig; }, 1200);
          }
        });
      }
    }

    // 3. Update footer button text and state
    const revertBtn = document.getElementById('council-revert-btn');
    if (revertBtn) {
      if (state.status === 'IN_PROGRESS' || state.status === 'BLOCKED') {
        revertBtn.textContent = 'Restart run';
        revertBtn.disabled = false;
      } else if (state.status === 'COMPLETE') {
        revertBtn.textContent = 'Restart from last brief';
        revertBtn.disabled = false;
      } else {
        revertBtn.textContent = 'Restart from brief';
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
    const actions = bar.querySelector('.gate-bar-actions');

    // DIRECT path completed — show muted skip indicator
    if (state.route === 'DIRECT' && state.status === 'COMPLETE') {
      bar.hidden = false;
      bar.classList.add('council-gate-bar--skipped');
      if (label) label.textContent = '⚡ APPROVAL GATE SKIPPED (DIRECT PATH)';
      if (body) body.textContent = '';
      if (actions) actions.style.display = 'none';
      return;
    }

    bar.classList.remove('council-gate-bar--skipped');
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
      const popoverWidth = popover.offsetWidth || 270;
      const popoverHeight = popover.offsetHeight || 220;

      const sidebar = anchorEl.closest('#sidebar, .sidebar') || document.getElementById('sidebar');
      const sidebarRect = sidebar ? sidebar.getBoundingClientRect() : null;

      let left = (sidebarRect && rect.left < sidebarRect.right) ? sidebarRect.right + 12 : rect.right + 12;
      if (left + popoverWidth > viewportWidth - 16) {
        left = Math.max(16, viewportWidth - popoverWidth - 16);
      }

      let top = rect.top + (rect.height / 2) - (popoverHeight / 2);
      if (top + popoverHeight > viewportHeight - 16) {
        top = viewportHeight - popoverHeight - 16;
      }
      if (top < 16) {
        top = 16;
      }

      popover.style.top  = `${Math.round(top)}px`;
      popover.style.left = `${Math.round(left)}px`;

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

      // Expand/collapse Active Cockpit Drawer (.active-cockpit-toggle)
      const cockpitToggle = e.target.closest('.active-cockpit-toggle');
      if (cockpitToggle) {
        e.stopPropagation();
        this._activeThoughtExpanded = !this._activeThoughtExpanded;
        const box = cockpitToggle.closest('.active-cockpit-box');
        if (box) {
          box.classList.toggle('is-expanded', this._activeThoughtExpanded);
          const lbl = box.querySelector('.cockpit-toggle-label');
          if (lbl) lbl.textContent = this._activeThoughtExpanded ? 'Collapse' : 'Expand';
        }
        return;
      }

      // Expand/collapse Completed Thought Card (.ghost-thought-header)
      const thoughtHeader = e.target.closest('.ghost-thought-header');
      if (thoughtHeader && !e.target.closest('.tx') && !e.target.closest('.ag') && !e.target.closest('.st')) {
        const card = thoughtHeader.closest('.ghost-thought-card');
        if (card && card.classList.contains('has-drawer')) {
          card.classList.toggle('open');
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

    // Contextual panel tab switching
    document.querySelectorAll('.council-ctx-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        this._activateCtxTab(tab.dataset.tab);
      });
    });

    // Global keyboard contract (Decision 5)
    document.addEventListener('keydown', e => this._handleGlobalKeydown(e, session));

    // Ledger manual scroll listener for auto-follow toggle
    const ledger = document.getElementById('council-ghost-stream-ledger');
    if (ledger && !ledger._scrollFollowWired) {
      ledger._scrollFollowWired = true;
      ledger.addEventListener('scroll', () => {
        const atBottom = (ledger.scrollHeight - ledger.scrollTop - ledger.clientHeight) < 40;
        if (!atBottom && this._autoFollow) {
          this._autoFollow = false;
          this._updateAutoFollowIndicator();
        } else if (atBottom && !this._autoFollow) {
          this._autoFollow = true;
          this._updateAutoFollowIndicator();
        }
      });
    }
  }

  _activateCtxTab(tabId) {
    // Deactivate all tabs and bodies
    document.querySelectorAll('.council-ctx-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.council-ctx-body').forEach(b => { b.hidden = true; });
    // Activate the selected tab
    const tab = document.querySelector(`.council-ctx-tab[data-tab="${tabId}"]`);
    const body = document.getElementById(`ctx-body-${tabId}`);
    if (tab) tab.classList.add('active');
    if (body) body.hidden = false;
    // Clear badge on Log tab when switching to it
    if (tabId === 'log') {
      const badge = document.getElementById('ctx-log-badge');
      if (badge) badge.hidden = true;
    }
    // When switching to Debug tab, render DAG if available
    if (tabId === 'debug' && this._state.dag) {
      const container = document.getElementById('council-dag-view-debug');
      if (container) this._renderDAGSVG(container, this._state.dag, true);
    }
  }

  _updateAutoFollowIndicator() {
    let pill = document.getElementById('council-stream-autofollow');
    if (!pill) {
      const footer = document.getElementById('council-ghost-footer');
      if (footer) {
        pill = document.createElement('span');
        pill.id = 'council-stream-autofollow';
        pill.className = 'council-autofollow-pill';
        pill.title = 'Press Space to toggle auto-scroll';
        pill.addEventListener('click', () => {
          this._autoFollow = !this._autoFollow;
          this._updateAutoFollowIndicator();
          if (this._autoFollow) {
            const ledger = document.getElementById('council-ghost-stream-ledger');
            if (ledger) ledger.scrollTop = ledger.scrollHeight;
          }
        });
        const metrics = footer.querySelector('.ghost-footer-metrics');
        if (metrics) metrics.appendChild(pill);
        else footer.appendChild(pill);
      }
    }
    if (pill) {
      pill.style.background = '';
      pill.style.color = '';
      pill.style.border = '';
      if (this._autoFollow) {
        pill.textContent = 'Auto: ON';
        pill.className = 'council-autofollow-pill active';
      } else {
        pill.textContent = 'Auto: OFF';
        pill.className = 'council-autofollow-pill paused';
      }
    }
  }

  _handleGlobalKeydown(e, session) {
    const target = e.target;
    // Mindful check: Ensure hotkeys are completely suppressed when the user
    // is actively typing in any input, textarea, or contenteditable element!
    if (
      target && (
        target.tagName === 'INPUT' ||
        target.tagName === 'TEXTAREA' ||
        target.tagName === 'SELECT' ||
        target.isContentEditable ||
        Boolean(target.closest?.('[contenteditable="true"]'))
      )
    ) {
      return;
    }

    // Escape: Trigger cancellation if run is active (IN_PROGRESS or BLOCKED),
    // or close DAG overlay if open
    if (e.key === 'Escape') {
      if (this._dagOverlayOpen) {
        e.preventDefault();
        this._toggleDagOverlay(false);
        return;
      }
      const isRunning = this._state.status === 'IN_PROGRESS' || this._state.status === 'BLOCKED';
      if (isRunning) {
        e.preventDefault();
        session.respond('cancel');
      }
      return;
    }

    // Do not capture modified key chords (Ctrl, Alt, Meta)
    if (e.ctrlKey || e.altKey || e.metaKey) return;

    // Space: Toggle stream auto-follow (pause/resume autoscroll) and update footer pill
    if (e.key === ' ' || e.code === 'Space') {
      e.preventDefault();
      this._autoFollow = !this._autoFollow;
      this._updateAutoFollowIndicator();
      if (this._autoFollow) {
        const ledger = document.getElementById('council-ghost-stream-ledger');
        if (ledger) ledger.scrollTop = ledger.scrollHeight;
      }
      return;
    }

    // G / g: Toggle DAG overlay view
    if (e.key === 'g' || e.key === 'G') {
      e.preventDefault();
      this._toggleDagOverlay();
      return;
    }

    // E / e: Toggle active cockpit thought drawer expansion
    if (e.key === 'e' || e.key === 'E') {
      const liveBox = document.querySelector('.active-cockpit-box');
      if (liveBox) {
        e.preventDefault();
        this._activeThoughtExpanded = !this._activeThoughtExpanded;
        liveBox.classList.toggle('is-expanded', this._activeThoughtExpanded);
        const lbl = liveBox.querySelector('.cockpit-toggle-label');
        if (lbl) lbl.textContent = this._activeThoughtExpanded ? 'Collapse' : 'Expand';
        return;
      }
    }

    // F: Focus Files tab in contextual panel
    if (e.key === 'f' || e.key === 'F') {
      e.preventDefault();
      this._activateCtxTab('files');
      document.getElementById('council-code-panel')?.focus();
      return;
    }
    // L: Focus Log tab in contextual panel
    if (e.key === 'l' || e.key === 'L') {
      e.preventDefault();
      this._activateCtxTab('log');
      document.getElementById('council-captains-log')?.focus();
      return;
    }
    // D: Open Debug tab (DAG)
    if (e.key === 'd' || e.key === 'D') {
      e.preventDefault();
      this._activateCtxTab('debug');
      return;
    }
  }
}

/* ─── Helpers ────────────────────────────────────────────────────── */
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

function formatBurstTelemetry(items, isRunning = false) {
  let edits = 0;
  let reads = 0;
  let queries = 0;
  let checks = 0;
  let builds = 0;
  let others = 0;

  for (const item of (items || [])) {
    const t = String(item?.tool || '').toLowerCase();
    if (t === 'write_file' || t === 'edit_file') {
      edits++;
    } else if (t === 'read_file' || t === 'view_file') {
      reads++;
    } else if (t === 'glob' || t === 'grep' || t === 'ls') {
      queries++;
    } else if (t === 'bash' || t === 'python') {
      const raw = String(item?.args?.command || item?.args?.code || item?.command || '').toLowerCase();
      if (/install|build|bundle/.test(raw)) {
        builds++;
      } else {
        checks++;
      }
    } else {
      others++;
    }
  }

  let category = 'Workspace Operations';
  let isMutation = false;
  if (edits > 0) {
    category = isRunning ? 'Updating files...' : 'File Modifications';
    isMutation = true;
  } else if (checks > 0) {
    category = isRunning ? 'Running checks...' : 'Test Verification';
  } else if (builds > 0) {
    category = isRunning ? 'Building project...' : 'Build Pipeline';
  } else if (reads > 0 && queries === 0) {
    category = isRunning ? 'Reading source...' : 'Source Inspection';
  } else if (queries > 0 || reads > 0) {
    category = isRunning ? 'Inspecting workspace...' : 'Workspace Discovery';
  }

  const parts = [];
  if (edits > 0) parts.push(`${edits} edit${edits === 1 ? '' : 's'}`);
  if (reads > 0) parts.push(`${reads} read${reads === 1 ? '' : 's'}`);
  if (queries > 0) parts.push(`${queries} ${reads > 0 || edits > 0 ? 'quer' + (queries === 1 ? 'y' : 'ies') : 'path quer' + (queries === 1 ? 'y' : 'ies')}`);
  if (checks > 0) parts.push(`${checks} check${checks === 1 ? '' : 's'}`);
  if (builds > 0) parts.push(`${builds} build op${builds === 1 ? '' : 's'}`);
  if (others > 0) parts.push(`${others} op${others === 1 ? '' : 's'}`);

  const breakdown = parts.join(' · ') || `${(items || []).length} operations`;

  return { category, breakdown, isMutation };
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

function _computeTaskWaves(nodes) {
  const byId = new Map((nodes || []).map(n => [String(n.id || n.i), n]));
  const memo = new Map();
  const depth = (id, trail = new Set()) => {
    if (memo.has(id)) return memo.get(id);
    if (trail.has(id)) return 1;
    const node = byId.get(id);
    const deps = Array.isArray(node?.dependencies || node?.dp) ? (node.dependencies || node.dp) : [];
    const nextTrail = new Set(trail).add(id);
    const val = deps.length ? 1 + Math.max(0, ...deps.map(dep => depth(String(dep), nextTrail))) : 1;
    memo.set(id, val);
    return val;
  };
  (nodes || []).forEach(n => depth(String(n.id || n.i)));
  return memo;
}

function compactAgentActivity(agent, tool = '') {
  const role = String(agent || 'agent').toLowerCase();
  if (tool) return `${role.charAt(0).toUpperCase() + role.slice(1)} is using a tool`;
  if (role === 'strategist') return 'Constructing execution plan';
  if (role === 'manager') return 'Reviewing execution plan';
  if (role === 'implementer') return 'Executing assigned task';
  if (role === 'chair') return 'Assessing request';
  if (role === 'perspective_analyzer') return 'Auditing security, performance, maintainability';
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

function _cleanNoiseAndTags(text) {
  if (!text) return '';
  let s = String(text);
  // 1. Strip standalone or paired XML/DSML tags safely without runaway multiline eating
  s = s.replace(/<[^>]*DSML[^>]*>/gi, '');
  s = s.replace(/<[^>]*\b(?:invoke|parameter|tool_call|tool_calls|calls|channel)\b[^>]*>/gi, '');
  s = s.replace(/<[^>]*\bthink(?:ing)?\b[^>]*>/gi, '');
  s = s.replace(/<[|｜][^>]+[|｜]>/g, '');
  s = s.replace(/<\|im_(?:start|end)\|>/gi, '');
  s = s.replace(/<\s*[/／]?\s*>/g, '');
  // 2. Clean up excessive blank lines
  s = s.replace(/^\s*\n+/, '').replace(/\n{3,}/g, '\n\n');
  return s;
}

function _normalizeStreamBoundaries(text) {
  if (!text) return '';
  let s = text;
  // 1. Separate glued braces: '}{' -> '}\n\n{'
  s = s.replace(/\}\s*\{/g, '}\n\n{');
  // 2. Separate glued closing brace to word: '}I'll inspect' -> '}\n\nI'll inspect'
  s = s.replace(/\}([A-Za-z])/g, '}\n\n$1');
  // 3. Separate glued word to opening brace: 'path = ./{' -> 'path = ./\n\n{'
  s = s.replace(/([a-zA-Z0-9.,!?:;=\-\/])\{/g, '$1\n\n{');
  // 4. Separate glued sentences: 'environment.I'll' -> 'environment. I'll'
  s = s.replace(/([a-z0-9][.!?])([A-Z])/g, '$1 $2');
  // 5. Repair half-JSON missing root brace ONLY when preceded by prose e.g. 'incorrect.,\n"issues": ['
  s = s.replace(/([a-zA-Z.!?])\s*,\s*(["']issues["']\s*:\s*\[[\s\S]*?\](?:\s*,\s*["']confidence["']\s*:\s*[\d.]+)?)(\s*\})/g, '$1\n\n{\n$2$3');
  return s;
}


function _formatToolDeclarations(text) {
  if (!text) return text;
  const toolListRegex = /Tool call list:\s*((?:Tool:\s*[a-zA-Z0-9_-]+[\s\S]*?(?=(?:Tool:|\n\s*\n[A-Z]|\n\s*\{|$)))+)/gi;
  return text.replace(toolListRegex, (match, body) => {
    const toolRegex = /Tool:\s*([a-zA-Z0-9_-]+)([\s\S]*?)(?=(?:Tool:|$))/gi;
    const chips = [];
    let tMatch;
    while ((tMatch = toolRegex.exec(body)) !== null) {
      const toolName = tMatch[1].trim();
      const paramsText = tMatch[2].trim();
      const params = [];
      const paramLineRegex = /-\s*([a-zA-Z0-9_-]+)\s*=\s*([^\n\r]+)/g;
      let pMatch;
      while ((pMatch = paramLineRegex.exec(paramsText)) !== null) {
        params.push(`${pMatch[1]}: ${pMatch[2].trim()}`);
      }
      const paramStr = params.length > 0 ? `(${params.join(', ')})` : '';
      chips.push(`<span class="tool-plan-chip"><span class="tool-plan-name">${_esc(toolName)}</span> <span class="tool-plan-args">${_esc(paramStr)}</span></span>`);
    }
    if (chips.length > 0) {
      return `\n\n<div class="ghost-tool-plan-deck"><div class="tool-plan-header"><span class="tool-plan-badge">DECLARED TOOLS</span></div><div class="tool-plan-chips">${chips.join(' ')}</div></div>\n\n`;
    }
    return match;
  });
}

function _dedupeStatements(text) {
  if (!text || text.length < 20) return text;
  // Paragraph-level deduplication
  const paras = text.split(/\n{2,}/);
  const deduped = [];
  const seenPreambleKeys = new Set();

  for (let i = 0; i < paras.length; i++) {
    const cur = paras[i].trim();
    if (!cur) continue;

    // Check consecutive similarity
    const prev = deduped.length > 0 ? deduped[deduped.length - 1].trim() : null;
    if (prev && _isSimilarStatement(cur, prev)) continue;

    // Check sliding boilerplate preamble duplicates (e.g. repeated "Let me inspect the workspace...")
    const norm = cur.replace(/[^a-zA-Z0-9]/g, '').toLowerCase();
    if (norm.length > 25 && (norm.startsWith('letmeinspect') || norm.startsWith('illinspect') || norm.startsWith('iwillinspect'))) {
      const key = norm.slice(0, 35);
      if (seenPreambleKeys.has(key)) continue;
      seenPreambleKeys.add(key);
    }

    deduped.push(paras[i]);
  }
  let res = deduped.join('\n\n');
  // Sentence-level immediate duplicate loops e.g. "Sentence A. Sentence A."
  res = res.replace(/([A-Z][^.!?\n]{15,}[.!?])\s*\1+/g, '$1');
  return res;
}

function _isSimilarStatement(a, b) {
  if (a === b) return true;
  const ca = a.replace(/[^a-zA-Z0-9]/g, '').toLowerCase();
  const cb = b.replace(/[^a-zA-Z0-9]/g, '').toLowerCase();
  if (ca.length > 15 && ca === cb) return true;
  if (ca.length > 25 && (ca.startsWith(cb) || cb.startsWith(ca)) && Math.abs(ca.length - cb.length) < 8) return true;
  return false;
}

function _formatTabularData(text) {
  if (!text || !text.includes('\t')) return text;
  const lines = text.split('\n');
  const out = [];
  let inTabTable = false;
  let tabRows = [];

  const flushTable = () => {
    if (tabRows.length >= 2) {
      const colCount = Math.max(...tabRows.map(r => r.length));
      out.push('| ' + tabRows[0].concat(Array(colCount - tabRows[0].length).fill('')).join(' | ') + ' |');
      out.push('| ' + Array(colCount).fill('---').join(' | ') + ' |');
      for (let i = 1; i < tabRows.length; i++) {
        out.push('| ' + tabRows[i].concat(Array(colCount - tabRows[i].length).fill('')).join(' | ') + ' |');
      }
    } else {
      tabRows.forEach(r => out.push(r.join('\t')));
    }
    tabRows = [];
    inTabTable = false;
  };

  for (const line of lines) {
    if (line.includes('\t')) {
      const parts = line.split('\t').map(p => p.trim());
      if (parts.length >= 2) {
        inTabTable = true;
        tabRows.push(parts);
        continue;
      }
    }
    if (inTabTable) {
      flushTable();
    }
    out.push(line);
  }
  if (inTabTable) flushTable();
  return out.join('\n');
}

function _renderFindingCard(data) {
  const sev = String(data.severity || data.level || 'warning').toLowerCase();
  const taskId = data.task_id || data.task || data.id || '';
  const desc = data.description || data.desc || data.issue || data.summary || '';
  const evidence = data.evidence || data.notes || data.reason || '';

  const toneClass = (sev === 'critical' || sev === 'error' || sev === 'fail' || sev === 'block')
    ? 'is-block'
    : (sev === 'must_fix' || sev === 'mustfix')
    ? 'is-mustfix'
    : (sev === 'warning' || sev === 'warn')
    ? 'is-warn'
    : 'is-ok';

  let html = `<div class="ghost-telemetry-finding ${toneClass}">`;
  html += `<div class="telemetry-finding-header">`;
  html += `<span class="telemetry-badge ${toneClass}">${_esc(sev.toUpperCase())}</span>`;
  if (taskId) {
    html += `<span class="telemetry-task-chip">${_esc(taskId)}</span>`;
  }
  html += `</div>`;
  if (desc) {
    html += `<div class="telemetry-finding-desc">${_esc(desc)}</div>`;
  }
  if (evidence) {
    html += `<div class="telemetry-finding-evidence"><span class="evidence-tag">Evidence:</span> ${_esc(evidence)}</div>`;
  }
  html += `</div>`;
  return html;
}

function _renderReviewCard(data) {
  const verdict = String(data.verdict || 'REVISE').toUpperCase();
  const toneClass = (verdict === 'APPROVED' || verdict === 'DONE' || verdict === 'CLEAR')
    ? 'is-ok'
    : (verdict === 'REVISE' || verdict === 'WARN')
    ? 'is-warn'
    : 'is-block';

  const confText = typeof data.confidence === 'number'
    ? `${Math.round(data.confidence * 100)}% Confidence`
    : '';

  const issues = Array.isArray(data.issues) ? data.issues : [];

  let html = `<div class="ghost-telemetry-card ghost-telemetry-review ${toneClass}">`;
  html += `<div class="telemetry-finding-header">`;
  html += `<span class="telemetry-badge ${toneClass}">${_esc(verdict)}</span>`;
  if (confText) {
    html += `<span class="telemetry-confidence-pill">${_esc(confText)}</span>`;
  }
  html += `<span class="telemetry-deliverable-title">Manager Review</span>`;
  html += `</div>`;

  if (data.summary) {
    html += `<div class="telemetry-finding-desc">${_esc(data.summary)}</div>`;
  }

  if (issues.length > 0) {
    html += `<div class="telemetry-review-issues">`;
    issues.forEach(iss => {
      const sev = String(iss.severity || 'warning').toLowerCase();
      const issTone = (sev === 'critical' || sev === 'error' || sev === 'block') ? 'is-block' : (sev === 'must_fix' || sev === 'mustfix') ? 'is-mustfix' : 'is-warn';
      html += `<div class="telemetry-issue-item ${issTone}">`;
      html += `<div class="telemetry-issue-head">`;
      html += `<span class="telemetry-badge ${issTone}">${_esc(sev.toUpperCase())}</span>`;
      if (iss.task_id) html += `<span class="telemetry-task-chip">${_esc(iss.task_id)}</span>`;
      if (iss.description) html += `<span class="telemetry-issue-desc">${_esc(iss.description)}</span>`;
      html += `</div>`;
      if (iss.evidence) html += `<div class="telemetry-finding-evidence"><span class="evidence-tag">Evidence:</span> ${_esc(iss.evidence)}</div>`;
      if (iss.suggestion) html += `<div class="telemetry-finding-suggestion"><span class="suggestion-tag">💡 Suggestion:</span> ${_esc(iss.suggestion)}</div>`;
      html += `</div>`;
    });
    html += `</div>`;
  }

  html += `</div>`;
  return html;
}

function _renderDeliverableCard(data) {
  const status = String(data.status || 'DONE').toUpperCase();
  const toneClass = (status === 'DONE' || status === 'COMPLETED' || status === 'SUCCESS')
    ? 'is-ok'
    : (status === 'BLOCKED' || status === 'FAILED')
    ? 'is-block'
    : 'is-warn';

  const filesCreated = Array.isArray(data.files_created) ? data.files_created : [];
  const filesModified = Array.isArray(data.files_modified) ? data.files_modified : [];

  let html = `<div class="ghost-telemetry-card ghost-telemetry-deliverable ${toneClass}">`;
  html += `<div class="telemetry-finding-header">`;
  html += `<span class="telemetry-badge ${toneClass}">${_esc(status)}</span>`;
  if (data.task_id) html += `<span class="telemetry-task-chip">${_esc(data.task_id)}</span>`;
  html += `<span class="telemetry-deliverable-title">Task Deliverable</span>`;
  const fileSummary = [];
  if (filesCreated.length > 0) fileSummary.push(`+${filesCreated.length} created`);
  if (filesModified.length > 0) fileSummary.push(`${filesModified.length} edited`);
  if (fileSummary.length > 0) {
    html += `<span class="telemetry-file-pills">${_esc(fileSummary.join(' · '))}</span>`;
  }
  html += `</div>`;

  if (data.verification_details) {
    html += `<div class="telemetry-deliverable-section"><span class="section-label">Verification:</span> ${_esc(data.verification_details)}</div>`;
  }
  if (data.notes) {
    html += `<div class="telemetry-deliverable-section"><span class="section-label">Notes:</span> ${_esc(data.notes)}</div>`;
  }
  html += `</div>`;
  return html;
}

function _renderPlanCard(data) {
  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  const risks = Array.isArray(data.risks) ? data.risks : [];

  let html = `<div class="ghost-telemetry-card ghost-telemetry-plan">`;
  html += `<div class="telemetry-finding-header">`;
  html += `<span class="telemetry-badge is-strat">STRATEGIST PLAN</span>`;
  html += `<span class="telemetry-plan-stats">${tasks.length} task${tasks.length === 1 ? '' : 's'} · ${risks.length} risk${risks.length === 1 ? '' : 's'}</span>`;
  html += `</div>`;

  if (tasks.length > 0) {
    html += `<div class="telemetry-plan-tasks">`;
    tasks.forEach(t => {
      const deps = Array.isArray(t.depends_on) && t.depends_on.length > 0 ? ` (depends on ${t.depends_on.join(', ')})` : '';
      html += `<div class="telemetry-plan-task-row">`;
      html += `<span class="telemetry-task-chip">${_esc(t.id || 'TX')}</span>`;
      html += `<span class="telemetry-task-desc">${_esc(t.description || t.summary || '')}${_esc(deps)}</span>`;
      html += `</div>`;
    });
    html += `</div>`;
  }

  if (risks.length > 0) {
    html += `<div class="telemetry-plan-risks">`;
    html += `<div class="risks-header">⚠ Identified Risks</div>`;
    html += `<ul class="risks-list">`;
    risks.forEach(r => {
      html += `<li>${_esc(r)}</li>`;
    });
    html += `</ul>`;
    html += `</div>`;
  }

  html += `</div>`;
  return html;
}

function _renderToolCallsCard(data) {
  const calls = Array.isArray(data.tool_calls) ? data.tool_calls : [];
  let html = `<div class="ghost-tool-plan-deck"><div class="tool-plan-header"><span class="tool-plan-badge">TOOL INVOCATIONS</span></div><div class="tool-plan-chips">`;
  calls.forEach(c => {
    const tool = c.tool || 'tool';
    const params = c.parameters || {};
    const paramStr = Object.entries(params).map(([k, v]) => `${k}: ${v}`).join(', ');
    html += `<span class="tool-plan-chip"><span class="tool-plan-name">${_esc(tool)}</span> <span class="tool-plan-args">(${_esc(paramStr)})</span></span> `;
  });
  html += `</div></div>`;
  return html;
}

function _renderJsonTelemetryCard(parsed) {
  if (parsed.status && (parsed.summary || parsed.notes || parsed.files_modified || parsed.verification_details)) {
    return _renderDeliverableCard(parsed);
  }
  // Generic formatted JSON block
  try {
    const formatted = JSON.stringify(parsed, null, 2);
    return `<div class="ghost-telemetry-code"><div class="telemetry-code-header"><span class="telemetry-code-tag">STRUCTURED DATA</span></div><pre class="telemetry-code-body">${_esc(formatted)}</pre></div>`;
  } catch (e) {
    return '';
  }
}

function safeParseJson(str) {
  if (!str || typeof str !== 'string') return null;
  const trimmed = str.trim();
  try {
    return JSON.parse(trimmed);
  } catch (e1) {
    try {
      // Repair invalid escapes: backtick \`, single-quote \', or raw Windows backslashes
      let fixed = trimmed;
      fixed = fixed.replace(/\\\\/g, '\u0001');
      fixed = fixed.replace(/\\([`'])/g, '$1');
      fixed = fixed.replace(/\\(?![/\"bfnrt]|u[0-9a-fA-F]{4})/g, '\\\\');
      fixed = fixed.replace(/\u0001/g, '\\\\');
      fixed = fixed.replace(/,\s*([}\]])/g, '$1');
      return JSON.parse(fixed);
    } catch (e2) {
      return null;
    }
  }
}

function extractJsonBlocks(text) {
  const blocks = [];
  let i = 0;
  while (i < text.length) {
    if (text[i] === '{') {
      let depth = 0;
      let inString = false;
      let escaped = false;
      let startIndex = i;
      let found = false;

      for (let j = i; j < text.length; j++) {
        const char = text[j];
        if (escaped) {
          escaped = false;
          continue;
        }
        if (char === '\\') {
          escaped = true;
          continue;
        }
        if (char === '"') {
          inString = !inString;
          continue;
        }
        if (!inString) {
          if (char === '{') {
            depth++;
          } else if (char === '}') {
            depth--;
            if (depth === 0) {
              const candidate = text.slice(startIndex, j + 1);
              const parsed = safeParseJson(candidate);
              if (parsed && typeof parsed === 'object') {
                blocks.push({
                  start: startIndex,
                  end: j + 1,
                  raw: candidate,
                  parsed: parsed
                });
                i = j + 1;
                found = true;
                break;
              }
            }
          }
        }
      }
      if (found) continue;
    }
    i++;
  }
  return blocks;
}

// Render streamed LLM thought text as structured markdown (headers, lists,
// tables, code, paragraphs) with de-duplication, noise-stripping, and
// structured JSON parsing. Falls back to escaped text if the parser throws.
function _ghostMd(text, agent) {
  const s = String(text || '');
  if (!s) return '';
  const cacheKey = `${agent || ''}|${s}`;
  if (_ghostMd._cacheKey === cacheKey && _ghostMd._cacheVal !== undefined) {
    return _ghostMd._cacheVal;
  }
  try {
    let clean = _cleanNoiseAndTags(s);
    clean = _normalizeStreamBoundaries(clean);
    clean = _dedupeStatements(clean);
    clean = _formatToolDeclarations(clean);
    clean = _formatTabularData(clean);

    const jsonBlocks = extractJsonBlocks(clean);
    const cards = [];

    // Replace json blocks from end to start so character indices remain valid
    for (let b = jsonBlocks.length - 1; b >= 0; b--) {
      const block = jsonBlocks[b];
      const parsed = block.parsed;
      let cardHtml = '';

      if (parsed.verdict || (parsed.issues && Array.isArray(parsed.issues))) {
        cardHtml = _renderReviewCard(parsed);
      } else if (parsed.status && (parsed.notes || parsed.verification_details || parsed.files_created || parsed.files_modified || parsed.summary)) {
        cardHtml = _renderDeliverableCard(parsed);
      } else if (parsed.tasks && Array.isArray(parsed.tasks)) {
        cardHtml = _renderPlanCard(parsed);
      } else if (parsed.tool_calls && Array.isArray(parsed.tool_calls)) {
        cardHtml = _renderToolCallsCard(parsed);
      } else if (parsed.severity || (parsed.description && parsed.evidence)) {
        cardHtml = _renderFindingCard(parsed);
      } else {
        cardHtml = _renderJsonTelemetryCard(parsed);
      }

      const cardIdx = cards.length;
      cards.push(cardHtml);
      clean = clean.slice(0, block.start) + `\n\n__TELEMETRY_CARD_${cardIdx}__\n\n` + clean.slice(block.end);
    }

    let html = markdownModule.mdToHtml(markdownModule.squashOutsideCode(clean));

    cards.forEach((card, idx) => {
      const placeholder = `__TELEMETRY_CARD_${idx}__`;
      const wrappedRegex = new RegExp(`<p>\\s*${placeholder}\\s*<\\/p>`, 'g');
      if (wrappedRegex.test(html)) {
        html = html.replace(wrappedRegex, card);
      } else {
        html = html.replace(placeholder, card);
      }
    });

    _ghostMd._cacheKey = cacheKey;
    _ghostMd._cacheVal = html;
    return html;
  } catch (e) {
    return _esc(s);
  }
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

function _roleMeta(agent) {
  const key = String(agent || '').toLowerCase().trim();
  const map = {
    implementer: { name: 'Implementer', cls: 'impl' },
    impl: { name: 'Implementer', cls: 'impl' },
    perspective_analyzer: { name: 'Perspective Analyzer', cls: 'strat' },
    perspective: { name: 'Perspective Analyzer', cls: 'strat' },
    chair: { name: 'Chair', cls: 'chair' },
    chairperson: { name: 'Chair', cls: 'chair' },
    strategist: { name: 'Strategist', cls: 'strat' },
    strat: { name: 'Strategist', cls: 'strat' },
    manager: { name: 'Manager', cls: 'mgr' },
    mgr: { name: 'Manager', cls: 'mgr' },
    completeness_auditor: { name: 'Completeness Auditor', cls: 'mgr' },
    system: { name: 'System', cls: 'sys' },
    sys: { name: 'System', cls: 'sys' }
  };
  if (map[key]) return map[key];
  if (!key) return { name: 'Council', cls: 'sys' };
  const capitalized = key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, ' ');
  return { name: capitalized, cls: 'sys' };
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

function computeLineDiff(originalText, currentText) {
  const a = originalText ? String(originalText).split('\n') : [];
  const b = currentText ? String(currentText).split('\n') : [];

  // Trim common prefix
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) {
    start++;
  }

  // Trim common suffix
  let aEnd = a.length - 1;
  let bEnd = b.length - 1;
  while (aEnd >= start && bEnd >= start && a[aEnd] === b[bEnd]) {
    aEnd--;
    bEnd--;
  }

  const result = [];
  for (let i = 0; i < start; i++) {
    result.push({ type: 'same', text: a[i], lineNum: i + 1 });
  }

  const aMid = a.slice(start, aEnd + 1);
  const bMid = b.slice(start, bEnd + 1);

  if (aMid.length === 0) {
    for (let i = 0; i < bMid.length; i++) {
      result.push({ type: 'add', text: bMid[i], lineNum: start + i + 1 });
    }
  } else if (bMid.length === 0) {
    for (let i = 0; i < aMid.length; i++) {
      result.push({ type: 'del', text: aMid[i], lineNum: null });
    }
  } else if (aMid.length * bMid.length <= 1000000) {
    const dp = Array.from({ length: aMid.length + 1 }, () => new Uint32Array(bMid.length + 1));
    for (let i = 1; i <= aMid.length; i++) {
      for (let j = 1; j <= bMid.length; j++) {
        if (aMid[i - 1] === bMid[j - 1]) {
          dp[i][j] = dp[i - 1][j - 1] + 1;
        } else {
          dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
        }
      }
    }

    let i = aMid.length;
    let j = bMid.length;
    const midDiff = [];
    while (i > 0 || j > 0) {
      if (i > 0 && j > 0 && aMid[i - 1] === bMid[j - 1]) {
        midDiff.unshift({ type: 'same', text: aMid[i - 1] });
        i--;
        j--;
      } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
        midDiff.unshift({ type: 'add', text: bMid[j - 1] });
        j--;
      } else if (i > 0 && (j === 0 || dp[i][j - 1] < dp[i - 1][j])) {
        midDiff.unshift({ type: 'del', text: aMid[i - 1] });
        i--;
      }
    }
    let curLine = start + 1;
    midDiff.forEach(d => {
      if (d.type === 'del') {
        result.push({ type: 'del', text: d.text, lineNum: null });
      } else {
        result.push({ type: d.type, text: d.text, lineNum: curLine++ });
      }
    });
  } else {
    aMid.forEach(line => result.push({ type: 'del', text: line, lineNum: null }));
    let curLine = start + 1;
    bMid.forEach(line => result.push({ type: 'add', text: line, lineNum: curLine++ }));
  }

  let curLine = result.filter(r => r.type !== 'del').length + 1;
  for (let i = aEnd + 1; i < a.length; i++) {
    result.push({ type: 'same', text: a[i], lineNum: curLine++ });
  }

  return result;
}

function _injectRemediationStyles() {
  if (typeof document === 'undefined' || document.getElementById('council-remediation-styles')) return;
  const style = document.createElement('style');
  style.id = 'council-remediation-styles';
  style.textContent = `
    .council-dag-rail {
      display: flex;
      align-items: center;
      gap: 6px;
      min-height: 28px;
      max-height: 32px;
      padding: 4px 12px;
      background: var(--bg, #090705);
      border-bottom: 1px solid var(--border, #2a221b);
      overflow-x: auto;
      overflow-y: hidden;
      white-space: nowrap;
      user-select: none;
    }
    .council-dag-rail::-webkit-scrollbar { height: 3px; }
    .council-dag-rail::-webkit-scrollbar-thumb { background: var(--border, #2a221b); border-radius: 2px; }
    .council-dag-pill {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      height: 20px;
      padding: 0 8px;
      border-radius: 10px;
      font-size: 10px;
      font-family: var(--font-mono, monospace);
      background: var(--panel, #120e0b);
      border: 1px solid var(--border, #2a221b);
      color: var(--fg, #e2dcd5);
      cursor: pointer;
      transition: all 0.15s ease;
      flex-shrink: 0;
    }
    .council-dag-pill:hover { border-color: var(--compass-accent, #df8e45); }
    .council-dag-pill .dag-pill-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
    .council-dag-pill.dag-pill--done { border-color: var(--impl, #4eb870); background: rgba(78, 184, 112, 0.12); color: var(--impl, #4eb870); }
    .council-dag-pill.dag-pill--in-progress { border-color: var(--warn, #df8e45); background: rgba(223, 142, 69, 0.15); color: var(--warn, #df8e45); animation: cc-node-pulse 2s infinite ease-in-out; }
    .council-dag-pill.dag-pill--failed { border-color: var(--fail, #e05858); background: rgba(224, 88, 88, 0.15); color: var(--fail, #e05858); }
    .council-dag-pill.dag-pill--blocked { border-color: var(--warn, #df8e45); background: rgba(223, 142, 69, 0.15); color: var(--warn, #df8e45); }
    .council-dag-pill.dag-pill--pending { color: var(--muted, #736b63); opacity: 0.8; }
    .council-dag-overlay {
      position: fixed;
      inset: 0;
      z-index: 9999;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .council-dag-backdrop {
      position: absolute;
      inset: 0;
      background: rgba(0, 0, 0, 0.72);
      backdrop-filter: blur(4px);
    }
    .council-dag-modal {
      position: relative;
      width: 90vw;
      max-width: 1100px;
      height: 82vh;
      background: var(--panel, #120e0b);
      border: 1px solid var(--border, #2a221b);
      border-radius: 8px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      z-index: 1;
    }
    .council-dag-modal-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 16px;
      border-bottom: 1px solid var(--border, #2a221b);
      background: var(--bg, #090705);
    }
    .council-dag-modal-close {
      background: transparent;
      border: none;
      font-size: 20px;
      color: var(--muted, #736b63);
      cursor: pointer;
      padding: 2px 8px;
      border-radius: 4px;
    }
    .council-dag-modal-close:hover { color: var(--fg, #e2dcd5); background: rgba(255, 255, 255, 0.08); }
    .council-dag-modal-body {
      flex: 1;
      overflow: auto;
      padding: 24px;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .council-autofollow-pill {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 9px;
      font-weight: 600;
      letter-spacing: .04em;
      text-transform: uppercase;
      padding: 2px 6px;
      border-radius: 3px;
      cursor: pointer;
      user-select: none;
      margin-left: 8px;
      transition: all 0.15s ease;
    }
    .diff-line {
      display: flex;
      align-items: flex-start;
      line-height: 1.5;
      font-family: var(--font-mono, monospace);
      white-space: pre;
    }
    .diff-line.diff-line-add {
      background: rgba(78, 184, 112, 0.12);
      border-left: 3px solid var(--impl, #4eb870);
      padding-left: 4px;
    }
    .diff-line.diff-line-del {
      background: rgba(224, 88, 88, 0.12);
      border-left: 3px solid var(--fail, #e05858);
      padding-left: 4px;
      text-decoration: line-through;
      opacity: 0.75;
    }
    .diff-gutter {
      display: inline-block;
      width: 14px;
      flex-shrink: 0;
    }
  `;
  document.head.appendChild(style);
}

function initResizers() {
  try {
    const resizerStreamCtx = document.getElementById('council-resizer-stream-ctx');
    const workspace = document.querySelector('.council-workspace');
    const ctxPane = document.querySelector('.council-ctx-pane');

    const applyCtxWidth = (width) => {
      if (!ctxPane) return;
      ctxPane.style.flex = `0 0 ${width}px`;
      ctxPane.style.width = `${width}px`;
    };

    // Restore saved ctx width if present
    try {
      const savedCtxWidth = localStorage.getItem('council_ctx_pane_width');
      if (savedCtxWidth && ctxPane) {
        applyCtxWidth(Number(savedCtxWidth));
      }
    } catch {}

    if (resizerStreamCtx && workspace && ctxPane) {
      setupResizer(resizerStreamCtx, (moveEvent) => {
        const rect = workspace.getBoundingClientRect();
        if (!rect.width) return;
        const rightWidth = Math.max(240, Math.min(rect.width - 320, rect.right - moveEvent.clientX));
        const rounded = Math.round(rightWidth);
        applyCtxWidth(rounded);
        try { localStorage.setItem('council_ctx_pane_width', rounded); } catch {}
      });

      resizerStreamCtx.addEventListener('keydown', (e) => {
        const rect = workspace.getBoundingClientRect();
        const currentWidth = ctxPane.getBoundingClientRect().width || 380;
        let nextWidth = currentWidth;
        if (e.key === 'ArrowLeft') nextWidth = Math.min(rect.width - 320, currentWidth + 20);
        else if (e.key === 'ArrowRight') nextWidth = Math.max(240, currentWidth - 20);
        else return;
        e.preventDefault();
        const rounded = Math.round(nextWidth);
        applyCtxWidth(rounded);
        try { localStorage.setItem('council_ctx_pane_width', rounded); } catch {}
      });
    }
  } catch (err) {
    console.error('[Council] Failed to initialize resizers:', err);
  }
}

function setupResizer(resizer, onDrag) {
  resizer.addEventListener('mousedown', (e) => {
    e.preventDefault();
    resizer.classList.add('resizing');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    function onMouseMove(moveEvent) {
      try {
        onDrag(moveEvent);
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

  // Inject styles for DAG rail, overlay, diff lines, and autofollow pill
  _injectRemediationStyles();

  // Setup resizers
  initResizers();

  // Initial render to set correct idle state
  ui.render(state);

  // Restore workspace input from localStorage & synchronize state
  const wsInput = document.getElementById('council-workspace-input');
  if (wsInput) {
    try {
      const saved = localStorage.getItem('councilWorkspace');
      if (saved) {
        wsInput.value = saved;
        state.workspace = saved.trim();
      }
    } catch {}
    const syncWorkspace = () => {
      const val = wsInput.value.trim();
      state.workspace = val;
      try { localStorage.setItem('councilWorkspace', val); } catch {}
    };
    wsInput.addEventListener('change', syncWorkspace);
    wsInput.addEventListener('input', syncWorkspace);
  }

  // Restore budget input from localStorage & synchronize state
  const budgetInput = document.getElementById('council-budget-input');
  if (budgetInput) {
    try {
      const saved = localStorage.getItem('councilBudget');
      if (saved) {
        budgetInput.value = saved;
        state.contextBudget = Number(saved) || 0;
      }
    } catch {}
    const syncBudget = () => {
      const val = budgetInput.value.trim();
      state.contextBudget = val ? Number(val) : 0;
      try { localStorage.setItem('councilBudget', val); } catch {}
    };
    budgetInput.addEventListener('change', syncBudget);
    budgetInput.addEventListener('input', syncBudget);
  }

  // Setup 1s timer to update LAST RESPONSE indicator
  setInterval(() => {
    // Keep the active-agent elapsed timer ticking even when no events arrive,
    // so a long-running step reads as "working · 2m14s" rather than frozen.
    if (state.status === 'IN_PROGRESS' || state.status === 'BLOCKED') {
      const agentEl = document.getElementById('council-ghost-agent');
      if (agentEl && state.activeAgent && state.activeAgentSince) {
        agentEl.innerHTML = ui._ghostBadgeHtml(state);
        agentEl.title = ui._ghostBadgeText(state);
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
