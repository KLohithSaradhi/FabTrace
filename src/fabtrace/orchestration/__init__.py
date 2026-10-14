"""Local, checkpointed execution of the FabTrace learning pipeline."""

from fabtrace.orchestration.pipeline import (
    PIPELINE_VERSION,
    PipelineExecutionError,
    PipelineExecutionResult,
    execute_plan_pipeline,
)

__all__ = [
    "PIPELINE_VERSION",
    "PipelineExecutionError",
    "PipelineExecutionResult",
    "execute_plan_pipeline",
]
