# AGENT-0

AGENT-0 is a single autonomous AI-agent project focused on buying, bidding, completing, and getting paid on agent marketplaces using USDC. This repository contains a Python-based agent skeleton designed to run in dry-run mode by default and switch to live mode only when credentials are configured.

## Project goals

- Register with marketplaces that support agents
- Get open tasks and score them
- Bid with a rational strategy (cost estimate × 1.2)
- Execute the work via local or remote LLM
- Submit deliverables and log everything
- Withdraw or hold USDC to a wallet when available

## Quick start

1. Copy environment variables:
   ```bash
   cp .env.example .env
   ```

2. Fill in your values in `.env`.

3. Install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Run a dry-run cycle:
   ```bash
   python -m agent.main
   ```

5. Run continuous loop mode:
   ```bash
   python -m agent.main --loop --platform opentask
   ```

## Run modes

- `dry-run` is the default and safe mode. No live API calls are sent.
- `live` uses real credentials and network calls.

## Environment variables

See `.env.example` for a full list.

## Platforms supported

- OpenTask
- MoltMarket
- AgentWorld

## Notes

- This project intentionally avoids creating a swarm; it is designed around one working agent first.
- Real marketplace endpoints should be validated against the platform docs before production use.
- The first step is wallet + API credentials; this repo does not send funds or perform live payouts by default.

## Example

```bash
python -m agent.main --platform opentask
python -m agent.main --platform moltmarket
python -m agent.main --platform agentworld
python -m agent.main --loop --platform opentask
```

## License

MIT
