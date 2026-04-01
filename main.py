from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import os
import uuid
import hmac
import hashlib
import json
import requests
from datetime import datetime, timedelta, timezone

app = FastAPI(title="Nametag Simulator", version="1.2.0")

SIMULATOR_SECRET = os.getenv("SIMULATOR_SECRET", "change-me")
SIMULATOR_BASE_URL = os.getenv("SIMULATOR_BASE_URL", "http://localhost:8000")
WEBHOOK_TARGET_URL = os.getenv("WEBHOOK_TARGET_URL", "")
AUTO_COMPLETE_SECONDS = int(os.getenv("AUTO_COMPLETE_SECONDS", "0"))

# New demo-default env vars
DEFAULT_SIMULATOR_RESULT = os.getenv("DEFAULT_SIMULATOR_RESULT", "success").strip().lower()
DEFAULT_IDENTITY_MATCH = os.getenv("DEFAULT_IDENTITY_MATCH", "true").strip().lower() in ("true", "1", "yes", "y", "on")

# In-memory store
REQUESTS: Dict[str, Dict[str, Any]] = {}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sign_payload(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


def normalize_result(value: Optional[str]) -> str:
    if not value:
        return DEFAULT_SIMULATOR_RESULT
    value = value.strip().lower()
    return value if value in ("success", "fail") else DEFAULT_SIMULATOR_RESULT


def normalize_identity_match(value: Optional[bool]) -> bool:
    if value is None:
        return DEFAULT_IDENTITY_MATCH
    return bool(value)


def build_properties_for_request(record: Dict[str, Any], requested_claims: List[str]) -> List[Dict[str, Any]]:
    identity_match = record.get("identity_match", True)
    simulator_result = record.get("simulator_result", "success")

    if identity_match:
        name_value = "Jane Doe"
        email_value = "jane@example.com"
        phone_value = "+1-615-555-0100"
        address_value = "123 Main St, Nashville, TN 37201"
    else:
        name_value = "Janet Wrong"
        email_value = "wrongperson@example.com"
        phone_value = "+1-615-555-9999"
        address_value = "999 Wrong Ave, Nowhere, ZZ 00000"

    expires = iso_z(utc_now() + timedelta(days=7))
    props = []

    for claim in requested_claims:
        claim = claim.strip()

        if claim == "name":
            value = name_value
        elif claim == "email":
            value = email_value
        elif claim == "phone_number":
            value = phone_value
        elif claim == "address":
            value = address_value
        else:
            value = f"demo-{claim}-value"

        status = 200 if simulator_result == "success" else 409

        props.append({
            "scope": claim,
            "value": value,
            "status": status,
            "expires": expires
        })

    return props


def send_webhook(record: Dict[str, Any], override_result: Optional[str] = None) -> Dict[str, Any]:
    webhook_target = record.get("webhook_target_url") or WEBHOOK_TARGET_URL
    if not webhook_target:
        raise HTTPException(status_code=400, detail="No webhook target URL configured")

    result_value = normalize_result(override_result or record.get("simulator_result"))

    payload = {
        "event_type": "share",
        "subject": record["subject"],
        "request": record["id"],
        "scopes": record["claims"],
        "label": record.get("label"),
        "result": result_value,
        "sent_at": iso_z(utc_now())
    }

    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = sign_payload(payload_bytes, SIMULATOR_SECRET)

    headers = {
        "Content-Type": "application/json",
        "X-Nametag-ID": f"wh_{uuid.uuid4().hex[:8]}",
        "X-Nametag-Signature": signature,
    }

    response = requests.post(webhook_target, data=payload_bytes, headers=headers, timeout=20)

    record["webhook_sent"] = True
    record["last_webhook_payload"] = payload
    record["last_webhook_signature"] = signature
    record["last_webhook_status_code"] = response.status_code
    record["last_webhook_sent_at"] = iso_z(utc_now())
    record["simulator_result"] = result_value

    return {
        "ok": True,
        "webhook_target_url": webhook_target,
        "webhook_status_code": response.status_code,
        "signature_used": signature,
        "payload": payload
    }


class CreateRequestBody(BaseModel):
    env: str = "demo-env"
    claims: List[str] = Field(default_factory=lambda: ["name", "email"])
    label: Optional[str] = None
    subject_hint: Optional[str] = "mac-demo@demo.nametag.co"
    simulator_result: Optional[str] = None
    identity_match: Optional[bool] = None
    webhook_target_url: Optional[str] = None


class CompleteRequestBody(BaseModel):
    result: Optional[str] = None


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "nametag-simulator",
        "version": "1.2.0",
        "defaults": {
            "default_simulator_result": DEFAULT_SIMULATOR_RESULT,
            "default_identity_match": DEFAULT_IDENTITY_MATCH
        }
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "time": iso_z(utc_now())
    }


@app.post("/api/requests")
def create_request(body: CreateRequestBody):
    request_id = f"req_{uuid.uuid4().hex[:8]}"
    subject = body.subject_hint or f"user-{uuid.uuid4().hex[:6]}@demo.nametag.co"

    simulator_result = normalize_result(body.simulator_result)
    identity_match = normalize_identity_match(body.identity_match)

    record = {
        "id": request_id,
        "env": body.env,
        "link": f"{SIMULATOR_BASE_URL}/verify/{request_id}",
        "status": 100,
        "label": body.label,
        "claims": body.claims,
        "created_at": iso_z(utc_now()),
        "subject": subject,
        "simulator_result": simulator_result,
        "identity_match": identity_match,
        "webhook_target_url": body.webhook_target_url or WEBHOOK_TARGET_URL,
        "webhook_sent": False,
    }

    REQUESTS[request_id] = record

    return {
        "id": record["id"],
        "env": record["env"],
        "link": record["link"],
        "status": record["status"],
        "label": record["label"],
        "claims": record["claims"],
        "created_at": record["created_at"],
        "subject": record["subject"],
        "simulator_result": record["simulator_result"],
        "identity_match": record["identity_match"]
    }


@app.get("/api/requests/{request_id}")
def get_request(request_id: str):
    record = REQUESTS.get(request_id)
    if not record:
        raise HTTPException(status_code=404, detail="Request not found")
    return record


@app.get("/simulator/requests")
def list_requests():
    return {
        "count": len(REQUESTS),
        "items": list(REQUESTS.values())
    }


@app.post("/simulator/complete/{request_id}")
def complete_request(request_id: str, body: CompleteRequestBody):
    record = REQUESTS.get(request_id)
    if not record:
        raise HTTPException(status_code=404, detail="Request not found")

    result_value = normalize_result(body.result or record.get("simulator_result"))

    if result_value == "success":
        record["status"] = 200
    else:
        record["status"] = 409

    return send_webhook(record, override_result=result_value)


@app.get("/people/{subject}/properties/{claims}")
def get_properties(subject: str, claims: str):
    matching_requests = [r for r in REQUESTS.values() if r.get("subject") == subject]
    if not matching_requests:
        raise HTTPException(status_code=404, detail="Subject not found")

    matching_requests.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    latest = matching_requests[0]

    requested_claims = [c.strip() for c in claims.split(",") if c.strip()]
    props = build_properties_for_request(latest, requested_claims)

    request_status = 200 if latest.get("simulator_result") == "success" else 409

    return {
        "subject": subject,
        "requests": [
            {
                "id": latest["id"],
                "status": request_status,
                "claims": latest["claims"],
                "label": latest.get("label"),
                "identity_match": latest.get("identity_match", True),
                "simulator_result": latest.get("simulator_result", "success")
            }
        ],
        "properties": props
    }


@app.get("/verify/{request_id}")
def verify_page_placeholder(request_id: str):
    record = REQUESTS.get(request_id)
    if not record:
        raise HTTPException(status_code=404, detail="Request not found")

    return {
        "ok": True,
        "message": "This is a simulator verification link placeholder.",
        "request_id": request_id,
        "subject": record["subject"],
        "simulator_result": record["simulator_result"],
        "identity_match": record["identity_match"]
    }
