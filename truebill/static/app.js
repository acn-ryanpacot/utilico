/* AI-TrueBILL UI - Alpine.js 3 (vendored), no other libraries. Every request carries X-User-Id; the server
   enforces every permission. Live updates arrive over Server-Sent Events (fetch-based reader, so the user header
   is sent on the stream too). */
const NAV = [
  ['Overview', ['dashboard', 'mywork', 'lookup', 'timeline']],
  ['Cases', ['cases']],
  ['POC 1 detector', ['backscan', 'daily']],
  ['Data & assurance', ['explorer', 'knowledge', 'audit', 'selftest']],
];
const LIFECYCLE = ['NEW', 'RETRIEVING', 'RECONCILING', 'RECOMMENDING', 'TRIAGE', 'AWAITING_DRI', 'DECIDED'];
const AGENT_STATES = ['NEW', 'RETRIEVING', 'RECONCILING', 'RECOMMENDING'];
const RULE_NAMES = { R1: 'Billed after disconnect', R2: 'Estimated reads after disconnect', R3: 'Complaint without billing hold', R4: 'Disputed revenue posted',
  PB1: 'SA active despite stop', PB2: 'Integration message failed', PB3: 'ID near-match' };
const PERSONA = { CSR: 'CSR / Billing Ops', DRI: 'DRI' };
const LANES = ['OMS', 'CCB', 'MDM', 'CRM', 'GL'];

function money(n) { if (n === null || n === undefined || n === '') return '—'; const v = Number(n); return (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function pct(x) { return x === null || x === undefined ? '—' : (x * 100).toFixed(x === 1 || x === 0 ? 0 : 1) + '%'; }
function hhmm(iso) { return iso ? iso.replace('T', ' ').slice(0, 16) : ''; }

function trueBill() {
  return {
    users: [], agent: null, knowledgeMode: null, me: null, perms: {}, labels: {}, datasets: [], scenarios: [], presets: [],
    dsId: 'dataset_A_with_errors', scId: 'DISPUTE_REPLAY', route: { screen: 'dashboard', id: null }, nav: NAV,
    st: null, data: {}, caseData: null, toastMsg: null, toastErr: false, labelsOn: false, feedFilter: 'all',
    auditF: {}, auditVerify: null, selftest: null, selftestRunning: false, explorer: { file: 'oms_work_orders.csv', q: '', page: 0 },
    lookupAcc: '', lookupResult: null, lookupBusy: false,
    knowledgeQ: 'Commercial disconnect not synced; full cycle billed', knowledgeColl: 'case_history', dailyAsOf: '2026-11-02',
    form: {}, sse: null, sseOn: false, refreshTimer: null, busy: false, theme: 'auto', guideHidden: false,
    LIFECYCLE, LANES, RULE_NAMES, PERSONA, money, pct, hhmm,

    async init() {
      try { const j = await (await fetch('/api/users')).json(); this.users = j.users; this.agent = j.agent; this.knowledgeMode = j.knowledge.mode; }
      catch (e) { this.toast('Cannot reach the local server: ' + e, true); }
      const saved = (() => { try { return JSON.parse(localStorage.getItem('tb.ctx') || '{}'); } catch (e) { return {}; } })();
      if (saved.dsId) this.dsId = saved.dsId; if (saved.scId) this.scId = saved.scId;
      window.addEventListener('hashchange', () => this.readHash());
    },
    hdrs() { return { 'X-User-Id': this.me ? this.me.user_id : '', 'X-Dataset-Id': this.dsId, 'X-Scenario-Id': this.scId }; },
    async api(path, opt = {}) {
      const r = await fetch(path, { method: opt.method || 'GET', headers: Object.assign(this.hdrs(), opt.body ? { 'Content-Type': 'application/json' } : {}),
        body: opt.body ? JSON.stringify(opt.body) : undefined, cache: 'no-store' });
      if (opt.raw) { if (!r.ok) throw new Error((await r.json()).error); return r; }
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { const e = new Error(j.error || ('HTTP ' + r.status)); e.status = r.status; throw e; }
      return j;
    },
    toast(msg, err) { this.toastMsg = msg; this.toastErr = !!err; clearTimeout(this._tt); this._tt = setTimeout(() => { this.toastMsg = null; }, err ? 7000 : 3500); },
    uname(id) { const u = this.users.find(x => x.user_id === id); return u ? u.name : id; },
    initials(n) { return (n || '').split(/\s+/).map(x => x[0]).join('').slice(0, 2); },
    can(screen) { return this.perms[screen] && this.perms[screen] !== '-'; },
    lvl(screen) { return this.perms[screen] || '-'; },
    code(acc) { return this.labelsOn && this.st && this.st.case_codes ? this.st.case_codes[acc] : null; },

    // ---- sign-in / context ----------------------------------------------------------------------------------
    async signIn(uid) {
      const prev = this.me ? this.me.user_id : null;
      this.me = this.users.find(u => u.user_id === uid);
      await this.api('/api/session/sign_in', { method: 'POST', body: { user_id: uid, previous_user_id: prev } });
      const me = await this.api('/api/me'); this.perms = me.permissions; this.labels = me.screens;
      const d = await this.api('/api/datasets'); this.datasets = d.datasets; this.scenarios = d.scenarios; this.presets = d.presets;
      this.connectSSE();
      if (!location.hash || !this.can(location.hash.replace(/^#\/?/, '').split('/')[0])) location.hash = '#/dashboard'; else this.readHash();
      await this.refreshState();
    },
    signOut() { this.me = null; this.stopSSE(); },
    async switchCtx(kind) {
      try { localStorage.setItem('tb.ctx', JSON.stringify({ dsId: this.dsId, scId: this.scId })); } catch (e) { }
      await this.api('/api/context', { method: 'POST', body: { changed: kind } });
      this.connectSSE(); this.caseData = null;
      if (this.route.screen === 'case') location.hash = '#/cases'; else this.load();
      this.refreshState();
    },
    readHash() {
      const p = location.hash.replace(/^#\/?/, '').split('/');
      this.route = { screen: p[0] || 'dashboard', id: p[1] ? decodeURIComponent(p[1]) : null };
      this.form = {}; this.load(true);
    },
    go(screen, id) { location.hash = '#/' + screen + (id ? '/' + encodeURIComponent(id) : ''); },

    // ---- loading -----------------------------------------------------------------------------------------
    async refreshState() { if (!this.me) return; try { this.st = await this.api('/api/state'); } catch (e) { } },
    async load(first) {
      if (!this.me) return;
      const s = this.route.screen;
      if (!this.can(s) && s !== 'case') { this.data = { denied: true }; return; }
      try {
        if (s === 'dashboard') this.data = await this.api('/api/dashboard?feed=' + this.feedFilter);
        else if (s === 'mywork') this.data = await this.api('/api/mywork');
        else if (s === 'cases') this.data = await this.api('/api/cases');
        else if (s === 'case') this.caseData = await this.api('/api/cases/' + encodeURIComponent(this.route.id) + (first ? '?view=1' : ''));
        else if (s === 'lookup') this.data = await this.api('/api/lookup');
        else if (s === 'backscan') this.data = await this.api('/api/backscan');
        else if (s === 'daily') this.data = await this.api('/api/daily?as_of=' + this.dailyAsOf);
        else if (s === 'explorer') this.data = await this.api('/api/explorer?' + new URLSearchParams(this.explorer));
        else if (s === 'knowledge') this.data = await this.api('/api/knowledge?' + new URLSearchParams({ q: this.knowledgeQ, collection: this.knowledgeColl }));
        else if (s === 'audit') this.data = await this.api('/api/audit?' + new URLSearchParams(Object.fromEntries(Object.entries(this.auditF).filter(([k, v]) => v))));
        else if (s === 'timeline' || s === 'selftest') this.data = {};
        if (s !== 'case') this.data.denied = false;
      } catch (e) { if (e.status === 403) this.data = { denied: true, msg: e.message }; else this.toast(e.message, true); }
    },
    scheduleRefresh() {
      clearTimeout(this.refreshTimer);
      this.refreshTimer = setTimeout(() => { this.refreshState(); if (!this.editing()) this.load(false); }, 300);
    },
    editing() { const a = document.activeElement; return a && /INPUT|TEXTAREA|SELECT/.test(a.tagName) && a.closest('main'); },

    // ---- SSE ----------------------------------------------------------------------------------------------
    stopSSE() { if (this.sse) { this.sse.abort(); this.sse = null; } this.sseOn = false; },
    async connectSSE() {
      this.stopSSE();
      const ctl = new AbortController(); this.sse = ctl;
      try {
        const r = await fetch('/api/stream', { headers: this.hdrs(), signal: ctl.signal });
        this.sseOn = true;
        const rd = r.body.getReader(); const dec = new TextDecoder(); let buf = '';
        for (;;) {
          const { value, done } = await rd.read(); if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf('\n\n')) >= 0) {
            const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
            const data = chunk.split('\n').filter(l => l.startsWith('data: ')).map(l => l.slice(6)).join('\n');
            if (data) { try { this.onEvent(JSON.parse(data)); } catch (e) { } }
          }
        }
      } catch (e) { }
      this.sseOn = false;
      if (this.sse === ctl && this.me) setTimeout(() => { if (this.sse === ctl) this.connectSSE(); }, 2000);
    },
    onEvent(m) {
      if (m.type === 'activity' && this.caseData && this.caseData.case_id === m.case_id) { this.caseData.activity.push(m.entry); return; }
      if (m.type === 'audit' && this.route.screen === 'dashboard' && this.data.feed && this.feedFilter === 'all') {
        this.data.feed.unshift(m.event); this.data.feed = this.data.feed.slice(0, 25);
      }
      if (m.type === 'timeline' && this.st) { this.st.timeline.clock = m.clock; this.st.timeline.playing = m.playing; this.st.timeline.paused_reason = m.paused_reason; }
      if (m.type === 'activity' || (m.type === 'audit' && m.event.action === 'AGENT_TOOL_CALL')) return;
      this.scheduleRefresh();
    },

    // ---- timeline / demo ------------------------------------------------------------------------------------
    async tl(op, body) { try { const r = await this.api('/api/timeline/' + op, { method: 'POST', body: body || {} }); if (r.timeline && this.st) this.st.timeline = r.timeline; this.scheduleRefresh(); } catch (e) { this.toast(e.message, true); } },
    async demo(op, body, msg) { if (op === 'guided') this.guideHidden = false; try { const r = await this.api('/api/demo/' + op, { method: 'POST', body: body || {} }); if (msg) this.toast(msg); this.scheduleRefresh(); return r; } catch (e) { this.toast(e.message, true); } },
    toggle(name, value) { this.tl('toggle', { name, value }); },
    scrubX(at) {
      const t = this.st.timeline; const a = Date.parse(t.start.replace(' ', 'T')), b = Date.parse(t.end.replace(' ', 'T'));
      return b > a ? 6 + (Date.parse(at.replace(' ', 'T')) - a) / (b - a) * 988 : 500;
    },
    markerColor(e) { return e.status === 'DONE' ? 'var(--faint)' : e.status === 'BLOCKED' ? 'var(--high)' : e.type === 'BILL_READY' ? 'var(--accent)' : e.type === 'DISPUTE_OPENED' ? 'var(--high)' : e.type === 'DISCONNECT_FOLLOWUP_DUE' ? 'var(--agent)' : 'var(--med)'; },
    markersSvg() {
      if (!this.st) return '';
      const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
      return this.st.timeline.events.map(e => `<rect x="${(this.scrubX(e.at) - 2).toFixed(1)}" y="5" width="4" height="20" rx="1" fill="${this.markerColor(e)}"><title>${esc(e.at + ' ' + e.type + ' ' + e.label + ' (' + e.status + ')')}</title></rect>`).join('');
    },
    pausedText() { const r = this.st && this.st.timeline.paused_reason; return r === 'humans' ? 'Paused for humans' : r === 'stale' ? 'Blocked: stale MDM feed' : r === 'end' ? 'End of scenario' : r === 'target' ? 'Preset reached' : ''; },

    // ---- case actions -----------------------------------------------------------------------------------------
    allowed(a) { return this.caseData && this.caseData.allowed_actions.includes(a); },
    async act(action, extra) {
      const body = Object.assign({ action, reason: this.form.reason || '', note: this.form.note || '' }, extra || {});
      try { await this.api('/api/cases/' + encodeURIComponent(this.caseData.case_id) + '/action', { method: 'POST', body });
        this.toast(action.replace(/_/g, ' ').toLowerCase() + ' — done'); this.form = {}; await this.load(false); this.refreshState(); }
      catch (e) { this.toast(e.message, true); }
    },
    decide() {
      const k = this.form.kind || 'APPROVE_RECOMMENDED';
      const final = { APPROVE_RECOMMENDED: 'APPROVE the recommendation', CHOOSE_OTHER: 'APPROVE another option', RELEASE_AS_IS: 'REJECT - release as-is', CANCEL_FALSE_POSITIVE: 'REJECT - cancel as a false positive' }[k];
      if (!confirm(final + '? This decision is final: it ends and seals the case.')) return;
      this.act('DECIDE', { kind: k, option: this.form.option || 'SUPPRESS' });
    },
    async runLookup() {
      const acc = (this.lookupAcc || '').trim(); if (!acc) { this.toast('Enter an account number', true); return; }
      this.lookupBusy = true; this.lookupResult = null;
      try { this.lookupResult = await this.api('/api/lookup', { method: 'POST', body: { account_id: acc } }); this.load(false); }
      catch (e) { this.lookupResult = { status: 'ERROR', message: e.message }; }
      this.lookupBusy = false;
    },
    lookupState(id) { const i = (this.data.items || []).find(x => x.case_id === id); return i ? i.state : null; },
    stateClass(s) { return s === 'APPROVED' ? 'done' : s === 'REJECTED' ? 'bad' : s === 'NO_ISSUE' ? 'off' : AGENT_STATES.includes(s) ? 'agent' : ''; },
    lifecycleClass(c, s) {
      const decided = ['APPROVED', 'REJECTED', 'NO_ISSUE'].includes(c.state);
      if (s === 'DECIDED') return decided ? 'cur' : '';
      const seen = new Set(c.history.map(h => h.to)); return s === c.state ? 'cur' : (seen.has(s) || decided && LIFECYCLE.indexOf(s) < 6) ? 'done' : '';
    },
    lifecycleLabel(c, s) { return s === 'DECIDED' ? (['APPROVED', 'REJECTED', 'NO_ISSUE'].includes(c.state) ? c.state.toLowerCase().replace('_', ' ') : 'approved / rejected') : s.toLowerCase().replace(/_/g, ' '); },
    laneEntries(lane) { return (this.caseData.activity || []).filter(a => a.agent === 'agent.retrieval' && a.lane === lane); },
    otherActivity() { return (this.caseData.activity || []).filter(a => a.agent !== 'agent.retrieval').slice().reverse(); },
    fact(fid) { return (this.caseData.facts || []).find(f => f.fact_id === fid); },
    openFact(f) { this.explorer = { file: f.file, q: f.record_id, page: 0 }; this.go('explorer'); },
    explanation(rule) { const e = ((this.caseData.reconciliation || {}).explanations || []).find(x => x.rule === rule); return e; },
    nearFact() { return (this.caseData.facts || []).find(f => f.kind === 'near_match'); },
    readsFact() { return (this.caseData.facts || []).find(f => (f.kind || '').startsWith('read_window') && f.fields.purpose === 'post-disconnect'); },
    async backscanSend(wo) { try { const r = await this.api('/api/backscan/send', { method: 'POST', body: { wo_ids: wo ? [wo] : null } }); this.toast((r.cases.length || 0) + ' case(s) opened — agents are working'); this.load(false); } catch (e) { this.toast(e.message, true); } },

    // ---- audit / self-test ---------------------------------------------------------------------------------
    async auditExport() { try { const r = await this.api('/api/audit/export?' + new URLSearchParams(Object.fromEntries(Object.entries(this.auditF).filter(([k, v]) => v))), { raw: true });
      const url = URL.createObjectURL(await r.blob()); const a = document.createElement('a'); a.href = url; a.download = 'audit_log.csv'; a.click(); } catch (e) { this.toast(e.message, true); } },
    async auditVerifyRun() { try { this.auditVerify = await this.api('/api/audit/verify', { method: 'POST', body: {} }); } catch (e) { this.toast(e.message, true); } },
    async runSelftest() { this.selftestRunning = true; this.selftest = null; try { this.selftest = await this.api('/api/selftest'); } catch (e) { this.toast(e.message, true); } this.selftestRunning = false; },
    bars(obj) { const m = Math.max(1, ...Object.values(obj || {})); return Object.entries(obj || {}).map(([k, v]) => ({ k, v, w: (v / m * 100) + '%' })); },
    who(e) { return e.actor_type === 'USER' ? e.actor_name + ' · ' + (PERSONA[e.actor_persona] || e.actor_persona) : e.actor_name; },
    detail(e) {
      const a = e.after || {}; const bits = [];
      if (e.before && e.before.state && a.state) bits.push(e.before.state + ' → ' + a.state);
      if (e.action === 'AGENT_TOOL_CALL') bits.push((a.lane ? a.lane + ' · ' : '') + (a.result || a.guardrail || ''));
      else if (a.fired) bits.push('fired ' + a.fired.join(', '));
      else if (a.option) bits.push(a.option + (a.confidence ? ' · conf ' + a.confidence : ''));
      else if (a.type) bits.push(a.type + ' ' + (a.priority || '') + ' ' + (a.amount !== undefined ? money(a.amount) : ''));
      else if (a.rules) bits.push((a.rules.join(',') || 'no rule') + (a.total !== undefined ? ' · ' + money(a.total) : ''));
      else if (a.total !== undefined) bits.push(money((e.before || {}).total) + ' → ' + money(a.total));
      else if (a.result) bits.push(a.result);
      if (e.reason) bits.push('“' + e.reason + '”');
      return bits.join(' · ').slice(0, 240);
    },
  };
}
document.addEventListener('alpine:init', () => { Alpine.data('trueBill', trueBill); });
