"""Paid-API cost tracking: one row in public.api_usage per external call.

Shared spec (2026-10-12) across the TripAgent backends; this repo is
service='customerbe'. record() never raises and never blocks the caller: the
insert runs on a small background thread using the existing Supabase
service-role client (get_supabase_admin_client). With no Supabase configured
it does nothing. A missing table logs one warning and pauses inserts for 10
minutes. API_USAGE_TRACKING=off disables it; API_PRICES_JSON overrides any
rate. cost_usd is an ESTIMATE.

Anthropic: track_anthropic(client) wraps client.messages.create so every
call through that client records its usage automatically (operation =
the calling function, member attribution from usage_context)."""
import contextlib
import contextvars
import functools
import inspect
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

log = logging.getLogger(__name__)

SERVICE = "customerbe"

# Per-million-token LLM rates (in, out, cache write, cache read). Ordered:
# first match wins. Each entry: key, list of alternatives, each alternative a
# tuple of substrings that must ALL appear in the lower-cased model id.
_OLD_OPUS = {"in": 15, "out": 75, "cw": 18.75, "cr": 1.50}
_LLM_RATES: list[tuple[str, list[tuple[str, ...]], dict]] = [
    # claude-opus-4-20250514 is Opus 4.0 (no "-0" in its id).
    ("opus-4-0", [("opus-4-0",), ("opus-4-2025",)], _OLD_OPUS),
    ("opus-4-1", [("opus-4-1",)], _OLD_OPUS),
    ("claude-3-opus", [("claude-3-opus",)], _OLD_OPUS),
    ("opus", [("opus",)], {"in": 5, "out": 25, "cw": 6.25, "cr": 0.50}),
    ("sonnet", [("sonnet",)], {"in": 3, "out": 15, "cw": 3.75, "cr": 0.30}),
    ("haiku-3", [("haiku-3",), ("claude-3-haiku",), ("3-5-haiku",)], {"in": 0.80, "out": 4, "cw": 1.00, "cr": 0.08}),
    ("haiku", [("haiku",)], {"in": 1, "out": 5, "cw": 1.25, "cr": 0.10}),
    ("gemini-pro", [("gemini", "pro")], {"in": 1.25, "out": 10, "cw": 0, "cr": 0.31}),
    ("gemini-flash-lite", [("gemini", "flash-lite")], {"in": 0.10, "out": 0.40, "cw": 0, "cr": 0.025}),
    ("gemini", [("gemini",)], {"in": 0.30, "out": 2.50, "cw": 0, "cr": 0.075}),
]

# USD per unit, keyed "<provider>:<model key>" or "<provider>".
_UNIT_RATES: dict[str, float] = {
    "google_places:places.searchText": 0.032,
    "google_places:places.searchNearby": 0.032,
    "google_places:places.details": 0.017,
    "google_places:places.photo": 0.007,
    "google_places:place.textsearch": 0.032,
    "google_places:place.details": 0.017,
    "google_places:geocode": 0.005,
    "google_search": 0.005,
    "serper": 0.001,
    "zyte": 0.0016,
    "zyte:browserHtml": 0.0016,
    "zyte:httpResponseBody": 0.0002,
    "apify": 0.01,
    "resend": 0.0004,
    "whatsapp": 0.0014,
    "tripsure_hotel": 0,
    "tripsure_flight": 0,
    "onevasco": 0,
}

_LLM_PROVIDERS = {"anthropic", "gemini"}

_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("api_usage_ctx", default={})
_disabled_until = 0.0
_overrides_cache: tuple[Optional[str], dict] = (None, {})
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()

# Modules whose frames are never "the caller" when deriving operation.
_SKIP_MODULE_PREFIXES = (__name__, "contextlib", "asyncio", "functools", "anthropic", "httpx", "httpcore", "anyio")


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------

def _overrides() -> dict:
    global _overrides_cache
    raw = os.environ.get("API_PRICES_JSON") or None
    if raw == _overrides_cache[0]:
        return _overrides_cache[1]
    parsed: dict = {}
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                parsed = value
        except ValueError:
            log.warning("api_usage: API_PRICES_JSON is not valid JSON; using built-in rates")
    _overrides_cache = (raw, parsed)
    return parsed


def _llm_table() -> list[tuple[str, list[tuple[str, ...]], dict]]:
    overrides = _overrides()
    known = {key for key, _, _ in _LLM_RATES}
    extra = [(k, [(k.lower(),)], v) for k, v in overrides.items()
             if isinstance(v, dict) and ":" not in k and k not in known]
    table = [(key, alts, {**rates, **overrides[key]} if isinstance(overrides.get(key), dict) else rates)
             for key, alts, rates in _LLM_RATES]
    return extra + table


def _llm_rate(model: Optional[str]) -> Optional[dict]:
    name = (model or "").lower()
    if not name:
        return None
    for _, alts, rates in _llm_table():
        if any(all(part in name for part in alt) for alt in alts):
            return rates
    return None


def _unit_rate(provider: str, model: Optional[str]) -> Optional[float]:
    overrides = _overrides()
    for key in ([f"{provider}:{model}"] if model else []) + [provider]:
        if key in overrides and isinstance(overrides[key], (int, float)):
            return float(overrides[key])
        if key in _UNIT_RATES:
            return _UNIT_RATES[key]
    return None


def rates() -> dict:
    """The rate table in effect (built-ins merged with API_PRICES_JSON)."""
    out: dict = {key: dict(r) for key, _, r in _llm_table()}
    out.update(_UNIT_RATES)
    out.update({k: v for k, v in _overrides().items() if isinstance(v, (int, float))})
    return out


def cost_of(provider: str, model: Optional[str], input_tokens: Optional[int], output_tokens: Optional[int],
            cache_read_tokens: Optional[int], cache_write_tokens: Optional[int], units: float) -> tuple[float, bool]:
    """(cost_usd, priced). An LLM call with no usage (failed call) costs 0."""
    has_tokens = any(t for t in (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens))
    if provider in _LLM_PROVIDERS or has_tokens:
        r = _llm_rate(model)
        if r is None:
            return 0.0, False
        cost = ((input_tokens or 0) * float(r.get("in") or 0) + (output_tokens or 0) * float(r.get("out") or 0)
                + (cache_write_tokens or 0) * float(r.get("cw") or 0)
                + (cache_read_tokens or 0) * float(r.get("cr") or 0)) / 1_000_000
        return cost, True
    rate = _unit_rate(provider, model)
    if rate is None:
        return 0.0, False
    return rate * float(units or 0), True


# ---------------------------------------------------------------------------
# Attribution context
# ---------------------------------------------------------------------------

def _merged(member_code, member_id, operation, meta) -> dict:
    current = dict(_ctx.get())
    for k, v in (("member_code", member_code), ("member_id", member_id), ("operation", operation)):
        if v is not None:
            current[k] = str(v)
    if meta:
        current["meta"] = {**(current.get("meta") or {}), **{k: v for k, v in meta.items() if v is not None}}
    return current


@contextlib.contextmanager
def usage_context(member_code: Optional[str] = None, member_id: Optional[str] = None,
                  operation: Optional[str] = None, meta: Optional[dict] = None):
    """Attribute every record() inside the block. Works for sync and async code
    (contextvars follow the task). Inner values override outer ones; meta merges."""
    token = None
    try:
        token = _ctx.set(_merged(member_code, member_id, operation, meta))
    except Exception:  # noqa: BLE001
        pass
    try:
        yield
    finally:
        if token is not None:
            try:
                _ctx.reset(token)
            except Exception:  # noqa: BLE001
                pass


def set_context(member_code: Optional[str] = None, member_id: Optional[str] = None,
                operation: Optional[str] = None, meta: Optional[dict] = None) -> None:
    """Merge attribution into the current context without a with-block (e.g.
    once a member id is resolved mid-request). Scoped to the current task —
    an enclosing usage_context() restores the previous value on exit."""
    try:
        _ctx.set(_merged(member_code, member_id, operation, meta))
    except Exception:  # noqa: BLE001
        pass


def _caller_operation(extra_skip: tuple = (), skip_frames: int = 0) -> Optional[str]:
    try:
        frame = sys._getframe(1)
        skip = _SKIP_MODULE_PREFIXES + tuple(extra_skip)
        while frame is not None:
            mod = frame.f_globals.get("__name__", "") or ""
            if not any(mod == s or mod.startswith(s + ".") for s in skip):
                if skip_frames <= 0:
                    return f"{mod.rsplit('.', 1)[-1]}.{frame.f_code.co_name}"
                skip_frames -= 1
            frame = frame.f_back
    except Exception:  # noqa: BLE001
        pass
    return None


def caller_operation(skip_frames: int = 0) -> Optional[str]:
    """`module.function` of the code calling the function that calls this,
    skipping `skip_frames` further frames (e.g. a shared request helper)."""
    return _caller_operation(skip_frames=1 + skip_frames)


def jwt_subject(token: Optional[str]) -> Optional[str]:
    """The `sub` (Supabase auth uid) of a JWT, decoded locally WITHOUT
    verification — attribution only, never auth. No network call."""
    try:
        if not token or token.count(".") != 2:
            return None
        import base64

        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        sub = json.loads(base64.urlsafe_b64decode(payload.encode("ascii"))).get("sub")
        return str(sub) if sub else None
    except Exception:  # noqa: BLE001
        return None


def request_meta(request: Any = None, **extra: Any) -> dict:
    """Attribution meta for a chat request: the given ids (session_id,
    enquiry_id, ...) plus, when the site session cookie is present, the
    member's Supabase auth uid (site_members.auth_uid) decoded from it
    locally — no extra network call per message."""
    meta = {k: v for k, v in extra.items() if v}
    try:
        if request is not None:
            uid = jwt_subject(request.cookies.get("ta_access_token"))
            if uid:
                meta["auth_uid"] = uid
    except Exception:  # noqa: BLE001
        pass
    return meta


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    if (os.environ.get("API_USAGE_TRACKING") or "").strip().lower() in ("off", "false", "0", "no"):
        return False
    return time.time() >= _disabled_until


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="api_usage")
    return _executor


def _is_missing_table(exc: Exception) -> bool:
    text = f"{getattr(exc, 'code', '')} {exc}"
    return "PGRST205" in text or "42P01" in text or ("api_usage" in text and "does not exist" in text) \
        or ("Could not find the table" in text)


def _insert(row: dict) -> None:
    global _disabled_until
    try:
        from app.dependencies.supabase_client import get_supabase_admin_client

        client = get_supabase_admin_client()
        if client is None:
            return
        client.table("api_usage").insert(row).execute()
    except Exception as exc:  # noqa: BLE001 — accounting must never break a request
        if _is_missing_table(exc):
            if time.time() >= _disabled_until:
                log.warning("api_usage: table public.api_usage does not exist; usage tracking paused for 10 minutes")
            _disabled_until = time.time() + 600
        else:
            log.debug("api_usage insert failed: %s", exc)


def record(provider: str, *, operation: Optional[str] = None, model: Optional[str] = None,
           input_tokens: Optional[int] = None, output_tokens: Optional[int] = None,
           cache_read_tokens: Optional[int] = None, cache_write_tokens: Optional[int] = None,
           units: float = 1, member_code: Optional[str] = None, member_id: Optional[str] = None,
           ok: bool = True, meta: Optional[dict] = None, _skip: tuple = ()) -> None:
    """Record one paid API call. Never raises, never waits on the database."""
    try:
        if not _enabled():
            return
        ctx = _ctx.get()
        operation = operation or ctx.get("operation") or _caller_operation(_skip)
        member_code = member_code if member_code is not None else ctx.get("member_code")
        member_id = member_id if member_id is not None else ctx.get("member_id")
        cost, priced = cost_of(provider, model, input_tokens, output_tokens, cache_read_tokens,
                               cache_write_tokens, units)
        merged_meta = {**(ctx.get("meta") or {}), **(meta or {})}
        if not priced:
            merged_meta["unpriced"] = True

        def _int(v):
            return int(v) if v is not None else None

        row = {
            "service": SERVICE,
            "provider": provider,
            "operation": operation,
            "model": model,
            "input_tokens": _int(input_tokens),
            "output_tokens": _int(output_tokens),
            "cache_read_tokens": _int(cache_read_tokens),
            "cache_write_tokens": _int(cache_write_tokens),
            "units": float(units if units is not None else 1),
            "cost_usd": round(cost, 6),
            "member_code": member_code,
            "member_id": str(member_id) if member_id is not None else None,
            "ok": bool(ok),
            "meta": json.loads(json.dumps(merged_meta, default=str)) if merged_meta else None,
        }
        _get_executor().submit(_insert, row)
    except Exception as exc:  # noqa: BLE001
        log.debug("api_usage record failed: %s", exc)


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

def record_anthropic(response: Any, *, model: Optional[str] = None, operation: Optional[str] = None,
                     ok: bool = True, meta: Optional[dict] = None) -> None:
    """Record one Anthropic Message (uses response.usage and response.model)."""
    try:
        u = getattr(response, "usage", None)
        record("anthropic", operation=operation, model=getattr(response, "model", None) or model,
               input_tokens=getattr(u, "input_tokens", None) if u is not None else None,
               output_tokens=getattr(u, "output_tokens", None) if u is not None else None,
               cache_read_tokens=getattr(u, "cache_read_input_tokens", None) if u is not None else None,
               cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) if u is not None else None,
               ok=ok, meta=meta)
    except Exception:  # noqa: BLE001
        pass


def _record_anthropic_error(exc: Exception, model: Optional[str], operation: Optional[str]) -> None:
    meta = {"error": type(exc).__name__}
    status = getattr(exc, "status_code", None)
    if status is not None:
        meta["status"] = status
    record("anthropic", operation=operation, model=model, ok=False, meta=meta)


def track_anthropic(client: Any, *, skip_modules: tuple = ()) -> Any:
    """Wrap client.messages.create (sync or async client) so every call
    records its usage. Idempotent; returns the client. Streaming calls
    (stream=True) are recorded as one call without token counts.
    skip_modules: extra module names to skip when deriving `operation`
    (e.g. a gateway module that only forwards calls)."""
    try:
        messages = client.messages
        original = messages.create
        if getattr(original, "_api_usage_wrapped", False):
            return client
        skip = tuple(skip_modules)

        # anthropic's create() is a plain function returning a coroutine on the
        # async client, so the awaitable case is handled on the result.
        @functools.wraps(original)
        def create(*args, **kwargs):
            operation = _ctx.get().get("operation") or _caller_operation(skip)
            model = kwargs.get("model")
            try:
                result = original(*args, **kwargs)
            except Exception as exc:
                _record_anthropic_error(exc, model, operation)
                raise
            if inspect.isawaitable(result):
                async def _awaited():
                    try:
                        response = await result
                    except Exception as exc:
                        _record_anthropic_error(exc, model, operation)
                        raise
                    _after(response, model, operation, kwargs)
                    return response
                return _awaited()
            _after(result, model, operation, kwargs)
            return result

        create._api_usage_wrapped = True  # type: ignore[attr-defined]
        messages.create = create
    except Exception as exc:  # noqa: BLE001 — tracking must never break client construction
        log.debug("api_usage: could not wrap Anthropic client: %s", exc)
    return client


def _after(response: Any, model: Optional[str], operation: Optional[str], kwargs: dict) -> None:
    try:
        if kwargs.get("stream") and getattr(response, "usage", None) is None:
            record("anthropic", operation=operation, model=model, meta={"stream": True, "usage_unknown": True})
        else:
            record_anthropic(response, model=model, operation=operation)
    except Exception:  # noqa: BLE001
        pass
