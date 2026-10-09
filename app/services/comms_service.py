import base64
import hashlib
import hmac
import time
import httpx
from typing import Optional
from app.config import settings


def verify_sinch_signature(raw_body: bytes, headers: dict) -> bool:
    secret = settings.sinch_webhook_secret
    if not secret:
        return True  # dev/simulated mode

    sig       = headers.get("x-sinch-webhook-signature", "")
    nonce     = headers.get("x-sinch-webhook-signature-nonce", "")
    timestamp = headers.get("x-sinch-webhook-signature-timestamp", "")

    if not all([sig, nonce, timestamp]):
        return False

    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False

    signed_data = raw_body + b"." + nonce.encode() + b"." + timestamp.encode()
    expected = base64.b64encode(
        hmac.new(secret.encode(), signed_data, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(sig, expected)


def normalize_inbound(payload: dict) -> Optional[dict]:
    messages = payload.get("messages")
    if not messages:
        return None
    if isinstance(messages, dict):
        messages = [messages]

    contacts = payload.get("contacts", [])
    contact_name = contacts[0]["profile"]["name"] if contacts else None
    msg = messages[0]
    msg_type = msg.get("type", "text")
    wa_id = msg.get("from", "")
    text = None
    media = None

    if msg_type == "text":
        text = (msg.get("text") or {}).get("body")
    elif msg_type in ("image", "document", "voice", "audio"):
        media = msg.get(msg_type)
    elif msg_type == "button":
        btn = msg.get("button", {})
        text = btn.get("text") if isinstance(btn, dict) else btn
    elif msg_type == "interactive":
        interactive = msg.get("interactive", {})
        if interactive.get("type") == "button_reply":
            text = interactive["button_reply"].get("title")
        elif interactive.get("type") == "list_reply":
            text = interactive["list_reply"].get("title")

    return {
        "wa_id": wa_id,
        "wa_message_id": msg.get("id"),
        "msg_type": msg_type,
        "text": text,
        "media": media,
        "contact_name": contact_name,
    }


def process_inbound(msg: dict) -> dict:
    wa_id = msg.get("wa_id", "")
    text = (msg.get("text") or "").strip().lower()

    # Basic routing
    if "visa" in text and "status" in text:
        route = "visa-status"
        reply = "Please check your visa status in the TripAgent app."
    elif "visa" in text:
        route = "visa-question"
        reply = "For visa queries, an advisor will follow up shortly."
    else:
        route = "concierge"
        reply = "Thanks for your message — an advisor will get back to you shortly."

    print(f"[CUSTOMERBE_COMMS] inbound from {wa_id} → routed to {route}")
    return {"ok": True, "routed_to": route, "reply": reply}


def send_whatsapp(to_number: str, body: str) -> dict:
    if not settings.sinch_api_password:
        print(f"[CUSTOMERBE_COMMS] simulated send to {to_number}: {body}")
        return {"status": "simulated", "reason": "sinch_password_not_configured"}

    try:
        resp = httpx.post(
            f"{settings.sinch_base_url}/messages",
            json={
                "to": to_number,
                "from": settings.sinch_sender_number,
                "type": "text",
                "text": {"body": body},
            },
            auth=(settings.sinch_api_username, settings.sinch_api_password),
            timeout=10,
        )
        resp.raise_for_status()
        return {"status": "sent", "to": to_number}
    except Exception as exc:
        print(f"[CUSTOMERBE_COMMS] Sinch send failed for {to_number}: {exc}")
        return {"status": "failed", "error": str(exc)}
