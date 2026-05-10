import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class AppState:
    # Collector lifecycle
    running: bool = False
    paused: bool = False

    # Counters
    loop_count: int = 0
    session_saved: int = 0
    session_skipped: int = 0
    error_count: int = 0

    # Last operation info
    last_fetch_at: Optional[str] = None
    last_fetch_ms: int = 0
    last_error: Optional[str] = None
    api_ok: bool = False

    # In-memory log ring (last 200 entries)
    logs: list = field(default_factory=list)

    # Async control handles — created inside event loop via setup()
    _pause_event: Optional[asyncio.Event] = field(default=None, repr=False)
    _force_event: Optional[asyncio.Event] = field(default=None, repr=False)
    _task: Optional[asyncio.Task] = field(default=None, repr=False)

    def setup(self) -> None:
        """Must be called from within a running async context (lifespan)."""
        self._pause_event = asyncio.Event()
        self._pause_event.set()   # not paused = event is set
        self._force_event = asyncio.Event()

    @property
    def pause_event(self) -> asyncio.Event:
        assert self._pause_event is not None, "AppState.setup() not called"
        return self._pause_event

    @property
    def force_event(self) -> asyncio.Event:
        assert self._force_event is not None, "AppState.setup() not called"
        return self._force_event

    def log(self, level: str, msg: str) -> None:
        self.logs.append({
            "t": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "l": level,
            "m": msg,
        })
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "paused": self.paused,
            "loop_count": self.loop_count,
            "session_saved": self.session_saved,
            "session_skipped": self.session_skipped,
            "error_count": self.error_count,
            "last_fetch_at": self.last_fetch_at,
            "last_fetch_ms": self.last_fetch_ms,
            "last_error": self.last_error,
            "api_ok": self.api_ok,
        }


# Module-level singleton shared across the whole app
app_state = AppState()
