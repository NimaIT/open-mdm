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
  'date', 'timestamp', 'uuid', 'json', 'email', 'url', 'enum'];

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
function AttributeEditor({ attrs, setAttrs }) {
  const update = (i, patch) => setAttrs(attrs.map((a, idx) => (idx === i ? { ...a, ...patch } : a)));
  const remove = (i) => setAttrs(attrs.filter((_, idx) => idx !== i));
  const add = () => setAttrs([...attrs, {
    name: '', data_type: 'string', length: 255, is_required: false, is_unique: false,
    is_business_key: false, is_match_key: false, is_indexed: false, validation: {}, normalization: [],
  }]);

  return html`
    <div>
      <div class="attr-editor">
        <div class="attr-row attr-head">
          <div>Column name</div><div>Type</div><div>Len</div><div>Flags</div><div></div>
        </div>
        ${attrs.map((a, i) => html`
          <div class="attr-row" key=${i}>
            <input type="text" class="mono" value=${a.name} placeholder="column_name"
              onInput=${(e) => update(i, { name: e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_') })} />
            <select value=${a.data_type} onChange=${(e) => update(i, { data_type: e.target.value })}>
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
            <button class="btn btn-sm btn-danger" onClick=${() => remove(i)} title="Remove attribute">×</button>
          </div>`)}
      </div>
      <div class="btn-row" style=${sx('margin-top:11px')}>
        <button class="btn btn-sm" onClick=${add}>+ Add attribute</button>
        <span class="small muted">
          Business key resolves updates to existing records. Match key drives duplicate detection.
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
      soft_delete: softDelete,
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
      <${AttributeEditor} attrs=${attrs} setAttrs=${setAttrs} />
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
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [edits, setEdits] = useState({});
  const [note, setNote] = useState('');
  const [rejectReason, setRejectReason] = useState('');
  const [showReject, setShowReject] = useState(false);

  const load = useCallback(async () => {
    try { setD(await api(`/stewardship/${entityName}/staging/${stagingId}`)); }
    catch (e) { setErr(e.message); }
  }, [entityName, stagingId]);
  useEffect(() => { load(); }, [load]);

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

  const approve = async () => {
    setBusy(true); setErr(null);
    try {
      const r = await api(`/stewardship/${entityName}/staging/${stagingId}/approve`, {
        method: 'POST', body: JSON.stringify({ note: note || null }),
      });
      notify(`Approved — golden record ${r.change_type} (v${r.version || 1}).`);
      onActioned(); onClose();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const reject = async () => {
    setBusy(true); setErr(null);
    try {
      await api(`/stewardship/${entityName}/staging/${stagingId}/reject`, {
        method: 'POST', body: JSON.stringify({ reason: rejectReason }),
      });
      notify('Record rejected.', 'ok');
      onActioned(); onClose();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  if (err && !d) return html`<${Modal} title="Review" onClose=${onClose}>
    <${Banner} kind="err">${err}<//><//>`;
  if (!d) return html`<${Modal} title="Review" onClose=${onClose}><${Spinner} /><//>`;

  const s = d.staging;
  const errors = s.mdm_errors || [];
  const supplied = s.mdm_supplied_fields || [];
  const attrNames = Object.keys(s).filter((k) => !k.startsWith('mdm_'));
  const canAct = me.is_steward || me.is_admin;

  return html`
    <${Modal} wide title=${`Review — ${entityName} #${stagingId}`} onClose=${onClose}
      footer=${html`<${Fragment}>
        ${Object.keys(edits).length > 0 && html`
          <button class="btn btn-primary" disabled=${busy} onClick=${saveEdits}>
            ${busy ? html`<span class="spinner"></span>` : null} Save ${Object.keys(edits).length} edit(s)
          </button>`}
        <button class="btn" onClick=${onClose}>Close</button>
        ${canAct && s.mdm_status !== 'applied' && s.mdm_status !== 'rejected' && html`<${Fragment}>
          <button class="btn btn-danger" disabled=${busy} onClick=${() => setShowReject(!showReject)}>Reject</button>
          <button class="btn btn-ok" disabled=${busy || !d.can_approve || Object.keys(edits).length > 0}
            title=${d.blocked_reason || ''} onClick=${approve}>
            ${busy ? html`<span class="spinner"></span>` : null} Approve & apply
          </button>
        <//>`}
      <//>`}>

      ${err && html`<${Banner} kind="err" title="Action failed">${err}<//>`}

      <div class="grid grid-4" style=${sx('margin-bottom:16px')}>
        <div><div class="small muted">Operation</div><div><${Pill} kind="info">${s.mdm_operation}<//></div></div>
        <div><div class="small muted">Resolved as</div><div>${s.mdm_change_type || '—'}</div></div>
        <div><div class="small muted">Status</div><div>${statusPill(s.mdm_status)}</div></div>
        <div><div class="small muted">Source</div><div class="mono small">${s.mdm_source_system || '—'}</div></div>
      </div>

      <div class="small muted" style=${sx('margin-bottom:14px')}>
        Submitted by <strong>${s.mdm_submitted_by || 'unknown'}</strong> ${fmtDate(s.mdm_submitted_at)}
        ${s.mdm_edited_by ? html` · last edited by <strong>${s.mdm_edited_by}</strong> ${fmtDate(s.mdm_edited_at)}` : null}
      </div>

      ${errors.length > 0 && html`
        <${Banner} kind="err" title=${`${errors.length} validation issue(s) — fix before approving`}>
          <ul class="err-list">${errors.map((e, i) => html`
            <li key=${i}><span class="err-code">${e.code}</span>
              <span><strong>${e.field}</strong> — ${e.message}</span></li>`)}
          </ul>
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
        Incoming values ${canAct ? html`<span class="small muted">— editable</span>` : null}
      </h3>
      <div class="table-scroll"><table>
        <thead><tr><th>Field</th><th>Value</th><th>Supplied</th>${d.current_golden_record ? html`<th>Current golden</th>` : null}</tr></thead>
        <tbody>${attrNames.map((f) => html`
          <tr key=${f}>
            <td class="mono small">${f}</td>
            <td>${canAct && s.mdm_status !== 'applied' ? html`
              <input type="text" class="mono"
                value=${edits[f] !== undefined ? edits[f] : (s[f] ?? '')}
                onInput=${(e) => setEdits({ ...edits, [f]: e.target.value })} />`
              : cell(s[f])}</td>
            <td>${supplied.includes(f) ? html`<${Pill} kind="info">sent<//>` : html`<span class="muted small">—</span>`}</td>
            ${d.current_golden_record ? html`<td class="mono small">${cell(d.current_golden_record[f])}</td>` : null}
          </tr>`)}
        </tbody></table></div>

      ${canAct && s.mdm_status !== 'applied' && html`
        <label class="field" style=${sx('margin-top:16px')}><span>Review note <span class="hint">— recorded in the audit trail</span></span>
          <input type="text" value=${note} onInput=${(e) => setNote(e.target.value)}
            placeholder="Verified against source system" /></label>`}

      ${showReject && html`
        <div class="card" style=${sx('margin-top:12px')}><div class="card-body">
          <label class="field"><span>Rejection reason <span class="hint">— required</span></span>
            <input type="text" value=${rejectReason} autoFocus
              onInput=${(e) => setRejectReason(e.target.value)} placeholder="Duplicate of existing record" /></label>
          <button class="btn btn-danger" disabled=${!rejectReason || busy} onClick=${reject}>Confirm rejection</button>
        </div></div>`}
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
            ${selected.length > 0 && (me.is_steward || me.is_admin) && html`
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
  const [data, setData] = useState(null);
  const [search, setSearch] = useState('');
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [history, setHistory] = useState(null);
  const [err, setErr] = useState(null);
  const [offset, setOffset] = useState(0);
  const LIMIT = 25;

  useEffect(() => {
    api('/models').then((m) => {
      const pub = m.filter((x) => x.status === 'published' || x.status === 'modified');
      setModels(pub);
      if (pub.length && !entity) setEntity(pub[0].name);
    }).catch((e) => setErr(e.message));
  }, []);

  const load = useCallback(async () => {
    if (!entity) return;
    try {
      const qs = new URLSearchParams({ limit: LIMIT, offset, include_deleted: includeDeleted });
      if (search) qs.set('q', search);
      setData(await api(`/data/${entity}?${qs}`));
    } catch (e) { setErr(e.message); }
  }, [entity, search, includeDeleted, offset]);
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
      </div>

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
                    <td class="right"><button class="btn btn-sm"
                      onClick=${() => openHistory(r.mdm_id)}>History</button></td>
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
    </div>`;
}

/* ============================================================ admin */
function AdminView({ me }) {
  const [tab, setTab] = useState('system');
  const tabs = [
    ['system', 'System'], ['users', 'Users & roles'], ['ldap', 'LDAP / AD'],
    ['keys', 'API keys'], ['audit', 'Audit log'], ['batches', 'Pipeline runs'],
  ];
  return html`
    <div>
      <div class="tabs">
        ${tabs.map(([k, label]) => html`
          <button class="tab ${tab === k ? 'active' : ''}" key=${k} onClick=${() => setTab(k)}>${label}</button>`)}
      </div>
      ${tab === 'system' && html`<${AdminSystem} />`}
      ${tab === 'users' && html`<${AdminUsers} me=${me} />`}
      ${tab === 'ldap' && html`<${AdminLdap} />`}
      ${tab === 'keys' && html`<${AdminKeys} />`}
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

function AdminUsers({ me }) {
  const [users, setUsers] = useState(null);
  const [roles, setRoles] = useState(null);
  const [err, setErr] = useState(null);

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
                <td class="right">${u.username !== me.username && html`
                  <button class="btn btn-sm" onClick=${() => toggleActive(u)}>
                    ${u.is_active ? 'Disable' : 'Enable'}</button>`}</td>
              </tr>`)}
            </tbody></table></div>
        </div>
      </div>

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
  const [issued, setIssued] = useState(null);

  const load = useCallback(() => { api('/admin/api-keys').then(setKeys).catch(() => setKeys([])); }, []);
  useEffect(load, [load]);

  const create = async () => {
    try {
      const r = await api('/admin/api-keys', {
        method: 'POST', body: JSON.stringify({ name, source_system: source || null, allowed_entities: [] }),
      });
      setIssued(r); setName(''); setSource(''); load();
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
                  <td><strong>${k.name}</strong></td>
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
  models: { label: 'Data models', title: 'Data models', crumb: 'Define entities and publish physical tables' },
  review: { label: 'Review queue', title: 'Stewardship', crumb: 'Review, correct and approve staged changes' },
  records: { label: 'Golden records', title: 'Golden records', crumb: 'Approved, versioned master data' },
  admin: { label: 'Administration', title: 'Administration', crumb: 'Users, directory, keys and audit' },
};

/* URL <-> view mapping, so deep links, refresh and the back button all work. */
const ROUTE_TO_VIEW = {
  '': 'dashboard', 'dashboard': 'dashboard', 'models': 'models',
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

  const nav = ['dashboard', 'review', 'records', 'models'];
  if (me.is_admin) nav.push('admin');

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
              ${v === 'review' && pending > 0 ? html`<span class="nav-count hot">${pending}</span>` : null}
            </button>`)}
          <div class="nav-label">Reference</div>
          <a class="nav-item" href="/api/docs" target="_blank" rel="noopener noreferrer">
            <span>API documentation</span><span class="nav-count">↗</span></a>
        </nav>
        <div class="side-foot">
          <div class="who">${me.display_name || me.username}</div>
          <div class="roles">${(me.roles || []).join(' · ') || 'no roles'}</div>
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
