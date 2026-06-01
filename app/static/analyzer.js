// Analyzer page client logic.
// State is held in `S`. Server is the source of truth for compute() — we
// always re-post csv_blob + overrides + prediction and trust the response.

const S = {
  filename: '',
  meta: {},
  csv_blob: '',
  rows_filtered: [],
  analysis: [],
  overrides: {},        // {row_index: {a?, c?, e?}}
  notes: Array(9).fill(null).map(() => ({ n1: '', n2: '', n3: '' })),
  note_text: '',
  pred_fh: null,
  pred_fa: null,
  real_fh: 0,
  real_fa: 0,
  favorite_home: false, // true if home is chấp (handicap < 0)
  session_id: null,
  edit_mode: false,
};

// ─── DOM helpers ────────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

function toast(msg, kind = 'ok') {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show ' + kind;
  setTimeout(() => t.classList.remove('show'), 3500);
}

function fmtNum(v) {
  if (v === null || v === undefined) return '-';
  if (typeof v === 'number') {
    return Number.isInteger(v) ? String(v) : String(+v.toFixed(4));
  }
  return String(v);
}

function dgLabel(val) {
  if (val === null || val === undefined) return { txt: '-', cls: 'lbl-none' };
  if (Math.abs(val + 0.25) < 1e-9) return { txt: '-1/2', cls: 'lbl-half' };
  if (Math.abs(val - 0.25) < 1e-9) return { txt: '1/2', cls: 'lbl-half' };
  if (val === 0) return { txt: 'Hòa', cls: 'lbl-draw' };
  return val > 0
    ? { txt: 'Thắng', cls: 'lbl-win' }
    : { txt: 'Thua', cls: 'lbl-lose' };
}

// ─── Build the 9-row Diễn Biến table once on load ──────────────────────────

function buildDB() {
  const body = $('db-body');
  body.innerHTML = '';
  for (let i = 0; i < 9; i++) {
    const tr = document.createElement('tr');
    tr.dataset.row = i;
    tr.innerHTML = `
      <td class="sc" data-cell="sc">-</td>
      <td><input type="text" data-cell="a" disabled></td>
      <td class="b" data-cell="b">-</td>
      <td><input type="text" class="note" data-cell="n1"></td>
      <td><input type="text" data-cell="c" disabled></td>
      <td data-cell="d">-</td>
      <td><input type="text" class="note" data-cell="n2"></td>
      <td><input type="text" data-cell="e" disabled></td>
      <td data-cell="g">-</td>
      <td><input type="text" class="note" data-cell="n3"></td>
    `;
    body.appendChild(tr);
  }
  // Wire note inputs to state
  body.querySelectorAll('input.note').forEach((el) => {
    el.addEventListener('input', () => {
      const tr = el.closest('tr');
      const i = +tr.dataset.row;
      const cell = el.dataset.cell;
      S.notes[i] = S.notes[i] || { n1: '', n2: '', n3: '' };
      S.notes[i][cell] = el.value;
    });
  });
  // Wire override inputs — store on blur if edit mode
  body.querySelectorAll('input[data-cell="a"], input[data-cell="c"], input[data-cell="e"]').forEach((el) => {
    el.addEventListener('change', () => {
      if (!S.edit_mode) return;
      const tr = el.closest('tr');
      const i = +tr.dataset.row;
      const cell = el.dataset.cell;
      const s = el.value.trim();
      S.overrides[i] = S.overrides[i] || {};
      if (s === '' || s === '-') {
        delete S.overrides[i][cell];
        if (Object.keys(S.overrides[i]).length === 0) delete S.overrides[i];
      } else {
        const f = parseFloat(s);
        if (isNaN(f)) {
          toast(`Lỗi giá trị cột '${cell}' dòng ${i + 1}`, 'err');
          el.focus();
          return;
        }
        S.overrides[i][cell] = f;
      }
    });
  });
}

// ─── Render analysis into DB table ─────────────────────────────────────────

function renderAnalysis() {
  const rows = $('db-body').children;
  for (let i = 0; i < 9; i++) {
    const tr = rows[i];
    const m = S.analysis[i];
    const cells = {
      sc: tr.querySelector('[data-cell="sc"]'),
      a:  tr.querySelector('[data-cell="a"]'),
      b:  tr.querySelector('[data-cell="b"]'),
      c:  tr.querySelector('[data-cell="c"]'),
      d:  tr.querySelector('[data-cell="d"]'),
      e:  tr.querySelector('[data-cell="e"]'),
      g:  tr.querySelector('[data-cell="g"]'),
    };
    if (!m) {
      cells.sc.textContent = '-';
      cells.sc.classList.add('dim');
      ['a','c','e'].forEach(k => { cells[k].value = ''; cells[k].placeholder = '-'; cells[k].className = ''; });
      ['b','d','g'].forEach(k => { cells[k].textContent = '-'; cells[k].className = (k==='b'?'b ':'') + 'lbl-none'; });
      continue;
    }
    cells.sc.textContent = m.score;
    cells.sc.classList.remove('dim');
    const sideClass = m.a_side === 'home' ? 'home' : m.a_side === 'level' ? 'level' : 'away';
    cells.a.value = m.a !== null && m.a !== undefined ? fmtNum(m.a) : '';
    cells.a.placeholder = '-';
    cells.a.className = sideClass;
    cells.b.textContent = m.b !== null && m.b !== undefined ? fmtNum(m.b) : '-';
    cells.b.className = 'b';
    cells.c.value = m.c !== null && m.c !== undefined ? fmtNum(m.c) : '';
    cells.c.placeholder = '-';
    const d = dgLabel(m.d);
    cells.d.textContent = d.txt; cells.d.className = d.cls;
    cells.e.value = m.e !== null && m.e !== undefined ? fmtNum(m.e) : '';
    cells.e.placeholder = '-';
    const g = dgLabel(m.g);
    cells.g.textContent = g.txt; cells.g.className = g.cls;
  }
  // Notes
  for (let i = 0; i < 9; i++) {
    const tr = rows[i];
    const n = S.notes[i] || { n1: '', n2: '', n3: '' };
    tr.querySelector('[data-cell="n1"]').value = n.n1 || '';
    tr.querySelector('[data-cell="n2"]').value = n.n2 || '';
    tr.querySelector('[data-cell="n3"]').value = n.n3 || '';
  }
}

// ─── Render the Trận table ─────────────────────────────────────────────────

function renderTran() {
  const body = $('tran-body');
  if (!S.rows_filtered.length) {
    body.innerHTML = '<tr><td colspan="6" class="no-data">Chưa có dữ liệu</td></tr>';
    return;
  }
  const frag = document.createDocumentFragment();
  for (const r of S.rows_filtered) {
    const tr = document.createElement('tr');
    if (r._goal) tr.className = 'goal';
    tr.innerHTML = `
      <td>${r['Half'] || ''}</td>
      <td>${r['Home Score'] || ''}</td>
      <td>${r['Away Score'] || ''}</td>
      <td>${r['Home Handicap'] || ''}</td>
      <td>${r['Away Handicap'] || ''}</td>
      <td>${r['Over/Under Line'] || ''}</td>
    `;
    frag.appendChild(tr);
  }
  body.innerHTML = '';
  body.appendChild(frag);
}

// ─── Render match info / handicap box ──────────────────────────────────────

function renderMeta() {
  const m = S.meta || {};
  const mi = $('match-info');
  mi.innerHTML = `
    <div class="league">${escapeHtml(m.league || '—')}</div>
    <div class="teams">${escapeHtml(m.home || '?')} <span class="muted">vs</span> ${escapeHtml(m.away || '?')}</div>
    <div class="dt">${escapeHtml(m.date || '')} ${escapeHtml(m.time || '')}</div>
  `;
  const dc = S.favorite_home ? (m.away || '—') : (m.home || '—');
  const ch = S.favorite_home ? (m.home || '—') : (m.away || '—');
  $('duoc-chap').textContent = dc;
  $('chap').textContent = ch;
  $('real-score').textContent = `${S.real_fh} — ${S.real_fa}`;
  if ($('pred-fh').value === '') $('pred-fh').value = S.real_fh;
  if ($('pred-fa').value === '') $('pred-fa').value = S.real_fa;
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, (c) => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

// ─── Upload / parse ────────────────────────────────────────────────────────

async function parseFile(file) {
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch('/api/analyzer/parse', { method: 'POST', body: fd });
  if (!r.ok) {
    toast('Upload thất bại: ' + r.status, 'err');
    return;
  }
  const data = await r.json();
  S.filename = data.filename;
  S.meta = data.meta;
  S.csv_blob = data.csv_blob;
  S.rows_filtered = data.rows_filtered;
  S.analysis = data.analysis;
  S.real_fh = data.real_fh;
  S.real_fa = data.real_fa;
  S.favorite_home = data.favorite_home;
  S.overrides = {};
  S.notes = Array(9).fill(null).map(() => ({ n1: '', n2: '', n3: '' }));
  S.note_text = '';
  S.pred_fh = data.real_fh;
  S.pred_fa = data.real_fa;
  S.session_id = null;
  $('pred-fh').value = data.real_fh;
  $('pred-fa').value = data.real_fa;
  $('note-text').value = '';
  renderMeta();
  renderTran();
  renderAnalysis();
  if (data.errors && data.errors.length) {
    toast('Cảnh báo: ' + data.errors.join('; '), 'warn');
  } else {
    toast('Đã nạp ' + S.rows_filtered.length + ' rows', 'ok');
  }
}

// ─── Compute (Run) ─────────────────────────────────────────────────────────

async function runCompute() {
  if (!S.csv_blob) { toast('Chưa có dữ liệu', 'warn'); return; }
  // Read prediction from inputs
  const pfh = $('pred-fh').value === '' ? null : parseInt($('pred-fh').value, 10);
  const pfa = $('pred-fa').value === '' ? null : parseInt($('pred-fa').value, 10);
  if ((pfh !== null && isNaN(pfh)) || (pfa !== null && isNaN(pfa))) {
    toast('Tỉ số dự đoán phải là số nguyên', 'err');
    return;
  }
  S.pred_fh = pfh;
  S.pred_fa = pfa;
  const r = await fetch('/api/analyzer/compute', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      csv_blob: S.csv_blob,
      overrides: S.overrides,
      pred_fh: pfh,
      pred_fa: pfa,
    }),
  });
  if (!r.ok) { toast('Run lỗi: ' + r.status, 'err'); return; }
  const data = await r.json();
  S.analysis = data.analysis;
  S.real_fh = data.real_fh;
  S.real_fa = data.real_fa;
  renderAnalysis();
  toast('Đã tính lại', 'ok');
}

function resetPred() {
  $('pred-fh').value = S.real_fh;
  $('pred-fa').value = S.real_fa;
  runCompute();
}

function toggleEdit() {
  S.edit_mode = !S.edit_mode;
  const btn = $('btn-edit');
  btn.classList.toggle('active', S.edit_mode);
  btn.textContent = S.edit_mode ? '✓ Đang sửa' : '✏ Sửa';
  document.querySelectorAll('#db-body input[data-cell="a"], #db-body input[data-cell="c"], #db-body input[data-cell="e"]').forEach((el) => {
    el.disabled = !S.edit_mode;
  });
}

// ─── Save / Open ───────────────────────────────────────────────────────────

async function saveSession() {
  if (!S.csv_blob) { toast('Chưa có dữ liệu', 'warn'); return; }
  S.note_text = $('note-text').value;
  const payload = {
    id: S.session_id,
    filename: S.filename,
    meta: S.meta,
    csv_blob: S.csv_blob,
    overrides: S.overrides,
    notes: S.notes,
    note_text: S.note_text,
    pred_fh: $('pred-fh').value === '' ? null : parseInt($('pred-fh').value, 10),
    pred_fa: $('pred-fa').value === '' ? null : parseInt($('pred-fa').value, 10),
    analysis: S.analysis,
  };
  const r = await fetch('/api/analyzer/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!r.ok) { toast('Save lỗi: ' + r.status, 'err'); return; }
  const data = await r.json();
  S.session_id = data.id;
  toast('Đã lưu session #' + data.id, 'ok');
}

async function openModal() {
  $('modal-bg').classList.add('open');
  const r = await fetch('/api/analyzer/sessions');
  const body = $('modal-body');
  if (!r.ok) {
    body.innerHTML = '<tr><td colspan="4" class="empty">Lỗi tải danh sách</td></tr>';
    return;
  }
  const { sessions } = await r.json();
  if (!sessions.length) {
    body.innerHTML = '<tr><td colspan="4" class="empty">Chưa có session nào</td></tr>';
    return;
  }
  body.innerHTML = '';
  for (const s of sessions) {
    const tr = document.createElement('tr');
    const teams = `${(s.meta && s.meta.home) || '?'} vs ${(s.meta && s.meta.away) || '?'}`;
    tr.innerHTML = `
      <td>${escapeHtml(s.filename)}</td>
      <td>${escapeHtml(teams)}</td>
      <td>${escapeHtml((s.updated_at || '').slice(0, 19).replace('T', ' '))}</td>
      <td>
        <button class="btn" style="padding:3px 8px;font-size:11px">Mở</button>
        <button class="btn" style="padding:3px 8px;font-size:11px;color:var(--red);border-color:var(--red)" data-del="1">Xóa</button>
      </td>
    `;
    tr.addEventListener('click', async (e) => {
      if (e.target.matches('[data-del]')) {
        e.stopPropagation();
        if (!confirm('Xóa session #' + s.id + '?')) return;
        await fetch('/api/analyzer/sessions/' + s.id, { method: 'DELETE' });
        openModal(); // refresh
        return;
      }
      await loadSession(s.id);
      closeModal();
    });
    body.appendChild(tr);
  }
}

function closeModalIfBg(ev) {
  if (ev.target.id === 'modal-bg') closeModal();
}

function closeModal() {
  $('modal-bg').classList.remove('open');
}

async function loadSession(id) {
  const r = await fetch('/api/analyzer/sessions/' + id);
  if (!r.ok) { toast('Load lỗi', 'err'); return; }
  const s = await r.json();
  S.session_id = s.id;
  S.filename = s.filename;
  S.meta = s.meta || {};
  S.csv_blob = s.csv_blob;
  S.rows_filtered = s.rows_filtered || [];
  S.analysis = s.analysis || [];
  S.overrides = s.overrides || {};
  S.notes = (s.notes && s.notes.length === 9) ? s.notes : Array(9).fill(null).map(() => ({ n1: '', n2: '', n3: '' }));
  S.note_text = s.note_text || '';
  S.pred_fh = s.pred_fh;
  S.pred_fa = s.pred_fa;

  // We need real_fh/fa to label things — recompute quickly to get them
  const r2 = await fetch('/api/analyzer/compute', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ csv_blob: S.csv_blob, overrides: S.overrides, pred_fh: S.pred_fh, pred_fa: S.pred_fa }),
  });
  if (r2.ok) {
    const d = await r2.json();
    S.analysis = d.analysis;
    S.real_fh = d.real_fh;
    S.real_fa = d.real_fa;
  }
  // determine favorite_home from analysis a_side or open handicap heuristic
  const open = S.analysis.find(x => x);
  S.favorite_home = open ? (open.a_side === 'home') : false;

  $('pred-fh').value = S.pred_fh ?? S.real_fh;
  $('pred-fa').value = S.pred_fa ?? S.real_fa;
  $('note-text').value = S.note_text;
  renderMeta();
  renderTran();
  renderAnalysis();
  toast('Đã mở session #' + id, 'ok');
}

// ─── Export / Import JSON ──────────────────────────────────────────────────

async function exportJSON() {
  if (!S.session_id) {
    toast('Save trước rồi mới export được', 'warn');
    return;
  }
  window.location.href = '/api/analyzer/sessions/' + S.session_id + '/export';
}

function exportCSV() {
  if (!S.session_id) { toast('Lưu session trước khi export CSV', 'warn'); return; }
  window.open('/api/analyzer/sessions/' + S.session_id + '/csv', '_blank');
}

async function importJSON(file) {
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch('/api/analyzer/import', { method: 'POST', body: fd });
  if (!r.ok) { toast('Import lỗi: ' + r.status, 'err'); return; }
  const data = await r.json();
  if (data.needs_csv) {
    toast('Đã đọc JSON. Bây giờ thả file CSV gốc để gắn dữ liệu.', 'warn');
    // Stage the imported state and wait for CSV
    S._pending_import = {
      meta: data.meta,
      overrides: Object.fromEntries(Object.entries(data.overrides).map(([k, v]) => [parseInt(k, 10), v])),
      notes: data.notes,
      note_text: data.note_text,
      analysis: data.analysis,
    };
  }
}

// ─── Match picker ──────────────────────────────────────────────────────────

let _mpMatches = [];

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

async function openMatchPicker() {
  $('match-picker-bg').classList.add('open');
  if (!_mpMatches.length) await _mpLoad('');
}

function closeMatchPicker() {
  $('match-picker-bg').classList.remove('open');
}

function closeMatchPickerBg(e) {
  if (e.target === $('match-picker-bg')) closeMatchPicker();
}

async function _mpLoad(q) {
  const params = new URLSearchParams({ limit: 300 });
  if (q) params.set('q', q);
  const r = await fetch('/api/data/matches?' + params.toString());
  if (!r.ok) { toast('Không tải được danh sách trận', 'err'); return; }
  const data = await r.json();
  if (!q) _mpMatches = data;
  _mpRender(data);
}

function _mpRender(matches) {
  const body = $('mp-body');
  const cnt = $('mp-count');
  if (cnt) cnt.textContent = matches.length + ' trận';
  if (!matches || !matches.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty">Không tìm thấy trận nào</td></tr>';
    return;
  }
  const liveOrder = ['H1', 'H2', 'LIVE', 'HT'];
  const sorted = [...matches].sort((a, b) => {
    const aL = liveOrder.includes(a.status) ? 0 : 1;
    const bL = liveOrder.includes(b.status) ? 0 : 1;
    return aL - bL || new Date(b.start_time_utc) - new Date(a.start_time_utc);
  });
  body.innerHTML = sorted.map(m => {
    const live = ['H1', 'H2', 'LIVE'].includes(m.status);
    const statusHtml = live
      ? '<span style="color:#f85149;font-weight:700">● ' + m.status + '</span>'
      : '<span style="color:var(--muted)">' + escapeHtml(m.status) + '</span>';
    return '<tr onclick="pickMatch(\'' + escapeHtml(m.id) + '\')" style="cursor:pointer">' +
      '<td style="color:var(--muted);max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml((m.competition || '').substring(0, 25)) + '</td>' +
      '<td><b>' + escapeHtml(m.home) + '</b> vs <b>' + escapeHtml(m.away) + '</b></td>' +
      '<td style="font-weight:700;color:#f85149;text-align:center">' + (m.home_score || 0) + ' - ' + (m.away_score || 0) + '</td>' +
      '<td>' + statusHtml + '</td>' +
      '<td><button class="btn" style="padding:3px 10px;font-size:11px" onclick="event.stopPropagation();pickMatch(\'' + escapeHtml(m.id) + '\')">Chọn</button></td>' +
      '</tr>';
  }).join('');
}

async function pickMatch(matchId) {
  closeMatchPicker();
  await loadFromMatch(matchId);
  const url = new URL(window.location);
  url.searchParams.set('match_id', matchId);
  window.history.replaceState({}, '', url);
}

// ─── Drag-drop wiring ──────────────────────────────────────────────────────

function wireDrop() {
  const drop = $('drop');
  const fi = $('file');
  // Click on drop zone → open match picker; CSV link inside handles file dialog
  drop.addEventListener('click', (e) => {
    if (e.target === fi || e.target.tagName === 'SPAN') return;
    openMatchPicker();
  });
  drop.addEventListener('keypress', (e) => { if (e.key === 'Enter' || e.key === ' ') openMatchPicker(); });
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => {
    e.preventDefault(); drop.classList.remove('over');
    if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
  });
  fi.addEventListener('change', () => {
    if (fi.files.length) handleFile(fi.files[0]);
  });
  $('import-file').addEventListener('change', (e) => {
    if (e.target.files.length) importJSON(e.target.files[0]);
  });
}

async function handleFile(file) {
  if (!file.name.toLowerCase().endsWith('.csv')) {
    toast('Chỉ nhận file .csv', 'err'); return;
  }
  await parseFile(file);
  // If we have a pending JSON import waiting for CSV, merge it in
  if (S._pending_import) {
    const p = S._pending_import;
    S.overrides = p.overrides || {};
    S.notes = (p.notes && p.notes.length === 9) ? p.notes : S.notes;
    S.note_text = p.note_text || '';
    $('note-text').value = S.note_text;
    delete S._pending_import;
    // re-run compute with the imported overrides
    await runCompute();
    toast('Đã gộp JSON + CSV', 'ok');
  }
}

// ─── Note text → state ─────────────────────────────────────────────────────

function wireNote() {
  $('note-text').addEventListener('input', (e) => { S.note_text = e.target.value; });
}

// ─── AI status badge ───────────────────────────────────────────────────────

function _setAIBadge({ dot, label, btnEnabled }) {
  const d = $('ai-dot');
  const l = $('ai-label');
  const b = $('ai-check-btn');
  const p = $('btn-ai-predict');
  if (d) d.className = 'ai-dot ' + dot;
  if (l) l.innerHTML = label;
  if (b) b.disabled = !btnEnabled;
  if (p) {
    p.disabled = !btnEnabled;
    p.title = btnEnabled ? 'AI phân tích trận hiện tại (grounded trên base-rate)' : 'Cần cấu hình AI';
  }
}

async function loadAIStatus() {
  try {
    const r = await fetch('/api/ai/status');
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    if (!d.configured) {
      _setAIBadge({
        dot: 'off',
        label: '<span class="dim">AI: chưa cấu hình</span>',
        btnEnabled: false,
      });
      const badge = $('ai-badge');
      if (badge) badge.title = 'Đặt AI_BASE_URL + AI_API_KEY + AI_MODEL trong .env để bật AI';
      return;
    }
    _setAIBadge({
      dot: 'off',
      label: 'AI: <b>' + escapeHtml(d.model) + '</b>',
      btnEnabled: true,
    });
    const badge = $('ai-badge');
    if (badge) badge.title = `Endpoint: ${d.base_url}\nModel: ${d.model}\nKey: ${d.api_key_masked}\nBấm "Kiểm tra" để xác nhận kết nối`;
  } catch (e) {
    _setAIBadge({
      dot: 'err',
      label: '<span class="dim">AI: lỗi tải trạng thái</span>',
      btnEnabled: false,
    });
  }
}

async function checkAI() {
  _setAIBadge({ dot: 'checking', label: 'AI: đang kiểm tra…', btnEnabled: false });
  try {
    const r = await fetch('/api/ai/check', { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      _setAIBadge({
        dot: 'on',
        label: 'AI: <b>' + escapeHtml(d.model) + '</b> <span class="dim">· ' + d.latency_ms + 'ms</span>',
        btnEnabled: true,
      });
      toast('AI hoạt động · ' + d.model + ' · ' + d.latency_ms + 'ms', 'ok');
    } else {
      _setAIBadge({
        dot: 'err',
        label: '<span class="dim">AI: không kết nối được</span>',
        btnEnabled: !!d.configured,
      });
      toast('AI lỗi: ' + (d.error || 'không xác định'), 'err');
    }
  } catch (e) {
    _setAIBadge({ dot: 'err', label: '<span class="dim">AI: lỗi kiểm tra</span>', btnEnabled: true });
    toast('Không gọi được /api/ai/check: ' + e, 'err');
  }
}

// ─── AI grounded prediction ─────────────────────────────────────────────────

function _confColor(c) {
  return c >= 0.66 ? 'var(--green)' : (c >= 0.4 ? 'var(--orange)' : 'var(--red)');
}

function _leanLabel(v) {
  return ({ favorite: 'Nghiêng cửa trên', underdog: 'Nghiêng cửa dưới',
            over: 'Nghiêng Tài', under: 'Nghiêng Xỉu', no_edge: 'Không rõ edge' }[v]) || (v || '—');
}

function _pct(x) { return (x === null || x === undefined) ? '—' : (Math.round(x * 1000) / 10) + '%'; }

function _renderAIResult(d) {
  const p = (d.parsed && typeof d.parsed === 'object') ? d.parsed : null;
  const st = d.stats || {};
  const bk = st.bucket || {};
  const ov = st.overall || {};
  const conf = p && typeof p.confidence === 'number' ? p.confidence : null;
  let html = '';

  if (p) {
    if (p.summary) {
      html += '<div class="ai-card"><h4>Tóm tắt</h4><div class="ai-summary">' + escapeHtml(p.summary) + '</div></div>';
    }
    const pr = p.prediction || {};
    html += '<div class="ai-card"><h4>Dự đoán</h4><div class="ai-pred-grid">';
    if (pr.score) html += '<span class="ai-pill">Tỉ số: <b>' + escapeHtml(String(pr.score)) + '</b></span>';
    html += '<span class="ai-pill">Chấp: <b>' + escapeHtml(_leanLabel(pr.handicap_lean)) + '</b></span>';
    html += '<span class="ai-pill">Tài/Xỉu: <b>' + escapeHtml(_leanLabel(pr.ou_lean)) + '</b></span>';
    if (pr.more_goals_likely !== undefined) {
      const moreGoalsLabel = pr.more_goals_likely ? 'Có khả năng' : 'Ít khả năng';
      const moreGoalsColor = pr.more_goals_likely ? 'var(--green)' : 'var(--red)';
      html += '<span class="ai-pill" style="color:' + moreGoalsColor + '">Thêm bàn: <b>' + moreGoalsLabel + '</b></span>';
    }
    html += '</div>';
    if (conf !== null) {
      html += '<div class="ai-meta">Confidence: ' + _pct(conf) + '</div>' +
              '<div class="ai-conf-bar"><div class="ai-conf-fill" style="width:' +
              Math.round(conf * 100) + '%;background:' + _confColor(conf) + '"></div></div>';
    }
    html += '</div>';
    if (Array.isArray(p.signals) && p.signals.length) {
      html += '<div class="ai-card"><h4>Tín hiệu</h4><ul class="ai-list">' +
              p.signals.map(s => '<li>' + escapeHtml(String(s)) + '</li>').join('') + '</ul></div>';
    }
    const TAG_DESCRIPTIONS = {
      fav_cover: 'Cửa trên thắng kèo chấp (HC)',
      fav_no_cover: 'Cửa trên thắng kèo chấp (HC)',
      over_hit: 'Tài thắng — tổng bàn > line O/U',
      under_hit: 'Xỉu thắng — tổng bàn < line O/U',
      btts: 'Cả 2 đội đều ghi bàn',
      clean_sheet_fav: 'Cửa trên giữ sạch lưới',
      comeback: 'Cửa trên lội ngược dòng',
      line_drifted_up: 'Kèo chấp tăng (cửa trên nhận thêm chấp)',
      line_drifted_down: 'Kèo chấp giảm (cửa dưới nhẹ hơn)',
      low_scoring: 'Ít bàn — thường under 2.5',
      high_scoring: 'Nhiều bàn — thường over 2.5',
      draw: 'Hòa kèo / hòa tỉ số',
      small_sample: 'Bucket có ít trận (<30) — base-rate thiếu tin cậy',
      expected_goal_market: 'Bàn thắng đúng kỳ vọng thị trường',
      unexpected_goals: 'Bàn thắng bất ngờ so với kỳ vọng',
    };
    if (Array.isArray(p.tags) && p.tags.length) {
      html += '<div class="ai-card"><h4>Tags (công thức)</h4><div class="ai-pred-grid">' +
              p.tags.map(t => {
                const desc = TAG_DESCRIPTIONS[t] || '';
                const title = desc ? ' title="' + desc.replace(/"/g, '&quot;') + '"' : '';
                return '<span class="ai-pill"' + title + '>' + escapeHtml(String(t)) + '</span>';
              }).join('') +
              '</div></div>';
      // Legend toggle
      html += '<div style="margin-top:8px;font-size:11px;color:var(--muted);cursor:pointer" onclick="'
              + 'var l=document.getElementById(\'tags-legend\');l.style.display=l.style.display?\'\':\'none\';'
              + 'this.textContent=l.style.display?\'🔽 Ẩn giải thích\':\'🔽 Xem giải thích\'">'
              + '🔽 Xem giải thích</div>';
      html += '<div id="tags-legend" style="display:none;margin-top:6px;font-size:11px;line-height:1.7;color:var(--muted)">';
      const shownTags = p.tags.filter(t => TAG_DESCRIPTIONS[t]);
      if (shownTags.length) {
        html += shownTags.map(t =>
          '<div><b>' + escapeHtml(t) + '</b>: ' + (TAG_DESCRIPTIONS[t] || '—') + '</div>'
        ).join('');
      } else {
        html += '<i>Chưa có mô tả cho các tag này.</i>';
      }
      html += '</div>';
    }
    if (Array.isArray(p.caveats) && p.caveats.length) {
      html += '<div class="ai-card"><h4>Lưu ý</h4><ul class="ai-list">' +
              p.caveats.map(s => '<li>' + escapeHtml(String(s)) + '</li>').join('') + '</ul></div>';
    }
  } else {
    html += '<div class="ai-card"><h4>Kết quả (raw)</h4><div class="ai-raw">' +
            escapeHtml(d.content || '(rỗng)') + '</div></div>';
  }

  // Base-rate evidence — the deterministic numbers the model was grounded on.
  const f = st.filters || {};
  html += '<div class="ai-card"><h4>Base-rate (bằng chứng)</h4>';
  html += '<div class="ai-base">' +
    '<span>Bucket HC: <b>' + escapeHtml(String(f.open_hc ?? '—')) + '</b> · OU <b>' + escapeHtml(String(f.open_ou ?? '—')) + '</b></span>' +
    '<span>n bucket: <b>' + (bk.n || 0) + '</b></span>' +
    '<span>n tổng: <b>' + (st.n_total || 0) + '</b></span>' +
    '</div>';
  html += '<div class="ai-base" style="margin-top:6px">' +
    '<span>Cover cửa trên: <b>' + _pct(bk.fav_cover_rate) + '</b> (n=' + (bk.fav_cover_n || 0) + ')</span>' +
    '<span>Tài: <b>' + _pct(bk.over_rate) + '</b></span>' +
    '<span>BTTS: <b>' + _pct(bk.btts_rate) + '</b></span>' +
    '<span>Bàn TB: <b>' + (bk.avg_goals ?? '—') + '</b></span>' +
    '<span>Hòa: <b>' + _pct(bk.draw_rate) + '</b></span>' +
    '</div>';
  if (bk.n < 30) {
    html += '<div class="ai-meta" style="color:var(--orange)">⚠ Sample bucket nhỏ (n=' + (bk.n || 0) +
            '), base-rate tham khảo có sai số lớn — đối chiếu n tổng (' + (st.n_total || 0) + ').</div>';
  }
  html += '</div>';

  const u = d.usage || {};
  html += '<div class="ai-meta">Model: ' + escapeHtml(d.model || '—') +
          (u.total_tokens ? ' · ' + u.total_tokens + ' tokens' : '') +
          (d.reasoning_chars ? ' · reasoning ' + d.reasoning_chars + ' ký tự' : '') + '</div>';

  $('ai-modal-body').innerHTML = html;
  const mm = $('ai-modal-model');
  if (mm) mm.textContent = d.model || '';
}

async function aiPredict() {
  if (!S.csv_blob) { toast('Chưa có dữ liệu', 'warn'); return; }
  const btn = $('btn-ai-predict');
  if (btn) { btn.disabled = true; btn.classList.add('busy'); btn.textContent = '🤖 Đang phân tích…'; }
  // Sync prediction inputs into state (mirror runCompute behaviour).
  const pfh = $('pred-fh').value === '' ? null : parseInt($('pred-fh').value, 10);
  const pfa = $('pred-fa').value === '' ? null : parseInt($('pred-fa').value, 10);
  try {
    const r = await fetch('/api/analyzer/ai-predict', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        csv_blob: S.csv_blob,
        overrides: S.overrides,
        pred_fh: pfh,
        pred_fa: pfa,
        meta: S.meta,
      }),
    });
    const d = await r.json();
    if (!r.ok || d.ok === false) {
      toast('AI lỗi: ' + (d.error || d.detail || r.status), 'err');
      return;
    }
    _renderAIResult(d);
    openAIModal();
  } catch (e) {
    toast('Không gọi được AI: ' + e, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('busy'); btn.textContent = '🤖 AI dự đoán'; }
  }
}

function openAIModal() { const m = $('ai-modal-bg'); if (m) m.classList.add('open'); }
function closeAIModal() { const m = $('ai-modal-bg'); if (m) m.classList.remove('open'); }
function closeAIModalBg(e) { if (e.target === $('ai-modal-bg')) closeAIModal(); }

// ─── Init ──────────────────────────────────────────────────────────────────

async function loadFromMatch(matchId) {
  const r = await fetch('/api/analyzer/from-match/' + encodeURIComponent(matchId));
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    toast('Lỗi load trận: ' + (body.detail || r.status), 'err');
    return;
  }
  const data = await r.json();
  S.filename     = data.filename;
  S.match_id     = matchId;
  S.meta         = data.meta;
  S.csv_blob     = data.csv_blob;
  S.rows_filtered = data.rows_filtered;
  S.analysis     = data.analysis;
  S.real_fh      = data.real_fh;
  S.real_fa      = data.real_fa;
  S.favorite_home = data.favorite_home;
  S.overrides    = {};
  S.notes        = Array(9).fill(null).map(() => ({ n1: '', n2: '', n3: '' }));
  S.note_text    = '';
  S.pred_fh      = data.real_fh;
  S.pred_fa      = data.real_fa;
  S.session_id   = null;
  $('pred-fh').value = data.real_fh;
  $('pred-fa').value = data.real_fa;
  $('note-text').value = '';

  // Show live badge if still in-play
  const liveStatuses = ['H1', 'H2', 'LIVE'];
  const isLive = liveStatuses.includes(data.match_status);
  const badge = isLive
    ? ' <span style="color:#f85149;font-weight:700;font-size:11px">● LIVE</span>'
    : '';
  $('drop').innerHTML = '<strong>' + escapeHtml(data.filename) + '</strong>' + badge +
    '<div style="font-size:11px;color:var(--muted);margin-top:4px">' + data.row_count + ' rows từ DB &nbsp;·&nbsp; <span style="color:var(--primary);cursor:pointer" onclick="event.stopPropagation();openMatchPicker()">🔄 Đổi trận</span></div>';

  renderMeta();
  renderTran();
  renderAnalysis();

  if (data.errors && data.errors.length) {
    toast('Cảnh báo: ' + data.errors.join('; '), 'warn');
  } else {
    toast('Đã nạp ' + data.row_count + ' rows (' + (isLive ? 'đang live' : data.match_status) + ')', 'ok');
  }

  // Reveal "Pattern đã lưu" only if this match already has a stored AI pattern.
  refreshStoredPatternButton();
}

async function refreshStoredPatternButton() {
  const btn = $('btn-view-pattern');
  if (!btn) return;
  btn.style.display = 'none';
  if (!S.match_id) return;
  try {
    const r = await fetch('/api/analyzer/patterns/' + encodeURIComponent(S.match_id));
    if (!r.ok) return;
    const d = await r.json();
    if (d.pattern) {
      S._stored_pattern = d.pattern;
      btn.style.display = '';
    }
  } catch (e) { /* silent — button just stays hidden */ }
}

function viewStoredPattern() {
  const p = S._stored_pattern;
  if (!p) { toast('Chưa có pattern lưu cho trận này', 'warn'); return; }
  // Adapt the stored row to the shape _renderAIResult expects.
  _renderAIResult({
    parsed: {
      summary: p.summary,
      signals: p.signals || [],
      prediction: p.prediction || {},
      confidence: p.confidence,
      caveats: p.caveats || [],
      tags: p.tags || [],
    },
    stats: { filters: { open_hc: p.open_hc, open_ou: p.open_ou }, ...(p.base_rate || {}) },
    model: p.model,
    content: p.raw_content || '',
    usage: {},
  });
  openAIModal();
}

document.addEventListener('DOMContentLoaded', () => {
  buildDB();
  renderAnalysis();
  wireDrop();
  wireNote();
  loadAIStatus();

  // Wire match picker search
  const mpSearch = $('mp-search');
  if (mpSearch) {
    mpSearch.addEventListener('input', debounce(e => {
      const q = e.target.value.trim();
      if (q) _mpLoad(q); else _mpRender(_mpMatches);
    }, 300));
  }

  // Auto-load if ?match_id= is in URL, else pre-load match list in background
  const params = new URLSearchParams(window.location.search);
  const mid = params.get('match_id');
  if (mid) {
    loadFromMatch(mid);
  } else {
    // Pre-fetch match list so picker opens instantly
    _mpLoad('');
  }
});

