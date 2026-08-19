# JSON API

[← README](../README.md)

A read-only JSON API for job state: dashboards, monitoring, or scripts.

| Endpoint | Description |
|---|---|
| `GET /api/jobs` | All jobs, most recent first. Optional `?status=` (`pending`, `running`, `scanning`, `awaiting_review`, `done`, `failed`, `canceled`; an unrecognised value returns `400`) and `?limit=N` (max 500, default 50). Returns `{"jobs": [...], "count": N}`. |
| `GET /api/jobs/{id}/status` | One job as JSON: list fields plus a `log_lines` array (last 50 lines). 404 if not found. |
| `GET /api/jobs/{id}/stream` | SSE stream for a live job: each log line is a `message` event, a `done` event signals completion. `progress` events and `: ping` keepalives may also appear. |
| `GET /api/queue/count` | A lightweight `{"count": N, "running": true/false, "signature": "...", "attention": N}` for an "is anything active?" badge, without pulling the whole job list. The opaque signature changes when the active work changes, and `attention` counts finished jobs that still need attention. |

(`/api/diagnostics` also lives under `/api/`, but it serves an HTML fragment for the Settings page, not JSON, so it is not part of this API.)

With login enabled, every endpoint needs the session cookie. A missing or invalid one returns `401 {"detail": "authentication required"}` rather than a redirect, so non-browser clients can detect the auth gate cleanly.

```bash
# Is any job running or scanning?
curl -s -b 'ql_session=<your-cookie>' http://localhost:8666/api/queue/count | jq '.running'
```
