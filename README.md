# Stock Recommender

A-share market-data collector and AI-assisted report generator for Hermes
stock-analysis jobs.

The default primary source is Eastmoney's `BK0800` Artificial Intelligence board. If that board
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
  universe.py      watchlist parsing, universe constraints, sector filters
  schedule.py      Beijing-time weekday and hourly publication guard
  tracking.py      daily recommendation state and intraday quote tracking
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
STOCK_AGENT_LLM_BASE_URL=http://192.168.3.213:8911/v1 \
STOCK_AGENT_LLM_MODEL=codex-worker \
PYTHONPATH=src python3 src/stock_agent.py
```

## Custom Watchlist And Sector Filters

Set `STOCK_AGENT_WATCHLIST` to make the configured stocks the complete
recommendation universe. Compact entries use `code:name:sector`; the name and
sector are optional:

```bash
STOCK_AGENT_MODE=report \
STOCK_AGENT_WATCHLIST='600519:贵州茅台:白酒,000858:五粮液:白酒,300750:宁德时代:新能源' \
PYTHONPATH=src python3 src/stock_agent.py
```

Apply one or more exact sector-label filters with
`STOCK_AGENT_SECTOR_FILTERS`:

```bash
STOCK_AGENT_WATCHLIST='600519:贵州茅台:白酒,000858:五粮液:白酒,300750:宁德时代:新能源' \
STOCK_AGENT_SECTOR_FILTERS='白酒' \
PYTHONPATH=src python3 src/stock_agent.py
```

JSON is supported when a stock needs multiple sector tags:

```bash
STOCK_AGENT_WATCHLIST='[
  {"symbol":"300750","name":"宁德时代","sector":"锂电池","sectors":["锂电池","新能源"]},
  {"symbol":"600519","name":"贵州茅台","sector":"白酒"}
]' \
STOCK_AGENT_SECTOR_FILTERS='新能源,白酒' \
PYTHONPATH=src python3 src/stock_agent.py
```

When a watchlist is configured, board collection and the built-in fallback pool
are not used. A quote failure or an empty sector-filter result produces no
recommendation instead of adding stocks outside the watchlist.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `STOCK_AGENT_BOARD_CODE` | `BK0800` | Eastmoney Artificial Intelligence board code. |
| `STOCK_AGENT_BOARD_NAME` | `人工智能` | Display name used in the report. |
| `STOCK_AGENT_WATCHLIST` | empty | Custom universe as comma-separated `code:name:sector` entries or JSON. |
| `STOCK_AGENT_SECTOR_FILTERS` | empty | Comma-separated sector labels; `STOCK_AGENT_SECTORS` is accepted as an alias. |
| `STOCK_AGENT_MODE` | `report` | `report` for fallback report, `data` for AI-agent context, `ai` for direct LLM analysis, `track` for saved recommendations. |
| `STOCK_AGENT_TOP_N` | `3` | Number of recommendations to include. |
| `STOCK_AGENT_CANDIDATE_LIMIT` | `8` | Number of candidates included in `data` mode. |
| `STOCK_AGENT_OUTPUT` | `data/stock_recommendation.md` | Report output path. |
| `STOCK_AGENT_LLM_BASE_URL` | empty | OpenAI-compatible base URL for `ai` mode. |
| `STOCK_AGENT_LLM_MODEL` | `Flux_AI/Flux_AI:latest` | Model name used for final analysis in `ai` mode. |
| `STOCK_AGENT_LLM_API_KEY` | `ollama` | Bearer token for OpenAI-compatible endpoint. |
| `STOCK_AGENT_LLM_TIMEOUT` | `60` | LLM request timeout in seconds. |
| `STOCK_AGENT_TRACKING_LIMIT` | `3` | Maximum recommended stocks in the hourly volume/change tracking block. |
| `STOCK_AGENT_STATE_PATH` | empty | Daily recommendation state shared by the recommendation and tracking jobs. |
| `STOCK_AGENT_SCHEDULE_GUARD` | `0` | Set to `1` to skip publication outside configured weekday hours. Background scripts default to `1`. |
| `STOCK_AGENT_PUBLISH_HOURS` | `9,10,11,13,14,15` | Beijing-time hours allowed by the schedule guard. |

## Weekday Hourly Tracking

At 09:30, the recommendation job selects the day's stocks and saves their symbols
to `STOCK_AGENT_STATE_PATH`. Later tracking jobs load that same list and fetch
fresh quotes without running selection again. Each tracking report contains
latest price, intraday change percentage, trading volume in hands, and turnover
amount. It does not include unrelated stocks from the full board or watchlist.

Configure two Hermes cron jobs in the `Asia/Shanghai` timezone. Generate and save
the recommendation after the market opens:

```cron
30 9 * * 1-5  hermes-ai-run.sh
```

Then publish the saved stocks' current volume and change at the full-hour market
checkpoints:

```cron
0 10,11,13,14,15 * * 1-5  hermes-tracking-run.sh
```

The application guard uses the A-share sessions 09:30-11:30 and 13:00-15:00.
Accidental triggers on weekends, during the lunch break, before open, or after
close exit without generating or overwriting a report. This guard treats
Monday-Friday as workdays; exchange holidays still need to be excluded by the
external scheduler when applicable.

## Hermes Cron

Preferred setup is now a no-agent Hermes cron that runs stock-agent in `ai` mode:

```bash
cd /path/to/internal-tools/apps/stock-agent
STOCK_AGENT_MODE=ai \
STOCK_AGENT_LLM_BASE_URL=http://192.168.3.213:8911/v1 \
STOCK_AGENT_LLM_MODEL=codex-worker \
PYTHONPATH=src python3 src/stock_agent.py
```

Or copy `scripts/hermes-ai-run.sh` and `scripts/hermes-tracking-run.sh` into
`~/.hermes/scripts/`. Configure both as `no-agent` jobs with the two cron
expressions above. The scripts share `/tmp/stock-agent-daily-selection.json` by
default, so hourly tracking always follows the 09:30 recommendation list.

Alternative setup is a Hermes agent-driven cron job:

1. Copy `scripts/hermes-agent-data-run.sh` into `~/.hermes/scripts/`.
2. Create or edit a Hermes cron job with `--script hermes-agent-data-run.sh`.
3. Keep `no_agent` disabled so Hermes injects the script output into the prompt.
4. Use `prompts/hermes-stock-analysis.md` as the job prompt.
