"""Count observed summary usage once, without turning missing data into zero."""
from sqlalchemy import select

from .models import ArtifactVersion, OperationRecord


def include_summary_usage(db, run_id, main_usage):
    operations = list(db.scalars(select(OperationRecord).where(
        OperationRecord.run_id == run_id, OperationRecord.node_id == "chat:context_summary")))
    if not operations:
        return main_usage
    artifacts = list(db.scalars(select(ArtifactVersion).where(
        ArtifactVersion.run_id == run_id, ArtifactVersion.kind == "context_summary")))
    by_operation = {row.operation_id: row for row in artifacts}
    complete = all(row.operation_id in by_operation and row.status == "completed" for row in operations)
    usages = [row.metadata_json.get("usage") or {} for row in by_operation.values()]
    result = dict(main_usage or {})
    result["summary_model_calls"] = len(operations)
    for key, alias in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                       ("total_tokens", "total_tokens")):
        values = [usage.get(key) for usage in usages]
        summary = sum(values) if complete and values and all(type(value) is int for value in values) else None
        result["summary_" + key] = summary
        main = (main_usage or {}).get(key, (main_usage or {}).get(alias))
        result[key] = main + summary if main is not None and summary is not None else None
    result["cost_usd"] = None  # Summary billing has no provider invoice in this layer.
    return result
