# AGENT-0

AGENT-0 is a single-agent marketplace runner focused on one working agent, not a swarm. The project is intentionally fail-closed: default mode is dry-run, live mode is explicit, and unknown API schemas are blocked until verified.

## Current status

The repository contains the initial Python agent scaffold with dry-run adapters for OpenTask, MoltMarket, and AgentWorld. Live integrations remain disabled until their current official authentication and API schemas are verified.

## Safety defaults

- dry-run is the default;
- no private key or seed phrase is required or accepted;
- live submissions require explicit `RUN_MODE=live` and `ALLOW_LIVE_SUBMISSIONS=true`;
- undocumented endpoints are not guessed;
- duplicate bids are prevented through SQLite logging;
- the agent uses one identity and does not attempt to bypass anti-abuse controls.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m agent.main
pytest -q
```

The default command does not call real marketplaces or move funds.

## Live mode

Before enabling live mode, confirm the current official API endpoints and auth flows for each marketplace. Configure only documented paths and start with a small bid limit.

```env
RUN_MODE=live
ALLOW_LIVE_SUBMISSIONS=true
MAX_BIDS_PER_CYCLE=1
```

Never put a seed phrase or private key in `.env`, GitHub, or chat.
