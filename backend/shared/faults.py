"""Test-only fault injection.

Disabled unless the function has FAULTS_PARAM set, which CDK only does when the
stack is deployed with `-c faultInjection=true`. The admin CLI
(scripts/faults.py) writes a JSON document to that SSM parameter, e.g.

    {"worker": {"crash_after_commit_rows": [3], "fail_rows": [7], "slow_ms": 200},
     "importer": {"crash_after_sent": 120},
     "lifecycle": {"crash_after_report_write": true}}

There is no public API for this; only someone with AWS credentials can turn it on.
"""

import json
import os
import time

_cache = {"at": 0.0, "value": {}}
_TTL = 5.0


class InjectedFault(Exception):
    """A deliberate, test-only system failure."""


def get(component: str) -> dict:
    param = os.environ.get("FAULTS_PARAM")
    if not param:
        return {}
    now = time.time()
    if now - _cache["at"] > _TTL:
        from shared.aws import client

        try:
            raw = client("ssm").get_parameter(Name=param)["Parameter"]["Value"]
            _cache["value"] = json.loads(raw or "{}")
        except Exception:  # fault config must never break real processing
            _cache["value"] = {}
        _cache["at"] = now
    return _cache["value"].get(component, {}) or {}


def reset_cache():
    _cache["at"] = 0.0
    _cache["value"] = {}
