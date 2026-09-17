# AGENT-0

AGENT-0 is a single autonomous AI-agent project focused on buying, bidding, completing, and getting paid on agent marketplaces using USDC. The first goal is one working agent, not a swarm.

## What this project does

- Reads task feeds from multiple agent marketplaces
- Evaluates whether a task is a fit
- Calculates a smart bid using cost estimate × 1.2
- Executes work through an LLM client
- Submits results to accepted contracts
- Logs bets, contracts, and payouts
- Uses Telegram notifications when configured

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

4. Run in safe dry-run mode:

```bash
python -m agent.main
```

5. Run a single platform cycle:

```bash
python -m agent.main --platform opentask
```

6. Run loop mode:

```bash
python -m agent.main --loop --platform opentask
```

## Modes

- `dry-run`: default, no live calls to marketplaces
- `live`: real requests when credentials are configured

## Required environment variables

See `.env.example` for the complete list.

## Supported platforms

- OpenTask
- MoltMarket
- AgentWorld

## Important notes

- Real marketplace APIs should be validated against their docs before production use.
- The repository is designed to start with a single working agent, then scale.
- The first phase is dry-run validation, then live credential testing.

## Example commands

```bash
python -m agent.main --platform opentask
python -m agent.main --platform moltmarket
python -m agent.main --platform agentworld
python -m agent.main --loop --platform opentask
```

## License

MIT
