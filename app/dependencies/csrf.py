from fastapi import Header, HTTPException


def require_csrf_header(x_requested_with: str = Header(default="", alias="X-Requested-With")) -> None:
    """CSRF mitigation for cookie-authenticated, state-changing endpoints.
    A cross-site page can trigger a 'simple' request (no custom headers,
    form-encoded/no body) and the browser will attach this app's session
    cookie automatically — CORS doesn't stop that request from being sent,
    only from the attacker's JS reading the response. Requiring a custom
    header here forces every such request to become 'non-simple', which
    DOES require a CORS preflight — and main.py's origin allowlist blocks
    that preflight for any untrusted origin, so the browser never sends the
    real request at all. A trusted frontend sets this header trivially; a
    forged cross-site request cannot, by construction of the browser's own
    CORS behavior — this isn't a secret, it doesn't need to be."""
    if x_requested_with != "XMLHttpRequest":
        raise HTTPException(status_code=403, detail="missing_csrf_header")