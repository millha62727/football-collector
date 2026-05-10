"""
Football Data Dashboard — main entry point.
Runs the web server AND the background collector in one process.

Start:  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""
import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .auth import check_rate_limit, create_token, decode_token, require_auth, verify_credentials
from .collector import run_collector
from .database import get_all_matches, get_live_matches, get_stats, init_db
from .state import app_state

# ---------------------------------------------------------------------------
# Static HTML pages (pure strings — no f-string so CSS {} are safe)
# ---------------------------------------------------------------------------

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Football Dashboard — Login</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--primary:#58a6ff;--danger:#f85149;--text:#c9d1d9;--muted:#8b949e;--r:6px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:40px;width:100%;max-width:380px}
.logo{text-align:center;font-size:52px;margin-bottom:12px}
h1{text-align:center;font-size:22px;font-weight:700;margin-bottom:4px}
.sub{text-align:center;color:var(--muted);font-size:13px;margin-bottom:28px}
label{display:block;font-size:13px;font-weight:600;color:var(--muted);margin-bottom:6px}
input{width:100%;padding:10px 14px;background:#0d1117;border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-size:14px;margin-bottom:16px;transition:border-color .15s}
input:focus{outline:none;border-color:var(--primary)}
button{width:100%;padding:11px;background:var(--primary);border:none;border-radius:var(--r);color:#0d1117;font-size:14px;font-weight:700;cursor:pointer;margin-top:4px;transition:background .15s}
button:hover{background:#79c0ff}
.err{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);border-radius:var(--r);padding:10px 14px;color:var(--danger);font-size:13px;margin-bottom:16px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">&#x26BD;</div>
  <h1>Football Dashboard</h1>
  <p class="sub">Đăng nhập để tiếp tục</p>
  %%ERROR%%
  <form method="post" action="/login">
    <label for="u">Tên đăng nhập</label>
    <input type="text" id="u" name="username" placeholder="admin" required autofocus>
    <label for="p">Mật khẩu</label>
    <input type="password" id="p" name="password" placeholder="••••••••" required>
    <button type="submit">Đăng nhập</button>
  </form>
</div>
</body>
</html>"""

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Football Dashboard</title>
<style>
:root{
  --bg:#0d1117;--surface:#161b22;--card:#21262d;--border:#30363d;
  --primary:#58a6ff;--pdim:#1f3a5f;
  --red:#f85149;--green:#3fb950;--orange:#d29922;--purple:#bc8cff;--cyan:#39d353;
  --text:#c9d1d9;--muted:#8b949e;--r:6px;
  --font:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100vh;display:flex;flex-direction:column}

/* ---- Header ---- */
.hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 24px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:100}
.hdr-logo{font-size:17px;font-weight:700;color:var(--primary);display:flex;align-items:center;gap:6px}
.hdr-spacer{flex:1}
.hdr-info{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}
.hdr-user{color:var(--text);font-weight:600}
.btn-sm{padding:5px 12px;border-radius:var(--r);border:1px solid var(--border);background:var(--card);color:var(--text);cursor:pointer;font-size:12px;transition:all .15s}
.btn-sm:hover{border-color:var(--primary);color:var(--primary)}
.btn-sm.danger:hover{border-color:var(--red);color:var(--red)}

/* ---- Status dot ---- */
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot-green{background:var(--green);animation:blink 2s infinite}
.dot-orange{background:var(--orange)}
.dot-red{background:var(--red)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

/* ---- Main layout ---- */
.main{flex:1;padding:20px 24px;max-width:1500px;margin:0 auto;width:100%}

/* ---- Control panel ---- */
.ctrl{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px 20px;margin-bottom:18px;display:flex;align-items:center;gap:20px;flex-wrap:wrap}
.ctrl-status{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600}
.ctrl-meta{font-size:12px;color:var(--muted);flex:1}
.ctrl-meta span{color:var(--text);font-weight:500}
.btn{padding:6px 16px;border-radius:var(--r);border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;font-size:13px;font-weight:500;transition:all .15s}
.btn:hover{background:var(--card)}
.btn-primary{background:var(--pdim);border-color:var(--primary);color:var(--primary)}
.btn-primary:hover{background:var(--primary);color:#0d1117}
.btn-warn{background:rgba(210,153,34,.15);border-color:var(--orange);color:var(--orange)}
.btn-warn:hover{background:var(--orange);color:#0d1117}
.btn-danger{background:rgba(248,81,73,.1);border-color:var(--red);color:var(--red)}
.btn-danger:hover{background:var(--red);color:white}
.btn:disabled{opacity:.4;cursor:default}

/* ---- Stats ---- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-bottom:18px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px;text-align:center}
.stat-n{font-size:26px;font-weight:700;line-height:1.1}
.stat-l{font-size:11px;color:var(--muted);margin-top:4px;text-transform:uppercase;letter-spacing:.5px}
.c-live{color:var(--red)}.c-ht{color:var(--purple)}.c-up{color:var(--orange)}.c-ft{color:var(--green)}.c-all{color:var(--primary)}

/* ---- Section ---- */
.sec{margin-bottom:24px}
.sec-hdr{display:flex;align-items:center;gap:10px;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.sec-title{font-size:14px;font-weight:700;letter-spacing:.3px}
.sec-badge{background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:2px 8px;font-size:11px;color:var(--muted)}

/* ---- Match cards ---- */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:10px}
.mcard{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px;transition:border-color .15s}
.mcard:hover{border-color:var(--primary)}
.mcard.live{border-left:3px solid var(--red)}
.mcard.ht{border-left:3px solid var(--purple)}
.mcomp{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mteams{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.mteam{font-size:13px;font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mteam.away{text-align:right}
.mscore{font-size:18px;font-weight:700;color:var(--red);white-space:nowrap;min-width:46px;text-align:center}
.minfo{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-bottom:8px}
.mmin{color:var(--orange);font-weight:700}
.modds{display:flex;gap:5px;flex-wrap:wrap}
.chip{background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:3px 7px;font-size:10px}
.chip b{color:var(--primary)}
.empty{color:var(--muted);padding:20px;text-align:center;font-size:13px}

/* ---- Table ---- */
.tbar{display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.tinput{flex:1;min-width:180px;padding:7px 12px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-size:13px}
.tinput:focus{outline:none;border-color:var(--primary)}
.tsel{padding:7px 10px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-size:13px;cursor:pointer}
.twrap{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px;min-width:750px}
th{background:var(--surface);padding:9px 12px;text-align:left;font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid var(--border);white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.025)}

/* ---- Badges ---- */
.badge{display:inline-block;padding:2px 7px;border-radius:3px;font-size:10px;font-weight:700;letter-spacing:.4px}
.b-live{background:rgba(248,81,73,.2);color:var(--red);border:1px solid rgba(248,81,73,.35)}
.b-up{background:rgba(210,153,34,.2);color:var(--orange);border:1px solid rgba(210,153,34,.35)}
.b-ft{background:rgba(63,185,80,.2);color:var(--green);border:1px solid rgba(63,185,80,.35)}
.b-ht{background:rgba(188,140,255,.2);color:var(--purple);border:1px solid rgba(188,140,255,.35)}
.b-x{background:rgba(139,148,158,.15);color:var(--muted);border:1px solid var(--border)}

/* ---- Logs ---- */
.logbox{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:12px;height:220px;overflow-y:auto;font-family:'Consolas','Monaco',monospace;font-size:11px}
.le{margin-bottom:3px;line-height:1.5}
.lt{color:var(--muted);margin-right:6px}
.ll-INFO{color:var(--primary)}
.ll-WARN{color:var(--orange)}
.ll-ERROR{color:var(--red)}

/* ---- Error toast ---- */
.toast{position:fixed;bottom:20px;right:20px;background:#3d1e20;border:1px solid var(--red);border-radius:var(--r);padding:12px 16px;color:var(--red);font-size:13px;display:none;z-index:999}
</style>
</head>
<body>

<!-- Header -->
<header class="hdr">
  <div class="hdr-logo">&#x26BD; Football</div>
  <div class="hdr-spacer"></div>
  <div class="hdr-info">
    <span class="dot" id="hdrDot"></span>
    <span id="hdrStatus">…</span>
    <span style="color:var(--border)">|</span>
    <span id="hdrLoop" style="color:var(--muted)"></span>
    <span style="color:var(--border)">|</span>
    <span class="hdr-user" id="hdrUser"></span>
  </div>
  <form method="post" action="/logout" style="margin:0">
    <button class="btn-sm danger" type="submit">Đăng xuất</button>
  </form>
</header>

<!-- Main -->
<div class="main">

  <!-- Control panel -->
  <div class="ctrl">
    <div class="ctrl-status">
      <span class="dot" id="ctrlDot"></span>
      <span id="ctrlLabel">Đang kết nối…</span>
    </div>
    <div class="ctrl-meta">
      Fetch gần nhất: <span id="ctrlLast">—</span> &nbsp;|&nbsp;
      Thời gian: <span id="ctrlMs">—</span> &nbsp;|&nbsp;
      Lỗi: <span id="ctrlErr">0</span> &nbsp;|&nbsp;
      Đã lưu: <span id="ctrlSaved">0</span>
    </div>
    <button class="btn btn-warn" id="btnPause" onclick="ctrlPause()">⏸ Tạm dừng</button>
    <button class="btn btn-primary" id="btnResume" onclick="ctrlResume()" style="display:none">▶ Tiếp tục</button>
    <button class="btn" id="btnForce" onclick="ctrlForce()">⚡ Fetch ngay</button>
  </div>

  <!-- Stats -->
  <div class="stats">
    <div class="stat"><div class="stat-n c-all" id="sAll">—</div><div class="stat-l">Tổng</div></div>
    <div class="stat"><div class="stat-n c-live" id="sLive">—</div><div class="stat-l">🔴 Live</div></div>
    <div class="stat"><div class="stat-n c-ht" id="sHt">—</div><div class="stat-l">⏸ HT</div></div>
    <div class="stat"><div class="stat-n c-up" id="sUp">—</div><div class="stat-l">⏱ Sắp đấu</div></div>
    <div class="stat"><div class="stat-n c-ft" id="sFt">—</div><div class="stat-l">✓ Kết thúc</div></div>
  </div>

  <!-- Live matches -->
  <div class="sec">
    <div class="sec-hdr">
      <span class="sec-title">🔴 Đang diễn ra</span>
      <span class="sec-badge" id="liveCount">0</span>
    </div>
    <div class="grid" id="liveGrid"><p class="empty">Không có trận nào đang diễn ra</p></div>
  </div>

  <!-- All matches -->
  <div class="sec">
    <div class="sec-hdr">
      <span class="sec-title">📋 Tất cả trận đấu</span>
      <span class="sec-badge" id="matchCount">0</span>
    </div>
    <div class="tbar">
      <input class="tinput" id="searchInput" type="search" placeholder="Tìm đội, giải đấu…">
      <select class="tsel" id="statusFilter">
        <option value="">Tất cả trạng thái</option>
        <option value="LIVE">LIVE</option>
        <option value="HT">HT</option>
        <option value="UPCOMING">UPCOMING</option>
        <option value="FT">FT</option>
      </select>
    </div>
    <div class="twrap">
      <table>
        <thead>
          <tr>
            <th>Giải đấu</th><th>Đội nhà</th><th>Đội khách</th>
            <th style="text-align:center">Tỷ số</th><th>Trạng thái</th><th>Phút</th>
            <th>Tài xỉu</th><th>Kèo chấp</th><th>1X2</th><th>Giờ (GMT+7)</th>
          </tr>
        </thead>
        <tbody id="matchesTbody"></tbody>
      </table>
    </div>
  </div>

  <!-- System logs -->
  <div class="sec">
    <div class="sec-hdr">
      <span class="sec-title">🖥 System Log</span>
      <button class="btn-sm" onclick="clearLogs()" style="margin-left:auto">Xóa</button>
    </div>
    <div class="logbox" id="logbox"></div>
  </div>

</div>

<div class="toast" id="toast"></div>

<script>
// ------------------------------------------------------------------ state
let allMatches = [];
let searchQ = '';
let statusF = '';
let localLogs = [];
let currentUser = '';

// ------------------------------------------------------------------ utils
function fmt(utcStr) {
  if (!utcStr) return '—';
  return new Date(utcStr).toLocaleString('vi-VN', {
    month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit',
    timeZone:'Asia/Ho_Chi_Minh'
  });
}

function fmtAgo(isoStr) {
  if (!isoStr) return '—';
  const diff = Math.round((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (diff < 5)  return 'vừa xong';
  if (diff < 60) return diff + 's trước';
  return Math.round(diff / 60) + 'm trước';
}

function badge(status) {
  const live = ['LIVE','H1','H2','INJURY_TIME_H1','INJURY_TIME_H2'];
  if (live.includes(status)) return '<span class="badge b-live">LIVE</span>';
  if (status === 'HT')       return '<span class="badge b-ht">HT</span>';
  if (status === 'UPCOMING') return '<span class="badge b-up">UPCOMING</span>';
  if (status === 'FT')       return '<span class="badge b-ft">FT</span>';
  return '<span class="badge b-x">' + status + '</span>';
}

function isLive(s) {
  return ['LIVE','H1','H2','INJURY_TIME_H1','INJURY_TIME_H2','HT'].includes(s);
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 3500);
}

// ------------------------------------------------------------------ api
async function apiFetch(url, opts) {
  const r = await fetch(url, opts);
  if (r.status === 401) { location.href = '/login'; return null; }
  return r.ok ? r.json() : null;
}

// ------------------------------------------------------------------ status poll
async function pollStatus() {
  const data = await apiFetch('/api/status');
  if (!data) return;

  const c = data.collector;
  currentUser = data.user || '';
  document.getElementById('hdrUser').textContent = currentUser;

  // Dot + label
  const runDot  = c.running && !c.paused;
  const dotId   = ['hdrDot','ctrlDot'];
  const dotCls  = c.running ? (c.paused ? 'dot dot-orange' : 'dot dot-green') : 'dot dot-red';
  dotId.forEach(id => document.getElementById(id).className = dotCls);
  document.getElementById('hdrStatus').textContent =
    c.running ? (c.paused ? 'Tạm dừng' : 'Đang chạy') : 'Đã dừng';
  document.getElementById('hdrLoop').textContent = 'Loop #' + c.loop_count;
  document.getElementById('ctrlLabel').textContent =
    c.running ? (c.paused ? '⏸ Đã tạm dừng' : '● Đang chạy') : '■ Đã dừng';

  // Control buttons
  document.getElementById('btnPause').style.display  = (c.running && !c.paused) ? '' : 'none';
  document.getElementById('btnResume').style.display = (c.running && c.paused)  ? '' : 'none';

  // Meta
  document.getElementById('ctrlLast').textContent  = fmtAgo(c.last_fetch_at);
  document.getElementById('ctrlMs').textContent    = c.last_fetch_ms ? c.last_fetch_ms + ' ms' : '—';
  document.getElementById('ctrlErr').textContent   = c.error_count;
  document.getElementById('ctrlSaved').textContent = c.session_saved;

  // Stats
  const s = data.stats;
  document.getElementById('sAll').textContent  = s.total;
  document.getElementById('sLive').textContent = s.live;
  document.getElementById('sHt').textContent   = s.ht;
  document.getElementById('sUp').textContent   = s.upcoming;
  document.getElementById('sFt').textContent   = s.ft;

  // Logs
  const newLogs = data.logs || [];
  if (newLogs.length !== localLogs.length || (newLogs.length > 0 && newLogs[newLogs.length-1].t !== (localLogs[localLogs.length-1]||{}).t)) {
    localLogs = newLogs;
    renderLogs();
  }
}

// ------------------------------------------------------------------ matches poll
async function pollMatches() {
  const data = await apiFetch('/api/matches');
  if (!data) return;
  allMatches = data;
  renderLive();
  renderTable();
}

// ------------------------------------------------------------------ render live cards
function renderLive() {
  const live = allMatches.filter(m => isLive(m.status));
  document.getElementById('liveCount').textContent = live.length;
  const el = document.getElementById('liveGrid');
  if (!live.length) {
    el.innerHTML = '<p class="empty">Không có trận nào đang diễn ra</p>';
    return;
  }
  el.innerHTML = live.map(m => {
    const cls = m.status === 'HT' ? 'ht' : 'live';
    const min = m.status === 'HT' ? 'HT' : (m.minute ? m.minute + "'" : '0\'');
    const ou  = m.ou_line   ? '<div class="chip">OU <b>' + m.ou_line + '</b> O' + (m.over_odds||'?') + ' U' + (m.under_odds||'?') + '</div>' : '';
    const hc  = m.home_handicap ? '<div class="chip">HC <b>' + m.home_handicap + '</b> ' + (m.home_handicap_odds||'?') + ' / <b>' + m.away_handicap + '</b> ' + (m.away_handicap_odds||'?') + '</div>' : '';
    const x2  = m.odds_1 ? '<div class="chip">1X2 <b>' + m.odds_1 + '</b> ' + (m.odds_x||'?') + ' <b>' + m.odds_2 + '</b></div>' : '';
    return '<div class="mcard ' + cls + '">' +
      '<div class="mcomp">' + m.competition + '</div>' +
      '<div class="mteams"><div class="mteam">' + m.home + '</div>' +
      '<div class="mscore">' + m.home_score + ' - ' + m.away_score + '</div>' +
      '<div class="mteam away">' + m.away + '</div></div>' +
      '<div class="minfo"><span>' + fmt(m.start_time_utc) + '</span><span class="mmin">' + min + '</span></div>' +
      '<div class="modds">' + ou + hc + x2 + '</div>' +
      '</div>';
  }).join('');
}

// ------------------------------------------------------------------ render table
function renderTable() {
  let rows = allMatches;
  if (statusF) rows = rows.filter(m => m.status === statusF);
  if (searchQ) {
    const q = searchQ.toLowerCase();
    rows = rows.filter(m =>
      m.home.toLowerCase().includes(q) ||
      m.away.toLowerCase().includes(q) ||
      m.competition.toLowerCase().includes(q)
    );
  }
  document.getElementById('matchCount').textContent = rows.length;
  const tbody = document.getElementById('matchesTbody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty">Không có kết quả</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(m => {
    const min = m.status === 'HT' ? 'HT' : (m.minute ? m.minute + "'" : '—');
    const ou  = m.ou_line ? m.ou_line + ' O' + (m.over_odds||'?') + '/U' + (m.under_odds||'?') : '—';
    const hc  = m.home_handicap ? m.home_handicap + '/' + (m.home_handicap_odds||'?') : '—';
    const x2  = m.odds_1 ? m.odds_1 + '/' + (m.odds_x||'?') + '/' + m.odds_2 : '—';
    return '<tr>' +
      '<td title="' + m.competition + '">' + m.competition.substring(0,30) + '</td>' +
      '<td><b>' + m.home + '</b></td>' +
      '<td><b>' + m.away + '</b></td>' +
      '<td style="text-align:center;font-weight:700">' + m.home_score + ' - ' + m.away_score + '</td>' +
      '<td>' + badge(m.status) + '</td>' +
      '<td>' + min + '</td>' +
      '<td style="color:var(--muted)">' + ou + '</td>' +
      '<td style="color:var(--muted)">' + hc + '</td>' +
      '<td style="color:var(--muted)">' + x2 + '</td>' +
      '<td style="color:var(--muted)">' + fmt(m.start_time_utc) + '</td>' +
      '</tr>';
  }).join('');
}

// ------------------------------------------------------------------ render logs
function renderLogs() {
  const box = document.getElementById('logbox');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
  box.innerHTML = localLogs.map(e =>
    '<div class="le"><span class="lt">' + e.t + '</span>' +
    '<span class="ll-' + e.l + '">[' + e.l + ']</span> ' + e.m + '</div>'
  ).join('');
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function clearLogs() { localLogs = []; renderLogs(); }

// ------------------------------------------------------------------ controls
async function ctrlPause() {
  const r = await apiFetch('/api/collector/pause', {method:'POST'});
  if (r) showToast('Đã tạm dừng collector');
  pollStatus();
}
async function ctrlResume() {
  const r = await apiFetch('/api/collector/resume', {method:'POST'});
  if (r) showToast('Đã tiếp tục collector');
  pollStatus();
}
async function ctrlForce() {
  const r = await apiFetch('/api/collector/force', {method:'POST'});
  if (r) { showToast('Đang fetch dữ liệu ngay…'); setTimeout(pollMatches, 2000); }
}

// ------------------------------------------------------------------ filters
document.getElementById('searchInput').addEventListener('input', e => {
  searchQ = e.target.value; renderTable();
});
document.getElementById('statusFilter').addEventListener('change', e => {
  statusF = e.target.value; renderTable();
});

// ------------------------------------------------------------------ init
pollStatus();
pollMatches();
setInterval(pollStatus,  5000);
setInterval(pollMatches, 12000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# App lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app_state.setup()
    app_state.log("INFO", "Application starting up")
    app_state._task = asyncio.create_task(run_collector(app_state))
    yield
    app_state.running = False
    if app_state._task:
        app_state._task.cancel()
        try:
            await app_state._task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Football Dashboard", lifespan=lifespan)


# ---- Security headers on every response -----------------------------------
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ---- Redirect 401 → /login ------------------------------------------------
@app.exception_handler(401)
async def _auth_redirect(request: Request, exc):
    return RedirectResponse(url="/login", status_code=302)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str = "", auth_token: Optional[str] = None):
    # Already logged in?
    if auth_token and decode_token(auth_token):
        return RedirectResponse("/", status_code=302)
    err_html = f'<div class="err">{error}</div>' if error else ""
    return HTMLResponse(_LOGIN_HTML.replace("%%ERROR%%", err_html))


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    ip = request.client.host if request.client else "unknown"

    if not check_rate_limit(ip):
        return RedirectResponse(
            "/login?error=Quá nhiều lần thử. Vui lòng đợi 1 phút.",
            status_code=302,
        )

    if not verify_credentials(username, password):
        app_state.log("WARN", f"Failed login attempt from {ip}")
        return RedirectResponse(
            "/login?error=Sai tên đăng nhập hoặc mật khẩu.",
            status_code=302,
        )

    app_state.log("INFO", f"User '{username}' logged in from {ip}")
    token = create_token(username)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(
        "auth_token", token,
        httponly=True, samesite="lax", secure=False,
        max_age=86400,
    )
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("auth_token")
    return resp


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(user: str = Depends(require_auth)):
    return HTMLResponse(_DASHBOARD_HTML)


# ---------------------------------------------------------------------------
# Data API (all protected)
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status(user: str = Depends(require_auth)):
    return JSONResponse({
        "collector": app_state.to_dict(),
        "logs": app_state.logs[-80:],
        "stats": get_stats(),
        "user": user,
    })


@app.get("/api/matches")
async def api_matches(user: str = Depends(require_auth)):
    return JSONResponse(get_all_matches())


@app.get("/api/live")
async def api_live(user: str = Depends(require_auth)):
    return JSONResponse(get_live_matches())


# ---------------------------------------------------------------------------
# Collector control API
# ---------------------------------------------------------------------------

@app.post("/api/collector/pause")
async def api_pause(user: str = Depends(require_auth)):
    app_state.paused = True
    app_state.pause_event.clear()
    app_state.log("INFO", f"Collector paused by '{user}'")
    return {"ok": True, "paused": True}


@app.post("/api/collector/resume")
async def api_resume(user: str = Depends(require_auth)):
    app_state.paused = False
    app_state.pause_event.set()
    app_state.log("INFO", f"Collector resumed by '{user}'")
    return {"ok": True, "paused": False}


@app.post("/api/collector/force")
async def api_force(user: str = Depends(require_auth)):
    app_state.force_event.set()
    app_state.log("INFO", f"Force-fetch triggered by '{user}'")
    return {"ok": True}
