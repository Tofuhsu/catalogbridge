"""Small DynamoDB helpers."""

import time
from decimal import Decimal

from botocore.exceptions import ClientError


def now_ms() -> int:
    return int(time.time() * 1000)


def is_conditional_fail(e: Exception) -> bool:
    return isinstance(e, ClientError) and e.response["Error"]["Code"] == "ConditionalCheckFailedException"


def plain(value):
    """Convert DynamoDB Decimals into int/float for JSON responses."""
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def get_job(job_id: str, consistent: bool = True):
    from shared.aws import jobs_table

    return jobs_table().get_item(Key={"jobId": job_id}, ConsistentRead=consistent).get("Item")


def as_int(v, default=0) -> int:
    return default if v is None else int(v)
