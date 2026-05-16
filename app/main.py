"""
Football Data Dashboard — web server only.
The data collector runs as a separate process/service (run_collector.py).

Start:  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""
import json
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .auth import check_rate_limit, create_token, decode_token, require_auth, verify_credentials
from .database import (
    get_all_matches,
    get_collector_state,
    get_live_matches,
    get_match_by_id,
    get_match_events,
    get_odds_history,
    get_stats,
    init_db,
    send_collector_command,
)

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
  try {
    const r = await fetch(url, opts);
    if (r.status === 401) { location.href = '/login'; return null; }
    return r.ok ? r.json() : null;
  } catch (e) {
    console.error('[apiFetch]', url, e.message);
    showToast('Loi ket noi: ' + e.message);
    return null;
  }
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
    const min = m.status === 'HT' ? 'HT' : (m.minute ? m.minute + "'" : "0'");
    const ou  = m.ou_line   ? '<div class="chip">OU <b>' + m.ou_line + '</b> O' + (m.over_odds||'?') + ' U' + (m.under_odds||'?') + '</div>' : '';
    const hc  = m.home_handicap ? '<div class="chip">HC <b>' + m.home_handicap + '</b> ' + (m.home_handicap_odds||'?') + ' / <b>' + m.away_handicap + '</b> ' + (m.away_handicap_odds||'?') + '</div>' : '';
    const x2  = m.odds_1 ? '<div class="chip">1X2 <b>' + m.odds_1 + '</b> ' + (m.odds_x||'?') + ' <b>' + m.odds_2 + '</b></div>' : '';
    const href = '/match/' + encodeURIComponent(m.id);
    return '<a href="' + href + '" class="mcard ' + cls + '" style="text-decoration:none;color:inherit;display:block">' +
      '<div class="mcomp">' + m.competition + '</div>' +
      '<div class="mteams"><div class="mteam">' + m.home + '</div>' +
      '<div class="mscore">' + m.home_score + ' - ' + m.away_score + '</div>' +
      '<div class="mteam away">' + m.away + '</div></div>' +
      '<div class="minfo"><span>' + fmt(m.start_time_utc) + '</span><span class="mmin">' + min + '</span></div>' +
      '<div class="modds">' + ou + hc + x2 + '</div>' +
      '</a>';
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
    const href = '/match/' + encodeURIComponent(m.id);
    return '<tr style="cursor:pointer" onclick="location.href=\\'' + href + '\\'">' +
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


_MATCH_DETAIL_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chi tiết trận đấu — Football Dashboard</title>
<style>
:root{
  --bg:#0d1117;--surface:#161b22;--card:#21262d;--border:#30363d;
  --primary:#58a6ff;--pdim:#1f3a5f;
  --red:#f85149;--green:#3fb950;--orange:#d29922;--purple:#bc8cff;
  --text:#c9d1d9;--muted:#8b949e;--r:6px;
  --font:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100vh;display:flex;flex-direction:column}

.hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 24px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:100}
.hdr-back{color:var(--primary);text-decoration:none;font-size:14px;font-weight:600;padding:5px 10px;border-radius:var(--r);border:1px solid var(--border);transition:all .15s}
.hdr-back:hover{background:var(--card);border-color:var(--primary)}
.hdr-title{font-size:15px;font-weight:700;color:var(--text);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.btn-sm{padding:5px 12px;border-radius:var(--r);border:1px solid var(--border);background:var(--card);color:var(--text);cursor:pointer;font-size:12px;transition:all .15s}
.btn-sm:hover{border-color:var(--primary);color:var(--primary)}
.btn-sm.danger:hover{border-color:var(--red);color:var(--red)}

.main{flex:1;padding:24px;max-width:1100px;margin:0 auto;width:100%}

.snap{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:22px;margin-bottom:22px}
.snap-comp{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:10px}
.snap-main{display:flex;align-items:center;gap:18px;margin-bottom:14px}
.snap-team{flex:1;font-size:22px;font-weight:700;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.snap-team.away{text-align:right}
.snap-score{font-size:36px;font-weight:800;color:var(--red);min-width:120px;text-align:center;letter-spacing:1px}
.snap-meta{display:flex;gap:14px;font-size:13px;color:var(--muted);margin-bottom:14px;flex-wrap:wrap}
.snap-meta b{color:var(--text);font-weight:600}
.snap-odds{display:flex;gap:10px;flex-wrap:wrap}
.chip{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:5px 10px;font-size:12px}
.chip b{color:var(--primary)}
.chip-label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px;margin-right:4px}

.badge{display:inline-block;padding:3px 9px;border-radius:3px;font-size:11px;font-weight:700;letter-spacing:.4px}
.b-live{background:rgba(248,81,73,.2);color:var(--red);border:1px solid rgba(248,81,73,.35)}
.b-up{background:rgba(210,153,34,.2);color:var(--orange);border:1px solid rgba(210,153,34,.35)}
.b-ft{background:rgba(63,185,80,.2);color:var(--green);border:1px solid rgba(63,185,80,.35)}
.b-ht{background:rgba(188,140,255,.2);color:var(--purple);border:1px solid rgba(188,140,255,.35)}
.b-x{background:rgba(139,148,158,.15);color:var(--muted);border:1px solid var(--border)}

.sec{margin-bottom:24px}
.sec-hdr{display:flex;align-items:center;gap:10px;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.sec-title{font-size:14px;font-weight:700;letter-spacing:.3px}
.sec-badge{background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:2px 8px;font-size:11px;color:var(--muted)}

.twrap{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px;min-width:600px}
th{background:var(--surface);padding:9px 12px;text-align:left;font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid var(--border);white-space:nowrap;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.025)}
.diff-up{color:var(--green)}
.diff-down{color:var(--red)}

.tline{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:8px}
.tev{display:flex;gap:14px;align-items:center;padding:10px 12px;border-bottom:1px solid var(--border);font-size:13px}
.tev:last-child{border-bottom:none}
.tev-time{color:var(--muted);font-size:11px;font-family:'Consolas','Monaco',monospace;min-width:72px}
.tev-min{background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:2px 7px;font-size:11px;color:var(--orange);font-weight:700;min-width:42px;text-align:center}
.tev-type{font-weight:700;min-width:90px}
.tev-type.goal{color:var(--red)}
.tev-type.status{color:var(--purple)}
.tev-detail{color:var(--text);flex:1}

.empty{color:var(--muted);padding:24px;text-align:center;font-size:13px;background:var(--card);border:1px solid var(--border);border-radius:var(--r)}
.notfound{text-align:center;padding:60px 20px}
.notfound h2{font-size:20px;margin-bottom:10px}
.notfound p{color:var(--muted);margin-bottom:20px}
.notfound a{color:var(--primary);text-decoration:none;font-weight:600}
.notfound a:hover{text-decoration:underline}
.toast{position:fixed;bottom:20px;right:20px;background:#3d1e20;border:1px solid var(--red);border-radius:var(--r);padding:12px 16px;color:var(--red);font-size:13px;display:none;z-index:999}
</style>
</head>
<body>

<header class="hdr">
  <a class="hdr-back" href="/">&larr; Quay lại</a>
  <div class="hdr-title" id="hdrTitle">Đang tải…</div>
  <form method="post" action="/logout" style="margin:0">
    <button class="btn-sm danger" type="submit">Đăng xuất</button>
  </form>
</header>

<div class="main" id="main">
  <div class="empty">Đang tải dữ liệu trận đấu…</div>
</div>

<div class="toast" id="toast"></div>

<script>
const matchId = %%MATCH_ID%%;

function fmt(utcStr){
  if(!utcStr) return '—';
  return new Date(utcStr).toLocaleString('vi-VN',{
    month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',
    timeZone:'Asia/Ho_Chi_Minh'
  });
}
function fmtTime(utcStr){
  if(!utcStr) return '—';
  return new Date(utcStr).toLocaleTimeString('vi-VN',{
    hour:'2-digit',minute:'2-digit',second:'2-digit',
    timeZone:'Asia/Ho_Chi_Minh'
  });
}
function badge(s){
  const live=['LIVE','H1','H2','INJURY_TIME_H1','INJURY_TIME_H2'];
  if(live.includes(s)) return '<span class="badge b-live">'+s+'</span>';
  if(s==='HT') return '<span class="badge b-ht">HT</span>';
  if(s==='UPCOMING') return '<span class="badge b-up">UPCOMING</span>';
  if(s==='FT') return '<span class="badge b-ft">FT</span>';
  return '<span class="badge b-x">'+s+'</span>';
}
function escHtml(s){
  if(s==null) return '';
  return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function showToast(msg){
  const t=document.getElementById('toast');
  t.textContent=msg;t.style.display='block';
  setTimeout(()=>{t.style.display='none';},3500);
}

async function apiFetch(url){
  try{
    const r=await fetch(url);
    if(r.status===401){location.href='/login';return null;}
    if(r.status===404) return {__notfound:true};
    return r.ok?r.json():null;
  }catch(e){
    showToast('Loi ket noi: '+e.message);
    return null;
  }
}

function renderNotFound(){
  document.getElementById('hdrTitle').textContent='Không tìm thấy';
  document.getElementById('main').innerHTML =
    '<div class="notfound"><h2>Không tìm thấy trận đấu</h2>'+
    '<p>Trận đấu này có thể đã bị xóa hoặc ID không hợp lệ.</p>'+
    '<a href="/">← Về dashboard</a></div>';
}

function renderSnapshot(m){
  document.getElementById('hdrTitle').textContent = m.home+' vs '+m.away;
  const min = m.status==='HT' ? 'HT' : (m.minute ? m.minute+"'" : '—');
  const ouChip = m.ou_line
    ? '<div class="chip"><span class="chip-label">OU</span><b>'+m.ou_line+'</b> O '+(m.over_odds||'?')+' / U '+(m.under_odds||'?')+'</div>'
    : '';
  const hcChip = m.home_handicap
    ? '<div class="chip"><span class="chip-label">HC</span>'+escHtml(m.home_handicap)+' @'+(m.home_handicap_odds||'?')+' / '+escHtml(m.away_handicap)+' @'+(m.away_handicap_odds||'?')+'</div>'
    : '';
  const x2Chip = m.odds_1
    ? '<div class="chip"><span class="chip-label">1X2</span><b>'+m.odds_1+'</b> / '+(m.odds_x||'?')+' / <b>'+m.odds_2+'</b></div>'
    : '';
  return ''+
    '<div class="snap">'+
      '<div class="snap-comp">'+escHtml(m.competition)+'</div>'+
      '<div class="snap-main">'+
        '<div class="snap-team">'+escHtml(m.home)+'</div>'+
        '<div class="snap-score">'+m.home_score+' - '+m.away_score+'</div>'+
        '<div class="snap-team away">'+escHtml(m.away)+'</div>'+
      '</div>'+
      '<div class="snap-meta">'+
        '<span>Trạng thái: '+badge(m.status)+'</span>'+
        '<span>Phút: <b>'+min+'</b></span>'+
        '<span>Bắt đầu: <b>'+fmt(m.start_time_utc)+'</b></span>'+
      '</div>'+
      (ouChip||hcChip||x2Chip ? '<div class="snap-odds">'+ouChip+hcChip+x2Chip+'</div>' : '')+
    '</div>';
}

function fmtCell(v){return v==null?'—':v;}
function diff(curr,prev){
  if(curr==null||prev==null||curr===prev) return '';
  return curr>prev?' diff-up':' diff-down';
}

function renderOddsHistory(rows){
  const count = rows.length;
  let html = '<div class="sec"><div class="sec-hdr">'+
    '<span class="sec-title">📊 Biến động kèo</span>'+
    '<span class="sec-badge">'+count+'</span></div>';
  if(!count){
    html += '<div class="empty">Chưa ghi nhận biến động</div></div>';
    return html;
  }
  html += '<div class="twrap"><table><thead><tr>'+
    '<th>Thời gian</th>'+
    '<th>HC nhà</th><th>HC khách</th>'+
    '<th>OU</th><th>Over</th><th>Under</th>'+
    '<th>1</th><th>X</th><th>2</th>'+
    '</tr></thead><tbody>';
  for(let i=0;i<rows.length;i++){
    const r=rows[i];
    const p=i>0?rows[i-1]:null;
    html += '<tr>'+
      '<td style="color:var(--muted)">'+fmtTime(r.captured_at)+'</td>'+
      '<td>'+escHtml(fmtCell(r.home_handicap))+' <span class="'+(p?diff(r.home_handicap_odds,p.home_handicap_odds):'').trim()+'">@'+fmtCell(r.home_handicap_odds)+'</span></td>'+
      '<td>'+escHtml(fmtCell(r.away_handicap))+' <span class="'+(p?diff(r.away_handicap_odds,p.away_handicap_odds):'').trim()+'">@'+fmtCell(r.away_handicap_odds)+'</span></td>'+
      '<td>'+escHtml(fmtCell(r.ou_line))+'</td>'+
      '<td class="'+(p?diff(r.over_odds,p.over_odds):'').trim()+'">'+fmtCell(r.over_odds)+'</td>'+
      '<td class="'+(p?diff(r.under_odds,p.under_odds):'').trim()+'">'+fmtCell(r.under_odds)+'</td>'+
      '<td class="'+(p?diff(r.odds_1,p.odds_1):'').trim()+'">'+fmtCell(r.odds_1)+'</td>'+
      '<td class="'+(p?diff(r.odds_x,p.odds_x):'').trim()+'">'+fmtCell(r.odds_x)+'</td>'+
      '<td class="'+(p?diff(r.odds_2,p.odds_2):'').trim()+'">'+fmtCell(r.odds_2)+'</td>'+
      '</tr>';
  }
  html += '</tbody></table></div></div>';
  return html;
}

function renderEvents(rows){
  const count=rows.length;
  let html = '<div class="sec"><div class="sec-hdr">'+
    '<span class="sec-title">⏱ Diễn biến trận</span>'+
    '<span class="sec-badge">'+count+'</span></div>';
  if(!count){
    html += '<div class="empty">Chưa ghi nhận sự kiện</div></div>';
    return html;
  }
  html += '<div class="tline">';
  for(const e of rows){
    const typeCls = e.event_type==='GOAL'?'goal':(e.event_type==='STATUS_CHANGE'?'status':'');
    const typeLabel = e.event_type==='GOAL'?'⚽ Bàn thắng':(e.event_type==='STATUS_CHANGE'?'🔁 Trạng thái':e.event_type);
    const min = e.minute ? e.minute+"'" : '—';
    html += '<div class="tev">'+
      '<span class="tev-time">'+fmtTime(e.occurred_at)+'</span>'+
      '<span class="tev-min">'+min+'</span>'+
      '<span class="tev-type '+typeCls+'">'+typeLabel+'</span>'+
      '<span class="tev-detail">'+escHtml(e.detail||'')+' &nbsp;<span style="color:var(--muted)">('+(e.home_score!=null?e.home_score:'?')+'-'+(e.away_score!=null?e.away_score:'?')+')</span></span>'+
      '</div>';
  }
  html += '</div></div>';
  return html;
}

async function load(){
  const [m, odds, events] = await Promise.all([
    apiFetch('/api/matches/'+encodeURIComponent(matchId)),
    apiFetch('/api/matches/'+encodeURIComponent(matchId)+'/odds-history'),
    apiFetch('/api/matches/'+encodeURIComponent(matchId)+'/events'),
  ]);
  if(!m||m.__notfound){renderNotFound();return;}
  document.getElementById('main').innerHTML =
    renderSnapshot(m) +
    renderOddsHistory(odds||[]) +
    renderEvents(events||[]);
}

load();
setInterval(load, 12000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# App lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


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


# ---- Health endpoint (no auth) -------------------------------------------
@app.get("/api/health")
async def health():
    state = get_collector_state()
    return JSONResponse({"status": "ok", "collector": {k: v for k, v in state.items() if k != "logs"}, "stats": get_stats()})

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
        print(f"[WARN] Failed login attempt from {ip}", flush=True)
        return RedirectResponse(
            "/login?error=Sai tên đăng nhập hoặc mật khẩu.",
            status_code=302,
        )

    print(f"[INFO] User '{username}' logged in from {ip}", flush=True)
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


@app.get("/match/{match_id}", response_class=HTMLResponse)
async def match_detail_page(match_id: str, user: str = Depends(require_auth)):
    return HTMLResponse(_MATCH_DETAIL_HTML.replace("%%MATCH_ID%%", json.dumps(match_id)))


# ---------------------------------------------------------------------------
# Data API (all protected)
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status(user: str = Depends(require_auth)):
    state = get_collector_state()
    logs  = state.pop("logs", [])
    return JSONResponse({
        "collector": state,
        "logs": logs[-80:],
        "stats": get_stats(),
        "user": user,
    })


@app.get("/api/matches")
async def api_matches(user: str = Depends(require_auth)):
    return JSONResponse(get_all_matches())


@app.get("/api/live")
async def api_live(user: str = Depends(require_auth)):
    return JSONResponse(get_live_matches())


@app.get("/api/matches/{match_id}")
async def api_match_detail(match_id: str, user: str = Depends(require_auth)):
    m = get_match_by_id(match_id)
    if m is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(m)


@app.get("/api/matches/{match_id}/odds-history")
async def api_match_odds_history(match_id: str, user: str = Depends(require_auth)):
    return JSONResponse(get_odds_history(match_id))


@app.get("/api/matches/{match_id}/events")
async def api_match_events_route(match_id: str, user: str = Depends(require_auth)):
    return JSONResponse(get_match_events(match_id))


# ---------------------------------------------------------------------------
# Collector control API
# ---------------------------------------------------------------------------

@app.post("/api/collector/pause")
async def api_pause(user: str = Depends(require_auth)):
    send_collector_command("pause")
    return {"ok": True, "paused": True}


@app.post("/api/collector/resume")
async def api_resume(user: str = Depends(require_auth)):
    send_collector_command("resume")
    return {"ok": True, "paused": False}


@app.post("/api/collector/force")
async def api_force(user: str = Depends(require_auth)):
    send_collector_command("force")
    return {"ok": True}
