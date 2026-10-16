# Clover

Witness-backed skill tracks. Clover schedules a skill task into a fixed window, checks that it was really done (`clover verify` reads your Docker state), and if you miss the window it tells a witness you picked, over WhatsApp.

Design: [docs/designs/clover-v1.md](docs/designs/clover-v1.md). This repo holds **Slice 1** (the witness loop): the laptop CLI, the server, and the first 5 Docker tasks.

## Layout

| Path | What |
|---|---|
| `clover/` | Laptop CLI (`clover verify`, `schedule`, `pause`, `witness`, `admin`) |
| `clover/checks/docker.py` | `state_check` runner (docker-py) |
| `app/outcomes.py` | Pure `decide(window, events, now)`: PASSED / PASSED_LATE / ATTEMPTED_FAILED / MISSED / PAUSED / SERVER_OUTAGE |
| `app/scheduler.py` | `run_pass(now)`, called every 30s from the FastAPI lifespan |
| `app/whatsapp.py` | Cloud API transport, outbox (at-least-once), webhook signature check |
| `app/witness.py` | Invite, consent by the witness's own message, STOP, undeliverable |
| `tracks/docker/` | Task specs; each needs at least one `unique: true` check |

## Develop

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest
```

`pytest -m docker` runs the real-Docker E2E tests (skipped when no daemon is reachable). CI runs both on ubuntu-24.04.

## Run the server

```bash
CLOVER_ADMIN_SECRET=... CLOVER_WA_APP_SECRET=... CLOVER_WA_VERIFY_TOKEN=... \
CLOVER_WA_ACCESS_TOKEN=... CLOVER_WA_PHONE_NUMBER_ID=... CLOVER_WA_BUSINESS_NUMBER=91XXXXXXXXXX \
uvicorn app.main:get_app --factory --workers 1
```

Always use a single worker. Without `CLOVER_WA_ACCESS_TOKEN`, alerts are written to the log so you can forward them by hand (use this until Meta approves the templates). The webhook is `/wa/webhook` and must be served over HTTPS (Caddy).

## Use the CLI

```bash
clover init --server https://clover.example.com
clover schedule --days mon,tue,wed,thu,fri --start 19:00 --minutes 60
clover witness add Anu +919876543210
clover task
clover verify
```
