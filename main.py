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
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Nametag Simulator", version="1.2.0")

SIMULATOR_SECRET = os.getenv("SIMULATOR_SECRET", "change-me-in-render")
SIMULATOR_BASE_URL = os.getenv("SIMULATOR_BASE_URL", "http://localhost:8000")
WEBHOOK_TARGET_URL = os.getenv("WEBHOOK_TARGET_URL", "")
AUTO_COMPLETE_SECONDS = int(os.getenv("AUTO_COMPLETE_SECONDS", "0"))

requests_store: Dict[str, dict] = {}
chat_sessions: Dict[str, dict] = {}


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


class ChatSessionUpsertBody(BaseModel):
    user_phone: str
    active_worker: str
    session_status: str
    pending_prompt: Optional[str] = None
    last_user_message: Optional[str] = None
    context: Optional[dict] = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_chat_session(user_phone: str) -> Optional[dict]:
    return chat_sessions.get(user_phone)


def set_chat_session(
    user_phone: str,
    active_worker: str,
    session_status: str,
    pending_prompt: Optional[str] = None,
    last_user_message: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    rec = {
        "user_phone": user_phone,
        "active_worker": active_worker,
        "session_status": session_status,
        "pending_prompt": pending_prompt,
        "last_user_message": last_user_message,
        "context": context or {},
        "updated_at": utc_now_iso(),
    }
    chat_sessions[user_phone] = rec
    return rec


def clear_chat_session(user_phone: str) -> bool:
    return chat_sessions.pop(user_phone, None) is not None


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


def render_verify_html(rec: dict) -> str:
    already_completed = rec.get("webhook_sent", False)
    completed_text = ""
    if already_completed:
        completed_text = """
        <div class="done-banner">
          This verification was already completed earlier.
        </div>
        """

    disabled_attr = "disabled" if already_completed else ""

    return f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <title>Nametag Simulator Verification</title>
        <style>
          body {{
            font-family: Arial, sans-serif;
            max-width: 720px;
            margin: 40px auto;
            padding: 20px;
            line-height: 1.5;
            background: #f7f7f9;
          }}
          .card {{
            background: white;
            border: 1px solid #ddd;
            border-radius: 10px;
            padding: 24px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.05);
          }}
          h2 {{
            margin-top: 0;
          }}
          .meta {{
            margin: 6px 0;
          }}
          .button-row {{
            margin-top: 22px;
          }}
          button {{
            background: #0a66c2;
            color: white;
            border: none;
            padding: 12px 18px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 16px;
            margin-right: 12px;
          }}
          button.secondary {{
            background: #b42318;
          }}
          button:disabled {{
            background: #999;
            cursor: not-allowed;
          }}
          #status {{
            margin-top: 20px;
            font-weight: bold;
            white-space: pre-wrap;
          }}
          .done-banner {{
            background: #ecfdf3;
            border: 1px solid #a6f4c5;
            color: #067647;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 18px;
          }}
        </style>
      </head>
      <body>
        <div class="card">
          <h2>Nametag Simulator</h2>
          {completed_text}
          <div class="meta"><strong>Request ID:</strong> {rec["id"]}</div>
          <div class="meta"><strong>Subject:</strong> {rec["subject"]}</div>
          <div class="meta"><strong>Label:</strong> {rec["label"]}</div>
          <div class="meta"><strong>Claims:</strong> {", ".join(rec["claims"])}</div>
          <div class="meta"><strong>Current result mode:</strong> {rec["result"]}</div>

          <p>This page now waits for an explicit button click before sending the Nametag webhook.</p>

          <div class="button-row">
            <button id="successBtn" onclick="completeVerification('success')" {disabled_attr}>
              Complete Verification
            </button>
            <button id="failBtn" class="secondary" onclick="completeVerification('fail')" {disabled_attr}>
              Fail Verification
            </button>
          </div>

          <div id="status"></div>
        </div>

        <script>
          async function completeVerification(resultValue) {{
            const status = document.getElementById('status');
            const successBtn = document.getElementById('successBtn');
            const failBtn = document.getElementById('failBtn');

            successBtn.disabled = true;
            failBtn.disabled = true;
            status.innerText = 'Submitting ' + resultValue + ' result...';

            try {{
              const res = await fetch('/simulator/complete/{rec["id"]}', {{
                method: 'POST',
                headers: {{
                  'Content-Type': 'application/json'
                }},
                body: JSON.stringify({{ result: resultValue }})
              }});

              const data = await res.json();

              if (res.ok) {{
                status.innerText =
                  'Done. Webhook sent.\\n' +
                  'HTTP status: ' + data.webhook_status_code + '\\n' +
                  'Request result: ' + data.payload.result;
              }} else {{
                status.innerText = 'Error: ' + JSON.stringify(data, null, 2);
                successBtn.disabled = false;
                failBtn.disabled = false;
              }}
            }} catch (err) {{
              status.innerText = 'Error: ' + err;
              successBtn.disabled = false;
              failBtn.disabled = false;
            }}
          }}
        </script>
      </body>
    </html>
    """


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
        threading.Thread(
            target=_auto_complete_later,
            args=(request_id, AUTO_COMPLETE_SECONDS),
            daemon=True,
        ).start()

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


@app.get("/verify/{request_id}", response_class=HTMLResponse)
def verify_page(request_id: str):
    rec = requests_store.get(request_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Request not found")

    if not rec.get("webhook_target_url"):
        raise HTTPException(
            status_code=400,
            detail="No webhook target configured on the request or service",
        )

    return HTMLResponse(content=render_verify_html(rec))


@app.post("/simulator/complete/{request_id}")
def complete_request(request_id: str, body: CompleteRequestBody):
    rec = requests_store.get(request_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Request not found")

    if rec.get("webhook_sent"):
        return {
            "ok": True,
            "already_completed": True,
            "request_id": request_id,
            "webhook_target_url": rec.get("webhook_target_url"),
            "status": rec.get("status"),
            "completed_at": rec.get("completed_at"),
        }

    if body.result:
        rec["result"] = body.result
    if body.webhook_target_url:
        rec["webhook_target_url"] = body.webhook_target_url

    if not rec["webhook_target_url"]:
        raise HTTPException(
            status_code=400,
            detail="No webhook target configured on the request or service",
        )

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


@app.get("/chat-sessions")
def list_chat_sessions():
    return {
        "count": len(chat_sessions),
        "sessions": list(chat_sessions.values()),
    }


@app.get("/chat-sessions/{user_phone:path}")
def read_chat_session(user_phone: str):
    rec = get_chat_session(user_phone)
    if not rec:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return rec


@app.post("/chat-sessions")
def upsert_chat_session(body: ChatSessionUpsertBody):
    rec = set_chat_session(
        user_phone=body.user_phone,
        active_worker=body.active_worker,
        session_status=body.session_status,
        pending_prompt=body.pending_prompt,
        last_user_message=body.last_user_message,
        context=body.context,
    )
    return {
        "ok": True,
        "session": rec,
    }


@app.delete("/chat-sessions/{user_phone:path}")
def delete_chat_session(user_phone: str):
    deleted = clear_chat_session(user_phone)
    if not deleted:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return {
        "ok": True,
        "deleted": True,
        "user_phone": user_phone,
    }


def _auto_complete_later(request_id: str, seconds: int):
    time.sleep(seconds)
    rec = requests_store.get(request_id)

    if not rec or not rec.get("webhook_target_url") or rec.get("webhook_sent"):
        return

    try:
        complete_request(request_id, CompleteRequestBody())
    except Exception:
        return
