# 策略持仓 Pipeline

## 目标

系统以“策略模拟组合”作为表现口径，不再用固定 30 天推荐涨跌衡量策略。每个策略拥有独立账户、独立订单和最多 10 个持仓槽位；空余资金允许保持现金。

## Pipeline 与边界

选股入口统一为 `factor_rank_v1`：20/60 日动量、均线趋势、成交量相对强度、反向波动率和 60 日回撤做横截面分位排名。线上推荐和滚动回测共用同一 `SignalEngine`；08:00 运行时严格排除当天及未来数据。LLM 只解释确定性入池结果，不得增删或替换股票。

一次推荐只生成一份结构化 `RecommendationPlan`。它包含生成时间、股票池、数据状态、板块状态、候选和最终入池股票，是报告、飞书、跟踪归档和组合执行之间唯一的数据契约：

1. `context.py` 只负责行情收集和上下文适配。
2. `recommendation.py` 只负责把分析结果组装成确定性推荐计划。
3. `reports.py` 只把计划渲染为脚本报告或 AI 说明。
4. `tracking.py` 直接持久化计划中的候选，不解析报告文本。
5. `portfolio_pipeline.py` 只消费显式候选、板块状态和账户快照。

因此修改飞书标题、数字精度或 Markdown 不会改变入池、价格、信号分和风控状态。AI 失败时也复用本次已经生成的计划，不再次拉行情。旧的“从推荐文案正则恢复股票和板块状态”路径已删除。

日评估按以下纯阶段运行，阶段通过版本化 `StageInput` / `StageOutput` 交换事实，不直接写持仓：

1. `candidate_normalization`：校验股票代码、价格和信号分，并去重排序。
2. `market_regime`：用股票池 20/60 日正动量广度和均线趋势广度生成板块状态、目标股票仓位，并过滤绝对动量为负的个股。
3. `portfolio_capacity`：把目标股票仓位换算为可用槽位；默认强势 100%、震荡 40%、弱势或数据不足 0%。
4. `risk_admission`：叠加账户启用状态、回撤闸门和交易模式。

`market_regime` 是独立的纯函数模块，不读取账户、不渲染文案也不下单。配置默认值与校验只保留在 `parameters.py`；线上报告、日持仓规划和组合回放显式传递同一份状态事实。组合入口不再提供“缺状态则默认满仓”的兼容分支。目标仓位下降时先撤销未成交买单，再从最低信号分持仓开始生成退出单；订单仍遵守 T+1、涨跌停和成交量约束。数据缺失按 `UNKNOWN = 0%` 处理，不用旧状态猜测行情。

Pipeline 完成后，事务层才把目标转换为订单意图并写入 append-only 事件账本。行情监控按“旧订单成交 → 持仓盯市 → 退出判断 → 组合风控 → 净值落账”的固定因果顺序执行；本次快照新建的订单不能使用同一快照成交。

## 默认退出和风控

- 固定止损：含费成本下跌 8%。
- 追踪止盈：上涨 10% 后激活；相对峰值回撤 5% 退出。
- 信号失效：连续 2 次日评估失效退出。
- 满仓替换：旧信号恶化、新候选分差通过门槛，且优势覆盖多倍往返成本后退出弱持仓。
- 组合回撤 12%：禁止新开仓，并把股票暴露压到不高于 70%。
- 组合回撤 14%：只减仓，退出全部当前可卖持仓。
- 组合回撤 15%：人工暂停；信号与新订单停止，风控和既有退出继续。

## 执行端

策略、敞口、风控和订单意图不依赖券商。`BrokerExecutionPort` 只负责把已经通过准入的订单意图交给执行端，并返回券商的累计订单状态；账本仍以成交事实更新组合，不从通知文本或本地行情猜测成交。

美股线上 Paper 设置 `STOCK_AGENT_EXECUTION_BACKEND=alpaca_paper`，通过官方 `alpaca-py` 连接 `https://paper-api.alpaca.markets/v2`：

- 每个订单意图生成不超过 48 字符的稳定 `client_order_id`。重试先回查该 ID，网络超时不会重复下单。
- Alpaca 的订单状态、部分成交数量和累计成交均价是唯一成交依据；不再叠加本地滑点、成交量参与率或模拟手续费。
- 券商成交是外部事实，成交后即使触发组合硬上限也必须先写入本地账本，同时记录风控诊断并进入退出阶段，不能按本地模拟方式回滚。
- 第一次接管时必须校验 Paper 账户状态、现金和持仓。与本地组合不一致则拒绝下单，必须通过新的策略账户做受控切换。
- 只接受 Paper Trading 域名和 `paper=True` 客户端，配置成实盘域名会直接失败。

回测和显式离线运行继续使用纯本地模拟执行：

- 买入金额按下单前冻结净值的目标权重计算，并按市场整手规则取整。
- 采用下一可执行行情，计入策略配置的滑点、佣金和税费。
- 单个行情条受成交量参与率约束并支持部分成交；A 股继续执行 T+1 和涨跌停规则。
- 策略版本切换会撤销旧版本未成交订单，并递增控制 epoch。

## 组合级滚动回测

回测通过纯内存账户调用与模拟盘相同的组合引擎，不写 PostgreSQL、生产 JSON 账本或策略配置。每个样本外窗口独立建账，08:00 用前一交易日收盘数据生成候选，09:35 按当日可执行价处理订单，15:00 盯市并执行止损、追踪止盈、信号失效、替换和组合回撤闸门。收益与最大回撤来自逐日清算净值，不再复合重叠的固定持有期事件收益。

可通过 `STOCK_AGENT_BACKTEST_DATASET_PATH` 提供只读 JSON 数据集。契约包含：

- `panel`：股票代码到历史 OHLCV 的映射。
- `universe_by_date`：日期到当时可投资股票代码的映射；信号日只读取前一交易日及更早快照。
- `benchmark`：独立基准的历史行情。
- `metadata.point_in_time_complete`、`benchmark_complete`、`strategy_parity_complete`、`execution_data_complete`：四个显式数据能力声明。

即使收益指标达标，只要历史成分、独立基准或执行时点成交量/涨跌停信息不完整，策略也不能通过 live 门禁。当前成分股和等权股票池只能用于探索或 paper。

公开历史数据通过可替换的 `HistoricalDataProvider` 接入。默认研究适配器使用国证 AI 50（399284）的历史时点成分快照、官方指数基准和东方财富日线；传入 `--include-intraday-execution` 时，还会补齐 09:35/15:00 的 5 分钟价量与按昨收计算的涨跌停价。构建器只写显式 `--output` 路径，并自动把评估起点截到首个可靠快照之后，避免把当前成分倒灌到历史。

```bash
stock-backtest-data \
  --output /private/tmp/stock-ai50.json \
  --start 2026-06-23 \
  --end 2026-07-22 \
  --include-intraday-execution
```

公司行动是独立门禁。当前公开适配器使用不复权成交价，尚未向组合现金和持仓数量计入分红送转，所以即使分时执行数据完整也不能据此直接进入 live。

## 2026-07 策略复核结论

详见 `docs/strategy-research-2026-07-23.md`。本轮只把新策略的 `signal_invalid_days` 默认值从 2 调整为 5；因子权重保持等权。原因是 5 天信号有效期在安全短样本和带偏差的长探索样本上都减少换手并改善收益，而不同因子权重在两个区间的排序不稳定，现阶段改权重会增加过拟合风险。现有策略配置不会被静默覆盖，应通过新 revision 进入 paper。

## 调度与通知

- 日信号：北京时间工作日 08:00。
- 风控检查：开市时段每 5 分钟；无动作时不发消息。
- 小时报告：北京时间 10:00、11:00、13:00、14:00、15:00。
- 动作通知和小时报告都包含策略名、版本、阶段、风险状态及策略表现深链。
- 只有 `paper` 和通过审批门禁的 `live` 策略可以定时写组合或投递；草稿、回测中、暂停和归档策略只能预览或完全禁止运行。

Hermes 可通过以下环境变量同步三个现有任务：

- `STOCK_AGENT_HERMES_JOB_ID`：日信号任务。
- `STOCK_AGENT_HERMES_TRACKING_JOB_ID`：小时持仓报告任务。
- `STOCK_AGENT_HERMES_RISK_JOB_ID`：5 分钟风控任务。

## 数据与页面

- 默认账本：`data/strategy_portfolios.json`，可用 `STOCK_AGENT_PORTFOLIO_PATH` 覆盖。
- 策略 API：`/api/strategies/{strategy_id}/portfolio`。
- 策略页面：`/strategies/{strategy_id}/portfolio`。
- 页面展示当前净值、累计收益、最大回撤、退出胜率、当前持仓、订单、退出记录和事件账本。

## 多空、杠杆与硬限制

敞口、融资和借券是三个独立策略块：`exposure_policy` 决定持仓方向与敞口，`margin_policy` 决定维持保证金、清算缓冲及融资日息，`short_policy` 决定借券准入、成本和空头风险。修改任一策略块都必须创建新 revision，并重新通过回测和 Paper 审批；运行中的账户不得在原 revision 上静默换参数。

系统硬限制不可由策略配置放大：最多 10 个持仓，总敞口不高于 150%，净敞口绝对值不高于 120%，多头不高于 120%，空头不高于 30%，单一多头不高于 15%，单一空头不高于 5%。`LONG_ONLY`、`LONG_LEVERAGED` 和 `LONG_SHORT` 共用同一 Pipeline 与账本，方向差异只由策略块输入决定。

借券数据缺失时采用 fail-closed：禁止新增空头。已有空头遇到借券撤销、不可借或数据退化时进入 `COVER_ONLY`，只能回补，不能增仓。估算借券利率必须标注来源，不能作为逐证券实际利率展示。保证金缓冲触发后只允许降低风险；低于维持线时产生可追踪的 `MARGIN_CALL` 强制去杠杆意图。融资费和借券费按独立 lifecycle 逐日计提。

部署不会自动修改现有策略。迁移后的所有既有策略继续保持 `LONG_ONLY`；启用杠杆或空头必须显式创建新 revision 并重新审批。

## PostgreSQL 迁移与隔离集成测试

`PostgresLedgerStore` 是独立适配器，不继承 JSON 或内存账本；生产运行必须显式设置 `STOCK_AGENT_LEDGER_BACKEND=postgres`、`STOCK_AGENT_PORTFOLIO_DATABASE_URL` 和 `STOCK_AGENT_PORTFOLIO_SCHEMA`，配置错误直接 fail-closed，禁止静默退回 JSON。JSON 只保留为本地开发适配器和一次性迁移源。PostgreSQL 将数据分成两类：追加式 canonical 审计表保存账户、批次、订单、成交、风控、费用、事件和 revision 全生命周期；`open_intents_current` 与 `execution_progress_current` 只物化当前未完成订单。新订单、部分成交、完全成交和 revision 切换都在同一提交事务中同步更新这两张当前状态表，因此它们的规模由开放订单数决定，不随运行天数增长。迁移瞬间尚未完成的订单也写入同一当前状态模型，不保留迁移专用运行分支。订单意图定义以 `(strategy_id, intent_id)` 全局唯一并校验 payload fingerprint，occurrence 以 `(strategy_id, run_key, intent_id)` 标识，内容冲突会 fail-closed。`batch_payload` 只是审计副本，不是读取权威；当前账户和开放订单从有界物化表读取，已提交批次和策略表现从审计表重建，不保存或读取整份 `ledger_state`。所有需要多条查询重建结果的公开只读入口都在 `REPEATABLE READ READ ONLY` 事务中运行，避免账户、facts 与批次来自不同数据库快照。

JSON 本地适配器还有第二道保护：`STOCK_AGENT_JSON_LEDGER_MAX_BYTES` 默认 16MiB。账本超过阈值时直接拒绝运行并提示配置 PostgreSQL，避免生产环境误配回 JSON 后重新出现锁竞争和超时。

读取已提交批次时，适配器用 `committed_runs` 元数据及各事实表的 ordinal 重建完整 `DecisionBatch`，再将 canonical graph 和 fingerprint 与审计副本及已存指纹逐项核对。任何事实缺行、多行、ordinal 断裂、列与 payload 不一致或审计副本被修改都会 fail-closed。批次事件通过 `source_kind` 区分原始事件与账本派生事件：前者还原到 `DecisionBatch.events`，后者只进入 ledger event view。

每次提交在一个数据库事务内完成：先对策略账户行 `FOR UPDATE`，再检查来源快照和 revision，写入 canonical 事实，最后更新当前账户。数据库生成的单调 `commit_ordinal` 是批次及批次事实的唯一追加顺序；不得用 run key 或业务时间猜测提交顺序，幂等重试也不得分配新序号。数据库主键 `(strategy_id, run_key)` 与逐类事实唯一键共同保证跨进程幂等；同一 run/request 的不同事实必须只有一个提交成功，另一方得到明确冲突。策略表现读取仍返回 `PortfolioPerformanceLedgerView`，不向服务层暴露 SQL 或 JSON 存储细节。

热读取只加载当前账户、有界开放订单状态和最近 100 条事件；热提交只额外按本批涉及的 intent ID 做索引查询，不扫描历史事件或历史订单。完整历史只在策略表现/显式审计读取中加载。这样每 5 分钟风控的工作量由当前持仓与开放订单决定，不随运行天数增长。

迁移前先暂停产生新组合写入的调度，确认目标 PostgreSQL 版本和容量、校验连接账户只拥有目标应用 schema 的权限、检查当前 JSON 账本完整性，并分别对应用目录、策略配置和组合账本创建带时间戳的备份。`stock-portfolio-bootstrap-postgres --check --archive ...` 只校验、不写入，同时报告活动订单与执行进度数量；`--apply` 仅允许空目标 schema，将已验证的当前账户及活动订单状态导入 PostgreSQL，并把原 JSON 语义等价地压缩为不可写历史归档。设置 `STOCK_AGENT_PORTFOLIO_ARCHIVE_PATH` 后，`ArchivedLedgerStore` 只在策略表现/显式历史审计读取时合并旧历史与 PostgreSQL 新事实，提交路径绝不打开归档，任何实时写入都只进入 PostgreSQL。先在空的预演 schema 执行建表和全量校验；只有 preflight 全部通过才允许 apply。schema 与表标识符必须使用 Psycopg `Identifier`，不得拼接用户输入。

每次 CLI 运行将成功/失败、模式、策略、账本后端、耗时和错误类型写入有上限的 SQLite 运行日志（默认最多 2000 条），通过 `stock-runtime-runs --limit 50` 查询。该日志用于区分应用执行失败与 Hermes/飞书投递失败，不包含数据库口令或飞书凭据。

隔离测试只允许创建 `stock_agent_test_<32位小写十六进制>` schema。fixture 在 `finally` 中使用独立连接执行精确 `DROP SCHEMA ... CASCADE`；禁止删除或修改 `public`、既有 schema、既有表或业务数据。运行方式：

```bash
python3 -m venv /private/tmp/stock-agent-integration
/private/tmp/stock-agent-integration/bin/python -m pip install -e '.[integration]'
env STOCK_AGENT_TEST_DATABASE_URL="$STOCK_AGENT_TEST_DATABASE_URL" \
  PYTHONPATH=src:tests \
  /private/tmp/stock-agent-integration/bin/python -m unittest \
  tests.test_month_long_short_integration -v
```

不设置 `STOCK_AGENT_TEST_DATABASE_URL` 时测试必须明确显示 `requires Docker PostgreSQL` 并跳过，不能伪装成已验证。一个月集成场景固定重放 22 个 session；回测和 Paper 只共享行情、借券与策略输入，各自通过独立 orchestration 路径运行。测试逐日比较已提交批次、订单、执行进度、成交、风险、结算、费用、事件、持仓和完整 NAV 指纹，并覆盖缺失/撤销借券、逼空、部分成交、融资费、借券费、保证金缓冲和强制风控。

隔离测试还会在运行前后计算所有非测试用户表的定义哈希、行数和无序行内容哈希，要求三者完全一致，并确认临时测试 schema 数量归零。双连接并发用例同时验证重复批次只写一份事实、冲突批次只有一个赢家，不能用单进程锁代替数据库约束。

apply 失败或发布后校验失败时立即停止写入，保留失败现场用于诊断；从迁移前的时间戳备份恢复应用目录、策略配置和组合账本，再校验原 JSON 账本及现有 `LONG_ONLY` 策略可读。不得用空账本覆盖生产账本，也不得在未验证备份可恢复前删除旧数据。
