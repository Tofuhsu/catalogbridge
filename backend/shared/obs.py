"""Structured logging and CloudWatch Embedded Metric Format (EMF) metrics.

Logs are one JSON object per line. Never log JWTs, credentials or whole user
files; log identifiers (jobId, runRequestId, rowNumber, messageId) instead.
"""

import json
import os
import sys
import time

NAMESPACE = os.environ.get("METRICS_NAMESPACE", "CatalogBridge")
SERVICE = os.environ.get("SERVICE_NAME", "catalogbridge")


def log(level: str, msg: str, **fields):
    rec = {"level": level, "msg": msg, "service": SERVICE, "ts": int(time.time() * 1000)}
    rec.update({k: v for k, v in fields.items() if v is not None})
    print(json.dumps(rec, default=str), file=sys.stdout, flush=True)


def info(msg, **fields):
    log("INFO", msg, **fields)


def warn(msg, **fields):
    log("WARN", msg, **fields)


def error(msg, **fields):
    log("ERROR", msg, **fields)


def metric(name: str, value: float = 1, unit: str = "Count", dims: dict | None = None, **props):
    """Emit one EMF metric. CloudWatch extracts it from the log line, no API call."""
    dims = dims or {"Service": SERVICE}
    rec = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [list(dims.keys())],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        name: value,
        **dims,
    }
    rec.update({k: v for k, v in props.items() if v is not None})
    print(json.dumps(rec, default=str), file=sys.stdout, flush=True)
