import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from stock_recommender.parameters import (
    PARAMETER_CATALOG,
    STRATEGY_STORE_VERSION,
    StrategyLifecycleError,
    activate_strategy,
    create_strategy,
    deactivate_strategy,
    delete_strategy,
    duplicate_strategy,
    convert_strategy_text,
    default_strategy_config,
    load_strategy_config,
    load_strategy_store,
    normalize_strategy_config,
    record_backtest_evaluation,
    save_strategy_config,
)
from stock_recommender.selection import filter_candidates


class ParameterCatalogTests(unittest.TestCase):
    def test_strategy_store_schema_version_is_six(self):
        self.assertEqual(STRATEGY_STORE_VERSION, 6)

    def test_legacy_strategy_normalizes_to_explicit_long_only_policies(self):
        strategy = normalize_strategy_config(
            {"version": 5, "name": "旧策略", "parameters": {}}
        )

        self.assertEqual(strategy["version"], 6)
        self.assertEqual(strategy["exposure_policy"]["mode"], "LONG_ONLY")
        self.assertIn("margin_policy", strategy)
        self.assertIn("short_policy", strategy)

    def test_v5_store_loads_in_memory_as_v6_without_creating_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategies.json"
            strategy = default_strategy_config()
            strategy.update(
                {
                    "version": 5,
                    "id": "legacy-strategy",
                    "revision": 3,
                    "parent_strategy_id": "legacy-parent",
                }
            )
            for section in ("exposure_policy", "margin_policy", "short_policy"):
                strategy.pop(section)
            legacy_store = {
                "version": 5,
                "active_strategy_id": "legacy-strategy",
                "strategies": [strategy],
            }
            path.write_text(
                json.dumps(legacy_store, ensure_ascii=False),
                encoding="utf-8",
            )

            loaded_store = load_strategy_store(path=path)
            loaded_strategy = load_strategy_config(path=path)
            persisted = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(loaded_store["version"], 6)
            self.assertEqual(loaded_store["active_strategy_id"], "legacy-strategy")
            self.assertEqual(len(loaded_store["strategies"]), 1)
            self.assertEqual(loaded_strategy["version"], 6)
            self.assertEqual(loaded_strategy["id"], "legacy-strategy")
            self.assertEqual(loaded_strategy["revision"], 3)
            self.assertEqual(loaded_strategy["parent_strategy_id"], "legacy-parent")
            self.assertEqual(loaded_strategy["exposure_policy"]["mode"], "LONG_ONLY")
            self.assertIn("margin_policy", loaded_strategy)
            self.assertIn("short_policy", loaded_strategy)
            self.assertEqual(persisted, legacy_store)

    def test_strategy_store_rejects_unknown_schema_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategies.json"
            path.write_text(
                json.dumps(
                    {"version": 4, "active_strategy_id": None, "strategies": []}
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StrategyLifecycleError, "version=6"):
                load_strategy_store(path=path)

    def test_v5_store_cannot_enable_policy_fields_from_the_new_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategies.json"
            strategy = default_strategy_config()
            strategy.update({"version": 5, "id": "legacy-strategy"})
            strategy["parameters"]["market"] = {"enabled": True, "value": "us"}
            strategy["exposure_policy"]["mode"] = "LONG_SHORT"
            path.write_text(
                json.dumps(
                    {
                        "version": 5,
                        "active_strategy_id": "legacy-strategy",
                        "strategies": [strategy],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            loaded = load_strategy_config(path=path)

            self.assertEqual(loaded["exposure_policy"]["mode"], "LONG_ONLY")

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

    def test_single_strategy_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsupported.json"
            config = default_strategy_config()
            path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(StrategyLifecycleError, "strategies"):
                load_strategy_store(path=path)

    def test_store_rejects_missing_lifecycle_and_invalid_active_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategies.json"
            strategy = create_strategy("严格策略", path=path)
            payload = load_strategy_store(path=path)
            payload["strategies"][0].pop("lifecycle")
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(StrategyLifecycleError, "lifecycle"):
                load_strategy_store(path=path)

            payload["strategies"][0]["lifecycle"] = strategy["lifecycle"]
            payload["active_strategy_id"] = "missing"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(StrategyLifecycleError, "active_strategy_id"):
                load_strategy_store(path=path)

    def test_store_rejects_malformed_json_instead_of_starting_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategies.json"
            path.write_text('{"version": 6,', encoding="utf-8")

            with self.assertRaisesRegex(StrategyLifecycleError, "JSON"):
                load_strategy_store(path=path)

    def test_policy_changes_create_inactive_revision_and_preserve_original(self):
        changes = (
            ("exposure_policy", "max_positions", 9),
            ("margin_policy", "financing_apr_pct", 7.0),
            ("short_policy", "stop_loss_pct", 5.0),
        )
        for section, field, value in changes:
            with self.subTest(section=section), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "strategies.json"
                original = create_strategy("待修订策略", path=path)
                original["delivery"]["enabled"] = True
                save_strategy_config(original, path=path)
                evaluated = record_backtest_evaluation(
                    original["id"],
                    {
                        "cumulative_return_pct": 12.0,
                        "approval_gate": {
                            "passed": True,
                            "checks": [{"code": "PASS"}],
                        },
                    },
                    path=path,
                )
                candidate = deepcopy(evaluated)
                candidate[section][field] = value

                revision = save_strategy_config(candidate, path=path)
                store = load_strategy_store(path=path)
                stored_original = next(
                    item for item in store["strategies"] if item["id"] == original["id"]
                )

                self.assertNotEqual(revision["id"], original["id"])
                self.assertEqual(revision["revision"], 2)
                self.assertEqual(revision["parent_strategy_id"], original["id"])
                self.assertEqual(revision[section][field], value)
                self.assertEqual(revision["lifecycle"]["stage"], "draft")
                self.assertIsNone(revision["validation"]["last_backtest"])
                self.assertEqual(
                    revision["validation"]["approval_gate"],
                    {"passed": False, "checks": [], "evaluated_at": None},
                )
                self.assertFalse(revision["delivery"]["enabled"])
                self.assertEqual(store["active_strategy_id"], original["id"])
                self.assertEqual(len(store["strategies"]), 2)
                self.assertEqual(
                    stored_original[section][field], evaluated[section][field]
                )
                self.assertEqual(stored_original["lifecycle"]["stage"], "paper")
                self.assertTrue(
                    stored_original["validation"]["approval_gate"]["passed"]
                )

    def test_policy_revision_includes_other_model_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategies.json"
            original = create_strategy("组合修订", path=path)
            candidate = deepcopy(original)
            candidate["margin_policy"]["financing_apr_pct"] = 7.0
            candidate["parameters"]["price_min"] = {
                "enabled": True,
                "value": 10,
            }

            revision = save_strategy_config(candidate, path=path)
            store = load_strategy_store(path=path)
            stored_original = next(
                item for item in store["strategies"] if item["id"] == original["id"]
            )

            self.assertEqual(revision["parameters"]["price_min"]["value"], 10)
            self.assertEqual(revision["margin_policy"]["financing_apr_pct"], 7.0)
            self.assertEqual(stored_original["parameters"]["price_min"]["value"], 0.01)
            self.assertEqual(store["active_strategy_id"], original["id"])

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
