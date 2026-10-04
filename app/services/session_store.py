from typing import Optional
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
        self.pending_action: Optional[dict] = None
        # Set once ai_router.py has fired summarize_conversation.py +
        # chat_enquiry_service.py for this session, so a repeated hand-off
        # signal (the member says "talk to a human" twice, or keeps
        # chatting after Aanya's sign-off) never creates a second enquiry
        # row for the same conversation.
        self.enquiry_created = False
        # The enquiries.id row created above (None until enquiry_created is
        # True). A substantive post-handoff message (aanya_flow.py's
        # _post_handoff_reply) updates THIS row's detail rather than
        # creating a second one — see ai_router.py's handling of
        # FlowResult.enquiry_update.
        self.enquiry_id: Optional[str] = None
        # Drives the fixed 6-turn flow (see aanya_flow.py) — which question
        # comes next and what's already been answered, so a later turn never
        # re-asks a field the member already gave. "turn0" is the initial
        # state, awaiting the member's free-text opener; "done" once the
        # flow has closed and handed off. `fields` accumulates raw answers
        # (destination, budget_choice, etc.) — free-form values, not
        # normalized against enquiries.detail's schema (summarize_
        # conversation.py still does that extraction from `history` itself,
        # unchanged).
        self.flow_step = "turn0"
        self.fields: dict = {}
        # Loop safety net (aanya_flow.py's `advance`) — tracks consecutive
        # "unresolved" rejections (a real, substantive message that still
        # doesn't answer the pending question's shape) FOR THE SAME STEP.
        # Reset to 0/None the moment anything else happens (a real match,
        # a genuine tangent, a step change) — this is purely about "has
        # the member been stuck on this exact question back-to-back,"
        # never a cross-step or cross-session count.
        self.unresolved_streak = 0
        self.unresolved_streak_step: Optional[str] = None
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
