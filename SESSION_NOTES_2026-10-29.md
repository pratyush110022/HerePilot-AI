# HerePilot-AI: end-of-day handoff, 29 Oct 2026

Written so tomorrow's session can resume without re-reading the conversation. No credentials or account numbers are in this file.

## Where everything is saved

| What | Where |
|---|---|
| Project files (working tree) | this repository, **uncommitted** (nothing staged, nothing committed) |
| Branch / HEAD | `maintenance/phase-1-stability` / `123cc98` (unchanged) |
| Full copy of the project as of tonight | `~/GeoPilot-AI-backups/phase3-end-of-day-20261029/` |
| Copy from before today's big feature work | `~/GeoPilot-AI-backups/phase3-pre-conversational-ai/` |
| Older checkpoints | `~/GeoPilot-AI-backups/phase1-*`, `phase2-pre-implementation` |
| Browser and test scripts (they lived in `/tmp`, which is wiped on reboot) | `…/phase3-end-of-day-20261029/tools/hp-live/` and `tools/test-support/no_network_guard.py` |
| The conversation itself | stored by Kiro: `~/.kiro/sessions/88d56c5af0d8945e/sess_a31fb337-e1cd-43c2-a9d7-1f5dd577f4d0/messages.jsonl` (about 33 MB) |
| AWS publishing requirements document | shown in the chat as an artifact; the same content is summarised below |

The `.env` files are untouched (last modified 26 Oct) and are not copied into backups.

## What was built today

1. **Travel guides** ("best time to visit Ooty"): Nova Pro writes the advice, HERE supplies the places, Tripadvisor photos and ratings (via HERE) are shown on cards.
2. **Conversational answers** (new `ask` tool): compare, pros and cons, "tell me more about X", follow-ups using a six-turn server-side memory. The model picks the layout (text, lists, pros and cons, comparison table, itinerary, facts, places) and can only point at places HERE returned. Figures it cannot know are removed.
3. **Airport and named-place fixes**: "Where is Kempegowda International Airport?" and "nearest airport from Puri" now work, and never search along or change an active route.
4. **Light theme**, smaller fonts, bigger auto-growing query box, official HERE logo before "HerePilot-AI", relevance-only suggestion chips.
5. **GPS button on the map** (above the zoom buttons) with a "you are here" dot, a ✕ to stop sharing, and clear permission errors. The server forgets the location immediately (`DELETE /session/{id}/location`).
6. **English map labels**, and the HERE map SDK upgraded 3.1 to 3.2 (fixes Satellite and Traffic being disabled).
7. **Tripadvisor compliance steps**: photos and ratings are no longer stored in the browser, ratings are plain text with a credit and review link.
8. Replies no longer repeat the place list that the cards show; places are de-duplicated by HERE ID.

## State of the checks (last run tonight)

- Backend: **409 tests pass** (also in the CI-style environment); orchestrator smoke test passes.
- Frontend: type check clean, lint 0 warnings and 0 errors, production build succeeds.
- Browser (desktop, tablet, phone): all flows checked against live HERE with a **scripted stand-in model**.
- **Never tested live: real Nova Pro output for the new `ask` answers.** No live Bedrock call was authorised or made this turn.

## Known loose ends

- README and ARCHITECTURE do not yet describe the guide, the `ask` tool, the GPS control or `HERE_RICH_PLACES`. `.env.example` does not list `HERE_RICH_PLACES`.
- Tripadvisor's own display rules (its logo and rating graphics) are not met; HERE's Tripadvisor data is a restricted beta. Decide before any public launch.
- "Nearest" distances are straight-line, not driving distances (the reply says so).
- Old unused backup files remain in `backend/services/` (`groq_*backup*.py`, `ev_planner b.py`).
- CI would fail on a clean checkout until the many untracked source files are committed (needs your approval).

## Decisions waiting for you (for AWS publishing)

1. Public audience, or HERE staff only?
2. AWS account and region (Bedrock is on `eu-west-2` today; users are in India).
3. Domain name.
4. Managed containers (S3 + CloudFront + one Fargate or App Runner task) or a single server?
5. Keep Tripadvisor photos and ratings on at launch, or `HERE_RICH_PLACES=0`?
6. Budget ceiling.

Key facts behind the plan: **one backend instance only** (sessions are in memory); no API Gateway (chats can take 5 to 40 s); HTTPS is required (GPS button); no login or rate limiting exists in the backend, so add WAF or SSO and a budget alarm before it is public; use an IAM role for Bedrock (the laptop `aws login` session expires); use two HERE keys (server secret, browser separate).

## How to restart tomorrow

Everything was stopped tonight (backend, frontend dev server, test browser).

```bash
# 1. If the AWS login has expired (chats then fall back to the "AI not connected" reply):
aws login --profile rocket-garage-login

# 2. Backend with Nova Pro (starting it makes no Bedrock call by itself)
cd ~/Downloads/GeoPilot-AI/backend
env PYTHONDONTWRITEBYTECODE=1 GEO_HTTP_CA_BUNDLE=$HOME/.aws/aws-ca-bundle.pem LLM_PROVIDER=bedrock \
  BEDROCK_MODEL_ID=amazon.nova-pro-v1:0 BEDROCK_REGION=eu-west-2 AWS_PROFILE=rocket-garage-login \
  .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000

# 3. Frontend (second terminal)
cd ~/Downloads/GeoPilot-AI/frontend && npm run dev        # http://localhost:5173

# Safe mode with no AI and no Bedrock cost: same as step 2 but LLM_PROVIDER=none
```

Offline tests (run from `backend/`, never from the repo root, because `test_groq.py` there calls a real service):

```bash
# The test environment lived in /tmp and is gone after a reboot. Recreate it once:
python3.12 -m venv /tmp/geopilot-dev-venv && /tmp/geopilot-dev-venv/bin/pip install -r requirements-dev.txt
cp ~/GeoPilot-AI-backups/phase3-end-of-day-20261029/tools/test-support/no_network_guard.py /tmp/
cd ~/Downloads/GeoPilot-AI/backend
env HERE_API_KEY= LLM_PROVIDER=none PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/tmp \
  /tmp/geopilot-dev-venv/bin/pytest -p no_network_guard -p no:cacheprovider -q        # expect 409 passed
```

The browser test scripts are in the backup's `tools/hp-live/`; copy that folder back to `/tmp/hp-live` to reuse them. `fake_llm_server.py` there runs the real backend with the scripted stand-in model (no Bedrock) for UI testing.

## Suggested order for tomorrow

1. Answer the six decisions above.
2. Approve (or decline) committing the work, so a pipeline can build from a clean checkout.
3. If you authorise a few live Nova Pro tests, validate the `ask` answers with real model output (suggested queries: "Is Coorg worth visiting? Explain the pros and cons.", "Compare Coorg and Mysuru for a three-day trip", "Tell me more about Puri Jagannath Temple").
4. Update README, ARCHITECTURE and `.env.example` for the new features.
5. Then the deployment files (Dockerfile, pipeline, infrastructure code), only on your go-ahead.
