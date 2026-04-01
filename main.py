import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

app = FastAPI(title="Nametag Simulator", version="1.0.0")

SIMULATOR_SECRET = os.getenv("SIMULATOR_SECRET", "change-me-in-render")
SIMULATOR_BASE_URL = os.getenv("SIMULATOR_BASE_URL", "http://localhost:8000")
WEBHOOK_TARGET_URL = os.getenv("WEBHOOK_TARGET_URL", "")
AUTO_COMPLETE_SECONDS = int(os.getenv("AUTO_COMPLETE_SECONDS", "0"))

requests_store: Dict[str, dict] = {}


class CreateRequestBody(BaseModel):
    env: str = Field(default="demo-env")
    claims: List[str] = Field(default_factory=lambda: ["name", "email"])
    label: Optional[str] = None
    subject_hint: Optional[str] = None
    simulator_result: str = Field(default="success", description="success or fail")
    identity_match: bool = True
    webhook_target_url: Optional[str] = None


class CompleteRequestBody(BaseModel):
    result: Optional[str] = None
    webhook_target_url: Optional[str] = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compute_signature(raw_body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def build_properties_for_request(rec: dict) -> List[dict]:
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    properties = []
    success = rec["result"] == "success"
    values = {
        "name": "Jane Doe" if rec["identity_match"] else "Unmatched Person",
        "email": "jane@example.com" if rec["identity_match"] else "not-a-match@example.com",
        "phone_number": "+15551234567",
        "birth_date": "1990-01-15",
        "account": "acct-001",
    }
    for claim in rec["claims"]:
        properties.append(
            {
                "scope": claim,
                "value": values.get(claim, f"demo-{claim}-value"),
                "status": 200 if success else 403,
                "expires": expires,
            }
        )
    return properties


@app.get("/")
def root():
    return {
        "service": "nametag-simulator",
        "status": "ok",
        "time": utc_now_iso(),
    }


@app.get("/health")
def health():
    return {"ok": True, "time": utc_now_iso()}


@app.post("/api/requests")
def create_request(body: CreateRequestBody):
    request_id = f"req_{secrets.token_hex(4)}"
    subject = body.subject_hint or f"demo-user-{secrets.token_hex(3)}@demo.nametag.co"
    rec = {
        "id": request_id,
        "env": body.env,
        "link": f"{SIMULATOR_BASE_URL}/verify/{request_id}",
        "status": 100,
        "label": body.label,
        "claims": body.claims,
        "subject": subject,
        "result": body.simulator_result,
        "identity_match": body.identity_match,
        "created_at": utc_now_iso(),
        "webhook_sent": False,
        "webhook_target_url": body.webhook_target_url or WEBHOOK_TARGET_URL,
    }
    requests_store[request_id] = rec

    if AUTO_COMPLETE_SECONDS > 0 and rec["webhook_target_url"]:
        threading.Thread(target=_auto_complete_later, args=(request_id, AUTO_COMPLETE_SECONDS), daemon=True).start()

    return {
        "id": rec["id"],
        "env": rec["env"],
        "link": rec["link"],
        "status": rec["status"],
        "label": rec["label"],
        "claims": rec["claims"],
        "created_at": rec["created_at"],
        "subject": rec["subject"],
    }


@app.get("/api/requests/{request_id}")
def get_request(request_id: str):
    rec = requests_store.get(request_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Request not found")
    return rec


@app.get("/verify/{request_id}")
def verify_page(request_id: str):
    rec = requests_store.get(request_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Request not found")
    return {
        "message": "This is a fake Nametag verification link for demo purposes.",
        "request_id": request_id,
        "subject": rec["subject"],
        "next_step": f"POST {SIMULATOR_BASE_URL}/simulator/complete/{request_id} to simulate completion.",
    }


@app.post("/simulator/complete/{request_id}")
def complete_request(request_id: str, body: CompleteRequestBody):
    rec = requests_store.get(request_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Request not found")

    if body.result:
        rec["result"] = body.result
    if body.webhook_target_url:
        rec["webhook_target_url"] = body.webhook_target_url

    if not rec["webhook_target_url"]:
        raise HTTPException(status_code=400, detail="No webhook target configured on the request or service")

    payload = {
        "event_type": "share",
        "subject": rec["subject"],
        "request": rec["id"],
        "scopes": rec["claims"],
        "label": rec["label"],
        "result": rec["result"],
        "sent_at": utc_now_iso(),
    }

    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = compute_signature(raw, SIMULATOR_SECRET)
    webhook_id = f"wh_{secrets.token_hex(4)}"

    try:
        response = requests.post(
            rec["webhook_target_url"],
            data=raw,
            headers={
                "Content-Type": "application/json",
                "X-Nametag-ID": webhook_id,
                "X-Nametag-Signature": signature,
            },
            timeout=20,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Webhook send failed: {exc}")

    rec["webhook_sent"] = True
    rec["status"] = 200 if rec["result"] == "success" else 403
    rec["completed_at"] = utc_now_iso()

    return {
        "ok": True,
        "request_id": request_id,
        "webhook_target_url": rec["webhook_target_url"],
        "webhook_status_code": response.status_code,
        "webhook_response_text": response.text,
        "signature_used": signature,
        "webhook_id": webhook_id,
        "payload": payload,
    }


@app.get("/people/{subject}/properties/{claims}")
def get_properties(subject: str, claims: str):
    claim_list = [item.strip() for item in claims.split(",") if item.strip()]
    matching = None
    for rec in requests_store.values():
        if rec["subject"] == subject:
            matching = rec
            break
    if not matching:
        raise HTTPException(status_code=404, detail="Subject not found")

    filtered_claims = [c for c in matching["claims"] if c in claim_list] or claim_list
    rec_copy = dict(matching)
    rec_copy["claims"] = filtered_claims
    properties = build_properties_for_request(rec_copy)

    return {
        "subject": subject,
        "requests": [
            {
                "id": matching["id"],
                "status": matching["status"],
                "claims": filtered_claims,
                "label": matching["label"],
            }
        ],
        "properties": properties,
    }


@app.get("/simulator/requests")
def list_requests():
    return {"count": len(requests_store), "requests": list(requests_store.values())}


@app.get("/simulator/signature")
def signature_helper(payload: str = Query(..., description="Exact JSON string to sign")):
    raw = payload.encode("utf-8")
    return {"signature": compute_signature(raw, SIMULATOR_SECRET)}


def _auto_complete_later(request_id: str, seconds: int):
    time.sleep(seconds)
    rec = requests_store.get(request_id)
    if not rec or not rec.get("webhook_target_url") or rec.get("webhook_sent"):
        return
    try:
        complete_request(request_id, CompleteRequestBody())
    except Exception:
        return
