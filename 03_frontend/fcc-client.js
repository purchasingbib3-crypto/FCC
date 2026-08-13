// =============================================================================
// FCC PPA-BIB — Custom client shim menggantikan Supabase JS client
// RPC fuel_public_staged_nrp_lookup juga lookup dari fcc.app_user (gabungan dashboard + lapangan)
// =============================================================================
// Tujuan: app.js lapangan jalan tanpa Supabase, pakai endpoint VPS lokal.
// Endpoint base: /api/fuel/  (di-handle oleh FastAPI fuel_bridge.py)
//
// Usage: const sb = createFCCClient();
//   sb.from('table').select().eq().order()  →  fetch list
//   sb.from('table').insert(payload).select().single()
//   sb.from('table').update(payload).eq('id', id).select().single()
//   sb.from('table').upsert(payload, { onConflict: 'col' })
//   sb.rpc('func_name', { args })
//   sb.auth.signInWithPassword({ email, password })
//   sb.auth.signUp({ email, password, options: { data: {...} } })
//   sb.auth.signOut()
//   sb.auth.getSession()
//   sb.auth.updateUser({ password })
//   sb.storage.from(bucket).upload(path, blob, { contentType, upsert })
// =============================================================================

const FCC_BASE = (window.FCC_API_BASE || '/api/fuel');

// ============================================================================
// HTTP helpers
// ============================================================================
async function http(method, path, opts = {}) {
  // Absolute URL (http/https) → langsung
  // Absolute path /api/* → langsung (untuk auth endpoint di luar /api/fuel)
  // Relative path → prefix dengan FCC_BASE
  let url;
  if (/^https?:\/\//i.test(path)) {
    url = path;
  } else if (path.startsWith('/api/')) {
    url = path;
  } else {
    url = FCC_BASE + path;
  }
  const init = {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
  };
  if (opts.body !== undefined && opts.body !== null) {
    init.body = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body);
  }
  const r = await fetch(url, init);
  let data = null;
  const text = await r.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!r.ok) {
    const msg = (data && data.detail) || (data && data.error && data.error.message) || r.statusText;
    const err = new Error(msg);
    err.status = r.status;
    err.data = data;
    throw err;
  }
  return data;
}

function buildQuery(params) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) qs.set(k, v);
  }
  const s = qs.toString();
  return s ? '?' + s : '';
}

// ============================================================================
// QueryBuilder — chainable, emulate sb.from('t').select().eq().order().limit()
// ============================================================================
class QueryBuilder {
  constructor(table, mode) {
    this.table = table;
    this.mode = mode;                  // 'select' | 'insert' | 'update' | 'upsert' | 'delete'
    this.payload = undefined;
    this.upsertOnConflict = null;
    this.selectCols = null;
    this.singleMode = false;
    this.filters = [];                 // [{op, col, val}]
    this.orderList = [];
    this.limitN = null;
    this.offsetN = null;
  }

  select(cols) {
    if (typeof cols === 'string') this.selectCols = cols.split(',').map(s => s.trim());
    return this;
  }

  single() {
    this.singleMode = true;
    return this;
  }

  maybeSingle() {
    this.singleMode = true;
    return this;
  }

  insert(payload) {
    this.mode = 'insert';
    this.payload = payload;
    return this;
  }

  update(payload) {
    this.mode = 'update';
    this.payload = payload;
    return this;
  }

  upsert(payload, opts = {}) {
    this.mode = 'upsert';
    this.payload = payload;
    if (opts.onConflict) this.upsertOnConflict = opts.onConflict;
    return this;
  }

  delete() {
    this.mode = 'delete';
    return this;
  }

  eq(col, val)      { this.filters.push({ op: 'eq', col, val }); return this; }
  neq(col, val)     { this.filters.push({ op: 'neq', col, val }); return this; }
  gt(col, val)      { this.filters.push({ op: 'gt', col, val }); return this; }
  gte(col, val)     { this.filters.push({ op: 'gte', col, val }); return this; }
  lt(col, val)      { this.filters.push({ op: 'lt', col, val }); return this; }
  lte(col, val)     { this.filters.push({ op: 'lte', col, val }); return this; }
  like(col, val)    { this.filters.push({ op: 'like', col, val }); return this; }
  ilike(col, val)   { this.filters.push({ op: 'ilike', col, val }); return this; }
  in(col, vals)     { this.filters.push({ op: 'in', col, val: vals.join(',') }); return this; }
  is(col, val)      { this.filters.push({ op: 'is', col, val }); return this; }

  order(col, opts = {}) {
    const ascending = opts.ascending !== false;
    this.orderList.push(`${col}.${ascending ? 'asc' : 'desc'}`);
    return this;
  }

  limit(n)  { this.limitN = n;  return this; }
  offset(n) { this.offsetN = n; return this; }

  _filterQS() {
    const qs = {};
    for (const f of this.filters) qs[`${f.op}.${f.col}`] = f.val;
    return qs;
  }

  async then(resolve, reject) {
    try {
      const data = await this._exec();
      resolve({ data, error: null });
    } catch (error) {
      resolve({ data: null, error });
    }
  }

  async _exec() {
    if (this.mode === 'select') {
      const qs = this._filterQS();
      if (this.selectCols) qs.select = this.selectCols.join(',');
      if (this.orderList.length) qs.order = this.orderList.join(',');
      if (this.limitN !== null) qs.limit = this.limitN;
      if (this.offsetN !== null) qs.offset = this.offsetN;
      const data = await http('GET', `/${this.table}${buildQuery(qs)}`);
      return this.singleMode ? (data[0] || null) : data;
    }
    if (this.mode === 'insert') {
      const qs = this.selectCols ? { select: this.selectCols.join(',') } : {};
      const data = await http('POST', `/${this.table}${buildQuery(qs)}`, { body: this.payload });
      return this.singleMode ? data : (Array.isArray(data) ? data : [data]);
    }
    if (this.mode === 'update') {
      const qs = this._filterQS();
      if (this.selectCols) qs.select = this.selectCols.join(',');
      const data = await http('PATCH', `/${this.table}${buildQuery(qs)}`, { body: this.payload });
      return this.singleMode ? (data[0] || null) : data;
    }
    if (this.mode === 'upsert') {
      const qs = this.upsertOnConflict ? { onConflict: this.upsertOnConflict } : {};
      const data = await http('POST', `/${this.table}/upsert${buildQuery(qs)}`, { body: this.payload });
      return this.singleMode ? (data[0] || data) : data;
    }
    if (this.mode === 'delete') {
      const qs = this._filterQS();
      const data = await http('DELETE', `/${this.table}${buildQuery(qs)}`);
      return data;
    }
    throw new Error(`QueryBuilder mode tidak dikenal: ${this.mode}`);
  }
}

// ============================================================================
// Auth shim — emulate sb.auth.* pakai cookie session backend
// Base endpoint = /api (parent of /api/fuel). Auth endpoints live outside /fuel
// ============================================================================
class AuthShim {
  constructor() {
    this._session = null;
    this._base = '/api';  // auth endpoint at /api/auth/*
  }

  async signInWithPassword({ email, password }) {
    const username = email.split('@')[0];
    try {
      const r = await http('POST', `${this._base}/auth/login`, {
        body: { username, password },
      });
      this._session = { user: r.user, access_token: 'cookie' };
      return { data: { session: this._session, user: r.user }, error: null };
    } catch (error) {
      return { data: { session: null, user: null }, error };
    }
  }

  async signUp({ email, password, options }) {
    try {
      const username = email.split('@')[0];
      const fullName = options?.data?.full_name || username;
      const r = await http('POST', `${this._base}/auth/register`, {
        body: { username, password, full_name: fullName },
      });
      return { data: { user: r.user }, error: null };
    } catch (error) {
      // 409 = user sudah ada (mis. dashboard user yang belum pernah ke lapangan)
      // Fallback: coba login dengan password yang sama
      if (error.status === 409) {
        const username = email.split('@')[0];
        try {
          const r = await http('POST', `${this._base}/auth/login`, {
            body: { username, password },
          });
          return { data: { user: r.user, session: { user: r.user, access_token: 'cookie' } }, error: null };
        } catch (loginErr) {
          return { data: { user: null }, error: new Error(`User "${username}" sudah ada tapi login gagal: ${loginErr.message}`) };
        }
      }
      return { data: { user: null }, error };
    }
  }

  async signOut() {
    try {
      await http('POST', `${this._base}/auth/logout`, {});
      this._session = null;
      return { error: null };
    } catch (error) {
      return { error };
    }
  }

  async getSession() {
    try {
      const r = await http('GET', `${this._base}/auth/me`, {});
      this._session = { user: r.user, access_token: 'cookie' };
      return { data: { session: this._session }, error: null };
    } catch (error) {
      return { data: { session: null }, error };
    }
  }

  async getUser() {
    const s = await this.getSession();
    return { data: { user: s.data.session?.user || null }, error: s.error };
  }

  async updateUser({ password }) {
    try {
      await http('POST', `${this._base}/auth/change_password`, { body: { new_password: password } });
      return { data: { user: this._session?.user }, error: null };
    } catch (error) {
      return { data: null, error };
    }
  }
}

// ============================================================================
// Storage shim — emulate sb.storage.from(bucket).upload()
// ============================================================================
class StorageBucket {
  constructor(name) { this.name = name; }

  async upload(path, blob, opts = {}) {
    const contentType = opts.contentType || blob.type || 'application/octet-stream';
    const buffer = await blob.arrayBuffer();
    const r = await fetch(`${FCC_BASE}/storage/${this.name}/${path}`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': contentType },
      body: buffer,
    });
    if (!r.ok) {
      const txt = await r.text();
      const err = new Error(`Upload gagal: ${r.status} ${txt}`);
      err.status = r.status;
      throw err;
    }
    return await r.json();
  }
}

class StorageShim {
  from(name) { return new StorageBucket(name); }
}

// ============================================================================
// Factory
// ============================================================================
function createFCCClient() {
  return {
    from: (table) => new QueryBuilder(table, 'select'),
    rpc: async (name, args = {}) => {
      try {
        let data = await http('POST', `/rpc/${name}`, { body: args });
        // Supabase RPC balikin single object untuk single-row functions;
        // backend kita balikin array. Kalau length 1, unwrap.
        if (Array.isArray(data) && data.length === 1) data = data[0];
        return { data, error: null };
      } catch (error) {
        return { data: null, error };
      }
    },
    auth: new AuthShim(),
    storage: new StorageShim(),
  };
}

// Globals (dipakai app.js sebagai `sb`)
globalThis.FCC_BASE = FCC_BASE;
globalThis.createFCCClient = createFCCClient;