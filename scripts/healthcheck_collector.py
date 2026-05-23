#!/usr/bin/env python3
"""Docker healthcheck for the collector container.

Exit 0  → healthy (DB reachable AND collector heartbeat is fresh)
Exit 1  → unhealthy (DB unreachable, no heartbeat, or heartbeat too old)

Designed to be cheap: one SELECT 1 against Postgres + one SELECT against the
`collector_state` table. Runs from inside the container via `python -m`.

Threshold: a healthy collector writes `last_heartbeat` on every loop iteration
(at minimum every CMD_POLL_S=3s while paused, every POLL_INTERVAL while
running). We allow up to 3 × POLL_INTERVAL of slack to absorb network jitter
on the upstream API call.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone


def _max_age_seconds() -> int:
    """Heartbeat must be newer than this many seconds, default 3 × POLL_INTERVAL."""
    explicit = os.getenv("COLLECTOR_HEARTBEAT_MAX_AGE_S")
    if explicit:
        try:
            return max(10, int(explicit))
        except ValueError:
            pass
    poll = int(os.getenv("POLL_INTERVAL", "30"))
    return max(60, poll * 3)


def main() -> int:
    # Import lazily so a broken DB pool import (e.g. psycopg2 missing) still
    # surfaces as exit-1 instead of an opaque traceback that obscures the cause.
    try:
        from app.database import db_ping, get_collector_state
    except Exception as exc:
        print(f"healthcheck: import failed: {exc!r}", file=sys.stderr)
        return 1

    if not db_ping():
        print("healthcheck: db_ping failed", file=sys.stderr)
        return 1

    try:
        state = get_collector_state()
    except Exception as exc:
        print(f"healthcheck: get_collector_state failed: {exc!r}", file=sys.stderr)
        return 1

    hb = state.get("last_heartbeat") or ""
    if not hb:
        print("healthcheck: no last_heartbeat yet", file=sys.stderr)
        return 1

    try:
        ts = datetime.fromisoformat(hb)
    except ValueError:
        print(f"healthcheck: bad last_heartbeat format: {hb!r}", file=sys.stderr)
        return 1

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    threshold = _max_age_seconds()
    if age > threshold:
        print(
            f"healthcheck: heartbeat stale (age={age:.0f}s > {threshold}s)",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
