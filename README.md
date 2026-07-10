# Stock Recommender

A-share market-data collector and AI-assisted report generator for Hermes
stock-analysis jobs.

The default primary source is Eastmoney's `BK0809` AI Agent board. If that board
endpoint is unavailable, the pipeline falls back to Sina realtime quotes for the
configured AI-agent stock pool. The strategy filters out non-Shenzhen/Shanghai
rows, ST rows, and empty quote rows before scoring candidates.

Recommended production flow:

```text
Market data -> candidate scoring -> AI context -> LLM analysis -> guarded recommendation
```

`data` mode remains available when another orchestrator, such as Hermes, should
consume the structured snapshot. `report` mode remains available as a pure-script
fallback when the AI model is unavailable.

## Architecture

The compatibility entrypoint is still `src/stock_agent.py`, but the implementation
is split by responsibility:

```text
src/stock_recommender/
  config.py        constants and stock-pool defaults
  data_sources.py  Eastmoney, Sina fallback, static fallback, tick fetchers
  selection.py     candidate filtering, scoring, price position, ignition signal
  context.py       structured JSON context for an AI agent
  llm.py           OpenAI-compatible chat completion client
  reports.py       script report, AI report orchestration, risk guard
  cli.py           environment variable parsing and output writing
```

New code should import from `stock_recommender.*`. Existing automation can keep
using `stock_agent.py`.

## Local Test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

If installed as a package, the CLI entrypoint is:

```bash
stock-agent
```

## Run Once

```bash
STOCK_AGENT_MODE=report PYTHONPATH=src python3 src/stock_agent.py
```

The generated report is written to:

```text
data/stock_recommendation.md
```

To output structured market data for an AI agent instead of a final report:

```bash
STOCK_AGENT_MODE=data PYTHONPATH=src python3 src/stock_agent.py
```

To let stock-agent call an OpenAI-compatible LLM endpoint directly:

```bash
STOCK_AGENT_MODE=ai \
STOCK_AGENT_LLM_BASE_URL=http://127.0.0.1:8911/v1 \
STOCK_AGENT_LLM_MODEL=your-model \
PYTHONPATH=src python3 src/stock_agent.py
```

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `STOCK_AGENT_BOARD_CODE` | `BK0809` | Eastmoney board code. |
| `STOCK_AGENT_BOARD_NAME` | `AI智能体` | Display name used in the report. |
| `STOCK_AGENT_MODE` | `report` | `report` for fallback report, `data` for AI-agent context, `ai` for direct LLM analysis. |
| `STOCK_AGENT_TOP_N` | `3` | Number of recommendations to include. |
| `STOCK_AGENT_CANDIDATE_LIMIT` | `8` | Number of candidates included in `data` mode. |
| `STOCK_AGENT_OUTPUT` | `data/stock_recommendation.md` | Report output path. |
| `STOCK_AGENT_LLM_BASE_URL` | empty | OpenAI-compatible base URL for `ai` mode. |
| `STOCK_AGENT_LLM_MODEL` | `Flux_AI/Flux_AI:latest` | Model name used for final analysis in `ai` mode. |
| `STOCK_AGENT_LLM_API_KEY` | `ollama` | Bearer token for OpenAI-compatible endpoint. |
| `STOCK_AGENT_LLM_TIMEOUT` | `60` | LLM request timeout in seconds. |
| `STOCK_AGENT_ENRICH_LIMIT` | `12` | Maximum candidates enriched with historical/financial data per run. |
| `STOCK_AGENT_ENRICH_TIMEOUT` | `8` | Timeout in seconds for each historical-data request. |
| `STOCK_AGENT_DELIVERY_RUN` | `0` | Set to `1` for scheduled runs so the strategy delivery policy can suppress stdout. |
| `STOCK_AGENT_HERMES_JOB_ID` | empty | Hermes cron job synchronized by the parameter admin. |
| `STOCK_AGENT_HERMES_BIN` | `hermes` | Hermes executable used for cron edit/pause/resume. |
| `STOCK_AGENT_DEFAULT_DELIVERY_TARGET` | empty | Legacy strategy recipient used until delivery settings are explicitly saved. |

## Parameter Admin

The parameter admin is a React 19 application built with TanStack Query,
TanStack Table, shadcn/ui, Tailwind CSS v4, and Vite. The production bundle is
served by the dependency-free Python admin server.

Run the production admin locally:

```bash
PYTHONPATH=src python3 -m stock_recommender.admin --host 127.0.0.1 --port 8765
```

Then open `http://127.0.0.1:8765`.

Frontend development and production build:

```bash
cd frontend
npm install
npm run dev
npm run build
```

The Vite development server proxies `/api` to port `8765`. The production build
is written to `src/stock_recommender/web/` so the production host does not need Node.js.

Saved settings use `data/strategy_config.json` by default. Override the path
with `STOCK_AGENT_CONFIG`. Parameters marked as realtime or derived affect the
selection pipeline; planned parameters preserve strategy intent until their data
source is implemented.

The admin supports multiple isolated strategies. The first screen is the
strategy library; create a strategy there before choosing parameters. New
strategies remain unused until explicitly enabled. Existing single-strategy
files are migrated in memory without changing their parameter values.

Each strategy includes an AI strategy assistant backed by the configured
OpenAI-compatible endpoint. It asks follow-up questions, presents a review, and
only creates a parameter draft after the user explicitly confirms. Parameter
IDs and values are still produced by the local catalog parser; model output
cannot directly write strategy configuration. If the model is unavailable, the
assistant falls back to a deterministic guided questionnaire.

The strategy library marks the configuration used by the scheduled Hermes job. Any
strategy can also be run immediately from the library or parameter editor. An
immediate run uses the production data/LLM settings but does not activate the
strategy, overwrite the scheduled report, or deliver a message. The latest 20
run results are stored beside `strategy_config.json` in `strategy_runs.json`.

Each strategy also owns its report delivery settings: enable/disable, channel,
recipient target, Beijing-time schedule, daily/weekday frequency, and whether
empty or failed reports should be sent. Saving the active strategy synchronizes
these settings to the configured Hermes cron job; activating another strategy
switches the live schedule and delivery target with it. New strategies are kept
offline and delivery-disabled until explicitly activated. Scheduled runs set
`STOCK_AGENT_DELIVERY_RUN=1`, allowing an empty stdout to suppress delivery
according to the strategy policy while still writing the local report file.

Historical and financial enrichment uses AkShare only when an enabled strategy
parameter requires it. Historical indicators use forward-adjusted daily prices;
fundamental filters use the latest disclosed reporting period. Install the
optional dependency with `pip install -e '.[analysis]'` when running outside the
Hermes stock-trading virtual environment.

## Hermes Cron

Preferred setup is now a no-agent Hermes cron that runs stock-agent in `ai` mode:

```bash
cd /path/to/internal-tools/apps/stock-agent
STOCK_AGENT_MODE=ai \
STOCK_AGENT_LLM_BASE_URL=http://127.0.0.1:8911/v1 \
STOCK_AGENT_LLM_MODEL=your-model \
PYTHONPATH=src python3 src/stock_agent.py
```

Or copy `scripts/hermes-ai-run.sh` into `~/.hermes/scripts/` and configure the
Hermes cron job as `no-agent` with `script=hermes-ai-run.sh`.

Alternative setup is a Hermes agent-driven cron job:

1. Copy `scripts/hermes-agent-data-run.sh` into `~/.hermes/scripts/`.
2. Create or edit a Hermes cron job with `--script hermes-agent-data-run.sh`.
3. Keep `no_agent` disabled so Hermes injects the script output into the prompt.
4. Use `prompts/hermes-stock-analysis.md` as the job prompt.

## Deployment

`deploy/stock-agent-admin.service` is a generic systemd system-service template.
It expects the application at `/opt/stock-agent`, runs as a dedicated
`stock-agent` user, and reads environment-specific settings from
`/etc/stock-agent/stock-agent.env`.

```bash
sudo install -d /etc/stock-agent
sudo install -m 600 deploy/stock-agent.env.example /etc/stock-agent/stock-agent.env
sudo install -m 644 deploy/stock-agent-admin.service /etc/systemd/system/stock-agent-admin.service
sudo systemctl daemon-reload
sudo systemctl enable --now stock-agent-admin.service
```

Edit the environment file before starting the service. Never commit API keys,
Hermes job identifiers, recipient targets, or generated files from `data/`.

## License

MIT. See `LICENSE`.

This project is provided for research and automation purposes and is not
financial advice.
