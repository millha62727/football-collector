"""FastAPI router for the Layer-2 analyzer page and its JSON API."""
from __future__ import annotations

import io
import json
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..auth import require_auth
from ..database import get_match_by_id, get_odds_history_for_analyzer
from . import ai_client as AI
from . import ai_pattern as AIP
from . import parser as P
from . import storage as ST

router = APIRouter()


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

_PAGE_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Football Analyzer — Diễn biến trận</title>
<style>
:root{
  --bg:#0d1117;--surface:#161b22;--card:#21262d;--border:#30363d;
  --primary:#58a6ff;--pdim:#1f3a5f;
  --red:#f85149;--green:#3fb950;--orange:#d29922;--purple:#bc8cff;
  --home:#3fb950;--away:#bc8cff;--level:#8b949e;
  --text:#c9d1d9;--muted:#8b949e;--r:6px;
  --font:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100vh;display:flex;flex-direction:column;font-size:13px}

.hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 20px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:10000}
.btn-sm{padding:5px 12px;border-radius:var(--r);border:1px solid var(--border);background:var(--card);color:var(--text);cursor:pointer;font-size:12px;transition:all .15s;text-decoration:none;display:inline-block;font-family:inherit}
.btn-sm:hover{border-color:var(--primary);color:var(--primary)}
.btn-sm.danger:hover{border-color:var(--red);color:var(--red)}
.hdr-logo{font-size:16px;font-weight:700;color:var(--primary)}
.hdr a{color:var(--muted);text-decoration:none;font-size:13px}
.hdr a:hover{color:var(--primary)}
.hdr-spacer{flex:1}

.main{flex:1;padding:14px 20px;max-width:1500px;margin:0 auto;width:100%}

/* top bar */
.topbar{display:flex;gap:12px;align-items:center;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;margin-bottom:14px;flex-wrap:wrap}
.drop{flex:1;min-width:280px;padding:10px;border:1px dashed var(--border);border-radius:var(--r);text-align:center;color:var(--muted);cursor:pointer;transition:all .15s}
.drop:hover,.drop.over{border-color:var(--primary);color:var(--primary);background:rgba(88,166,255,.05)}
.match-info{font-size:13px;line-height:1.6}
.match-info .league{font-weight:700;color:var(--primary)}
.match-info .dt{color:var(--muted);font-size:12px}

.hcap-box{background:rgba(63,185,80,.06);border:1px solid rgba(63,185,80,.3);border-radius:var(--r);padding:8px 12px;font-size:12px;line-height:1.6;min-width:200px}
.hcap-box span.h{color:var(--home);font-weight:600}
.hcap-box span.a{color:var(--away);font-weight:600}

.pred{display:flex;align-items:center;gap:6px;background:rgba(210,153,34,.08);border:1px solid rgba(210,153,34,.3);border-radius:var(--r);padding:6px 10px}
.pred input{width:42px;background:#0d1117;border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:16px;font-weight:700;text-align:center;padding:4px}
.pred input:focus{outline:none;border-color:var(--orange)}
.pred .lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}

/* mid */
.mid{display:flex;gap:14px;margin-bottom:14px}
.col-db{flex:0 0 auto}
.col-tran{flex:1;min-width:0}
.panel{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}
.panel-h{padding:8px 12px;background:var(--surface);border-bottom:1px solid var(--border);font-weight:700;font-size:12px;color:var(--primary);text-transform:uppercase;letter-spacing:.6px}
.panel-b{padding:8px}

/* dien bien table */
table.db{border-collapse:collapse;font-size:12px}
table.db th{padding:6px 4px;background:var(--surface);color:var(--muted);font-weight:600;border:1px solid var(--border);font-size:11px;text-transform:uppercase}
table.db td{padding:3px;background:#1a1f26;border:1px solid var(--border);text-align:center;vertical-align:middle}
table.db td.sc{font-weight:700;color:var(--primary);min-width:48px}
table.db td.dim{color:var(--muted)}
table.db input[type=text]{width:46px;background:#0d1117;border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;text-align:center;padding:2px 1px}
table.db input[type=text]:disabled{background:#181d24;border-color:transparent;color:var(--text);opacity:.85}
table.db input.note{width:42px;background:#222831;border:1px solid #2a3038;color:var(--orange);font-size:11px}
table.db .home{color:var(--home);font-weight:700}
table.db .away{color:var(--away);font-weight:700}
table.db .level{color:var(--level);font-weight:700}
table.db .b{font-weight:700;color:var(--text)}
table.db .lbl-win{color:#1060d0;font-weight:700}
table.db .lbl-lose{color:var(--red);font-weight:700}
table.db .lbl-draw{color:var(--orange);font-weight:700}
table.db .lbl-half{color:var(--muted);font-weight:700}
table.db .lbl-none{color:var(--muted)}

/* tran scrollable */
.tran-wrap{max-height:520px;overflow-y:auto;border:1px solid var(--border);border-radius:var(--r)}
table.tran{width:100%;border-collapse:collapse;font-size:12px}
table.tran th{position:sticky;top:0;padding:6px 4px;background:var(--surface);color:var(--muted);font-weight:600;border-bottom:1px solid var(--border);font-size:11px;text-transform:uppercase;text-align:center;z-index:1}
table.tran td{padding:3px 4px;border-bottom:1px solid #1a1f26;text-align:center;font-variant-numeric:tabular-nums}
table.tran tr:nth-child(even){background:#11161d}
table.tran tr.goal{background:#3c2a00 !important;color:#ffd866;font-weight:700}
table.tran tr.goal:nth-child(even){background:#4a3500 !important}

/* bot */
.bot{display:flex;gap:14px;align-items:stretch}
.note-panel{flex:1;background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;display:flex;flex-direction:column;min-height:120px}
.note-panel textarea{flex:1;background:#0d1117;color:var(--text);border:0;padding:8px 10px;font-family:var(--font);font-size:12px;resize:none;outline:none}
.actions{display:flex;flex-direction:column;gap:6px;min-width:130px}
.btn{padding:8px 12px;border-radius:var(--r);border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;font-size:12px;font-weight:600;transition:all .15s;text-align:center}
.btn:hover{background:var(--card);border-color:var(--primary);color:var(--primary)}
.btn.active{background:var(--orange);border-color:var(--orange);color:#000}
.btn.run{background:var(--pdim);border-color:var(--primary);color:var(--primary)}
.btn.run:hover{background:var(--primary);color:#000}
.btn.save{background:rgba(63,185,80,.12);border-color:var(--green);color:var(--green)}
.btn.save:hover{background:var(--green);color:#000}
.btn.open{background:rgba(248,81,73,.12);border-color:var(--red);color:var(--red)}
.btn.open:hover{background:var(--red);color:#fff}
.btn.ai{background:rgba(188,140,255,.12);border-color:var(--purple);color:var(--purple)}
.btn.ai:hover:not(:disabled){background:var(--purple);color:#000}
.btn:disabled{opacity:.45;cursor:default}
.btn.ai.busy{opacity:.6;cursor:wait}

/* AI result cards */
.ai-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:12px 14px;margin-bottom:10px}
.ai-card h4{margin:0 0 6px;font-size:12px;color:var(--purple);text-transform:uppercase;letter-spacing:.04em}
.ai-summary{font-size:14px;line-height:1.5;color:var(--text)}
.ai-pred-grid{display:flex;gap:10px;flex-wrap:wrap;margin-top:4px}
.ai-pill{background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:4px 12px;font-size:13px}
.ai-pill b{color:var(--primary)}
.ai-conf-bar{height:8px;border-radius:4px;background:var(--surface);overflow:hidden;margin-top:6px}
.ai-conf-fill{height:100%;background:linear-gradient(90deg,var(--orange),var(--green))}
.ai-list{margin:4px 0 0;padding-left:18px;font-size:13px;line-height:1.55}
.ai-list li{margin-bottom:2px}
.ai-meta{font-size:11px;color:var(--muted);margin-top:6px}
.ai-raw{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:11px;color:var(--muted);background:var(--bg);border:1px solid var(--border);border-radius:var(--r);padding:8px;max-height:240px;overflow:auto}
.ai-base{font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:8px 16px}
.ai-base b{color:var(--text)}

/* modal */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:100}
.modal-bg.open{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:18px;width:600px;max-width:92vw;max-height:80vh;overflow:auto}
.modal h3{margin-bottom:12px;color:var(--primary);font-size:14px}
.modal table{width:100%;border-collapse:collapse;font-size:12px}
.modal th{padding:6px;text-align:left;color:var(--muted);border-bottom:1px solid var(--border)}
.modal td{padding:6px;border-bottom:1px solid #1a1f26}
.modal tr:hover{background:#1a1f26;cursor:pointer}
.modal .empty{text-align:center;color:var(--muted);padding:24px}

.toast{position:fixed;bottom:20px;right:20px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:10px 16px;font-size:12px;display:none;z-index:200;max-width:340px}
.toast.show{display:block}
.toast.ok{border-color:var(--green);color:var(--green)}
.toast.err{border-color:var(--red);color:var(--red)}
.toast.warn{border-color:var(--orange);color:var(--orange)}
.muted{color:var(--muted)}
.no-data{color:var(--muted);text-align:center;padding:24px}

/* AI status badge (header) */
.ai-badge{display:flex;align-items:center;gap:7px;padding:4px 10px;border-radius:var(--r);border:1px solid var(--border);background:var(--card);font-size:12px;white-space:nowrap}
.ai-badge .ai-dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:0 0 auto;transition:background .2s}
.ai-badge .ai-dot.on{background:var(--green);box-shadow:0 0 6px rgba(63,185,80,.6)}
.ai-badge .ai-dot.off{background:var(--muted)}
.ai-badge .ai-dot.err{background:var(--red);box-shadow:0 0 6px rgba(248,81,73,.6)}
.ai-badge .ai-dot.checking{background:var(--orange);animation:ai-pulse 1s ease-in-out infinite}
@keyframes ai-pulse{0%,100%{opacity:1}50%{opacity:.3}}
.ai-badge #ai-label{color:var(--text)}
.ai-badge #ai-label .dim{color:var(--muted)}
.ai-badge .ai-check{padding:2px 9px;border-radius:4px;border:1px solid var(--border);background:var(--surface);color:var(--muted);cursor:pointer;font-size:11px;font-family:inherit;transition:all .15s}
.ai-badge .ai-check:hover:not(:disabled){border-color:var(--primary);color:var(--primary)}
.ai-badge .ai-check:disabled{opacity:.5;cursor:default}
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-logo">&#x26BD; Analyzer</div>
  <a href="/">&larr; Dashboard</a>
  <div class="hdr-spacer"></div>
  <div class="ai-badge" id="ai-badge" title="Trạng thái AI LLM (OpenAI-compatible)">
    <span class="ai-dot off" id="ai-dot"></span>
    <span id="ai-label"><span class="dim">AI: …</span></span>
    <button class="ai-check" id="ai-check-btn" onclick="checkAI()" disabled>Kiểm tra</button>
  </div>
  <span class="muted" id="user-info"></span>
  <button class="btn-sm" onclick="goDisguise()">&#x1F504; &#x110;&#x1ED5;i</button>
  <form method="post" action="/logout" style="display:inline">
    <button type="submit" style="background:none;border:0;color:var(--muted);cursor:pointer;font-size:13px">Đăng xuất</button>
  </form>
</div>

<div class="main">

  <!-- TOP BAR -->
  <div class="topbar">
    <div class="drop" id="drop" tabindex="0">
      <strong>⚽ Chọn trận từ hệ thống</strong>
      <div style="font-size:11px;margin-top:5px;color:var(--muted)">hoặc kéo thả / <span style="color:var(--primary);cursor:pointer" onclick="event.stopPropagation();document.getElementById('file').click()">upload CSV</span></div>
      <input type="file" id="file" accept=".csv,text/csv" style="display:none">
    </div>
    <div class="match-info" id="match-info">
      <div class="league">—</div>
      <div class="teams muted">Chưa có trận</div>
      <div class="dt">—</div>
    </div>
    <div class="hcap-box" id="hcap-box">
      <div>Được chấp: <span id="duoc-chap">—</span></div>
      <div>Chấp: <span id="chap">—</span></div>
      <div>Tỉ số thực: <span id="real-score">—</span></div>
    </div>
    <div class="pred">
      <span class="lbl">Dự đoán</span>
      <input id="pred-fh" type="number" min="0" max="99">
      <span>—</span>
      <input id="pred-fa" type="number" min="0" max="99">
      <button class="btn" style="padding:4px 8px;font-size:11px" onclick="resetPred()">↺ Reset</button>
      <button class="btn run" style="padding:4px 8px;font-size:11px" onclick="runCompute()">▶ Run</button>
    </div>
  </div>

  <!-- MID -->
  <div class="mid">
    <div class="col-db panel">
      <div class="panel-h">Diễn Biến</div>
      <div class="panel-b">
        <table class="db" id="db-table">
          <thead>
            <tr>
              <th>Score</th>
              <th>[a] Hcap</th><th>b</th><th>n</th>
              <th>[c] O/U-B</th><th>d</th><th>n</th>
              <th>[e] O/U-A</th><th>g</th><th>n</th>
            </tr>
          </thead>
          <tbody id="db-body"></tbody>
        </table>
      </div>
    </div>
    <div class="col-tran panel">
      <div class="panel-h">Trận</div>
      <div class="panel-b" style="padding:0">
        <div class="tran-wrap">
          <table class="tran" id="tran-table">
            <thead>
              <tr>
                <th>Half</th><th>Home</th><th>Away</th>
                <th>Home Hcap</th><th>Away Hcap</th><th>O/U Line</th>
              </tr>
            </thead>
            <tbody id="tran-body">
              <tr><td colspan="6" class="no-data">Chưa có dữ liệu</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- BOT -->
  <div class="bot">
    <div class="note-panel">
      <div class="panel-h" style="border-bottom:1px solid var(--border);padding:6px 10px;background:var(--surface);font-size:11px">Note</div>
      <textarea id="note-text" placeholder="Ghi chú trận..."></textarea>
    </div>
    <div class="actions">
      <button class="btn" id="btn-edit" onclick="toggleEdit()">✏ Sửa</button>
      <button class="btn run" onclick="runCompute()">▶ Run</button>
      <button class="btn ai" id="btn-ai-predict" onclick="aiPredict()" disabled title="Cần cấu hình AI">🤖 AI dự đoán</button>
      <button class="btn" id="btn-view-pattern" onclick="viewStoredPattern()" style="display:none" title="Xem pattern AI đã lưu cho trận này">📋 Pattern đã lưu</button>
      <button class="btn save" onclick="saveSession()">💾 Save</button>
      <button class="btn open" onclick="openModal()">📂 Open</button>
      <button class="btn" onclick="exportJSON()">⬇ Export JSON</button>
      <button class="btn" onclick="exportCSV()">⬇ Export CSV</button>
      <button class="btn" onclick="document.getElementById('import-file').click()">⬆ Import JSON</button>
      <input type="file" id="import-file" accept=".json,application/json" style="display:none">
    </div>
  </div>

</div>

<!-- session modal -->
<div class="modal-bg" id="modal-bg" onclick="closeModalIfBg(event)">
  <div class="modal">
    <h3>Mở session đã lưu</h3>
    <table>
      <thead><tr><th>Tên file</th><th>Trận</th><th>Cập nhật</th><th></th></tr></thead>
      <tbody id="modal-body"></tbody>
    </table>
  </div>
</div>

<!-- match picker modal -->
<div class="modal-bg" id="match-picker-bg" onclick="closeMatchPickerBg(event)">
  <div class="modal" style="width:720px;max-width:96vw">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
      <h3 style="margin:0">Chọn trận đấu</h3>
      <span id="mp-count" style="font-size:11px;color:var(--muted)"></span>
      <button onclick="closeMatchPicker()" style="margin-left:auto;background:none;border:0;color:var(--muted);cursor:pointer;font-size:20px;line-height:1">&times;</button>
    </div>
    <input id="mp-search" type="search" placeholder="Tìm đội, giải đấu..." style="width:100%;padding:8px 10px;background:#0d1117;border:1px solid var(--border);border-radius:var(--r);color:var(--text);font-size:13px;margin-bottom:10px;box-sizing:border-box;outline:none">
    <div style="max-height:420px;overflow-y:auto">
      <table>
        <thead><tr><th>Giải</th><th>Trận</th><th>Score</th><th>Trạng thái</th><th></th></tr></thead>
        <tbody id="mp-body"><tr><td colspan="5" class="empty">Đang tải...</td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<!-- AI prediction modal -->
<div class="modal-bg" id="ai-modal-bg" onclick="closeAIModalBg(event)">
  <div class="modal" style="width:760px;max-width:96vw">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
      <h3 style="margin:0">🤖 AI phân tích</h3>
      <span id="ai-modal-model" style="font-size:11px;color:var(--muted)"></span>
      <button onclick="closeAIModal()" style="margin-left:auto;background:none;border:0;color:var(--muted);cursor:pointer;font-size:20px;line-height:1">&times;</button>
    </div>
    <div id="ai-modal-body" style="max-height:70vh;overflow-y:auto"></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script src="/static/analyzer.js"></script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/analyzer", response_class=HTMLResponse)
async def analyzer_page(user: str = Depends(require_auth)):
    return HTMLResponse(_PAGE_HTML)


@router.post("/api/analyzer/parse")
async def api_parse(
    file: UploadFile = File(...),
    user: str = Depends(require_auth),
):
    """Parse uploaded CSV and run the initial analysis (no overrides)."""
    raw = (await file.read())
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    rows = P.read_csv_text(text)
    meta = P.parse_fname(file.filename or "")
    errs = P.validate(rows)
    result = P.compute(rows)

    return JSONResponse({
        "filename": file.filename or "",
        "meta": meta,
        "csv_blob": text,
        "rows_filtered": _filtered_rows(rows),
        "errors": errs,
        "analysis": result["milestones"],
        "real_fh": result["real_fh"],
        "real_fa": result["real_fa"],
        "ohh": result["ohh"],
        "oah": result["oah"],
        "favorite_home": result["ohh"] < 0,  # True if home is chấp
    })


@router.post("/api/analyzer/compute")
async def api_compute(
    payload: dict[str, Any] = Body(...),
    user: str = Depends(require_auth),
):
    """Re-run compute with edits / prediction."""
    csv_text = payload.get("csv_blob") or ""
    if not csv_text:
        raise HTTPException(400, "csv_blob required")
    overrides_raw = payload.get("overrides") or {}
    overrides = {int(k): v for k, v in overrides_raw.items() if isinstance(v, dict)}
    pred_fh = payload.get("pred_fh")
    pred_fa = payload.get("pred_fa")
    pred_fh = int(pred_fh) if pred_fh not in (None, "") else None
    pred_fa = int(pred_fa) if pred_fa not in (None, "") else None

    rows = P.read_csv_text(csv_text)
    result = P.compute(rows, overrides=overrides, pred_fh=pred_fh, pred_fa=pred_fa)
    return JSONResponse({
        "analysis": result["milestones"],
        "real_fh": result["real_fh"],
        "real_fa": result["real_fa"],
        "effective_fh": result["effective_fh"],
        "effective_fa": result["effective_fa"],
    })


@router.post("/api/analyzer/save")
async def api_save(
    payload: dict[str, Any] = Body(...),
    user: str = Depends(require_auth),
):
    if not payload.get("csv_blob"):
        raise HTTPException(400, "csv_blob required")
    if not payload.get("filename"):
        raise HTTPException(400, "filename required")
    sid = ST.save_session(user, payload)
    return JSONResponse({"id": sid})


@router.get("/api/analyzer/sessions")
async def api_list_sessions(user: str = Depends(require_auth)):
    return JSONResponse({"sessions": ST.list_sessions(user)})


@router.get("/api/analyzer/sessions/{sid}")
async def api_load_session(sid: int, user: str = Depends(require_auth)):
    s = ST.load_session(user, sid)
    if not s:
        raise HTTPException(404, "not found")
    rows = P.read_csv_text(s["csv_blob"])
    s["rows_filtered"] = _filtered_rows(rows)
    return JSONResponse(s)


@router.delete("/api/analyzer/sessions/{sid}")
async def api_delete_session(sid: int, user: str = Depends(require_auth)):
    ok = ST.delete_session(user, sid)
    if not ok:
        raise HTTPException(404, "not found")
    return JSONResponse({"ok": True})


@router.get("/api/analyzer/sessions/{sid}/export")
async def api_export(sid: int, user: str = Depends(require_auth)):
    """Export a session as a JSON file matching the original tool's format."""
    s = ST.load_session(user, sid)
    if not s:
        raise HTTPException(404, "not found")
    legacy = {
        "filepath": s["filename"],
        "meta": s["meta"],
        "overrides": {str(k): v for k, v in (s["overrides"] or {}).items()},
        "notes": s["notes"],
        "note_text": s["note_text"],
        "analysis": s["analysis"],
    }
    text = json.dumps(legacy, ensure_ascii=False, indent=2)
    base_name = (s["filename"] or "session").replace(".csv", "") + ".json"
    encoded_name = quote(base_name, safe="")
    content_disp = f"attachment; filename*=UTF-8''{encoded_name}"
    return Response(
        content=text.encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": content_disp},
    )


@router.get("/api/analyzer/sessions/{sid}/csv")
async def api_export_csv(sid: int, user: str = Depends(require_auth)):
    """Export the raw CSV blob from a session."""
    s = ST.load_session(user, sid)
    if not s:
        raise HTTPException(404, "not found")
    blob = s.get("csv_blob") or ""
    base_name = (s["filename"] or "session")
    if not base_name.endswith(".csv"):
        base_name += ".csv"
    encoded_name = quote(base_name, safe="")
    return Response(
        content=blob.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )


@router.post("/api/analyzer/import")
async def api_import(
    file: UploadFile = File(...),
    user: str = Depends(require_auth),
):
    """Import a legacy JSON file (saved by the old Tkinter tool).
    The CSV may not be embedded in legacy format — caller must also re-upload it.
    """
    raw = (await file.read())
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"invalid JSON: {e}")
    return JSONResponse({
        "meta": data.get("meta") or {},
        "overrides": data.get("overrides") or {},
        "notes": data.get("notes") or [],
        "note_text": data.get("note_text") or "",
        "analysis": data.get("analysis") or [],
        "filename": data.get("filepath") or file.filename or "",
        "needs_csv": True,
    })


# ---------------------------------------------------------------------------
# AI (OpenAI-compatible) — optional, env-gated
# ---------------------------------------------------------------------------

@router.get("/api/ai/status")
async def api_ai_status(user: str = Depends(require_auth)):
    """Resolved AI config (no round-trip). Drives the header badge.

    The API key is masked server-side — never returned in full.
    """
    return JSONResponse(AI.diagnose())


@router.post("/api/ai/check")
async def api_ai_check(user: str = Depends(require_auth)):
    """Real connectivity probe: sends a minimal completion and reports latency.

    Never 500s on an upstream failure — the failure is reported in the body so
    the UI can show a precise reason. Returns 400 only when AI isn't configured.
    """
    result = await AI.check()
    status = 200 if result.get("ok") else (400 if not result.get("configured") else 502)
    return JSONResponse(result, status_code=status)


@router.post("/api/analyzer/ai-predict")
async def api_ai_predict(
    payload: dict[str, Any] = Body(...),
    user: str = Depends(require_auth),
):
    """Grounded LLM analysis of the current analyzer state.

    Sends deterministic features (parser.compute) + empirical base-rates
    (database.compute_pattern_stats) to the model. The LLM only interprets the
    numbers it is given — it never invents sample sizes or rates. Advisory only.

    Returns 400 when AI isn't configured, 502 on upstream/LLM failure.
    """
    if not AI.is_configured():
        raise HTTPException(400, "AI chưa được cấu hình")

    csv_text = payload.get("csv_blob") or ""
    if not csv_text:
        raise HTTPException(400, "csv_blob required")

    overrides_raw = payload.get("overrides") or {}
    overrides = {int(k): v for k, v in overrides_raw.items() if isinstance(v, dict)}
    pred_fh = payload.get("pred_fh")
    pred_fa = payload.get("pred_fa")
    pred_fh = int(pred_fh) if pred_fh not in (None, "") else None
    pred_fa = int(pred_fa) if pred_fa not in (None, "") else None
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    prestigious_only = bool(payload.get("prestigious_only", False))

    rows = P.read_csv_text(csv_text)
    try:
        out = await AIP.analyze_match(
            rows,
            meta=meta,
            pred_fh=pred_fh,
            pred_fa=pred_fa,
            overrides=overrides,
            prestigious_only=prestigious_only,
        )
    except Exception as e:  # upstream/LLM/parse failure — never 500
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True, **out})


@router.get("/api/analyzer/patterns/{match_id:path}")
async def api_get_pattern(match_id: str, user: str = Depends(require_auth)):
    """Most recent stored AI pattern for a match (None if not yet processed)."""
    from ..database import get_match_pattern
    p = get_match_pattern(match_id)
    return JSONResponse({"pattern": p})


@router.post("/api/analyzer/save-pattern")
async def api_save_pattern(
    payload: dict[str, Any] = Body(...),
    user: str = Depends(require_auth),
):
    """On-demand: run grounded analysis for a DB match and persist it.

    Mirrors what the cron sweep does, but for a single match the user is
    looking at. Returns the compact status dict from analyze_and_store.
    """
    if not AI.is_configured():
        raise HTTPException(400, "AI chưa được cấu hình")
    match_id = (payload.get("match_id") or "").strip()
    if not match_id:
        raise HTTPException(400, "match_id required")
    prestigious_only = bool(payload.get("prestigious_only", False))
    try:
        out = await AIP.analyze_and_store(match_id, prestigious_only=prestigious_only)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True, **out})


@router.get("/api/analyzer/patterns-aggregate")
async def api_patterns_aggregate(
    prestigious_only: bool = True,
    user: str = Depends(require_auth),
):
    """Tag/signal frequency roll-up across stored patterns — the raw material
    for distilling 'công thức'."""
    from ..database import aggregate_patterns, count_pattern_progress
    agg = aggregate_patterns(prestigious_only=prestigious_only)
    progress = count_pattern_progress(AI._model(), prestigious_only=prestigious_only) if AI.is_configured() else {}
    return JSONResponse({"aggregate": agg, "progress": progress})


# ---------------------------------------------------------------------------
# Live match → analyzer
# ---------------------------------------------------------------------------

def _make_half(status: Optional[str], minute: Optional[int]) -> str:
    """Synthesize CSV Half value (e.g. '1H_5'') from DB status + minute."""
    if not status or not minute:
        return "-"
    s = status.upper()
    m = int(minute)
    if s == "H1":
        return f"1H_{m}'"
    if s in ("H2", "LIVE"):
        # minute from API can be absolute (46-90+) or relative (1-45+) per half
        rel = (m - 45) if m > 45 else m
        rel = max(1, rel)
        return f"2H_{rel}'"
    if s == "HT":
        return "HT"
    if s in ("UPCOMING", "NOT_STARTED"):
        return "-"
    if s == "FT":
        return f"2H_{max(m, 45)}'"
    return "-"


def _db_rows_to_csv_rows(db_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Convert match_odds_history DB rows to parser-compatible dicts.

    The parser expects column names matching the original CSV:
    Half, Home Score, Away Score, Home Handicap, Away Handicap, Over/Under Line
    (and the others can be empty — parser only reads these 6).
    """
    out = []
    for r in db_rows:
        minute = r.get("minute") or 0
        hs = r.get("home_score") or 0
        aw = r.get("away_score") or 0
        status = r.get("status") or ""
        hh = r.get("home_handicap") or "0"
        ah = r.get("away_handicap") or "0"
        ou = r.get("ou_line") or "0"
        out.append({
            "Event Status":        status,
            "Time":                "",
            "Half":                _make_half(status, minute),
            "Home Score":          str(hs),
            "Away Score":          str(aw),
            "Home Handicap":       str(hh),
            "Home Handicap Odds":  str(r.get("home_handicap_odds") or 0),
            "Away Handicap":       str(ah),
            "Away Handicap Odds":  str(r.get("away_handicap_odds") or 0),
            "Home Handicap 2":     "0",
            "Home Handicap 2 Odds": "0",
            "Away Handicap 2":     "0",
            "Away Handicap 2 Odds": "0",
            "Over/Under Line":     str(ou),
            "Over Odds":           str(r.get("over_odds") or 0),
            "Under Odds":          str(r.get("under_odds") or 0),
            "Over/Under Line 2":   "0",
            "Over Odds 2":         "0",
            "Under Odds 2":        "0",
            "1X2 Home":            str(r.get("odds_1") or 0),
            "1X2 Draw":            str(r.get("odds_x") or 0),
            "1X2 Away":            str(r.get("odds_2") or 0),
        })
    return out


def _rows_to_csv_text(rows: list[dict[str, str]]) -> str:
    """Serialize parser-compatible rows back to CSV text for storage."""
    if not rows:
        return ""
    header = ",".join(rows[0].keys())
    lines = [header] + [",".join(str(v) for v in r.values()) for r in rows]
    return "\n".join(lines)


@router.get("/api/analyzer/from-match/{match_id:path}")
async def api_from_match(match_id: str, user: str = Depends(require_auth)):
    """Reconstruct analyzer state from a live/finished match in the DB."""
    match = get_match_by_id(match_id)
    if not match:
        raise HTTPException(404, "match not found")

    db_rows = get_odds_history_for_analyzer(match_id)
    if not db_rows:
        # Fall back to current match state as single data point
        db_rows = [{
            "home_handicap":       match.get("home_handicap"),
            "home_handicap_odds":  match.get("home_handicap_odds"),
            "away_handicap":       match.get("away_handicap"),
            "away_handicap_odds":  match.get("away_handicap_odds"),
            "ou_line":             match.get("ou_line"),
            "over_odds":           match.get("over_odds"),
            "under_odds":          match.get("under_odds"),
            "odds_1":              match.get("odds_1"),
            "odds_x":              match.get("odds_x"),
            "odds_2":              match.get("odds_2"),
            "minute":              match.get("minute"),
            "home_score":          match.get("home_score"),
            "away_score":          match.get("away_score"),
            "status":              match.get("status"),
        }]

    rows = _db_rows_to_csv_rows(db_rows)
    errs = P.validate(rows)
    result = P.compute(rows)

    # Synthesize a filename from match metadata
    from datetime import timezone as _tz
    import re as _re
    start = (match.get("start_time_utc") or "")[:16].replace("T", " ")
    league = _re.sub(r"[^\w\s-]", "", match.get("competition", "")).replace(" ", "_")[:40]
    home   = _re.sub(r"[^\w\s-]", "", match.get("home", "")).replace(" ", "_")[:20]
    away   = _re.sub(r"[^\w\s-]", "", match.get("away", "")).replace(" ", "_")[:20]
    filename = f"{start[:10].replace('-','')}_{start[11:13]}{start[14:16]}_{league}-{home}_vs_{away}.csv"

    meta = P.parse_fname(filename)
    # Prefer actual team names from DB
    meta["home"] = match.get("home", meta["home"])
    meta["away"] = match.get("away", meta["away"])
    meta["league"] = match.get("competition", meta["league"])

    csv_blob = _rows_to_csv_text(rows)

    return JSONResponse({
        "match_id":      match_id,
        "match_status":  match.get("status", ""),
        "filename":      filename,
        "meta":          meta,
        "csv_blob":      csv_blob,
        "rows_filtered": _filtered_rows(rows),
        "errors":        errs,
        "analysis":      result["milestones"],
        "real_fh":       result["real_fh"],
        "real_fa":       result["real_fa"],
        "ohh":           result["ohh"],
        "oah":           result["oah"],
        "favorite_home": result["ohh"] < 0 if result["ohh"] else False,
        "row_count":     len(rows),
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VISIBLE_COLS = (
    "Half", "Home Score", "Away Score",
    "Home Handicap", "Away Handicap", "Over/Under Line",
)


def _filtered_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Return only the 6 columns the GUI displays, plus a `goal` flag."""
    out = []
    ph = pa = 0
    for r in rows:
        try:
            h = int(P.to_f(r.get("Home Score", 0)))
            a = int(P.to_f(r.get("Away Score", 0)))
        except Exception:
            h = a = 0
        is_goal = (h >= ph and a >= pa and h + a > ph + pa)
        if is_goal:
            ph, pa = h, a
        out.append({
            "Half": r.get("Half", ""),
            "Home Score": r.get("Home Score", ""),
            "Away Score": r.get("Away Score", ""),
            "Home Handicap": r.get("Home Handicap", ""),
            "Away Handicap": r.get("Away Handicap", ""),
            "Over/Under Line": r.get("Over/Under Line", ""),
            "_goal": is_goal,
        })
    return out
