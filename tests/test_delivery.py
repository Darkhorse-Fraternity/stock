import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from stock_recommender.delivery import delivery_cron, delivery_target, should_deliver_report, sync_active_strategy_delivery, sync_hermes_delivery
from stock_recommender.parameters import default_strategy_config, normalize_strategy_config


class DeliveryTests(unittest.TestCase):
    def test_missing_delivery_uses_disabled_current_default(self):
        with patch.dict(os.environ, {}, clear=True):
            strategy = normalize_strategy_config({"name": "当前策略", "parameters": {}})

        self.assertFalse(strategy["delivery"]["enabled"])
        self.assertEqual(strategy["delivery"]["target"], "")

    def test_new_strategy_defaults_to_delivery_disabled(self):
        delivery = default_strategy_config()["delivery"]
        self.assertFalse(delivery["enabled"])
        self.assertEqual((delivery["frequency"], delivery["hour"], delivery["minute"]), ("weekdays", 8, 0))

    def test_beijing_schedule_is_converted_to_utc_cron(self):
        strategy = default_strategy_config()
        strategy["delivery"].update({"hour": 9, "minute": 30, "frequency": "weekdays"})

        self.assertEqual(delivery_cron(strategy), "30 1 * * 1-5")

    def test_early_beijing_weekday_schedule_shifts_utc_weekdays(self):
        strategy = default_strategy_config()
        strategy["delivery"].update(
            {
                "schedule_mode": "fixed",
                "hour": 7,
                "minute": 30,
                "frequency": "weekdays",
            }
        )

        self.assertEqual(delivery_cron(strategy), "30 23 * * 0-4")

    def test_platform_delivery_target_includes_channel(self):
        strategy = default_strategy_config()
        strategy["delivery"].update({"channel": "feishu", "target": "oc_test"})

        self.assertEqual(delivery_target(strategy), "feishu:oc_test")

    def test_sync_edits_and_resumes_enabled_job(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        strategy = default_strategy_config()
        strategy["lifecycle"]["stage"] = "paper"
        strategy["delivery"]["schedule_mode"] = "fixed"
        strategy["delivery"].update({"enabled": True, "channel": "feishu", "target": "oc_test", "hour": 8, "minute": 0, "frequency": "daily"})
        with patch.dict(os.environ, {"STOCK_AGENT_HERMES_JOB_ID": "job-1", "STOCK_AGENT_HERMES_BIN": "hermes"}):
            result = sync_hermes_delivery(strategy, runner=runner)

        self.assertEqual(result["status"], "synced")
        self.assertEqual(calls[0], ["hermes", "cron", "edit", "job-1", "--schedule", "0 0 * * *", "--deliver", "feishu:oc_test"])
        self.assertEqual(calls[1], ["hermes", "cron", "resume", "job-1"])

    def test_sync_pauses_disabled_job(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        with patch.dict(os.environ, {"STOCK_AGENT_HERMES_JOB_ID": "job-1", "STOCK_AGENT_HERMES_BIN": "hermes"}):
            result = sync_hermes_delivery(default_strategy_config(), runner=runner)

        self.assertEqual(result["status"], "paused")
        self.assertEqual(calls, [["hermes", "cron", "pause", "job-1"]])

    def test_syncs_hourly_portfolio_and_five_minute_risk_jobs(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        strategy = default_strategy_config()
        strategy["lifecycle"]["stage"] = "paper"
        strategy["delivery"].update({"enabled": True, "channel": "feishu", "target": "oc_test", "hour": 8, "minute": 0})
        environment = {
            "STOCK_AGENT_HERMES_JOB_ID": "daily-job",
            "STOCK_AGENT_HERMES_TRACKING_JOB_ID": "hourly-job",
            "STOCK_AGENT_HERMES_RISK_JOB_ID": "risk-job",
            "STOCK_AGENT_HERMES_BIN": "hermes",
        }
        with patch.dict(os.environ, environment, clear=True):
            result = sync_hermes_delivery(strategy, runner=runner)

        self.assertEqual(result["status"], "synced")
        self.assertEqual([job["kind"] for job in result["jobs"]], ["daily", "tracking", "risk"])
        self.assertIn(["hermes", "cron", "edit", "hourly-job", "--schedule", "0 2,3,5,6,7 * * 1-5", "--deliver", "feishu:oc_test"], calls)
        self.assertIn(["hermes", "cron", "edit", "risk-job", "--schedule", "*/5 1-7 * * 1-5", "--deliver", "feishu:oc_test"], calls)

    def test_missing_active_strategy_does_not_modify_hermes(self):
        with patch("stock_recommender.delivery.load_strategy_config", return_value=default_strategy_config()):
            result = sync_active_strategy_delivery()

        self.assertEqual(result["status"], "unavailable")

    def test_empty_and_error_reports_follow_delivery_policy(self):
        strategy = default_strategy_config()
        strategy["lifecycle"]["stage"] = "paper"
        strategy["delivery"].update({"enabled": True, "push_on_empty": False, "push_on_error": False})

        self.assertFalse(should_deliver_report("当前策略无匹配股票", strategy))
        self.assertFalse(should_deliver_report("实时行情不可用", strategy))
        self.assertTrue(should_deliver_report("今日候选：测试股票", strategy))

    def test_draft_strategy_is_paused_even_when_delivery_is_enabled(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        strategy = default_strategy_config()
        strategy["delivery"].update({"enabled": True, "channel": "feishu", "target": "oc_test"})
        with patch.dict(os.environ, {"STOCK_AGENT_HERMES_JOB_ID": "job-1"}):
            result = sync_hermes_delivery(strategy, runner=runner)

        self.assertEqual(result["status"], "paused")
        self.assertEqual(calls, [["hermes", "cron", "pause", "job-1"]])
        self.assertIn("draft", result["message"])


if __name__ == "__main__":
    unittest.main()
