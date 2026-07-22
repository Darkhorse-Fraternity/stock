from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence


class PipelineContractError(ValueError):
    """Raised when a pipeline stage violates the shared contract."""


@dataclass(frozen=True)
class StageInput:
    run_id: str
    strategy_id: str
    strategy_version: int
    as_of: str
    market_snapshot_id: str
    portfolio_snapshot_id: str
    upstream_facts: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StageOutput:
    stage: str
    component_version: str
    schema_version: int = 1
    facts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    diagnostics: tuple[dict[str, Any], ...] = field(default_factory=tuple)


class PipelineStage(Protocol):
    name: str
    component_version: str

    def evaluate(self, stage_input: StageInput) -> StageOutput: ...


def _validate_output(stage: PipelineStage, output: StageOutput) -> None:
    if not isinstance(output, StageOutput):
        raise PipelineContractError(f"{stage.name} 必须返回 StageOutput")
    if output.stage != stage.name:
        raise PipelineContractError(f"阶段名不匹配: {stage.name} != {output.stage}")
    if output.component_version != stage.component_version:
        raise PipelineContractError(f"组件版本不匹配: {stage.name}")
    if output.schema_version < 1:
        raise PipelineContractError(f"{stage.name} schema_version 无效")
    for fact in output.facts:
        if not isinstance(fact, dict):
            raise PipelineContractError(f"{stage.name} facts 必须是对象")
    for diagnostic in output.diagnostics:
        if not isinstance(diagnostic, dict):
            raise PipelineContractError(f"{stage.name} diagnostics 必须是对象")


class PipelineRunner:
    """Pure stage orchestration. Stages never mutate portfolio state directly."""

    def __init__(self, stages: Sequence[PipelineStage]):
        names = [stage.name for stage in stages]
        if len(names) != len(set(names)):
            raise PipelineContractError("Pipeline 阶段名必须唯一")
        self._stages = tuple(stages)

    def run(self, stage_input: StageInput) -> tuple[StageOutput, ...]:
        outputs: list[StageOutput] = []
        upstream = tuple(deepcopy(item) for item in stage_input.upstream_facts)
        for stage in self._stages:
            current = StageInput(
                run_id=stage_input.run_id,
                strategy_id=stage_input.strategy_id,
                strategy_version=stage_input.strategy_version,
                as_of=stage_input.as_of,
                market_snapshot_id=stage_input.market_snapshot_id,
                portfolio_snapshot_id=stage_input.portfolio_snapshot_id,
                upstream_facts=upstream,
            )
            output = stage.evaluate(current)
            _validate_output(stage, output)
            outputs.append(output)
            upstream = (*upstream, *(deepcopy(item) for item in output.facts))
        return tuple(outputs)
