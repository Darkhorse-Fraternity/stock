import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from stock_recommender.performance import (
    build_recommendation_performance,
    load_recommendation_history,
    reconcile_recommendation_history_strategies,
    upsert_recommendation_history,
)
from stock_recommender.reports import append_performance_link, format_recommendation_snapshot
from stock_recommender.tracking import generate_saved_tracking_report, save_daily_selection
from recommendation_fixtures import make_recommendation_plan


class RecommendationPerformanceTests(unittest.TestCase):
    def test_history_strategy_reconciliation_is_auditable_and_idempotent(self):
        now = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            upsert_recommendation_history(
                {
                    "trade_date": "2026-07-21",
                    "strategy_id": "retired-id",
                    "strategy_name": "科技 AI",
                    "recommendations": [{"symbol": "300001", "name": "测试", "entry_price": 10}],
                },
                path=path,
                now=now,
            )
            store = {
                "active_strategy_id": "current-id",
                "strategies": [{"id": "current-id", "name": "科技 AI", "revision": 1}],
            }

            dry_run = reconcile_recommendation_history_strategies(store, path=path, now=now, dry_run=True)
            migrated = reconcile_recommendation_history_strategies(store, path=path, now=now)
            repeated = reconcile_recommendation_history_strategies(store, path=path, now=now)
            record = load_recommendation_history(days=2, path=path, now=now)[0]

        self.assertEqual(dry_run["migrated_count"], 1)
        self.assertEqual(migrated["migrated_count"], 1)
        self.assertEqual(repeated["migrated_count"], 0)
        self.assertEqual(record["strategy_id"], "current-id")
        self.assertEqual(record["legacy_strategy_id"], "retired-id")
        self.assertEqual(record["strategy_id_migration"], "unique_strategy_name_match")

    def test_history_strategy_reconciliation_leaves_ambiguous_names_unchanged(self):
        now = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            upsert_recommendation_history(
                {
                    "trade_date": "2026-07-21",
                    "strategy_id": "retired-id",
                    "strategy_name": "科技 AI",
                    "recommendations": [{"symbol": "300001", "name": "测试", "entry_price": 10}],
                },
                path=path,
                now=now,
            )
            store = {
                "strategies": [
                    {"id": "one", "name": "科技 AI"},
                    {"id": "two", "name": "科技 AI"},
                ]
            }

            result = reconcile_recommendation_history_strategies(store, path=path, now=now)
            record = load_recommendation_history(days=2, path=path, now=now)[0]

        self.assertEqual(result["migrated_count"], 0)
        self.assertEqual(result["unresolved_count"], 1)
        self.assertEqual(result["unresolved"][0]["reason"], "ambiguous_name")
        self.assertEqual(record["strategy_id"], "retired-id")

    def test_history_strategy_reconciliation_supports_audited_explicit_mapping(self):
        now = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            upsert_recommendation_history(
                {
                    "trade_date": "2026-07-21",
                    "strategy_id": "retired-id",
                    "strategy_name": "旧名称",
                    "recommendations": [{"symbol": "300001", "name": "测试", "entry_price": 10}],
                },
                path=path,
                now=now,
            )
            store = {"strategies": [{"id": "current-id", "name": "科技 AI"}]}

            result = reconcile_recommendation_history_strategies(
                store,
                path=path,
                now=now,
                explicit_mapping={"retired-id": "current-id"},
            )
            record = load_recommendation_history(days=2, path=path, now=now)[0]

        self.assertEqual(result["migrated_count"], 1)
        self.assertEqual(result["migrated"][0]["reason"], "explicit_strategy_id_mapping")
        self.assertEqual(record["strategy_id"], "current-id")
        self.assertEqual(record["strategy_name"], "科技 AI")
        self.assertEqual(record["legacy_strategy_id"], "retired-id")

    def test_history_upsert_replaces_same_strategy_day_and_builds_summary(self):
        now = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            first = {
                "trade_date": "2026-07-20",
                "generated_at": "2026-07-20 09:35:00 CST",
                "strategy_id": "strategy-1",
                "strategy_name": "科技 AI",
                "recommendations": [{"symbol": "300001", "name": "测试一", "entry_price": 99}],
            }
            replacement = {
                **first,
                "recommendations": [{"symbol": "300001", "name": "测试一", "entry_price": 100}],
            }
            second = {
                "trade_date": "2026-07-21",
                "generated_at": "2026-07-21 09:35:00 CST",
                "strategy_id": "strategy-1",
                "strategy_name": "科技 AI",
                "recommendations": [{"symbol": "300002", "name": "测试二", "entry_price": 100}],
            }
            upsert_recommendation_history(first, path=path, now=now)
            upsert_recommendation_history(replacement, path=path, now=now)
            upsert_recommendation_history(second, path=path, now=now)

            payload = build_recommendation_performance(
                days=30,
                path=path,
                now=now,
                quote_fetcher=lambda entries: (
                    [
                        {"symbol": "300001", "name": "测试一", "price": 110, "percent": 2, "volume": 2000},
                        {"symbol": "300002", "name": "测试二", "price": 90, "percent": -3, "volume": 3000},
                    ],
                    None,
                ),
            )

        self.assertEqual(payload["summary"]["total_recommendations"], 2)
        self.assertEqual(payload["summary"]["recommendation_days"], 2)
        self.assertEqual(payload["summary"]["success_rate"], 50)
        self.assertEqual(payload["summary"]["win_rate"], 50)
        self.assertEqual(payload["summary"]["average_return_pct"], 0)
        self.assertEqual(payload["summary"]["best"]["symbol"], "300001")
        self.assertEqual(payload["summary"]["worst"]["symbol"], "300002")
        self.assertEqual(payload["summary"]["strategy_count"], 1)
        self.assertEqual(payload["strategies"][0]["name"], "科技 AI")
        self.assertEqual(payload["strategies"][0]["revision"], 1)
        self.assertEqual(payload["strategies"][0]["recommendations"], 2)

    def test_performance_identifies_multiple_strategies(self):
        now = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            for strategy_id, strategy_name, symbol in (
                ("strategy-1", "科技 AI", "300001"),
                ("strategy-2", "成长股", "300002"),
            ):
                upsert_recommendation_history(
                    {
                        "trade_date": "2026-07-20",
                        "generated_at": "2026-07-20 08:00:00 CST",
                        "strategy_id": strategy_id,
                        "strategy_name": strategy_name,
                        "strategy_revision": 2,
                        "strategy_stage": "paper",
                        "recommendations": [{"symbol": symbol, "name": symbol, "entry_price": 100}],
                    },
                    path=path,
                    now=now,
                )

            payload = build_recommendation_performance(
                days=30,
                path=path,
                now=now,
                quote_fetcher=lambda entries: (
                    [{"symbol": item["symbol"], "name": item["symbol"], "price": 105} for item in entries],
                    None,
                ),
            )

        self.assertEqual(payload["summary"]["strategy_count"], 2)
        self.assertEqual({item["name"] for item in payload["strategies"]}, {"科技 AI", "成长股"})
        self.assertTrue(all(item["revision"] == 2 for item in payload["strategies"]))
        self.assertTrue(all(item["stage"] == "paper" for item in payload["strategies"]))

    def test_repeated_stock_uses_first_recommendation_price_for_success_rate(self):
        now = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            for trade_date, entry_price in (("2026-07-20", 100), ("2026-07-21", 120)):
                upsert_recommendation_history(
                    {
                        "trade_date": trade_date,
                        "generated_at": f"{trade_date} 09:35:00 CST",
                        "strategy_id": "strategy-1",
                        "strategy_name": "科技 AI",
                        "recommendations": [
                            {"symbol": "300001", "name": "重复推荐股", "entry_price": entry_price}
                        ],
                    },
                    path=path,
                    now=now,
                )

            payload = build_recommendation_performance(
                days=30,
                path=path,
                now=now,
                quote_fetcher=lambda entries: (
                    [{"symbol": "300001", "name": "重复推荐股", "price": 110, "percent": 1, "volume": 2000}],
                    None,
                ),
            )

        self.assertEqual(payload["summary"]["total_recommendations"], 2)
        self.assertEqual(payload["summary"]["unique_stocks"], 1)
        self.assertEqual(payload["summary"]["successful_stocks"], 1)
        self.assertEqual(payload["summary"]["success_rate"], 100)
        self.assertEqual(len(payload["stocks"]), 1)
        self.assertEqual(payload["stocks"][0]["first_recommendation_price"], 100)
        self.assertEqual(payload["stocks"][0]["current_price"], 110)
        self.assertEqual(payload["stocks"][0]["return_pct"], 10)
        self.assertEqual(payload["stocks"][0]["recommendation_count"], 2)

    def test_tracking_updates_archived_recommendation(self):
        recommendation_time = datetime(2026, 7, 20, 1, 35, tzinfo=timezone.utc)
        tracking_time = datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc)
        plan = make_recommendation_plan(
            [{"symbol": "300001", "name": "测试科技", "price": 100, "percent": 2, "volume": 1000, "turnover": 100000}],
            now=recommendation_time,
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            history_path = Path(directory) / "history.json"
            save_daily_selection(state_path, plan, now=recommendation_time, history_path=history_path)
            generate_saved_tracking_report(
                state_path=state_path,
                history_path=history_path,
                now=tracking_time,
                quote_fetcher=lambda entries: ([{"symbol": "300001", "name": "测试科技", "price": 105, "percent": 7, "volume": 2500, "turnover": 300000}], None),
            )
            history = load_recommendation_history(days=30, path=history_path, now=tracking_time)

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["recommendations"][0]["last_price"], 105)
        self.assertEqual(history[0]["recommendations"][0]["last_volume"], 2500)
        self.assertAlmostEqual(history[0]["recommendations"][0]["return_since_recommendation_pct"], 5)

    def test_performance_link_is_appended_once(self):
        url = "https://stocks.example.test/performance"
        report = append_performance_link("模拟盘报告", url)

        self.assertIn(f"[查看策略表现]({url})", report)
        self.assertEqual(append_performance_link(report, url), report)


if __name__ == "__main__":
    unittest.main()
