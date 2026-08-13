// FCC PPA-BIB — replaced Supabase client with local FastAPI bridge (fcc-client.js)
// fcc-client.js loaded as classic script before this module (see index.html)
const sb = (typeof createFCCClient === 'function') ? createFCCClient() : (globalThis.sb || {});
const SITE_CODE = 'PPA-BIB';
const PHOTO_BUCKET = 'fuel-control-photos';

const $ = (id) => document.getElementById(id);
const qsa = (selector) => Array.from(document.querySelectorAll(selector));
const fmt = new Intl.NumberFormat('id-ID', { maximumFractionDigits: 2 });

const state = {
  session: null,
  user: null,
  profile: null,
  master: { jalur: [], tandon: [], fuelTruck: [], fmSettings: [], routeConfig: [], profiles: [] },
  transferRows: [],
  monitoringRows: [],
  activeView: 'Dashboard',
  activeAdminTab: 'tandon',
  activeHistoryTab: 'transfer',
  transferPhotos: { awal: null, akhir: null },
  monitoringPhotos: { fmIn: null, fmOut: null, hm: null },
  teraTangki: null,
  transferTera: null
};

function showToast(message, type = 'info') {
  const el = $('toast');
  el.textContent = message;
  el.className = `toast show ${type}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => el.className = 'toast', 4200);
}

function setBusy(button, busy, textBusy = 'Memproses...') {
  if (!button) return;
  if (busy) {
    button.dataset.oldText = button.textContent;
    button.textContent = textBusy;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.oldText || button.textContent;
    button.disabled = false;
  }
}

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function parseNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  let text = String(value).trim().replace(/\s/g, '').replace(/[^0-9,.-]/g, '');
  if (!text || text === '-' || text === '.' || text === ',') return null;
  const lastComma = text.lastIndexOf(',');
  const lastDot = text.lastIndexOf('.');
  if (lastComma >= 0 && lastDot >= 0) {
    text = lastComma > lastDot ? text.replace(/\./g, '').replace(/,/g, '.') : text.replace(/,/g, '');
  } else if (lastComma >= 0) {
    text = text.replace(/,/g, '.');
  }
  const n = Number(text);
  return Number.isFinite(n) ? n : null;
}

function formatNumber(value) {
  if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return '-';
  return fmt.format(Number(value));
}

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#039;', '"': '&quot;' }[c]));
}

function normalize(value) {
  return String(value || '').toUpperCase().replace(/\s+/g, '').replace(/[^A-Z0-9]/g, '');
}

function nrpToAuthEmail(value) {
  const raw = String(value || '').trim();
  if (!raw) throw new Error('NRP wajib diisi.');
  if (raw.includes('@')) return raw.toLowerCase(); // fallback untuk akun lama yang masih memakai email
  const nrp = normalize(raw);
  if (!nrp) throw new Error('NRP tidak valid.');
  return `${nrp.toLowerCase()}@fuelppabib.local`;
}


async function completeOwnProfile(fullName, nrp) {
  const cleanNrp = normalize(nrp);
  if (!state.user?.id || !cleanNrp) return;
  const payload = {
    full_name: fullName || cleanNrp,
    nrp: cleanNrp,
    login_nrp: cleanNrp
  };
  const { error } = await sb.from('fuel_profiles').update(payload).eq('id', state.user.id);
  if (error) console.warn('Gagal update profil NRP:', error.message);
}

async function autoRegisterFromStaging(nrpRaw, password) {
  const nrp = normalize(nrpRaw);
  if (!nrp) throw new Error('NRP wajib diisi.');
  if (normalize(password) !== nrp) throw new Error('Password awal harus sama dengan NRP.');

  // Cek staging dulu (untuk info full_name & jabatan)
  let fullName = nrp;
  let jabatan = '';
  try {
    const { data: seed } = await sb.rpc('fuel_public_staged_nrp_lookup', { p_nrp: nrp });
    if (seed && seed.is_found) {
      fullName = seed.full_name || nrp;
      jabatan = seed.jabatan || '';
    }
    // Jika seed tidak ditemukan di staging TETAPI user ada di app_user,
    // backend register endpoint akan handle. Kita tetap coba register.
  } catch (e) {
    // RPC gagal — lanjut coba register langsung
    console.warn('Staging lookup skipped:', e.message);
  }

  const email = nrpToAuthEmail(nrp);
  const { data, error } = await sb.auth.signUp({
    email,
    password: nrp,
    options: {
      data: { full_name: fullName, nrp, jabatan }
    }
  });
  if (error) {
    // User mungkin sudah ada di app_user (gabungan dashboard + lapangan).
    // Backend register balikin 409 jika duplikat. Saran: coba login langsung.
    if (error.status === 409 || /sudah dipakai|already exists/i.test(error.message)) {
      throw new Error(`User "${nrp}" sudah terdaftar. Silakan login langsung dengan password Anda.`);
    }
    throw error;
  }
  if (!data.session) {
    // Backend register auto-login, harusnya session ada.
    // Fallback: coba login langsung dengan password yang sama
    const { data: loginData, error: loginErr } = await sb.auth.signInWithPassword({ email, password: nrp });
    if (loginErr) throw loginErr;
    state.session = loginData.session;
    state.user = loginData.user;
    await enterApp();
    return loginData;
  }
  state.session = data.session;
  state.user = data.user;
  await completeOwnProfile(fullName, nrp);
  return data;
}

async function changeMyPassword() {
  const newPassword = prompt('Masukkan password baru minimal 6 karakter:');
  if (!newPassword) return;
  if (newPassword.length < 6) return showToast('Password minimal 6 karakter.', 'error');
  const confirmPassword = prompt('Ulangi password baru:');
  if (newPassword !== confirmPassword) return showToast('Konfirmasi password tidak sama.', 'error');
  const { error } = await sb.auth.updateUser({ password: newPassword });
  if (error) return showToast(error.message, 'error');
  showToast('Password berhasil diganti. Gunakan password baru saat login berikutnya.', 'success');
}


function profileNrp(row) {
  const direct = row?.login_nrp || row?.nrp || '';
  if (direct) return direct;
  const email = row?.email || '';
  if (email.endsWith('@fuelppabib.local')) return email.split('@')[0].toUpperCase();
  return '';
}

async function loadTeraTangkiAsset() {
  if (state.teraTangki) return state.teraTangki;
  const response = await fetch('assets/tera-tangki.json', { cache: 'force-cache' });
  if (!response.ok) throw new Error('File import Tera Tangki tidak ditemukan di assets.');
  state.teraTangki = await response.json();
  return state.teraTangki;
}

function getUnitCodeById(id) {
  const row = state.master.fuelTruck.find((item) => item.id === id);
  return row?.unit_code || '';
}

async function getTeraVolumeDb(fuelTruckId, dipValue, unitCode) {
  if ((!fuelTruckId && !unitCode) || dipValue === null || dipValue === undefined || Number.isNaN(Number(dipValue))) return null;
  // Strategi lookup (fallback chain):
  //   1) Endpoint public /api/v1/sounding/volume (no auth, supports MAINTANK & FT strip/no-strip)
  //   2) RPC sb.rpc('fuel_get_tera_volume') (kalau user login & fuel_truck_id ada)
  //
  // Prioritaskan endpoint public karena:
  //   - Tidak butuh login
  //   - Backend sudah auto-normalize kode FT (strip ↔ no-strip)
  //   - Support MAINTANK (TA11/FS10) lewat unitCode
  const headers = { 'Content-Type': 'application/json' };

  // Resolve unitCode kalau belum ada (cari dari state.master.fuelTruck by id)
  let resolvedCode = unitCode;
  if (!resolvedCode && fuelTruckId && state.master?.fuelTruck) {
    const row = state.master.fuelTruck.find((item) => item.id === fuelTruckId);
    resolvedCode = row?.unit_code || '';
  }

  // 1) Endpoint public — selalu coba kalau ada resolvedCode
  if (resolvedCode) {
    try {
      const resp = await fetch(`/api/v1/sounding/volume?aset=${encodeURIComponent(resolvedCode)}&dip=${encodeURIComponent(Number(dipValue))}`);
      if (resp.ok) {
        const j = await resp.json();
        if (j.found && j.volume_l !== null && j.volume_l !== undefined) {
          return Number(j.volume_l);
        }
      }
    } catch (_) { /* network error → coba RPC */ }
  }

  // 2) Fallback RPC kalau user login (butuh session Supabase auth)
  if (fuelTruckId) {
    try {
      const { data, error } = await sb.rpc('fuel_get_tera_volume', {
        p_fuel_truck_id: fuelTruckId,
        p_dip_value: Number(dipValue),
      });
      if (!error) {
        let vol = null;
        if (Array.isArray(data) && data.length > 0) vol = data[0].volume_l;
        else if (data && typeof data === 'object' && 'volume_l' in data) vol = data.volume_l;
        else if (typeof data === 'number') vol = data;
        if (vol !== null && vol !== undefined) return Number(vol);
      }
    } catch (_) { /* RPC gagal → return null */ }
  }

  return null;
}

function isAdmin() { return ['SUPER_ADMIN', 'ADMIN'].includes(state.profile?.role); }
function isSupervisor() { return ['SUPER_ADMIN', 'ADMIN', 'SUPERVISOR'].includes(state.profile?.role); }

async function getProfile() {
  // Lookup profile by id — backend returns deterministic UUID in state.user.id
  const { data, error } = await sb.from('fuel_profiles')
    .select('*')
    .eq('id', state.user.id)
    .single();
  if (error) throw error;
  return data;
}

async function init() {
  bindAuthEvents();
  const { data } = await sb.auth.getSession();
  state.session = data.session;
  state.user = data.session?.user || null;
  if (state.user) await enterApp();
  else enterAuth();
}

function enterAuth() {
  $('authView').classList.remove('hidden');
  $('appView').classList.add('hidden');
}

async function enterApp() {
  $('authView').classList.add('hidden');
  $('appView').classList.remove('hidden');
  try {
    state.profile = await getProfile();
    $('userInfo').textContent = `${state.profile.full_name} · ${profileNrp(state.profile) || 'NRP belum diset'} · ${state.profile.role}`;
    $('navAdmin').classList.toggle('hidden', !isAdmin());
    $('navUsers').classList.toggle('hidden', !isAdmin());
    bindNav();
    await loadAllMaster();
    renderAll();
  } catch (error) {
    showToast(error.message || 'Gagal memuat profil.', 'error');
  }
}

function bindAuthEvents() {
  $('btnShowLogin').onclick = () => toggleAuthMode('login');
  $('btnShowSignup').onclick = () => toggleAuthMode('signup');

  $('loginForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const btn = event.submitter;
    setBusy(btn, true, 'Masuk...');
    let authEmail;
    try {
      authEmail = nrpToAuthEmail($('loginEmail').value.trim());
    } catch (err) {
      setBusy(btn, false);
      return showToast(err.message, 'error');
    }
    const loginNrp = $('loginEmail').value.trim();
    const loginPass = $('loginPassword').value;
    let { data, error } = await sb.auth.signInWithPassword({
      email: authEmail,
      password: loginPass
    });
    if (error) {
      // Auto-register fallback untuk user dari dashboard utama yang belum
      // punya profile di lapangan. Backend register akan reject duplikat
      // (409) dan kita kasih pesan jelas.
      if (!loginNrp.includes('@') && normalize(loginPass) === normalize(loginNrp)) {
        try {
          await autoRegisterFromStaging(loginNrp, loginPass);
          setBusy(btn, false);
          return await enterApp();
        } catch (autoError) {
          setBusy(btn, false);
          return showToast(autoError.message || error.message, 'error');
        }
      }
      setBusy(btn, false);
      return showToast(error.message || 'Login gagal.', 'error');
    }
    setBusy(btn, false);
    state.session = data.session;
    state.user = data.user;
    await enterApp();
  });

  $('signupForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const btn = event.submitter;
    setBusy(btn, true, 'Mendaftarkan...');
    let authEmail;
    const nrp = normalize($('signupEmail').value.trim());
    try {
      authEmail = nrpToAuthEmail($('signupEmail').value.trim());
    } catch (err) {
      setBusy(btn, false);
      return showToast(err.message, 'error');
    }
    const { data, error } = await sb.auth.signUp({
      email: authEmail,
      password: $('signupPassword').value,
      options: { data: { full_name: $('signupName').value.trim(), nrp } }
    });
    setBusy(btn, false);
    if (error) return showToast(error.message, 'error');
    if (data.session) {
      state.session = data.session;
      state.user = data.user;
      await completeOwnProfile($('signupName').value.trim(), nrp);
      await enterApp();
    } else {
      showToast('User dibuat. Jika belum langsung login, pastikan Confirm Email di Supabase sudah OFF.', 'success');
      toggleAuthMode('login');
    }
  });

  if ($('btnChangePassword')) $('btnChangePassword').onclick = changeMyPassword;

  $('btnLogout').onclick = async () => {
    await sb.auth.signOut();
    state.session = null;
    state.user = null;
    state.profile = null;
    enterAuth();
  };
}

function toggleAuthMode(mode) {
  const login = mode === 'login';
  $('loginForm').classList.toggle('hidden', !login);
  $('signupForm').classList.toggle('hidden', login);
  $('btnShowLogin').classList.toggle('active', login);
  $('btnShowSignup').classList.toggle('active', !login);
}

function bindNav() {
  qsa('.nav-btn').forEach((btn) => {
    btn.onclick = () => {
      const view = btn.dataset.view;
      if ((view === 'Admin' || view === 'Users') && !isAdmin()) return showToast('Menu ini hanya untuk ADMIN/SUPER_ADMIN.', 'error');
      switchView(view);
    };
  });
}

function switchView(view) {
  state.activeView = view;
  qsa('.nav-btn').forEach((btn) => btn.classList.toggle('active', btn.dataset.view === view));
  ['Dashboard', 'Transfer', 'Flowmeter', 'HM', 'History', 'Admin', 'Users'].forEach((name) => {
    $(`view${name}`).classList.toggle('hidden', name !== view);
  });
  if (view === 'Dashboard') loadDashboard();
  if (view === 'History') loadHistory();
  if (view === 'Admin') loadAdminData();
  if (view === 'Users') loadUsersPageData();
}

async function loadAllMaster() {
  const [jalur, tandon, fuelTruck, fmSettings] = await Promise.all([
    sb.from('fuel_master_jalur').select('*').order('sort_order', { ascending: true }),
    sb.from('fuel_master_tandon').select('*').order('tandon_code', { ascending: true }),
    sb.from('fuel_master_fuel_truck').select('*').order('unit_code', { ascending: true }),
    sb.from('fuel_fm_awal_settings').select('*').order('created_at', { ascending: true })
  ]);
  for (const res of [jalur, tandon, fuelTruck, fmSettings]) if (res.error) throw res.error;
  state.master.jalur = jalur.data || [];
  state.master.tandon = tandon.data || [];
  state.master.fuelTruck = fuelTruck.data || [];
  state.master.fmSettings = fmSettings.data || [];
}

function activeRows(rows) { return rows.filter((r) => r.status === 'ACTIVE'); }
function optionHtml(rows, valueKey, textKey, selected = '') {
  return rows.map((r) => `<option value="${esc(r[valueKey])}" ${r[valueKey] === selected ? 'selected' : ''}>${esc(r[textKey])}</option>`).join('');
}

function renderAll() {
  renderDashboard();
  renderTransfer();
  renderMonitoring();
  renderHistoryShell();
  renderAdmin();
  renderUsersPage();
  switchView(state.activeView);
}

function renderDashboard() {
  $('viewDashboard').innerHTML = `
    <div class="card">
      <div class="card-header">
        <div><h2>Dashboard Fuel</h2><p>Ringkasan transfer fuel dan status deviasi.</p></div>
        <div class="toolbar">
          <input id="dashDate" type="date" value="${todayISO()}" />
          <button id="btnRefreshDash" class="btn secondary" type="button">Refresh</button>
        </div>
      </div>
      <div class="card-body">
        <div id="dashKpi" class="kpi-grid"></div>
      </div>
    </div>
    <div class="card">
      <div class="card-header">
        <div><h2>Data Hari Ini</h2><p>Gunakan search untuk mencari unit, jalur, tandon, atau petugas.</p></div>
        <button id="btnExportDash" class="btn secondary" type="button">Download CSV</button>
      </div>
      <div class="card-body">
        <div class="toolbar"><input id="dashSearch" class="search" type="search" placeholder="Search dashboard..." /></div>
        <div id="dashTable"></div>
      </div>
    </div>`;
  $('btnRefreshDash').onclick = loadDashboard;
  $('btnExportDash').onclick = () => exportCsv('dashboard-transfer.csv', filteredTransferRows($('dashSearch')?.value || ''));
  $('dashSearch').oninput = () => renderDashboardTable();
}

async function loadDashboard() {
  const tanggal = $('dashDate')?.value || todayISO();
  const { data, error } = await sb.from('fuel_v_transfer_fuel').select('*').eq('tanggal', tanggal).order('created_at', { ascending: false });
  if (error) return showToast(error.message, 'error');
  state.transferRows = data || [];
  renderDashboardKpi();
  renderDashboardTable();
}

function renderDashboardKpi() {
  const rows = state.transferRows.filter((r) => !r.voided_at);
  const totalFm = rows.reduce((sum, r) => sum + Number(r.total_fm_liter || 0), 0);
  const critical = rows.filter((r) => r.status_deviasi === 'CRITICAL').length;
  const warning = rows.filter((r) => r.status_deviasi === 'WARNING').length;
  $('dashKpi').innerHTML = `
    <div class="kpi-card"><span>Total Submit</span><strong>${rows.length}</strong></div>
    <div class="kpi-card"><span>Total FM Liter</span><strong>${formatNumber(totalFm)}</strong></div>
    <div class="kpi-card"><span>Warning</span><strong>${warning}</strong></div>
    <div class="kpi-card"><span>Critical</span><strong>${critical}</strong></div>`;
}

function filteredTransferRows(search = '') {
  const q = normalize(search);
  return (state.transferRows || []).filter((r) => {
    if (!q) return true;
    return [r.petugas_name, r.jalur_code, r.tandon_code, r.fuel_truck_code, r.shift, r.status_deviasi].some((v) => normalize(v).includes(q));
  });
}

function statusBadge(status) {
  const s = status || 'BELUM_DIHITUNG';
  const cls = s === 'OK' ? 'ok' : s === 'WARNING' ? 'warning' : s === 'CRITICAL' ? 'critical' : 'neutral';
  return `<span class="badge ${cls}">${esc(s)}</span>`;
}

function renderDashboardTable() {
  const rows = filteredTransferRows($('dashSearch')?.value || '');
  $('dashTable').innerHTML = tableHtml(rows, [
    ['tanggal', 'Tanggal'], ['shift', 'Shift'], ['petugas_name', 'Petugas'], ['jalur_code', 'Jalur'],
    ['tandon_code', 'Tandon'], ['fuel_truck_code', 'Unit'], ['total_fm_liter', 'Total FM', formatNumber],
    ['deviasi_tera_percent', 'Deviasi %', (v) => v == null ? '-' : `${formatNumber(v)}%`],
    ['status_deviasi', 'Status', statusBadge]
  ]);
}

function renderTransfer() {
  $('viewTransfer').innerHTML = `
    <form id="transferForm" class="card">
      <div class="card-header"><div><h2>Input Transfer Fuel</h2><p>Input tandon ke unit penerima. FM Awal bisa otomatis dari transaksi terakhir.</p></div></div>
      <div class="card-body">
        <div class="section-title">Data Utama</div>
        <div class="grid">
          <div class="field"><label>Tanggal</label><input id="tfTanggal" type="date" value="${todayISO()}" required /></div>
          <div class="field"><label>Shift</label><select id="tfShift" required><option value="SHIFT_1">Shift 1</option><option value="SHIFT_2">Shift 2</option></select></div>
          <div class="field"><label>Petugas</label><input id="tfPetugas" required value="${esc(state.profile?.full_name || '')}" /></div>
          <div class="field"><label>Jalur</label><select id="tfJalur" required disabled><option value="">Memuat Jalur Aktif</option>${optionHtml(activeRows(state.master.jalur), 'id', 'jalur_code')}</select></div>
          <div class="field"><label>Tandon Aktif</label><select id="tfTandon" required disabled><option value="">Terisi dari konfigurasi jalur</option>${optionHtml(activeRows(state.master.tandon), 'id', 'tandon_code')}</select><small id="tfRouteHint" class="hint">Memuat konfigurasi jalur aktif…</small></div>
          <div class="field"><label>Search Unit Penerima</label><input id="tfUnitSearch" type="search" placeholder="Cari FT / FS10, contoh 2635" /></div>
          <div class="field full"><label>Unit Penerima</label><select id="tfUnit" required><option value="">Pilih Unit</option>${optionHtml(activeRows(state.master.fuelTruck), 'id', 'unit_code')}</select></div>
        </div>
        <div class="section-title">Flowmeter</div>
        <div class="grid">
          <div class="field"><label>FM Awal</label><input id="tfFmAwal" inputmode="decimal" required placeholder="Otomatis/manual" /><small id="tfFmHint" class="hint">Pilih jalur untuk memuat FM Awal.</small></div>
          <div class="field"><label>FM Akhir</label><input id="tfFmAkhir" inputmode="decimal" required placeholder="0" /></div>
        </div>
        <div class="total-box"><span>Total FM</span><strong id="tfTotalFm">0</strong></div>
        <div class="section-title">Tera Unit Penerima (Wajib untuk cek FM vs Tera)</div>
        <div class="grid">
          <div class="field"><label>Tera Awal <span class="required">*</span></label><input id="tfTeraAwal" inputmode="decimal" required placeholder="Masukkan tera awal" /></div>
          <div class="field"><label>Tera Akhir <span class="required">*</span></label><input id="tfTeraAkhir" inputmode="decimal" required placeholder="Masukkan tera akhir" /></div>
        </div>
        <div class="total-box"><span>Total Volume Tera</span><strong id="tfTotalTera">0</strong><span id="tfDeviasiInfo"></span></div>
        <div id="tfCatatanWrap" class="field hidden"><label>Catatan Deviasi</label><textarea id="tfCatatan" placeholder="Wajib jika deviasi lebih dari 5%"></textarea></div>
        <div class="section-title">Foto Wajib</div>
        <div class="photo-grid">
          <div class="field"><label>Foto Flowmeter Awal</label><input id="tfPhotoAwal" type="file" accept="image/*" capture="environment" required /><div class="photo-preview"><img id="tfPreviewAwal"><small id="tfInfoAwal" class="hint">Belum ada foto.</small></div></div>
          <div class="field"><label>Foto Flowmeter Akhir</label><input id="tfPhotoAkhir" type="file" accept="image/*" capture="environment" required /><div class="photo-preview"><img id="tfPreviewAkhir"><small id="tfInfoAkhir" class="hint">Belum ada foto.</small></div></div>
        </div>
        <div class="bottom-actions"><button id="tfReset" class="btn secondary" type="button">Reset</button><button class="btn primary" type="submit">Simpan Transfer</button></div>
      </div>
    </form>`;
  bindTransfer();
}

function bindTransfer() {
  $('tfJalur').onchange = async () => { applySelectedTransferRoute(); await loadDefaultFmAwal(); };
  $('tfTanggal').onchange = loadActiveTransferRoutes;
  $('tfShift').onchange = loadActiveTransferRoutes;
  $('tfUnitSearch').oninput = () => filterUnitSelect('tfUnitSearch', 'tfUnit');
  ['tfFmAwal', 'tfFmAkhir', 'tfTeraAwal', 'tfTeraAkhir', 'tfUnit'].forEach((id) => $(id).addEventListener('input', updateTransferTotals));
  $('tfUnit').addEventListener('change', updateTransferTotals);
  $('tfPhotoAwal').onchange = async () => state.transferPhotos.awal = await processPhoto($('tfPhotoAwal'), $('tfPreviewAwal'), $('tfInfoAwal'));
  $('tfPhotoAkhir').onchange = async () => state.transferPhotos.akhir = await processPhoto($('tfPhotoAkhir'), $('tfPreviewAkhir'), $('tfInfoAkhir'));
  $('tfReset').onclick = () => { renderTransfer(); showToast('Form dikosongkan.', 'info'); };
  $('transferForm').onsubmit = submitTransfer;
  loadActiveTransferRoutes();
}

function filterUnitSelect(searchId, selectId) {
  const q = normalize($(searchId).value);
  const rows = activeRows(state.master.fuelTruck).filter((r) => !q || normalize(r.unit_code).includes(q) || normalize(r.unit_name).includes(q));
  $(selectId).innerHTML = `<option value="">${q ? 'Pilih hasil pencarian' : 'Pilih Unit'}</option>${optionHtml(rows, 'id', 'unit_code')}`;
}

async function loadActiveTransferRoutes() {
  const tanggal = $('tfTanggal')?.value || todayISO();
  const shift = $('tfShift')?.value || 'SHIFT_1';
  const hint = $('tfRouteHint');
  if (hint) hint.textContent = 'Memuat konfigurasi jalur aktif…';
  const { data, error } = await sb.from('fuel_v_route_config').select('*')
    .eq('site_code', SITE_CODE).eq('tanggal', tanggal).eq('shift', shift)
    .eq('peruntukan', 'TRANSFER').eq('status', 'VALIDATED')
    .order('jalur_code', { ascending: true });
  if (error) {
    state.master.routeConfig = [];
    $('tfJalur').innerHTML = '<option value="">Konfigurasi jalur belum tersedia</option>';
    $('tfJalur').disabled = true;
    $('tfTandon').value = '';
    if (hint) hint.textContent = `Gagal membaca konfigurasi: ${error.message}`;
    return;
  }
  state.master.routeConfig = data || [];
  $('tfJalur').disabled = state.master.routeConfig.length === 0;
  const current = $('tfJalur').value;
  $('tfJalur').innerHTML = '<option value="">Pilih Jalur Aktif</option>' +
    state.master.routeConfig.map(r => `<option value="${esc(r.jalur_id)}" ${String(r.jalur_id)===String(current)?'selected':''}>${esc(r.jalur_code)} → ${esc(r.tandon_code)}</option>`).join('');
  if (state.master.routeConfig.length === 1) $('tfJalur').value = state.master.routeConfig[0].jalur_id;
  applySelectedTransferRoute();
  if (hint) hint.textContent = state.master.routeConfig.length
    ? `${state.master.routeConfig.length} jalur TRANSFER berstatus VALIDATED tersedia.`
    : `Tidak ada jalur TRANSFER VALIDATED untuk ${tanggal} ${shift}. Hubungi Admin Fuel.`;
  if ($('tfJalur').value) await loadDefaultFmAwal();
}

function applySelectedTransferRoute() {
  const route = state.master.routeConfig.find(r => String(r.jalur_id) === String($('tfJalur').value));
  $('tfTandon').value = route?.tandon_id || '';
  const hint = $('tfRouteHint');
  if (hint && route) hint.textContent = `${route.jalur_code} terhubung ke ${route.tandon_code} · ${route.peruntukan} · VALIDATED`;
}

async function loadDefaultFmAwal() {
  const jalurId = $('tfJalur').value;
  if (!jalurId) return;
  $('tfFmHint').textContent = 'Memuat FM Awal...';
  const { data, error } = await sb.rpc('fuel_get_default_fm_awal', { p_site_code: SITE_CODE, p_jalur_id: jalurId });
  if (error) return $('tfFmHint').textContent = error.message;
  const row = data?.[0];
  $('tfFmAwal').value = row?.fm_awal ?? '';
  $('tfFmHint').textContent = row?.message || 'FM Awal bisa diedit sebelum simpan.';
  updateTransferTotals();
}

async function updateTransferTotals() {
  const awal = parseNumber($('tfFmAwal').value);
  const akhir = parseNumber($('tfFmAkhir').value);
  const total = awal !== null && akhir !== null ? akhir - awal : 0;
  $('tfTotalFm').textContent = formatNumber(total);
  $('tfTotalFm').classList.toggle('danger', total < 0);

  const teraAwal = parseNumber($('tfTeraAwal').value);
  const teraAkhir = parseNumber($('tfTeraAkhir').value);
  $('tfDeviasiInfo').textContent = '';
  $('tfCatatanWrap').classList.add('hidden');
  state.transferTera = null;

  if (!$('tfUnit').value || teraAwal === null || teraAkhir === null) {
    $('tfTotalTera').textContent = '0';
    $('tfDeviasiInfo').textContent = ' Tera wajib diisi lengkap untuk cek penyetokan.';
    return;
  }

  try {
    const unitId = $('tfUnit').value;
    const unitCode = getUnitCodeById(unitId);
    const [volAwal, volAkhir] = await Promise.all([
      getTeraVolumeDb(unitId, teraAwal, unitCode),
      getTeraVolumeDb(unitId, teraAkhir, unitCode)
    ]);

    if (volAwal === null || volAkhir === null) {
      $('tfTotalTera').textContent = '-';
      $('tfDeviasiInfo').textContent = ` Data Tera Tangki belum ada di database / di luar range untuk ${unitCode || 'unit ini'}. Admin perlu import Tera DB.`;
      return;
    }

    const totalTera = volAkhir - volAwal;
    const dev = total > 0 ? Math.abs(total - totalTera) / Math.abs(total) * 100 : null;
    state.transferTera = { volAwal, volAkhir, totalTera, deviasi: dev };
    $('tfTotalTera').textContent = formatNumber(totalTera);

    if (dev !== null) {
      $('tfDeviasiInfo').textContent = ` Deviasi ${formatNumber(dev)}% · Vol Awal ${formatNumber(volAwal)} · Vol Akhir ${formatNumber(volAkhir)}`;
      if (dev > 5) $('tfCatatanWrap').classList.remove('hidden');
    }
  } catch (error) {
    $('tfTotalTera').textContent = '-';
    $('tfDeviasiInfo').textContent = ` ${error.message || 'Gagal membaca data Tera Tangki.'}`;
  }
}

async function submitTransfer(event) {
  event.preventDefault();
  const btn = event.submitter;
  const fmAwal = parseNumber($('tfFmAwal').value);
  const fmAkhir = parseNumber($('tfFmAkhir').value);
  const teraAwal = parseNumber($('tfTeraAwal').value);
  const teraAkhir = parseNumber($('tfTeraAkhir').value);
  if (fmAwal === null || fmAkhir === null) return showToast('FM Awal dan FM Akhir wajib angka.', 'error');
  if (fmAkhir < fmAwal) return showToast('FM Akhir tidak boleh lebih kecil dari FM Awal.', 'error');
  if (teraAwal === null || teraAkhir === null) return showToast('Tera Awal dan Akhir wajib diisi untuk cek penyetokan.', 'error');
  await updateTransferTotals();
  if (!state.transferTera) return showToast('Volume Tera belum bisa dihitung. Cek unit penerima dan range Tera Tangki.', 'error');
  if (state.transferTera.deviasi > 5 && !$('tfCatatan').value.trim()) return showToast('Deviasi lebih dari 5%. Catatan deviasi wajib diisi.', 'error');
  const activeRoute = state.master.routeConfig.find(r => String(r.jalur_id) === String($('tfJalur').value));
  if (!activeRoute || String(activeRoute.tandon_id) !== String($('tfTandon').value))
    return showToast('Jalur dan tandon belum terhubung pada Konfigurasi Jalur berstatus VALIDATED.', 'error');
  if (!state.transferPhotos.awal || !state.transferPhotos.akhir) return showToast('Foto flowmeter awal dan akhir wajib diupload.', 'error');

  setBusy(btn, true, 'Menyimpan...');
  try {
    const basePath = `${state.user.id}/transfer/${$('tfTanggal').value}/${globalThis.fccUniqueId()}`;
    const pAwal = await uploadPhoto(`${basePath}_awal.jpg`, state.transferPhotos.awal.blob);
    const pAkhir = await uploadPhoto(`${basePath}_akhir.jpg`, state.transferPhotos.akhir.blob);
    const payload = {
      site_code: SITE_CODE,
      tanggal: $('tfTanggal').value,
      shift: $('tfShift').value,
      petugas_name: $('tfPetugas').value.trim(),
      jalur_id: $('tfJalur').value,
      tandon_id: $('tfTandon').value,
      fuel_truck_id: $('tfUnit').value,
      fm_awal: fmAwal,
      fm_akhir: fmAkhir,
      tera_unit_awal: teraAwal,
      tera_unit_akhir: teraAkhir,
      volume_tera_unit_awal: state.transferTera.volAwal,
      volume_tera_unit_akhir: state.transferTera.volAkhir,
      catatan_deviasi: $('tfCatatan').value.trim() || null,
      created_by: state.user.id
    };
    const { data, error } = await sb.from('fuel_tx_transfer_fuel').insert(payload).select('id,no_urut').single();
    if (error) throw error;
    await insertAttachments([
      { transfer_fuel_id: data.id, photo_type: 'TRANSFER_FM_AWAL', storage_path: pAwal.path, mime_type: state.transferPhotos.awal.mime, file_size_bytes: state.transferPhotos.awal.size },
      { transfer_fuel_id: data.id, photo_type: 'TRANSFER_FM_AKHIR', storage_path: pAkhir.path, mime_type: state.transferPhotos.akhir.mime, file_size_bytes: state.transferPhotos.akhir.size }
    ]);
    showToast(`Data berhasil disimpan. No ${data.no_urut}`, 'success');
    state.transferPhotos = { awal: null, akhir: null };
    renderTransfer();
    loadDashboard();
  } catch (error) {
    showToast(error.message || 'Gagal simpan transfer.', 'error');
  } finally {
    setBusy(btn, false);
  }
}

function renderMonitoring() {
  renderFlowmeter();
  renderHM();
  bindMonitoring();
}

function renderFlowmeter() {
  $('viewFlowmeter').innerHTML = `
    <form id="flowmeterForm" class="card">
      <div class="card-header"><div><h2>Input Flowmeter Fuel Truck</h2><p>Form khusus Flowmeter. Data HM ada di menu HM terpisah.</p></div></div>
      <div class="card-body">
        <div class="grid">
          <div class="field"><label>Tanggal</label><input id="fmTanggal" type="date" value="${todayISO()}" required /></div>
          <div class="field"><label>Shift</label><select id="fmShift" required><option value="SHIFT_1">Shift 1</option><option value="SHIFT_2">Shift 2</option></select></div>
          <div class="field"><label>Petugas</label><input id="fmPetugas" required value="${esc(state.profile?.full_name || '')}" /></div>
          <div class="field"><label>Search Unit</label><input id="fmUnitSearch" type="search" placeholder="Cari FT / FS10" /></div>
          <div class="field full"><label>Fuel Truck</label><select id="fmUnit" required><option value="">Pilih Unit</option>${optionHtml(activeRows(state.master.fuelTruck), 'id', 'unit_code')}</select></div>
        </div>
        <div class="section-title">Flowmeter</div>
        <div class="grid">
          <div class="field"><label>FM IN</label><input id="fmIn" inputmode="decimal" required placeholder="0" /></div>
          <div class="field"><label>FM OUT</label><input id="fmOut" inputmode="decimal" required placeholder="0" /></div>
        </div>
        <div class="total-box"><span>Total Flowmeter</span><strong id="fmTotal">0</strong></div>
        <div class="section-title">Foto Flowmeter</div>
        <div class="photo-grid">
          <div class="field"><label>Foto FM IN</label><input id="fmPhotoIn" type="file" accept="image/*" capture="environment" required /><div class="photo-preview"><img id="fmPreviewIn"><small id="fmInfoIn" class="hint">Belum ada foto.</small></div></div>
          <div class="field"><label>Foto FM OUT</label><input id="fmPhotoOut" type="file" accept="image/*" capture="environment" required /><div class="photo-preview"><img id="fmPreviewOut"><small id="fmInfoOut" class="hint">Belum ada foto.</small></div></div>
        </div>
        <div class="bottom-actions"><button id="fmReset" class="btn secondary" type="button">Reset FM</button><button class="btn primary" type="submit">Simpan Flowmeter</button></div>
      </div>
    </form>`;
}

function renderHM() {
  $('viewHM').innerHTML = `
    <form id="hmForm" class="card">
      <div class="card-header"><div><h2>Input Hour Meter Fuel Truck</h2><p>Form khusus HM. Data Flowmeter ada di menu FM terpisah.</p></div></div>
      <div class="card-body">
        <div class="grid">
          <div class="field"><label>Tanggal</label><input id="hmTanggal" type="date" value="${todayISO()}" required /></div>
          <div class="field"><label>Shift</label><select id="hmShift" required><option value="SHIFT_1">Shift 1</option><option value="SHIFT_2">Shift 2</option></select></div>
          <div class="field"><label>Petugas</label><input id="hmPetugas" required value="${esc(state.profile?.full_name || '')}" /></div>
          <div class="field"><label>Search Unit</label><input id="hmUnitSearch" type="search" placeholder="Cari FT / FS10" /></div>
          <div class="field full"><label>Fuel Truck</label><select id="hmUnit" required><option value="">Pilih Unit</option>${optionHtml(activeRows(state.master.fuelTruck), 'id', 'unit_code')}</select></div>
        </div>
        <div class="section-title">Hour Meter</div>
        <div class="field"><label>HM Fuel Truck</label><input id="hmValue" inputmode="decimal" required placeholder="0" /></div>
        <div class="section-title">Foto HM</div>
        <div class="photo-grid">
          <div class="field full"><label>Foto HM</label><input id="hmPhoto" type="file" accept="image/*" capture="environment" required /><div class="photo-preview"><img id="hmPreview"><small id="hmInfo" class="hint">Belum ada foto.</small></div></div>
        </div>
        <div class="bottom-actions"><button id="hmReset" class="btn secondary" type="button">Reset HM</button><button class="btn primary" type="submit">Simpan HM</button></div>
      </div>
    </form>`;
}

function bindMonitoring() {
  $('fmUnitSearch').oninput = () => filterUnitSelect('fmUnitSearch', 'fmUnit');
  $('hmUnitSearch').oninput = () => filterUnitSelect('hmUnitSearch', 'hmUnit');
  ['fmIn', 'fmOut'].forEach((id) => $(id).oninput = updateFlowmeterTotal);
  $('fmPhotoIn').onchange = async () => state.monitoringPhotos.fmIn = await processPhoto($('fmPhotoIn'), $('fmPreviewIn'), $('fmInfoIn'));
  $('fmPhotoOut').onchange = async () => state.monitoringPhotos.fmOut = await processPhoto($('fmPhotoOut'), $('fmPreviewOut'), $('fmInfoOut'));
  $('hmPhoto').onchange = async () => state.monitoringPhotos.hm = await processPhoto($('hmPhoto'), $('hmPreview'), $('hmInfo'));
  $('fmReset').onclick = () => { state.monitoringPhotos.fmIn = null; state.monitoringPhotos.fmOut = null; renderMonitoring(); };
  $('hmReset').onclick = () => { state.monitoringPhotos.hm = null; renderMonitoring(); };
  $('flowmeterForm').onsubmit = submitFlowmeter;
  $('hmForm').onsubmit = submitHM;
}

function updateFlowmeterTotal() {
  const fmIn = parseNumber($('fmIn').value);
  const fmOut = parseNumber($('fmOut').value);
  const total = fmIn !== null && fmOut !== null ? fmOut - fmIn : 0;
  $('fmTotal').textContent = formatNumber(total);
  $('fmTotal').classList.toggle('danger', total < 0);
}

async function submitFlowmeter(event) {
  event.preventDefault();
  const btn = event.submitter;
  const fmIn = parseNumber($('fmIn').value);
  const fmOut = parseNumber($('fmOut').value);
  if (fmIn === null || fmOut === null) return showToast('FM IN dan FM OUT wajib angka.', 'error');
  if (fmOut < fmIn) return showToast('FM OUT tidak boleh lebih kecil dari FM IN.', 'error');
  if (!state.monitoringPhotos.fmIn || !state.monitoringPhotos.fmOut) return showToast('Foto FM IN dan FM OUT wajib diupload.', 'error');
  setBusy(btn, true, 'Menyimpan FM...');
  try {
    const basePath = `${state.user.id}/flowmeter/${$('fmTanggal').value}/${globalThis.fccUniqueId()}`;
    const pIn = await uploadPhoto(`${basePath}_in.jpg`, state.monitoringPhotos.fmIn.blob);
    const pOut = await uploadPhoto(`${basePath}_out.jpg`, state.monitoringPhotos.fmOut.blob);
    const { data, error } = await sb.from('fuel_tx_fuel_truck_monitoring').insert({
      site_code: SITE_CODE,
      tanggal: $('fmTanggal').value,
      shift: $('fmShift').value,
      monitoring_type: 'FLOWMETER',
      petugas_name: $('fmPetugas').value.trim(),
      fuel_truck_id: $('fmUnit').value,
      fm_in: fmIn,
      fm_out: fmOut,
      hm_value: null,
      created_by: state.user.id
    }).select('id').single();
    if (error) throw error;
    await insertAttachments([
      { monitoring_id: data.id, photo_type: 'MONITORING_FM_IN', storage_path: pIn.path, mime_type: state.monitoringPhotos.fmIn.mime, file_size_bytes: state.monitoringPhotos.fmIn.size },
      { monitoring_id: data.id, photo_type: 'MONITORING_FM_OUT', storage_path: pOut.path, mime_type: state.monitoringPhotos.fmOut.mime, file_size_bytes: state.monitoringPhotos.fmOut.size }
    ]);
    showToast('Data Flowmeter berhasil disimpan.', 'success');
    state.monitoringPhotos.fmIn = null;
    state.monitoringPhotos.fmOut = null;
    renderMonitoring();
  } catch (error) {
    showToast(error.message || 'Gagal simpan Flowmeter.', 'error');
  } finally {
    setBusy(btn, false);
  }
}

async function submitHM(event) {
  event.preventDefault();
  const btn = event.submitter;
  const hm = parseNumber($('hmValue').value);
  if (hm === null) return showToast('HM wajib diisi angka.', 'error');
  if (hm < 0) return showToast('HM tidak boleh minus.', 'error');
  if (!state.monitoringPhotos.hm) return showToast('Foto HM wajib diupload.', 'error');
  setBusy(btn, true, 'Menyimpan HM...');
  try {
    const basePath = `${state.user.id}/hm/${$('hmTanggal').value}/${globalThis.fccUniqueId()}`;
    const pHm = await uploadPhoto(`${basePath}_hm.jpg`, state.monitoringPhotos.hm.blob);
    const { data, error } = await sb.from('fuel_tx_fuel_truck_monitoring').insert({
      site_code: SITE_CODE,
      tanggal: $('hmTanggal').value,
      shift: $('hmShift').value,
      monitoring_type: 'HM',
      petugas_name: $('hmPetugas').value.trim(),
      fuel_truck_id: $('hmUnit').value,
      fm_in: null,
      fm_out: null,
      hm_value: hm,
      created_by: state.user.id
    }).select('id').single();
    if (error) throw error;
    await insertAttachments([{ monitoring_id: data.id, photo_type: 'MONITORING_HM', storage_path: pHm.path, mime_type: state.monitoringPhotos.hm.mime, file_size_bytes: state.monitoringPhotos.hm.size }]);
    showToast('Data HM berhasil disimpan.', 'success');
    state.monitoringPhotos.hm = null;
    renderMonitoring();
  } catch (error) {
    showToast(error.message || 'Gagal simpan HM.', 'error');
  } finally {
    setBusy(btn, false);
  }
}

async function processPhoto(input, preview, info) {
  const file = input.files?.[0];
  if (!file) return null;
  if (!file.type.startsWith('image/')) { showToast('File harus berupa gambar.', 'error'); input.value = ''; return null; }
  const result = await compressImage(file, 1280, 1280, 0.75);
  preview.src = URL.createObjectURL(result.blob);
  preview.style.display = 'block';
  info.textContent = `Kompres: ${Math.round(file.size / 1024)} KB → ${Math.round(result.blob.size / 1024)} KB`;
  return result;
}

function compressImage(file, maxWidth, maxHeight, quality) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = reject;
    reader.onload = () => {
      const img = new Image();
      img.onerror = reject;
      img.onload = () => {
        let { width, height } = img;
        if (width > height && width > maxWidth) { height = Math.round(height * maxWidth / width); width = maxWidth; }
        if (height >= width && height > maxHeight) { width = Math.round(width * maxHeight / height); height = maxHeight; }
        const canvas = document.createElement('canvas');
        canvas.width = width; canvas.height = height;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, width, height); ctx.drawImage(img, 0, 0, width, height);
        canvas.toBlob((blob) => {
          if (!blob) return reject(new Error('Gagal kompres foto.'));
          resolve({ blob, mime: 'image/jpeg', size: blob.size });
        }, 'image/jpeg', quality);
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

async function uploadPhoto(path, blob) {
  const { data, error } = await sb.storage.from(PHOTO_BUCKET).upload(path, blob, { contentType: 'image/jpeg', upsert: false });
  if (error) throw error;
  return data;
}

async function insertAttachments(rows) {
  const payload = rows.map((r) => ({ site_code: SITE_CODE, bucket_name: PHOTO_BUCKET, uploaded_by: state.user.id, ...r }));
  const { error } = await sb.from('fuel_attachment_log').insert(payload);
  if (error) throw error;
}

function renderHistoryShell() {
  $('viewHistory').innerHTML = `
    <div class="card">
      <div class="card-header"><div><h2>Riwayat Data</h2><p>Search dan download CSV untuk memudahkan rekap.</p></div></div>
      <div class="card-body">
        <div class="history-tabs"><button class="tab-chip active" data-history="transfer">Transfer Fuel</button><button class="tab-chip" data-history="monitoring">Flowmeter/HM</button></div>
        <div class="toolbar" style="margin-top:12px"><input id="histDate" type="date" value="${todayISO()}" /><input id="histSearch" class="search" type="search" placeholder="Search riwayat..." /><button id="btnHistRefresh" class="btn secondary" type="button">Refresh</button><button id="btnHistCsv" class="btn secondary" type="button">Download CSV</button></div>
        <div id="historyResult"></div>
      </div>
    </div>`;
  qsa('[data-history]').forEach((b) => b.onclick = () => { state.activeHistoryTab = b.dataset.history; qsa('[data-history]').forEach((x) => x.classList.toggle('active', x === b)); loadHistory(); });
  $('btnHistRefresh').onclick = loadHistory;
  $('histSearch').oninput = renderHistory;
  $('btnHistCsv').onclick = () => exportCsv(`${state.activeHistoryTab}-${$('histDate').value}.csv`, getFilteredHistoryRows());
}

async function loadHistory() {
  const tanggal = $('histDate')?.value || todayISO();
  const source = state.activeHistoryTab === 'transfer' ? 'fuel_v_transfer_fuel' : 'fuel_v_fuel_truck_monitoring';
  const { data, error } = await sb.from(source).select('*').eq('tanggal', tanggal).order('created_at', { ascending: false }).limit(500);
  if (error) return showToast(error.message, 'error');
  if (state.activeHistoryTab === 'transfer') state.transferRows = data || [];
  else state.monitoringRows = data || [];
  renderHistory();
}

function getFilteredHistoryRows() {
  const q = normalize($('histSearch')?.value || '');
  const rows = state.activeHistoryTab === 'transfer' ? state.transferRows : state.monitoringRows;
  return rows.filter((r) => !q || Object.values(r).some((v) => normalize(v).includes(q)));
}

function renderHistory() {
  const rows = getFilteredHistoryRows();
  if (state.activeHistoryTab === 'transfer') {
    $('historyResult').innerHTML = tableHtml(rows, [
      ['tanggal', 'Tanggal'], ['shift', 'Shift'], ['petugas_name', 'Petugas'], ['jalur_code', 'Jalur'], ['tandon_code', 'Tandon'], ['fuel_truck_code', 'Unit'], ['total_fm_liter', 'Total FM', formatNumber], ['status_deviasi', 'Status', statusBadge]
    ]);
  } else {
    $('historyResult').innerHTML = tableHtml(rows, [
      ['tanggal', 'Tanggal'], ['shift', 'Shift'], ['petugas_name', 'Petugas'], ['fuel_truck_code', 'Unit'], ['monitoring_type', 'Type'], ['total_fm_liter', 'Total FM', formatNumber], ['hm_value', 'HM', formatNumber]
    ]);
  }
}


async function loadUsersPageData() {
  if (!isAdmin()) return;
  const { data, error } = await sb.from('fuel_profiles').select('*').order('created_at', { ascending: false });
  if (error) return showToast(error.message, 'error');
  state.master.profiles = data || [];
  renderUsersPage();
}

function renderUsersPage() {
  if (!$('viewUsers')) return;
  if (!isAdmin()) {
    $('viewUsers').innerHTML = '<div class="card"><div class="card-body">Tidak punya akses setting user.</div></div>';
    return;
  }
  const rows = state.master.profiles || [];
  $('viewUsers').innerHTML = `
    <div class="card">
      <div class="card-header">
        <div><h2>Setting User</h2><p>Kelola role dan status user aplikasi. Gunakan search agar cepat di HP.</p></div>
        <div class="toolbar">
          <button id="btnReloadUsers" class="btn secondary" type="button">Refresh User</button>
          <button id="btnUsersCsv" class="btn secondary" type="button">Download CSV</button>
        </div>
      </div>
      <div class="card-body">
        <div class="toolbar"><input id="usersSearch" class="search" type="search" placeholder="Search nama, NRP, role, status..." /></div>
        <div id="usersTable"></div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><div><h2>Panduan Role</h2><p>Hak akses user dibuat berbeda supaya data lebih aman.</p></div></div>
      <div class="card-body">
        <div class="card-list">
          <div class="record-card"><div class="row"><span>SUPER_ADMIN</span><span>Akses penuh termasuk kelola user</span></div></div>
          <div class="record-card"><div class="row"><span>ADMIN</span><span>Kelola master data dan user</span></div></div>
          <div class="record-card"><div class="row"><span>SUPERVISOR</span><span>Lihat dashboard, riwayat, download CSV</span></div></div>
          <div class="record-card"><div class="row"><span>FIELD</span><span>Input data lapangan dan lihat data sendiri</span></div></div>
        </div>
      </div>
    </div>`;
  const renderRows = () => {
    const q = normalize($('usersSearch').value);
    const filtered = rows.filter((r) => !q || normalize(`${r.full_name}${r.email}${profileNrp(r)}${r.role}${r.status}`).includes(q));
    $('usersTable').innerHTML = tableHtml(filtered, [['full_name', 'Nama'], ['nrp', 'NRP', (v, r) => esc(profileNrp(r) || '-')], ['email', 'Email'], ['role', 'Role'], ['status', 'Status']], (r) => `<button class="btn secondary" data-user-page="${r.id}" type="button">Edit User</button>`);
    qsa('[data-user-page]').forEach((b) => b.onclick = () => editUserRole(rows.find((r) => r.id === b.dataset.userPage)));
  };
  $('usersSearch').oninput = renderRows;
  $('btnReloadUsers').onclick = loadUsersPageData;
  $('btnUsersCsv').onclick = () => exportCsv('setting-users.csv', rows);
  renderRows();
}

function renderAdmin() {
  if (!isAdmin()) { $('viewAdmin').innerHTML = '<div class="card"><div class="card-body">Tidak punya akses admin.</div></div>'; return; }
  $('viewAdmin').innerHTML = `
    <div class="card">
      <div class="card-header"><div><h2>Admin Master Data</h2><p>Edit master data langsung dari HP. Semua tabel punya search dan download CSV.</p></div><button id="btnReloadMaster" class="btn secondary" type="button">Refresh Master</button></div>
      <div class="card-body">
        <div class="admin-tabs">
          <button class="tab-chip active" data-admin="tandon">Tandon</button>
          <button class="tab-chip" data-admin="unit">Fuel Truck</button>
          <button class="tab-chip" data-admin="jalur">Konfigurasi Jalur</button>
          <button class="tab-chip" data-admin="tera">Tera DB</button>
          <button class="tab-chip" data-admin="users">Setting User</button>
        </div>
        <div id="adminContent" style="margin-top:14px"></div>
      </div>
    </div>`;
  $('btnReloadMaster').onclick = async () => { await loadAllMaster(); renderAdminContent(); showToast('Master data diperbarui.', 'success'); };
  qsa('[data-admin]').forEach((btn) => btn.onclick = () => { state.activeAdminTab = btn.dataset.admin; qsa('[data-admin]').forEach((b) => b.classList.toggle('active', b === btn)); renderAdminContent(); });
  renderAdminContent();
}

async function loadAdminData() {
  if (!isAdmin()) return;
  await loadAllMaster();
  const { data, error } = await sb.from('fuel_profiles').select('*').order('created_at', { ascending: false });
  if (!error) state.master.profiles = data || [];
  renderAdmin();
}

function renderAdminContent() {
  const tab = state.activeAdminTab;
  if (tab === 'tandon') return renderMasterCrud('tandon', 'fuel_master_tandon', state.master.tandon, ['tandon_code', 'tandon_name', 'status'], 'Tandon');
  if (tab === 'unit') return renderMasterCrud('unit', 'fuel_master_fuel_truck', state.master.fuelTruck, ['unit_code', 'unit_name', 'unit_type', 'status'], 'Fuel Truck');
  if (tab === 'jalur') return renderMasterCrud('jalur', 'fuel_master_jalur', state.master.jalur, ['jalur_code', 'jalur_name', 'sort_order', 'status'], 'Jalur');
  if (tab === 'fm') return renderFmSettingsAdmin();
  if (tab === 'tera') return renderTeraDbAdmin();
  if (tab === 'users') return renderUsersAdmin();
}

function renderMasterCrud(kind, table, rows, fields, title) {
  const statusOptions = `<option value="ACTIVE">ACTIVE</option><option value="INACTIVE">INACTIVE</option>`;
  $('adminContent').innerHTML = `
    <form id="masterForm" class="card" style="box-shadow:none">
      <div class="card-header"><h2>Form ${esc(title)}</h2></div>
      <div class="card-body"><input id="masterId" type="hidden" />
        <div class="grid-3">${fields.map((f) => `
          <div class="field"><label>${esc(f)}</label>${f === 'status' ? `<select id="m_${f}">${statusOptions}</select>` : `<input id="m_${f}" ${f === 'sort_order' ? 'type="number"' : 'type="text"'} required />`}</div>`).join('')}</div>
        <div class="toolbar"><button class="btn secondary" id="btnMasterReset" type="button">Reset</button><button class="btn primary" type="submit">Simpan ${esc(title)}</button></div>
      </div>
    </form>
    <div class="toolbar"><input id="adminSearch" class="search" type="search" placeholder="Search ${esc(title)}..." /><button id="btnAdminCsv" class="btn secondary" type="button">Download CSV</button></div>
    <div id="adminTable"></div>`;
  const renderRows = () => {
    const q = normalize($('adminSearch').value);
    const filtered = rows.filter((r) => !q || fields.some((f) => normalize(r[f]).includes(q)));
    $('adminTable').innerHTML = tableHtml(filtered, fields.map((f) => [f, f]), (r) => `<button class="btn secondary" data-edit="${r.id}" type="button">Edit</button>`);
    qsa('[data-edit]').forEach((b) => b.onclick = () => fillMasterForm(rows.find((r) => r.id === b.dataset.edit), fields));
  };
  $('adminSearch').oninput = renderRows;
  $('btnAdminCsv').onclick = () => exportCsv(`${kind}-master.csv`, rows);
  $('btnMasterReset').onclick = () => $('masterForm').reset();
  $('masterForm').onsubmit = async (event) => {
    event.preventDefault();
    const payload = { site_code: SITE_CODE, updated_by: state.user.id };
    fields.forEach((f) => payload[f] = $(`m_${f}`).value || null);
    if (payload.sort_order !== undefined) payload.sort_order = Number(payload.sort_order || 1);
    const id = $('masterId').value;
    const res = id ? await sb.from(table).update(payload).eq('id', id).select('*').single() : await sb.from(table).insert(payload).select('*').single();
    if (res.error) return showToast(res.error.message, 'error');
    showToast(`${title} berhasil disimpan.`, 'success');
    await loadAllMaster();
    renderAdminContent();
    renderTransfer();
    renderMonitoring();
  };
  renderRows();
}

function fillMasterForm(row, fields) {
  $('masterId').value = row.id;
  fields.forEach((f) => { const el = $(`m_${f}`); if (el) el.value = row[f] ?? ''; });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderFmSettingsAdmin() {
  const rows = state.master.fmSettings.map((s) => ({ ...s, jalur_code: state.master.jalur.find((j) => j.id === s.jalur_id)?.jalur_code || '-' }));
  $('adminContent').innerHTML = `
    <div class="toolbar"><input id="adminSearch" class="search" type="search" placeholder="Search jalur/mode..." /><button id="btnAdminCsv" class="btn secondary" type="button">Download CSV</button></div>
    <div id="adminTable"></div>`;
  const renderRows = () => {
    const q = normalize($('adminSearch').value);
    const filtered = rows.filter((r) => !q || normalize(`${r.jalur_code}${r.mode}${r.fm_awal_manual}`).includes(q));
    $('adminTable').innerHTML = tableHtml(filtered, [
      ['jalur_code', 'Jalur'], ['mode', 'Mode'], ['fm_awal_manual', 'FM Manual', formatNumber], ['notes', 'Catatan']
    ], (r) => `<button class="btn secondary" data-fm="${r.id}" type="button">Edit</button>`);
    qsa('[data-fm]').forEach((b) => b.onclick = () => editFmSetting(rows.find((r) => r.id === b.dataset.fm)));
  };
  $('adminSearch').oninput = renderRows;
  $('btnAdminCsv').onclick = () => exportCsv('fm-awal-settings.csv', rows);
  renderRows();
}

async function editFmSetting(row) {
  const mode = prompt(`Mode FM Awal ${row.jalur_code}: AUTO atau MANUAL`, row.mode || 'AUTO');
  if (!mode) return;
  const normalizedMode = mode.toUpperCase() === 'MANUAL' ? 'MANUAL' : 'AUTO';
  let manual = null;
  if (normalizedMode === 'MANUAL') {
    const raw = prompt('Isi FM Awal Manual', row.fm_awal_manual ?? '');
    manual = parseNumber(raw);
    if (manual === null) return showToast('FM Awal Manual wajib angka.', 'error');
  }
  const { error } = await sb.from('fuel_fm_awal_settings').update({ mode: normalizedMode, fm_awal_manual: manual, updated_by: state.user.id, notes: normalizedMode === 'MANUAL' ? 'Dikunci manual admin.' : 'Otomatis dari transaksi terakhir.' }).eq('id', row.id);
  if (error) return showToast(error.message, 'error');
  showToast('Setting FM Awal berhasil disimpan.', 'success');
  await loadAllMaster();
  renderFmSettingsAdmin();
}


async function renderTeraDbAdmin() {
  $('adminContent').innerHTML = `
    <div class="card" style="box-shadow:none">
      <div class="card-header">
        <div><h2>Database Tera Tangki</h2><p>Data Tera disimpan di Supabase. Frontend hanya memanggil RPC per unit supaya input HP lebih ringan.</p></div>
      </div>
      <div class="card-body">
        <div class="toolbar">
          <button id="btnTeraStatus" class="btn secondary" type="button">Cek Status DB</button>
          <button id="btnImportTera" class="btn primary" type="button">Import Tera Tangki ke Database</button>
          <button id="btnTeraCsv" class="btn secondary" type="button">Download CSV Status</button>
        </div>
        <div id="teraDbStatus" class="record-card">Klik Cek Status DB untuk melihat jumlah data Tera di Supabase.</div>
        <div id="teraDbTable"></div>
      </div>
    </div>`;
  $('btnTeraStatus').onclick = loadTeraDbStatus;
  $('btnImportTera').onclick = importTeraTangkiToDatabase;
  $('btnTeraCsv').onclick = async () => {
    const rows = await getTeraDbRows();
    exportCsv('tera-db-status.csv', rows);
  };
  await loadTeraDbStatus();
}

async function getTeraDbRows() {
  const { data, error } = await sb
    .from('fuel_tera_tangki_grid')
    .select('unit_code,source_label,point_count,max_dip,dip_step,source_file,updated_at')
    .order('unit_code', { ascending: true });
  if (error) throw error;
  return data || [];
}

async function loadTeraDbStatus() {
  try {
    const rows = await getTeraDbRows();
    $('teraDbStatus').innerHTML = `Total unit Tera di database: <b>${rows.length}</b>`;
    $('teraDbTable').innerHTML = tableHtml(rows, [
      ['unit_code', 'Unit'], ['source_label', 'Label Sheet'], ['point_count', 'Point'], ['max_dip', 'Max Dip', formatNumber], ['updated_at', 'Updated']
    ]);
  } catch (error) {
    $('teraDbStatus').textContent = error.message || 'Gagal cek Tera DB.';
  }
}

async function importTeraTangkiToDatabase() {
  const btn = $('btnImportTera');
  if (!confirm('Import data Tera Tangki dari assets ke Supabase? Data lama untuk unit yang sama akan ditimpa.')) return;
  setBusy(btn, true, 'Importing...');
  try {
    const tera = await loadTeraTangkiAsset();
    const unitMap = new Map(state.master.fuelTruck.map((u) => [normalize(u.unit_code), u]));
    const rows = Object.entries(tera.units || {}).map(([unitCode, profile]) => {
      const unit = unitMap.get(normalize(unitCode));
      return {
        site_code: SITE_CODE,
        fuel_truck_id: unit?.id || null,
        unit_code: unitCode,
        source_label: String(profile.sourceLabel || unitCode),
        dip_min: Number(profile.dipMin || 0),
        dip_step: Number(profile.dipStep || 0.1),
        point_count: Array.isArray(profile.volumes) ? profile.volumes.length : 0,
        max_dip: Number(profile.maxDip || 0),
        volumes_json: profile.volumes || [],
        source_sheet: tera.sourceSheet || 'Tera Tangki',
        source_file: tera.sourceFile || 'NEW HITUNG TERA BULAN JUNI 2026.xlsx'
      };
    }).filter((r) => r.point_count > 1);

    for (let i = 0; i < rows.length; i += 3) {
      const chunk = rows.slice(i, i + 3);
      // fuel_tera_tangki_grid di Supabase hanya punya PK 'id' (no UNIQUE di site_code+unit_code).
      // Untuk re-import reliable, pakai pattern delete-then-insert per batch.
      const unitCodes = chunk.map(r => r.unit_code);
      const { error: delErr } = await sb.from('fuel_tera_tangki_grid')
        .delete().in('unit_code', unitCodes);
      if (delErr) throw delErr;
      // Strip 'id' field supaya PostgREST auto-generate UUID baru
      const cleanChunk = chunk.map(({ id, ...rest }) => rest);
      const { error } = await sb.from('fuel_tera_tangki_grid').insert(cleanChunk);
      if (error) throw error;
      $('teraDbStatus').textContent = `Import progress ${Math.min(i + chunk.length, rows.length)} / ${rows.length} unit...`;
    }
    showToast(`Import Tera selesai: ${rows.length} unit masuk database.`, 'success');
    state.teraTangki = null;
    await loadTeraDbStatus();
  } catch (error) {
    showToast(error.message || 'Import Tera gagal.', 'error');
  } finally {
    setBusy(btn, false);
  }
}

function renderUsersAdmin() {
  const rows = state.master.profiles || [];
  $('adminContent').innerHTML = `
    <div class="toolbar"><input id="adminSearch" class="search" type="search" placeholder="Search user/nama/NRP/role..." /><button id="btnAdminCsv" class="btn secondary" type="button">Download CSV</button></div>
    <div id="adminTable"></div>`;
  const renderRows = () => {
    const q = normalize($('adminSearch').value);
    const filtered = rows.filter((r) => !q || normalize(`${r.full_name}${r.email}${profileNrp(r)}${r.role}${r.status}`).includes(q));
    $('adminTable').innerHTML = tableHtml(filtered, [['full_name', 'Nama'], ['nrp', 'NRP', (v, r) => esc(profileNrp(r) || '-')], ['email', 'Email'], ['role', 'Role'], ['status', 'Status']], (r) => `<button class="btn secondary" data-user="${r.id}" type="button">Edit Role</button>`);
    qsa('[data-user]').forEach((b) => b.onclick = () => editUserRole(rows.find((r) => r.id === b.dataset.user)));
  };
  $('adminSearch').oninput = renderRows;
  $('btnAdminCsv').onclick = () => exportCsv('users.csv', rows);
  renderRows();
}

async function editUserRole(row) {
  const role = prompt('Role: SUPER_ADMIN / ADMIN / SUPERVISOR / FIELD', row.role || 'FIELD');
  if (!role) return;
  const status = prompt('Status: ACTIVE / INACTIVE', row.status || 'ACTIVE');
  const payload = { role: role.toUpperCase(), status: (status || 'ACTIVE').toUpperCase() };
  const { error } = await sb.from('fuel_profiles').update(payload).eq('id', row.id);
  if (error) return showToast(error.message, 'error');
  showToast('Role user berhasil diperbarui.', 'success');
  const { data, error: reloadError } = await sb.from('fuel_profiles').select('*').order('created_at', { ascending: false });
  if (!reloadError) state.master.profiles = data || [];
  if (state.activeView === 'Users') renderUsersPage();
  else renderAdmin();
}

function tableHtml(rows, columns, actionRenderer) {
  if (!rows.length) return '<div class="record-card">Tidak ada data.</div>';
  return `<div class="table-wrap"><table><thead><tr>${columns.map((c) => `<th>${esc(c[1])}</th>`).join('')}${actionRenderer ? '<th>Aksi</th>' : ''}</tr></thead><tbody>${rows.map((r) => `<tr>${columns.map((c) => `<td data-label="${esc(c[1])}">${c[2] ? c[2](r[c[0]], r) : esc(r[c[0]] ?? '-')}</td>`).join('')}${actionRenderer ? `<td class="actions-cell">${actionRenderer(r)}</td>` : ''}</tr>`).join('')}</tbody></table></div>`;
}

function exportCsv(filename, rows) {
  if (!rows || !rows.length) return showToast('Tidak ada data untuk didownload.', 'error');
  const flatRows = rows.map((row) => {
    const out = {};
    Object.entries(row).forEach(([k, v]) => {
      if (typeof v !== 'object' || v === null) out[k] = v;
    });
    return out;
  });
  const headers = Object.keys(flatRows[0]);
  const csv = [headers.join(',')].concat(flatRows.map((r) => headers.map((h) => csvCell(r[h])).join(','))).join('\n');
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8;' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

function csvCell(value) {
  const text = String(value ?? '');
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}


/* PATCH R1 — route mapping master, no admin FM Awal menu */
function patchRouteNumber(code){const m=String(code||'').match(/(\d+)\s*$/);return m?Number(m[1]):null;}
function patchAllowedRoute(code){return [1,2,3,5,6,7].includes(patchRouteNumber(code));}
function patchRoutePurpose(code){const n=patchRouteNumber(code);return n&&n<=3?'TRANSFER':'RECEIVING';}
function patchLatestRouteRows(purpose=''){
  const jalurById=Object.fromEntries((state.master.jalur||[]).map(j=>[String(j.id),j]));
  const tankById=Object.fromEntries((state.master.tandon||[]).map(t=>[String(t.id),t]));
  const rows=(state.master.routeConfig||[]).slice().sort((a,b)=>String(b.updated_at||b.created_at||b.tanggal||'').localeCompare(String(a.updated_at||a.created_at||a.tanggal||'')));
  const seen=new Set(),out=[];for(const r of rows){const j=jalurById[String(r.jalur_id)],code=j?.jalur_code||j?.kode||'';if(!patchAllowedRoute(code))continue;const per=patchRoutePurpose(code);if(purpose&&per!==purpose)continue;if(String(r.status||'VALIDATED').toUpperCase()==='INACTIVE')continue;const key=String(r.jalur_id);if(seen.has(key))continue;seen.add(key);const t=tankById[String(r.tandon_id)];out.push({...r,peruntukan:per,jalur_code:code,tandon_code:t?.tandon_code||t?.kode||''});}return out.sort((a,b)=>(patchRouteNumber(a.jalur_code)||99)-(patchRouteNumber(b.jalur_code)||99));
}
async function loadActiveTransferRoutes(){
  const hint=$('tfRouteHint');if(hint)hint.textContent='Memuat master mapping Jalur 1–3…';
  const {data,error}=await sb.from('fuel_v_route_config').select('*').eq('site_code',SITE_CODE).eq('peruntukan','TRANSFER').order('jalur_code',{ascending:true});
  if(error){state.master.routeConfig=[];$('tfJalur').innerHTML='<option value="">Mapping jalur belum tersedia</option>';$('tfJalur').disabled=true;$('tfTandon').value='';if(hint)hint.textContent='Gagal membaca mapping: '+error.message;return;}
  const latest=[],seen=new Set();(data||[]).filter(r=>patchAllowedRoute(r.jalur_code)&&patchRoutePurpose(r.jalur_code)==='TRANSFER').slice().reverse().forEach(r=>{if(!seen.has(String(r.jalur_id))){seen.add(String(r.jalur_id));latest.push(r)}});latest.reverse();state.master.routeConfig=latest;$('tfJalur').disabled=!latest.length;const cur=$('tfJalur').value;$('tfJalur').innerHTML='<option value="">Pilih Jalur 1–3</option>'+latest.map(r=>`<option value="${esc(r.jalur_id)}" ${String(r.jalur_id)===String(cur)?'selected':''}>${esc(r.jalur_code)} → ${esc(r.tandon_code)}</option>`).join('');if(latest.length===1)$('tfJalur').value=latest[0].jalur_id;applySelectedTransferRoute();if(hint)hint.textContent=latest.length?`${latest.length} master mapping TRANSFER tersedia.`:'Belum ada mapping Jalur 1–3.';if($('tfJalur').value)await loadDefaultFmAwal();
}
function applySelectedTransferRoute(){const route=(state.master.routeConfig||[]).find(r=>String(r.jalur_id)===String($('tfJalur').value));$('tfTandon').value=route?.tandon_id||'';const hint=$('tfRouteHint');if(hint&&route)hint.textContent=`${route.jalur_code} → ${route.tandon_code}. Mapping master Admin.`;}
function renderAdminContent(){const tab=state.activeAdminTab;if(tab==='tandon')return renderMasterCrud('tandon','fuel_master_tandon',state.master.tandon,['tandon_code','tandon_name','status'],'Tandon');if(tab==='unit')return renderMasterCrud('unit','fuel_master_fuel_truck',state.master.fuelTruck,['unit_code','unit_name','unit_type','status'],'Fuel Truck');if(tab==='jalur')return renderRouteConfigAdmin();if(tab==='tera')return renderTeraDbAdmin();if(tab==='users')return renderUsersAdmin();}
function renderRouteConfigAdmin(){
  const jalur=(state.master.jalur||[]).filter(j=>patchAllowedRoute(j.jalur_code||j.kode)).sort((a,b)=>patchRouteNumber(a.jalur_code||a.kode)-patchRouteNumber(b.jalur_code||b.kode)),tanks=(state.master.tandon||[]).filter(t=>!t.status||t.status==='ACTIVE'),latest=patchLatestRouteRows(),byJalur=Object.fromEntries(latest.map(r=>[String(r.jalur_id),r]));
  $('adminContent').innerHTML=`<div class="card" style="box-shadow:none"><div class="card-header"><div><h2>Konfigurasi Jalur → Tandon</h2><p>Master permanen: Jalur 1–3 Transfer, Jalur 5–7 Penerimaan. Tanpa tanggal, shift, status, atau FM Awal.</p></div></div><div class="card-body"><div class="card-list">${jalur.map(j=>{const code=j.jalur_code||j.kode,r=byJalur[String(j.id)]||{},per=patchRoutePurpose(code);return `<div class="record-card"><div class="row"><span><b>${esc(code)}</b><small>${per}</small></span><span><select class="route-tank-select" data-route-id="${esc(j.id)}">${tanks.map(t=>`<option value="${esc(t.id)}" ${String(t.id)===String(r.tandon_id)?'selected':''}>${esc(t.tandon_code||t.kode)}</option>`).join('')}</select></span></div><div class="toolbar"><div style="flex:1"></div><button class="btn primary" data-save-route="${esc(j.id)}" type="button">Simpan Mapping</button></div></div>`}).join('')}</div></div></div>`;
  qsa('[data-save-route]').forEach(btn=>btn.onclick=async()=>{const jalurId=btn.dataset.saveRoute,j=(state.master.jalur||[]).find(x=>String(x.id)===String(jalurId)),code=j?.jalur_code||j?.kode||'',tankId=document.querySelector(`.route-tank-select[data-route-id="${jalurId}"]`)?.value,existing=byJalur[String(jalurId)];if(!j||!tankId)return showToast('Pilih tandon.','error');const payload={site_code:SITE_CODE,tanggal:existing?.tanggal||'2000-01-01',shift:existing?.shift||'SHIFT_1',jalur_id:jalurId,tandon_id:tankId,peruntukan:patchRoutePurpose(code),status:'VALIDATED',fm_akhir_shift_sebelumnya:existing?.fm_akhir_shift_sebelumnya??null,fm_aktual_awal:existing?.fm_aktual_awal??null,notes:'MASTER_ROUTE_MAPPING'};const res=existing?await sb.from('fuel_route_config').update(payload).eq('id',existing.id).select('*').single():await sb.from('fuel_route_config').insert(payload).select('*').single();if(res.error)return showToast(res.error.message,'error');await loadAllMaster();showToast(`${code} mapping tersimpan.`,'success');renderRouteConfigAdmin();});
}

init();
