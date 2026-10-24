"""Lazily created AWS clients and table handles (reused across warm invocations)."""

import os
from functools import lru_cache

import boto3
from botocore.config import Config

_CFG = Config(retries={"max_attempts": 4, "mode": "standard"}, connect_timeout=3, read_timeout=10)


@lru_cache(maxsize=None)
def client(name: str, **kw):
    if name == "s3" and not kw:
        # Regional virtual-hosted endpoint + SigV4 for presigned URLs. The global
        # endpoint answers 307 redirects for newly created buckets outside us-east-1,
        # which breaks browser uploads (found on the first real deployment).
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
        cfg = _CFG.merge(Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
        return boto3.client("s3", region_name=region, endpoint_url=f"https://s3.{region}.amazonaws.com", config=cfg)
    return boto3.client(name, config=kw.pop("config", _CFG), **kw)


@lru_cache(maxsize=None)
def resource_ddb():
    return boto3.resource("dynamodb", config=_CFG)


def jobs_table():
    return resource_ddb().Table(os.environ["JOBS_TABLE"])


def results_table():
    return resource_ddb().Table(os.environ["RESULTS_TABLE"])


def templates_table():
    return resource_ddb().Table(os.environ["TEMPLATES_TABLE"])


def env(name: str, default=None):
    return os.environ.get(name, default)
