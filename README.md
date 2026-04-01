# Nametag Simulator for n8n (Render + Python)

This is a beginner-friendly FastAPI service that simulates three Nametag behaviors:

1. Create a verification request
2. Send a Nametag-like signed webhook to n8n
3. Return verified properties for a subject

## Files
- `main.py` - FastAPI app
- `requirements.txt` - Python packages
- `runtime.txt` - Python version for Render

## Environment variables for Render
Set these in Render:

- `SIMULATOR_SECRET` = a shared secret used to sign outgoing webhooks
- `SIMULATOR_BASE_URL` = your Render URL, e.g. `https://nametag-simulator-demo.onrender.com`
- `WEBHOOK_TARGET_URL` = optional default n8n or test webhook URL
- `AUTO_COMPLETE_SECONDS` = optional; set to `0` to disable auto-completion

## Local run
```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

## Main endpoints

### 1) Create request
`POST /api/requests`

Example body:
```json
{
  "env": "demo-env",
  "claims": ["name", "email"],
  "label": "ticket-123",
  "subject_hint": "mac-demo@demo.nametag.co",
  "simulator_result": "success",
  "identity_match": true,
  "webhook_target_url": "https://YOUR-WEBHOOK-URL"
}
```

### 2) Complete request and send webhook
`POST /simulator/complete/{request_id}`

Example body:
```json
{
  "result": "success"
}
```

### 3) Get properties
`GET /people/{subject}/properties/{claims}`

Example:
`GET /people/mac-demo@demo.nametag.co/properties/name,email`

## Render build settings
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
