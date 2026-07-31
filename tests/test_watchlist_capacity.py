from __future__ import annotations

import unittest

from stock_recommender.parameters import (
    default_strategy_config,
    normalize_strategy_config,
)


class WatchlistCapacityTests(unittest.TestCase):
    def test_watchlist_accepts_more_than_thirty_symbols(self) -> None:
        config = default_strategy_config()
        symbols = [f"TEST{index:03d}" for index in range(34)]
        config["parameters"]["market"] = {"enabled": True, "value": "us"}
        config["parameters"]["watchlist"] = {"enabled": True, "value": symbols}

        normalized = normalize_strategy_config(config)

        self.assertEqual(
            normalized["parameters"]["watchlist"]["value"],
            symbols,
        )

    def test_other_tag_parameters_keep_the_default_limit(self) -> None:
        config = default_strategy_config()
        filters = [f"sector-{index:03d}" for index in range(34)]
        config["parameters"]["sector_filters"] = {
            "enabled": True,
            "value": filters,
        }

        normalized = normalize_strategy_config(config)

        self.assertEqual(
            normalized["parameters"]["sector_filters"]["value"],
            filters[:30],
        )


if __name__ == "__main__":
    unittest.main()
