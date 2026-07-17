import tempfile
import unittest
from pathlib import Path

from stock_recommender.parameters import (
    PARAMETER_CATALOG,
    activate_strategy,
    create_strategy,
    deactivate_strategy,
    delete_strategy,
    duplicate_strategy,
    convert_strategy_text,
    default_strategy_config,
    load_strategy_config,
    load_strategy_store,
    save_strategy_config,
)
from stock_recommender.selection import filter_candidates


class ParameterCatalogTests(unittest.TestCase):
    def test_catalog_covers_common_stock_screening_dimensions(self):
        groups = {item["group"] for item in PARAMETER_CATALOG}
        self.assertTrue(
            {
                "universe",
                "market",
                "technical",
                "valuation",
                "growth",
                "quality",
                "income",
                "flow_event",
                "risk",
            }.issubset(groups)
        )
        self.assertGreaterEqual(len(PARAMETER_CATALOG), 45)
        self.assertTrue(all(item.get("selected") for item in PARAMETER_CATALOG))

    def test_strategy_text_maps_to_parameter_updates(self):
        result = convert_strategy_text(
            "只看沪深A股，排除ST，流通市值20到100亿，涨幅至少3%，换手率不低于2%，PE不超过60倍"
        )
        updates = {item["id"]: item for item in result["updates"]}

        self.assertEqual(updates["float_market_cap_min"]["value"], 2_000_000_000)
        self.assertEqual(updates["float_market_cap_max"]["value"], 10_000_000_000)
        self.assertEqual(updates["change_pct_min"]["value"], 3)
        self.assertEqual(updates["turnover_rate_min"]["value"], 2)
        self.assertEqual(updates["pe_max"]["value"], 60)
        self.assertTrue(updates["exclude_st"]["value"])

    def test_config_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.json"
            config = default_strategy_config()
            config["name"] = "低估值动量"
            config["parameters"]["pe_max"] = {"enabled": True, "value": 45}

            saved = save_strategy_config(config, path=path)
            loaded = load_strategy_config(path=path)

            self.assertEqual(saved["name"], "低估值动量")
            self.assertEqual(loaded["parameters"]["pe_max"]["value"], 45)

    def test_empty_store_starts_without_user_strategies(self):
        with tempfile.TemporaryDirectory() as directory:
            store = load_strategy_store(path=Path(directory) / "missing.json")

        self.assertEqual(store["strategies"], [])
        self.assertIsNone(store["active_strategy_id"])

    def test_multiple_strategies_are_isolated_and_can_be_activated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategies.json"
            momentum = create_strategy("动量策略", path=path)
            value = create_strategy("价值策略", path=path)
            value["parameters"]["pe_max"] = {"enabled": True, "value": 25}
            save_strategy_config(value, path=path, strategy_id=value["id"])
            activate_strategy(value["id"], path=path)

            active = load_strategy_config(path=path)
            original = load_strategy_config(path=path, strategy_id=momentum["id"])

            self.assertEqual(active["name"], "价值策略")
            self.assertEqual(active["parameters"]["pe_max"]["value"], 25)
            self.assertFalse(original["parameters"]["pe_max"]["enabled"])

    def test_strategy_can_be_deactivated_without_being_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategies.json"
            strategy = create_strategy("暂停策略", path=path)

            deactivate_strategy(strategy["id"], path=path)
            store = load_strategy_store(path=path)

            self.assertIsNone(store["active_strategy_id"])
            self.assertEqual([item["id"] for item in store["strategies"]], [strategy["id"]])

    def test_explicitly_inactive_new_strategy_stays_inactive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategies.json"
            strategy = create_strategy("待配置策略", path=path, activate=False)

            store = load_strategy_store(path=path)

            self.assertIsNone(store["active_strategy_id"])
            self.assertEqual(store["strategies"][0]["id"], strategy["id"])

    def test_strategy_can_be_duplicated_and_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategies.json"
            original = create_strategy("成长策略", description="高成长", path=path)
            copied = duplicate_strategy(original["id"], path=path)
            delete_strategy(original["id"], path=path)
            store = load_strategy_store(path=path)

            self.assertNotEqual(copied["id"], original["id"])
            self.assertEqual(copied["name"], "成长策略 - 副本")
            self.assertEqual(copied["description"], "高成长")
            self.assertEqual([item["id"] for item in store["strategies"]], [copied["id"]])

    def test_legacy_single_strategy_file_is_migrated_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            config = default_strategy_config()
            config["name"] = "旧策略"
            path.write_text(__import__("json").dumps(config, ensure_ascii=False), encoding="utf-8")

            store = load_strategy_store(path=path)

            self.assertEqual(store["strategies"][0]["name"], "旧策略")
            self.assertEqual(store["active_strategy_id"], store["strategies"][0]["id"])

    def test_available_saved_parameters_affect_filtering(self):
        config = default_strategy_config()
        config["parameters"]["price_min"] = {"enabled": True, "value": 10}
        rows = [
            {"symbol": "600001", "name": "低价股", "price": 8, "turnover": 1, "float_market_cap": 3_000_000_000},
            {"symbol": "600002", "name": "目标股", "price": 12, "turnover": 1, "float_market_cap": 3_000_000_000},
        ]

        filtered = filter_candidates(rows, strategy=config)

        self.assertEqual([row["symbol"] for row in filtered], ["600002"])


if __name__ == "__main__":
    unittest.main()
