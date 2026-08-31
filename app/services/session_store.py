"""In-memory, per-chat-session conversation state for Aanya's tool-calling flow.

Keyed by an opaque `session_id` the frontend generates and sends on every
request (see js/assistant.js and ai_router.py) — see the session-identifier
writeup in the accompanying summary for why this, not a new auth system, is
the right key: there is no existing anonymous visitor id anywhere in this
codebase (only a post-sign-in `ta_session.memberId` in localStorage), so
signed-out members get a lightweight per-tab id the frontend mints itself,
the same way every router here already mints its own `trace_id`.

Deliberately in-process memory, not a database or Redis: this is the minimum
needed to make multi-turn tool-calling (and the booking-confirmation gate)
work at all, without a new piece of infrastructure. Known limitations:
state is lost on process restart and isn't shared across multiple worker
processes/instances. Fine for a single-instance deployment; revisit
(Redis/Supabase) if this backend ever runs with more than one worker.
"""

import logging
import threading
import time

_log = logging.getLogger("session_store")

_MAX_HISTORY_TURNS = 16
_MAX_SESSIONS = 2000
_SESSION_TTL_SECONDS = 60 * 60 * 2  # evict a session after 2 hours of inactivity


class SessionState:
    def __init__(self):
        self.history: list[dict] = []
        # The booking/visa-application draft awaiting the member's explicit
        # "yes" — see concierge_tools.py. None when nothing is pending.
        self.pending_action: dict | None = None
        self.last_seen = time.time()

    def add_turn(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        if len(self.history) > _MAX_HISTORY_TURNS:
            self.history = self.history[-_MAX_HISTORY_TURNS:]
        self.last_seen = time.time()


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> SessionState:
        with self._lock:
            self._evict_stale_locked()
            state = self._sessions.get(session_id)
            if state is None:
                state = SessionState()
                self._sessions[session_id] = state
            else:
                state.last_seen = time.time()
            return state

    def _evict_stale_locked(self) -> None:
        now = time.time()
        expired = [sid for sid, s in self._sessions.items() if now - s.last_seen > _SESSION_TTL_SECONDS]
        for sid in expired:
            del self._sessions[sid]
        if len(self._sessions) > _MAX_SESSIONS:
            oldest_first = sorted(self._sessions.items(), key=lambda kv: kv[1].last_seen)
            for sid, _ in oldest_first[: len(self._sessions) - _MAX_SESSIONS]:
                del self._sessions[sid]


_store = SessionStore()


def get_session_store() -> SessionStore:
    return _store
