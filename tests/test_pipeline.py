import unittest

from stock_recommender.pipeline import (
    PipelineContractError,
    PipelineRunner,
    StageInput,
    StageOutput,
)


class _Stage:
    def __init__(self, name, value):
        self.name = name
        self.value = value
        self.component_version = "1.0.0"

    def evaluate(self, stage_input):
        return StageOutput(
            stage=self.name,
            component_version="1.0.0",
            facts=({"value": self.value, "upstream": len(stage_input.upstream_facts)},),
        )


class PipelineTests(unittest.TestCase):
    def _input(self):
        return StageInput(
            run_id="run-1",
            strategy_id="strategy-1",
            strategy_version=3,
            as_of="2026-07-22T08:00:00+08:00",
            market_snapshot_id="market-1",
            portfolio_snapshot_id="portfolio-1",
        )

    def test_runner_passes_only_immutable_facts_between_stages(self):
        outputs = PipelineRunner([_Stage("screen", 1), _Stage("rank", 2)]).run(self._input())

        self.assertEqual([output.stage for output in outputs], ["screen", "rank"])
        self.assertEqual(outputs[0].facts[0]["upstream"], 0)
        self.assertEqual(outputs[1].facts[0]["upstream"], 1)

    def test_duplicate_stage_names_are_rejected(self):
        with self.assertRaises(PipelineContractError):
            PipelineRunner([_Stage("screen", 1), _Stage("screen", 2)])

    def test_stage_output_must_match_declared_stage(self):
        class BadStage:
            name = "screen"
            component_version = "1"

            def evaluate(self, stage_input):
                return StageOutput(stage="rank", component_version="1")

        with self.assertRaises(PipelineContractError):
            PipelineRunner([BadStage()]).run(self._input())


if __name__ == "__main__":
    unittest.main()
