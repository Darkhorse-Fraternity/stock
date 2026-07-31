# Long/Short and Leverage Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add strategy-scoped leveraged-long and long/short simulation to Paper portfolios and backtests through a decoupled, versioned PortfolioEngine while keeping every existing strategy explicitly long-only after migration.

**Architecture:** Reuse the existing pure `PipelineRunner`, but move portfolio decisions into a new `portfolio_engine` domain package. Signal, target netting, exposure, borrow, margin, risk, execution, valuation, and ledger modules communicate through immutable contracts; only the ledger store mutates persisted state. Paper and backtest paths call the same `PortfolioEngine` service.

**Tech Stack:** Python 3.11+, dataclasses, `unittest`, JSON event ledger, optional Psycopg 3 integration-test adapter, React 19, TypeScript 5.9, Vite 7, ESLint.

---

## File map

### New domain files

- `src/stock_recommender/portfolio_engine/__init__.py` — public PortfolioEngine API only.
- `src/stock_recommender/portfolio_engine/contracts.py` — immutable enums, snapshots, targets, intents, fills, events, and decision batches.
- `src/stock_recommender/portfolio_engine/config.py` — strategy policy defaults, hard limits, and validation.
- `src/stock_recommender/portfolio_engine/ports.py` — quote, borrow, event-calendar, ledger-store, and future broker protocols.
- `src/stock_recommender/portfolio_engine/signal_ports.py` — long/short signal model registry.
- `src/stock_recommender/portfolio_engine/short_signal.py` — `short_trend_breakdown_v1` only.
- `src/stock_recommender/portfolio_engine/target_pipeline.py` — signal conversion and same-symbol netting.
- `src/stock_recommender/portfolio_engine/exposure.py` — mode and hard-cap enforcement.
- `src/stock_recommender/portfolio_engine/borrow.py` — shortability admission and estimated borrow costs.
- `src/stock_recommender/portfolio_engine/margin.py` — projected buying power, margin rate, and margin admission.
- `src/stock_recommender/portfolio_engine/risk.py` — direction-aware exits, cover-only state, drawdown, and forced deleveraging.
- `src/stock_recommender/portfolio_engine/execution.py` — order-intent planning and simulated fills.
- `src/stock_recommender/portfolio_engine/valuation.py` — long/short P&L, equity, exposure, NAV, and margin metrics.
- `src/stock_recommender/portfolio_engine/ledger.py` — schema-v2 JSON ledger store and idempotent event application.
- `src/stock_recommender/portfolio_engine/migration.py` — dry-run and atomic v1-to-v2 ledger migration.
- `src/stock_recommender/portfolio_engine/service.py` — stage orchestration and transaction boundary.
- `src/stock_recommender/portfolio_engine/postgres_store.py` — optional integration-test store adapter; imported lazily.
- `src/stock_recommender/portfolio_migration_cli.py` — migration preflight/apply command.

### Existing files to modify or remove

- `src/stock_recommender/parameters.py` — schema v6 plus strategy-scoped policy blocks.
- `src/stock_recommender/context.py` — provide one analyzed-universe snapshot to both signal sides.
- `src/stock_recommender/recommendation.py` — carry immutable signed signal candidates without parsing report text.
- `src/stock_recommender/tracking.py` — pass recommendation facts to `PortfolioEngine`.
- `src/stock_recommender/cli.py` — call new monitor/report service.
- `src/stock_recommender/portfolio_backtest.py` — replay through `PortfolioEngine`.
- `src/stock_recommender/backtest.py` — borrow/financing metadata and approval checks.
- `src/stock_recommender/admin.py` — policy API, portfolio performance, and migration health.
- `src/stock_recommender/reports.py` — direction-aware report content.
- `src/stock_recommender/delivery.py` — no scheduling change; notifications consume new fields.
- `src/stock_recommender/portfolio.py` — delete after all imports move.
- `src/stock_recommender/portfolio_pipeline.py` — delete after stages move.
- `frontend/src/lib/api.ts` — policy, position, exposure, margin, and event types.
- `frontend/src/App.tsx` — independent exposure/margin/short policy editor.
- `frontend/public/performance.html` — long/short strategy-performance view.
- `src/stock_recommender/web/performance.html` — rebuilt static page.
- `pyproject.toml` — migration CLI and optional integration dependency.
- `docs/strategy-portfolio-pipeline.md` — document the v2 engine and operating commands.

## Task 1: Freeze baseline and add architecture guards

**Files:**
- Modify: `tests/test_architecture.py`
- Create: `tests/test_portfolio_engine_architecture.py`

- [ ] **Step 1: Add a failing package-boundary test**

```python
class PortfolioEngineArchitectureTests(unittest.TestCase):
    def test_domain_modules_import_without_portfolio_facade(self):
        modules = (
            "contracts", "config", "ports", "signal_ports", "short_signal",
            "target_pipeline", "exposure", "borrow", "margin", "risk",
            "execution", "valuation", "ledger", "service",
        )
        for name in modules:
            with self.subTest(name=name):
                self.assertIsNotNone(
                    importlib.import_module(f"stock_recommender.portfolio_engine.{name}")
                )

    def test_engine_modules_do_not_import_rendering_or_http_layers(self):
        forbidden = {"reports", "admin", "delivery", "tracking"}
        root = ROOT / "src" / "stock_recommender" / "portfolio_engine"
        for source in root.glob("*.py"):
            text = source.read_text(encoding="utf-8")
            for module in forbidden:
                self.assertNotIn(f"stock_recommender.{module}", text)
                self.assertNotIn(f"from ..{module}", text)
```

- [ ] **Step 2: Run the new test and verify it fails**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_engine_architecture -v`

Expected: FAIL because `stock_recommender.portfolio_engine` does not exist.

- [ ] **Step 3: Create the package skeleton with explicit exports**

Create each mapped module with `from __future__ import annotations`; until the contracts and service exist, keep `__init__.py` limited to:

```python
"""Public PortfolioEngine package; exports are added with the implemented service."""

__all__: list[str] = []
```

Each not-yet-populated module must contain a module docstring describing its one responsibility and no imports from rendering, HTTP, scheduling, or persistence callers.

- [ ] **Step 4: Run architecture and existing baseline tests**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_architecture tests.test_portfolio_engine_architecture -v`

Expected: PASS.

- [ ] **Step 5: Commit the boundary**

```bash
git add src/stock_recommender/portfolio_engine tests/test_architecture.py tests/test_portfolio_engine_architecture.py
git commit -m "Add portfolio engine domain boundary"
```

## Task 2: Add immutable contracts and strategy policy validation

**Files:**
- Modify: `src/stock_recommender/portfolio_engine/contracts.py`
- Modify: `src/stock_recommender/portfolio_engine/config.py`
- Modify: `src/stock_recommender/portfolio_engine/ports.py`
- Modify: `src/stock_recommender/parameters.py`
- Create: `tests/test_portfolio_engine_config.py`
- Modify: `tests/test_parameters.py`

- [ ] **Step 1: Write failing contract and hard-limit tests**

```python
class PortfolioEngineConfigTests(unittest.TestCase):
    def test_existing_strategy_normalizes_to_explicit_long_only(self):
        strategy = normalize_strategy_config({"name": "旧策略", "parameters": {}})
        self.assertEqual(strategy["exposure_policy"]["mode"], "LONG_ONLY")
        self.assertEqual(strategy["exposure_policy"]["max_positions"], 10)
        self.assertEqual(strategy["margin_policy"]["maintenance_margin_pct"], 30.0)

    def test_policy_cannot_exceed_system_hard_limits(self):
        policy = normalize_exposure_policy({
            "mode": "LONG_SHORT",
            "max_positions": 99,
            "max_gross_exposure_pct": 500,
            "max_net_exposure_pct": 500,
            "max_short_exposure_pct": 100,
        })
        self.assertEqual(policy["max_positions"], 10)
        self.assertEqual(policy["max_gross_exposure_pct"], 150.0)
        self.assertEqual(policy["max_net_exposure_pct"], 120.0)
        self.assertEqual(policy["max_short_exposure_pct"], 30.0)

    def test_long_only_effective_caps_are_one_hundred_percent(self):
        effective = effective_exposure_policy(normalize_exposure_policy({
            "mode": "LONG_ONLY",
            "max_gross_exposure_pct": 150,
            "max_net_exposure_pct": 120,
        }))
        self.assertEqual(effective.max_gross_exposure_pct, 100.0)
        self.assertEqual(effective.max_net_exposure_pct, 100.0)
        self.assertEqual(effective.max_short_exposure_pct, 0.0)

    def test_non_us_strategy_cannot_enable_leverage_or_short(self):
        for mode in ("LONG_LEVERAGED", "LONG_SHORT"):
            with self.subTest(mode=mode), self.assertRaises(StrategyPolicyError):
                validate_strategy_policies({"market": "cn", "exposure_policy": {"mode": mode}})
```

- [ ] **Step 2: Run the policy tests and verify they fail**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_engine_config tests.test_parameters -v`

Expected: FAIL because policy normalizers and contracts are missing.

- [ ] **Step 3: Implement exact contracts and policy defaults**

Define frozen enums/dataclasses in `contracts.py`:

```python
class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class PositionEffect(str, Enum):
    OPEN = "OPEN"
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"

@dataclass(frozen=True)
class SignalCandidate:
    symbol: str
    side: PositionSide
    score: float
    requested_weight_pct: float
    model_id: str
    thesis_id: str
    facts: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class TargetPosition:
    symbol: str
    side: PositionSide
    target_weight_pct: float
    signal_score: float
    model_id: str
    thesis_id: str

@dataclass(frozen=True)
class OrderIntent:
    id: str
    symbol: str
    position_side: PositionSide
    order_side: OrderSide
    position_effect: PositionEffect
    quantity: int
    reason: str
    created_snapshot_id: str

    @property
    def increases_risk(self) -> bool:
        return self.position_effect in {PositionEffect.OPEN, PositionEffect.INCREASE}

@dataclass(frozen=True)
class MarketSnapshot:
    id: str
    occurred_at: datetime
    quotes: Mapping[str, Mapping[str, Any]]

@dataclass(frozen=True)
class ExecutionFill:
    intent_id: str
    symbol: str
    quantity: int
    price: float
    fees: float
    status: str

@dataclass(frozen=True)
class PortfolioEvent:
    id: str
    type: str
    occurred_at: datetime
    data: Mapping[str, Any]

@dataclass(frozen=True)
class DecisionBatch:
    run_key: str
    strategy_id: str
    strategy_revision: int
    portfolio_snapshot_id: str
    market_snapshot_id: str
    intents: tuple[OrderIntent, ...] = ()
    fills: tuple[ExecutionFill, ...] = ()
    events: tuple[PortfolioEvent, ...] = ()
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    stage_outputs: tuple[StageOutput, ...] = ()

    @property
    def diagnostic_codes(self) -> tuple[str, ...]:
        return tuple(
            str(item["code"])
            for item in self.diagnostics
            if item.get("code")
        )
```

Add `ExposurePolicy`, `MarginPolicy`, and `ShortPolicy` frozen dataclasses. Define `QuoteProvider`, `BorrowProvider`, `EventCalendarProvider`, `LedgerStore`, and a future-only `BrokerExecutionPort.submit(intents) -> tuple[ExecutionFill, ...]` protocol in `ports.py`; the simulator must not instantiate or call the broker port. Implement config normalizers with these hard limits:

```python
SYSTEM_MAX_POSITIONS = 10
SYSTEM_MAX_GROSS_EXPOSURE_PCT = 150.0
SYSTEM_MAX_NET_EXPOSURE_PCT = 120.0
SYSTEM_MAX_LONG_EXPOSURE_PCT = 120.0
SYSTEM_MAX_SHORT_EXPOSURE_PCT = 30.0
SYSTEM_MAX_LONG_POSITION_PCT = 15.0
SYSTEM_MAX_SHORT_POSITION_PCT = 5.0
VALID_EXPOSURE_MODES = {"LONG_ONLY", "LONG_LEVERAGED", "LONG_SHORT"}
```

Defaults must exactly match the approved design. Increment `STRATEGY_STORE_VERSION` from 5 to 6 and include normalized `exposure_policy`, `margin_policy`, and `short_policy` in every strategy payload. Only US strategies may select `LONG_LEVERAGED` or `LONG_SHORT` in this release; all other markets return `StrategyPolicyError` and remain `LONG_ONLY`. A change to any of the three blocks must invalidate `last_backtest`, reset the approval gate, and return the strategy to `draft` through the same revision path used by other strategy changes.

- [ ] **Step 4: Run config and full parameter tests**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_engine_config tests.test_parameters -v`

Expected: PASS.

- [ ] **Step 5: Commit contracts and policy validation**

```bash
git add src/stock_recommender/portfolio_engine/contracts.py src/stock_recommender/portfolio_engine/config.py src/stock_recommender/portfolio_engine/ports.py src/stock_recommender/parameters.py tests/test_portfolio_engine_config.py tests/test_parameters.py
git commit -m "Add strategy exposure margin and short policies"
```

## Task 3: Implement signal ports and `short_trend_breakdown_v1`

**Files:**
- Modify: `src/stock_recommender/portfolio_engine/contracts.py`
- Modify: `src/stock_recommender/portfolio_engine/ports.py`
- Modify: `src/stock_recommender/portfolio_engine/signal_ports.py`
- Modify: `src/stock_recommender/portfolio_engine/short_signal.py`
- Create: `tests/test_short_signal.py`

- [ ] **Step 1: Write failing short-signal tests**

```python
class ShortTrendBreakdownTests(unittest.TestCase):
    def test_admits_persistent_liquid_breakdown(self):
        row = make_row(
            symbol="WEAK", momentum20=-0.12, momentum60=-0.25,
            price=70, ma20=80, ma60=95, volatility20=0.45,
            turnover=50_000_000, one_day_return=-0.03,
        )
        signals = ShortTrendBreakdownV1().evaluate([row], event_calendar={"WEAK": None})
        self.assertEqual([(item.symbol, item.side.value) for item in signals], [("WEAK", "SHORT")])

    def test_does_not_chase_single_day_crash(self):
        row = make_row(momentum20=-0.12, momentum60=-0.25, one_day_return=-0.14)
        self.assertEqual(ShortTrendBreakdownV1().evaluate([row], event_calendar={"TEST": None}), [])

    def test_blocks_event_window_and_extreme_volatility(self):
        event_row = make_row(symbol="EVENT")
        volatile_row = make_row(symbol="VOL", volatility20=0.81)
        signals = ShortTrendBreakdownV1().evaluate(
            [event_row, volatile_row], event_calendar={"EVENT": 1, "VOL": None}
        )
        self.assertEqual(signals, [])
```

- [ ] **Step 2: Run the short-signal tests and verify they fail**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_short_signal -v`

Expected: FAIL because the model and registry are empty.

- [ ] **Step 3: Implement the port, registry, and deterministic model**

`signal_ports.py` must expose a `SignalModel` protocol with `model_id`, `side`, and `evaluate(rows, event_calendar) -> tuple[SignalCandidate, ...]`, plus `FactorRankLongAdapter`, which converts the existing deterministic `factor_rank_v1` selections into `SignalCandidate(side=LONG)` without reranking them. Register models with this exact duplicate guard:

```python
SIGNAL_MODELS: dict[str, SignalModel] = {}

def register_signal_model(model: SignalModel) -> None:
    if model.model_id in SIGNAL_MODELS:
        raise ValueError(f"重复信号模型：{model.model_id}")
    SIGNAL_MODELS[model.model_id] = model
```

`ShortTrendBreakdownV1.evaluate` must require negative 20/60-day momentum, price below MA20/MA60, 20-day annualized volatility no greater than policy maximum, minimum USD 20 million average daily turnover, no event within two sessions, and a one-day return strictly above -10% so a larger single-day crash is rejected. Rank admitted rows by a normalized combination of negative momentum persistence, distance below MA60, inverse volatility, and liquidity; return at most 10 signals with requested weight 5% and stable `thesis_id` derived from model, symbol, and cutoff date.

- [ ] **Step 4: Run short-signal and existing signal tests**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_short_signal tests.test_signal_engine -v`

Expected: PASS.

- [ ] **Step 5: Commit the independent short model**

```bash
git add src/stock_recommender/portfolio_engine/contracts.py src/stock_recommender/portfolio_engine/ports.py src/stock_recommender/portfolio_engine/signal_ports.py src/stock_recommender/portfolio_engine/short_signal.py tests/test_short_signal.py
git commit -m "Add independent short trend signal model"
```

## Task 4: Build target netting and exposure stages

**Files:**
- Modify: `src/stock_recommender/portfolio_engine/target_pipeline.py`
- Modify: `src/stock_recommender/portfolio_engine/exposure.py`
- Create: `tests/test_target_exposure_pipeline.py`

- [ ] **Step 1: Write failing netting and exposure tests**

```python
class TargetExposurePipelineTests(unittest.TestCase):
    def test_same_symbol_opposing_targets_are_netted(self):
        targets = net_signal_candidates((
            signal("ABC", "LONG", 0.9, 15),
            signal("ABC", "SHORT", 0.8, 5),
        ))
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].side, PositionSide.LONG)
        self.assertEqual(targets[0].target_weight_pct, 10.0)

    def test_long_short_targets_scale_to_all_hard_caps(self):
        raw = tuple(signal(f"L{i}", "LONG", 1 - i / 100, 15) for i in range(10)) + tuple(
            signal(f"S{i}", "SHORT", 0.8 - i / 100, 5) for i in range(6)
        )
        admitted, diagnostic = allocate_exposure(raw, effective_policy("LONG_SHORT"))
        self.assertLessEqual(len(admitted), 10)
        self.assertLessEqual(diagnostic.gross_exposure_pct, 150.0)
        self.assertLessEqual(abs(diagnostic.net_exposure_pct), 120.0)
        self.assertLessEqual(diagnostic.short_exposure_pct, 30.0)

    def test_long_only_rejects_short_targets(self):
        admitted, diagnostic = allocate_exposure(
            (signal("S", "SHORT", 1.0, 5),), effective_policy("LONG_ONLY")
        )
        self.assertEqual(admitted, ())
        self.assertEqual(diagnostic.rejections[0]["reason"], "MODE_DISALLOWS_SHORT")
```

- [ ] **Step 2: Run the pipeline tests and verify they fail**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_target_exposure_pipeline -v`

Expected: FAIL because netting and exposure allocation are not implemented.

- [ ] **Step 3: Implement deterministic pure stages**

Implement `TargetNettingStage` and `ExposureBudgetStage` as `PipelineStage` implementations. Net signed weights by symbol (`LONG` positive, `SHORT` negative); zero removes the target. Sort by descending score then symbol. Enforce per-position caps before portfolio caps. Select at most 10 symbols, then scale each direction pro rata if long, short, gross, or net limits are exceeded. Emit these fact kinds:

```python
{"kind": "net_targets", "items": tuple(TargetPosition, ...)}
{"kind": "exposure_targets", "items": tuple(TargetPosition, ...)}
{"kind": "exposure_diagnostic", "gross_exposure_pct": ..., "net_exposure_pct": ...,
 "long_exposure_pct": ..., "short_exposure_pct": ..., "rejections": (...) }
```

No stage may read or write a ledger.

- [ ] **Step 4: Run target, generic pipeline, and capacity regressions**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_target_exposure_pipeline tests.test_pipeline tests.test_watchlist_capacity -v`

Expected: PASS.

- [ ] **Step 5: Commit target and exposure stages**

```bash
git add src/stock_recommender/portfolio_engine/target_pipeline.py src/stock_recommender/portfolio_engine/exposure.py tests/test_target_exposure_pipeline.py
git commit -m "Add net target and exposure allocation stages"
```

## Task 5: Implement multi-side valuation and account invariants

**Files:**
- Modify: `src/stock_recommender/portfolio_engine/contracts.py`
- Modify: `src/stock_recommender/portfolio_engine/valuation.py`
- Create: `tests/test_portfolio_valuation.py`

- [ ] **Step 1: Write failing accounting tests**

```python
class PortfolioValuationTests(unittest.TestCase):
    def test_short_profit_and_loss_have_correct_direction(self):
        account = short_account(entry=100, quantity=10, restricted_proceeds=1000, cash=500)
        down = value_account(account, prices={"SHORT": 80})
        up = value_account(account, prices={"SHORT": 120})
        self.assertEqual(down.positions[0].unrealized_pnl, 200.0)
        self.assertEqual(up.positions[0].unrealized_pnl, -200.0)

    def test_equity_formula_does_not_double_count_short_proceeds(self):
        account = mixed_account(cash=100, restricted=1000, long_value=600, short_liability=900, loan=200)
        metrics = value_account(account, prices={"LONG": 600, "SHORT": 900}).metrics
        self.assertEqual(metrics.equity, 600.0)
        self.assertEqual(metrics.gross_exposure_pct, 250.0)

    def test_accrued_cost_is_statistical_after_cash_deduction(self):
        account = cash_account(available_cash=992, accrued_financing_cost=8)
        self.assertEqual(value_account(account, prices={}).metrics.equity, 992.0)
```

- [ ] **Step 2: Run valuation tests and verify they fail**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_valuation -v`

Expected: FAIL because account, position, and valuation contracts are missing.

- [ ] **Step 3: Implement account snapshots and formulas**

Add frozen `PositionSnapshot`, `AccountSnapshot`, and `PortfolioMetrics`. Position quantity is always a positive integer and direction is explicit. Implement:

```python
def position_market_value(position: PositionSnapshot, price: float) -> float:
    return position.quantity * price

def position_unrealized_pnl(position: PositionSnapshot, price: float) -> float:
    direction = 1.0 if position.side is PositionSide.LONG else -1.0
    return direction * (price - position.average_cost) * position.quantity

def account_equity(account: AccountSnapshot, long_value: float, short_liability: float) -> float:
    return (
        account.available_cash
        + account.restricted_short_proceeds
        + long_value
        - short_liability
        - account.margin_loan
    )
```

Calculate gross as `(long_value + short_liability) / equity`, net as `(long_value - short_liability) / equity`, and margin rate as `equity / (long_value + short_liability)`. Return infinity when gross market value is zero. Reject negative quantities, negative restricted proceeds, negative loans, duplicate symbols, and simultaneous long/short positions for the same symbol.

- [ ] **Step 4: Run valuation and property-style invariant loops**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_valuation -v`

Expected: PASS, including a loop over at least 100 deterministic price/quantity combinations.

- [ ] **Step 5: Commit valuation core**

```bash
git add src/stock_recommender/portfolio_engine/contracts.py src/stock_recommender/portfolio_engine/valuation.py tests/test_portfolio_valuation.py
git commit -m "Add long short portfolio valuation core"
```

## Task 6: Add borrow and margin admission

**Files:**
- Modify: `src/stock_recommender/portfolio_engine/borrow.py`
- Modify: `src/stock_recommender/portfolio_engine/margin.py`
- Create: `tests/test_borrow_margin.py`

- [ ] **Step 1: Write failing local-degradation and margin tests**

```python
class BorrowMarginTests(unittest.TestCase):
    def test_missing_borrow_data_blocks_only_new_shorts(self):
        result = admit_borrow(
            targets=(target("LONG", "L", 10), target("SHORT", "S", 5)),
            snapshot=BorrowSnapshot(status="UNAVAILABLE", securities={}),
            policy=default_short_policy(),
            existing_positions=(),
        )
        self.assertEqual([item.symbol for item in result.admitted], ["L"])
        self.assertEqual(result.rejections[0].reason, "BORROW_DATA_MISSING")

    def test_existing_short_becomes_cover_only_when_borrow_revoked(self):
        result = admit_borrow(
            targets=(), snapshot=borrow_snapshot("S", shortable=False),
            policy=default_short_policy(), existing_positions=(short_position("S"),),
        )
        self.assertEqual(result.position_modes["S"], "COVER_ONLY")

    def test_projected_margin_below_buffer_rejects_risk_increase(self):
        result = admit_margin(overleveraged_account(margin_rate_pct=39), risk_increasing_intent())
        self.assertFalse(result.admitted)
        self.assertEqual(result.reason, "MARGIN_BUFFER_BREACH")
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_borrow_margin -v`

Expected: FAIL because admission functions do not exist.

- [ ] **Step 3: Implement immutable admission results**

Add these frozen contracts before implementing the stages:

```python
@dataclass(frozen=True)
class BorrowSecurity:
    symbol: str
    shortable: bool
    easy_to_borrow: bool
    borrow_apr_pct: float | None

@dataclass(frozen=True)
class BorrowSnapshot:
    id: str
    status: str
    securities: Mapping[str, BorrowSecurity]

    @classmethod
    def unavailable(cls, snapshot_id: str = "borrow:unavailable") -> "BorrowSnapshot":
        return cls(id=snapshot_id, status="UNAVAILABLE", securities={})
```

`BorrowAdmissionStage` must pass all long targets, require both `shortable` and `easy_to_borrow` for new/increased shorts, and set an existing short to `COVER_ONLY` when either flag becomes false. Missing snapshot data follows `block_on_borrow_data_missing` and never invents a fee.

`MarginAdmissionStage` must project the post-trade account, calculate equity and gross market value with `valuation.py`, and apply:

```python
buffer_threshold = maintenance_margin_pct + liquidation_buffer_pct
if projected.margin_rate_pct < maintenance_margin_pct:
    state = "MARGIN_CALL"
elif projected.margin_rate_pct < buffer_threshold:
    state = "REDUCE_ONLY"
else:
    state = "NORMAL"
```

`REDUCE_ONLY` and `MARGIN_CALL` reject only risk-increasing intents. Return required margin, available buying power, difference, state, and stable rejection code.

- [ ] **Step 4: Run borrow, margin, and valuation tests**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_borrow_margin tests.test_portfolio_valuation -v`

Expected: PASS.

- [ ] **Step 5: Commit borrow and margin admission**

```bash
git add src/stock_recommender/portfolio_engine/borrow.py src/stock_recommender/portfolio_engine/margin.py tests/test_borrow_margin.py
git commit -m "Add borrow and margin admission stages"
```

## Task 7: Add direction-aware risk and forced deleveraging

**Files:**
- Modify: `src/stock_recommender/portfolio_engine/risk.py`
- Create: `tests/test_portfolio_risk_v2.py`

- [ ] **Step 1: Write failing direction and margin-call tests**

```python
class PortfolioRiskV2Tests(unittest.TestCase):
    def test_short_stop_loss_triggers_after_six_percent_rise(self):
        decisions = evaluate_position_risk(short_position(entry=100, current=106.1), default_policies())
        self.assertEqual(decisions[0].reason, "SHORT_STOP_LOSS")
        self.assertEqual(decisions[0].position_effect, PositionEffect.CLOSE)

    def test_short_trailing_profit_covers_after_four_percent_rebound(self):
        position = short_position(entry=100, current=83.2, trough=80, trailing_active=True)
        self.assertEqual(evaluate_position_risk(position, default_policies())[0].reason, "SHORT_TRAILING_STOP")

    def test_squeeze_signal_sets_cover_only(self):
        decision = evaluate_squeeze(short_position(), quote(percent=10.1, volume_ratio=3.1), default_short_policy())
        self.assertEqual(decision.position_mode, "COVER_ONLY")

    def test_margin_call_orders_release_margin_until_safe(self):
        intents = plan_forced_deleveraging(margin_call_account(), prices=margin_call_prices())
        self.assertTrue(intents)
        self.assertTrue(all(not intent.increases_risk for intent in intents))
        self.assertEqual(intents[0].reason, "MARGIN_CALL")
```

- [ ] **Step 2: Run risk tests and verify they fail**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_risk_v2 -v`

Expected: FAIL because v2 risk functions are missing.

- [ ] **Step 3: Implement deterministic exit and deleveraging decisions**

Long exits retain 8% fixed stop and 10%/5% trailing rules. Short exits use 6% fixed stop and 8%/4% trailing rules. Borrow revocation or a quote with at least 10% rise and 3x volume ratio sets `COVER_ONLY`. Portfolio drawdown states retain 12% warning, 14% derisk, and 15% manual halt, but consume `PortfolioMetrics.equity`.

Forced-deleveraging candidates must be sorted by:

```python
key = (
    -candidate.margin_released,
    -candidate.risk_contribution,
    candidate.estimated_transaction_cost,
    candidate.symbol,
)
```

After each proposed reduction, revalue the projected account and stop once margin rate reaches the 40% buffer. Equity at or below zero returns `INSOLVENT_HALT` and no new intent.

- [ ] **Step 4: Run v2 and existing risk regressions**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_risk_v2 tests.test_portfolio -v`

Expected: v2 tests PASS; existing tests remain PASS until their caller migration task.

- [ ] **Step 5: Commit risk engine**

```bash
git add src/stock_recommender/portfolio_engine/risk.py tests/test_portfolio_risk_v2.py
git commit -m "Add direction aware portfolio risk engine"
```

## Task 8: Add order-intent planning, fills, and financing accrual

**Files:**
- Modify: `src/stock_recommender/portfolio_engine/execution.py`
- Create: `tests/test_portfolio_execution_v2.py`

- [ ] **Step 1: Write failing intent/fill/accrual tests**

```python
class PortfolioExecutionV2Tests(unittest.TestCase):
    def test_order_semantics_are_unambiguous(self):
        self.assertEqual(intent_for_delta(None, target("SHORT", "S", 5)).semantic_tuple(),
                         ("SHORT", "SELL", "OPEN"))
        self.assertEqual(intent_for_delta(short_position("S"), no_target("S")).semantic_tuple(),
                         ("SHORT", "BUY", "CLOSE"))

    def test_partial_short_fill_respects_volume_limit(self):
        fill = simulate_fill(short_open_intent(quantity=1000), quote(price=20, bar_volume=2000),
                             max_participation_pct=5)
        self.assertEqual(fill.quantity, 100)
        self.assertEqual(fill.status, "PARTIAL")

    def test_daily_cost_accrual_is_idempotent(self):
        first = accrue_carry_costs(leveraged_short_account(), as_of=date(2026, 7, 31))
        repeated = accrue_carry_costs(first.account, as_of=date(2026, 7, 31))
        self.assertGreater(first.financing_cost + first.borrow_cost, 0)
        self.assertEqual(repeated.financing_cost + repeated.borrow_cost, 0)
```

- [ ] **Step 2: Run execution tests and verify they fail**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_execution_v2 -v`

Expected: FAIL because intent and fill functions are missing.

- [ ] **Step 3: Implement intent planning and simulation**

Map deltas exactly:

| Existing | Target | Intent |
|---|---|---|
| none | long | `LONG/BUY/OPEN` |
| long smaller than target | long | `LONG/BUY/INCREASE` |
| long larger than target | long | `LONG/SELL/REDUCE` |
| long | none | `LONG/SELL/CLOSE` |
| none | short | `SHORT/SELL/OPEN` |
| short smaller than target | short | `SHORT/SELL/INCREASE` |
| short larger than target | short | `SHORT/BUY/REDUCE` |
| short | none | `SHORT/BUY/CLOSE` |

Use the existing market adapter lot size, maximum bar participation, commission, transfer/stamp rules, and slippage. Short-open proceeds are restricted; short-close releases proportional restricted proceeds. Leveraged long fills create or reduce `margin_loan` only after available cash is consumed. Accrue financing on `margin_loan` and borrow fees on short liability using actual elapsed calendar days divided by 365, keyed by account, cost type, and date.

- [ ] **Step 4: Run execution and partial-fill regressions**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_execution_v2 tests.test_portfolio_t_plus_one_regression -v`

Expected: PASS.

- [ ] **Step 5: Commit execution core**

```bash
git add src/stock_recommender/portfolio_engine/execution.py tests/test_portfolio_execution_v2.py
git commit -m "Add long short execution and carry costs"
```

## Task 9: Add schema-v2 ledger and atomic migration

**Files:**
- Modify: `src/stock_recommender/portfolio_engine/contracts.py`
- Modify: `src/stock_recommender/portfolio_engine/ports.py`
- Modify: `src/stock_recommender/portfolio_engine/ledger.py`
- Modify: `src/stock_recommender/portfolio_engine/migration.py`
- Create: `src/stock_recommender/portfolio_migration_cli.py`
- Modify: `pyproject.toml`
- Create: `tests/test_portfolio_ledger_v2.py`
- Create: `tests/test_portfolio_migration.py`

- [ ] **Step 1: Write failing ledger and migration tests**

```python
class PortfolioLedgerV2Tests(unittest.TestCase):
    def test_decision_batch_commits_once(self):
        store = JsonLedgerStore(self.path)
        store.commit(batch(run_key="run-1", events=(cash_event(-10),)))
        store.commit(batch(run_key="run-1", events=(cash_event(-10),)))
        self.assertEqual(store.load("strategy").available_cash, 990.0)

class PortfolioMigrationTests(unittest.TestCase):
    def test_dry_run_does_not_write_and_preserves_nav(self):
        self.path.write_text(json.dumps(v1_long_only_store()), encoding="utf-8")
        before = self.path.read_bytes()
        report = migrate_portfolio_store(self.path, apply=False)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(report.nav_parity)

    def test_apply_creates_backup_and_explicit_long_positions(self):
        report = migrate_portfolio_store(self.path, apply=True, now=FIXED_NOW)
        migrated = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["version"], 2)
        self.assertEqual(migrated["accounts"]["tech"]["positions"]["600001"]["side"], "LONG")
        self.assertTrue(report.backup_path.is_file())

    def test_strategy_store_migration_preserves_active_id_and_sets_long_only(self):
        self.strategy_path.write_text(json.dumps(v5_strategy_store()), encoding="utf-8")
        report = migrate_strategy_store(self.strategy_path, apply=True, now=FIXED_NOW)
        migrated = json.loads(self.strategy_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["version"], 6)
        self.assertEqual(migrated["active_strategy_id"], "active-tech")
        self.assertTrue(all(
            item["exposure_policy"]["mode"] == "LONG_ONLY"
            for item in migrated["strategies"]
        ))
        self.assertTrue(report.backup_path.is_file())
```

- [ ] **Step 2: Run ledger/migration tests and verify they fail**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_ledger_v2 tests.test_portfolio_migration -v`

Expected: FAIL because v2 store and migrator do not exist.

- [ ] **Step 3: Implement store protocol, event application, and migrator**

`LedgerStore` exposes `load(strategy_id)`, `commit(DecisionBatch)`, and `list_accounts()`. `JsonLedgerStore.commit` must lock, reload, reject stale snapshot IDs, ignore a previously committed run key, apply all events to a copy, validate invariants, write a temporary file, and atomically replace the ledger.

The migration command must support exactly:

```bash
stock-portfolio-migrate --check
stock-portfolio-migrate --apply
stock-portfolio-migrate --strategy-path /explicit/strategies.json --portfolio-path /explicit/strategy_portfolios.json --check
```

Without explicit paths, resolve the existing strategy and portfolio paths through the same helpers used by the application. Strategy migration preserves IDs, active strategy, revisions, lifecycle state, delivery, signal, allocation, and parameters; it writes all three policy blocks with `LONG_ONLY` and upgrades store schema 5 to 6. Portfolio migration rules: v1 positions become positive quantity plus `side=LONG`; `BUY` orders become `LONG/BUY/OPEN|INCREASE`; `SELL` orders become `LONG/SELL/REDUCE|CLOSE`; `cash` becomes `available_cash`; `reserved_cash` remains explicit; short proceeds and margin loan initialize to zero. Compare pre/post NAV to currency minor-unit precision. `--apply` creates timestamped sibling backups for both stores and aborts before replacing either store on any parity or invariant error. Runtime schema-v6/v2 loading must reject old schemas rather than maintaining compatibility branches.

- [ ] **Step 4: Run ledger, migration, and concurrency tests**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_ledger_v2 tests.test_portfolio_migration tests.test_portfolio -v`

Expected: PASS.

- [ ] **Step 5: Commit ledger and migration tooling**

```bash
git add src/stock_recommender/portfolio_engine/contracts.py src/stock_recommender/portfolio_engine/ports.py src/stock_recommender/portfolio_engine/ledger.py src/stock_recommender/portfolio_engine/migration.py src/stock_recommender/portfolio_migration_cli.py pyproject.toml tests/test_portfolio_ledger_v2.py tests/test_portfolio_migration.py
git commit -m "Add portfolio ledger v2 migration"
```

## Task 10: Orchestrate the PortfolioEngine and migrate callers

**Files:**
- Modify: `src/stock_recommender/portfolio_engine/service.py`
- Modify: `src/stock_recommender/portfolio_engine/__init__.py`
- Modify: `src/stock_recommender/context.py`
- Modify: `src/stock_recommender/recommendation.py`
- Modify: `src/stock_recommender/tracking.py`
- Modify: `src/stock_recommender/cli.py`
- Modify: `tests/test_portfolio.py`
- Modify: `tests/test_cli_portfolio.py`
- Modify: `tests/test_tracking_metrics.py`
- Create: `tests/test_portfolio_engine_service.py`

- [ ] **Step 1: Write failing end-to-end service tests**

```python
class PortfolioEngineServiceTests(unittest.TestCase):
    def test_local_borrow_failure_keeps_long_target(self):
        decision = engine().plan(
            strategy=long_short_strategy(), account=empty_account(),
            long_signals=(signal("L", "LONG", 0.9, 10),),
            short_signals=(signal("S", "SHORT", 0.9, 5),),
            market=market_snapshot("L", "S"), borrow=BorrowSnapshot.unavailable(),
        )
        self.assertEqual([intent.symbol for intent in decision.intents], ["L"])
        self.assertIn("BORROW_DATA_MISSING", decision.diagnostic_codes)

    def test_same_snapshot_cannot_fill_new_intent(self):
        planned = engine().plan_and_commit(inputs(snapshot_id="m1"))
        processed = engine().process_and_commit(inputs(snapshot_id="m1"))
        self.assertEqual(processed.fills, ())
        later = engine().process_and_commit(inputs(snapshot_id="m2"))
        self.assertTrue(later.fills)
```

- [ ] **Step 2: Run service tests and verify they fail**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_engine_service -v`

Expected: FAIL because orchestration methods are not implemented.

- [ ] **Step 3: Implement a thin transaction orchestrator**

`PortfolioEngine` constructor receives signal registry, policy normalizers, quote/borrow/calendar ports, and ledger store.

`context.py` must build one immutable analyzed-universe snapshot and pass it to `PortfolioEngine.evaluate`. The engine runs the registered long and short signal stages internally, followed by netting, exposure, borrow, margin, risk, and rebalance stages, but does not write the ledger. Extend `RecommendationPlan` with the resulting immutable `portfolio_decision: DecisionBatch`; reports render its signed signal facts, and `tracking.save_daily_selection` persists and commits that exact decision. No caller may regenerate signals or parse rendered text.

Expose the transaction boundary explicitly:

| Method | Input | Output | Side effect |
|---|---|---|---|
| `evaluate` | `PlanRequest` | `DecisionBatch` | none |
| `commit` | `DecisionBatch` | `AccountSnapshot` | one atomic ledger commit |
| `plan_and_commit` | `PlanRequest` | `DecisionBatch` | equivalent to evaluating then committing once |
| `process_and_commit` | `ProcessRequest` | `DecisionBatch` | one atomic ledger commit |
| `performance` | strategy ID and `MarketSnapshot` | `PortfolioSnapshot` | none |

Define request/response contracts in `contracts.py` with complete inputs:

```python
@dataclass(frozen=True)
class PlanRequest:
    run_key: str
    strategy: Mapping[str, Any]
    account: AccountSnapshot
    analyzed_rows: tuple[Mapping[str, Any], ...]
    market: MarketSnapshot
    borrow: BorrowSnapshot
    event_calendar: Mapping[str, int | None]

@dataclass(frozen=True)
class ProcessRequest:
    run_key: str
    strategy: Mapping[str, Any]
    account: AccountSnapshot
    market: MarketSnapshot
    borrow: BorrowSnapshot

@dataclass(frozen=True)
class PortfolioSnapshot:
    account: AccountSnapshot
    metrics: PortfolioMetrics
    positions: tuple[PositionSnapshot, ...]
    open_intents: tuple[OrderIntent, ...]
    recent_events: tuple[PortfolioEvent, ...]
```

`evaluate` is pure with respect to the ledger. `plan_and_commit` is a convenience for non-rendering callers and must be equivalent to `commit(evaluate(request))`. `process_and_commit` fills only intents created before the current market snapshot, marks positions, accrues carry costs once per day, evaluates exits and margin state, and commits one batch. CLI `track` and `risk` modes call engine services and render returned snapshots/events.

- [ ] **Step 4: Run service and migrated caller tests**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_engine_service tests.test_portfolio tests.test_cli_portfolio tests.test_tracking_metrics -v`

Expected: PASS.

- [ ] **Step 5: Commit service integration**

```bash
git add src/stock_recommender/portfolio_engine/service.py src/stock_recommender/portfolio_engine/__init__.py src/stock_recommender/context.py src/stock_recommender/recommendation.py src/stock_recommender/tracking.py src/stock_recommender/cli.py tests/test_portfolio_engine_service.py tests/test_portfolio.py tests/test_cli_portfolio.py tests/test_tracking_metrics.py
git commit -m "Route portfolio workflows through PortfolioEngine"
```

## Task 11: Reuse the engine in backtests and add short-data gates

**Files:**
- Modify: `src/stock_recommender/portfolio_backtest.py`
- Modify: `src/stock_recommender/backtest.py`
- Modify: `tests/test_backtest.py`
- Create: `tests/test_long_short_backtest.py`

- [ ] **Step 1: Write failing parity and cost-stress tests**

```python
class LongShortBacktestTests(unittest.TestCase):
    def test_paper_and_backtest_same_snapshots_produce_same_events_and_nav(self):
        paper = replay_with_engine(mode="paper", snapshots=FIXTURE_SNAPSHOTS)
        backtest = replay_with_engine(mode="backtest", snapshots=FIXTURE_SNAPSHOTS)
        self.assertEqual(paper.event_fingerprints, backtest.event_fingerprints)
        self.assertAlmostEqual(paper.final_nav, backtest.final_nav, places=6)

    def test_double_financing_and_borrow_cost_stress_is_reported(self):
        result = replay_long_short(cost_multiplier=2.0)
        self.assertGreater(result.metrics["financing_cost"], 0)
        self.assertGreater(result.metrics["borrow_cost"], 0)
        self.assertEqual(result.metadata["borrow_cost_estimated"], True)

    def test_missing_historical_borrow_capability_fails_short_live_gate(self):
        gate = approval_gate(short_backtest(borrow_history_complete=False))
        check = next(item for item in gate["checks"] if item["id"] == "borrow_history")
        self.assertFalse(check["passed"])
```

- [ ] **Step 2: Run backtest tests and verify they fail**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_long_short_backtest tests.test_backtest -v`

Expected: FAIL because the replay still calls the long-only functions.

- [ ] **Step 3: Replace replay-specific accounting with engine snapshots**

Build `PlanRequest` at the signal cutoff, `ProcessRequest` at entry and close snapshots, and use an in-memory `LedgerStore`. Remove `_liquidation_nav`; call `valuation.value_account`. Backtest metadata must include financing APR, borrow APR, cost multiplier, `borrow_cost_estimated`, and `borrow_history_complete`. Add an approval check named `borrow_history` that is required only for `LONG_SHORT`; estimated fees are allowed in draft/Paper research but cannot pass a future live gate.

- [ ] **Step 4: Run backtest, monthly replay, and parity tests**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_long_short_backtest tests.test_backtest tests.test_month_pipeline_integration -v`

Expected: PASS.

- [ ] **Step 5: Commit shared backtest execution**

```bash
git add src/stock_recommender/portfolio_backtest.py src/stock_recommender/backtest.py tests/test_backtest.py tests/test_long_short_backtest.py
git commit -m "Replay long short backtests through PortfolioEngine"
```

## Task 12: Remove the old mixed portfolio implementation

**Files:**
- Delete: `src/stock_recommender/portfolio.py`
- Delete: `src/stock_recommender/portfolio_pipeline.py`
- Modify: all remaining importers identified by the repository content search
- Modify: `tests/test_architecture.py`
- Modify: `tests/test_portfolio_engine_architecture.py`

- [ ] **Step 1: Add a failing removal guard**

```python
def test_legacy_portfolio_modules_are_removed(self):
    root = ROOT / "src" / "stock_recommender"
    self.assertFalse((root / "portfolio.py").exists())
    self.assertFalse((root / "portfolio_pipeline.py").exists())
    self.assertIsNone(importlib.util.find_spec("stock_recommender.portfolio"))
    self.assertIsNone(importlib.util.find_spec("stock_recommender.portfolio_pipeline"))
```

- [ ] **Step 2: Run the guard and verify it fails**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_portfolio_engine_architecture -v`

Expected: FAIL because the old files still exist.

- [ ] **Step 3: Move every remaining caller and delete both files**

Use the `Grep` tool with pattern `(?:from|import).*portfolio(?:_pipeline)?` across `src` and `tests` to identify all imports from the two modules. Replace them with `portfolio_engine` contracts/services. Keep no compatibility re-export. Move still-required formatters into `reports.py` and performance projection into `portfolio_engine.service`. Run the same `Grep` search again and delete the two legacy modules only when no old-module import remains.

- [ ] **Step 4: Run the complete Python suite**

Run: `env PYTHONPATH=src:tests python3 -m unittest discover -s tests`

Expected: all tests PASS; the count is greater than the pre-feature baseline of 199.

- [ ] **Step 5: Commit the old-engine removal**

```bash
git add -A src/stock_recommender tests
git commit -m "Remove legacy mixed portfolio engine"
```

## Task 13: Extend API, strategy performance, and Feishu messages

**Files:**
- Modify: `src/stock_recommender/admin.py`
- Modify: `src/stock_recommender/reports.py`
- Modify: `src/stock_recommender/tracking.py`
- Modify: `tests/test_admin_portfolio.py`
- Modify: `tests/test_cli_portfolio.py`
- Create: `tests/test_long_short_notifications.py`

- [ ] **Step 1: Write failing API and notification tests**

```python
class LongShortNotificationTests(unittest.TestCase):
    def test_action_message_contains_direction_exposure_margin_and_link(self):
        message = format_portfolio_actions(long_short_snapshot(), margin_call_events(),
                                           performance_url="http://host/strategies/s1/portfolio")
        self.assertIn("策略：多空测试 · v2", message)
        self.assertIn("空头回补", message)
        self.assertIn("总敞口 145.00%", message)
        self.assertIn("净敞口 85.00%", message)
        self.assertIn("保证金率 38.00%", message)
        self.assertIn("/strategies/s1/portfolio", message)

def test_portfolio_api_exposes_direction_and_account_metrics(self):
    payload = request_json("/api/strategies/s1/portfolio")
    self.assertEqual(payload["positions"][0]["side"], "SHORT")
    self.assertIn("gross_exposure_pct", payload["summary"])
    self.assertIn("borrow_cost", payload["summary"])
```

- [ ] **Step 2: Run API/notification tests and verify they fail**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_admin_portfolio tests.test_long_short_notifications -v`

Expected: FAIL because v2 fields are absent.

- [ ] **Step 3: Add direction-aware payloads and copy**

Strategy GET/PUT payloads must include all three policy blocks. Portfolio summary must include `long_market_value`, `short_liability`, `gross_exposure_pct`, `net_exposure_pct`, `margin_rate_pct`, `buying_power`, `margin_loan`, `financing_cost`, and `borrow_cost`. Position payloads include side, direction-aware return, position mode, borrow rate, margin used, and exit distance. Events expose `MARGIN_CALL`, `COVER_ONLY`, `FINANCING_COST_ACCRUED`, `BORROW_COST_ACCRUED`, and forced-deleverage reasons.

Feishu action and hourly portfolio reports must include strategy name/revision, direction, gross/net exposure, margin rate, cost totals, and performance link. Five-minute risk runs remain silent when no event is emitted.

- [ ] **Step 4: Run API, CLI, notification, and delivery tests**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_admin_portfolio tests.test_cli_portfolio tests.test_long_short_notifications tests.test_delivery -v`

Expected: PASS.

- [ ] **Step 5: Commit product API and notifications**

```bash
git add src/stock_recommender/admin.py src/stock_recommender/reports.py src/stock_recommender/tracking.py tests/test_admin_portfolio.py tests/test_cli_portfolio.py tests/test_long_short_notifications.py
git commit -m "Expose long short portfolio metrics and alerts"
```

## Task 14: Add the independent backend configuration UI and performance view

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/index.css`
- Modify: `frontend/public/performance.html`
- Modify after build: `src/stock_recommender/web/index.html`
- Modify after build: `src/stock_recommender/web/performance.html`
- Modify after build: `src/stock_recommender/web/assets/*`
- Create: `tests/test_performance_page_long_short.py`

- [ ] **Step 1: Add failing static-page assertions**

```python
class PerformancePageLongShortTests(unittest.TestCase):
    def test_page_renders_direction_and_margin_fields(self):
        script = performance_html().read_text(encoding="utf-8")
        for field in (
            "gross_exposure_pct", "net_exposure_pct", "margin_rate_pct",
            "financing_cost", "borrow_cost", "short_liability",
        ):
            self.assertIn(field, script)
        self.assertIn("position.side", script)
```

- [ ] **Step 2: Run page test and verify it fails**

Run: `env PYTHONPATH=src:tests python3 -m unittest tests.test_performance_page_long_short -v`

Expected: FAIL because the fields are not rendered.

- [ ] **Step 3: Implement typed policy editors and performance cards**

Add TypeScript interfaces matching the backend policy blocks. Add an independent “多空与杠杆” settings section with mode select and numeric fields. Show the system hard limit beside each field and disable short-only inputs unless mode is `LONG_SHORT`. Before saving a change from `LONG_ONLY`, display a confirmation that a new revision is created and backtest/Paper approval is reset.

The strategy-performance page must add cards for long value, short liability, gross exposure, net exposure, margin rate, buying power, financing cost, and borrow cost. Add a direction badge to positions and semantic labels for margin-call/cover-only events. For non-US strategies, the mode selector is fixed to `LONG_ONLY` and explains that market-specific financing support is not connected. Keep the page responsive at 390px width.

- [ ] **Step 4: Build, lint, copy artifacts, and run page tests**

Run:

```bash
cd frontend
npm run lint
npm run build
cd ..
env PYTHONPATH=src:tests python3 -m unittest tests.test_performance_page_long_short tests.test_performance_page_regression_1 -v
```

Expected: ESLint PASS, TypeScript/Vite build PASS, page tests PASS. `frontend/vite.config.ts` already writes directly to `src/stock_recommender/web/`; do not manually edit hashed asset files.

- [ ] **Step 5: Commit frontend and built assets**

```bash
git add frontend src/stock_recommender/web tests/test_performance_page_long_short.py
git commit -m "Add long short strategy controls and performance view"
```

## Task 15: Add isolated PostgreSQL month replay and final verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/stock_recommender/portfolio_engine/ports.py`
- Modify: `src/stock_recommender/portfolio_engine/postgres_store.py`
- Create: `tests/postgres_integration.py`
- Create: `tests/test_month_long_short_integration.py`
- Modify: `docs/strategy-portfolio-pipeline.md`

- [ ] **Step 1: Write an integration test that requires an isolated schema**

```python
@unittest.skipUnless(os.getenv("STOCK_AGENT_TEST_DATABASE_URL"), "requires Docker PostgreSQL")
class MonthLongShortIntegrationTests(unittest.TestCase):
    def test_one_month_replay_is_isolated_and_respects_all_caps(self):
        with isolated_postgres_schema(os.environ["STOCK_AGENT_TEST_DATABASE_URL"]) as schema:
            store = PostgresLedgerStore(os.environ["STOCK_AGENT_TEST_DATABASE_URL"], schema=schema)
            result = replay_22_sessions(store=store, strategy=long_short_strategy())
            self.assertEqual(result.sessions, 22)
            self.assertLessEqual(result.maximum_positions, 10)
            self.assertLessEqual(result.maximum_gross_exposure_pct, 150.0)
            self.assertLessEqual(abs(result.maximum_net_exposure_pct), 120.0)
            self.assertLessEqual(result.maximum_short_exposure_pct, 30.0)
            self.assertTrue(result.paper_backtest_parity)
        self.assertFalse(schema_exists(os.environ["STOCK_AGENT_TEST_DATABASE_URL"], schema))
```

- [ ] **Step 2: Run without a DB and confirm an explicit skip, then run against Docker PG and verify failure**

Run without DB:

`env PYTHONPATH=src:tests python3 -m unittest tests.test_month_long_short_integration -v`

Expected: SKIP with `requires Docker PostgreSQL`.

Run against the existing Docker PostgreSQL instance:

```bash
python3 -m pip install -e '.[integration]'
env STOCK_AGENT_TEST_DATABASE_URL="$STOCK_AGENT_TEST_DATABASE_URL" PYTHONPATH=src:tests python3 -m unittest tests.test_month_long_short_integration -v
```

Expected before implementation: FAIL because the optional store and schema fixture are incomplete.

- [ ] **Step 3: Implement the optional store and guaranteed cleanup**

Add `integration = ["psycopg[binary]>=3.2"]` to optional dependencies. `isolated_postgres_schema` creates `stock_agent_test_<uuid>`, creates account/event tables inside it, and drops the schema with `CASCADE` in `finally`, including when the test raises. `PostgresLedgerStore` implements the same `LedgerStore` protocol and uses a transaction plus unique `(strategy_id, run_key)` constraint for idempotency. Import Psycopg only inside the adapter so production JSON deployments gain no runtime dependency.

Populate 22 sessions of deterministic long, short, missing-borrow, squeeze, partial-fill, financing, borrow-cost, and margin-buffer scenarios. Compare each day’s event fingerprints and NAV between in-memory backtest and Paper orchestration.

- [ ] **Step 4: Run every verification layer**

Run:

```bash
env PYTHONPATH=src:tests python3 -m unittest discover -s tests
env STOCK_AGENT_TEST_DATABASE_URL="$STOCK_AGENT_TEST_DATABASE_URL" PYTHONPATH=src:tests python3 -m unittest tests.test_month_long_short_integration -v
python3 -m compileall -q src tests
git diff --check
cd frontend && npm run lint && npm run build
```

Expected: all Python tests PASS, PostgreSQL month replay PASS, compileall PASS, diff check PASS, ESLint PASS, and Vite build PASS.

- [ ] **Step 5: Document operations and commit integration coverage**

Document migration preflight/apply, policy modes, hard limits, borrow-data degradation, isolated PostgreSQL test command, rollback from timestamped backup, and the rule that deployment leaves existing strategies `LONG_ONLY`.

```bash
git add pyproject.toml src/stock_recommender/portfolio_engine/ports.py src/stock_recommender/portfolio_engine/postgres_store.py tests/postgres_integration.py tests/test_month_long_short_integration.py docs/strategy-portfolio-pipeline.md
git commit -m "Add isolated long short month integration coverage"
```

## Task 16: Deployment preflight and dormant rollout

**Files:**
- Modify only if verification finds a concrete issue; otherwise no code changes.

- [ ] **Step 1: Verify repository and commit state**

Run:

```bash
git status --short --branch
git log --oneline --decorate -20
```

Expected: clean worktree and all tasks committed on the feature branch.

- [ ] **Step 2: Back up and dry-run production migration**

On the deployment host, back up the application directory, strategy store, and portfolio ledger. Run:

```bash
stock-portfolio-migrate --check
```

Expected: schema-v1 recognized, every account migratable, NAV parity true, no production file changed.

- [ ] **Step 3: Deploy code with strategy features dormant**

Deploy the release artifact, install only normal runtime dependencies, run the atomic ledger migration, restart the user-level service, and verify `/api/health`. Do not modify any strategy mode during deployment.

Expected: service healthy; every strategy payload has `exposure_policy.mode=LONG_ONLY`; current active strategy ID is unchanged.

- [ ] **Step 4: Run isolated production-host verification**

From `/tmp`, with project source/tests on `PYTHONPATH`, run the complete suite so production `.env` is not loaded. Run the PostgreSQL month test against a unique schema. Verify strategy page HTTP 200 and no new portfolio event was written by tests. Invoke the `/gstack-qa` skill against the deployed strategy page and require desktop plus 390px mobile coverage, health score 100, zero console errors, and no horizontal overflow. Run `hermes cron list` and verify job IDs, schedules, delivery targets, and active state are unchanged.

- [ ] **Step 5: Push the branch and record handoff**

```bash
git push origin codex/add-us-stock
```

Record deployed commit, backup paths, migration report, service timestamp, Python totals, frontend results, browser QA result, active strategy ID, and confirmation that leverage/short remain disabled until a strategy revision explicitly enables them.
