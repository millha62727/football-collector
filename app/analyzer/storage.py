"""CRUD helpers for analyzer_sessions table.

Sessions are owned by `username` (from JWT). Each session bundles the raw CSV
text plus all user inputs (overrides, notes, prediction, cached analysis) so
the page can be fully reconstructed without the original file on disk.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import psycopg2.extras

from ..database import _connect  # type: ignore


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner": row["owner"],
        "filename": row["filename"],
        "meta": row["meta"] or {},
        "csv_blob": row["csv_blob"],
        "overrides": {int(k): v for k, v in (row["overrides"] or {}).items()},
        "notes": row["notes"] or [],
        "note_text": row["note_text"] or "",
        "pred_fh": row["pred_fh"],
        "pred_fa": row["pred_fa"],
        "analysis": row["analysis"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def save_session(owner: str, payload: dict[str, Any]) -> int:
    """Insert a new session, or update if `payload['id']` is present.

    `payload` keys: filename, meta, csv_blob, overrides, notes, note_text,
    pred_fh, pred_fa, analysis. Returns the session id.
    """
    sid = payload.get("id")
    overrides = payload.get("overrides") or {}
    overrides = {str(k): v for k, v in overrides.items()}
    notes = payload.get("notes") or []
    meta = payload.get("meta") or {}
    analysis = payload.get("analysis")
    note_text = payload.get("note_text") or ""

    with _connect() as conn:
        cur = conn.cursor()
        if sid:
            cur.execute(
                """
                UPDATE analyzer_sessions
                SET filename = %s,
                    meta = %s::jsonb,
                    csv_blob = %s,
                    overrides = %s::jsonb,
                    notes = %s::jsonb,
                    note_text = %s,
                    pred_fh = %s,
                    pred_fa = %s,
                    analysis = %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s AND owner = %s
                RETURNING id
                """,
                (
                    payload.get("filename", ""),
                    json.dumps(meta, ensure_ascii=False),
                    payload.get("csv_blob", ""),
                    json.dumps(overrides),
                    json.dumps(notes, ensure_ascii=False),
                    note_text,
                    payload.get("pred_fh"),
                    payload.get("pred_fa"),
                    json.dumps(analysis) if analysis is not None else None,
                    int(sid),
                    owner,
                ),
            )
            row = cur.fetchone()
            if not row:
                raise PermissionError("session not found or not owned")
            return int(row[0])
        else:
            cur.execute(
                """
                INSERT INTO analyzer_sessions
                  (owner, filename, meta, csv_blob, overrides, notes, note_text,
                   pred_fh, pred_fa, analysis)
                VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    owner,
                    payload.get("filename", ""),
                    json.dumps(meta, ensure_ascii=False),
                    payload.get("csv_blob", ""),
                    json.dumps(overrides),
                    json.dumps(notes, ensure_ascii=False),
                    note_text,
                    payload.get("pred_fh"),
                    payload.get("pred_fa"),
                    json.dumps(analysis) if analysis is not None else None,
                ),
            )
            return int(cur.fetchone()[0])


def load_session(owner: str, session_id: int) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM analyzer_sessions WHERE id = %s AND owner = %s",
            (session_id, owner),
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def list_sessions(owner: str, limit: int = 200) -> list[dict[str, Any]]:
    """Return brief metadata for the user's sessions, newest first."""
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT id, filename, meta, pred_fh, pred_fa, created_at, updated_at
            FROM analyzer_sessions
            WHERE owner = %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (owner, limit),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r["id"],
            "filename": r["filename"],
            "meta": r["meta"] or {},
            "pred_fh": r["pred_fh"],
            "pred_fa": r["pred_fa"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


def delete_session(owner: str, session_id: int) -> bool:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM analyzer_sessions WHERE id = %s AND owner = %s",
            (session_id, owner),
        )
        return cur.rowcount > 0
