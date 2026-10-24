"""Small DynamoDB helpers."""

import time
from decimal import Decimal

from botocore.exceptions import ClientError


def now_ms() -> int:
    return int(time.time() * 1000)


def is_tx_conflict(e: Exception) -> bool:
    return isinstance(e, ClientError) and e.response["Error"]["Code"] == "TransactionConflictException"


def retry_on_tx_conflict(fn, attempts: int = 5):
    """Run fn(); retry with jitter while a concurrent transaction holds the same item.

    Non-transactional writes to the Job item conflict with in-flight worker
    transactions on that item (observed in the duplicate-delivery experiment).
    """
    import random

    for i in range(attempts):
        try:
            return fn()
        except ClientError as e:
            if not is_tx_conflict(e) or i == attempts - 1:
                raise
            time.sleep(random.uniform(0, 0.05 * (2 ** i)))


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
