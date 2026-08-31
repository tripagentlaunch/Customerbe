import logging
import re
import uuid

from fastapi import APIRouter, HTTPException

from app.models.ai_models import ConciergeChatRequest, ConciergeChatResponse, RetrievedSource
from app.services.claude_client import ClaudeClient
from app.services.session_store import get_session_store
from app.services.vector_store import get_vector_store

router = APIRouter(prefix="/ai", tags=["ai"])
_log = logging.getLogger("ai_concierge")

# Below this Pinecone cosine-similarity score, a match is treated as noise —
# the member's question gets an honest "not in the corpus" answer instead of
# Claude being handed a barely-related chunk to ground against.
_MIN_RELEVANCE_SCORE = 0.5
_TOP_K = 6

# Deterministic fast-path: an explicit ask for a human bypasses Claude and the
# tool-use loop entirely. The handoff must never depend on the model being
# available, in-budget, or "choosing" to comply — it's the CORE KRA's always-
# reachable, frictionless click, not a feature the LLM decides to offer. It's
# also always reachable in the React UI's own persistent top-bar button
# (concierge-chat/src/App.tsx), which posts this same phrase.
_HUMAN_HANDOFF_RE = re.compile(
    r"\b(talk to (a |my )?(human|person|advisor|someone)|speak (to|with) (a |my )?(human|person|advisor|someone)|"
    r"connect me (to|with)( a| my)?( human| advisor| person)?|human (please|pls)|real person|someone real)\b",
    re.IGNORECASE,
)

_claude: ClaudeClient | None = None


def _get_claude() -> ClaudeClient:
    global _claude
    if _claude is None:
        _claude = ClaudeClient()
    return _claude


@router.post("/concierge/chat", response_model=ConciergeChatResponse)
async def concierge_chat(payload: ConciergeChatRequest):
    session_id = payload.session_id or f"anon-{uuid.uuid4()}"
    session = get_session_store().get(session_id)

    if _HUMAN_HANDOFF_RE.search(payload.query):
        # Structured, no URL — rendered as an in-chat card by the React
        # frontend, never a navigating link. See concierge_tools.py's
        # _build_handoff for the same shape used by confirmed bookings.
        return ConciergeChatResponse(
            intro="Of course — connecting you to your TripAgent advisor now.",
            bubbles=["Of course — connecting you to your TripAgent advisor now."],
            handoff={"label": "Continue with your advisor", "kind": "advisor_prompt", "summary": None},
            grounded=False,
        )

    try:
        vstore = get_vector_store()
        matches = vstore.query(payload.query, top_k=_TOP_K)
    except RuntimeError as exc:
        # Missing PINECONE_API_KEY etc. — clean 503 so the frontend can show
        # its own in-chat "couldn't reach the concierge" fallback card.
        _log.error("[AI_CONCIERGE] vector store unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - retrieval failing must not take the endpoint down
        _log.error("[AI_CONCIERGE] retrieval failed: %s: %s", type(exc).__name__, exc)
        matches = []

    relevant = [m for m in matches if (m.get("score") or 0) >= _MIN_RELEVANCE_SCORE]
    retrieved_context = [{"metadata": m.get("metadata") or {}} for m in relevant]

    try:
        claude = _get_claude()
    except RuntimeError as exc:
        _log.error("[AI_CONCIERGE] claude client unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        result = await claude.ask(message=payload.query, session=session, retrieved_context=retrieved_context)
    except Exception as exc:  # noqa: BLE001 - upstream Claude failure -> clean fallback, not a 500 leak
        _log.error("[AI_CONCIERGE] claude call failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail="assistant temporarily unavailable")

    # Session history stores only plain turns — never the RAG-context wrapper
    # or the intra-turn tool_use/tool_result scratch work (see claude_client.py).
    session.add_turn("user", payload.query)
    session.add_turn("assistant", result["text"])

    sources = [
        RetrievedSource(
            source_type=(m.get("metadata") or {}).get("source_type", "entry"),
            city_slug=(m.get("metadata") or {}).get("city_slug"),
            city_name=(m.get("metadata") or {}).get("city_name"),
            score=m.get("score") or 0.0,
        )
        for m in relevant
    ]

    # No default "talk to your advisor" handoff attached to every reply
    # anymore — that was the old per-message inline link. The React UI has a
    # persistent top-bar button for that instead (App.tsx); a handoff CARD
    # here means one specific thing: a booking/visa/hotel request was just
    # confirmed (or, above, an explicit "talk to a human" ask).
    return ConciergeChatResponse(
        intro=result["text"],
        bubbles=result.get("bubbles") or [result["text"]],
        cards=result.get("cards") or [],
        handoff=result.get("handoff"),
        demo_confirmation=result.get("demo_confirmation"),
        grounded=result["grounded"],
        sources=sources,
        tools_called=result.get("tools_called") or [],
    )
