/* FCC V12.3.1 — FAST IMPORT FRONTEND RUNTIME PATCH
 * Scope: Reporting > Upload Data only.
 * Backend V12.3 already supports parse-once cache tokens; this patch makes the
 * browser use that contract so Commit never re-uploads/re-parses the workbook.
 */
(() => {
  'use strict';

  const PATCH = 'V12.3.1-FAST-FRONTEND-20260814';
  document.documentElement.dataset.fccFastImportFrontend = PATCH;

  const monthValueFast = () => state.reporting?.period || todayISO().slice(0, 7);
  const emptyFast = (title, message) => `<div class="reporting-empty"><strong>${esc(title)}</strong><span>${esc(message)}</span></div>`;
  const litersFast = (value) => `${new Intl.NumberFormat('id-ID', { maximumFractionDigits: 3 }).format(Number(value || 0))} L`;
  const msFast = (value) => Number.isFinite(Number(value)) ? `${new Intl.NumberFormat('id-ID', { maximumFractionDigits: 1 }).format(Number(value))} ms` : '-';
  const secFast = (value) => Number.isFinite(Number(value)) ? `${new Intl.NumberFormat('id-ID', { maximumFractionDigits: 2 }).format(Number(value) / 1000)} dtk` : '-';
  const statusFast = (value, tone = '') => `<span class="report-status ${tone || 'neutral'}">${esc(value || '-')}</span>`;

  state.reporting = state.reporting || {};
  state.reporting.uploadPreview = state.reporting.uploadPreview || { SS6: null, SAP: null };
  state.reporting.uploadFastSignature = state.reporting.uploadFastSignature || { SS6: '', SAP: '' };

  function fileSignature(source) {
    const file = $(`file${source}`)?.files?.[0];
    return file ? `${file.name}|${file.size}|${file.lastModified}` : '';
  }

  function resetSource(source) {
    state.reporting.uploadPreview[source] = null;
    state.reporting.uploadFastSignature[source] = '';
    renderFastPreview(source);
    updateFastCommitState(source);
  }

  function updateFastCommitState(source) {
    const button = $(`commit${source}`);
    const preview = state.reporting.uploadPreview[source];
    if (!button) return;
    const sameFile = Boolean(state.reporting.uploadFastSignature[source]) && state.reporting.uploadFastSignature[source] === fileSignature(source);
    const tokenReady = Boolean(preview?.validation_token);
    button.disabled = !(sameFile && tokenReady && Boolean(preview?.commit_allowed) && Number(preview?.valid_rows || 0) > 0);
    button.title = tokenReady ? 'Commit memakai validation token; file tidak di-upload/parse ulang.' : 'FAST Validate terlebih dahulu.';
  }

  async function fastMultipartRequest(path, formData) {
    let response;
    try {
      response = await fetch(path, { method: 'POST', credentials: 'include', body: formData });
    } catch (error) {
      let healthOk = false;
      let healthStatus = '';
      try {
        const health = await fetch('/api/v1/health', { credentials: 'include', cache: 'no-store' });
        healthOk = health.ok;
        healthStatus = `HTTP ${health.status}`;
      } catch (healthError) {
        healthStatus = healthError?.message || 'tidak terhubung';
      }
      if (!healthOk) {
        throw new Error(`API FCC tidak dapat dijangkau (${healthStatus}). Periksa service/reverse proxy. Detail: ${error?.message || 'Failed to fetch'}`);
      }
      throw new Error(`API FCC aktif tetapi request terputus. Periksa reverse-proxy timeout/limit upload. Detail: ${error?.message || 'Failed to fetch'}`);
    }
    let payload = null;
    try { payload = await response.json(); } catch { payload = {}; }
    if (!response.ok) {
      const detail = payload?.detail;
      const message = typeof detail === 'string' ? detail : (detail?.message || payload?.message || `HTTP ${response.status}`);
      throw new Error(message);
    }
    return payload;
  }

  function uploadCardFast(source) {
    const note = source === 'SS6'
      ? 'SS6 Refueling (.xls/.xlsx) · Transaction ID, Unit, Date, Shift, Vol.'
      : 'SAP MB51 (.xlsx/.xls) · Posting Date, signed Qty, Movement Type, Order/Text.';
    return `<div class="upload-card">
      <h3>${source === 'SAP' ? 'SAP MB51' : 'SS6'} Upload</h3>
      <p>${note}</p>
      <div class="field"><label>File Excel <span class="required">*</span></label><input id="file${source}" type="file" accept=".xlsx,.xls"></div>
      <div class="toolbar"><button id="validate${source}" class="btn secondary" type="button">1. FAST Validate</button><button id="commit${source}" class="btn primary" type="button" disabled>2. FAST Commit</button></div>
      <div id="preview${source}" class="upload-preview">${emptyFast('Belum divalidasi', 'Pilih file lalu tekan FAST Validate.')}</div>
    </div>`;
  }

  async function loadFastEngineStatus() {
    const root = $('uploadEngineStatus');
    if (!root) return;
    try {
      const response = await fetch('/api/v1/health', { credentials: 'include', cache: 'no-store' });
      const data = await response.json();
      const info = data?.reporting_import || {};
      const fast = info.fast_excel_engine === 'CALAMINE';
      const tokenMode = info.validation_mode === 'PARSE_ONCE_CACHE_TOKEN';
      const commitReady = Boolean(info.commit_ready);
      const ready = response.ok && tokenMode && commitReady;
      root.className = `dashboard-summary-note mt-8 ${ready ? 'report-delta-zero' : 'report-delta-nonzero'}`;
      root.innerHTML = `<b>${ready ? 'FAST IMPORT READY' : 'IMPORT CHECK REQUIRED'}</b> · Parser <b>${esc(info.fast_excel_engine || 'UNKNOWN')}</b> · Validate <b>${esc(info.validation_mode || '-')}</b> · Commit <b>${esc(info.commit_engine || '-')}</b> · XLS ${info.xls_supported ? 'READY' : 'NOT READY'}${fast ? '' : ' · Calamine fallback aktif'}`;
    } catch (error) {
      root.className = 'dashboard-summary-note mt-8 report-delta-nonzero';
      root.textContent = 'Status FAST Import tidak dapat dibaca. Periksa /api/v1/health.';
    }
  }

  function renderFastPreview(source) {
    const root = $(`preview${source}`);
    const data = state.reporting.uploadPreview[source];
    if (!root) return;
    if (!data) {
      root.innerHTML = emptyFast('Belum divalidasi', 'Pilih file lalu tekan FAST Validate.');
      return;
    }
    const t = data.timings_ms || {};
    const ct = data.commit_timings_ms || {};
    const unmapped = data.sample_unmapped || [];
    const ambiguous = data.sample_ambiguous || [];
    const rejected = data.sample_rejected || data.rejected_sample || [];
    const topAmbiguous = data.top_ambiguous_aliases || [];
    const tokenReady = Boolean(data.validation_token);
    const cacheNote = tokenReady
      ? `<div class="dashboard-summary-note report-delta-zero"><b>FAST Commit ready.</b> File tidak akan di-upload atau di-parse ulang. Cache ${formatNumber(data.validation_cache_bytes || 0)} byte · mode ${esc(data.commit_mode || 'TOKEN_NO_REPARSE')}.</div>`
      : `<div class="dashboard-summary-note report-delta-nonzero"><b>Validation token tidak diterima.</b> Backend belum memakai kontrak V12.3; FAST Commit dinonaktifkan.</div>`;
    const commitNote = data.status === 'COMMITTED'
      ? `<div class="dashboard-summary-note report-delta-zero"><b>COMMITTED.</b> Cache hit: ${data.commit_cache_hit ? 'YA' : 'TIDAK'} · DB COPY ${msFast(ct.database_copy)} · total ${secFast(ct.total_server)}.</div>`
      : '';
    root.innerHTML = `
      <div class="dashboard-summary-note"><b>${esc(data.source_format || source)}</b> · Parser ${esc(data.parser_engine || '-')} · Periode ${esc(data.period || '-')} · Coverage ${esc(data.date_from || '-')} s/d ${esc(data.date_to || '-')} · Mapping ${formatNumber(data.mapping_coverage_pct || 0)}%</div>
      <div class="upload-result-kpis">
        <div class="upload-mini"><span>Total</span><strong>${formatNumber(data.total_rows || 0)}</strong></div>
        <div class="upload-mini"><span>Mapped</span><strong class="report-delta-zero">${formatNumber(data.mapped_rows || 0)}</strong></div>
        <div class="upload-mini"><span>Unmapped</span><strong class="${Number(data.unmapped_rows || 0) ? 'report-delta-nonzero' : 'report-delta-zero'}">${formatNumber(data.unmapped_rows || 0)}</strong></div>
        <div class="upload-mini"><span>Ambiguous</span><strong class="${Number(data.ambiguous_rows || 0) ? 'warning-text' : 'report-delta-zero'}">${formatNumber(data.ambiguous_rows || 0)}</strong></div>
        <div class="upload-mini"><span>Tech Reject</span><strong class="${Number(data.rejected_rows || 0) ? 'report-delta-nonzero' : 'report-delta-zero'}">${formatNumber(data.rejected_rows || 0)}</strong></div>
      </div>
      ${data.timings_ms ? `<div class="dashboard-summary-note"><b>Timing server:</b> upload ${msFast(t.read_upload)} · parse ${msFast(t.parse_excel)} · mapping ${msFast(t.map_validate)} · cache ${msFast(t.cache_for_commit)} · total <b>${secFast(t.total_server)}</b></div>` : ''}
      ${cacheNote}${commitNote}
      ${Number(data.unmapped_rows || 0) ? `<div class="dashboard-summary-note"><b>UNMAPPED:</b> disimpan sebagai raw exception dan tidak ikut reconciliation. ${(data.top_unmapped_aliases || []).slice(0, 6).map(x => `${esc(x.alias)} (${formatNumber(x.rows)})`).join(' · ')}</div>` : ''}
      ${Number(data.ambiguous_rows || 0) ? `<div class="dashboard-summary-note warning-text"><b>AMBIGUOUS:</b> boleh di-commit sebagai raw master-data exception, tidak di-auto-guess dan tidak ikut reconciliation. ${topAmbiguous.slice(0, 6).map(x => `${esc(x.alias)} (${formatNumber(x.rows)})`).join(' · ')}</div>` : ''}
      ${unmapped.length ? tableHtml(unmapped.slice(0, 5), [['source_row','Row'],['alias_unit','Alias'],['volume_net_l','Net L',v=>esc(litersFast(v))],['reason','Status',v=>statusFast(v,'warning')]]) : ''}
      ${ambiguous.length ? tableHtml(ambiguous.slice(0, 5), [['source_row','Row'],['alias_unit','Alias'],['volume_net_l','Net L',v=>esc(litersFast(v))],['reason','Status',v=>statusFast(v,'warning')]]) : ''}
      ${rejected.length ? `<div class="dashboard-summary-note report-delta-nonzero"><b>Technical reject memblokir Commit.</b></div>${tableHtml(rejected.slice(0, 8), [['source_row','Row'],['source_record_id','Source ID'],['alias_unit','Alias'],['reason','Reason',v=>statusFast(v,'critical')]])` : '<div class="dashboard-summary-note report-delta-zero">Tidak ada technical reject. Commit diizinkan.</div>'}`;
  }

  async function loadFastBatchHistory() {
    const root = $('uploadBatchTable');
    if (!root) return;
    try {
      const result = await http('GET', '/api/v1/import/batches');
      const rows = (result?.data || []).filter(row => String(row.periode || '') === monthValueFast()).slice(0, 30);
      root.innerHTML = rows.length ? tableHtml(rows, [
        ['sumber','Source',v=>`<strong>${esc(v)}</strong>`],
        ['source_format','Format'],
        ['nama_file','File'],
        ['date_from','Coverage',(_,r)=>`${esc(r.date_from || '-')} → ${esc(r.date_to || '-')}`],
        ['baris_mapped','Mapped',v=>esc(formatNumber(v || 0))],
        ['baris_unmapped','Unmapped',v=>esc(formatNumber(v || 0))],
        ['baris_ambiguous','Ambiguous',v=>esc(formatNumber(v || 0))],
        ['baris_tolak','Reject',v=>esc(formatNumber(v || 0))],
        ['status','Status',v=>statusFast(v, String(v).toUpperCase()==='COMMITTED' ? 'match' : 'info')]
      ]) : emptyFast('Belum ada batch', 'Upload SS6/SAP untuk periode terpilih.');
    } catch (error) {
      root.innerHTML = emptyFast('Batch history gagal dimuat', error.message || '');
    }
  }

  async function runFastImport(source, action, button) {
    const file = $(`file${source}`)?.files?.[0];
    const preview = state.reporting.uploadPreview[source];
    const requestedPeriod = $('uploadPeriod')?.value || monthValueFast();

    if (action === 'validate' && !file) return showToast(`Pilih file ${source} terlebih dahulu.`, 'error');
    if (action === 'commit') {
      const sameFile = state.reporting.uploadFastSignature[source] === fileSignature(source);
      if (!file || !sameFile || !preview?.validation_token) return showToast(`FAST Validate ${source} untuk file ini terlebih dahulu.`, 'error');
      if (!preview?.commit_allowed) return showToast('Commit diblokir oleh technical reject. Review hasil Validate.', 'error');
      const message = [
        `Commit ${source} periode ${preview.period || requestedPeriod}?`,
        `${formatNumber(preview.mapped_rows || 0)} MAPPED`,
        `${formatNumber(preview.unmapped_rows || 0)} UNMAPPED`,
        `${formatNumber(preview.ambiguous_rows || 0)} AMBIGUOUS`,
        'File TIDAK akan di-upload/parse ulang.'
      ].join('\n');
      if (!confirm(message)) return;
    }

    setBusy(button, true, action === 'validate' ? 'FAST Validate…' : 'FAST Commit…');
    try {
      const form = new FormData();
      form.append('source', source);
      form.append('period', action === 'commit' ? (preview?.period || requestedPeriod) : requestedPeriod);
      if (action === 'validate') {
        form.append('file', file, file.name);
      } else {
        form.append('validation_token', preview.validation_token);
      }
      const result = await fastMultipartRequest(`/api/v1/import/reconciliation/${action}`, form);

      if (result?.period && $('uploadPeriod') && $('uploadPeriod').value !== result.period) {
        $('uploadPeriod').value = result.period;
        state.reporting.period = result.period;
      }

      if (action === 'validate') {
        state.reporting.uploadPreview[source] = result;
        state.reporting.uploadFastSignature[source] = result.commit_allowed && result.validation_token ? fileSignature(source) : '';
        if (!result.validation_token) showToast(`${source}: Validate selesai tetapi validation_token tidak tersedia; FAST Commit dinonaktifkan.`, 'error');
        else showToast(`${source}: FAST Validate selesai ${secFast(result.timings_ms?.total_server)} · ${formatNumber(result.mapped_rows || 0)} mapped · ${formatNumber(result.unmapped_rows || 0)} unmapped · ${formatNumber(result.ambiguous_rows || 0)} ambiguous.`, 'success');
      } else {
        state.reporting.uploadPreview[source] = result;
        state.reporting.uploadFastSignature[source] = '';
        showToast(`${source} COMMITTED · cache hit ${result.commit_cache_hit ? 'YA' : 'TIDAK'} · ${formatNumber(result.committed_rows || 0)} rows.`, 'success');
        state.reporting.overview = null;
        state.reporting.monthly = null;
        state.reporting.reconciliation = null;
        state.reporting.exceptions = null;
        await loadFastBatchHistory();
      }
      renderFastPreview(source);
      updateFastCommitState(source);
    } catch (error) {
      if (action === 'validate') state.reporting.uploadFastSignature[source] = '';
      updateFastCommitState(source);
      showToast(error.message || `${source} ${action} gagal.`, 'error');
      const root = $(`preview${source}`);
      if (root) root.innerHTML = `<div class="reporting-empty"><strong>${action === 'validate' ? 'Validasi' : 'Commit'} gagal</strong><span>${esc(error.message || '')}</span></div>`;
    } finally {
      setBusy(button, false);
      updateFastCommitState(source);
    }
  }

  function renderFastUploadDataPage() {
    const root = $('viewUploadData');
    if (!root) return;
    if (!isAdmin()) {
      root.innerHTML = emptyFast('Akses ditolak', 'Upload data hanya untuk ADMIN/SUPER_ADMIN.');
      return;
    }
    root.innerHTML = `<div class="reporting-hero">
      <div><span class="module-kicker">V12.3.1 FAST IMPORT</span><h2>Upload SS6 Refueling & SAP MB51</h2><p><b>Validate = upload + parse sekali.</b> Commit memakai validation token dari cache; workbook tidak di-upload/parse ulang. UNMAPPED dan AMBIGUOUS tetap disimpan sebagai raw exception dan tidak ikut reconciliation.</p><div id="uploadEngineStatus" class="dashboard-summary-note mt-8">Memeriksa FAST Import engine…</div></div>
      <div class="field reporting-period"><label>Periode Upload <span class="hint">(auto dari file)</span></label><input id="uploadPeriod" type="month" value="${esc(monthValueFast())}"><small class="hint">Tanggal di workbook adalah source of truth; periode UI otomatis dikoreksi saat Validate.</small></div>
    </div>
    <div class="upload-grid">${uploadCardFast('SS6')}${uploadCardFast('SAP')}</div>
    <section class="card"><div class="card-header"><div><h2>Batch History</h2><p>COMMITTED = source aktif. Re-upload source + periode yang sama membuat batch lama SUPERSEDED.</p></div><button id="uploadBatchRefresh" class="btn secondary" type="button">Refresh</button></div><div id="uploadBatchTable"></div></section>`;

    $('uploadPeriod').onchange = event => {
      state.reporting.period = event.target.value;
      ['SS6','SAP'].forEach(resetSource);
      loadFastBatchHistory();
    };
    ['SS6','SAP'].forEach(source => {
      $(`validate${source}`).onclick = event => runFastImport(source, 'validate', event.currentTarget);
      $(`commit${source}`).onclick = event => runFastImport(source, 'commit', event.currentTarget);
      $(`file${source}`).onchange = () => resetSource(source);
      updateFastCommitState(source);
    });
    $('uploadBatchRefresh').onclick = loadFastBatchHistory;
    loadFastEngineStatus();
    loadFastBatchHistory();
  }

  globalThis.renderUploadDataPage = renderFastUploadDataPage;

  // Ensure the current reporting switcher resolves the new page even if its
  // original closure was created before this runtime patch loaded.
  const previousSwitch = globalThis.switchView;
  if (typeof previousSwitch === 'function') {
    globalThis.switchView = function patchedSwitchView(view) {
      if (view !== 'UploadData') return previousSwitch(view);
      if (!isAdmin()) return showToast(`Akses UploadData ditolak untuk role ${currentRole()}.`, 'error');
      state.activeView = view;
      if (typeof updateViewChrome === 'function') updateViewChrome(view);
      qsa('.nav-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.view === view));
      if (typeof FIELD_VIEWS !== 'undefined') FIELD_VIEWS.forEach(name => { const el = $(`view${name}`); if (el) el.classList.toggle('hidden', name !== view); });
      renderFastUploadDataPage();
    };
  }

  console.info(`[FCC] ${PATCH} loaded`);
})();
