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

# Load .env BEFORE importing any module that reads env vars. Without this the
# web server has no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_IDS when running outside
# docker-compose (which sets `env_file`). The collector already calls this in
# app/collector.py, so the bug only affected uvicorn-launched dev/prod.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass

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

/* ---- Header ----
   z-index intentionally above the idle-lock overlay (9999) so the "🔄 Đổi"
   button stays clickable even when the screen is locked, letting the user
   one-click to the /market decoy from any state. */
.hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 24px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:10000}
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

/* ---- Priority highlights (Yêu cầu #4) ---- */
.prio-1{border:2px solid #d29922;background:linear-gradient(135deg,rgba(210,153,34,.18),var(--card))}
.prio-2{border:2px solid #f85149;background:linear-gradient(135deg,rgba(248,81,73,.14),var(--card))}
.prio-3{border:2px solid #3fb950;background:linear-gradient(135deg,rgba(63,185,80,.14),var(--card))}
.prio-4{border:1px solid #58a6ff;background:linear-gradient(135deg,rgba(88,166,255,.10),var(--card))}
.prio-tag{position:absolute;top:4px;left:8px;font-size:10px;font-weight:700;letter-spacing:.4px;text-transform:uppercase;opacity:.85}

/* ---- Modal ---- */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9000;display:none;align-items:center;justify-content:center;padding:24px}
.modal-bg.on{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:24px;width:100%;max-width:560px;max-height:90vh;overflow-y:auto}
.modal h2{font-size:17px;margin-bottom:16px;color:var(--primary)}
.modal label{display:block;font-size:12px;font-weight:600;color:var(--muted);margin:10px 0 4px}
.modal input[type=text],.modal input[type=number]{width:100%;padding:8px 10px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-size:13px}
.modal input:focus{outline:none;border-color:var(--primary)}
.modal-row{display:flex;gap:10px}
.modal-row > *{flex:1}
.modal-check{display:flex;align-items:center;gap:8px;padding:5px 0;font-size:13px;cursor:pointer}
.modal-check input{width:auto;margin:0}
.modal-radio{display:flex;gap:14px;margin:8px 0}
.modal-radio label{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--text);margin:0;cursor:pointer}
.modal-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:18px;padding-top:14px;border-top:1px solid var(--border)}
.modal-fieldset{border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;margin-top:8px}
.modal-fieldset legend{font-size:11px;color:var(--muted);padding:0 6px;text-transform:uppercase;letter-spacing:.5px}
.modal-status{font-size:12px;color:var(--muted);min-height:18px}
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
  <a href="/data" class="btn-sm" style="text-decoration:none">📦 Dữ liệu (Layer 3)</a>
  <button class="btn-sm" onclick="openTelegramModal()">📨 Cài đặt Telegram</button>
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

  <!-- Stats (TỔNG = matches imported via CSV) -->
  <div class="stats">
    <div class="stat" title="Trận đã import qua CSV"><div class="stat-n c-all" id="sAll">—</div><div class="stat-l">Tổng (CSV)</div></div>
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

  <!-- Highlighted matches (priority levels 1-4) -->
  <div class="sec">
    <div class="sec-hdr">
      <span class="sec-title">⭐ Trận nổi bật (ưu tiên)</span>
      <span class="sec-badge" id="prioCount">0</span>
      <span style="margin-left:auto;font-size:11px;color:var(--muted)">
        Toàn bộ danh sách → <a href="/data" style="color:var(--primary)">/data (Layer 3)</a>
      </span>
    </div>
    <div class="grid" id="prioGrid"><p class="empty">Không có trận nào đạt ngưỡng ưu tiên</p></div>
  </div>

  <!-- Timeline stats (date-picker) -->
  <div class="sec">
    <div class="sec-hdr">
      <span class="sec-title">📅 Thống kê hệ thống</span>
      <div style="margin-left:auto;display:flex;align-items:center;gap:6px">
        <button class="btn-sm" onclick="shiftStatsDate(-1)">◀</button>
        <input type="date" id="statsDate" class="tsel" style="font-size:12px;padding:4px 8px">
        <button class="btn-sm" onclick="shiftStatsDate(+1)">▶</button>
        <button class="btn-sm" onclick="setStatsToToday()" style="margin-left:6px">Hôm nay</button>
      </div>
    </div>
    <div id="statsTimeline" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px"></div>
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

<!-- Telegram Setting modal — Yêu cầu #7 -->
<div class="modal-bg" id="tgModal" onclick="if(event.target===this)closeTelegramModal()">
  <div class="modal">
    <h2>📨 Cài đặt thông báo Telegram</h2>
    <label>Trận áp dụng</label>
    <div class="modal-radio">
      <label><input type="radio" name="tgScope" value="prestigious"> Chỉ giải uy tín</label>
      <label><input type="radio" name="tgScope" value="all"> Tất cả giải</label>
    </div>

    <fieldset class="modal-fieldset">
      <legend>Điều kiện kích hoạt</legend>
      <div class="modal-row">
        <div><label>Bàn thắng thứ</label><input type="number" id="tgGoal" min="1" max="20"></div>
        <div><label>Trước phút</label><input type="number" id="tgMin" min="1" max="120"></div>
      </div>
    </fieldset>

    <fieldset class="modal-fieldset">
      <legend>Nội dung báo cáo</legend>
      <label class="modal-check"><input type="checkbox" id="tgIncName"> Tên trận</label>
      <label class="modal-check"><input type="checkbox" id="tgIncComp"> Tên giải</label>
      <label class="modal-check"><input type="checkbox" id="tgIncG1"> Bàn 1 — (HC trước→sau · OU trước→sau · phút)</label>
      <label class="modal-check"><input type="checkbox" id="tgIncG2"> Bàn 2</label>
      <label class="modal-check"><input type="checkbox" id="tgIncG3"> Bàn 3</label>
      <label class="modal-check"><input type="checkbox" id="tgIncG4"> Bàn 4</label>
    </fieldset>

    <label>Bot token (để trống = dùng <code>TELEGRAM_BOT_TOKEN</code> từ .env)</label>
    <input type="text" id="tgBotToken" placeholder="123456:ABC-DEF...">

    <label>Chat ID (phân cách dấu phẩy, để trống = dùng <code>TELEGRAM_CHAT_IDS</code> từ .env)</label>
    <input type="text" id="tgChats" placeholder="123456789,987654321">

    <div id="tgDiag" style="margin-top:10px;font-size:11px;color:var(--muted)"></div>
    <div class="modal-status" id="tgStatus"></div>

    <div class="modal-actions">
      <button class="btn-sm" onclick="testTelegram()">Gửi thử</button>
      <button class="btn-sm" onclick="closeTelegramModal()">Đóng</button>
      <button class="btn-sm" style="background:var(--pdim);color:var(--primary);border-color:var(--primary)" onclick="saveTelegramSettings()">💾 Đổi & lưu</button>
    </div>
  </div>
</div>

<script>
// ------------------------------------------------------------------ state
let allMatches = [];
let localLogs = [];
let currentUser = '';
let statsDate = ''; // YYYY-MM-DD; '' = let the picker pick today on init

// ------------------------------------------------------------------ pin system
const PINNED_KEY = 'fbc_pinned';
function getPinned() { try { return new Set(JSON.parse(localStorage.getItem(PINNED_KEY)||'[]')); } catch(_){ return new Set(); } }
function savePinned(s) { localStorage.setItem(PINNED_KEY, JSON.stringify([...s])); }
function togglePin(id, ev) {
  ev && ev.stopPropagation();
  const p = getPinned();
  if (p.has(id)) p.delete(id); else p.add(id);
  savePinned(p);
  renderLive(); renderPriority();
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

  // Stats — TỔNG = CSV-imported matches (Yêu cầu #1)
  const s = data.stats;
  document.getElementById('sAll').textContent  = (s.csv_total != null ? s.csv_total : s.total);
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
  renderPriority();
}

// ------------------------------------------------------------------ render live cards
function renderMatchCard(m, prefix){
  const pinned = getPinned();
  const cls = m.status === 'HT' ? 'ht' : (isLive(m.status) ? 'live' : '');
  const min = m.status === 'HT' ? 'HT' : (m.minute ? m.minute + "'" : (m.status==='FT' ? 'FT' : "0'"));
  const ou  = m.ou_line   ? '<div class="chip">OU <b>' + m.ou_line + '</b></div>' : '';
  const hc  = m.home_handicap ? '<div class="chip">HC <b>' + m.home_handicap + '</b></div>' : '';
  const x2  = m.odds_1 ? '<div class="chip">1X2 <b>' + m.odds_1 + '</b> ' + (m.odds_x||'?') + ' <b>' + m.odds_2 + '</b></div>' : '';
  const href = '/match/' + encodeURIComponent(m.id);
  const aHref = '/analyzer?match_id=' + encodeURIComponent(m.id);
  const isPinned = pinned.has(m.id);
  const pinBtn = '<button onclick="togglePin(' + JSON.stringify(m.id) + ',event)" style="position:absolute;top:8px;right:8px;background:none;border:none;cursor:pointer;font-size:14px;opacity:' + (isPinned?'1':'.35') + ';padding:2px;z-index:2">' + (isPinned?'⭐':'☆') + '</button>';
  const prio = m.priority_level || 5;
  const prioCls = (prio <= 4) ? ('prio-' + prio) : '';
  const prioTag = (prio <= 4) ? ('<span class="prio-tag" style="color:'+(prio===1?'#d29922':prio===2?'#f85149':prio===3?'#3fb950':'#58a6ff')+'">★ P'+prio+'</span>') : '';
  return '<div class="mcard ' + cls + ' ' + prioCls + '" style="position:relative" data-prio="'+prio+'">' +
    prioTag + pinBtn +
    '<a href="' + href + '" style="text-decoration:none;color:inherit;display:block;padding-top:'+(prio<=4?'12px':'0')+'">' +
    '<div class="mcomp">' + m.competition + '</div>' +
    '<div class="mteams"><div class="mteam">' + m.home + '</div>' +
    '<div class="mscore">' + (m.home_score||0) + ' - ' + (m.away_score||0) + '</div>' +
    '<div class="mteam away">' + m.away + '</div></div>' +
    '<div class="minfo"><span>' + fmt(m.start_time_utc) + '</span><span class="mmin">' + min + '</span></div>' +
    '<div class="modds">' + ou + hc + x2 + '</div>' +
    '</a>' +
    '<a href="' + aHref + '" style="display:block;text-align:center;padding:5px;font-size:11px;font-weight:700;color:var(--primary);border-top:1px solid var(--border);text-decoration:none;background:rgba(88,166,255,.05)">🔍 Phân tích</a>' +
    '</div>';
}

function renderLive() {
  let live = allMatches.filter(m => isLive(m.status));
  // Sort by priority (1..5), then start time desc
  live.sort((a,b) => (a.priority_level||5) - (b.priority_level||5) || new Date(b.start_time_utc) - new Date(a.start_time_utc));
  document.getElementById('liveCount').textContent = live.length;
  const el = document.getElementById('liveGrid');
  if (!live.length) { el.innerHTML = '<p class="empty">Không có trận nào đang diễn ra</p>'; return; }
  el.innerHTML = live.map(m => renderMatchCard(m, 'live')).join('');
}

function renderPriority() {
  // All non-live priority 1-4 matches (live ones already shown above). Capped at 30.
  let prio = allMatches.filter(m => (m.priority_level||5) <= 4 && !isLive(m.status));
  prio.sort((a,b) => (a.priority_level||5) - (b.priority_level||5) || new Date(b.start_time_utc) - new Date(a.start_time_utc));
  prio = prio.slice(0, 30);
  document.getElementById('prioCount').textContent = prio.length;
  const el = document.getElementById('prioGrid');
  if (!prio.length) { el.innerHTML = '<p class="empty">Không có trận nào đạt ngưỡng ưu tiên — xem đầy đủ ở <a href="/data" style="color:var(--primary)">/data</a></p>'; return; }
  el.innerHTML = prio.map(m => renderMatchCard(m, 'prio')).join('');
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

// ------------------------------------------------------------------ timeline stats (date-picker)
function isoToday(){ const d=new Date(); return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0'); }
function setStatsToToday(){ statsDate = isoToday(); document.getElementById('statsDate').value = statsDate; pollTimeline(); }
function shiftStatsDate(days){
  const cur = statsDate || isoToday();
  const d = new Date(cur+'T00:00:00');
  d.setDate(d.getDate()+days);
  statsDate = d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
  document.getElementById('statsDate').value = statsDate;
  pollTimeline();
}
async function pollTimeline() {
  if (!statsDate) statsDate = isoToday();
  const url = '/api/stats/timeline?date=' + encodeURIComponent(statsDate);
  const data = await apiFetch(url);
  if (!data) return;
  const el = document.getElementById('statsTimeline');
  if (!el) return;
  el.innerHTML = [
    {n: data.matches, l:'Trận đấu', c:'c-all'},
    {n: data.goals, l:'Tổng bàn', c:'c-live'},
    {n: data.avg_goals, l:'TB bàn/trận', c:'c-live'},
    {n: (data.pct_3plus!=null?data.pct_3plus+'%':'—'), l:'≥3 bàn', c:'c-up'},
    {n: data.live, l:'Đang live', c:'c-live'},
    {n: data.finished, l:'Kết thúc', c:'c-ft'},
    {n: data.competitions, l:'Giải đấu', c:'c-ht'},
    {n: data.prestigious_count, l:'Giải uy tín', c:'c-up'},
    {n: data.big_odds_swing_count, l:'Kèo biến động lớn', c:'c-ht'},
  ].map(s=>'<div class="stat"><div class="stat-n '+s.c+'">'+(s.n!=null?s.n:'—')+'</div><div class="stat-l">'+s.l+'</div></div>').join('');
}

// ------------------------------------------------------------------ Telegram modal
async function openTelegramModal(){
  const m = await apiFetch('/api/telegram/settings'); if (!m) return;
  document.querySelector('input[name=tgScope][value="'+m.scope+'"]').checked = true;
  document.getElementById('tgGoal').value = m.goal_threshold;
  document.getElementById('tgMin').value  = m.before_minute;
  document.getElementById('tgIncName').checked = !!m.include_match_name;
  document.getElementById('tgIncComp').checked = !!m.include_competition;
  document.getElementById('tgIncG1').checked = !!m.include_goal_1;
  document.getElementById('tgIncG2').checked = !!m.include_goal_2;
  document.getElementById('tgIncG3').checked = !!m.include_goal_3;
  document.getElementById('tgIncG4').checked = !!m.include_goal_4;
  document.getElementById('tgChats').value = m.chat_ids || '';
  document.getElementById('tgBotToken').value = m.bot_token || '';
  document.getElementById('tgStatus').textContent = '';
  document.getElementById('tgModal').classList.add('on');
  await refreshTgDiag();
}
async function refreshTgDiag(){
  const d = await apiFetch('/api/telegram/diagnose');
  const el = document.getElementById('tgDiag');
  if (!d) { el.textContent = ''; return; }
  el.innerHTML =
    'Bot token: <b style="color:'+(d.has_bot_token?'var(--green)':'var(--red)')+'">'+(d.has_bot_token?('OK · '+d.token_source):'thiếu')+'</b>' +
    ' · Chat ID: <b style="color:'+(d.has_chat_ids?'var(--green)':'var(--red)')+'">'+(d.has_chat_ids?(d.chat_count+' chat · '+d.chat_source):'thiếu')+'</b>';
}
function closeTelegramModal(){ document.getElementById('tgModal').classList.remove('on'); }
async function saveTelegramSettings(){
  const payload = {
    scope: (document.querySelector('input[name=tgScope]:checked')||{}).value || 'prestigious',
    goal_threshold: parseInt(document.getElementById('tgGoal').value || '3', 10),
    before_minute:  parseInt(document.getElementById('tgMin').value  || '75', 10),
    include_match_name:  document.getElementById('tgIncName').checked,
    include_competition: document.getElementById('tgIncComp').checked,
    include_goal_1: document.getElementById('tgIncG1').checked,
    include_goal_2: document.getElementById('tgIncG2').checked,
    include_goal_3: document.getElementById('tgIncG3').checked,
    include_goal_4: document.getElementById('tgIncG4').checked,
    chat_ids: document.getElementById('tgChats').value.trim(),
    bot_token: document.getElementById('tgBotToken').value.trim(),
  };
  const r = await fetch('/api/telegram/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  if (!r || !r.ok) { document.getElementById('tgStatus').textContent = 'Lỗi lưu cấu hình'; return; }
  document.getElementById('tgStatus').textContent = 'Đã lưu. Phiên sẽ bị khoá để xác thực lại.';
  // Yêu cầu #12 — force re-auth via the existing idle-lock OTP flow.
  setTimeout(() => {
    closeTelegramModal();
    if (typeof window.showLock === 'function') window.showLock();
  }, 500);
}
async function testTelegram(){
  const el = document.getElementById('tgStatus');
  el.textContent = 'Đang gửi tin thử...';
  const r = await fetch('/api/telegram/test', {method:'POST'});
  if (r.status === 401) { location.href='/login'; return; }
  let j = null; try { j = await r.json(); } catch(_) {}
  await refreshTgDiag();
  if (!j) { el.textContent = 'Không gửi được tin thử (HTTP '+r.status+').'; return; }
  if (j.ok) {
    el.textContent = 'Đã gửi ' + (j.sent||0) + '/' + (j.total||0) + ' chat. Mở Telegram kiểm tra.';
  } else {
    el.textContent = 'Lỗi: ' + (j.error || 'unknown');
  }
}

// ------------------------------------------------------------------ init
setStatsToToday();
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

.hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 24px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:10000}
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
  <button class="btn-sm" onclick="goDisguise()">🔄 Đổi</button>
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
// In-half time: 1H:24, HT, 2H:10, FT — Yêu cầu #2
function fmtInHalf(r){
  if(!r) return '—';
  const s = (r.status||'').toUpperCase();
  const m = r.minute;
  if (s === 'FT') return 'FT';
  if (s === 'HT') return 'HT';
  if (s === 'INJURY_TIME_H1') return '1H:45+';
  if (s === 'INJURY_TIME_H2') return '2H:90+';
  if (s === 'H1') return '1H:' + (m != null ? m : '');
  if (s === 'H2') return '2H:' + (m != null ? m : '');
  if (s === 'LIVE' && m != null) return (m <= 45 ? '1H:' : '2H:') + m;
  if (s === 'UPCOMING' || m == null) return '—';
  return s;
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
    '<th>Thời gian (hiệp)</th>'+
    '<th>HC nhà</th><th>HC khách</th>'+
    '<th>OU</th><th>Over</th><th>Under</th>'+
    '<th>1</th><th>X</th><th>2</th>'+
    '</tr></thead><tbody>';
  for(let i=0;i<rows.length;i++){
    const r=rows[i];
    const p=i>0?rows[i-1]:null;
    html += '<tr>'+
      '<td style="color:var(--muted)">'+fmtInHalf(r)+'</td>'+
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

/* ---- Return-gate popup (subtle red/yellow) ----
   Forces a Telegram OTP before the user can leave /market.
   Background uses a dimmer + slight warm tint instead of pure black so
   the decoy market UI is still visible behind. */
.gate-bg{position:fixed;inset:0;background:radial-gradient(ellipse at center,rgba(255,180,80,.08),rgba(0,0,0,.78));backdrop-filter:blur(2px);z-index:9000;display:flex;align-items:center;justify-content:center;padding:24px}
.gate-bg.hidden{display:none}
.gate{position:relative;background:linear-gradient(180deg,#1a1410,#0e0a07);border:1px solid transparent;border-radius:12px;padding:0;width:100%;max-width:380px;box-shadow:0 22px 48px rgba(0,0,0,.55),0 0 0 1px rgba(255,200,90,.18)}
.gate::before{content:'';position:absolute;inset:-1px;border-radius:13px;padding:1px;background:linear-gradient(135deg,#ff7676 0%,#ffd166 55%,#ff7676 100%);-webkit-mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);-webkit-mask-composite:xor;mask-composite:exclude;opacity:.6;pointer-events:none}
.gate-hdr{display:flex;align-items:center;gap:10px;padding:14px 18px;background:linear-gradient(90deg,rgba(255,118,118,.10),rgba(255,209,102,.10));border-bottom:1px solid rgba(255,209,102,.18);border-radius:12px 12px 0 0}
.gate-hdr .icon{font-size:18px}
.gate-hdr .title{font-size:13px;font-weight:700;letter-spacing:.3px;color:#ffd9a3}
.gate-hdr .close{margin-left:auto;background:none;border:0;color:#8b6a4a;cursor:pointer;font-size:18px;line-height:1;padding:2px 6px;border-radius:4px}
.gate-hdr .close:hover{background:rgba(255,209,102,.08);color:#ffd166}
.gate-body{padding:18px 20px 20px}
.gate-body p{color:#c9bca8;font-size:12.5px;line-height:1.5;margin-bottom:14px}
.gate-body p b{color:#ffd9a3}
.gate-row{display:flex;flex-direction:column;gap:8px;margin-bottom:10px}
.gate-otp{padding:11px 14px;background:#15110c;border:1px solid rgba(255,209,102,.22);border-radius:6px;color:#ffe1ba;font-size:20px;letter-spacing:7px;text-align:center;font-family:'SF Mono','Consolas',monospace}
.gate-otp:focus{outline:none;border-color:#ffb454;background:#1a140d}
.gate-otp:disabled{opacity:.45}
.gate-btn{padding:10px 14px;border-radius:6px;border:1px solid rgba(255,209,102,.32);background:rgba(255,209,102,.10);color:#ffd9a3;font-size:13px;font-weight:600;cursor:pointer;transition:all .15s}
.gate-btn:hover:not(:disabled){background:rgba(255,209,102,.18);border-color:#ffd166;color:#fff2d1}
.gate-btn:disabled{opacity:.45;cursor:not-allowed}
.gate-btn.primary{background:linear-gradient(90deg,#ff7676,#ffb454);color:#1a0f06;border-color:transparent;font-weight:700}
.gate-btn.primary:hover:not(:disabled){background:linear-gradient(90deg,#ff8585,#ffc275);box-shadow:0 4px 14px rgba(255,170,90,.28)}
.gate-msg{font-size:11.5px;color:#a59078;min-height:16px;margin-top:8px}
.gate-err{color:#ff8a8a}
.gate-ok{color:#a8d39b}
.gate-reopen{position:fixed;bottom:18px;right:18px;background:linear-gradient(135deg,#ff7676,#ffd166);color:#1a0f06;border:0;border-radius:50%;width:46px;height:46px;font-size:20px;cursor:pointer;box-shadow:0 6px 18px rgba(0,0,0,.4),0 0 0 1px rgba(255,209,102,.4);z-index:8500;display:none}
.gate-reopen.on{display:flex;align-items:center;justify-content:center}
</style>
</head>
<body>

<!-- Return-gate: hidden by default. Server-side middleware (set on /market
     visit, cleared on OTP verify) already prevents navigation away, so the
     popup only opens when the user explicitly clicks the floating button. -->
<div class="gate-bg hidden" id="gateBg">
  <div class="gate">
    <div class="gate-hdr">
      <span class="icon">&#x1F510;</span>
      <span class="title">X&#xE1;c th&#x1EF1;c &#x111;&#x1EC3; quay l&#x1EA1;i</span>
      <button class="close" type="button" onclick="hideGate()" title="&#x110;&#xF3;ng">&times;</button>
    </div>
    <div class="gate-body">
      <p>Phi&#xEA;n &#x111;ang &#x1EDF; <b>ch&#x1EBF; &#x111;&#x1ED9; decoy</b>. Nh&#x1EADp m&#xE3; OTP nh&#x1EADn qua Telegram &#x111;&#x1EC3; quay l&#x1EA1;i b&#x1EA3;ng &#x111;i&#x1EC1;u khi&#x1EC3;n.</p>
      <div class="gate-row">
        <button id="gateBtnReq" class="gate-btn" type="button" onclick="gateReqOtp()">G&#x1EEDi OTP qua Telegram</button>
        <input id="gateOtp" class="gate-otp" type="text" inputmode="numeric" maxlength="6" placeholder="------" disabled onkeydown="if(event.key==='Enter')gateVerify()" autocomplete="one-time-code">
        <button id="gateBtnVerify" class="gate-btn primary" type="button" onclick="gateVerify()" disabled>M&#x1EDF; kh&#xF3;a &amp; quay l&#x1EA1;i</button>
      </div>
      <div class="gate-msg" id="gateMsg">B&#x1EA5;m "G&#x1EEDi OTP" &#x111;&#x1EC3; nh&#x1EADn m&#xE3; 6 ch&#x1EEF; s&#x1ED1; qua Telegram.</div>
    </div>
  </div>
</div>

<button class="gate-reopen on" id="gateReopen" type="button" onclick="showGate()" title="M&#x1EDF; x&#xE1;c th&#x1EF1;c">&#x1F510;</button>

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

// ───────────────────────────────────────────────────────────────────────
// Return-gate. The page is hidden by default — server-side middleware (the
// market_gate cookie set on /market visit) already enforces that any other
// route the user tries (Back, Forward, address bar, link click) bounces
// straight back to /market. The popup only opens when the user explicitly
// clicks the 🔐 floating button.
// Reuses /api/lock/request-otp + /api/lock/verify-otp so we don't duplicate
// the bot plumbing — verify-otp clears the cookie server-side on success.
// ───────────────────────────────────────────────────────────────────────
(function(){
  const gateBg = document.getElementById('gateBg');
  const reopen = document.getElementById('gateReopen');
  const msg    = document.getElementById('gateMsg');
  const otp    = document.getElementById('gateOtp');
  const btnReq = document.getElementById('gateBtnReq');
  const btnVer = document.getElementById('gateBtnVerify');
  let otpRequested = false;

  function setMsg(text, cls){
    msg.textContent = text || '';
    msg.className = 'gate-msg' + (cls ? ' ' + cls : '');
  }

  window.showGate = function(){
    gateBg.classList.remove('hidden');
    reopen.classList.remove('on');
    setMsg('Bấm "Gửi OTP" để nhận mã 6 chữ số qua Telegram.', '');
  };
  window.hideGate = function(){
    gateBg.classList.add('hidden');
    reopen.classList.add('on');
  };

  window.gateReqOtp = async function(){
    btnReq.disabled = true; btnReq.textContent = 'Đang gửi...';
    setMsg('Đang gửi OTP qua Telegram...', '');
    try {
      const r = await fetch('/api/lock/request-otp', {method:'POST'});
      if (r.status === 401) { location.href = '/login'; return; }
      const j = await r.json().catch(()=>({}));
      if (!r.ok || !j.ok) {
        setMsg(j.error || 'Không gửi được OTP. Kiểm tra Cài đặt Telegram.', 'gate-err');
        btnReq.disabled = false; btnReq.textContent = 'Gửi OTP qua Telegram';
        return;
      }
      otpRequested = true;
      otp.disabled = false; otp.focus();
      btnVer.disabled = false;
      btnReq.disabled = false; btnReq.textContent = 'Gửi lại OTP';
      const ttl = Math.round((j.ttl_seconds||300)/60);
      setMsg('OTP đã gửi. Hết hạn sau ' + ttl + ' phút.', 'gate-ok');
    } catch (e) {
      setMsg('Lỗi mạng: ' + e.message, 'gate-err');
      btnReq.disabled = false; btnReq.textContent = 'Gửi OTP qua Telegram';
    }
  };

  window.gateVerify = async function(){
    if (!otpRequested) { setMsg('Hãy bấm "Gửi OTP" trước.', 'gate-err'); return; }
    const code = (otp.value||'').trim();
    if (!code) { setMsg('Nhập OTP', 'gate-err'); otp.focus(); return; }
    btnVer.disabled = true;
    setMsg('Đang xác thực...', '');
    try {
      const r = await fetch('/api/lock/verify-otp', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({otp: code})
      });
      if (r.status === 401) {
        const j = await r.json().catch(()=>({}));
        setMsg(j.error || 'OTP không đúng', 'gate-err');
        otp.value = ''; otp.focus();
        btnVer.disabled = false;
        return;
      }
      const j = await r.json();
      if (j.ok) {
        setMsg('Mở khoá thành công, đang chuyển hướng...', 'gate-ok');
        // Drop the sessionStorage idle-lock so the dashboard doesn't immediately
        // re-lock on arrival. The market_gate cookie was already cleared by the
        // verify-otp response.
        try { sessionStorage.removeItem('fbc_locked'); } catch(_){}
        location.href = '/';
      } else {
        setMsg(j.error || 'OTP không đúng', 'gate-err');
        otp.value = ''; otp.focus();
        btnVer.disabled = false;
      }
    } catch (e) {
      setMsg('Lỗi mạng: ' + e.message, 'gate-err');
      btnVer.disabled = false;
    }
  };
})();
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
.hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 24px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:10000}
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

/* ---- Advanced search panel (Yêu cầu #8) ---- */
.adv-panel{padding:10px 12px;border-bottom:1px solid var(--border);background:rgba(88,166,255,.03)}
.adv-toggle{display:flex;align-items:center;cursor:pointer;font-size:12px;font-weight:600;color:var(--primary);user-select:none}
.adv-toggle::before{content:'▶';margin-right:6px;transition:transform .15s;display:inline-block}
.adv-panel[data-open] .adv-toggle::before{transform:rotate(90deg)}
.adv-body{display:none;margin-top:10px}
.adv-panel[data-open] .adv-body{display:block}
.adv-row{display:flex;gap:6px;align-items:center;margin-bottom:6px}
.adv-row label{font-size:11px;color:var(--muted);flex:0 0 70px}
.adv-row input{flex:1;padding:5px 8px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-size:12px}
.adv-row input:focus{outline:none;border-color:var(--primary)}
.adv-sub{margin:6px 0 4px;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.adv-actions{display:flex;gap:6px;margin-top:8px}
.adv-actions button{flex:1}

/* ---- Sortable result table ---- */
.rtable{width:100%;border-collapse:collapse;font-size:11px;background:var(--card)}
.rtable th{padding:8px 10px;background:var(--surface);text-align:left;font-size:10px;text-transform:uppercase;color:var(--muted);cursor:pointer;user-select:none;border-bottom:1px solid var(--border)}
.rtable th:hover{color:var(--primary)}
.rtable th .sortar{font-size:9px;margin-left:4px;opacity:.5}
.rtable th.sort-asc .sortar,.rtable th.sort-desc .sortar{opacity:1;color:var(--primary)}
.rtable td{padding:7px 10px;border-bottom:1px solid #1a1f26;white-space:nowrap}
.rtable tr.expanded{background:rgba(88,166,255,.04)}
.rtable tr.row-prio-1{background:linear-gradient(90deg,rgba(210,153,34,.12),transparent)}
.rtable tr.row-prio-2{background:linear-gradient(90deg,rgba(248,81,73,.10),transparent)}
.rtable tr.row-prio-3{background:linear-gradient(90deg,rgba(63,185,80,.10),transparent)}
.rtable tr.row-prio-4{background:linear-gradient(90deg,rgba(88,166,255,.08),transparent)}
.rtable tr.row-clickable{cursor:pointer}
.rtable tr.row-clickable:hover td{background:rgba(255,255,255,.03)}
.results-bar{display:flex;align-items:center;gap:10px;margin-bottom:10px;padding:8px 12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);font-size:12px;color:var(--muted)}
.results-bar b{color:var(--text)}
.results-bar .spacer{flex:1}

/* ---- Scroll-sync toggle (Yêu cầu #10) ---- */
.scroll-toggle{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;background:var(--card);border:1px solid var(--border);border-radius:14px;font-size:11px;cursor:pointer;user-select:none}
.scroll-toggle .dot{width:16px;height:16px;border-radius:50%;background:var(--border);transition:all .15s;position:relative}
.scroll-toggle.on .dot{background:var(--primary)}
.split-pane{max-height:calc(100vh - 200px);overflow-y:auto}
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

    <!-- Advanced filters — Yêu cầu #8 -->
    <div class="adv-panel" id="advPanel">
      <div class="adv-toggle" onclick="toggleAdv()">T&#xEC;m n&#xE2;ng cao (Opening / OU / HC theo b&#xE0;n)</div>
      <div class="adv-body">
        <div class="adv-sub">Opening</div>
        <div class="adv-row"><label>HC opening</label><input id="advOpenHc" placeholder="vd: 0.75"></div>
        <div class="adv-row"><label>OU opening</label><input id="advOpenOu" placeholder="vd: 2.5"></div>

        <div class="adv-sub">OU tr&#x01B0;&#x1EDB;c b&#xE0;n th&#x1EAF;ng</div>
        <div class="adv-row"><label>B&#xE0;n 1</label><input id="advOuB1" placeholder=""><label style="flex:0 0 50px">B&#xE0;n 2</label><input id="advOuB2"></div>
        <div class="adv-row"><label>B&#xE0;n 3</label><input id="advOuB3"><label style="flex:0 0 50px">B&#xE0;n 4</label><input id="advOuB4"></div>
        <div class="adv-row"><label>B&#xE0;n 5</label><input id="advOuB5"><label style="flex:0 0 50px">B&#xE0;n 6</label><input id="advOuB6"></div>
        <div class="adv-row"><label>B&#xE0;n 7</label><input id="advOuB7"></div>

        <div class="adv-sub">HC sau b&#xE0;n th&#x1EAF;ng</div>
        <div class="adv-row"><label>B&#xE0;n 1</label><input id="advHcA1" placeholder=""><label style="flex:0 0 50px">B&#xE0;n 2</label><input id="advHcA2"></div>
        <div class="adv-row"><label>B&#xE0;n 3</label><input id="advHcA3"><label style="flex:0 0 50px">B&#xE0;n 4</label><input id="advHcA4"></div>
        <div class="adv-row"><label>B&#xE0;n 5</label><input id="advHcA5"><label style="flex:0 0 50px">B&#xE0;n 6</label><input id="advHcA6"></div>
        <div class="adv-row"><label>B&#xE0;n 7</label><input id="advHcA7"></div>

        <div class="adv-actions">
          <button class="btn-sm" onclick="resetAdv()">Reset</button>
          <button class="btn-sm" style="background:var(--pdim);color:var(--primary);border-color:var(--primary)" onclick="doAdvancedSearch()">&#x1F3AF; &#xC1;p d&#x1EE5;ng</button>
        </div>
      </div>
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
function fmtInHalf(r){if(!r)return '—';const s=(r.status||'').toUpperCase(),m=r.minute;if(s==='FT')return 'FT';if(s==='HT')return 'HT';if(s==='INJURY_TIME_H1')return '1H:45+';if(s==='INJURY_TIME_H2')return '2H:90+';if(s==='H1')return '1H:'+(m!=null?m:'');if(s==='H2')return '2H:'+(m!=null?m:'');if(s==='LIVE'&&m!=null)return (m<=45?'1H:':'2H:')+m;if(s==='UPCOMING'||m==null)return '—';return s;}
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
  // Yêu cầu #10 — scroll-sync toggle. Default = independent. Persist in session.
  const syncMode = sessionStorage.getItem('layer3_scroll') === 'sync';
  const toggleHtml =
    '<div class="scroll-toggle '+(syncMode?'on':'')+'" id="scrollToggle" onclick="toggleScrollSync()" title="Chuy\\u1ec3n c\\u01b0u\\u1ed9n">' +
      '<span>'+(syncMode?'\\u21F5 Cu\\u1ed9n chung':'\\u21F4 Cu\\u1ed9n ri\\u00eang')+'</span><span class="dot"></span>' +
    '</div>';
  const html = '<div class="actions-bar">' + toggleHtml +
    '<button class="btn" onclick="clearCompare()" style="margin-left:auto">&#x2715; B\\u1ecf so s\\u00e1nh</button></div>' +
    '<div class="split">' +
    '<div class="split-pane" id="paneA">' +
      '<div class="actions-bar"><b>'+escHtml(selectedA.home)+' vs '+escHtml(selectedA.away)+'</b>'+badge(selectedA.status)+'<a href="/api/data/matches/'+encodeURIComponent(selectedA.id)+'/csv" class="btn" download>&#x2B07; CSV</a></div>' +
      renderOddsTable(oddsA||[], selectedA.home, selectedA.away) +
    '</div>' +
    '<div class="split-pane" id="paneB">' +
      '<div class="actions-bar"><b>'+escHtml(selectedB.home)+' vs '+escHtml(selectedB.away)+'</b>'+badge(selectedB.status)+'<a href="/api/data/matches/'+encodeURIComponent(selectedB.id)+'/csv" class="btn" download>&#x2B07; CSV</a></div>' +
      renderOddsTable(oddsB||[], selectedB.home, selectedB.away) +
    '</div></div>';
  document.getElementById('contentArea').innerHTML = html;
  if (syncMode) attachScrollSync();
}

let _scrollSyncFlag = false;
function attachScrollSync() {
  const a = document.getElementById('paneA'); const b = document.getElementById('paneB');
  if (!a || !b) return;
  const mk = (src, dst) => () => {
    if (_scrollSyncFlag) return;
    _scrollSyncFlag = true;
    dst.scrollTop = src.scrollTop;
    // Reset on next tick so the matching scroll-event-from-dst doesn't bounce back.
    setTimeout(() => _scrollSyncFlag = false, 0);
  };
  a._syncHandler = mk(a, b); b._syncHandler = mk(b, a);
  a.addEventListener('scroll', a._syncHandler); b.addEventListener('scroll', b._syncHandler);
}
function detachScrollSync() {
  for (const id of ['paneA','paneB']) {
    const el = document.getElementById(id);
    if (el && el._syncHandler) { el.removeEventListener('scroll', el._syncHandler); el._syncHandler = null; }
  }
}
function toggleScrollSync(){
  const cur = sessionStorage.getItem('layer3_scroll') === 'sync';
  sessionStorage.setItem('layer3_scroll', cur ? 'free' : 'sync');
  // Re-render the split view to reattach handlers cleanly.
  detachScrollSync();
  if (selectedA && selectedB) renderSplit();
}

window.clearCompare = function() { selectedB=null; updateSidebarSel(); if(selectedA) renderDetail(selectedA.id,'A'); };

function fmtCell(v){return v==null?'\\u2014':v;}
function diffCls(curr,prev,f){if(curr==null||prev==null)return '';const c=parseFloat(curr),p=parseFloat(prev[f]);if(isNaN(c)||isNaN(p)||c===p)return '';return c>p?' diff-up':' diff-down';}

function renderOddsTable(rows, home, away) {
  if (!rows.length) return '<div class="panel"><div class="panel-h">&#x1F4CA; Bi\\u1ebfn \\u0111\\u1ed9ng k\\u00e8o</div><div style="color:var(--muted);text-align:center;padding:24px;font-size:12px">Ch\\u01b0a c\\u00f3 d\\u1eef li\\u1ec7u</div></div>';
  let h = '<div class="panel"><div class="panel-h">&#x1F4CA; Bi\\u1ebfn \\u0111\\u1ed9ng k\\u00e8o <span style="font-size:11px;color:var(--muted);font-weight:400">('+rows.length+' snapshots)</span></div><div class="panel-b"><table class="ot"><thead><tr>' +
    '<th>Hi\\u1ec7p</th><th>Score</th><th>Ph\\u00fat</th>' +
    '<th>HC '+escHtml((home||'').split(' ')[0])+'</th><th>@</th>' +
    '<th>HC '+escHtml((away||'').split(' ')[0])+'</th><th>@</th>' +
    '<th>OU</th><th>Over</th><th>Under</th>' +
    '<th>1</th><th>X</th><th>2</th>' +
    '</tr></thead><tbody>';
  for(let i=0;i<rows.length;i++){
    const r=rows[i], p=i>0?rows[i-1]:null;
    const score=(r.home_score!=null?r.home_score:'?')+'-'+(r.away_score!=null?r.away_score:'?');
    h+='<tr>'+
      '<td style="color:var(--muted)">'+fmtInHalf(r)+'</td>'+
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

function toggleAdv(){
  const p = document.getElementById('advPanel');
  if (p.hasAttribute('data-open')) p.removeAttribute('data-open');
  else p.setAttribute('data-open','1');
}
function resetAdv(){
  ['advOpenHc','advOpenOu'].forEach(id => document.getElementById(id).value = '');
  for (let n=1;n<=7;n++){ document.getElementById('advOuB'+n).value=''; document.getElementById('advHcA'+n).value=''; }
}

let advLastResults = [];
let advSortField = 'start_time_utc';
let advSortDir = 'desc';

async function doAdvancedSearch(){
  const payload = {
    open_hc: document.getElementById('advOpenHc').value.trim() || null,
    open_ou: document.getElementById('advOpenOu').value.trim() || null,
    ou_before_goal: {},
    hc_after_goal: {},
    limit: 300,
  };
  for (let n=1;n<=7;n++){
    const ou = document.getElementById('advOuB'+n).value.trim();
    if (ou) payload.ou_before_goal[String(n)] = ou;
    const hc = document.getElementById('advHcA'+n).value.trim();
    if (hc) payload.hc_after_goal[String(n)] = hc;
  }
  const r = await fetch('/api/data/search-advanced', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  if (r.status === 401) { location.href='/login'; return; }
  const j = await r.json();
  advLastResults = (j && j.results) || [];
  renderResults(j && j.elapsed_ms);
}

function priorityFor(m){
  // Mirror compute_priority on the client for highlighting result rows.
  const total = (m.home_score||0) + (m.away_score||0);
  const minute = m.minute || 0;
  if (total >= 3 && minute < 75) return (m.competition && /Champions|Premier|Liga|Bundesliga|Serie A|Ligue 1|World Cup|MLS|Eredivisie|Primeira/i.test(m.competition)) ? 1 : 2;
  return 5;
}

function renderResults(elapsedMs){
  // Sort first
  const rows = [...advLastResults].sort((a,b)=>{
    const f = advSortField; const dir = advSortDir==='asc'?1:-1;
    let av = a[f], bv = b[f];
    if (f === 'start_time_utc') { av = new Date(av||0).getTime(); bv = new Date(bv||0).getTime(); }
    if (av==null) av=''; if (bv==null) bv='';
    return av>bv?dir:av<bv?-dir:0;
  });

  let h = '<div class="results-bar">' +
    '<span><b>'+rows.length+'</b> k\\u1ebft qu\\u1ea3</span>' +
    (elapsedMs!=null?'<span>· '+elapsedMs+' ms</span>':'') +
    '<span class="spacer"></span>' +
    '<button class="btn" onclick="exportResults(\\'csv\\')">&#x2B07; CSV</button>' +
    '<button class="btn" onclick="exportResults(\\'txt\\')">&#x2B07; TXT</button>' +
    '</div>';
  if (!rows.length){ document.getElementById('contentArea').innerHTML = h + '<div class="empty">Kh\\u00f4ng c\\u00f3 tr\\u1eadn n\\u00e0o kh\\u1edbp b\\u1ed9 l\\u1ecdc.</div>'; return; }

  const headers = [
    ['competition','Gi\\u1ea3i'],
    ['home','Nh\\u00e0'],
    ['away','Kh\\u00e1ch'],
    ['_score','T\\u1ef7 s\\u1ed1'],
    ['status','Tr\\u1ea1ng'],
    ['open_hc','HC open'],
    ['open_ou','OU open'],
    ['start_time_utc','Th\\u1eddi gian'],
  ];
  h += '<table class="rtable" id="rtable"><thead><tr>' + headers.map(([f,l])=>{
    const cls = (advSortField===f) ? (' class="sort-'+advSortDir+'"') : '';
    return '<th'+cls+' onclick="sortResults(\\''+f+'\\')">'+l+' <span class="sortar">\\u25BE</span></th>';
  }).join('') + '</tr></thead><tbody>';

  for (const m of rows){
    const prio = priorityFor(m);
    const rowCls = (prio<=4 ? 'row-prio-'+prio+' ' : '') + 'row-clickable';
    const score = (m.home_score||0)+' - '+(m.away_score||0);
    h += '<tr class="'+rowCls+'" onclick="toggleExpand(\\''+escHtml(m.id)+'\\', this)">' +
      '<td title="'+escHtml(m.competition||'')+'">'+escHtml((m.competition||'').substring(0,28))+'</td>' +
      '<td>'+escHtml(m.home)+'</td>' +
      '<td>'+escHtml(m.away)+'</td>' +
      '<td style="text-align:center;font-weight:700">'+score+'</td>' +
      '<td>'+badge(m.status)+'</td>' +
      '<td>'+escHtml(m.open_hc||'\\u2014')+'</td>' +
      '<td>'+escHtml(m.open_ou||'\\u2014')+'</td>' +
      '<td style="color:var(--muted)">'+fmt(m.start_time_utc)+'</td>' +
      '</tr>';
    // Hidden expansion row (filled on first click)
    h += '<tr id="exp-'+escHtml(m.id)+'" style="display:none"><td colspan="8" style="padding:8px 12px;background:rgba(88,166,255,.04)"></td></tr>';
  }
  h += '</tbody></table>';
  document.getElementById('contentArea').innerHTML = h;
}

function sortResults(field){
  if (advSortField === field) advSortDir = (advSortDir==='asc' ? 'desc' : 'asc');
  else { advSortField = field; advSortDir = 'asc'; }
  renderResults();
}

async function toggleExpand(matchId, tr){
  const exp = document.getElementById('exp-'+matchId);
  if (!exp) return;
  if (exp.style.display !== 'none') { exp.style.display = 'none'; tr.classList.remove('expanded'); return; }
  exp.style.display = '';
  tr.classList.add('expanded');
  const cell = exp.querySelector('td');
  cell.innerHTML = '<div style="color:var(--muted);font-size:11px">\\u0110ang t\\u1ea3i b\\u1ea3ng bi\\u1ebfn \\u0111\\u1ed9ng k\\u00e8o...</div>';
  const odds = await apiFetch('/api/matches/'+encodeURIComponent(matchId)+'/odds-history');
  const m = advLastResults.find(r => r.id === matchId) || {};
  cell.innerHTML = renderOddsTable(odds||[], m.home, m.away);
}

function exportResults(kind){
  if (!advLastResults.length){ showToast('Kh\\u00f4ng c\\u00f3 d\\u1eef li\\u1ec7u \\u0111\\u1ec3 export'); return; }
  let body, mime, ext;
  if (kind === 'csv') {
    const headers = ['id','competition','home','away','home_score','away_score','status','open_hc','open_ou','start_time_utc'];
    const esc = v => '"' + String(v==null?'':v).replace(/"/g,'""') + '"';
    body = headers.join(',') + '\\n' + advLastResults.map(r => headers.map(h => esc(r[h])).join(',')).join('\\n');
    mime = 'text/csv'; ext = 'csv';
  } else {
    body = advLastResults.map(r => {
      const score = (r.home_score||0)+'-'+(r.away_score||0);
      const goalsTxt = (r.goals||[]).map(g => '  #' + g.goal_number + ' (' + g.team + ', \\''+ (g.minute||'') +') HC ' + (g.hc_before||'?') + ' \\u2192 ' + (g.hc_after||'?') + ' \\u00b7 OU ' + (g.ou_before||'?') + ' \\u2192 ' + (g.ou_after||'?')).join('\\n');
      return [r.competition, r.home + ' ' + score + ' ' + r.away, r.status, r.start_time_utc, goalsTxt].join('\\n') + '\\n';
    }).join('\\n----\\n');
    mime = 'text/plain'; ext = 'txt';
  }
  const blob = new Blob([body], {type: mime + ';charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'football_search_' + Date.now() + '.' + ext;
  document.body.appendChild(a); a.click(); a.remove();
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


# ---- Market-gate enforcement ----------------------------------------------
# Routes the gated user is still allowed to hit: /market itself, the auth
# pages, OTP endpoints (so they can verify and escape), static assets, and
# health checks. Anything else is redirected to /market.
_MARKET_GATE_ALLOW_PREFIXES = (
    "/market",
    "/login",
    "/logout",
    "/static/",
    "/api/health",
    "/api/auth/",
    "/api/lock/",
    "/api/telegram/diagnose",
    "/favicon",
)


@app.middleware("http")
async def _market_gate(request: Request, call_next):
    if request.cookies.get("market_gate") == "pending":
        path = request.url.path
        if not any(path == p.rstrip("/") or path.startswith(p) for p in _MARKET_GATE_ALLOW_PREFIXES):
            return RedirectResponse(url="/market", status_code=302)
    return await call_next(request)


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
    diag = tg.diagnose()
    if not diag["has_bot_token"] or not diag["has_chat_ids"]:
        return JSONResponse(
            {"ok": False,
             "error": f"Bot Telegram chưa được cấu hình ({diag}) — đặt TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_IDS trong .env hoặc Cài đặt Telegram"},
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

# ---------------------------------------------------------------------------
# Market decoy + return-gate
# ---------------------------------------------------------------------------
# When the user lands on /market we set an HttpOnly cookie `market_gate=pending`.
# A middleware (_market_gate, registered above) bounces every request for the
# real app routes back to /market while the cookie is set. The cookie is
# cleared only when /api/lock/verify-otp succeeds, so the only way out is a
# valid Telegram OTP — even repeated Back-button presses just land back here.
_MARKET_GATE_COOKIE = "market_gate"
_MARKET_GATE_MAX_AGE = 60 * 60 * 24  # 24h


@app.get("/market", response_class=HTMLResponse)
async def market_page():
    resp = HTMLResponse(_MARKET_HTML)
    resp.set_cookie(
        _MARKET_GATE_COOKIE,
        "pending",
        max_age=_MARKET_GATE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


@app.get("/data", response_class=HTMLResponse)
async def data_browser_page(user: str = Depends(require_auth)):
    return HTMLResponse(_DATA_HTML)


@app.post("/api/lock/request-otp")
async def api_lock_request_otp(user: str = Depends(require_auth)):
    """Send an unlock-OTP to the configured Telegram chats for the locked user."""
    from .database import store_otp
    from . import telegram as tg

    diag = tg.diagnose()
    if not diag["has_bot_token"] or not diag["has_chat_ids"]:
        return JSONResponse(
            {"ok": False, "error": f"Bot Telegram chưa được cấu hình ({diag})"},
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
        resp = JSONResponse({"ok": True})
        # Also clear the market gate (if set) — same OTP serves both idle-lock
        # and market-return flows. The cookie was set with HttpOnly so JS can't
        # tamper; clearing here is the only escape from /market.
        resp.delete_cookie(_MARKET_GATE_COOKIE, path="/")
        return resp
    return JSONResponse({"ok": False, "error": "OTP không đúng hoặc đã hết hạn"}, status_code=401)


@app.get("/api/lock/config")
async def api_lock_config(user: str = Depends(require_auth)):
    """Frontend reads idle timeout from here so the value lives in .env only."""
    return {"idle_seconds": _IDLE_LOCK_SECONDS, "otp_ttl_seconds": _OTP_TTL}


@app.get("/api/stats/timeline")
async def api_stats_timeline(
    period: str = "day",
    date: str = "",
    user: str = Depends(require_auth),
):
    """Stats for a period (day/month/year) or a specific calendar date.

    `date=YYYY-MM-DD` overrides `period` and serves the date-picker dashboard
    (Yêu cầu #6B).
    """
    from .database import get_timeline_stats
    return JSONResponse(get_timeline_stats(period, target_date=date or None))


# ---------------------------------------------------------------------------
# Telegram Setting API — Yêu cầu #7
# ---------------------------------------------------------------------------

@app.get("/api/telegram/settings")
async def api_telegram_settings_get(user: str = Depends(require_auth)):
    from .database import get_telegram_settings
    return JSONResponse(get_telegram_settings())


@app.post("/api/telegram/settings")
async def api_telegram_settings_save(payload: dict = Body(...), user: str = Depends(require_auth)):
    from .database import update_telegram_settings
    # Sanity-clamp the numeric thresholds before persisting.
    if "goal_threshold" in payload:
        try:
            payload["goal_threshold"] = max(1, min(20, int(payload["goal_threshold"])))
        except (TypeError, ValueError):
            payload.pop("goal_threshold", None)
    if "before_minute" in payload:
        try:
            payload["before_minute"] = max(1, min(120, int(payload["before_minute"])))
        except (TypeError, ValueError):
            payload.pop("before_minute", None)
    if "scope" in payload and payload["scope"] not in ("prestigious", "all"):
        payload.pop("scope", None)
    out = update_telegram_settings(payload)
    # `lock_required` cues the client to call window.showLock() — Yêu cầu #12.
    return {"ok": True, "settings": out, "lock_required": True}


@app.post("/api/telegram/test")
async def api_telegram_test(user: str = Depends(require_auth)):
    from . import telegram as tg
    diag = tg.diagnose()
    if not diag["has_bot_token"]:
        return JSONResponse({
            "ok": False,
            "error": "Bot token chưa cấu hình (đặt TELEGRAM_BOT_TOKEN trong .env hoặc nhập trong Cài đặt Telegram)",
            "diag": diag,
        }, status_code=400)
    if not diag["has_chat_ids"]:
        return JSONResponse({
            "ok": False,
            "error": "Chưa có chat_id (đặt TELEGRAM_CHAT_IDS trong .env hoặc nhập trong Cài đặt Telegram)",
            "diag": diag,
        }, status_code=400)
    result = tg.send_message("✅ <b>Test thành công</b>\nFootball Collector — Telegram Setting hoạt động.")
    if not result.get("ok"):
        return JSONResponse({
            "ok": False,
            "error": result.get("error") or "Không gửi được",
            "diag": diag,
        }, status_code=502)
    return {"ok": True, "sent": result.get("sent"), "total": result.get("total"), "diag": diag}


@app.get("/api/telegram/diagnose")
async def api_telegram_diagnose(user: str = Depends(require_auth)):
    """Surface exactly what's wired up so the UI can show a precise reason."""
    from . import telegram as tg
    return JSONResponse(tg.diagnose())


# ---------------------------------------------------------------------------
# Layer 3 advanced search — Yêu cầu #8
# ---------------------------------------------------------------------------

@app.post("/api/data/search-advanced")
async def api_search_advanced(payload: dict = Body(...), user: str = Depends(require_auth)):
    from .database import advanced_search
    open_hc = payload.get("open_hc")
    open_ou = payload.get("open_ou")
    ou_before_goal = payload.get("ou_before_goal") or {}
    hc_after_goal = payload.get("hc_after_goal") or {}
    limit = int(payload.get("limit") or 200)

    import time as _t
    t0 = _t.perf_counter()
    rows = advanced_search(
        open_hc=open_hc,
        open_ou=open_ou,
        ou_before_goal=ou_before_goal,
        hc_after_goal=hc_after_goal,
        limit=limit,
    )
    elapsed_ms = int((_t.perf_counter() - t0) * 1000)
    return JSONResponse({"results": rows, "count": len(rows), "elapsed_ms": elapsed_ms})


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
