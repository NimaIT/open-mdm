/* MDM Platform — React SPA (no build step; htm tagged templates).
 *
 * React is vendored locally, so this runs in air-gapped networks with no CDN
 * and no bundler. Every view here is a thin client over /api/v1.
 */
const { createElement, useState, useEffect, useCallback, useMemo, useRef, Fragment } = React;
const html = htm.bind(createElement);
const API = '/api/v1';

/* ============================================================ http helper */
async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    credentials: 'same-origin',
    headers: opts.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...opts,
  });
  const ctype = res.headers.get('content-type') || '';
  let payload = null;
  if (ctype.includes('application/json')) payload = await res.json();
  else payload = await res.text();

  if (!res.ok) {
    let msg = 'Request failed';
    const d = payload && payload.detail;
    if (typeof d === 'string') msg = d;
    else if (d && d.message) msg = d.message;
    else if (Array.isArray(d)) msg = d.map((e) => `${(e.loc || []).slice(-1)}: ${e.msg}`).join('; ');
    else if (typeof payload === 'string' && payload) msg = payload.slice(0, 300);
    const err = new Error(msg);
    err.status = res.status;
    err.detail = d;
    throw err;
  }
  return payload;
}

/* ---------------------------------------------------------------- styling
 * React requires `style` to be an object, not a CSS string. htm passes
 * attribute values through verbatim, so `style="margin:4px"` throws
 * (React error #62) and takes the whole tree down. sx() converts a CSS
 * declaration string into the object React expects.
 */
const _sxCache = new Map();
function sx(css) {
  if (!css) return undefined;
  if (typeof css === 'object') return css;
  if (_sxCache.has(css)) return _sxCache.get(css);
  const style = {};
  for (const decl of String(css).split(';')) {
    const idx = decl.indexOf(':');
    if (idx < 1) continue;
    const prop = decl.slice(0, idx).trim();
    const value = decl.slice(idx + 1).trim();
    if (!prop || !value) continue;
    // Preserve custom properties; camel-case standard ones.
    const key = prop.startsWith('--')
      ? prop
      : prop.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    style[key] = value;
  }
  _sxCache.set(css, style);
  return style;
}

/* ------------------------------------------------------- error boundary
 * Without this, any render-time exception yields a silently blank page —
 * which is exactly how the bug above manifested.
 */
class ErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  componentDidCatch(error, info) { console.error('UI render failure:', error, info); }
  render() {
    if (!this.state.error) return this.props.children;
    return html`
      <div style=${sx('padding:40px;max-width:760px;margin:0 auto')}>
        <div class="banner banner-err">
          <div>
            <strong>The interface hit an unexpected error.</strong>
            <div>Reload to try again. If it persists, the detail below helps diagnose it.</div>
            <pre class="sql" style=${sx('margin-top:12px')}>${String(this.state.error && this.state.error.message || this.state.error)}</pre>
          </div>
        </div>
        <button class="btn btn-primary" onClick=${() => location.reload()}>Reload</button>
      </div>`;
  }
}

/* ============================================================ primitives */
const Spinner = ({ label }) => html`
  <div class="loading"><span class="spinner"></span>${label || 'Loading…'}</div>`;

const Banner = ({ kind = 'info', title, children }) => html`
  <div class="banner banner-${kind}">
    <div>
      ${title && html`<strong>${title}</strong>`}
      <div>${children}</div>
    </div>
  </div>`;

const Pill = ({ kind = 'mute', children }) => html`
  <span class="pill pill-${kind}">${children}</span>`;

const statusPill = (s) => {
  const map = {
    published: 'ok', draft: 'mute', modified: 'warn', deprecated: 'err',
    pending_review: 'warn', approved: 'ok', applied: 'ok', rejected: 'err',
    changes_requested: 'warn', error: 'err', pending: 'info', promoted: 'info',
  };
  return html`<${Pill} kind=${map[s] || 'mute'}>${(s || '').replace(/_/g, ' ')}<//>`;
};

const Empty = ({ title, children }) => html`
  <div class="empty"><div class="big">${title}</div><div>${children}</div></div>`;

function Modal({ title, onClose, children, footer, wide }) {
  useEffect(() => {
    const h = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onClose]);
  return html`
    <div class="modal-back" onClick=${(e) => e.target.classList.contains('modal-back') && onClose()}>
      <div class="modal ${wide ? 'wide' : ''}">
        <div class="modal-head">
          <h3>${title}</h3>
          <button class="x-btn" onClick=${onClose} title="Close">×</button>
        </div>
        <div class="modal-body">${children}</div>
        ${footer && html`<div class="modal-foot">${footer}</div>`}
      </div>
    </div>`;
}

/* toast bus */
let toastSeq = 0;
const toastListeners = new Set();
function notify(message, kind = 'ok') {
  const t = { id: ++toastSeq, message, kind };
  toastListeners.forEach((fn) => fn(t));
}
function Toasts() {
  const [items, setItems] = useState([]);
  useEffect(() => {
    const add = (t) => {
      setItems((cur) => [...cur, t]);
      setTimeout(() => setItems((cur) => cur.filter((x) => x.id !== t.id)), 5200);
    };
    toastListeners.add(add);
    return () => toastListeners.delete(add);
  }, []);
  return html`<div class="toast-wrap">
    ${items.map((t) => html`<div class="toast ${t.kind}" key=${t.id}>${t.message}</div>`)}
  </div>`;
}

const fmtDate = (v) => {
  if (!v) return '—';
  const d = new Date(v);
  if (isNaN(d)) return String(v);
  return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
};
const cell = (v) => {
  if (v === null || v === undefined || v === '') return html`<span class="muted">—</span>`;
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'object') return html`<code>${JSON.stringify(v)}</code>`;
  return String(v);
};

const DATA_TYPES = ['string', 'text', 'integer', 'bigint', 'decimal', 'float', 'boolean',
  'date', 'timestamp', 'uuid', 'json', 'email', 'url', 'enum', 'reference'];
const ENTITY_KINDS = ['master', 'reference', 'association'];
const NOTIF_EVENTS = ['submitted', 'changes_requested', 'rejected', 'approved', 'terminated'];

/* Permission helpers — the frontend gates on the *global* permission set from
 * /auth/me. Domain-conferred permissions may grant more than this shows; the
 * backend is always the authoritative gate and returns a clean 403 which api()
 * surfaces, so a hidden button never means a broken flow. */
const can = (me, perm) => (me?.permissions || []).includes(perm);
const canEditStaging = (me) => me?.is_admin || can(me, 'staging:edit');
const canApprove = (me) => me?.can_approve || can(me, 'staging:approve');
const canReject = (me) => me?.is_admin || can(me, 'staging:reject');

/* Human-friendly seconds → "3d 4h" / "12m". */
const fmtAge = (s) => {
  if (s == null) return '—';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
};

/* Debounce a changing value (used by the reference autocomplete). */
function useDebounced(value, ms) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/* ------------------------------------------------------- reference autocomplete
 * A searchable dropdown backed by GET /data/{refEntity}/options?q=. Shows the
 * human label, submits the selected mdm_id. Resolves an existing value's label
 * on mount so editing shows a recognisable value instead of a raw uuid.
 */
function RefSelect({ refEntity, value, label, onChange, placeholder }) {
  const [q, setQ] = useState('');
  const [open, setOpen] = useState(false);
  const [opts, setOpts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [curLabel, setCurLabel] = useState(label || '');
  const dq = useDebounced(q, 250);

  useEffect(() => { if (label) setCurLabel(label); }, [label]);

  // Resolve the label for a pre-existing value (edit case).
  useEffect(() => {
    if (!value || curLabel || !refEntity) return;
    let alive = true;
    api(`/data/${refEntity}/options?limit=100`)
      .then((r) => { if (alive) { const m = r.find((o) => o.mdm_id === value); if (m) setCurLabel(m.label); } })
      .catch(() => {});
    return () => { alive = false; };
  }, [value, refEntity]);

  useEffect(() => {
    if (!open || !refEntity) return;
    let alive = true;
    setLoading(true);
    api(`/data/${refEntity}/options?q=${encodeURIComponent(dq)}&limit=20`)
      .then((r) => { if (alive) setOpts(r); })
      .catch(() => { if (alive) setOpts([]); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [open, dq, refEntity]);

  if (!refEntity) return html`<input type="text" disabled placeholder="no ref_entity set" />`;
  const shown = open ? q : (curLabel || (value ? `${String(value).slice(0, 8)}…` : ''));
  return html`
    <div class="ref-select">
      <input type="text" value=${shown}
        placeholder=${placeholder || `Search ${refEntity}…`}
        onFocus=${() => { setOpen(true); setQ(''); }}
        onInput=${(e) => setQ(e.target.value)}
        onBlur=${() => setTimeout(() => setOpen(false), 180)} />
      ${value ? html`<button type="button" class="ref-clear"
        onMouseDown=${() => { onChange(null); setCurLabel(''); }} title="Clear">×</button>` : null}
      ${open ? html`<div class="ref-menu">
        ${loading ? html`<div class="ref-opt muted">Searching…</div>`
        : opts.length === 0 ? html`<div class="ref-opt muted">No matches</div>`
        : opts.map((o) => html`<div class="ref-opt" key=${o.mdm_id}
            onMouseDown=${() => { onChange(o.mdm_id); setCurLabel(o.label); setOpen(false); }}>
            <span>${o.label}</span><span class="muted mono small">${String(o.mdm_id).slice(0, 8)}…</span>
          </div>`)}
      </div>` : null}
    </div>`;
}

/* ------------------------------------------------------- direct record editor
 * Power-user (can_direct_edit) create / edit of a golden record. Writes go
 * through the normal API with ?direct=true (the W2 direct path), which still
 * lands + stages the row before applying it to live.
 */
function RecordForm({ model, record, onClose, onSaved }) {
  const editing = !!record;
  const attrs = model.attributes || [];
  const [vals, setVals] = useState(() => {
    const init = {};
    for (const a of attrs) init[a.name] = record ? (record[a.name] ?? '') : '';
    return init;
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const setV = (n, v) => setVals((s) => ({ ...s, [n]: v }));

  const save = async () => {
    setBusy(true); setErr(null);
    const payload = {};
    for (const a of attrs) {
      const v = vals[a.name];
      if (v === '' || v === null || v === undefined) continue;
      payload[a.name] = v;
    }
    try {
      if (editing) {
        await api(`/data/${model.name}/${record.mdm_id}?direct=true`,
          { method: 'PATCH', body: JSON.stringify(payload) });
        notify('Record updated — direct edit applied to the golden record.');
      } else {
        const r = await api(`/data/${model.name}?direct=true`,
          { method: 'POST', body: JSON.stringify(payload) });
        notify(r.applied ? 'Record created and applied to live.'
          : 'Captured, but held for review — check the stewardship queue.',
          r.applied ? 'ok' : 'err');
      }
      onSaved();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const fieldInput = (a) => {
    if (a.data_type === 'reference') {
      return html`<${RefSelect} refEntity=${a.ref_entity} value=${vals[a.name] || null}
        onChange=${(id) => setV(a.name, id)} />`;
    }
    if (a.data_type === 'boolean') {
      return html`<select value=${vals[a.name] === '' ? '' : String(vals[a.name])}
        onChange=${(e) => setV(a.name, e.target.value)}>
        <option value="">—</option><option value="true">true</option><option value="false">false</option>
      </select>`;
    }
    if (a.data_type === 'enum' && a.validation?.enum?.length) {
      return html`<select value=${vals[a.name] ?? ''} onChange=${(e) => setV(a.name, e.target.value)}>
        <option value="">—</option>
        ${a.validation.enum.map((o) => html`<option key=${o} value=${o}>${o}</option>`)}
      </select>`;
    }
    return html`<input type="text" class=${a.data_type === 'uuid' ? 'mono' : ''}
      value=${vals[a.name] ?? ''} onInput=${(e) => setV(a.name, e.target.value)} />`;
  };

  return html`
    <${Modal} wide title=${editing ? `Edit record — ${model.display_name || model.name}` : `New ${model.display_name || model.name} (direct)`}
      onClose=${onClose}
      footer=${html`<${Fragment}>
        <button class="btn" onClick=${onClose}>Cancel</button>
        <button class="btn btn-primary" disabled=${busy} onClick=${save}>
          ${busy ? html`<span class="spinner"></span>` : null} ${editing ? 'Apply direct edit' : 'Create record'}
        </button>
      <//>`}>
      ${err && html`<${Banner} kind="err" title="Could not save">${err}<//>`}
      <${Banner} kind="warn" title="Direct edit — bypasses review">
        This applies straight to the golden record (power-user auto-approve). The
        change is still captured in landing and staging for the audit trail.
      <//>
      <div class="grid grid-2">
        ${attrs.map((a) => html`
          <label class="field" key=${a.name}>
            <span>${a.display_name || a.name}${a.is_required ? ' *' : ''}
              <span class="hint">${a.data_type}${a.is_business_key ? ' · bkey' : ''}</span></span>
            ${fieldInput(a)}
          </label>`)}
      </div>
    <//>`;
}

/* ============================================================ login */
function Login({ onSignedIn }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      const r = await api('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) });
      onSignedIn(r);
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };

  return html`
    <div class="login-wrap">
      <div class="login-brand">
        <div>
          <div class="tag">Master Data Management</div>
          <h1>One governed source of truth.</h1>
          <p>
            Inbound changes land, get validated, and wait for a human steward to
            review them. Nothing reaches your golden records unapproved.
          </p>
          <div class="flow">
            <span class="flow-node">API write</span><span class="flow-arrow">→</span>
            <span class="flow-node">landing</span><span class="flow-arrow">→</span>
            <span class="flow-node">staging</span><span class="flow-arrow">→</span>
            <span class="flow-node">review</span><span class="flow-arrow">→</span>
            <span class="flow-node">live</span>
          </div>
        </div>
        <div class="small" style=${sx('opacity:.55')}>Authenticated against Active Directory / LDAP</div>
      </div>
      <div class="login-panel">
        <h2>Sign in</h2>
        <div class="sub">Use your directory credentials.</div>
        ${error && html`<${Banner} kind="err">${error}<//>`}
        <form onSubmit=${submit}>
          <label class="field">
            <span>Username</span>
            <input type="text" value=${username} autoFocus autoComplete="username"
              onInput=${(e) => setUsername(e.target.value)} placeholder="jdoe" />
          </label>
          <label class="field">
            <span>Password</span>
            <input type="password" value=${password} autoComplete="current-password"
              onInput=${(e) => setPassword(e.target.value)} />
          </label>
          <button class="btn btn-primary" style=${sx('width:100%;justify-content:center')} disabled=${busy || !username || !password}>
            ${busy ? html`<span class="spinner"></span>` : null} ${busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>`;
}

/* ============================================================ dashboard */
function Dashboard({ me, go }) {
  const [queue, setQueue] = useState(null);
  const [models, setModels] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const [q, m] = await Promise.all([
          api('/stewardship/queue').catch(() => ({ queues: [], total_pending: 0 })),
          api('/models'),
        ]);
        setQueue(q); setModels(m);
      } catch (e) { setErr(e.message); }
    })();
  }, []);

  if (err) return html`<${Banner} kind="err" title="Could not load dashboard">${err}<//>`;
  if (!queue || !models) return html`<${Spinner} />`;

  const published = models.filter((m) => m.status === 'published' || m.status === 'modified');
  const drafts = models.filter((m) => m.status === 'draft');
  const totalInvalid = queue.queues.reduce((a, q) => a + (q.invalid || 0), 0);

  return html`
    <div>
      <div class="grid grid-4" style=${sx('margin-bottom:22px')}>
        <div class="stat"><div class="k">Entities</div><div class="v">${models.length}</div>
          <div class="sub">${published.length} published · ${drafts.length} draft</div></div>
        <div class="stat ${queue.total_pending ? 'alert' : ''}">
          <div class="k">Awaiting review</div><div class="v">${queue.total_pending}</div>
          <div class="sub">staged records pending a steward</div></div>
        <div class="stat ${totalInvalid ? 'alert' : ''}">
          <div class="k">Invalid staged</div><div class="v">${totalInvalid}</div>
          <div class="sub">need correction before approval</div></div>
        <div class="stat"><div class="k">Your access</div>
          <div class="v" style=${sx('font-size:16px;padding-top:7px')}>${(me.roles || []).join(', ') || 'none'}</div>
          <div class="sub">${me.source} account</div></div>
      </div>

      ${queue.total_pending > 0 && me.is_steward ? html`
        <${Banner} kind="warn" title="You have data waiting for review">
          ${queue.total_pending} record${queue.total_pending === 1 ? '' : 's'} across
          ${queue.queues.length} entit${queue.queues.length === 1 ? 'y' : 'ies'} need a steward decision.
        <//>` : null}

      <div class="grid grid-2">
        <div class="card">
          <div class="card-head">
            <div><h3>Review queues</h3><div class="desc">Staged changes by entity</div></div>
            ${me.is_steward && html`<button class="btn btn-sm" onClick=${() => go('review')}>Open queue</button>`}
          </div>
          <div class="card-body flush">
            ${queue.queues.length === 0 ? html`
              <${Empty} title="Nothing pending">All staged records have been reviewed.<//>`
            : html`<table>
                <thead><tr><th>Entity</th><th class="right">Pending</th><th class="right">Invalid</th></tr></thead>
                <tbody>${queue.queues.map((q) => html`
                  <tr class="clickable" key=${q.entity} onClick=${() => go('review', { entity: q.entity })}>
                    <td><strong>${q.display_name || q.entity}</strong><div class="small muted mono">${q.entity}</div></td>
                    <td class="num">${q.pending_review}</td>
                    <td class="num">${q.invalid > 0 ? html`<${Pill} kind="err">${q.invalid}<//>` : '0'}</td>
                  </tr>`)}
                </tbody>
              </table>`}
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <div><h3>Data models</h3><div class="desc">Configured entities and deployment state</div></div>
            <button class="btn btn-sm" onClick=${() => go('models')}>Manage</button>
          </div>
          <div class="card-body flush">
            ${models.length === 0 ? html`
              <${Empty} title="No entities yet">
                ${me.is_admin ? 'Define your first data model to get started.' : 'An administrator needs to define a data model.'}
              <//>`
            : html`<table>
                <thead><tr><th>Entity</th><th>Domain</th><th>Attrs</th><th>Status</th></tr></thead>
                <tbody>${models.slice(0, 8).map((m) => html`
                  <tr class="clickable" key=${m.name} onClick=${() => go('models', { entity: m.name })}>
                    <td><strong>${m.display_name || m.name}</strong><div class="small muted mono">${m.name}</div></td>
                    <td class="small">${m.domain || html`<span class="muted">—</span>`}</td>
                    <td class="num">${m.attribute_count}</td>
                    <td>${statusPill(m.status)}</td>
                  </tr>`)}
                </tbody>
              </table>`}
          </div>
        </div>
      </div>
    </div>`;
}

/* ============================================================ model designer */
function AttributeEditor({ attrs, setAttrs, entities }) {
  const [expanded, setExpanded] = useState({});
  const update = (i, patch) => setAttrs(attrs.map((a, idx) => (idx === i ? { ...a, ...patch } : a)));
  const remove = (i) => setAttrs(attrs.filter((_, idx) => idx !== i));
  const toggle = (i) => setExpanded((s) => ({ ...s, [i]: !s[i] }));
  const add = () => setAttrs([...attrs, {
    name: '', data_type: 'string', length: 255, is_required: false, is_unique: false,
    is_business_key: false, is_match_key: false, is_indexed: false, validation: {},
    normalization: [], transforms: [], ref_entity: null, ref_attribute: null,
  }]);

  const entityNames = (entities || []).map((e) => e.name);

  return html`
    <div>
      <div class="attr-editor">
        <div class="attr-row attr-head">
          <div>Column name</div><div>Type</div><div>Len</div><div>Flags</div><div></div>
        </div>
        ${attrs.map((a, i) => html`<${Fragment} key=${i}>
          <div class="attr-row">
            <input type="text" class="mono" value=${a.name} placeholder="column_name"
              onInput=${(e) => update(i, { name: e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_') })} />
            <select value=${a.data_type} onChange=${(e) => {
              const dt = e.target.value;
              // Auto-expand config for types that need it.
              if (dt === 'reference' || dt === 'enum') setExpanded((s) => ({ ...s, [i]: true }));
              update(i, { data_type: dt });
            }}>
              ${DATA_TYPES.map((t) => html`<option value=${t} key=${t}>${t}</option>`)}
            </select>
            <input type="number" value=${a.length ?? ''} placeholder="—"
              disabled=${!['string', 'email', 'enum'].includes(a.data_type)}
              onInput=${(e) => update(i, { length: e.target.value ? +e.target.value : null })} />
            <div class="attr-flags">
              <label title="NOT NULL on the golden table">
                <input type="checkbox" checked=${a.is_required} onChange=${(e) => update(i, { is_required: e.target.checked })} />req</label>
              <label title="Unique index">
                <input type="checkbox" checked=${a.is_unique} onChange=${(e) => update(i, { is_unique: e.target.checked })} />uniq</label>
              <label title="Business key — used to resolve updates">
                <input type="checkbox" checked=${a.is_business_key} onChange=${(e) => update(i, { is_business_key: e.target.checked })} />bkey</label>
              <label title="Match key — used for duplicate detection">
                <input type="checkbox" checked=${a.is_match_key} onChange=${(e) => update(i, { is_match_key: e.target.checked })} />match</label>
              <label title="Create an index">
                <input type="checkbox" checked=${a.is_indexed} onChange=${(e) => update(i, { is_indexed: e.target.checked })} />idx</label>
              <label title="Contains personal data">
                <input type="checkbox" checked=${a.is_pii} onChange=${(e) => update(i, { is_pii: e.target.checked })} />pii</label>
            </div>
            <div class="btn-row" style=${sx('justify-content:flex-end;gap:4px')}>
              <button class="btn btn-sm ${expanded[i] ? 'btn-primary' : ''}" onClick=${() => toggle(i)}
                title="Advanced: reference, enum values, transforms">⚙</button>
              <button class="btn btn-sm btn-danger" onClick=${() => remove(i)} title="Remove attribute">×</button>
            </div>
          </div>
          ${expanded[i] ? html`<div class="attr-advanced">
            ${a.data_type === 'reference' ? html`<div class="adv-grid">
              <label class="field"><span>Reference entity <span class="hint">— parent this FK points at</span></span>
                <select value=${a.ref_entity || ''} onChange=${(e) => update(i, { ref_entity: e.target.value || null })}>
                  <option value="">— select entity —</option>
                  ${entityNames.map((n) => html`<option key=${n} value=${n}>${n}</option>`)}
                </select></label>
              <label class="field"><span>Reference attribute <span class="hint">— optional; defaults to business key</span></span>
                <input type="text" class="mono" value=${a.ref_attribute || ''}
                  onInput=${(e) => update(i, { ref_attribute: e.target.value || null })} placeholder="code" /></label>
            </div>` : null}
            ${a.data_type === 'enum' ? html`<label class="field">
              <span>Allowed values <span class="hint">— comma-separated; stored as validation.enum</span></span>
              <input type="text" value=${(a.validation?.enum || []).join(', ')}
                onInput=${(e) => update(i, { validation: { ...(a.validation || {}),
                  enum: e.target.value.split(',').map((x) => x.trim()).filter(Boolean) } })}
                placeholder="alpha, beta, gamma" /></label>` : null}
            <div class="adv-grid">
              <label class="field"><span>Normalisation <span class="hint">— trim, lower, upper, …</span></span>
                <input type="text" class="mono" value=${(a.normalization || []).join(', ')}
                  onInput=${(e) => update(i, { normalization: e.target.value.split(',').map((x) => x.trim()).filter(Boolean) })}
                  placeholder="trim, upper" /></label>
              <label class="field"><span>Transforms <span class="hint">— custom fn names, applied in order</span></span>
                <input type="text" class="mono" value=${(a.transforms || []).map((t) => (typeof t === 'string' ? t : (t.fn || JSON.stringify(t)))).join(', ')}
                  onInput=${(e) => update(i, { transforms: e.target.value.split(',').map((x) => x.trim()).filter(Boolean) })}
                  placeholder="titlecase, phone_e164" /></label>
            </div>
            <label class="field"><span>Default value</span>
              <input type="text" value=${a.default_value || ''}
                onInput=${(e) => update(i, { default_value: e.target.value || null })} /></label>
          </div>` : null}
        <//>`)}
      </div>
      <div class="btn-row" style=${sx('margin-top:11px')}>
        <button class="btn btn-sm" onClick=${add}>+ Add attribute</button>
        <span class="small muted">
          Business key resolves updates. Match key drives duplicate detection. ⚙ configures references, enum values and transforms.
        </span>
      </div>
    </div>`;
}

function EntityForm({ initial, onSaved, onCancel }) {
  const editing = !!initial;
  const [name, setName] = useState(initial?.name || '');
  const [displayName, setDisplayName] = useState(initial?.display_name || '');
  const [domain, setDomain] = useState(initial?.domain || '');
  const [description, setDescription] = useState(initial?.description || '');
  const [requiresApproval, setRequiresApproval] = useState(initial?.requires_approval ?? true);
  const [softDelete, setSoftDelete] = useState(initial?.soft_delete ?? true);
  const [kind, setKind] = useState(initial?.kind || 'master');
  const [entities, setEntities] = useState([]);
  useEffect(() => { api('/models').then(setEntities).catch(() => setEntities([])); }, []);
  const [attrs, setAttrs] = useState(initial?.attributes?.length ? initial.attributes.map((a) => ({ ...a })) : [
    { name: '', data_type: 'string', length: 100, is_required: true, is_unique: true, is_business_key: true, is_match_key: false, is_indexed: false, validation: {}, normalization: ['trim'] },
  ]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const save = async () => {
    setBusy(true); setErr(null);
    const body = {
      name, display_name: displayName || null, domain: domain || null,
      description: description || null, requires_approval: requiresApproval,
      soft_delete: softDelete, kind,
      attributes: attrs.filter((a) => a.name).map((a, i) => ({ ...a, position: i })),
    };
    try {
      const saved = editing
        ? await api(`/models/${name}`, { method: 'PUT', body: JSON.stringify(body) })
        : await api('/models', { method: 'POST', body: JSON.stringify(body) });
      notify(`Entity “${saved.name}” ${editing ? 'updated' : 'created'}. Publish it to apply the DDL.`);
      onSaved(saved);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const valid = name && attrs.some((a) => a.name);
  return html`
    <${Modal} wide title=${editing ? `Edit model — ${name}` : 'New data model'} onClose=${onCancel}
      footer=${html`<${Fragment}>
        <button class="btn" onClick=${onCancel}>Cancel</button>
        <button class="btn btn-primary" disabled=${busy || !valid} onClick=${save}>
          ${busy ? html`<span class="spinner"></span>` : null} ${editing ? 'Save changes' : 'Create model'}
        </button>
      <//>`}>
      ${err && html`<${Banner} kind="err" title="Could not save">${err}<//>`}
      ${editing && initial.status !== 'draft' ? html`
        <${Banner} kind="warn" title="This model is already deployed">
          Saving changes here updates the definition only. You must publish afterwards to
          alter the physical tables — and column removals require explicit confirmation.
        <//>` : null}
      <div class="grid grid-2">
        <label class="field"><span>Entity name <span class="hint">— physical table name, lower snake_case</span></span>
          <input type="text" class="mono" value=${name} disabled=${editing}
            onInput=${(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_'))} placeholder="customer" /></label>
        <label class="field"><span>Display name</span>
          <input type="text" value=${displayName} onInput=${(e) => setDisplayName(e.target.value)} placeholder="Customer Master" /></label>
        <label class="field"><span>Domain <span class="hint">— grouping, e.g. party, catalog</span></span>
          <input type="text" value=${domain} onInput=${(e) => setDomain(e.target.value)} placeholder="party" /></label>
        <label class="field"><span>Kind <span class="hint">— master, reference lookup, or association</span></span>
          <select value=${kind} onChange=${(e) => setKind(e.target.value)}>
            ${ENTITY_KINDS.map((k) => html`<option key=${k} value=${k}>${k}</option>`)}
          </select></label>
        <label class="field"><span>Description</span>
          <input type="text" value=${description} onInput=${(e) => setDescription(e.target.value)} /></label>
      </div>
      <div class="btn-row" style=${sx('margin-bottom:16px')}>
        <label class="check"><input type="checkbox" checked=${requiresApproval}
          onChange=${(e) => setRequiresApproval(e.target.checked)} /> Require steward approval</label>
        <label class="check"><input type="checkbox" checked=${softDelete}
          onChange=${(e) => setSoftDelete(e.target.checked)} /> Soft delete (retain tombstones)</label>
      </div>
      <div class="sep"></div>
      <h3 style=${sx('font-size:13px;margin-bottom:10px')}>Attributes</h3>
      <${AttributeEditor} attrs=${attrs} setAttrs=${setAttrs} entities=${entities} />
    <//>`;
}

function PublishDialog({ entity, onClose, onPublished }) {
  const [plan, setPlan] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [confirmDestructive, setConfirmDestructive] = useState(false);

  useEffect(() => {
    api(`/models/${entity.name}/ddl`).then(setPlan).catch((e) => setErr(e.message));
  }, [entity.name]);

  const publish = async () => {
    setBusy(true); setErr(null);
    try {
      const r = await api(`/models/${entity.name}/publish`, {
        method: 'POST',
        body: JSON.stringify({ confirm_destructive: confirmDestructive, dry_run: false }),
      });
      notify(`Published ${entity.name} — ${r.statements_executed} statement(s) executed across 4 tiers.`);
      onPublished(r);
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const destructive = plan?.destructive || [];
  return html`
    <${Modal} wide title=${`Publish “${entity.name}” — DDL preview`} onClose=${onClose}
      footer=${html`<${Fragment}>
        <button class="btn" onClick=${onClose}>Cancel</button>
        <button class="btn btn-primary" disabled=${busy || !plan || plan.statement_count === 0 && !destructive.length
          || (destructive.length > 0 && !confirmDestructive)} onClick=${publish}>
          ${busy ? html`<span class="spinner"></span>` : null} Apply to database
        </button>
      <//>`}>
      ${err && html`<${Banner} kind="err" title="Publish failed">${err}<//>`}
      ${!plan ? html`<${Spinner} label="Computing migration…" />` : html`<${Fragment}>
        <${Banner} kind="info" title=${plan.mode === 'create' ? 'Creating four tiers' : 'Altering deployed tables'}>
          ${plan.mode === 'create'
            ? html`This will create <code>mdm_landing.${entity.name}</code>, <code>mdm_staging.${entity.name}</code>,
                   <code>mdm.${entity.name}</code> and <code>mdm_history.${entity.name}</code>.`
            : html`${plan.statement_count} additive statement(s) will be applied to the existing tables.`}
        <//>
        ${plan.warnings?.length ? html`
          <${Banner} kind="warn" title="Warnings">
            <ul>${plan.warnings.map((w, i) => html`<li key=${i}>${w}</li>`)}</ul>
          <//>` : null}
        ${destructive.length ? html`
          <${Banner} kind="err" title="Destructive changes detected">
            These operations can permanently destroy data and are blocked unless you confirm:
            <ul>${destructive.map((d, i) => html`<li key=${i}><code>${d}</code></li>`)}</ul>
            <label class="check" style=${sx('margin-top:9px')}>
              <input type="checkbox" checked=${confirmDestructive}
                onChange=${(e) => setConfirmDestructive(e.target.checked)} />
              I understand this may destroy data — apply anyway
            </label>
          <//>` : null}
        ${plan.statement_count === 0 && !destructive.length ? html`
          <${Banner} kind="ok" title="Already in sync">No schema changes are required.<//>` : null}
        ${plan.sql ? html`<pre class="sql">${plan.sql}</pre>` : null}
      <//>`}
    <//>`;
}

function ImportDialog({ onClose, onImported }) {
  const [file, setFile] = useState(null);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [replace, setReplace] = useState(false);

  const run = async (dryRun) => {
    if (!file) return;
    setBusy(true); setErr(null);
    const fd = new FormData();
    fd.append('file', file);
    try {
      const r = await api(`/models/import?dry_run=${dryRun}&replace=${replace}`, { method: 'POST', body: fd });
      if (dryRun) setPreview(r);
      else { notify(`Imported ${r.results.length} entit${r.results.length === 1 ? 'y' : 'ies'}. Publish to apply the DDL.`); onImported(r); }
    } catch (e) {
      setErr(e.detail?.errors ? e.detail.errors.join(' · ') : e.message);
    } finally { setBusy(false); }
  };

  return html`
    <${Modal} wide title="Import data models" onClose=${onClose}
      footer=${html`<${Fragment}>
        <button class="btn" onClick=${onClose}>Cancel</button>
        <button class="btn" disabled=${!file || busy} onClick=${() => run(true)}>Preview diff</button>
        <button class="btn btn-primary" disabled=${!preview || busy} onClick=${() => run(false)}>Apply import</button>
      <//>`}>
      ${err && html`<${Banner} kind="err" title="Import problem">${err}<//>`}
      <${Banner} kind="info">
        Import accepts JSON or YAML exported from this application. Importing changes
        definitions only — the physical tables are untouched until you publish.
      <//>
      <label class="field"><span>Model file (.json / .yaml)</span>
        <input type="file" accept=".json,.yaml,.yml" onChange=${(e) => { setFile(e.target.files[0]); setPreview(null); }} /></label>
      <label class="check" style=${sx('margin-bottom:14px')}>
        <input type="checkbox" checked=${replace} onChange=${(e) => setReplace(e.target.checked)} />
        Remove attributes absent from the file (otherwise they are kept)
      </label>
      ${preview && html`<${Fragment}>
        <div class="sep"></div>
        <h3 style=${sx('font-size:13px;margin-bottom:9px')}>Planned changes</h3>
        ${preview.warnings?.length ? html`<${Banner} kind="warn">
          <ul>${preview.warnings.map((w, i) => html`<li key=${i}>${w}</li>`)}</ul><//>` : null}
        <table>
          <thead><tr><th>Entity</th><th>Action</th><th>Added</th><th>Removed</th><th>Changed</th></tr></thead>
          <tbody>${preview.diff.map((d) => html`
            <tr key=${d.entity}>
              <td class="mono">${d.entity}</td>
              <td>${statusPill(d.action === 'create' ? 'draft' : d.action === 'no_change' ? 'approved' : 'modified')} ${d.action}</td>
              <td class="small">${d.attributes_added.join(', ') || '—'}</td>
              <td class="small">${d.attributes_removed.length
                ? html`<${Pill} kind="err">${d.attributes_removed.join(', ')}<//>` : '—'}</td>
              <td class="small">${d.attributes_changed.map((c) => c.attribute).join(', ') || '—'}</td>
            </tr>`)}
          </tbody>
        </table>
      <//>`}
    <//>`;
}

function ModelsView({ me, params, go }) {
  const [models, setModels] = useState(null);
  const [err, setErr] = useState(null);
  const [editing, setEditing] = useState(null);
  const [creating, setCreating] = useState(false);
  const [publishing, setPublishing] = useState(null);
  const [importing, setImporting] = useState(false);
  const [detail, setDetail] = useState(null);

  const load = useCallback(async () => {
    try { setModels(await api('/models')); } catch (e) { setErr(e.message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (params?.entity) api(`/models/${params.entity}`).then(setDetail).catch(() => {});
  }, [params?.entity]);

  const openDetail = async (name) => {
    try { setDetail(await api(`/models/${name}`)); } catch (e) { notify(e.message, 'err'); }
  };

  if (err) return html`<${Banner} kind="err" title="Could not load models">${err}<//>`;
  if (!models) return html`<${Spinner} />`;

  return html`
    <div>
      <div class="btn-row" style=${sx('margin-bottom:18px')}>
        ${me.is_admin && html`<button class="btn btn-primary" onClick=${() => setCreating(true)}>+ New model</button>`}
        ${me.is_admin && html`<button class="btn" onClick=${() => setImporting(true)}>Import file</button>`}
        <a class="btn" href="${API}/models/export/all?fmt=yaml" download>Export YAML</a>
        <a class="btn" href="${API}/models/export/all?fmt=json" download>Export JSON</a>
      </div>

      <div class="card">
        <div class="card-head">
          <div><h3>Data models</h3>
            <div class="desc">Each published entity materialises four physical tables</div></div>
        </div>
        <div class="card-body flush">
          ${models.length === 0 ? html`<${Empty} title="No entities defined">
            ${me.is_admin ? 'Create a model or import a definition file.' : 'An administrator must define a model first.'}<//>`
          : html`<div class="table-scroll"><table>
              <thead><tr><th>Entity</th><th>Domain</th><th class="right">Attrs</th><th>Status</th><th>Version</th><th>Published</th><th></th></tr></thead>
              <tbody>${models.map((m) => html`
                <tr key=${m.name}>
                  <td class="clickable" onClick=${() => openDetail(m.name)}>
                    <strong>${m.display_name || m.name}</strong>
                    <div class="small muted mono">${m.name}</div></td>
                  <td class="small">${m.domain || html`<span class="muted">—</span>`}</td>
                  <td class="num">${m.attribute_count}</td>
                  <td>${statusPill(m.status)}</td>
                  <td class="num">v${m.version}</td>
                  <td class="small muted">${m.status === 'draft' ? 'not deployed' : 'deployed'}</td>
                  <td class="right nowrap">
                    <div class="btn-row" style=${sx('justify-content:flex-end')}>
                      <button class="btn btn-sm" onClick=${() => openDetail(m.name)}>View</button>
                      ${me.is_admin && html`
                        <button class="btn btn-sm" onClick=${async () => {
                          const full = await api(`/models/${m.name}`); setEditing(full);
                        }}>Edit</button>`}
                      ${me.is_admin && html`
                        <button class="btn btn-sm btn-primary" onClick=${() => setPublishing(m)}>
                          ${m.status === 'draft' ? 'Publish' : 'Sync DDL'}
                        </button>`}
                    </div>
                  </td>
                </tr>`)}
              </tbody></table></div>`}
        </div>
      </div>

      ${creating && html`<${EntityForm} onCancel=${() => setCreating(false)}
        onSaved=${() => { setCreating(false); load(); }} />`}
      ${editing && html`<${EntityForm} initial=${editing} onCancel=${() => setEditing(null)}
        onSaved=${() => { setEditing(null); load(); }} />`}
      ${publishing && html`<${PublishDialog} entity=${publishing} onClose=${() => setPublishing(null)}
        onPublished=${() => { setPublishing(null); load(); }} />`}
      ${importing && html`<${ImportDialog} onClose=${() => setImporting(false)}
        onImported=${() => { setImporting(false); load(); }} />`}
      ${detail && html`<${ModelDetail} entity=${detail} onClose=${() => setDetail(null)} go=${go} />`}
    </div>`;
}

function ModelDetail({ entity, onClose, go }) {
  const [stats, setStats] = useState(null);
  useEffect(() => {
    if (entity.status !== 'draft') {
      api(`/data/${entity.name}/statistics`).then((r) => setStats(r.statistics)).catch(() => {});
    }
  }, [entity.name, entity.status]);

  return html`
    <${Modal} wide title=${`${entity.display_name || entity.name}`} onClose=${onClose}
      footer=${html`<${Fragment}>
        <a class="btn" href="${API}/models/${entity.name}/export?fmt=yaml" download>Export YAML</a>
        <button class="btn" onClick=${onClose}>Close</button>
      <//>`}>
      <dl class="kv" style=${sx('margin-bottom:18px')}>
        <dt>Entity name</dt><dd>${entity.name}</dd>
        <dt>Status</dt><dd>${statusPill(entity.status)} version ${entity.version}</dd>
        <dt>Domain</dt><dd>${entity.domain || '—'}</dd>
        <dt>Approval required</dt><dd>${entity.requires_approval ? 'yes' : 'no'}</dd>
        <dt>Delete behaviour</dt><dd>${entity.soft_delete ? 'soft delete' : 'hard delete'}</dd>
        ${entity.description ? html`<${Fragment}><dt>Description</dt><dd style=${sx('font-family:var(--sans)')}>${entity.description}</dd><//>` : null}
      </dl>

      ${stats && html`<${Fragment}>
        <h3 style=${sx('font-size:13px;margin-bottom:9px')}>Pipeline</h3>
        <div class="flow-diagram" style=${sx('margin-bottom:18px')}>
          <div class="flow-stage ${stats.landing_pending ? 'hot' : ''}">
            <div class="fs-name">Landing</div><div class="fs-count">${stats.landing_pending}</div>
            <div class="fs-schema">mdm_landing · ${stats.landing_total} total</div></div>
          <div class="flow-stage ${(stats.staging_by_status?.pending_review || 0) ? 'hot' : ''}">
            <div class="fs-name">Staging</div>
            <div class="fs-count">${stats.staging_by_status?.pending_review || 0}</div>
            <div class="fs-schema">mdm_staging · ${stats.staging_invalid} invalid</div></div>
          <div class="flow-stage">
            <div class="fs-name">Live</div><div class="fs-count">${stats.live_active}</div>
            <div class="fs-schema">mdm · ${stats.live_deleted} deleted</div></div>
          <div class="flow-stage">
            <div class="fs-name">History</div><div class="fs-count">${stats.history_versions}</div>
            <div class="fs-schema">mdm_history</div></div>
        </div>
      <//>`}

      <h3 style=${sx('font-size:13px;margin-bottom:9px')}>Attributes</h3>
      <div class="table-scroll"><table>
        <thead><tr><th>Column</th><th>Type</th><th>Constraints</th><th>Keys</th><th>Normalisation</th></tr></thead>
        <tbody>${entity.attributes.map((a) => html`
          <tr key=${a.name}>
            <td class="mono">${a.name}${a.is_pii ? html` <${Pill} kind="purple">pii<//>` : null}</td>
            <td class="mono small">${a.data_type}${a.length ? `(${a.length})` : ''}${
              a.numeric_precision ? `(${a.numeric_precision},${a.numeric_scale || 0})` : ''}</td>
            <td class="small">
              ${a.is_required ? html`<${Pill} kind="info">required<//> ` : null}
              ${a.is_unique ? html`<${Pill} kind="info">unique<//> ` : null}
              ${a.validation && Object.keys(a.validation).length
                ? html`<code class="small">${JSON.stringify(a.validation)}</code>` : null}
            </td>
            <td class="small">
              ${a.is_business_key ? html`<${Pill} kind="ok">business<//> ` : null}
              ${a.is_match_key ? html`<${Pill} kind="purple">match<//>` : null}
            </td>
            <td class="small mono">${(a.normalization || []).join(', ') || '—'}</td>
          </tr>`)}
        </tbody></table></div>
    <//>`;
}

/* ============================================================ review queue */
function ReviewDetail({ entityName, stagingId, onClose, onActioned, me }) {
  const [d, setD] = useState(null);
  const [model, setModel] = useState(null);
  const [wf, setWf] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [edits, setEdits] = useState({});
  const [decision, setDecision] = useState(null); // 'approve' | 'reject' | 'changes'
  const [comment, setComment] = useState('');
  const [showHistory, setShowHistory] = useState(false);

  const load = useCallback(async () => {
    try { setD(await api(`/stewardship/${entityName}/staging/${stagingId}`)); }
    catch (e) { setErr(e.message); }
    api(`/stewardship/${entityName}/staging/${stagingId}/workflow`)
      .then(setWf).catch(() => setWf(null));
  }, [entityName, stagingId]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { api(`/models/${entityName}`).then(setModel).catch(() => {}); }, [entityName]);

  const attrByName = useMemo(() => {
    const m = {};
    (model?.attributes || []).forEach((a) => { m[a.name] = a; });
    return m;
  }, [model]);

  const saveEdits = async () => {
    setBusy(true); setErr(null);
    try {
      const r = await api(`/stewardship/${entityName}/staging/${stagingId}`, {
        method: 'PATCH', body: JSON.stringify({ updates: edits }),
      });
      notify(r.is_valid ? 'Record updated — validation now passes.' : `Saved, but ${r.errors.length} issue(s) remain.`,
        r.is_valid ? 'ok' : 'err');
      setEdits({}); await load(); onActioned();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const runDecision = async () => {
    setBusy(true); setErr(null);
    try {
      if (decision === 'approve') {
        const r = await api(`/stewardship/${entityName}/staging/${stagingId}/approve`, {
          method: 'POST', body: JSON.stringify({ note: comment || null }),
        });
        notify(`Approved — golden record ${r.change_type} (v${r.version || 1}).`);
      } else if (decision === 'reject') {
        await api(`/stewardship/${entityName}/staging/${stagingId}/reject`, {
          method: 'POST', body: JSON.stringify({ reason: comment }),
        });
        notify('Record rejected.', 'ok');
      } else if (decision === 'changes') {
        await api(`/stewardship/${entityName}/staging/${stagingId}/request-changes`, {
          method: 'POST', body: JSON.stringify({ comment }),
        });
        notify('Changes requested — sent back to the submitter.', 'ok');
      }
      onActioned();
      if (decision === 'changes') { setDecision(null); setComment(''); await load(); }
      else onClose();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const claim = async (action) => {
    setBusy(true); setErr(null);
    try {
      await api(`/stewardship/${entityName}/staging/${stagingId}/${action}`, { method: 'POST' });
      notify(action === 'claim' ? 'Claimed — assigned to you.' : 'Released.');
      await load();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const reresolve = async () => {
    setBusy(true); setErr(null);
    try {
      const r = await api(`/stewardship/${entityName}/reresolve`, { method: 'POST' });
      notify(`Re-resolution ran — ${r.unblocked ?? r.updated ?? 0} row(s) unblocked.`);
      await load(); onActioned();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  if (err && !d) return html`<${Modal} title="Review" onClose=${onClose}>
    <${Banner} kind="err">${err}<//><//>`;
  if (!d) return html`<${Modal} title="Review" onClose=${onClose}><${Spinner} /><//>`;

  const s = d.staging;
  const errors = s.mdm_errors || [];
  const supplied = s.mdm_supplied_fields || [];
  const attrNames = Object.keys(s).filter((k) => !k.startsWith('mdm_'));
  const editable = canEditStaging(me) && s.mdm_status !== 'applied' && s.mdm_status !== 'rejected';
  const terminal = s.mdm_status === 'applied' || s.mdm_status === 'rejected';
  const hasBrokenRef = errors.some((e) => e.code === 'broken_reference');
  const task = wf?.task;
  const claimed = task?.claimed_by;
  const commentRequired = decision === 'reject' || decision === 'changes'
    || (decision === 'approve');

  const fieldEditor = (f) => {
    const a = attrByName[f];
    const cur = edits[f] !== undefined ? edits[f] : (s[f] ?? '');
    if (a && a.data_type === 'reference') {
      return html`<${RefSelect} refEntity=${a.ref_entity} value=${cur || null}
        onChange=${(id) => setEdits({ ...edits, [f]: id })} />`;
    }
    if (a && a.data_type === 'enum' && a.validation?.enum?.length) {
      return html`<select value=${cur} onChange=${(e) => setEdits({ ...edits, [f]: e.target.value })}>
        <option value="">—</option>
        ${a.validation.enum.map((o) => html`<option key=${o} value=${o}>${o}</option>`)}
      </select>`;
    }
    return html`<input type="text" class="mono" value=${cur}
      onInput=${(e) => setEdits({ ...edits, [f]: e.target.value })} />`;
  };

  return html`
    <${Modal} wide title=${`Review — ${entityName} #${stagingId}`} onClose=${onClose}
      footer=${html`<${Fragment}>
        ${wf ? html`<button class="btn btn-sm" onClick=${() => setShowHistory(true)}>History</button>` : null}
        ${Object.keys(edits).length > 0 && html`
          <button class="btn btn-primary" disabled=${busy} onClick=${saveEdits}>
            ${busy ? html`<span class="spinner"></span>` : null} Save ${Object.keys(edits).length} edit(s)
          </button>`}
        <button class="btn" onClick=${onClose}>Close</button>
        ${!terminal && canReject(me) ? html`<${Fragment}>
          <button class="btn ${decision === 'changes' ? 'btn-primary' : ''}" disabled=${busy}
            onClick=${() => { setDecision(decision === 'changes' ? null : 'changes'); setComment(''); }}>Request changes</button>
          <button class="btn btn-danger" disabled=${busy}
            onClick=${() => { setDecision(decision === 'reject' ? null : 'reject'); setComment(''); }}>Reject</button>
        <//>` : null}
        ${!terminal && canApprove(me) ? html`
          <button class="btn btn-ok" disabled=${busy || !d.can_approve || Object.keys(edits).length > 0}
            title=${d.blocked_reason || ''}
            onClick=${() => { setDecision(decision === 'approve' ? null : 'approve'); setComment(''); }}>Approve…</button>` : null}
      <//>`}>

      ${err && html`<${Banner} kind="err" title="Action failed">${err}<//>`}

      ${task ? html`<div class="wf-bar">
        <div>
          <span class="small muted">Assignment:</span>
          ${claimed ? html` <strong>${claimed}</strong> ${claimed === me.username ? html`<${Pill} kind="ok">you<//>` : null}`
          : task.assigned_to ? html` assigned to <strong>${task.assigned_to}</strong>`
          : html` <span class="muted">unclaimed</span>`}
        </div>
        ${!terminal && canEditStaging(me) ? html`<div class="btn-row">
          ${claimed === me.username
            ? html`<button class="btn btn-sm" disabled=${busy} onClick=${() => claim('release')}>Release</button>`
            : !claimed ? html`<button class="btn btn-sm" disabled=${busy} onClick=${() => claim('claim')}>Claim</button>` : null}
        </div>` : null}
      </div>` : null}

      <div class="grid grid-4" style=${sx('margin-bottom:16px')}>
        <div><div class="small muted">Operation</div><div><${Pill} kind="info">${s.mdm_operation}<//></div></div>
        <div><div class="small muted">Resolved as</div><div>${s.mdm_change_type || '—'}</div></div>
        <div><div class="small muted">Status</div><div>${statusPill(s.mdm_status)}</div></div>
        <div><div class="small muted">Source</div><div class="mono small">${s.mdm_source_system || '—'}</div></div>
      </div>

      <div class="small muted" style=${sx('margin-bottom:14px')}>
        Submitted by <strong>${s.mdm_submitted_by || 'unknown'}</strong> ${fmtDate(s.mdm_submitted_at)}
        ${s.mdm_edited_by ? html` · last edited by <strong>${s.mdm_edited_by}</strong> ${fmtDate(s.mdm_edited_at)}` : null}
        ${task?.submit_rationale ? html`<div>Rationale: <em>${task.submit_rationale}</em></div>` : null}
      </div>

      ${errors.length > 0 && html`
        <${Banner} kind="err" title=${`${errors.length} validation issue(s) — fix before approving`}>
          <ul class="err-list">${errors.map((e, i) => html`
            <li key=${i}><span class="err-code">${e.code}</span>
              <span><strong>${e.field}</strong> — ${e.message}</span></li>`)}
          </ul>
          ${hasBrokenRef ? html`<div style=${sx('margin-top:10px')}>
            <button class="btn btn-sm" disabled=${busy} onClick=${reresolve}>Re-resolve references</button>
            <span class="small muted"> — retry now that referenced parents may exist.</span>
          </div>` : null}
        <//>`}

      ${!errors.length && d.can_approve && html`
        <${Banner} kind="ok" title="Passes all validation">Ready to apply to the golden record.<//>`}

      ${d.current_golden_record && Object.keys(d.diff).length > 0 && html`<${Fragment}>
        <h3 style=${sx('font-size:13px;margin:16px 0 8px')}>Changes to the golden record</h3>
        ${Object.entries(d.diff).map(([f, v]) => html`
          <div class="diff-row" key=${f}>
            <div class="fname">${f}</div>
            <div class="diff-old">${cell(v.current)}</div>
            <div class="diff-arrow">→</div>
            <div class="diff-new">${cell(v.incoming)}</div>
          </div>`)}
      <//>`}

      <div class="sep"></div>
      <h3 style=${sx('font-size:13px;margin-bottom:9px')}>
        Incoming values ${editable ? html`<span class="small muted">— editable</span>` : null}
      </h3>
      <div class="table-scroll"><table>
        <thead><tr><th>Field</th><th>Value</th><th>Supplied</th>${d.current_golden_record ? html`<th>Current golden</th>` : null}</tr></thead>
        <tbody>${attrNames.map((f) => html`
          <tr key=${f}>
            <td class="mono small">${f}${attrByName[f]?.data_type === 'reference' ? html` <${Pill} kind="purple">ref<//>` : null}</td>
            <td>${editable ? fieldEditor(f) : cell(s[f])}</td>
            <td>${supplied.includes(f) ? html`<${Pill} kind="info">sent<//>` : html`<span class="muted small">—</span>`}</td>
            ${d.current_golden_record ? html`<td class="mono small">${cell(d.current_golden_record[f])}</td>` : null}
          </tr>`)}
        </tbody></table></div>

      ${decision && html`
        <div class="card" style=${sx('margin-top:14px')}><div class="card-body">
          <label class="field"><span>
            ${decision === 'approve' ? 'Approval note' : decision === 'reject' ? 'Rejection reason' : 'What needs to change'}
            <span class="hint">— ${commentRequired ? 'required' : 'optional'}, recorded in the workflow history</span></span>
            <input type="text" value=${comment} autoFocus onInput=${(e) => setComment(e.target.value)}
              placeholder=${decision === 'reject' ? 'Duplicate of existing record'
                : decision === 'changes' ? 'Please correct the postal code' : 'Verified against source system'} /></label>
          ${commentRequired && !comment.trim() ? html`<div class="small" style=${sx('color:var(--err)')}>A comment is required for this decision.</div>` : null}
          <div class="btn-row" style=${sx('margin-top:10px')}>
            <button class="btn" onClick=${() => setDecision(null)}>Cancel</button>
            <button class="btn ${decision === 'reject' ? 'btn-danger' : decision === 'approve' ? 'btn-ok' : 'btn-primary'}"
              disabled=${busy || (commentRequired && !comment.trim())} onClick=${runDecision}>
              ${busy ? html`<span class="spinner"></span>` : null}
              ${decision === 'approve' ? 'Approve & apply' : decision === 'reject' ? 'Confirm rejection' : 'Send back'}
            </button>
          </div>
        </div></div>`}

      ${showHistory && wf && html`
        <${Modal} title=${`Workflow history — #${stagingId}`} onClose=${() => setShowHistory(false)}>
          ${(wf.history || []).length === 0 ? html`<${Empty} title="No steps recorded"><//>`
          : html`<div class="wf-timeline">${wf.history.map((h) => html`
            <div class="wf-step" key=${h.seq}>
              <div class="wf-step-head">
                <${Pill} kind=${h.step === 'approve' || h.step === 'apply' ? 'ok'
                  : h.step === 'reject' || h.step === 'terminate' ? 'err'
                  : h.step === 'request_changes' ? 'warn' : 'info'}>${h.step}<//>
                <span class="small">${h.actor || '—'}</span>
                <span class="small muted">${fmtDate(h.occurred_at)}</span>
              </div>
              ${h.from_status || h.to_status ? html`<div class="small muted">${h.from_status || '∅'} → ${h.to_status || '∅'}</div>` : null}
              ${h.comment ? html`<div class="small">${h.comment}</div>` : null}
            </div>`)}</div>`}
        <//>`}
    <//>`;
}

function ReviewView({ me, params, go }) {
  const [queues, setQueues] = useState(null);
  const [entity, setEntity] = useState(params?.entity || null);
  const [rows, setRows] = useState(null);
  const [statusFilter, setStatusFilter] = useState('pending_review');
  const [onlyInvalid, setOnlyInvalid] = useState(false);
  const [selected, setSelected] = useState([]);
  const [open, setOpen] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  const loadQueues = useCallback(async () => {
    try { setQueues(await api('/stewardship/queue')); } catch (e) { setErr(e.message); }
  }, []);
  useEffect(() => { loadQueues(); }, [loadQueues]);

  const loadRows = useCallback(async () => {
    if (!entity) { setRows(null); return; }
    try {
      const r = await api(`/stewardship/${entity}/queue?status=${statusFilter}&only_invalid=${onlyInvalid}&limit=200`);
      setRows(r); setSelected([]);
    } catch (e) { setErr(e.message); }
  }, [entity, statusFilter, onlyInvalid]);
  useEffect(() => { loadRows(); }, [loadRows]);

  const bulk = async (action) => {
    if (!selected.length) return;
    setBusy(true);
    try {
      const r = await api(`/stewardship/${entity}/staging/bulk-${action}`, {
        method: 'POST',
        body: JSON.stringify({ staging_ids: selected, note: action === 'reject' ? 'Bulk rejected' : 'Bulk approved' }),
      });
      const done = r.approved ?? r.rejected ?? 0;
      notify(`${done} record(s) ${action}ed${r.failed ? `, ${r.failed} failed` : ''}.`, r.failed ? 'err' : 'ok');
      if (r.errors?.length) r.errors.slice(0, 3).forEach((e) => notify(`#${e.staging_id}: ${e.error}`, 'err'));
      await loadRows(); await loadQueues();
    } catch (e) { notify(e.message, 'err'); } finally { setBusy(false); }
  };

  if (err) return html`<${Banner} kind="err" title="Could not load queue">${err}<//>`;
  if (!queues) return html`<${Spinner} />`;

  const attrCols = rows?.data?.length
    ? Object.keys(rows.data[0]).filter((k) => !k.startsWith('mdm_')).slice(0, 5) : [];

  return html`
    <div>
      <div class="filters">
        <select value=${entity || ''} onChange=${(e) => setEntity(e.target.value || null)}>
          <option value="">— select an entity —</option>
          ${queues.queues.map((q) => html`
            <option value=${q.entity} key=${q.entity}>${q.display_name || q.entity} (${q.pending_review})</option>`)}
        </select>
        ${entity && html`<${Fragment}>
          <select value=${statusFilter} onChange=${(e) => setStatusFilter(e.target.value)}>
            <option value="pending_review">Pending review</option>
            <option value="changes_requested">Changes requested</option>
            <option value="applied">Applied</option>
            <option value="rejected">Rejected</option>
            <option value="all">All statuses</option>
          </select>
          <label class="check"><input type="checkbox" checked=${onlyInvalid}
            onChange=${(e) => setOnlyInvalid(e.target.checked)} /> Only invalid</label>
          <button class="btn btn-sm" onClick=${() => { loadRows(); loadQueues(); }}>Refresh</button>
        <//>`}
      </div>

      ${!entity ? html`
        <div class="card"><div class="card-head"><div>
          <h3>Review queues</h3><div class="desc">Pick an entity to start reviewing</div></div></div>
          <div class="card-body flush">
            ${queues.queues.length === 0 ? html`<${Empty} title="Queue is clear">
              No staged records are waiting for review.<//>`
            : html`<table>
                <thead><tr><th>Entity</th><th class="right">Pending</th><th class="right">Invalid</th><th></th></tr></thead>
                <tbody>${queues.queues.map((q) => html`
                  <tr class="clickable" key=${q.entity} onClick=${() => setEntity(q.entity)}>
                    <td><strong>${q.display_name || q.entity}</strong>
                      <div class="small muted mono">${q.entity}</div></td>
                    <td class="num">${q.pending_review}</td>
                    <td class="num">${q.invalid ? html`<${Pill} kind="err">${q.invalid}<//>` : '0'}</td>
                    <td class="right"><button class="btn btn-sm">Review →</button></td>
                  </tr>`)}
                </tbody></table>`}
          </div></div>`
      : !rows ? html`<${Spinner} />`
      : html`
        <div class="card">
          <div class="card-head">
            <div><h3>${entity} — ${rows.meta.total} record(s)</h3>
              <div class="desc">Staged changes awaiting a steward decision</div></div>
            ${selected.length > 0 && (canApprove(me) || canReject(me)) && html`
              <div class="btn-row">
                <span class="small muted">${selected.length} selected</span>
                <button class="btn btn-sm btn-ok" disabled=${busy} onClick=${() => bulk('approve')}>Approve selected</button>
                <button class="btn btn-sm btn-danger" disabled=${busy} onClick=${() => bulk('reject')}>Reject selected</button>
              </div>`}
          </div>
          <div class="card-body flush">
            ${rows.data.length === 0 ? html`<${Empty} title="Nothing here">
              No records match the current filter.<//>`
            : html`<div class="table-scroll"><table>
                <thead><tr>
                  <th style=${sx('width:34px')}><input type="checkbox"
                    checked=${selected.length === rows.data.filter((r) => r.mdm_is_valid).length && selected.length > 0}
                    onChange=${(e) => setSelected(e.target.checked
                      ? rows.data.filter((r) => r.mdm_is_valid && r.mdm_status === 'pending_review').map((r) => r.mdm_staging_id) : [])} /></th>
                  <th>#</th><th>Op</th><th>Valid</th>
                  ${attrCols.map((c) => html`<th key=${c}>${c}</th>`)}
                  <th>Submitted by</th><th>Status</th><th></th>
                </tr></thead>
                <tbody>${rows.data.map((r) => html`
                  <tr key=${r.mdm_staging_id}>
                    <td><input type="checkbox" disabled=${!r.mdm_is_valid || r.mdm_status !== 'pending_review'}
                      checked=${selected.includes(r.mdm_staging_id)}
                      onChange=${(e) => setSelected(e.target.checked
                        ? [...selected, r.mdm_staging_id]
                        : selected.filter((x) => x !== r.mdm_staging_id))} /></td>
                    <td class="num">${r.mdm_staging_id}</td>
                    <td><${Pill} kind="info">${r.mdm_operation}<//></td>
                    <td>${r.mdm_is_valid ? html`<${Pill} kind="ok">valid<//>`
                      : html`<${Pill} kind="err">${(r.mdm_errors || []).length} error(s)<//>`}</td>
                    ${attrCols.map((c) => html`<td class="small mono" key=${c}>${cell(r[c])}</td>`)}
                    <td class="small">${r.mdm_submitted_by || '—'}</td>
                    <td>${statusPill(r.mdm_status)}</td>
                    <td class="right"><button class="btn btn-sm"
                      onClick=${() => setOpen(r.mdm_staging_id)}>Open</button></td>
                  </tr>`)}
                </tbody></table></div>`}
          </div>
        </div>`}

      ${open && html`<${ReviewDetail} entityName=${entity} stagingId=${open} me=${me}
        onClose=${() => setOpen(null)}
        onActioned=${() => { loadRows(); loadQueues(); }} />`}
    </div>`;
}

/* ============================================================ golden records */
function RecordsView({ me }) {
  const [models, setModels] = useState(null);
  const [entity, setEntity] = useState(null);
  const [fullModel, setFullModel] = useState(null);
  const [data, setData] = useState(null);
  const [search, setSearch] = useState('');
  const [colFilters, setColFilters] = useState({});
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [history, setHistory] = useState(null);
  const [editRecord, setEditRecord] = useState(null); // record | 'new' | null
  const [err, setErr] = useState(null);
  const [offset, setOffset] = useState(0);
  const LIMIT = 25;
  const canDirect = !!me.can_direct_edit;

  useEffect(() => {
    api('/models').then((m) => {
      const pub = m.filter((x) => x.status === 'published' || x.status === 'modified');
      setModels(pub);
      if (pub.length && !entity) setEntity(pub[0].name);
    }).catch((e) => setErr(e.message));
  }, []);

  useEffect(() => {
    if (!entity) { setFullModel(null); return; }
    setColFilters({});
    api(`/models/${entity}`).then(setFullModel).catch(() => setFullModel(null));
  }, [entity]);

  const load = useCallback(async () => {
    if (!entity) return;
    try {
      const qs = new URLSearchParams({ limit: LIMIT, offset, include_deleted: includeDeleted });
      if (search) qs.set('q', search);
      Object.entries(colFilters).forEach(([k, v]) => { if (v !== '' && v != null) qs.set(k, v); });
      setData(await api(`/data/${entity}?${qs}`));
    } catch (e) { setErr(e.message); }
  }, [entity, search, includeDeleted, offset, colFilters]);
  useEffect(() => { load(); }, [load]);

  const openHistory = async (id) => {
    try { setHistory(await api(`/data/${entity}/${id}/history`)); }
    catch (e) { notify(e.message, 'err'); }
  };

  if (err) return html`<${Banner} kind="err" title="Could not load records">${err}<//>`;
  if (!models) return html`<${Spinner} />`;
  if (!models.length) return html`<${Empty} title="No published entities">
    Golden records appear here once a model is published and data approved.<//>`;

  const cols = data?.data?.length
    ? Object.keys(data.data[0]).filter((k) => !['mdm_created_by', 'mdm_updated_by', 'mdm_source_system'].includes(k))
    : [];
  const filterAttrs = (fullModel?.attributes || []).map((a) => a.name);

  return html`
    <div>
      <div class="filters">
        <select value=${entity || ''} onChange=${(e) => { setEntity(e.target.value); setOffset(0); }}>
          ${models.map((m) => html`<option value=${m.name} key=${m.name}>${m.display_name || m.name}</option>`)}
        </select>
        <input type="text" placeholder="Search text fields…" value=${search}
          onInput=${(e) => { setSearch(e.target.value); setOffset(0); }} />
        <label class="check"><input type="checkbox" checked=${includeDeleted}
          onChange=${(e) => setIncludeDeleted(e.target.checked)} /> Include deleted</label>
        <a class="btn btn-sm" href="${API}/data/${entity}/export-csv" download>Export CSV</a>
        ${canDirect && fullModel ? html`<button class="btn btn-sm btn-primary"
          onClick=${() => setEditRecord('new')}>+ New record (direct)</button>` : null}
      </div>

      ${filterAttrs.length > 0 && html`
        <div class="filters col-filters">
          <span class="small muted">Column filters (exact):</span>
          ${filterAttrs.map((n) => html`
            <input key=${n} type="text" class="col-filter mono" placeholder=${n}
              value=${colFilters[n] ?? ''}
              onInput=${(e) => { setOffset(0); setColFilters((f) => ({ ...f, [n]: e.target.value })); }} />`)}
          ${Object.values(colFilters).some((v) => v) ? html`<button class="btn btn-sm"
            onClick=${() => { setColFilters({}); setOffset(0); }}>Clear</button>` : null}
        </div>`}

      ${!data ? html`<${Spinner} />` : html`
        <div class="card">
          <div class="card-head">
            <div><h3>${entity} — golden records</h3>
              <div class="desc">${data.meta.total} record(s) · approved, versioned master data</div></div>
          </div>
          <div class="card-body flush">
            ${data.data.length === 0 ? html`<${Empty} title="No records yet">
              Approved records will appear here.<//>`
            : html`<div class="table-scroll"><table>
                <thead><tr>${cols.map((c) => html`<th key=${c}>${c.replace('mdm_', '')}</th>`)}<th></th></tr></thead>
                <tbody>${data.data.map((r) => html`
                  <tr key=${r.mdm_id} style=${sx(r.mdm_is_deleted ? 'opacity:.55' : '')}>
                    ${cols.map((c) => html`<td class="small ${c.startsWith('mdm_') ? 'mono' : ''}" key=${c}>
                      ${c === 'mdm_id' ? html`<code>${String(r[c]).slice(0, 8)}…</code>`
                        : c.includes('_at') ? fmtDate(r[c])
                        : c === 'mdm_is_deleted' ? (r[c] ? html`<${Pill} kind="err">deleted<//>` : '—')
                        : cell(r[c])}</td>`)}
                    <td class="right nowrap"><div class="btn-row" style=${sx('justify-content:flex-end')}>
                      ${canDirect && fullModel && !r.mdm_is_deleted ? html`<button class="btn btn-sm"
                        onClick=${() => setEditRecord(r)}>Edit</button>` : null}
                      <button class="btn btn-sm" onClick=${() => openHistory(r.mdm_id)}>History</button>
                    </div></td>
                  </tr>`)}
                </tbody></table></div>`}
          </div>
          ${data.meta.total > LIMIT && html`
            <div class="card-head" style=${sx('border-top:1px solid var(--border);border-bottom:none')}>
              <span class="small muted">Showing ${offset + 1}–${Math.min(offset + LIMIT, data.meta.total)} of ${data.meta.total}</span>
              <div class="btn-row">
                <button class="btn btn-sm" disabled=${offset === 0}
                  onClick=${() => setOffset(Math.max(0, offset - LIMIT))}>← Previous</button>
                <button class="btn btn-sm" disabled=${!data.meta.has_more}
                  onClick=${() => setOffset(offset + LIMIT)}>Next →</button>
              </div>
            </div>`}
        </div>`}

      ${history && html`
        <${Modal} wide title=${`Version history — ${String(history.record_id).slice(0, 8)}…`}
          onClose=${() => setHistory(null)}>
          ${history.versions.length === 0 ? html`<${Empty} title="No prior versions">
            This record has not been changed since it was created.<//>`
          : html`<div class="table-scroll"><table>
              <thead><tr><th>Version</th><th>Change</th><th>Valid from</th><th>Valid to</th><th>Changed by</th>
                ${Object.keys(history.versions[0]).filter((k) => !k.startsWith('mdm_')).slice(0, 4)
                  .map((c) => html`<th key=${c}>${c}</th>`)}</tr></thead>
              <tbody>${history.versions.map((v) => html`
                <tr key=${v.mdm_history_id}>
                  <td class="num">v${v.mdm_version}</td>
                  <td><${Pill} kind=${v.mdm_change_type === 'delete' ? 'err' : 'info'}>${v.mdm_change_type}<//></td>
                  <td class="small">${fmtDate(v.mdm_valid_from)}</td>
                  <td class="small">${fmtDate(v.mdm_valid_to)}</td>
                  <td class="small">${v.mdm_changed_by || '—'}</td>
                  ${Object.keys(v).filter((k) => !k.startsWith('mdm_')).slice(0, 4)
                    .map((c) => html`<td class="small mono" key=${c}>${cell(v[c])}</td>`)}
                </tr>`)}
              </tbody></table></div>`}
        <//>`}

      ${editRecord && fullModel && html`
        <${RecordForm} model=${fullModel}
          record=${editRecord === 'new' ? null : editRecord}
          onClose=${() => setEditRecord(null)}
          onSaved=${() => { setEditRecord(null); load(); }} />`}
    </div>`;
}

/* ============================================================ inbox */
function InboxView({ me, go }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(null); // {entity, staging_id}

  const load = useCallback(async () => {
    try { setData(await api('/stewardship/inbox')); } catch (e) { setErr(e.message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const act = async (t, action) => {
    setBusy(true);
    try {
      await api(`/stewardship/${t.entity_name}/staging/${t.staging_id}/${action}`, { method: 'POST' });
      notify(action === 'claim' ? 'Claimed.' : 'Released.');
      await load();
    } catch (e) { notify(e.message, 'err'); } finally { setBusy(false); }
  };

  if (err) return html`<${Banner} kind="err" title="Could not load inbox">${err}<//>`;
  if (!data) return html`<${Spinner} />`;
  const c = data.counts || {};

  const taskTable = (tasks, showClaim) => tasks.length === 0
    ? html`<${Empty} title="Nothing here"><//>`
    : html`<div class="table-scroll"><table>
        <thead><tr><th>Entity</th><th>#</th><th>Status</th><th>Submitted by</th>
          <th>Claimant</th><th>Age</th><th></th></tr></thead>
        <tbody>${tasks.map((t) => html`
          <tr key=${t.task_id}>
            <td class="mono small">${t.entity_name}${t.domain ? html`<div class="small muted">${t.domain}</div>` : null}</td>
            <td class="num">${t.staging_id}</td>
            <td>${statusPill(t.status)}</td>
            <td class="small">${t.submitted_by || '—'}</td>
            <td class="small">${t.claimed_by || t.assigned_to || html`<span class="muted">—</span>`}</td>
            <td class="small muted">${fmtAge(t.age_seconds)}</td>
            <td class="right nowrap"><div class="btn-row" style=${sx('justify-content:flex-end')}>
              ${showClaim && canEditStaging(me)
                ? (t.claimed_by === me.username
                    ? html`<button class="btn btn-sm" disabled=${busy} onClick=${() => act(t, 'release')}>Release</button>`
                    : !t.claimed_by ? html`<button class="btn btn-sm" disabled=${busy} onClick=${() => act(t, 'claim')}>Claim</button>` : null)
                : null}
              <button class="btn btn-sm" onClick=${() => setOpen({ entity: t.entity_name, staging_id: t.staging_id })}>Open</button>
            </div></td>
          </tr>`)}
        </tbody></table></div>`;

  return html`
    <div>
      <div class="grid grid-4" style=${sx('margin-bottom:20px')}>
        <div class="stat ${c.assigned_to_me ? 'alert' : ''}"><div class="k">Assigned to me</div>
          <div class="v">${c.assigned_to_me || 0}</div></div>
        <div class="stat"><div class="k">Unassigned pool</div><div class="v">${c.unassigned || 0}</div></div>
        <div class="stat ${c.changes_requested ? 'alert' : ''}"><div class="k">Changes requested</div>
          <div class="v">${c.changes_requested || 0}</div></div>
        <div class="stat"><div class="k">Total pending</div><div class="v">${c.total_pending || 0}</div></div>
      </div>

      <div class="card" style=${sx('margin-bottom:18px')}>
        <div class="card-head"><div><h3>Assigned to me</h3>
          <div class="desc">Change requests you have claimed or been assigned</div></div>
          <button class="btn btn-sm" onClick=${load}>Refresh</button></div>
        <div class="card-body flush">${taskTable(data.assigned_to_me || [], true)}</div>
      </div>

      <div class="card">
        <div class="card-head"><div><h3>Unassigned pool</h3>
          <div class="desc">Available for any reviewer in your domains to claim</div></div></div>
        <div class="card-body flush">${taskTable(data.unassigned || [], true)}</div>
      </div>

      ${open && html`<${ReviewDetail} entityName=${open.entity} stagingId=${open.staging_id} me=${me}
        onClose=${() => setOpen(null)} onActioned=${load} />`}
    </div>`;
}

/* ============================================================ admin */
function AdminView({ me }) {
  const [tab, setTab] = useState('system');
  const tabs = [
    ['system', 'System'], ['users', 'Users & roles'], ['domains', 'Domains'],
    ['workflows', 'Workflows'], ['ldap', 'LDAP / AD'], ['keys', 'API keys'],
    ['notifications', 'Notifications'], ['distribution', 'Distribution'],
    ['mappings', 'Field mappings'], ['audit', 'Audit log'], ['batches', 'Pipeline runs'],
  ];
  return html`
    <div>
      <div class="tabs">
        ${tabs.map(([k, label]) => html`
          <button class="tab ${tab === k ? 'active' : ''}" key=${k} onClick=${() => setTab(k)}>${label}</button>`)}
      </div>
      ${tab === 'system' && html`<${AdminSystem} />`}
      ${tab === 'users' && html`<${AdminUsers} me=${me} />`}
      ${tab === 'domains' && html`<${AdminDomains} />`}
      ${tab === 'workflows' && html`<${AdminWorkflows} me=${me} />`}
      ${tab === 'ldap' && html`<${AdminLdap} />`}
      ${tab === 'keys' && html`<${AdminKeys} />`}
      ${tab === 'notifications' && html`<${AdminNotifications} />`}
      ${tab === 'distribution' && html`<${AdminDistribution} />`}
      ${tab === 'mappings' && html`<${AdminFieldMappings} />`}
      ${tab === 'audit' && html`<${AdminAudit} />`}
      ${tab === 'batches' && html`<${AdminBatches} />`}
    </div>`;
}

function AdminSystem() {
  const [info, setInfo] = useState(null);
  const [privs, setPrivs] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api('/admin/system').then(setInfo).catch((e) => setErr(e.message));
    api('/models/cluster/privileges').then(setPrivs).catch(() => {});
  }, []);
  useEffect(load, [load]);

  const bootstrap = async () => {
    setBusy(true);
    try {
      const r = await api('/models/cluster/bootstrap', { method: 'POST' });
      notify(`Provisioned ${r.schemas.length} schemas and ${r.metadata_tables.length} metadata tables.`);
      load();
    } catch (e) { notify(e.message, 'err'); } finally { setBusy(false); }
  };

  if (err) return html`<${Banner} kind="err">${err}<//>`;
  if (!info) return html`<${Spinner} />`;

  return html`
    <div class="grid grid-2">
      <div class="card">
        <div class="card-head"><div><h3>Database</h3>
          <div class="desc">Target PostgreSQL cluster</div></div></div>
        <div class="card-body">
          ${info.database.connected ? html`
            <${Banner} kind="ok" title="Connected">
              PostgreSQL ${info.database.server_version} · database
              <code>${info.database.database}</code> as <code>${info.database.user}</code>
            <//>`
          : html`<${Banner} kind="err" title="Not connected">${info.database.error}<//>`}
          <dl class="kv">
            <dt>Environment</dt><dd>${info.environment}</dd>
            <dt>Schemas</dt><dd>${info.schemas.join(', ')}</dd>
            <dt>Segregation of duties</dt><dd>${info.segregation_of_duties ? 'enforced' : 'disabled'}</dd>
            <dt>Auto-promote landing</dt><dd>${info.auto_promote_landing ? 'on' : 'off'}</dd>
            <dt>Delete behaviour</dt><dd>${info.soft_delete ? 'soft delete' : 'hard delete'}</dd>
          </dl>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><div><h3>Cluster privileges</h3>
          <div class="desc">Preflight for schema and table creation</div></div>
          <button class="btn btn-sm" disabled=${busy} onClick=${bootstrap}>
            ${busy ? html`<span class="spinner"></span>` : null} Run bootstrap</button></div>
        <div class="card-body">
          ${!privs ? html`<${Spinner} />` : html`<${Fragment}>
            <div class="small muted" style=${sx('margin-bottom:10px')}>DDL role: <code>${privs.ddl_user}</code></div>
            <table><tbody>
              ${privs.checks.map((c) => html`
                <tr key=${c.name}>
                  <td class="mono small">${c.name}</td>
                  <td>${c.ok ? html`<${Pill} kind="ok">ok<//>` : html`<${Pill} kind="warn">missing<//>`}</td>
                  <td class="small muted">${c.detail}${c.remedy ? html`<div><code class="small">${c.remedy}</code></div>` : null}</td>
                </tr>`)}
            </tbody></table>
          <//>`}
        </div>
      </div>
    </div>`;
}

function UserPermissionsModal({ user, onClose, onSaved }) {
  const [entityPerms, setEntityPerms] = useState(JSON.stringify(user.entity_permissions || {}, null, 2));
  const [domainPerms, setDomainPerms] = useState(JSON.stringify(user.domain_permissions || {}, null, 2));
  const [domainRoles, setDomainRoles] = useState(JSON.stringify(user.domain_roles || {}, null, 2));
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true); setErr(null);
    let body;
    try {
      body = {
        entity_permissions: JSON.parse(entityPerms || '{}'),
        domain_permissions: JSON.parse(domainPerms || '{}'),
        domain_roles: JSON.parse(domainRoles || '{}'),
      };
    } catch (e) { setErr(`Invalid JSON: ${e.message}`); setBusy(false); return; }
    try {
      await api(`/admin/users/${user.username}/permissions`, { method: 'PUT', body: JSON.stringify(body) });
      notify(`Permissions updated for ${user.username}.`); onSaved();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  return html`
    <${Modal} wide title=${`Access overrides — ${user.username}`} onClose=${onClose}
      footer=${html`<${Fragment}>
        <button class="btn" onClick=${onClose}>Cancel</button>
        <button class="btn btn-primary" disabled=${busy} onClick=${save}>Save overrides</button>
      <//>`}>
      ${err && html`<${Banner} kind="err">${err}<//>`}
      <${Banner} kind="info">
        <strong>domain_roles</strong> GRANTS a role's permissions within a domain
        (conferral, e.g. <code>{"finance": ["approver"]}</code>).
        <strong>entity_permissions</strong> / <strong>domain_permissions</strong>
        RESTRICT to an allow-list (e.g. <code>{"customer": ["read","write"]}</code>).
      <//>
      <label class="field"><span>domain_roles <span class="hint">— {domain: [role, …]}</span></span>
        <textarea class="mono" rows="4" value=${domainRoles} onInput=${(e) => setDomainRoles(e.target.value)}></textarea></label>
      <label class="field"><span>entity_permissions <span class="hint">— {entity: [read|write, …]}</span></span>
        <textarea class="mono" rows="4" value=${entityPerms} onInput=${(e) => setEntityPerms(e.target.value)}></textarea></label>
      <label class="field"><span>domain_permissions <span class="hint">— {domain: [read|write, …]}</span></span>
        <textarea class="mono" rows="4" value=${domainPerms} onInput=${(e) => setDomainPerms(e.target.value)}></textarea></label>
    <//>`;
}

function AdminUsers({ me }) {
  const [users, setUsers] = useState(null);
  const [roles, setRoles] = useState(null);
  const [err, setErr] = useState(null);
  const [permUser, setPermUser] = useState(null);

  const load = useCallback(() => {
    api('/admin/users').then(setUsers).catch((e) => setErr(e.message));
    api('/admin/roles').then((r) => setRoles(r.roles)).catch(() => {});
  }, []);
  useEffect(load, [load]);

  const setUserRoles = async (username, next) => {
    try {
      const r = await api(`/admin/users/${username}/roles`, { method: 'PUT', body: JSON.stringify(next) });
      notify(r.note || `Roles updated for ${username}.`);
      load();
    } catch (e) { notify(e.message, 'err'); }
  };
  const toggleActive = async (u) => {
    try {
      await api(`/admin/users/${u.username}/status?is_active=${!u.is_active}`, { method: 'PUT' });
      notify(`${u.username} ${u.is_active ? 'disabled' : 'enabled'}.`); load();
    } catch (e) { notify(e.message, 'err'); }
  };

  if (err) return html`<${Banner} kind="err">${err}<//>`;
  if (!users || !roles) return html`<${Spinner} />`;
  const allRoles = roles.map((r) => r.name);

  return html`
    <div>
      <div class="card" style=${sx('margin-bottom:18px')}>
        <div class="card-head"><div><h3>Users</h3>
          <div class="desc">Directory accounts are provisioned automatically on first sign-in</div></div></div>
        <div class="card-body flush">
          <div class="table-scroll"><table>
            <thead><tr><th>User</th><th>Source</th><th>Roles</th><th>Last sign-in</th><th>Active</th><th></th></tr></thead>
            <tbody>${users.map((u) => html`
              <tr key=${u.username}>
                <td><strong>${u.display_name || u.username}</strong>
                  <div class="small muted mono">${u.username}${u.email ? ` · ${u.email}` : ''}</div>
                  ${u.dn ? html`<div class="small muted mono" style=${sx('font-size:10.5px')}>${u.dn}</div>` : null}</td>
                <td>${statusPill(u.source === 'ldap' ? 'approved' : u.source === 'local' ? 'pending' : 'draft')} ${u.source}</td>
                <td><div class="attr-flags">
                  ${allRoles.map((r) => html`
                    <label key=${r}><input type="checkbox" checked=${(u.roles || []).includes(r)}
                      onChange=${(e) => setUserRoles(u.username,
                        e.target.checked ? [...(u.roles || []), r] : (u.roles || []).filter((x) => x !== r))} />${r}</label>`)}
                </div></td>
                <td class="small">${fmtDate(u.last_login_at)}</td>
                <td>${u.is_active ? html`<${Pill} kind="ok">yes<//>` : html`<${Pill} kind="err">no<//>`}</td>
                <td class="right nowrap"><div class="btn-row" style=${sx('justify-content:flex-end')}>
                  <button class="btn btn-sm" onClick=${() => setPermUser(u)}>Permissions</button>
                  ${u.username !== me.username ? html`
                    <button class="btn btn-sm" onClick=${() => toggleActive(u)}>
                      ${u.is_active ? 'Disable' : 'Enable'}</button>` : null}
                </div></td>
              </tr>`)}
            </tbody></table></div>
        </div>
      </div>

      ${permUser && html`<${UserPermissionsModal} user=${permUser}
        onClose=${() => setPermUser(null)}
        onSaved=${() => { setPermUser(null); load(); }} />`}

      <div class="card">
        <div class="card-head"><div><h3>Role capabilities</h3>
          <div class="desc">What each role is permitted to do</div></div></div>
        <div class="card-body flush">
          <table><thead><tr><th>Role</th><th>Description</th><th>Permissions</th></tr></thead>
            <tbody>${roles.map((r) => html`
              <tr key=${r.name}>
                <td><${Pill} kind=${r.name === 'admin' ? 'purple' : r.name === 'steward' ? 'info' : 'mute'}>${r.name}<//></td>
                <td class="small">${r.description}</td>
                <td class="small mono">${r.permissions.join(' · ')}</td>
              </tr>`)}
            </tbody></table>
        </div>
      </div>
    </div>`;
}

function AdminLdap() {
  const [maps, setMaps] = useState(null);
  const [test, setTest] = useState(null);
  const [dn, setDn] = useState('');
  const [role, setRole] = useState('steward');
  const [err, setErr] = useState(null);

  const load = useCallback(() => {
    api('/admin/ldap/group-mappings').then(setMaps).catch((e) => setErr(e.message));
  }, []);
  useEffect(load, [load]);

  const add = async () => {
    try {
      await api('/admin/ldap/group-mappings', { method: 'POST', body: JSON.stringify({ group_dn: dn, role }) });
      notify('Group mapping added.'); setDn(''); load();
    } catch (e) { notify(e.message, 'err'); }
  };
  const remove = async (id) => {
    try { await api(`/admin/ldap/group-mappings/${id}`, { method: 'DELETE' }); notify('Mapping removed.'); load(); }
    catch (e) { notify(e.message, 'err'); }
  };
  const runTest = async () => {
    setTest({ loading: true });
    try { setTest(await api('/auth/ldap/test')); } catch (e) { setTest({ ok: false, error: e.message }); }
  };

  if (err) return html`<${Banner} kind="err">${err}<//>`;
  return html`
    <div class="grid grid-2">
      <div class="card">
        <div class="card-head"><div><h3>Directory connection</h3>
          <div class="desc">Configured through environment variables</div></div>
          <button class="btn btn-sm" onClick=${runTest}>Test connection</button></div>
        <div class="card-body">
          ${test?.loading ? html`<${Spinner} label="Contacting directory…" />`
          : test ? (test.ok
            ? html`<${Banner} kind="ok" title="Directory reachable">
                Bound as <code>${test.bound_as}</code> · StartTLS ${test.start_tls ? 'on' : 'off'} · LDAPS ${test.use_ssl ? 'on' : 'off'}<//>`
            : html`<${Banner} kind="err" title="Connection failed">${test.error}<//>`)
          : html`<div class="small muted">Run a test to verify the service-account bind.</div>`}
          <${Banner} kind="info" title="How authentication works">
            ${'The service account locates the user, then the application re-binds '}
            <em>as that user</em>${' to verify the password. Nested AD groups are '}
            ${'resolved transitively, then mapped to roles below.'}
          <//>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><div><h3>Group → role mapping</h3>
          <div class="desc">Directory group DNs that grant application roles</div></div></div>
        <div class="card-body">
          <div class="filters">
            <input type="text" class="mono" placeholder="CN=MDM_Stewards,OU=Groups,DC=corp,DC=com"
              value=${dn} style=${sx('flex:1;min-width:260px')} onInput=${(e) => setDn(e.target.value)} />
            <select value=${role} onChange=${(e) => setRole(e.target.value)}>
              <option value="admin">admin</option><option value="steward">steward</option>
              <option value="reader">reader</option>
            </select>
            <button class="btn btn-sm btn-primary" disabled=${!dn} onClick=${add}>Add</button>
          </div>
          ${!maps ? html`<${Spinner} />`
          : maps.length === 0 ? html`
            <${Banner} kind="warn" title="No mappings configured">
              Without a mapping, directory users authenticate but receive no roles
              and cannot access anything.
            <//>`
          : html`<table><thead><tr><th>Group DN</th><th>Role</th><th></th></tr></thead>
              <tbody>${maps.map((m) => html`
                <tr key=${m.id}>
                  <td class="mono small">${m.group_dn}</td>
                  <td><${Pill} kind="info">${m.role}<//></td>
                  <td class="right"><button class="btn btn-sm btn-danger"
                    onClick=${() => remove(m.id)}>Remove</button></td>
                </tr>`)}
              </tbody></table>`}
        </div>
      </div>
    </div>`;
}

function AdminKeys() {
  const [keys, setKeys] = useState(null);
  const [name, setName] = useState('');
  const [source, setSource] = useState('');
  const [elevated, setElevated] = useState(false);
  const [allowedDomains, setAllowedDomains] = useState('');
  const [issued, setIssued] = useState(null);

  const load = useCallback(() => { api('/admin/api-keys').then(setKeys).catch(() => setKeys([])); }, []);
  useEffect(load, [load]);

  const create = async () => {
    try {
      const r = await api('/admin/api-keys', {
        method: 'POST', body: JSON.stringify({
          name, source_system: source || null, allowed_entities: [],
          elevated,
          allowed_domains: allowedDomains.split(',').map((x) => x.trim()).filter(Boolean),
        }),
      });
      setIssued(r); setName(''); setSource(''); setElevated(false); setAllowedDomains(''); load();
    } catch (e) { notify(e.message, 'err'); }
  };
  const revoke = async (id) => {
    try { await api(`/admin/api-keys/${id}`, { method: 'DELETE' }); notify('Key revoked.'); load(); }
    catch (e) { notify(e.message, 'err'); }
  };

  return html`
    <div>
      <div class="card" style=${sx('margin-bottom:18px')}>
        <div class="card-head"><div><h3>Issue a service key</h3>
          <div class="desc">Machine accounts write into landing only — they cannot approve</div></div></div>
        <div class="card-body">
          <div class="filters">
            <input type="text" placeholder="Key name, e.g. SAP integration" value=${name}
              onInput=${(e) => setName(e.target.value)} style=${sx('flex:1;min-width:220px')} />
            <input type="text" placeholder="Source system (optional)" value=${source}
              onInput=${(e) => setSource(e.target.value)} />
            <label class="check" title="Cross-domain write reach (never approval)">
              <input type="checkbox" checked=${elevated} onChange=${(e) => setElevated(e.target.checked)} /> Elevated</label>
            <input type="text" placeholder="Allowed domains (comma-sep, elevated)" value=${allowedDomains}
              disabled=${!elevated} onInput=${(e) => setAllowedDomains(e.target.value)} />
            <button class="btn btn-primary" disabled=${!name} onClick=${create}>Issue key</button>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head"><div><h3>API keys</h3></div></div>
        <div class="card-body flush">
          ${!keys ? html`<${Spinner} />`
          : keys.length === 0 ? html`<${Empty} title="No API keys">
            Issue a key to let a source system submit changes.<//>`
          : html`<table>
              <thead><tr><th>Name</th><th>Prefix</th><th>Source</th><th>Last used</th><th>Active</th><th></th></tr></thead>
              <tbody>${keys.map((k) => html`
                <tr key=${k.id}>
                  <td><strong>${k.name}</strong>
                    ${k.elevated ? html` <${Pill} kind="purple">elevated<//>` : null}
                    ${(k.allowed_domains || []).length ? html`<div class="small muted mono">${k.allowed_domains.join(', ')}</div>` : null}</td>
                  <td class="mono small">${k.key_prefix}…</td>
                  <td class="small">${k.source_system || '—'}</td>
                  <td class="small">${fmtDate(k.last_used_at)}</td>
                  <td>${k.is_active ? html`<${Pill} kind="ok">yes<//>` : html`<${Pill} kind="mute">revoked<//>`}</td>
                  <td class="right">${k.is_active && html`<button class="btn btn-sm btn-danger"
                    onClick=${() => revoke(k.id)}>Revoke</button>`}</td>
                </tr>`)}
              </tbody></table>`}
        </div>
      </div>

      ${issued && html`
        <${Modal} title="API key issued" onClose=${() => setIssued(null)}
          footer=${html`<button class="btn btn-primary" onClick=${() => setIssued(null)}>Done</button>`}>
          <${Banner} kind="warn" title="Copy this now">
            ${issued.warning} It will not be shown again.
          <//>
          <label class="field"><span>API key</span>
            <input type="text" class="mono" readOnly value=${issued.api_key}
              onClick=${(e) => e.target.select()} /></label>
          <div class="small muted">${issued.usage}</div>
          <pre class="sql" style=${sx('margin-top:12px')}>curl -X POST ${location.origin}${API}/data/&lt;entity&gt; \\
  -H "X-API-Key: ${issued.api_key}" \\
  -H "Content-Type: application/json" \\
  -d '{"field":"value"}'</pre>
        <//>`}
    </div>`;
}

/* ------------------------------------------------------------- admin: domains */
function DomainForm({ initial, onClose, onSaved }) {
  const editing = !!initial;
  const [f, setF] = useState({
    name: initial?.name || '', display_name: initial?.display_name || '',
    description: initial?.description || '',
    requires_approval: initial?.requires_approval ?? true,
    default_soft_delete: initial?.default_soft_delete ?? true,
    retention_days: initial?.retention_days ?? '',
  });
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));

  const save = async () => {
    setBusy(true); setErr(null);
    const body = {
      name: f.name, display_name: f.display_name || null, description: f.description || null,
      requires_approval: f.requires_approval, default_soft_delete: f.default_soft_delete,
      retention_days: f.retention_days === '' ? null : +f.retention_days,
    };
    try {
      if (editing) await api(`/domains/${f.name}`, { method: 'PUT', body: JSON.stringify(body) });
      else await api('/domains', { method: 'POST', body: JSON.stringify(body) });
      notify(`Domain “${f.name}” ${editing ? 'updated' : 'created'}.`); onSaved();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  return html`
    <${Modal} title=${editing ? `Edit domain — ${f.name}` : 'New domain'} onClose=${onClose}
      footer=${html`<${Fragment}>
        <button class="btn" onClick=${onClose}>Cancel</button>
        <button class="btn btn-primary" disabled=${busy || !f.name} onClick=${save}>${editing ? 'Save' : 'Create'}</button>
      <//>`}>
      ${err && html`<${Banner} kind="err">${err}<//>`}
      <label class="field"><span>Name <span class="hint">— lower snake_case, immutable</span></span>
        <input type="text" class="mono" disabled=${editing} value=${f.name}
          onInput=${(e) => set('name', e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_'))} placeholder="finance" /></label>
      <label class="field"><span>Display name</span>
        <input type="text" value=${f.display_name} onInput=${(e) => set('display_name', e.target.value)} /></label>
      <label class="field"><span>Description</span>
        <input type="text" value=${f.description} onInput=${(e) => set('description', e.target.value)} /></label>
      <div class="grid grid-2">
        <label class="field"><span>Retention (days) <span class="hint">— lifecycle default</span></span>
          <input type="number" value=${f.retention_days} onInput=${(e) => set('retention_days', e.target.value)} /></label>
        <div style=${sx('display:flex;flex-direction:column;gap:8px;justify-content:flex-end')}>
          <label class="check"><input type="checkbox" checked=${f.requires_approval}
            onChange=${(e) => set('requires_approval', e.target.checked)} /> Requires approval (default)</label>
          <label class="check"><input type="checkbox" checked=${f.default_soft_delete}
            onChange=${(e) => set('default_soft_delete', e.target.checked)} /> Soft delete (default)</label>
        </div>
      </div>
    <//>`;
}

function AdminDomains() {
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState(null);
  const [editing, setEditing] = useState(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(() => { api('/domains').then(setRows).catch((e) => setErr(e.message)); }, []);
  useEffect(load, [load]);

  const del = async (name) => {
    try { await api(`/domains/${name}`, { method: 'DELETE' }); notify(`Domain ${name} deleted.`); load(); }
    catch (e) { notify(e.message, 'err'); }
  };

  if (err) return html`<${Banner} kind="err">${err}<//>`;
  if (!rows) return html`<${Spinner} />`;
  return html`
    <div>
      <div class="btn-row" style=${sx('margin-bottom:16px')}>
        <button class="btn btn-primary" onClick=${() => setCreating(true)}>+ New domain</button>
      </div>
      <div class="card"><div class="card-head"><div><h3>Governance domains</h3>
        <div class="desc">Group entities, scope access and supply lifecycle defaults</div></div></div>
        <div class="card-body flush">
          ${rows.length === 0 ? html`<${Empty} title="No domains defined"><//>`
          : html`<div class="table-scroll"><table>
            <thead><tr><th>Name</th><th>Entities</th><th>Approval</th><th>Soft delete</th><th>Retention</th><th></th></tr></thead>
            <tbody>${rows.map((d) => html`
              <tr key=${d.name}>
                <td><strong>${d.display_name || d.name}</strong><div class="small muted mono">${d.name}</div></td>
                <td class="num">${d.entity_count}</td>
                <td>${d.requires_approval ? 'yes' : 'no'}</td>
                <td>${d.default_soft_delete ? 'soft' : 'hard'}</td>
                <td class="small">${d.retention_days ?? '—'}</td>
                <td class="right nowrap"><div class="btn-row" style=${sx('justify-content:flex-end')}>
                  <button class="btn btn-sm" onClick=${() => setEditing(d)}>Edit</button>
                  ${d.name !== 'default' ? html`<button class="btn btn-sm btn-danger" onClick=${() => del(d.name)}>Delete</button>` : null}
                </div></td>
              </tr>`)}
            </tbody></table></div>`}
        </div>
      </div>
      ${creating && html`<${DomainForm} onClose=${() => setCreating(false)} onSaved=${() => { setCreating(false); load(); }} />`}
      ${editing && html`<${DomainForm} initial=${editing} onClose=${() => setEditing(null)} onSaved=${() => { setEditing(null); load(); }} />`}
    </div>`;
}

/* ----------------------------------------------------------- admin: workflows */
function AdminWorkflows({ me }) {
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState(null);
  const [statusFilter, setStatusFilter] = useState('');
  const [action, setAction] = useState(null); // {task, kind}
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const qs = new URLSearchParams();
    if (statusFilter) qs.set('status', statusFilter);
    try { setRows((await api(`/admin/workflows?${qs}`)).data); } catch (e) { setErr(e.message); }
  }, [statusFilter]);
  useEffect(() => { load(); }, [load]);

  const run = async () => {
    setBusy(true);
    try {
      if (action.kind === 'terminate') {
        await api(`/admin/workflows/${action.task.task_id}/terminate`, { method: 'POST', body: JSON.stringify({ reason: input }) });
        notify('Task terminated.');
      } else {
        await api(`/admin/workflows/${action.task.task_id}/reassign`, { method: 'POST', body: JSON.stringify({ assignee: input || null }) });
        notify('Task reassigned.');
      }
      setAction(null); setInput(''); await load();
    } catch (e) { notify(e.message, 'err'); } finally { setBusy(false); }
  };

  if (err) return html`<${Banner} kind="err">${err}<//>`;
  return html`
    <div class="card">
      <div class="card-head"><div><h3>Active workflow tasks</h3>
        <div class="desc">Cross-entity view so stuck change requests are visible</div></div>
        <div class="filters" style=${sx('margin:0')}>
          <select value=${statusFilter} onChange=${(e) => setStatusFilter(e.target.value)}>
            <option value="">Active (default)</option>
            <option value="pending_review">Pending review</option>
            <option value="changes_requested">Changes requested</option>
            <option value="applied">Applied</option>
            <option value="rejected">Rejected</option>
            <option value="terminated">Terminated</option>
          </select>
          <button class="btn btn-sm" onClick=${load}>Refresh</button>
        </div>
      </div>
      <div class="card-body flush">
        ${!rows ? html`<${Spinner} />`
        : rows.length === 0 ? html`<${Empty} title="No tasks"><//>`
        : html`<div class="table-scroll"><table>
            <thead><tr><th>Entity</th><th>#</th><th>Status</th><th>Submitted by</th>
              <th>Assignee</th><th>Age</th><th></th></tr></thead>
            <tbody>${rows.map((t) => html`
              <tr key=${t.task_id}>
                <td class="mono small">${t.entity_name}${t.domain ? html`<div class="small muted">${t.domain}</div>` : null}</td>
                <td class="num">${t.staging_id}</td>
                <td>${statusPill(t.status)}</td>
                <td class="small">${t.submitted_by || '—'}</td>
                <td class="small">${t.claimed_by || t.assigned_to || html`<span class="muted">—</span>`}</td>
                <td class="small ${t.age_seconds > 604800 ? '' : 'muted'}"
                  style=${sx(t.age_seconds > 604800 ? 'color:var(--err)' : '')}>${fmtAge(t.age_seconds)}</td>
                <td class="right nowrap"><div class="btn-row" style=${sx('justify-content:flex-end')}>
                  <button class="btn btn-sm" onClick=${() => { setAction({ task: t, kind: 'reassign' }); setInput(''); }}>Reassign</button>
                  <button class="btn btn-sm btn-danger" onClick=${() => { setAction({ task: t, kind: 'terminate' }); setInput(''); }}>Terminate</button>
                </div></td>
              </tr>`)}
            </tbody></table></div>`}
      </div>
      ${action && html`
        <${Modal} title=${action.kind === 'terminate' ? `Terminate #${action.task.staging_id}` : `Reassign #${action.task.staging_id}`}
          onClose=${() => setAction(null)}
          footer=${html`<${Fragment}>
            <button class="btn" onClick=${() => setAction(null)}>Cancel</button>
            <button class="btn ${action.kind === 'terminate' ? 'btn-danger' : 'btn-primary'}"
              disabled=${busy || (action.kind === 'terminate' && !input.trim())} onClick=${run}>
              ${action.kind === 'terminate' ? 'Terminate task' : 'Reassign'}</button>
          <//>`}>
          <label class="field"><span>${action.kind === 'terminate' ? 'Reason (required)' : 'Assignee username (blank = unassign)'}</span>
            <input type="text" value=${input} autoFocus onInput=${(e) => setInput(e.target.value)} /></label>
        <//>`}
    </div>`;
}

/* ------------------------------------------------------- admin: notifications */
function AdminNotifications() {
  const [rows, setRows] = useState(null);
  const [meta, setMeta] = useState(null);
  const [filters, setFilters] = useState({ status: '', domain: '', event: '' });
  const [busy, setBusy] = useState(false);
  const [testTo, setTestTo] = useState('');

  const load = useCallback(async () => {
    const qs = new URLSearchParams({ limit: 100 });
    Object.entries(filters).forEach(([k, v]) => v && qs.set(k, v));
    try { const r = await api(`/admin/notifications?${qs}`); setRows(r.data); setMeta(r.meta); }
    catch (e) { notify(e.message, 'err'); }
  }, [filters]);
  useEffect(() => { load(); }, [load]);

  const flush = async () => {
    setBusy(true);
    try { const c = await api('/admin/notifications/flush', { method: 'POST' });
      notify(`Flushed: ${c.sent || 0} sent, ${c.failed || 0} failed.`); await load();
    } catch (e) { notify(e.message, 'err'); } finally { setBusy(false); }
  };
  const resend = async (id) => {
    try { await api(`/admin/notifications/${id}/resend`, { method: 'POST' }); notify('Re-queued.'); await load(); }
    catch (e) { notify(e.message, 'err'); }
  };
  const sendTest = async () => {
    try { const r = await api('/admin/notifications/test', { method: 'POST', body: JSON.stringify({ to_address: testTo }) });
      notify(r.sent ? 'Test sent.' : 'Test recorded (check transport).', r.sent ? 'ok' : 'err'); setTestTo(''); await load();
    } catch (e) { notify(e.message, 'err'); }
  };

  return html`
    <div>
      <div class="card" style=${sx('margin-bottom:18px')}>
        <div class="card-head"><div><h3>Outbox</h3>
          <div class="desc">Transport: ${meta?.transport || '—'}</div></div>
          <div class="btn-row">
            <input type="text" placeholder="test@example.com" value=${testTo}
              onInput=${(e) => setTestTo(e.target.value)} style=${sx('min-width:170px')} />
            <button class="btn btn-sm" disabled=${!testTo} onClick=${sendTest}>Send test</button>
            <button class="btn btn-sm btn-primary" disabled=${busy} onClick=${flush}>Flush queue</button>
          </div>
        </div>
        <div class="card-body">
          <div class="filters">
            <select value=${filters.status} onChange=${(e) => setFilters({ ...filters, status: e.target.value })}>
              <option value="">Any status</option><option value="queued">queued</option>
              <option value="sent">sent</option><option value="failed">failed</option><option value="skipped">skipped</option>
            </select>
            <select value=${filters.event} onChange=${(e) => setFilters({ ...filters, event: e.target.value })}>
              <option value="">Any event</option>
              ${NOTIF_EVENTS.map((ev) => html`<option key=${ev} value=${ev}>${ev}</option>`)}
            </select>
            <input type="text" placeholder="Domain" value=${filters.domain}
              onInput=${(e) => setFilters({ ...filters, domain: e.target.value })} />
          </div>
          ${!rows ? html`<${Spinner} />`
          : rows.length === 0 ? html`<${Empty} title="Outbox empty"><//>`
          : html`<div class="table-scroll"><table>
              <thead><tr><th>When</th><th>Event</th><th>Domain</th><th>To</th><th>Subject</th><th>Status</th><th></th></tr></thead>
              <tbody>${rows.map((n) => html`
                <tr key=${n.id}>
                  <td class="small nowrap">${fmtDate(n.created_at)}</td>
                  <td>${statusPill(n.event)}</td>
                  <td class="small">${n.domain || '—'}</td>
                  <td class="small mono">${(n.to_addresses || []).join(', ') || '—'}</td>
                  <td class="small">${n.subject || '—'}${n.error ? html`<div class="small" style=${sx('color:var(--err)')}>${n.error}</div>` : null}</td>
                  <td>${statusPill(n.status === 'sent' ? 'approved' : n.status === 'failed' ? 'error' : n.status)}</td>
                  <td class="right">${(n.to_addresses || []).length ? html`<button class="btn btn-sm" onClick=${() => resend(n.id)}>Resend</button>` : null}</td>
                </tr>`)}
              </tbody></table></div>`}
        </div>
      </div>
      <${AdminNotificationTemplates} />
    </div>`;
}

function AdminNotificationTemplates() {
  const [data, setData] = useState(null);
  const [editing, setEditing] = useState(null); // template | 'new'
  const load = useCallback(() => { api('/admin/notification-templates').then(setData).catch(() => setData({ data: [], defaults: {} })); }, []);
  useEffect(load, [load]);

  const del = async (id) => {
    try { await api(`/admin/notification-templates/${id}`, { method: 'DELETE' }); notify('Template deleted.'); load(); }
    catch (e) { notify(e.message, 'err'); }
  };

  return html`
    <div class="card">
      <div class="card-head"><div><h3>Templates</h3>
        <div class="desc">Per-domain (or global) subject/body overrides</div></div>
        <button class="btn btn-sm btn-primary" onClick=${() => setEditing('new')}>+ New template</button></div>
      <div class="card-body flush">
        ${!data ? html`<${Spinner} />`
        : (data.data || []).length === 0 ? html`<${Empty} title="No custom templates">Defaults are used for every event.<//>`
        : html`<table><thead><tr><th>Event</th><th>Domain</th><th>Subject</th><th>Enabled</th><th></th></tr></thead>
            <tbody>${data.data.map((t) => html`
              <tr key=${t.id}>
                <td>${statusPill(t.event)}</td>
                <td class="small">${t.domain || html`<span class="muted">global</span>`}</td>
                <td class="small">${t.subject}</td>
                <td>${t.enabled ? 'yes' : 'no'}</td>
                <td class="right nowrap"><div class="btn-row" style=${sx('justify-content:flex-end')}>
                  <button class="btn btn-sm" onClick=${() => setEditing(t)}>Edit</button>
                  <button class="btn btn-sm btn-danger" onClick=${() => del(t.id)}>Delete</button>
                </div></td>
              </tr>`)}
            </tbody></table>`}
      </div>
      ${editing && html`<${NotificationTemplateForm} template=${editing === 'new' ? null : editing}
        defaults=${data?.defaults || {}} onClose=${() => setEditing(null)}
        onSaved=${() => { setEditing(null); load(); }} />`}
    </div>`;
}

function NotificationTemplateForm({ template, defaults, onClose, onSaved }) {
  const editing = !!template;
  const [f, setF] = useState({
    event: template?.event || 'submitted', domain: template?.domain || '',
    subject: template?.subject || '', body: template?.body || '',
    recipients: (template?.recipients || []).join(', '), enabled: template?.enabled ?? true,
  });
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));

  const applyDefault = () => {
    const d = defaults[f.event];
    if (d) setF((s) => ({ ...s, subject: d.subject, body: d.body }));
  };

  const save = async () => {
    setBusy(true); setErr(null);
    const body = {
      event: f.event, domain: f.domain || null, subject: f.subject, body: f.body,
      recipients: f.recipients.split(',').map((x) => x.trim()).filter(Boolean), enabled: f.enabled,
    };
    try {
      if (editing) await api(`/admin/notification-templates/${template.id}`, { method: 'PUT', body: JSON.stringify(body) });
      else await api('/admin/notification-templates', { method: 'POST', body: JSON.stringify(body) });
      notify('Template saved.'); onSaved();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  return html`
    <${Modal} wide title=${editing ? 'Edit template' : 'New template'} onClose=${onClose}
      footer=${html`<${Fragment}>
        <button class="btn" onClick=${onClose}>Cancel</button>
        <button class="btn btn-primary" disabled=${busy || !f.subject || !f.body} onClick=${save}>Save</button>
      <//>`}>
      ${err && html`<${Banner} kind="err">${err}<//>`}
      <div class="grid grid-2">
        <label class="field"><span>Event</span>
          <select value=${f.event} onChange=${(e) => set('event', e.target.value)}>
            ${NOTIF_EVENTS.map((ev) => html`<option key=${ev} value=${ev}>${ev}</option>`)}
          </select></label>
        <label class="field"><span>Domain <span class="hint">— blank = global default</span></span>
          <input type="text" value=${f.domain} onInput=${(e) => set('domain', e.target.value)} /></label>
      </div>
      <label class="field"><span>Subject <button class="btn btn-sm" style=${sx('margin-left:8px')} onClick=${applyDefault}>Load default</button></span>
        <input type="text" value=${f.subject} onInput=${(e) => set('subject', e.target.value)} /></label>
      <label class="field"><span>Body <span class="hint">— {entity} {actor} {comment} {deep_link} …</span></span>
        <textarea class="mono" rows="5" value=${f.body} onInput=${(e) => set('body', e.target.value)}></textarea></label>
      <label class="field"><span>Recipients <span class="hint">— comma-separated; blank = resolve by role</span></span>
        <input type="text" value=${f.recipients} onInput=${(e) => set('recipients', e.target.value)} /></label>
      <label class="check"><input type="checkbox" checked=${f.enabled} onChange=${(e) => set('enabled', e.target.checked)} /> Enabled</label>
    <//>`;
}

/* -------------------------------------------------------- admin: distribution */
function AdminDistribution() {
  const [dist, setDist] = useState(null);
  const [sched, setSched] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api('/admin/distribution').then(setDist).catch((e) => setErr(e.message));
    api('/admin/scheduler').then(setSched).catch(() => {});
  }, []);
  useEffect(load, [load]);

  const refresh = async (entity) => {
    setBusy(true);
    try { const r = await api(`/admin/distribution/refresh${entity ? `?entity=${entity}` : ''}`, { method: 'POST' });
      notify(`Refreshed ${r.count ?? ''} view(s).`); load();
    } catch (e) { notify(e.message, 'err'); } finally { setBusy(false); }
  };
  const retention = async () => {
    setBusy(true);
    try { await api('/admin/retention/run', { method: 'POST' }); notify('Retention run complete.'); }
    catch (e) { notify(e.message, 'err'); } finally { setBusy(false); }
  };

  if (err) return html`<${Banner} kind="err">${err}<//>`;
  if (!dist) return html`<${Spinner} />`;
  return html`
    <div class="grid grid-2">
      <div class="card">
        <div class="card-head"><div><h3>Distribution views</h3>
          <div class="desc">Materialized views in ${dist.schema}</div></div>
          <div class="btn-row">
            <button class="btn btn-sm" disabled=${busy} onClick=${retention}>Run retention</button>
            <button class="btn btn-sm btn-primary" disabled=${busy} onClick=${() => refresh()}>Refresh all</button>
          </div>
        </div>
        <div class="card-body flush">
          ${(dist.entities || []).length === 0 ? html`<${Empty} title="No published entities"><//>`
          : html`<table><thead><tr><th>Entity</th><th>Matview</th><th>Exists</th><th>Populated</th><th></th></tr></thead>
              <tbody>${dist.entities.map((e) => html`
                <tr key=${e.entity}>
                  <td class="mono small">${e.entity}</td>
                  <td class="mono small">${e.matview}</td>
                  <td>${e.exists ? html`<${Pill} kind="ok">yes<//>` : html`<${Pill} kind="mute">no<//>`}</td>
                  <td>${e.populated ? html`<${Pill} kind="ok">yes<//>` : html`<${Pill} kind="warn">no<//>`}</td>
                  <td class="right"><button class="btn btn-sm" disabled=${busy} onClick=${() => refresh(e.entity)}>Refresh</button></td>
                </tr>`)}
              </tbody></table>`}
        </div>
      </div>
      <div class="card">
        <div class="card-head"><div><h3>Scheduler</h3><div class="desc">Background job status</div></div></div>
        <div class="card-body">
          ${!sched ? html`<${Spinner} />` : html`<dl class="kv">
            <dt>Enabled</dt><dd>${sched.enabled ? 'yes' : 'no'}</dd>
            <dt>Running</dt><dd>${sched.running ? html`<${Pill} kind="ok">running<//>` : html`<${Pill} kind="mute">stopped<//>`}</dd>
            <dt>Retention</dt><dd>${sched.retention_enabled ? 'enabled' : 'disabled'}</dd>
            <dt>View refresh</dt><dd>${sched.intervals?.view_refresh_seconds}s</dd>
            <dt>Retention interval</dt><dd>${sched.intervals?.retention_seconds}s</dd>
          </dl>
          ${sched.last_runs && Object.keys(sched.last_runs).length ? html`
            <table style=${sx('margin-top:10px')}><thead><tr><th>Job</th><th>Last run</th></tr></thead>
              <tbody>${Object.entries(sched.last_runs).map(([j, v]) => html`
                <tr key=${j}><td class="mono small">${j}</td><td class="small">${fmtDate(typeof v === 'object' ? v.at || v.last_run : v)}</td></tr>`)}
              </tbody></table>` : null}`}
        </div>
      </div>
    </div>`;
}

/* ------------------------------------------------------- admin: field mappings */
function AdminFieldMappings() {
  const [rows, setRows] = useState(null);
  const [editing, setEditing] = useState(null); // mapping | 'new'
  const load = useCallback(() => { api('/admin/field-mappings').then(setRows).catch(() => setRows([])); }, []);
  useEffect(load, [load]);

  const del = async (id) => {
    try { await api(`/admin/field-mappings/${id}`, { method: 'DELETE' }); notify('Mapping deleted.'); load(); }
    catch (e) { notify(e.message, 'err'); }
  };

  return html`
    <div class="card">
      <div class="card-head"><div><h3>Field mappings</h3>
        <div class="desc">Rename source fields to target columns during promotion</div></div>
        <button class="btn btn-sm btn-primary" onClick=${() => setEditing('new')}>+ New mapping</button></div>
      <div class="card-body flush">
        ${!rows ? html`<${Spinner} />`
        : rows.length === 0 ? html`<${Empty} title="No field mappings"><//>`
        : html`<div class="table-scroll"><table>
            <thead><tr><th>Entity</th><th>Source system</th><th>Source field</th><th>Target field</th><th>Enabled</th><th></th></tr></thead>
            <tbody>${rows.map((m) => html`
              <tr key=${m.id}>
                <td class="mono small">${m.entity_name}</td>
                <td class="small">${m.source_system || html`<span class="muted">any</span>`}</td>
                <td class="mono small">${m.source_field}</td>
                <td class="mono small">${m.target_field}</td>
                <td>${m.enabled ? 'yes' : 'no'}</td>
                <td class="right nowrap"><div class="btn-row" style=${sx('justify-content:flex-end')}>
                  <button class="btn btn-sm" onClick=${() => setEditing(m)}>Edit</button>
                  <button class="btn btn-sm btn-danger" onClick=${() => del(m.id)}>Delete</button>
                </div></td>
              </tr>`)}
            </tbody></table></div>`}
      </div>
      ${editing && html`<${FieldMappingForm} mapping=${editing === 'new' ? null : editing}
        onClose=${() => setEditing(null)} onSaved=${() => { setEditing(null); load(); }} />`}
    </div>`;
}

function FieldMappingForm({ mapping, onClose, onSaved }) {
  const editing = !!mapping;
  const [f, setF] = useState({
    entity_name: mapping?.entity_name || '', source_system: mapping?.source_system || '',
    source_field: mapping?.source_field || '', target_field: mapping?.target_field || '',
    default_value: mapping?.default_value || '', enabled: mapping?.enabled ?? true,
    transform: mapping?.transform ? JSON.stringify(mapping.transform) : '',
  });
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));

  const save = async () => {
    setBusy(true); setErr(null);
    let transform = null;
    if (f.transform.trim()) {
      try { transform = JSON.parse(f.transform); } catch { transform = f.transform.trim(); }
    }
    const body = {
      entity_name: f.entity_name, source_system: f.source_system || null,
      source_field: f.source_field, target_field: f.target_field,
      default_value: f.default_value || null, enabled: f.enabled, transform,
    };
    try {
      if (editing) await api(`/admin/field-mappings/${mapping.id}`, { method: 'PUT', body: JSON.stringify(body) });
      else await api('/admin/field-mappings', { method: 'POST', body: JSON.stringify(body) });
      notify('Mapping saved.'); onSaved();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  return html`
    <${Modal} title=${editing ? 'Edit mapping' : 'New field mapping'} onClose=${onClose}
      footer=${html`<${Fragment}>
        <button class="btn" onClick=${onClose}>Cancel</button>
        <button class="btn btn-primary" disabled=${busy || !f.entity_name || !f.source_field || !f.target_field} onClick=${save}>Save</button>
      <//>`}>
      ${err && html`<${Banner} kind="err">${err}<//>`}
      <div class="grid grid-2">
        <label class="field"><span>Entity name</span>
          <input type="text" class="mono" value=${f.entity_name} onInput=${(e) => set('entity_name', e.target.value)} /></label>
        <label class="field"><span>Source system <span class="hint">— blank = all</span></span>
          <input type="text" value=${f.source_system} onInput=${(e) => set('source_system', e.target.value)} /></label>
        <label class="field"><span>Source field</span>
          <input type="text" class="mono" value=${f.source_field} onInput=${(e) => set('source_field', e.target.value)} /></label>
        <label class="field"><span>Target field</span>
          <input type="text" class="mono" value=${f.target_field} onInput=${(e) => set('target_field', e.target.value)} /></label>
      </div>
      <label class="field"><span>Transform <span class="hint">— name or JSON spec</span></span>
        <input type="text" class="mono" value=${f.transform} onInput=${(e) => set('transform', e.target.value)} placeholder="upper" /></label>
      <label class="field"><span>Default value</span>
        <input type="text" value=${f.default_value} onInput=${(e) => set('default_value', e.target.value)} /></label>
      <label class="check"><input type="checkbox" checked=${f.enabled} onChange=${(e) => set('enabled', e.target.checked)} /> Enabled</label>
    <//>`;
}

function AdminAudit() {
  const [rows, setRows] = useState(null);
  const [filters, setFilters] = useState({ entity_name: '', actor: '', action: '' });
  const load = useCallback(async () => {
    const qs = new URLSearchParams({ limit: 150 });
    Object.entries(filters).forEach(([k, v]) => v && qs.set(k, v));
    try { setRows(await api(`/admin/audit?${qs}`)); } catch (e) { notify(e.message, 'err'); }
  }, [filters]);
  useEffect(() => { load(); }, [load]);

  return html`
    <div class="card">
      <div class="card-head"><div><h3>Audit log</h3>
        <div class="desc">Every consequential action, append-only</div></div></div>
      <div class="card-body">
        <div class="filters">
          <input type="text" placeholder="Entity" value=${filters.entity_name}
            onInput=${(e) => setFilters({ ...filters, entity_name: e.target.value })} />
          <input type="text" placeholder="Actor" value=${filters.actor}
            onInput=${(e) => setFilters({ ...filters, actor: e.target.value })} />
          <input type="text" placeholder="Action" value=${filters.action}
            onInput=${(e) => setFilters({ ...filters, action: e.target.value })} />
          <button class="btn btn-sm" onClick=${load}>Apply</button>
        </div>
        ${!rows ? html`<${Spinner} />`
        : rows.data.length === 0 ? html`<${Empty} title="No matching events"><//>`
        : html`<div class="table-scroll"><table>
            <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Entity</th><th>Tier</th><th>Detail</th></tr></thead>
            <tbody>${rows.data.map((e) => html`
              <tr key=${e.id}>
                <td class="small nowrap">${fmtDate(e.occurred_at)}</td>
                <td class="small">${e.actor || '—'}
                  ${(e.actor_roles || []).length ? html`<div class="small muted mono">${e.actor_roles.join(',')}</div>` : null}</td>
                <td><${Pill} kind=${e.success === false ? 'err' : e.action.includes('approve') ? 'ok'
                  : e.action.includes('reject') || e.action.includes('delete') ? 'err' : 'info'}>${e.action}<//></td>
                <td class="small mono">${e.entity_name || '—'}</td>
                <td>${e.tier ? html`<span class="tier-tag">${e.tier}</span>` : '—'}</td>
                <td class="small mono" style=${sx('max-width:340px;overflow:hidden;text-overflow:ellipsis')}>
                  ${e.detail && Object.keys(e.detail).length ? JSON.stringify(e.detail) : '—'}</td>
              </tr>`)}
            </tbody></table></div>`}
      </div>
    </div>`;
}

function AdminBatches() {
  const [rows, setRows] = useState(null);
  useEffect(() => { api('/stewardship/batches').then(setRows).catch(() => setRows([])); }, []);
  return html`
    <div class="card">
      <div class="card-head"><div><h3>Pipeline runs</h3>
        <div class="desc">Landing → staging promotion history</div></div></div>
      <div class="card-body flush">
        ${!rows ? html`<${Spinner} />`
        : rows.length === 0 ? html`<${Empty} title="No pipeline runs yet"><//>`
        : html`<table>
            <thead><tr><th>Started</th><th>Entity</th><th>Stage</th><th class="right">In</th>
              <th class="right">OK</th><th class="right">Failed</th><th>Status</th><th>By</th></tr></thead>
            <tbody>${rows.map((b) => html`
              <tr key=${b.id}>
                <td class="small nowrap">${fmtDate(b.started_at)}</td>
                <td class="mono small">${b.entity}</td>
                <td class="small">${b.stage}</td>
                <td class="num">${b.rows_in}</td>
                <td class="num">${b.rows_ok}</td>
                <td class="num">${b.rows_failed ? html`<${Pill} kind="err">${b.rows_failed}<//>` : '0'}</td>
                <td>${statusPill(b.status === 'completed' ? 'approved' : b.status)}</td>
                <td class="small">${b.triggered_by || '—'}</td>
              </tr>`)}
            </tbody></table>`}
      </div>
    </div>`;
}

/* ============================================================ shell */
const VIEWS = {
  dashboard: { label: 'Overview', title: 'Overview', crumb: 'Pipeline health and pending work' },
  inbox: { label: 'My inbox', title: 'My inbox', crumb: 'Change requests assigned to you and the unassigned pool' },
  models: { label: 'Data models', title: 'Data models', crumb: 'Define entities and publish physical tables' },
  review: { label: 'Review queue', title: 'Stewardship', crumb: 'Review, correct and approve staged changes' },
  records: { label: 'Golden records', title: 'Golden records', crumb: 'Approved, versioned master data' },
  admin: { label: 'Administration', title: 'Administration', crumb: 'Users, directory, keys and audit' },
};

/* URL <-> view mapping, so deep links, refresh and the back button all work. */
const ROUTE_TO_VIEW = {
  '': 'dashboard', 'dashboard': 'dashboard', 'inbox': 'inbox', 'models': 'models',
  'review': 'review', 'records': 'records', 'admin': 'admin',
};

function routeFromLocation() {
  const segs = location.pathname.split('/').filter(Boolean);
  const view = ROUTE_TO_VIEW[segs[0] || ''] || 'dashboard';
  // A second segment names an entity, e.g. /review/vendor
  const params = segs[1] ? { entity: decodeURIComponent(segs[1]) } : {};
  return { view, params };
}

function App() {
  const [me, setMe] = useState(null);
  const [booting, setBooting] = useState(true);
  const initial = routeFromLocation();
  const [view, setView] = useState(initial.view);
  const [params, setParams] = useState(initial.params);
  const [pending, setPending] = useState(0);
  const [inboxCounts, setInboxCounts] = useState({});

  useEffect(() => {
    api('/auth/me').then(setMe).catch(() => setMe(null)).finally(() => setBooting(false));
  }, []);

  // Keep the address bar in step with in-app navigation.
  useEffect(() => {
    const onPop = () => {
      const r = routeFromLocation();
      setView(r.view); setParams(r.params);
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  const refreshPending = useCallback(() => {
    if (!me) return;
    api('/stewardship/queue').then((q) => setPending(q.total_pending || 0)).catch(() => {});
    api('/stewardship/inbox/counts').then(setInboxCounts).catch(() => setInboxCounts({}));
  }, [me]);
  useEffect(() => { refreshPending(); }, [refreshPending]);
  useEffect(() => {
    if (!me) return;
    const t = setInterval(refreshPending, 30000);
    return () => clearInterval(t);
  }, [me, refreshPending]);

  const go = (v, p = {}) => {
    setView(v); setParams(p);
    const path = '/' + (v === 'dashboard' ? '' : v) + (p.entity ? `/${encodeURIComponent(p.entity)}` : '');
    if (location.pathname !== path) history.pushState({}, '', path);
  };

  const signOut = async () => {
    try { await api('/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
    setMe(null); setView('dashboard');
  };

  if (booting) return html`<div class="boot">
    <div class="boot-mark">MDM</div><div class="boot-text">Loading workspace…</div></div>`;
  if (!me) return html`<${Fragment}><${Login} onSignedIn=${() => {
    api('/auth/me').then(setMe);
  }} /><${Toasts} /><//>`;

  const nav = ['dashboard', 'inbox', 'review', 'records', 'models'];
  if (me.is_admin) nav.push('admin');
  const navBadge = (v) => {
    if (v === 'review' && pending > 0) return pending;
    if (v === 'inbox') {
      const n = (inboxCounts.assigned_to_me || 0) + (inboxCounts.changes_requested || 0);
      return n > 0 ? n : null;
    }
    return null;
  };

  return html`
    <div class="shell">
      <aside class="side">
        <div class="side-head">
          <div class="logo">MDM PLATFORM</div>
          <div class="env">master data management</div>
        </div>
        <nav>
          <div class="nav-label">Workspace</div>
          ${nav.map((v) => html`
            <button class="nav-item ${view === v ? 'active' : ''}" key=${v} onClick=${() => go(v)}>
              <span>${VIEWS[v].label}</span>
              ${navBadge(v) ? html`<span class="nav-count hot">${navBadge(v)}</span>` : null}
            </button>`)}
          <div class="nav-label">Reference</div>
          <a class="nav-item" href="/api/docs" target="_blank" rel="noopener noreferrer">
            <span>API documentation</span><span class="nav-count">↗</span></a>
        </nav>
        <div class="side-foot">
          <div class="who">${me.display_name || me.username}</div>
          <div class="roles">${
            (me.roles || []).join(' · ')
            || Object.entries(me.domain_roles || {}).map(([d, rs]) => `${(rs || []).join('/')} @ ${d}`).join(' · ')
            || 'no roles'
          }</div>
          <button class="signout" onClick=${signOut}>Sign out</button>
        </div>
      </aside>
      <div class="main">
        <div class="topbar">
          <div>
            <h1>${VIEWS[view].title}</h1>
            <div class="crumb">${VIEWS[view].crumb}</div>
          </div>
        </div>
        <div class="content">
          ${view === 'dashboard' && html`<${Dashboard} me=${me} go=${go} />`}
          ${view === 'inbox' && html`<${InboxView} me=${me} go=${go} />`}
          ${view === 'models' && html`<${ModelsView} me=${me} params=${params} go=${go} />`}
          ${view === 'review' && html`<${ReviewView} me=${me} params=${params} go=${go} />`}
          ${view === 'records' && html`<${RecordsView} me=${me} />`}
          ${view === 'admin' && html`<${AdminView} me=${me} />`}
        </div>
      </div>
      <${Toasts} />
    </div>`;
}

ReactDOM.createRoot(document.getElementById('root')).render(
  html`<${ErrorBoundary}><${App} /><//>`
);
