from fastapi import APIRouter, Request
from app.services import comms_service

router = APIRouter(prefix="/comms", tags=["comms"])


@router.post("/wa/inbound")
async def post_inbound(request: Request):
    raw_body = await request.body()
    headers = dict(request.headers)

    if not comms_service.verify_sinch_signature(raw_body, headers):
        print("[CUSTOMERBE_COMMS] Sinch signature verification failed")
        return {"ok": False, "rejected": "bad_signature"}

    try:
        payload = await request.json()
    except ValueError:
        return {"ok": False, "rejected": "invalid_json"}

    msg = comms_service.normalize_inbound(payload)
    if not msg:
        return {"ok": True, "ignored": "no_message"}

    try:
        return comms_service.process_inbound(msg)
    except Exception as exc:
        print(f"[CUSTOMERBE_COMMS] process_inbound failed: {exc}")
        return {"ok": False, "error": "unexpected"}


@router.post("/wa/send")
async def post_send(request: Request):
    body = await request.json()
    to_number = body.get("to")
    message = body.get("message")
    if not to_number or not message:
        return {"ok": False, "error": "to and message are required"}
    result = comms_service.send_whatsapp(to_number, message)
    return {"ok": True, **result}
