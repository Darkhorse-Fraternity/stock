from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from stock_recommender import cli
from stock_recommender.delivery import delivery_cron
from stock_recommender.parameters import (
    _strategy_delivery_payload,
    default_strategy_config,
    normalize_report_delivery,
)
from stock_recommender.schedule import should_publish_at_market_open


class MarketOpenDeliveryTests(unittest.TestCase):
    def test_us_market_open_cron_covers_both_dst_offsets(self) -> None:
        config = {
            "parameters": {"market": {"enabled": True, "value": "us"}},
            "delivery": {"schedule_mode": "market_open"},
        }

        self.assertEqual(delivery_cron(config), "30 13,14 * * 1-5")

    def test_us_market_open_guard_handles_daylight_saving_time(self) -> None:
        summer_open = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
        summer_inactive_window = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)
        winter_inactive_window = datetime(2026, 12, 15, 13, 30, tzinfo=timezone.utc)
        winter_open = datetime(2026, 12, 15, 14, 30, tzinfo=timezone.utc)

        self.assertTrue(should_publish_at_market_open(summer_open, market="us"))
        self.assertFalse(
            should_publish_at_market_open(summer_inactive_window, market="us")
        )
        self.assertFalse(
            should_publish_at_market_open(winter_inactive_window, market="us")
        )
        self.assertTrue(should_publish_at_market_open(winter_open, market="us"))

    def test_market_open_guard_rejects_weekends(self) -> None:
        saturday_open_time = datetime(2026, 7, 18, 13, 30, tzinfo=timezone.utc)

        self.assertFalse(
            should_publish_at_market_open(saturday_open_time, market="us")
        )

    def test_cn_market_open_uses_shanghai_session(self) -> None:
        config = {
            "parameters": {"market": {"enabled": True, "value": "cn"}},
            "delivery": {"schedule_mode": "market_open"},
        }
        open_time = datetime(2026, 7, 15, 1, 30, tzinfo=timezone.utc)

        self.assertEqual(delivery_cron(config), "30 1 * * 1-5")
        self.assertTrue(should_publish_at_market_open(open_time, market="cn"))

    def test_fixed_schedule_remains_explicitly_available(self) -> None:
        config = {
            "parameters": {"market": {"enabled": True, "value": "us"}},
            "delivery": {
                "schedule_mode": "fixed",
                "hour": 8,
                "minute": 0,
                "frequency": "weekdays",
            },
        }

        self.assertEqual(delivery_cron(config), "0 0 * * 1-5")

    def test_invalid_schedule_mode_falls_back_to_market_open(self) -> None:
        normalized = normalize_report_delivery({"schedule_mode": "unexpected"})

        self.assertEqual(normalized["schedule_mode"], "market_open")

    def test_strategy_payload_exposes_automatic_market_open_label(self) -> None:
        config = default_strategy_config()
        config["parameters"]["market"] = {"enabled": True, "value": "us"}

        delivery = _strategy_delivery_payload(config)

        self.assertEqual(delivery["minute"], 30)
        self.assertIn(delivery["hour"], {21, 22})
        self.assertEqual(delivery["schedule_label"], "美股开盘 09:30（自动时区）")

    def test_scheduled_cli_stops_before_work_outside_opening_window(self) -> None:
        strategy = default_strategy_config()
        strategy["id"] = "us-strategy"
        strategy["lifecycle"]["stage"] = "paper"
        strategy["parameters"]["market"] = {"enabled": True, "value": "us"}
        strategy["delivery"]["schedule_mode"] = "market_open"

        with (
            patch.dict(
                "os.environ",
                {
                    "STOCK_AGENT_STRATEGY_ID": "us-strategy",
                    "STOCK_AGENT_MODE": "report",
                    "STOCK_AGENT_EXECUTION_KIND": "scheduled",
                    "STOCK_AGENT_SCHEDULE_GUARD": "0",
                },
                clear=False,
            ),
            patch(
                "stock_recommender.cli.find_strategy_config",
                return_value=strategy,
            ),
            patch(
                "stock_recommender.cli.should_publish_at_market_open",
                return_value=False,
            ) as opening_guard,
            patch("stock_recommender.cli.assert_strategy_runnable") as runnable,
        ):
            cli.main()

        opening_guard.assert_called_once_with(market="us")
        runnable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
