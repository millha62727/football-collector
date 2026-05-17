"""
Football Data Dashboard — web server only.
The data collector runs as a separate process/service (run_collector.py).

Start:  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""
import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

from fastapi import Body, Cookie, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

# OTP / idle-lock config (read once at import). Both surfaceable to JS as well.
_OTP_TTL = max(60, int(os.getenv("TELEGRAM_OTP_TTL", "300")))
_IDLE_LOCK_SECONDS = max(60, int(os.getenv("IDLE_LOCK_SECONDS", "300")))

from .analyzer.views import router as analyzer_router
from .auth import check_rate_limit, create_token, decode_token, require_auth
from .database import (
    get_all_matches,
    get_collector_state,
    get_live_matches,
    get_match_by_id,
    get_match_events,
    get_odds_history,
    get_stats,
    init_db,
    search_matches,
    send_collector_command,
)

# ---------------------------------------------------------------------------
# Static HTML pages (pure strings — no f-string so CSS {} are safe)
# ---------------------------------------------------------------------------

# Shared idle-lock overlay + toast container. Substituted into every protected
# page that pulls in /static/lock.js. Each page just writes %%LOCK_OVERLAY%%.
_LOCK_OVERLAY = """<div id="lockOverlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.97);z-index:9999;align-items:center;justify-content:center;flex-direction:column;gap:12px">
  <div style="font-size:52px">&#x1F512;</div>
  <div style="color:#c9d1d9;font-size:18px;font-weight:700">Phi&#xEA;n &#x111;&#xE3; b&#x1ECB; kh&#xF3;a do kh&#xF4;ng ho&#x1EA1;t &#x111;&#x1ED9;ng</div>
  <div id="lockMsg" style="color:#8b949e;font-size:13px;max-width:360px;text-align:center">B&#x1EA5;m "Xin OTP m&#x1edb;i" &#x111;&#x1EC3; nh&#x1EAD;n m&#xE3; qua Telegram.</div>
  <button id="lockBtnReq" onclick="lockRequestOtp()" style="padding:10px 28px;background:#21262d;border:1px solid #30363d;border-radius:6px;color:#58a6ff;font-size:14px;font-weight:600;cursor:pointer">Xin OTP m&#x1edb;i</button>
  <input id="lockOtp" type="text" inputmode="numeric" maxlength="6" placeholder="------" disabled style="padding:12px 20px;background:#161b22;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:22px;letter-spacing:8px;text-align:center;width:240px;outline:none;font-family:'SF Mono','Consolas',monospace" onkeydown="if(event.key==='Enter')lockVerify()">
  <div id="lockErr" style="color:#f85149;font-size:13px;min-height:18px"></div>
  <button id="lockBtnVerify" onclick="lockVerify()" disabled style="padding:10px 28px;background:#58a6ff;border:none;border-radius:6px;color:#0d1117;font-size:14px;font-weight:700;cursor:pointer">M&#x1EDF; kh&#xF3;a</button>
</div>"""

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Football Dashboard — Login</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--primary:#58a6ff;--danger:#f85149;--text:#c9d1d9;--muted:#8b949e;--green:#3fb950;--r:6px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:40px;width:100%;max-width:400px}
.logo{text-align:center;font-size:52px;margin-bottom:12px}
h1{text-align:center;font-size:22px;font-weight:700;margin-bottom:4px}
.sub{text-align:center;color:var(--muted);font-size:13px;margin-bottom:28px}
label{display:block;font-size:13px;font-weight:600;color:var(--muted);margin-bottom:6px}
input{width:100%;padding:10px 14px;background:#0d1117;border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-size:14px;margin-bottom:16px;transition:border-color .15s}
input:focus{outline:none;border-color:var(--primary)}
input.otp{text-align:center;font-size:22px;letter-spacing:8px;font-family:'SF Mono','Consolas',monospace}
button{width:100%;padding:11px;background:var(--primary);border:none;border-radius:var(--r);color:#0d1117;font-size:14px;font-weight:700;cursor:pointer;margin-top:4px;transition:background .15s}
button:hover{background:#79c0ff}
button:disabled{opacity:.5;cursor:not-allowed}
button.link{background:transparent;color:var(--muted);font-weight:400;font-size:12px;text-decoration:underline;padding:6px;margin-top:8px}
button.link:hover{background:transparent;color:var(--primary)}
.err{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);border-radius:var(--r);padding:10px 14px;color:var(--danger);font-size:13px;margin-bottom:16px}
.ok{background:rgba(63,185,80,.1);border:1px solid rgba(63,185,80,.3);border-radius:var(--r);padding:10px 14px;color:var(--green);font-size:13px;margin-bottom:16px}
.muted{color:var(--muted);font-size:12px;margin-bottom:12px}
.muted b{color:var(--text)}
.hidden{display:none}
</style>
</head>
<body>
<div class="card">
  <div class="logo">&#x26BD;</div>
  <h1>Football Dashboard</h1>
  <p class="sub">Đăng nhập bằng OTP qua Telegram</p>
  <div id="msg"></div>

  <!-- Step 1: enter username, request OTP -->
  <div id="step1">
    <label for="u">Tên đăng nhập</label>
    <input type="text" id="u" placeholder="admin" autofocus autocomplete="username" onkeydown="if(event.key==='Enter')requestOtp()">
    <button id="btnReq" onclick="requestOtp()">Gửi OTP qua Telegram</button>
  </div>

  <!-- Step 2: enter OTP -->
  <div id="step2" class="hidden">
    <div class="muted">Tài khoản: <b id="uShow"></b><br>OTP đã được gửi đến Telegram. Hết hạn sau <b id="ttl">5</b> phút.</div>
    <label for="otp">Mã OTP</label>
    <input type="text" id="otp" class="otp" inputmode="numeric" maxlength="6" placeholder="------" autocomplete="one-time-code" onkeydown="if(event.key==='Enter')verifyOtp()">
    <button id="btnVerify" onclick="verifyOtp()">Đăng nhập</button>
    <button class="link" onclick="resetStep()">Quay lại</button>
    <button class="link" onclick="requestOtp(true)">Gửi lại OTP</button>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
function showMsg(html, cls){ $('msg').innerHTML = html ? '<div class="'+cls+'">'+html+'</div>' : ''; }
function escHtml(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

async function requestOtp(isResend){
  const u = $('u').value.trim();
  if (!u) { showMsg('Nhập tên đăng nhập', 'err'); $('u').focus(); return; }
  showMsg('Đang gửi OTP...', 'ok');
  $('btnReq').disabled = true;
  try {
    const r = await fetch('/api/auth/request-otp', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username: u})
    });
    const j = await r.json();
    if (!r.ok || !j.ok) {
      showMsg(escHtml(j.error || 'Không gửi được OTP'), 'err');
      $('btnReq').disabled = false;
      return;
    }
    $('uShow').textContent = u;
    $('ttl').textContent = Math.max(1, Math.round((j.ttl_seconds||300)/60));
    $('step1').classList.add('hidden');
    $('step2').classList.remove('hidden');
    showMsg(isResend ? 'Đã gửi lại OTP' : 'OTP đã được gửi qua Telegram', 'ok');
    setTimeout(() => $('otp').focus(), 50);
  } catch(e) {
    showMsg('Lỗi kết nối: ' + escHtml(e.message||e), 'err');
    $('btnReq').disabled = false;
  }
}

async function verifyOtp(){
  const u = $('u').value.trim();
  const code = $('otp').value.trim();
  if (!code) { showMsg('Nhập OTP', 'err'); return; }
  $('btnVerify').disabled = true;
  showMsg('Đang xác thực...', 'ok');
  try {
    const r = await fetch('/api/auth/verify-otp', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username: u, otp: code})
    });
    const j = await r.json();
    if (!r.ok || !j.ok) {
      showMsg(escHtml(j.error || 'OTP không đúng hoặc đã hết hạn'), 'err');
      $('otp').value = '';
      $('otp').focus();
      $('btnVerify').disabled = false;
      return;
    }
    showMsg('Đăng nhập thành công, đang chuyển...', 'ok');
    location.href = '/';
  } catch(e) {
    showMsg('Lỗi kết nối: ' + escHtml(e.message||e), 'err');
    $('btnVerify').disabled = false;
  }
}

function resetStep(){
  $('step2').classList.add('hidden');
  $('step1').classList.remove('hidden');
  $('btnReq').disabled = false;
  $('btnVerify').disabled = false;
  $('otp').value = '';
  showMsg('', '');
  $('u').focus();
}
</script>
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
  <a href="/analyzer" class="btn-sm" style="text-decoration:none;display:inline-block">🔍 Analyzer</a>
  <a href="/data" class="btn-sm" style="text-decoration:none">📦 Dữ liệu</a>
  <button class="btn-sm" onclick="goDisguise()">🔄 Đổi</button>
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
        <option value="H1">H1 (Hiệp 1)</option>
        <option value="H2">H2 (Hiệp 2)</option>
        <option value="HT">HT</option>
        <option value="UPCOMING">UPCOMING</option>
        <option value="FT">FT</option>
      </select>
      <select class="tsel" id="sortSelect">
        <option value="">Mặc định</option>
        <option value="recent_goal">Gần đây có bàn</option>
        <option value="high_goals">&gt;3 bàn thắng</option>
        <option value="major">Giải lớn</option>
        <option value="pinned">Ghim ★</option>
      </select>
    </div>
    <div class="twrap">
      <table>
        <thead>
          <tr>
            <th style="width:28px"></th>
            <th>Giải đấu</th><th>Đội nhà</th><th>Đội khách</th>
            <th style="text-align:center">Tỷ số</th><th>Trạng thái</th><th>Phút</th>
            <th>Tài xỉu</th><th>Kèo chấp</th><th>1X2</th><th>Giờ (GMT+7)</th><th></th>
          </tr>
        </thead>
        <tbody id="matchesTbody"></tbody>
      </table>
    </div>
  </div>

  <!-- Timeline stats -->
  <div class="sec">
    <div class="sec-hdr">
      <span class="sec-title">📅 Thống kê hệ thống</span>
      <div style="margin-left:auto;display:flex;gap:6px">
        <button class="btn-sm" id="btnStatsDay" onclick="setStatsPeriod('day')">Hôm nay</button>
        <button class="btn-sm" id="btnStatsMonth" onclick="setStatsPeriod('month')">Tháng này</button>
        <button class="btn-sm" id="btnStatsYear" onclick="setStatsPeriod('year')">Năm nay</button>
      </div>
    </div>
    <div id="statsTimeline" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px"></div>
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

%%LOCK_OVERLAY%%

<div class="toast" id="toast"></div>

<script>
// ------------------------------------------------------------------ state
let allMatches = [];
let searchQ = '';
let statusF = '';
let sortMode = '';
let localLogs = [];
let currentUser = '';
let statsPeriod = 'day';

// ------------------------------------------------------------------ pin system
const PINNED_KEY = 'fbc_pinned';
function getPinned() { try { return new Set(JSON.parse(localStorage.getItem(PINNED_KEY)||'[]')); } catch(_){ return new Set(); } }
function savePinned(s) { localStorage.setItem(PINNED_KEY, JSON.stringify([...s])); }
function togglePin(id, ev) {
  ev && ev.stopPropagation();
  const p = getPinned();
  if (p.has(id)) p.delete(id); else p.add(id);
  savePinned(p);
  renderLive(); renderTable();
}

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
  const pinned = getPinned();
  let live = allMatches.filter(m => isLive(m.status));
  document.getElementById('liveCount').textContent = live.length;
  const el = document.getElementById('liveGrid');
  if (!live.length) {
    el.innerHTML = '<p class="empty">Không có trận nào đang diễn ra</p>';
    return;
  }
  // Sort pinned to top
  if (sortMode === 'pinned') {
    live = [...live].sort((a, b) => (pinned.has(b.id)?1:0) - (pinned.has(a.id)?1:0));
  }
  el.innerHTML = live.map(m => {
    const cls = m.status === 'HT' ? 'ht' : 'live';
    const min = m.status === 'HT' ? 'HT' : (m.minute ? m.minute + "'" : "0'");
    const ou  = m.ou_line   ? '<div class="chip">OU <b>' + m.ou_line + '</b> O' + (m.over_odds||'?') + ' U' + (m.under_odds||'?') + '</div>' : '';
    const hc  = m.home_handicap ? '<div class="chip">HC <b>' + m.home_handicap + '</b> ' + (m.home_handicap_odds||'?') + ' / <b>' + m.away_handicap + '</b> ' + (m.away_handicap_odds||'?') + '</div>' : '';
    const x2  = m.odds_1 ? '<div class="chip">1X2 <b>' + m.odds_1 + '</b> ' + (m.odds_x||'?') + ' <b>' + m.odds_2 + '</b></div>' : '';
    const href = '/match/' + encodeURIComponent(m.id);
    const aHref = '/analyzer?match_id=' + encodeURIComponent(m.id);
    const isPinned = pinned.has(m.id);
    const pinBtn = '<button onclick="togglePin(' + JSON.stringify(m.id) + ',event)" style="position:absolute;top:8px;right:8px;background:none;border:none;cursor:pointer;font-size:14px;opacity:' + (isPinned?'1':'.35') + ';padding:2px">' + (isPinned?'⭐':'☆') + '</button>';
    return '<div class="mcard ' + cls + '" style="position:relative">' +
      pinBtn +
      '<a href="' + href + '" style="text-decoration:none;color:inherit;display:block">' +
      '<div class="mcomp">' + m.competition + '</div>' +
      '<div class="mteams"><div class="mteam">' + m.home + '</div>' +
      '<div class="mscore">' + m.home_score + ' - ' + m.away_score + '</div>' +
      '<div class="mteam away">' + m.away + '</div></div>' +
      '<div class="minfo"><span>' + fmt(m.start_time_utc) + '</span><span class="mmin">' + min + '</span></div>' +
      '<div class="modds">' + ou + hc + x2 + '</div>' +
      '</a>' +
      '<a href="' + aHref + '" style="display:block;text-align:center;padding:5px;font-size:11px;font-weight:700;color:var(--primary);border-top:1px solid var(--border);text-decoration:none;background:rgba(88,166,255,.05)">🔍 Phân tích</a>' +
      '</div>';
  }).join('');
}

// ------------------------------------------------------------------ render table
const MAJOR_LEAGUES = ["Premier","Liga","Bundesliga","Serie","Ligue","Champions","Europa","V.League","MLS","Eredivisie","Primeira"];
function renderTable() {
  const pinned = getPinned();
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
  // Sort / filter by sortMode
  if (sortMode === 'recent_goal') {
    rows = [...rows].sort((a,b) => ((b.home_score+b.away_score)-(a.home_score+a.away_score)) || (isLive(a.status)?-1:isLive(b.status)?1:0));
  } else if (sortMode === 'high_goals') {
    rows = rows.filter(m => (m.home_score||0)+(m.away_score||0) > 3);
  } else if (sortMode === 'major') {
    rows = rows.filter(m => MAJOR_LEAGUES.some(l => (m.competition||'').includes(l)));
  } else if (sortMode === 'pinned') {
    rows = [...rows].sort((a,b) => (pinned.has(b.id)?1:0) - (pinned.has(a.id)?1:0));
  }
  // Always bubble pinned to top regardless of sort mode
  if (sortMode !== 'pinned') {
    rows = [...rows.filter(m => pinned.has(m.id)), ...rows.filter(m => !pinned.has(m.id))];
  }
  document.getElementById('matchCount').textContent = rows.length;
  const tbody = document.getElementById('matchesTbody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="12" class="empty">Không có kết quả</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(m => {
    const min = m.status === 'HT' ? 'HT' : (m.minute ? m.minute + "'" : '—');
    const ou  = m.ou_line ? m.ou_line + ' O' + (m.over_odds||'?') + '/U' + (m.under_odds||'?') : '—';
    const hc  = m.home_handicap ? m.home_handicap + '/' + (m.home_handicap_odds||'?') : '—';
    const x2  = m.odds_1 ? m.odds_1 + '/' + (m.odds_x||'?') + '/' + m.odds_2 : '—';
    const href = '/match/' + encodeURIComponent(m.id);
    const isPinned = pinned.has(m.id);
    const pinTd = '<td style="padding:0;text-align:center"><button onclick="togglePin(' + JSON.stringify(m.id) + ',event)" style="background:none;border:none;cursor:pointer;font-size:13px;opacity:' + (isPinned?'1':'.3') + ';padding:4px">' + (isPinned?'⭐':'☆') + '</button></td>';
    return '<tr style="cursor:pointer" onclick="location.href=\\'' + href + '\\'">' +
      pinTd +
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
      '<td><a href="/analyzer?match_id=' + encodeURIComponent(m.id) + '" style="color:var(--primary);font-size:11px;font-weight:700;text-decoration:none">🔍</a></td>' +
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

// ------------------------------------------------------------------ timeline stats
function setStatsPeriod(p) { statsPeriod = p; pollTimeline(); }
async function pollTimeline() {
  const data = await apiFetch('/api/stats/timeline?period=' + statsPeriod);
  if (!data) return;
  const el = document.getElementById('statsTimeline');
  if (!el) return;
  el.innerHTML = [
    {n: data.matches, l:'Trận đấu', c:'c-all'},
    {n: data.goals, l:'Bàn thắng', c:'c-live'},
    {n: data.live, l:'Đang live', c:'c-live'},
    {n: data.finished, l:'Kết thúc', c:'c-ft'},
    {n: data.competitions, l:'Giải đấu', c:'c-ht'},
  ].map(s=>'<div class="stat"><div class="stat-n '+s.c+'">'+(s.n!=null?s.n:'—')+'</div><div class="stat-l">'+s.l+'</div></div>').join('');
}

// ------------------------------------------------------------------ filters
document.getElementById('searchInput').addEventListener('input', e => {
  searchQ = e.target.value; renderTable();
});
document.getElementById('statusFilter').addEventListener('change', e => {
  statusF = e.target.value; renderTable();
});
document.getElementById('sortSelect').addEventListener('change', e => {
  sortMode = e.target.value; renderTable(); renderLive();
});

// ------------------------------------------------------------------ init
pollStatus();
pollMatches();
pollTimeline();
setInterval(pollStatus,  5000);
setInterval(pollMatches, 12000);
setInterval(pollTimeline, 30000);
</script>
<script src="/static/lock.js"></script>
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
  <a class="btn-sm" id="analyzeBtn" href="#" style="text-decoration:none">🔍 Phân tích</a>
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
  document.getElementById('analyzeBtn').href = '/analyzer?match_id=' + encodeURIComponent(matchId);
  document.getElementById('main').innerHTML =
    renderSnapshot(m) +
    renderOddsHistory(odds||[]) +
    renderEvents(events||[]);
}

load();
setInterval(load, 12000);
</script>
%%LOCK_OVERLAY%%
<script src="/static/lock.js"></script>
</body>
</html>"""


_MARKET_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CryptoWatch — Live Markets</title>
<style>
:root{--bg:#0a0e1a;--card:#0f1629;--border:#1e2a42;--primary:#00c2ff;--green:#00e676;--red:#ff4444;--text:#e2e8f0;--muted:#64748b;--r:8px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.nav{background:var(--card);border-bottom:1px solid var(--border);padding:14px 28px;display:flex;align-items:center;gap:24px}
.nav-brand{font-size:18px;font-weight:800;color:var(--primary);letter-spacing:-0.5px}
.nav-links{display:flex;gap:20px}
.nav-links a{color:var(--muted);text-decoration:none;font-size:14px;transition:color .15s}
.nav-links a:hover{color:var(--text)}
.nav-links a.active{color:var(--primary);font-weight:600}
.nav-spacer{flex:1}
.nav-price{font-size:12px;color:var(--muted)}
.main{padding:28px;max-width:1300px;margin:0 auto}
h2{font-size:20px;font-weight:700;margin-bottom:18px;color:var(--text)}
.hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:28px}
.hero-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:18px}
.hc-sym{font-size:13px;font-weight:700;color:var(--muted);letter-spacing:.5px;margin-bottom:4px}
.hc-price{font-size:24px;font-weight:800;margin-bottom:6px}
.hc-chg{font-size:13px;font-weight:600}
.up{color:var(--green)}.dn{color:var(--red)}
.tbl-wrap{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:13px}
th{padding:12px 16px;text-align:left;font-size:11px;font-weight:700;color:var(--muted);border-bottom:1px solid var(--border);text-transform:uppercase;letter-spacing:.5px}
td{padding:12px 16px;border-bottom:1px solid rgba(255,255,255,.04);white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(0,194,255,.03)}
.sym{font-weight:700;color:var(--text)}
.bar{display:inline-block;width:80px;height:4px;background:#1e2a42;border-radius:2px;overflow:hidden;vertical-align:middle;margin-left:8px}
.bar-fill{height:100%;background:var(--green);border-radius:2px;transition:width .5s}
</style>
</head>
<body>
<nav class="nav">
  <div class="nav-brand">&#x2B21; CryptoWatch</div>
  <div class="nav-links">
    <a href="#" class="active">Markets</a>
    <a href="#">Portfolio</a>
    <a href="#">Watchlist</a>
    <a href="#">News</a>
  </div>
  <div class="nav-spacer"></div>
  <div class="nav-price" id="btcNav">BTC $&#x2014;</div>
</nav>
<div class="main">
  <div class="hero" id="hero"></div>
  <h2>All Markets</h2>
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>#</th><th>Name</th><th>Price</th><th>24h %</th><th>Market Cap</th><th>Volume 24h</th><th>7d Range</th></tr></thead>
      <tbody id="tbl"></tbody>
    </table>
  </div>
</div>
<script>
const BASE = {BTC:67432.18,ETH:3521.44,BNB:587.22,SOL:178.95,XRP:0.6234,ADA:0.4821,DOT:7.34,AVAX:36.72,MATIC:0.8821,LINK:14.92,LTC:82.44,UNI:8.73,ATOM:9.11,ALGO:0.1893};
const state = {};
for(const [k,v] of Object.entries(BASE)) state[k]={p:v,p0:v,chg:0};
const CAPS = {BTC:1320000,ETH:423000,BNB:88000,SOL:79000,XRP:34000,ADA:17000,DOT:10000,AVAX:15000,MATIC:8000,LINK:8500,LTC:6100,UNI:5200,ATOM:3300,ALGO:1500};
function rng(min,max){return min+(Math.random()*(max-min));}
function tick(){
  for(const k of Object.keys(state)){
    const s=state[k]; const d=(Math.random()-.5)*s.p0*0.0012; s.p=Math.max(s.p0*.7,s.p+d);
    s.chg=((s.p-s.p0)/s.p0*100);
  }
  render();
}
function fmt(n,d=2){return n>=1e9?(n/1e9).toFixed(2)+'B':n>=1e6?(n/1e6).toFixed(2)+'M':n.toFixed(d);}
function render(){
  const keys=Object.keys(state);
  document.getElementById('hero').innerHTML=keys.slice(0,4).map(k=>{
    const s=state[k]; const up=s.chg>=0;
    return '<div class="hero-card"><div class="hc-sym">'+k+'/USDT</div>'+
      '<div class="hc-price">$'+fmt(s.p,s.p<1?4:2)+'</div>'+
      '<div class="hc-chg '+(up?'up':'dn')+'">'+(up?'&#x25B2;':'&#x25BC;')+' '+Math.abs(s.chg).toFixed(2)+'%</div></div>';
  }).join('');
  document.getElementById('btcNav').textContent='BTC $'+fmt(state.BTC.p,0);
  let r=''; keys.forEach((k,i)=>{
    const s=state[k]; const up=s.chg>=0; const vol=CAPS[k]*(rng(.08,.14));
    const hi=s.p0*rng(1.005,1.025); const lo=s.p0*rng(.975,.995);
    const pct=(s.p-lo)/(hi-lo)*100;
    r+='<tr><td style="color:var(--muted)">'+(i+1)+'</td>'+
      '<td class="sym">'+k+'<span style="color:var(--muted);font-weight:400;margin-left:6px">'+k+'USDT</span></td>'+
      '<td>$'+fmt(s.p,s.p<1?4:2)+'</td>'+
      '<td class="'+(up?'up':'dn')+'">'+(up?'+':'')+s.chg.toFixed(2)+'%</td>'+
      '<td>$'+fmt(CAPS[k]*1e6)+'</td>'+
      '<td>$'+fmt(vol*1e6)+'</td>'+
      '<td><span style="color:var(--muted);font-size:11px">$'+fmt(s.p0*.975,s.p<1?4:2)+'</span>'+
      '<div class="bar"><div class="bar-fill" style="width:'+pct.toFixed(0)+'%"></div></div>'+
      '<span style="color:var(--muted);font-size:11px">$'+fmt(s.p0*1.025,s.p<1?4:2)+'</span></td></tr>';
  });
  document.getElementById('tbl').innerHTML=r;
}
render();
setInterval(tick,2200);
</script>
</body>
</html>"""

_DATA_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Data Browser &#x2014; Football Dashboard</title>
<style>
:root{--bg:#0d1117;--surface:#161b22;--card:#21262d;--border:#30363d;--primary:#58a6ff;--pdim:#1f3a5f;--red:#f85149;--green:#3fb950;--orange:#d29922;--purple:#bc8cff;--text:#c9d1d9;--muted:#8b949e;--r:6px;--font:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100vh;display:flex;flex-direction:column}
.hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 24px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:50}
.hdr-logo{font-size:16px;font-weight:700;color:var(--primary)}
.hdr a{color:var(--muted);text-decoration:none;font-size:13px}
.hdr a:hover{color:var(--primary)}
.hdr-spacer{flex:1}
.btn-sm{padding:5px 12px;border-radius:var(--r);border:1px solid var(--border);background:var(--card);color:var(--text);cursor:pointer;font-size:12px;transition:all .15s;text-decoration:none;display:inline-block}
.btn-sm:hover{border-color:var(--primary);color:var(--primary)}
.main{flex:1;display:flex;gap:0}
.sidebar{width:340px;min-width:280px;border-right:1px solid var(--border);display:flex;flex-direction:column;height:calc(100vh - 45px);position:sticky;top:45px;overflow:hidden}
.search-panel{padding:12px;border-bottom:1px solid var(--border)}
.sinput{width:100%;padding:7px 10px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-size:12px;margin-bottom:6px}
.sinput:focus{outline:none;border-color:var(--primary)}
.sflex{display:flex;gap:6px}
.slist{flex:1;overflow-y:auto}
.smatch{padding:10px 12px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s;font-size:12px}
.smatch:hover{background:rgba(88,166,255,.05)}
.smatch.sel{background:var(--pdim);border-left:3px solid var(--primary)}
.smatch.cmp{background:rgba(63,185,80,.08);border-left:3px solid var(--green)}
.smatch-teams{font-weight:700;margin-bottom:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.smatch-meta{color:var(--muted);font-size:11px;display:flex;gap:8px}
.smatch-score{color:var(--red);font-weight:700}
.smatch-empty{color:var(--muted);text-align:center;padding:24px;font-size:12px}
.content{flex:1;overflow-y:auto;padding:16px;height:calc(100vh - 45px)}
.badge{display:inline-block;padding:2px 6px;border-radius:3px;font-size:10px;font-weight:700}
.b-live{background:rgba(248,81,73,.2);color:var(--red);border:1px solid rgba(248,81,73,.35)}
.b-ft{background:rgba(63,185,80,.2);color:var(--green);border:1px solid rgba(63,185,80,.35)}
.b-ht{background:rgba(188,140,255,.2);color:var(--purple);border:1px solid rgba(188,140,255,.35)}
.b-up{background:rgba(210,153,34,.2);color:var(--orange);border:1px solid rgba(210,153,34,.35)}
.b-x{background:rgba(139,148,158,.15);color:var(--muted);border:1px solid var(--border)}
.split{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.panel{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;margin-bottom:14px}
.panel-h{padding:8px 12px;background:var(--surface);border-bottom:1px solid var(--border);font-size:12px;font-weight:700;color:var(--primary);display:flex;align-items:center;gap:8px}
.panel-b{padding:0;overflow-x:auto}
table.ot{width:100%;border-collapse:collapse;font-size:11px;min-width:500px}
table.ot th{padding:7px 8px;background:var(--surface);color:var(--muted);font-weight:600;border-bottom:1px solid var(--border);font-size:10px;text-transform:uppercase;white-space:nowrap;text-align:center}
table.ot td{padding:6px 8px;border-bottom:1px solid #1a1f26;text-align:center;white-space:nowrap;font-variant-numeric:tabular-nums}
table.ot tr:last-child td{border-bottom:none}
table.ot tr:hover td{background:rgba(255,255,255,.02)}
.diff-up{color:var(--green)}.diff-down{color:var(--red)}
.actions-bar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.btn{padding:6px 14px;border-radius:var(--r);border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;font-size:12px;font-weight:600;transition:all .15s}
.btn:hover{border-color:var(--primary);color:var(--primary)}
.btn-green{border-color:var(--green);color:var(--green);background:rgba(63,185,80,.1)}
.btn-green:hover{background:var(--green);color:#000}
.empty{color:var(--muted);text-align:center;padding:48px;font-size:13px}
.toast{position:fixed;bottom:20px;right:20px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 16px;font-size:12px;display:none;z-index:200}
</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-logo">&#x1F4E6; Data Browser</div>
  <a href="/">&larr; Dashboard</a>
  <a href="/analyzer">&#x1F50D; Analyzer</a>
  <div class="hdr-spacer"></div>
  <button class="btn-sm" onclick="goDisguise()">&#x1F504; &#x110;&#x1ED5;i</button>
  <form method="post" action="/logout" style="display:inline;margin:0">
    <button type="submit" class="btn-sm" style="border-color:var(--red);color:var(--red)">&#x110;&#x103;ng xu&#x1EA5;t</button>
  </form>
</div>
<div class="main">
  <div class="sidebar">
    <div class="search-panel">
      <input class="sinput" id="sqText" type="search" placeholder="T&#xEC;m &#x111;&#x1ED9;i, gi&#x1EA3;i &#x111;&#x1EA5;u...">
      <div class="sflex">
        <input class="sinput" id="sqFrom" type="date" style="flex:1" title="T&#x1EEB; ng&#xE0;y">
        <input class="sinput" id="sqTo" type="date" style="flex:1" title="&#x110;&#x1EBF;n ng&#xE0;y">
      </div>
      <div class="sflex">
        <select class="sinput" id="sqStatus" style="flex:1">
          <option value="">M&#x1ECD;i tr&#x1EA1;ng th&#xE1;i</option>
          <option value="H1">H1</option>
          <option value="H2">H2</option>
          <option value="HT">HT</option>
          <option value="FT">FT</option>
          <option value="UPCOMING">UPCOMING</option>
        </select>
        <button class="btn-sm" onclick="doSearch()" style="flex:0 0 auto">&#x1F50D; T&#xEC;m</button>
      </div>
      <button class="btn-sm" onclick="openImportModal()" style="width:100%;margin-top:8px;border-color:var(--green);color:var(--green)">&#x1F4E5; Import CSV</button>
    </div>
    <div class="slist" id="slist"><div class="smatch-empty">Nh&#x1EAD;p t&#x1EEB; kh&#xF3;a &#x111;&#x1EC3; t&#xEC;m ki&#x1EBF;m</div></div>
  </div>
  <div class="content">
    <div id="contentArea"><div class="empty">&larr; Ch&#x1ECD;n m&#x1ED9;t tr&#x1EAD;n &#x111;&#x1EA5;u &#x111;&#x1EC3; xem chi ti&#x1EBF;t</div></div>
  </div>
</div>

<!-- Import CSV modal -->
<div id="importModalBg" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:200;align-items:center;justify-content:center" onclick="if(event.target===this)closeImportModal()">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:20px;width:560px;max-width:94vw">
    <div style="display:flex;align-items:center;margin-bottom:14px">
      <h3 style="margin:0;color:var(--primary);font-size:15px">&#x1F4E5; Import CSV t&#x1EEB; tool c&#x0169;</h3>
      <button onclick="closeImportModal()" style="margin-left:auto;background:none;border:0;color:var(--muted);cursor:pointer;font-size:20px;line-height:1">&times;</button>
    </div>
    <div id="importDrop" tabindex="0" style="border:1px dashed var(--border);border-radius:var(--r);padding:24px;text-align:center;cursor:pointer;color:var(--muted);transition:all .15s">
      <div style="font-size:28px;margin-bottom:6px">&#x1F4C2;</div>
      <strong>Th&#x1EA3; file CSV v&#xE0;o &#x111;&#xE2;y</strong>
      <div style="font-size:11px;margin-top:4px">ho&#x1EB7;c click &#x111;&#x1EC3; ch&#x1ECD;n nhi&#x1EC1;u file</div>
      <input type="file" id="importFile" accept=".csv,text/csv" multiple style="display:none">
    </div>
    <div id="importProgress" style="margin-top:12px;font-size:12px;color:var(--muted);min-height:18px"></div>
    <div id="importResult" style="margin-top:8px;font-size:12px"></div>
    <div style="margin-top:14px;font-size:11px;color:var(--muted)">Pattern t&#xEA;n file: <code>YYYYMMDD_HHMM_&lt;league&gt;-&lt;home&gt;_vs_&lt;away&gt;.csv</code></div>
  </div>
</div>

%%LOCK_OVERLAY%%
<div class="toast" id="toast"></div>
<script>
let selectedA = null, selectedB = null;
const matches_cache = {};

function escHtml(s){if(s==null)return '';return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function fmt(utcStr){if(!utcStr)return '—';return new Date(utcStr).toLocaleString('vi-VN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Ho_Chi_Minh'});}
function fmtTime(utcStr){if(!utcStr)return '—';return new Date(utcStr).toLocaleTimeString('vi-VN',{hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:'Asia/Ho_Chi_Minh'});}
function badge(s){const live=['LIVE','H1','H2'];if(live.includes(s))return '<span class="badge b-live">'+s+'</span>';if(s==='HT')return '<span class="badge b-ht">HT</span>';if(s==='FT')return '<span class="badge b-ft">FT</span>';if(s==='UPCOMING')return '<span class="badge b-up">UP</span>';return '<span class="badge b-x">'+escHtml(s)+'</span>';}
function showToast(m){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2800);}
async function apiFetch(url){try{const r=await fetch(url);if(r.status===401){location.href='/login';return null;}return r.ok?r.json():null;}catch(e){showToast('L\\u1ed7i: '+e.message);return null;}}

async function doSearch() {
  const q = document.getElementById('sqText').value;
  const from_ = document.getElementById('sqFrom').value;
  const to_ = document.getElementById('sqTo').value;
  const st = document.getElementById('sqStatus').value;
  const params = new URLSearchParams({q, date_from:from_, date_to:to_, status:st, limit:300});
  const data = await apiFetch('/api/data/matches?' + params.toString());
  const sl = document.getElementById('slist');
  if (!data || !data.length) {
    sl.innerHTML = '<div class="smatch-empty">Kh\\u00f4ng t\\u00ecm th\\u1ea5y tr\\u1eadn n\\u00e0o</div>'; return;
  }
  sl.innerHTML = data.map(m => {
    const clsA = selectedA && selectedA.id===m.id ? ' sel' : '';
    const clsB = selectedB && selectedB.id===m.id ? ' cmp' : '';
    return '<div class="smatch'+clsA+clsB+'" onclick="selectMatch(\\'' + escHtml(m.id) + '\\')" data-id="'+escHtml(m.id)+'">' +
      '<div class="smatch-teams">'+escHtml(m.home)+' <span class="smatch-score">'+m.home_score+'-'+m.away_score+'</span> '+escHtml(m.away)+'</div>' +
      '<div class="smatch-meta"><span>'+badge(m.status)+'</span><span>'+escHtml((m.competition||'').substring(0,25))+'</span><span>'+fmt(m.start_time_utc)+'</span></div>' +
      '</div>';
  }).join('');
  data.forEach(m => matches_cache[m.id] = m);
}

document.getElementById('sqText').addEventListener('keydown', e => { if(e.key==='Enter') doSearch(); });

async function selectMatch(id) {
  const m = matches_cache[id];
  if (!m) return;
  selectedA = m;
  updateSidebarSel();
  await renderDetail(id, 'A');
}

async function setCompare(id) {
  const m = matches_cache[id];
  if (!m) return;
  if (selectedB && selectedB.id === id) { selectedB = null; updateSidebarSel(); renderDetail(selectedA.id, 'A'); return; }
  selectedB = m;
  updateSidebarSel();
  if (selectedA) await renderSplit();
}

function updateSidebarSel() {
  document.querySelectorAll('.smatch').forEach(el => {
    el.classList.remove('sel','cmp');
    const id = el.dataset.id;
    if (selectedA && id===selectedA.id) el.classList.add('sel');
    if (selectedB && id===selectedB.id) el.classList.add('cmp');
  });
}

async function renderDetail(id, which) {
  const oddsData = await apiFetch('/api/matches/'+encodeURIComponent(id)+'/odds-history');
  const m = matches_cache[id];
  const analyzeHref = '/analyzer?match_id='+encodeURIComponent(id);
  const csvHref = '/api/data/matches/'+encodeURIComponent(id)+'/csv';
  let html = '<div class="actions-bar">' +
    '<b style="font-size:14px;font-weight:700">'+escHtml(m.home)+' vs '+escHtml(m.away)+'</b>' +
    badge(m.status) +
    '<span style="color:var(--muted);font-size:12px">'+escHtml(m.competition||'')+'</span>' +
    '<a href="'+analyzeHref+'" class="btn btn-green" target="_blank">&#x1F50D; Ph\\u00e2n t\\u00edch</a>' +
    '<a href="'+csvHref+'" class="btn" download>&#x2B07; CSV</a>' +
    (selectedB && which==='A' ? '' : '<button class="btn" onclick="selectCompare()">&#x2696; So s\\u00e1nh</button>') +
    '</div>';
  html += renderOddsTable(oddsData||[], m.home, m.away);
  document.getElementById('contentArea').innerHTML = html;
}

window.selectCompare = async function() {
  if (!selectedA) return;
  document.getElementById('contentArea').innerHTML = '<div class="empty">&larr; Click tr\\u1eadn th\\u1ee9 hai trong danh s\\u00e1ch \\u0111\\u1ec3 so s\\u00e1nh</div>';
  document.querySelectorAll('.smatch').forEach(el => {
    if (!selectedA || el.dataset.id !== selectedA.id) {
      el.onclick = function(){ setCompare(el.dataset.id); };
    }
  });
};

async function renderSplit() {
  const [oddsA, oddsB] = await Promise.all([
    apiFetch('/api/matches/'+encodeURIComponent(selectedA.id)+'/odds-history'),
    apiFetch('/api/matches/'+encodeURIComponent(selectedB.id)+'/odds-history'),
  ]);
  const html = '<div class="split">' +
    '<div>' +
      '<div class="actions-bar"><b>'+escHtml(selectedA.home)+' vs '+escHtml(selectedA.away)+'</b>'+badge(selectedA.status)+'<a href="/api/data/matches/'+encodeURIComponent(selectedA.id)+'/csv" class="btn" download>&#x2B07; CSV</a></div>' +
      renderOddsTable(oddsA||[], selectedA.home, selectedA.away) +
    '</div>' +
    '<div>' +
      '<div class="actions-bar"><b>'+escHtml(selectedB.home)+' vs '+escHtml(selectedB.away)+'</b>'+badge(selectedB.status)+'<a href="/api/data/matches/'+encodeURIComponent(selectedB.id)+'/csv" class="btn" download>&#x2B07; CSV</a>' +
      '<button class="btn" onclick="clearCompare()" style="margin-left:auto">&#x2715; B\\u1ecf so s\\u00e1nh</button></div>' +
      renderOddsTable(oddsB||[], selectedB.home, selectedB.away) +
    '</div></div>';
  document.getElementById('contentArea').innerHTML = html;
}

window.clearCompare = function() { selectedB=null; updateSidebarSel(); if(selectedA) renderDetail(selectedA.id,'A'); };

function fmtCell(v){return v==null?'\\u2014':v;}
function diffCls(curr,prev,f){if(curr==null||prev==null)return '';const c=parseFloat(curr),p=parseFloat(prev[f]);if(isNaN(c)||isNaN(p)||c===p)return '';return c>p?' diff-up':' diff-down';}

function renderOddsTable(rows, home, away) {
  if (!rows.length) return '<div class="panel"><div class="panel-h">&#x1F4CA; Bi\\u1ebfn \\u0111\\u1ed9ng k\\u00e8o</div><div style="color:var(--muted);text-align:center;padding:24px;font-size:12px">Ch\\u01b0a c\\u00f3 d\\u1eef li\\u1ec7u</div></div>';
  let h = '<div class="panel"><div class="panel-h">&#x1F4CA; Bi\\u1ebfn \\u0111\\u1ed9ng k\\u00e8o <span style="font-size:11px;color:var(--muted);font-weight:400">('+rows.length+' snapshots)</span></div><div class="panel-b"><table class="ot"><thead><tr>' +
    '<th>Th\\u1eddi gian</th><th>Score</th><th>Ph\\u00fat</th>' +
    '<th>HC '+escHtml((home||'').split(' ')[0])+'</th><th>@</th>' +
    '<th>HC '+escHtml((away||'').split(' ')[0])+'</th><th>@</th>' +
    '<th>OU</th><th>Over</th><th>Under</th>' +
    '<th>1</th><th>X</th><th>2</th>' +
    '</tr></thead><tbody>';
  for(let i=0;i<rows.length;i++){
    const r=rows[i], p=i>0?rows[i-1]:null;
    const score=(r.home_score!=null?r.home_score:'?')+'-'+(r.away_score!=null?r.away_score:'?');
    h+='<tr>'+
      '<td style="color:var(--muted)">'+fmtTime(r.captured_at)+'</td>'+
      '<td style="font-weight:700;color:var(--red)">'+score+'</td>'+
      '<td style="color:var(--orange)">'+(r.minute?r.minute+"'":'\\u2014')+'</td>'+
      '<td>'+escHtml(fmtCell(r.home_handicap))+'</td>'+
      '<td class="'+(p?diffCls(r.home_handicap_odds,p,'home_handicap_odds'):'').trim()+'">'+fmtCell(r.home_handicap_odds)+'</td>'+
      '<td>'+escHtml(fmtCell(r.away_handicap))+'</td>'+
      '<td class="'+(p?diffCls(r.away_handicap_odds,p,'away_handicap_odds'):'').trim()+'">'+fmtCell(r.away_handicap_odds)+'</td>'+
      '<td>'+escHtml(fmtCell(r.ou_line))+'</td>'+
      '<td class="'+(p?diffCls(r.over_odds,p,'over_odds'):'').trim()+'">'+fmtCell(r.over_odds)+'</td>'+
      '<td class="'+(p?diffCls(r.under_odds,p,'under_odds'):'').trim()+'">'+fmtCell(r.under_odds)+'</td>'+
      '<td class="'+(p?diffCls(r.odds_1,p,'odds_1'):'').trim()+'">'+fmtCell(r.odds_1)+'</td>'+
      '<td class="'+(p?diffCls(r.odds_x,p,'odds_x'):'').trim()+'">'+fmtCell(r.odds_x)+'</td>'+
      '<td class="'+(p?diffCls(r.odds_2,p,'odds_2'):'').trim()+'">'+fmtCell(r.odds_2)+'</td>'+
      '</tr>';
  }
  h+='</tbody></table></div></div>';
  return h;
}

(async function(){
  document.getElementById('sqText').value='';
  const data = await apiFetch('/api/data/matches?limit=200');
  if (data && data.length) {
    const sl = document.getElementById('slist');
    sl.innerHTML = data.map(m => {
      return '<div class="smatch" onclick="selectMatch(\\'' + escHtml(m.id) + '\\')" data-id="'+escHtml(m.id)+'">' +
        '<div class="smatch-teams">'+escHtml(m.home)+' <span class="smatch-score">'+m.home_score+'-'+m.away_score+'</span> '+escHtml(m.away)+'</div>' +
        '<div class="smatch-meta"><span>'+badge(m.status)+'</span><span>'+escHtml((m.competition||'').substring(0,25))+'</span><span>'+fmt(m.start_time_utc)+'</span></div>' +
        '</div>';
    }).join('');
    data.forEach(m => matches_cache[m.id] = m);
  }
})();

// ---- Import CSV ----
function openImportModal() {
  document.getElementById('importModalBg').style.display = 'flex';
  document.getElementById('importProgress').textContent = '';
  document.getElementById('importResult').innerHTML = '';
}
function closeImportModal() {
  document.getElementById('importModalBg').style.display = 'none';
}
(function(){
  const drop = document.getElementById('importDrop');
  const inp = document.getElementById('importFile');
  if (!drop || !inp) return;

  // Allow folder selection via webkitdirectory toggle is impossible without
  // re-rendering. Instead, expose folder picking via shift+click.
  drop.addEventListener('click', e => {
    inp.removeAttribute('webkitdirectory');
    inp.removeAttribute('directory');
    inp.click();
  });
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.style.borderColor='var(--primary)'; drop.style.color='var(--primary)'; });
  drop.addEventListener('dragleave', () => { drop.style.borderColor=''; drop.style.color=''; });
  drop.addEventListener('drop', async e => {
    e.preventDefault(); drop.style.borderColor=''; drop.style.color='';
    const items = e.dataTransfer.items;
    let csvFiles = [];
    if (items && items.length && items[0].webkitGetAsEntry) {
      // Folder-aware traversal
      const promises = [];
      for (let i = 0; i < items.length; i++) {
        const entry = items[i].webkitGetAsEntry();
        if (entry) promises.push(walkEntry(entry, csvFiles));
      }
      await Promise.all(promises);
    } else {
      // Fallback: flat FileList
      for (const f of e.dataTransfer.files) if (isCsv(f.name)) csvFiles.push(f);
    }
    if (csvFiles.length) doImport(csvFiles);
    else document.getElementById('importProgress').textContent = 'Không tìm thấy file .csv nào';
  });
  inp.addEventListener('change', () => {
    const csvs = Array.from(inp.files).filter(f => isCsv(f.name));
    if (csvs.length) doImport(csvs);
  });

  // Also expose a "chọn thư mục" link inside the drop zone via a 2nd input
  const folderInp = document.createElement('input');
  folderInp.type = 'file';
  folderInp.webkitdirectory = true;
  folderInp.directory = true;
  folderInp.multiple = true;
  folderInp.style.display = 'none';
  folderInp.addEventListener('change', () => {
    const csvs = Array.from(folderInp.files).filter(f => isCsv(f.name));
    if (csvs.length) doImport(csvs);
    else document.getElementById('importProgress').textContent = 'Thư mục không có file .csv';
  });
  document.body.appendChild(folderInp);
  const folderLink = document.createElement('div');
  folderLink.style.marginTop = '6px';
  folderLink.style.fontSize = '11px';
  folderLink.innerHTML = 'hoặc <span style="color:var(--primary);cursor:pointer;text-decoration:underline">chọn cả thư mục</span>';
  folderLink.querySelector('span').addEventListener('click', e => { e.stopPropagation(); folderInp.click(); });
  drop.appendChild(folderLink);
})();

function isCsv(name) { return /\.csv$/i.test(name || ''); }

function walkEntry(entry, out) {
  return new Promise(resolve => {
    if (entry.isFile) {
      entry.file(f => { if (isCsv(f.name)) out.push(f); resolve(); }, () => resolve());
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const readAll = () => {
        reader.readEntries(async entries => {
          if (!entries.length) { resolve(); return; }
          await Promise.all(entries.map(e => walkEntry(e, out)));
          readAll();
        }, () => resolve());
      };
      readAll();
    } else { resolve(); }
  });
}

async function doImport(files) {
  const prog = document.getElementById('importProgress');
  const res = document.getElementById('importResult');
  res.innerHTML = '';
  // Batch large uploads to avoid one giant request
  const BATCH = 50;
  const total = files.length;
  let agg = { files:0, matches_created:0, matches_updated:0, rows_inserted:0, rows_skipped:0, duplicates:0, excluded:0, errors:[], results:[] };
  for (let i = 0; i < total; i += BATCH) {
    const slice = Array.from(files).slice(i, i + BATCH);
    prog.textContent = 'Đang upload ' + Math.min(i + slice.length, total) + ' / ' + total + ' file...';
    const fd = new FormData();
    for (const f of slice) fd.append('files', f, f.name);
    try {
      const r = await fetch('/api/data/import-csv', { method: 'POST', body: fd });
      if (r.status === 401) { location.href = '/login'; return; }
      const j = await r.json();
      agg.files += j.files || 0;
      agg.matches_created += j.matches_created || 0;
      agg.matches_updated += j.matches_updated || 0;
      agg.rows_inserted += j.rows_inserted || 0;
      agg.rows_skipped += j.rows_skipped || 0;
      agg.duplicates += j.duplicates || 0;
      agg.excluded += j.excluded || 0;
      if (j.errors) agg.errors = agg.errors.concat(j.errors);
      if (j.results) agg.results = agg.results.concat(j.results);
    } catch (e) {
      const msg = e.message || String(e);
      agg.errors.push({ filename:'(batch ' + (i/BATCH+1) + ')', reason: msg });
      // Mark every file in the failed batch as error
      for (const f of slice) agg.results.push({ filename: f.name, status: 'error', detail: msg });
    }
  }
  prog.textContent = '';

  // Group per-file results by status
  const groups = { created:[], updated:[], duplicate:[], excluded:[], error:[] };
  for (const r of agg.results) {
    if (groups[r.status]) groups[r.status].push(r);
  }
  const successCount = groups.created.length + groups.updated.length;

  let h = '<div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px">';
  h += '<div style="font-size:13px;margin-bottom:6px"><b>Kết quả import ' + agg.files + ' file</b></div>';
  h += '<div style="color:var(--muted);font-size:11px;margin-bottom:8px">';
  h += '<b style="color:var(--green)">✓ ' + successCount + '</b> thành công · ';
  h += '<b style="color:#888">↺ ' + groups.duplicate.length + '</b> duplicate (bỏ qua) · ';
  if (groups.excluded.length) h += '<b style="color:var(--orange)">⊘ ' + groups.excluded.length + '</b> loại trừ · ';
  h += '<b style="color:var(--red)">✗ ' + groups.error.length + '</b> lỗi';
  h += '<br>Tạo mới: <b>' + agg.matches_created + '</b> · Cập nhật: <b>' + agg.matches_updated + '</b> · ';
  h += 'Rows thêm: <b style="color:var(--primary)">' + agg.rows_inserted + '</b> · ';
  h += 'Rows bỏ qua: <b>' + agg.rows_skipped + '</b>';
  h += '</div>';

  function renderGroup(title, color, list, limit) {
    if (!list.length) return '';
    limit = limit || 80;
    let s = '<details ' + (list === groups.error ? 'open' : '') + ' style="margin-top:6px">';
    s += '<summary style="cursor:pointer;font-size:11px;color:' + color + '"><b>' + title + ' (' + list.length + ')</b></summary>';
    s += '<ul style="margin:4px 0 0 16px;font-size:11px;max-height:160px;overflow-y:auto;color:var(--text)">';
    for (const e of list.slice(0, limit)) {
      s += '<li><span style="color:var(--muted)">' + escHtml(e.filename) + '</span>';
      if (e.detail) s += ' <span style="color:' + color + '">— ' + escHtml(e.detail) + '</span>';
      s += '</li>';
    }
    if (list.length > limit) s += '<li style="color:var(--muted)">... và ' + (list.length - limit) + ' file khác</li>';
    s += '</ul></details>';
    return s;
  }
  h += renderGroup('✓ Thành công', 'var(--green)', groups.created.concat(groups.updated));
  h += renderGroup('↺ Duplicate (đã tồn tại, bỏ qua)', '#888', groups.duplicate);
  h += renderGroup('⊘ Loại trừ (e-sports/virtual)', 'var(--orange)', groups.excluded);
  h += renderGroup('✗ Lỗi', 'var(--red)', groups.error);

  h += '</div>';
  res.innerHTML = h;
  doSearch();
}
</script>
<script src="/static/lock.js"></script>
</body>
</html>"""


# Inline the shared lock overlay HTML once at import time so route handlers
# don't have to do the substitution on every request.
_DASHBOARD_HTML    = _DASHBOARD_HTML.replace("%%LOCK_OVERLAY%%", _LOCK_OVERLAY)
_MATCH_DETAIL_HTML = _MATCH_DETAIL_HTML.replace("%%LOCK_OVERLAY%%", _LOCK_OVERLAY)
_DATA_HTML         = _DATA_HTML.replace("%%LOCK_OVERLAY%%", _LOCK_OVERLAY)


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

# ---- Static files (analyzer.js etc.) --------------------------------------
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# ---- Analyzer page + API --------------------------------------------------
app.include_router(analyzer_router)


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
async def login_page(auth_token: Optional[str] = Cookie(default=None)):
    if auth_token and decode_token(auth_token):
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(_LOGIN_HTML)


@app.post("/api/auth/request-otp")
async def api_request_otp(request: Request, payload: dict = Body(...)):
    """Generate a one-time OTP and push it to all configured Telegram chats."""
    from .database import store_otp, user_exists
    from . import telegram as tg

    ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(ip):
        return JSONResponse(
            {"ok": False, "error": "Quá nhiều lần thử. Vui lòng đợi 1 phút."},
            status_code=429,
        )

    username = (payload.get("username") or "").strip()
    if not username:
        return JSONResponse({"ok": False, "error": "Thiếu tên đăng nhập"}, status_code=400)
    if not user_exists(username):
        # Don't leak which usernames exist; respond identically with a 200,
        # but skip the Telegram send.
        print(f"[WARN] OTP requested for unknown user '{username}' from {ip}", flush=True)
        return JSONResponse({"ok": True, "ttl_seconds": _OTP_TTL})
    if not tg.is_configured():
        return JSONResponse(
            {"ok": False, "error": "Bot Telegram chưa được cấu hình (.env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS)"},
            status_code=500,
        )

    otp = f"{secrets.randbelow(1_000_000):06d}"
    store_otp(username, "login", otp, _OTP_TTL)
    result = tg.send_login_otp(username, otp, _OTP_TTL)
    if not result.get("ok"):
        return JSONResponse(
            {"ok": False, "error": f"Không gửi được Telegram: {result.get('error') or ''}"},
            status_code=502,
        )
    print(f"[INFO] OTP sent to {result['sent']}/{result['total']} chats for user '{username}' (login)", flush=True)
    return JSONResponse({"ok": True, "ttl_seconds": _OTP_TTL})


@app.post("/api/auth/verify-otp")
async def api_verify_otp(request: Request, payload: dict = Body(...)):
    from .database import verify_and_consume_otp

    ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(ip):
        return JSONResponse(
            {"ok": False, "error": "Quá nhiều lần thử. Vui lòng đợi 1 phút."},
            status_code=429,
        )

    username = (payload.get("username") or "").strip()
    otp = (payload.get("otp") or "").strip()
    if not username or not otp:
        return JSONResponse({"ok": False, "error": "Thiếu username hoặc OTP"}, status_code=400)

    if not verify_and_consume_otp(username, "login", otp):
        print(f"[WARN] Failed OTP verify for '{username}' from {ip}", flush=True)
        return JSONResponse({"ok": False, "error": "OTP không đúng hoặc đã hết hạn"}, status_code=401)

    print(f"[INFO] User '{username}' logged in via OTP from {ip}", flush=True)
    token = create_token(username)
    resp = JSONResponse({"ok": True})
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


# ---------------------------------------------------------------------------
# New pages and API routes
# ---------------------------------------------------------------------------

@app.get("/market", response_class=HTMLResponse)
async def market_page():
    return HTMLResponse(_MARKET_HTML)


@app.get("/data", response_class=HTMLResponse)
async def data_browser_page(user: str = Depends(require_auth)):
    return HTMLResponse(_DATA_HTML)


@app.post("/api/lock/request-otp")
async def api_lock_request_otp(user: str = Depends(require_auth)):
    """Send an unlock-OTP to the configured Telegram chats for the locked user."""
    from .database import store_otp
    from . import telegram as tg

    if not tg.is_configured():
        return JSONResponse(
            {"ok": False, "error": "Bot Telegram chưa được cấu hình"},
            status_code=500,
        )
    otp = f"{secrets.randbelow(1_000_000):06d}"
    store_otp(user, "unlock", otp, _OTP_TTL)
    result = tg.send_unlock_otp(user, otp, _OTP_TTL)
    if not result.get("ok"):
        return JSONResponse(
            {"ok": False, "error": f"Không gửi được Telegram: {result.get('error') or ''}"},
            status_code=502,
        )
    print(f"[INFO] Unlock OTP sent to {result['sent']}/{result['total']} chats for '{user}'", flush=True)
    return {"ok": True, "ttl_seconds": _OTP_TTL}


@app.post("/api/lock/verify-otp")
async def api_lock_verify_otp(payload: dict = Body(...), user: str = Depends(require_auth)):
    from .database import verify_and_consume_otp
    otp = (payload.get("otp") or "").strip()
    if not otp:
        return JSONResponse({"ok": False, "error": "Thiếu OTP"}, status_code=400)
    if verify_and_consume_otp(user, "unlock", otp):
        return {"ok": True}
    return JSONResponse({"ok": False, "error": "OTP không đúng hoặc đã hết hạn"}, status_code=401)


@app.get("/api/lock/config")
async def api_lock_config(user: str = Depends(require_auth)):
    """Frontend reads idle timeout from here so the value lives in .env only."""
    return {"idle_seconds": _IDLE_LOCK_SECONDS, "otp_ttl_seconds": _OTP_TTL}


@app.get("/api/stats/timeline")
async def api_stats_timeline(period: str = "day", user: str = Depends(require_auth)):
    from .database import get_timeline_stats
    return JSONResponse(get_timeline_stats(period))


@app.get("/api/data/matches")
async def api_data_matches(q: str = "", date_from: str = "", date_to: str = "", status: str = "", limit: int = 300, user: str = Depends(require_auth)):
    return JSONResponse(search_matches(q=q, date_from=date_from, date_to=date_to, status=status, limit=limit))


@app.get("/api/data/matches/{match_id:path}/csv")
async def api_data_csv(match_id: str, user: str = Depends(require_auth)):
    from .database import get_odds_history_for_analyzer
    from .analyzer.views import _db_rows_to_csv_rows, _rows_to_csv_text
    import re
    m = get_match_by_id(match_id)
    if not m:
        return JSONResponse({"error": "not found"}, status_code=404)
    db_rows = get_odds_history_for_analyzer(match_id)
    rows = _db_rows_to_csv_rows(db_rows)
    text = _rows_to_csv_text(rows)
    safe = re.sub(r'[^\w\s-]', '', f"{m.get('home','')} vs {m.get('away','')}")
    fname = quote(safe.replace(' ', '_') + ".csv", safe="")
    return Response(
        content=text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"}
    )


@app.post("/api/data/import-csv")
async def api_import_csv(
    files: list[UploadFile] = File(...),
    user: str = Depends(require_auth),
):
    """Bulk-import legacy CSV files (from the old Tkinter tool) into Postgres."""
    from datetime import datetime, timezone, timedelta
    from .analyzer.parser import parse_fname, read_csv_text
    from .database import bulk_import_csv_match

    LOCAL_TZ = timezone(timedelta(hours=7))  # filename's timestamp is GMT+7

    summary = {
        "files": 0,
        "matches_created": 0,
        "matches_updated": 0,
        "rows_inserted": 0,
        "rows_skipped": 0,
        "duplicates": 0,
        "excluded": 0,
        "errors": [],
        "results": [],  # per-file: {filename, status, detail}
    }

    def _record(filename: str, status: str, detail: str = ""):
        summary["results"].append({"filename": filename, "status": status, "detail": detail})

    for f in files:
        summary["files"] += 1
        fname = f.filename or "?"
        try:
            raw = await f.read()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")

            meta = parse_fname(fname)
            if not meta.get("date") or not meta.get("league"):
                reason = "filename không khớp pattern YYYYMMDD_HHMM_<league>-<home>_vs_<away>.csv"
                summary["errors"].append({"filename": fname, "reason": reason})
                _record(fname, "error", reason)
                continue

            dd, mm, yyyy = meta["date"].split("/")
            hh, mi = meta["time"].split(":")
            local_dt = datetime(int(yyyy), int(mm), int(dd), int(hh), int(mi), tzinfo=LOCAL_TZ)
            start_utc = local_dt.astimezone(timezone.utc)

            rows = read_csv_text(text)
            if not rows:
                summary["errors"].append({"filename": fname, "reason": "CSV rỗng"})
                _record(fname, "error", "CSV rỗng")
                continue

            r = bulk_import_csv_match(
                competition=meta["league"],
                home=meta["home"],
                away=meta["away"],
                start_time_utc=start_utc,
                rows=rows,
            )
            if r["excluded"]:
                summary["excluded"] += 1
                _record(fname, "excluded", "competition bị loại (e-sports/virtual)")
                continue

            inserted = r.get("rows_inserted", 0)
            skipped = r.get("rows_skipped", 0)
            summary["rows_inserted"] += inserted
            summary["rows_skipped"] += skipped

            # Duplicate = match đã tồn tại + không có row mới nào được thêm
            if not r["created"] and inserted == 0:
                summary["duplicates"] += 1
                _record(fname, "duplicate", f"đã có {skipped} dòng, bỏ qua")
                continue

            if r["created"]:
                summary["matches_created"] += 1
                _record(fname, "created", f"+{inserted} dòng")
            else:
                summary["matches_updated"] += 1
                _record(fname, "updated", f"+{inserted} dòng (đã có {skipped})")
        except Exception as e:
            reason = str(e)[:200]
            summary["errors"].append({"filename": fname, "reason": reason})
            _record(fname, "error", reason)

    return JSONResponse(summary)
