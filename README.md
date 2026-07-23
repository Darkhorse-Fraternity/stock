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

The implementation is split by responsibility:

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
  recommendation.py structured recommendation plan shared by reports, tracking, and portfolio execution
  performance.py   recommendation audit archive
  backtest.py      shared-signal rolling walk-forward orchestration and approval gate
  portfolio_backtest.py  isolated in-memory replay using the live portfolio engine
  market_regime.py  sector breadth, exposure budget, and absolute-momentum admission
  pipeline.py      composable strategy stages and execution context
  portfolio.py     per-strategy positions, orders, exits, and performance
  portfolio_pipeline.py  entry, risk, exit, and replacement orchestration
```

Application code imports from `stock_recommender.*`; automation runs the package
CLI with `python3 -m stock_recommender.cli`.

## Strategy Lifecycle And Validation

Persisted strategies follow `draft -> backtesting -> paper -> live`. A newly
created or materially edited strategy returns to `draft`; a completed backtest
moves it to paper trading even when the live gate fails. Live promotion requires
all rolling out-of-sample checks to pass and at least 40 distinct paper-trading
sessions. Live and archived configurations are immutable; create a revision to
change their model parameters.

The persisted strategy store accepts only schema `version: 5`. Unsupported
single-strategy files, missing lifecycle metadata, missing or duplicate IDs, and
an invalid `active_strategy_id` fail explicitly; the service does not migrate or
silently repair them.

The default gate uses roughly three years of history, purged rolling windows,
the same T+1/order/exit/risk pipeline as paper trading, doubled-cost stress,
liquidation NAV drawdown, positive-window ratio and Deflated Sharpe probability.
Promotion also requires dated universe snapshots, an independent benchmark and
historical execution-time volume/limit data. Data built from today's constituents
is explicitly marked incomplete and cannot pass the live gate, which prevents a
convenient current-universe backtest from hiding survivorship bias. The LLM
explains fixed strategy output; it does not choose factors or tune thresholds
during the evaluation.

## Local Test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

Instance-specific settings belong in an ignored `.env` file. Start from the
versioned template and restrict its permissions before adding model credentials,
Hermes job IDs, or delivery targets:

```bash
cp .env.example .env
chmod 600 .env
```

The Hermes launch scripts and the systemd service load this file automatically.
Set `STOCK_AGENT_ENV_FILE` when a launch script should use a different file.

If installed as a package, the CLI entrypoint is:

```bash
stock-agent
```

## Run Once

```bash
STOCK_AGENT_MODE=report PYTHONPATH=src python3 -m stock_recommender.cli
```

The generated report is written to:

```text
data/stock_recommendation.md
```

To output structured market data for an AI agent instead of a final report:

```bash
STOCK_AGENT_MODE=data PYTHONPATH=src python3 -m stock_recommender.cli
```

To let stock-agent call an OpenAI-compatible LLM endpoint directly:

```bash
STOCK_AGENT_MODE=ai \
STOCK_AGENT_LLM_BASE_URL=http://127.0.0.1:8911/v1 \
STOCK_AGENT_LLM_MODEL=your-model \
PYTHONPATH=src python3 -m stock_recommender.cli
```

## Custom Watchlist And Sector Filters

Set `STOCK_AGENT_WATCHLIST` to make the configured stocks the complete
recommendation universe. Compact entries use `code:name:sector`; the name and
sector are optional:

```bash
STOCK_AGENT_MODE=report \
STOCK_AGENT_WATCHLIST='600519:贵州茅台:白酒,000858:五粮液:白酒,300750:宁德时代:新能源' \
PYTHONPATH=src python3 -m stock_recommender.cli
```

Apply one or more exact sector-label filters with
`STOCK_AGENT_SECTOR_FILTERS`:

```bash
STOCK_AGENT_WATCHLIST='600519:贵州茅台:白酒,000858:五粮液:白酒,300750:宁德时代:新能源' \
STOCK_AGENT_SECTOR_FILTERS='白酒' \
PYTHONPATH=src python3 -m stock_recommender.cli
```

JSON is supported when a stock needs multiple sector tags:

```bash
STOCK_AGENT_WATCHLIST='[
  {"symbol":"300750","name":"宁德时代","sector":"锂电池","sectors":["锂电池","新能源"]},
  {"symbol":"600519","name":"贵州茅台","sector":"白酒"}
]' \
STOCK_AGENT_SECTOR_FILTERS='新能源,白酒' \
PYTHONPATH=src python3 -m stock_recommender.cli
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
| `STOCK_AGENT_HISTORY_PATH` | `data/recommendation_history.json` | Recommendation audit archive; strategy performance uses the full portfolio ledger. |
| `STOCK_AGENT_MARKET_HISTORY_CACHE_DIR` | `data/market_history_cache` | Per-symbol daily-history cache used by enrichment, backtests, and replay tools. |
| `STOCK_AGENT_HISTORY_CACHE_TTL_SECONDS` | `21600` | Fresh-cache lifetime; stale data remains available when the upstream source fails. |
| `STOCK_AGENT_HISTORY_FETCH_ATTEMPTS` | `3` | Bounded daily-history download attempts. |
| `STOCK_AGENT_HISTORY_FETCH_BACKOFF_SECONDS` | `1` | Initial exponential retry delay. |
| `STOCK_AGENT_HISTORY_FETCH_WORKERS` | `2` | Maximum concurrent history downloads during a backtest. |
| `STOCK_AGENT_BACKTEST_DATASET_PATH` | empty | Optional read-only point-in-time JSON dataset; when absent the current-universe exploratory loader is used and cannot pass the live gate. |
| `STOCK_AGENT_PUBLIC_URL` | empty | Public service origin used to build canonical strategy-performance links, for example `http://host:8765`. |
| `STOCK_AGENT_SCHEDULE_GUARD` | `0` | Set to `1` to skip publication outside configured weekday hours. Background scripts default to `1`. |
| `STOCK_AGENT_PUBLISH_HOURS` | `9,10,11,13,14,15` | Beijing-time hours allowed by the schedule guard. |

## Weekday Hourly Tracking

At 08:00 Beijing time, the strategy ranks stocks from data strictly before the
current trading day and saves the deterministic `factor_rank_v1` portfolio list
to `STOCK_AGENT_STATE_PATH`. Later tracking jobs load that same list and fetch
fresh quotes without running selection again. Each tracking report contains
latest price, intraday change percentage, trading volume in hands, and turnover
amount. It does not include unrelated stocks from the full board or watchlist.

Configure two Hermes cron jobs in the `Asia/Shanghai` timezone. Generate and save
the strategy list before the market opens:

```cron
0 8 * * 1-5  hermes-ai-run.sh
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

## Strategy Portfolio Performance

Each strategy owns an independent paper portfolio with at most ten positions.
The portfolio pipeline records entries, partial fills, fees, exits, replacement
events, NAV, drawdown, and win rate for the strategy's full lifecycle. Open a
strategy at `/strategies/<strategy-id>/portfolio`; the matching JSON endpoint is
`/api/strategies/<strategy-id>/portfolio`.

Set `STOCK_AGENT_PUBLIC_URL` in both recommendation and tracking jobs to
append a Feishu-compatible Markdown link below every delivered report.
Recommendation and portfolio records are associated strictly by strategy ID;
records are never remapped between strategy versions.

Daily history downloads are cached per symbol and retried with exponential
backoff. A fresh cache avoids the upstream request entirely; if refresh attempts
fail, the most recent valid cache is returned so an already reproducible replay
does not become unavailable during a provider outage.

## Hermes Cron

Preferred setup is now a no-agent Hermes cron that runs stock-agent in `ai` mode:

```bash
cd /path/to/internal-tools/apps/stock-agent
STOCK_AGENT_MODE=ai \
STOCK_AGENT_LLM_BASE_URL=http://127.0.0.1:8911/v1 \
STOCK_AGENT_LLM_MODEL=your-model \
PYTHONPATH=src python3 -m stock_recommender.cli
```

Install regular runtime launchers into Hermes' allowed scripts directory:

```bash
scripts/install-hermes-launchers.sh
```

Configure each `no-agent` cron job with `--workdir` set to the stock-agent
checkout. Do not symlink files into `~/.hermes/scripts/`: Hermes resolves the
scheduled path and rejects links whose target is outside that directory. The
installer records the checkout in a plain `stock-agent-app-dir` pointer file.
The runtime launchers stay inside the allowed directory, read that pointer, and
delegate to the matching versioned script. Re-run the installer after moving the
checkout. The daily and tracking scripts
share `/tmp/stock-agent-daily-selection.json` by default, so hourly tracking
always follows the 08:00 strategy portfolio list.

Alternative setup is a Hermes agent-driven cron job:

1. Run `scripts/install-hermes-launchers.sh`.
2. Create or edit a Hermes cron job with `--script hermes-agent-data-run.sh`.
3. Keep `no_agent` disabled so Hermes injects the script output into the prompt.
4. Use `prompts/hermes-stock-analysis.md` as the job prompt.
