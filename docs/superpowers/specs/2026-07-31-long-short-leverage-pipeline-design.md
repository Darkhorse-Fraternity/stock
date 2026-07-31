# 多空与杠杆组合 Pipeline 设计

## 1. 目标

在现有策略模拟组合和组合回测中支持杠杆做多与做空，同时保持信号、目标仓位、敞口、保证金、借券、风控、成交、估值和账本彼此解耦。线上 Paper 与回测必须复用同一个组合执行核心，避免出现两套成交或净值逻辑。

本期不连接真实券商。系统只提供 `BrokerExecutionPort` 契约，为后续接入 Alpaca Paper 或实盘留出边界。

## 2. 当前边界与问题

现有系统已经具备可复用的纯阶段基础：

- `pipeline.py` 提供版本化 `PipelineRunner`、`StageInput` 和 `StageOutput`。
- `portfolio_pipeline.py` 已将候选标准化、市场状态、容量和风险准入拆成纯阶段。
- `portfolio_backtest.py` 复用线上组合函数，具备线上与回测一致性的基础。
- 事件账本、幂等运行键、部分成交、费用、退出和组合回撤闸门已经存在。

现有限制集中在组合领域模型：

- 仓位、订单和估值默认只表达做多。
- `BUY` 与 `SELL` 没有区分开仓、增仓、减仓和回补。
- 现金、订单、成交、估值、风控和持久化集中在 `portfolio.py`。
- 暴露上限被限制在 0% 到 100%，没有总敞口、净敞口和空头敞口的独立概念。
- 回测清算价值只处理多头持仓。
- 没有保证金、融资负债、受限卖空所得、借券费和强平状态。

## 3. 方案选择

### 3.1 未采用：在现有组合文件中增加方向分支

直接在现有订单和仓位函数中增加 `SHORT` 判断，短期改动较少，但会让信号、资金、保证金、借券、成交和账本继续相互依赖。每增加一种资产或券商规则都需要修改同一组核心函数，不符合后续扩展目标。

### 3.2 采用：领域 Pipeline 解耦

保留通用 `PipelineRunner`，新增专用组合执行领域包。所有决策阶段使用不可变快照并返回事实、诊断和订单意图；只有账本提交阶段允许更新账户状态。线上 Paper 与回测调用同一个 `PortfolioEngine`。

### 3.3 未采用：完整券商级清算系统

完整事件溯源、券商对账、公司行动、跨币种和监管保证金体系扩展性最高，但超出当前模拟盘与回测范围。本设计保留端口，不提前实现这些能力。

## 4. 模块边界

新增 `src/stock_recommender/portfolio_engine/`：

| 模块 | 单一职责 | 依赖 |
|---|---|---|
| `contracts.py` | 多空仓位、目标仓位、订单意图、快照和事件契约 | 标准库 |
| `config.py` | 策略级敞口、保证金和做空配置校验 | `contracts.py` |
| `signal_ports.py` | 多头与空头信号端口和模型注册表 | 信号契约 |
| `target_pipeline.py` | 多空信号合并、净额化和目标仓位生成 | `pipeline.py`、信号契约 |
| `exposure.py` | 总敞口、净敞口、方向敞口、单股和持仓数控制 | 账户与目标快照 |
| `borrow.py` | 借券资格、费率和数据降级 | `BorrowSnapshot` |
| `margin.py` | 融资负债、保证金占用、购买力和强平判断 | 账户与市场快照 |
| `risk.py` | 方向化止损、追踪止盈、逼空和组合去杠杆 | 风险配置与账户快照 |
| `execution.py` | 滑点、费用、交易量限制和部分成交模拟 | 订单意图与行情快照 |
| `valuation.py` | 多空估值、权益、敞口、保证金率和 NAV | 账户与行情快照 |
| `ledger.py` | 幂等事件应用、账户快照和持久化 | 事件契约、存储端口 |
| `service.py` | 组装阶段并提交决策批次，不承载领域计算 | 上述模块 |
| `ports.py` | 行情、借券、存储和未来券商执行端口 | 契约 |

现有调用方迁移完成后拆除 `portfolio.py` 中的混合领域实现，不保留新旧两套运行时分支。报告和 API 只消费 `PortfolioSnapshot`，不得直接推导保证金或盈亏。

## 5. 策略级配置

每个策略独立持有三个配置块：

```yaml
exposure_policy:
  mode: LONG_ONLY
  max_positions: 10
  max_gross_exposure_pct: 150
  max_net_exposure_pct: 120
  max_long_exposure_pct: 120
  max_short_exposure_pct: 30
  max_long_position_pct: 15
  max_short_position_pct: 5

margin_policy:
  maintenance_margin_pct: 30
  liquidation_buffer_pct: 10
  financing_apr_pct: 8
  accrual_mode: DAILY

short_policy:
  signal_model: short_trend_breakdown_v1
  require_shortable: true
  require_easy_to_borrow: true
  estimated_borrow_apr_pct: 8
  cost_stress_multiplier: 2
  block_on_borrow_data_missing: true
  stop_loss_pct: 6
  trailing_activation_pct: 8
  trailing_rebound_pct: 4
  event_blackout_sessions: 2
  squeeze_rise_pct: 10
  squeeze_volume_ratio: 3
  maximum_volatility_20d_pct: 80
```

模式约束：

- `LONG_ONLY`：禁止空头，禁止杠杆，有效总敞口和净敞口最多 100%。
- `LONG_LEVERAGED`：禁止空头，有效多头和净敞口最多 120%。
- `LONG_SHORT`：多头最多 120%，空头最多 30%，总敞口最多 150%，净敞口绝对值最多 120%。

系统级硬上限固定为：总敞口 150%、净敞口绝对值 120%、空头敞口 30%、多空合计 10 只。策略配置只能收紧，不能放宽系统硬上限。

修改任一配置块会生成新策略 revision，清除旧回测审批结果并强制重新进入回测和 Paper 流程。现有策略迁移后显式写入 `LONG_ONLY`，不会自动启用杠杆或做空。

## 6. 领域契约

仓位方向不使用负数量表达。核心契约包括：

```python
TargetPosition(
    symbol: str,
    side: PositionSide,
    target_weight_pct: float,
    signal_score: float,
    model_id: str,
    thesis_id: str,
)

OrderIntent(
    symbol: str,
    position_side: PositionSide,
    order_side: OrderSide,
    position_effect: PositionEffect,
    quantity: int,
    reason: str,
)
```

枚举值：

- `PositionSide`：`LONG`、`SHORT`。
- `OrderSide`：`BUY`、`SELL`。
- `PositionEffect`：`OPEN`、`INCREASE`、`REDUCE`、`CLOSE`。

例如，卖出开空为 `SHORT + SELL + OPEN`，买入回补为 `SHORT + BUY + CLOSE`。同一股票的多空目标必须先净额化，账户禁止同时持有同一股票的多头与空头。

所有快照和事件必须包含策略 ID、策略 revision、运行 ID、市场快照 ID、账户快照 ID、组件版本和发生时间，以支持审计和幂等处理。

## 7. 信号边界

多头与空头信号完全独立：

- `LongSignalPipeline` 继续使用 `factor_rank_v1`。
- `ShortSignalPipeline` 首期提供 `short_trend_breakdown_v1`。
- 两个 Pipeline 只输出标准化信号，不读取现金、保证金或现有订单。
- `ExposureAllocator` 只消费标准化目标，不依赖信号模型内部字段。

`short_trend_breakdown_v1` 的准入条件：

- 20 日和 60 日动量为负。
- 价格位于 MA20 和 MA60 下方。
- 趋势必须持续，不因单日暴跌临时追空。
- 排除低流动性、借券不可用、异常波动和逼空风险股票。
- 财报等重大事件窗口禁止新开空仓。

模型通过注册表接入。新增空头模型不得修改组合执行、估值或账本模块。

## 8. Pipeline 数据流

每次运行先构造不可变输入：

- `StrategySnapshot`
- `AccountSnapshot`
- `MarketSnapshot`
- `BorrowSnapshot`

阶段顺序固定为：

1. `LongSignalStage`
2. `ShortSignalStage`
3. `TargetNettingStage`
4. `ExposureBudgetStage`
5. `BorrowAdmissionStage`
6. `MarginAdmissionStage`
7. `PortfolioRiskStage`
8. `RebalanceIntentStage`
9. `ExecutionSimulationStage`
10. `LedgerCommit`

前九个阶段不写账户或持久化数据。每个阶段输出版本化事实、诊断和拒绝原因。`LedgerCommit` 接收完整 `DecisionBatch`，使用运行键和事件键保证任务重试不会重复下单或重复计费。

## 9. 账户与估值

账户显式记录：

- `available_cash`
- `restricted_short_proceeds`
- `margin_loan`
- `long_market_value`
- `short_liability`
- `accrued_financing_cost`
- `accrued_borrow_cost`
- `equity`
- `buying_power`

权益公式：

```text
equity =
  available_cash
  + restricted_short_proceeds
  + long_market_value
  - short_liability
  - margin_loan
```

开空所得资金进入受限现金，不能重复增加购买力。融资买入形成明确的 `margin_loan`。融资利息按融资负债和实际持有时间计提，借券费按空头负债市值和实际持有时间计提。费用计提生成独立事件，不通过修改成交价隐藏。

费用事件提交时立即从 `available_cash` 扣除；`accrued_financing_cost` 和 `accrued_borrow_cost` 是累计统计字段，不是尚未从权益扣除的第二份负债。

每次行情快照都重新计算总敞口、净敞口、方向敞口、保证金占用、保证金率、购买力和 NAV。

模拟保证金率统一定义为 `equity / (long_market_value + short_liability) * 100`；没有持仓时视为无限。未来券商适配器可以提供更严格的证券级保证金要求，但不能放宽策略和系统硬上限。

## 10. 风控与强制去杠杆

方向化仓位规则：

- 多头固定止损默认 8%。
- 空头固定止损默认 6%。
- 多头盈利 10% 后激活追踪止盈，相对峰值回撤 5% 退出。
- 空头盈利 8% 后激活追踪止盈，价格相对最低点反弹 4% 回补。
- 借券资格取消时，空仓立即进入 `COVER_ONLY`。
- 当日上涨达到 10% 且成交量相对强度达到 3 倍时，视为逼空风险并进入 `COVER_ONLY`。
- 20 日年化波动率超过 80% 或距离财报等重大事件不超过 2 个交易日时，禁止新开空仓。

账户级规则：

- 保证金率低于 `maintenance_margin_pct + liquidation_buffer_pct`，默认 40%，禁止新增风险。
- 保证金率低于 `maintenance_margin_pct`，默认 30%，进入 `MARGIN_CALL`。
- `MARGIN_CALL` 先撤销所有增加风险的开放订单，再生成强制减仓意图。
- 强制减仓按释放保证金、风险贡献和预计交易成本综合排序。
- 组合回撤 12%/14%/15% 闸门继续保留，但统一基于账户权益计算。
- 权益小于等于零时进入 `INSOLVENT_HALT`，禁止交易，仅允许审计、估值和通知。

系统硬上限在目标准入、订单意图和成交后不变量检查三个位置重复验证，任何阶段都不能通过部分成交或任务重试绕过上限。

## 11. 数据缺失与错误隔离

- 借券数据缺失：阻止新开或增加空仓；多头 Pipeline 继续；已有空仓只允许减仓或平仓。
- 融资数据缺失：阻止增加杠杆；现金范围内交易和减仓继续。
- 行情缺失：对应股票不成交，不使用旧价伪造成交。
- 空头信号失败：不影响多头信号和已有仓位风控。
- 重大事件日历缺失：对应股票禁止新开空仓，不影响多头和已有仓位减仓。
- 保证金不足：拒绝增加风险的订单并输出预计需求、可用金额和差额。
- 事件提交失败：整个 `DecisionBatch` 不落账；相同运行键重试时得到同一组意图。

Paper 运行必须取得实时 `shortable` 和 `easy_to_borrow` 状态，否则禁止新空仓。回测允许使用策略配置的保守借券费和融资利率，但必须标记为估算数据，并执行至少 2 倍成本压力测试。缺少历史借券可用性数据时，做空策略不能通过未来实盘门禁。

## 12. 存储迁移

策略存储和组合账本各提升一个 schema 版本。迁移流程为：

1. 对策略和组合账本生成带时间戳备份。
2. 使用只读预检验证所有账户、开放订单和仓位可迁移。
3. 将现有策略显式写入 `LONG_ONLY` 配置。
4. 将现有仓位转换为 `side=LONG`、绝对数量和新的资金字段。
5. 将现有买卖订单补充 `position_side` 和 `position_effect`。
6. 重新计算账户权益并与迁移前 NAV 比较，差异必须在货币最小精度内。
7. 原子替换账本文件；迁移失败则保留原文件并停止服务启动。

运行时不维护旧 schema 分支。迁移脚本支持预检和正式执行，测试仅使用临时账本。

## 13. 后台、策略表现和飞书

策略后台新增独立的敞口、保证金和做空配置区，并展示系统硬上限。开启杠杆或做空后必须明确提示会创建新 revision、清除旧门禁结果并进入回测。

策略表现页新增：

- 多头市值与空头负债
- 总敞口与净敞口
- 保证金率与购买力
- 融资利息与借券费
- 每个仓位的方向、方向化盈亏和退出距离
- `MARGIN_CALL`、`COVER_ONLY` 和强平事件

飞书推荐、持仓报告和动作通知必须包含策略名、revision、方向、当前总/净敞口、保证金率及策略表现链接。无动作的五分钟风控检查继续保持静默。

## 14. 测试策略

### 14.1 单元与不变量测试

- 多头和空头开仓、增仓、减仓、平仓动作映射正确。
- 空头上涨产生亏损、下跌产生盈利。
- 开仓再平仓后，权益变化只来自盈亏和费用。
- 开空所得受限资金不能重复增加购买力。
- 任意目标和部分成交组合都不能突破系统硬上限。
- 同股票相反方向信号被确定性净额化。
- 借券数据失败只阻止增加空头风险。
- 融资与借券费用按持有时间计提且任务重试不重复收费。
- 保证金率低于 40% 和 30% 时分别进入限仓和强平状态。
- 重复市场快照、重复运行键和并发提交保持幂等。

### 14.2 场景测试

- 空头隔夜缺口上涨触发止损和部分回补。
- 借券资格撤销但流动性不足时保留回补订单并持续告警。
- 多空组合同时受冲击时按保证金释放效率去杠杆。
- 强平过程中部分成交后重新计算保证金，达到安全线即停止额外减仓。
- 行情、借券或融资数据源分别失败时保持局部降级。

### 14.3 一致性与集成测试

- 现有 `LONG_ONLY` 策略迁移前后产生相同候选、订单、费用和 NAV。
- Paper 与回测对相同快照产生相同订单事件和 NAV。
- 一个月集成测试使用临时策略账本、临时组合账本和隔离 PostgreSQL schema，不修改生产数据。
- 后台 API、页面和飞书内容覆盖多空、敞口、保证金和费用字段。
- 全量 Python、前端构建、ESLint、页面 JavaScript 和浏览器 QA 全部通过后才能部署。

## 15. 实施与上线顺序

1. 领域契约、配置和迁移预检。
2. 多空账户、估值和费用核心。
3. 目标仓位、敞口、借券和保证金 Pipeline。
4. 成交模拟、方向化风控和强制去杠杆。
5. 回测复用与审批门禁。
6. 后台、策略表现和飞书展示。
7. 隔离一个月集成测试与部署验证。

能力部署后，所有现有策略仍为 `LONG_ONLY`。用户必须在后台显式开启 `LONG_LEVERAGED` 或 `LONG_SHORT`，生成新 revision，并重新完成回测与 Paper 门禁。当前活动美股策略不会自动启用做空或杠杆。

## 16. 验收标准

- 组合执行核心没有依据运行环境区分 Paper 与回测的成交、估值或风控分支。
- 信号模型、敞口、借券、保证金、执行、估值和账本可以独立单测和替换。
- 系统在所有成交路径上维持持仓数、总敞口、净敞口和空头敞口硬上限。
- 生产策略和组合数据可以一次性迁移，迁移前后 `LONG_ONLY` NAV 在货币最小精度内一致。
- 借券或融资数据失败不会误开风险仓位，也不会阻断无关方向的安全操作。
- 策略表现页与飞书能够解释多空方向、敞口、保证金、费用和强平原因。
- 本地和部署环境的完整测试与隔离集成测试全部通过。
