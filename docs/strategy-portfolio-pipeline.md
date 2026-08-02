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

## 模拟成交

- 买入金额按下单前冻结净值的 10% 计算，按 100 股整手取整。
- 采用下一可执行行情，默认计 10 bps 滑点。
- 计佣金、最低佣金、卖出印花税和过户费。
- 单个行情条最多参与 5% 成交量，支持部分成交。
- 买入遵守 T+1；卖出受可卖数量和涨跌停锁单约束。
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

`PostgresLedgerStore` 是独立的可选适配器，不继承 JSON 或内存账本；生产 JSON 部署也不依赖 Psycopg。PostgreSQL 把 canonical 事实拆分到 14 张规范化表：账户、持仓、借券与融资生命周期、已提交批次、订单意图、执行进度、成交、持仓风险事实与更新、结算更新、费用计提、事件和 revision 迁移。`batch_payload` 只用于精确重放已提交批次，当前账户、开放订单和策略表现都从类型化关系表重建，不保存或读取整份 `ledger_state`。

每次提交在一个数据库事务内完成：先对策略账户行 `FOR UPDATE`，再检查来源快照和 revision，写入 canonical 事实，最后更新当前账户。数据库主键 `(strategy_id, run_key)` 与逐类事实唯一键共同保证跨进程幂等；同一 run/request 的不同事实必须只有一个提交成功，另一方得到明确冲突。策略表现读取仍返回 `PortfolioPerformanceLedgerView`，不向服务层暴露 SQL 或 JSON 存储细节。

迁移前先执行 preflight：确认目标 PostgreSQL 版本和容量、校验连接账户只拥有目标应用 schema 的权限、检查当前 JSON 账本完整性，并分别对应用目录、策略配置和组合账本创建带时间戳的备份。先在空的预演 schema 执行建表和全量校验；只有 preflight 全部通过才允许 apply。schema 与表标识符必须使用 Psycopg `Identifier`，不得拼接用户输入。

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
