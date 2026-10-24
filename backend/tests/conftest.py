import json
import os
import sys
import uuid

import boto3
import pytest
from moto import mock_aws

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REGION = "us-west-2"


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("FAULTS_PARAM", raising=False)
    with mock_aws():
        from shared import aws as shared_aws
        from shared import faults

        shared_aws.client.cache_clear()
        shared_aws.resource_ddb.cache_clear()
        faults.reset_cache()

        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName="Jobs", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"},
                                  {"AttributeName": "ownerId", "AttributeType": "S"},
                                  {"AttributeName": "createdAt", "AttributeType": "N"}],
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{"IndexName": "byOwner",
                                     "KeySchema": [{"AttributeName": "ownerId", "KeyType": "HASH"},
                                                   {"AttributeName": "createdAt", "KeyType": "RANGE"}],
                                     "Projection": {"ProjectionType": "ALL"}}],
        )
        ddb.create_table(
            TableName="Results", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"},
                                  {"AttributeName": "rowNumber", "AttributeType": "N"}],
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"},
                       {"AttributeName": "rowNumber", "KeyType": "RANGE"}],
        )
        ddb.create_table(
            TableName="Templates", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "ownerId", "AttributeType": "S"},
                                  {"AttributeName": "templateId", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "ownerId", "KeyType": "HASH"},
                       {"AttributeName": "templateId", "KeyType": "RANGE"}],
        )
        s3 = boto3.client("s3", region_name=REGION)
        for b in ("raw-bucket", "work-bucket"):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_bucket_versioning(Bucket="raw-bucket", VersioningConfiguration={"Status": "Enabled"})
        sqs = boto3.client("sqs", region_name=REGION)
        work_url = sqs.create_queue(QueueName="work")["QueueUrl"]
        ingest_url = sqs.create_queue(QueueName="ingest")["QueueUrl"]
        ssm = boto3.client("ssm", region_name=REGION)
        ssm.put_parameter(Name="/cb/faults", Value="{}", Type="String")

        env = {"JOBS_TABLE": "Jobs", "RESULTS_TABLE": "Results", "TEMPLATES_TABLE": "Templates",
               "RAW_BUCKET": "raw-bucket", "WORK_BUCKET": "work-bucket",
               "WORK_QUEUE_URL": work_url, "INGEST_QUEUE_URL": ingest_url}
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        yield {"ddb": ddb, "s3": s3, "sqs": sqs, "ssm": ssm, **env}


def set_faults(aws, monkeypatch, cfg: dict):
    from shared import faults

    monkeypatch.setenv("FAULTS_PARAM", "/cb/faults")
    aws["ssm"].put_parameter(Name="/cb/faults", Value=json.dumps(cfg), Type="String", Overwrite=True)
    faults.reset_cache()


def api_event(route, owner, body=None, path=None, query=None):
    return {
        "routeKey": route,
        "pathParameters": path or {},
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": owner}}}},
    }


def call(route, owner, body=None, path=None, query=None):
    from api import app

    r = app.handler(api_event(route, owner, body, path, query), None)
    return r["statusCode"], json.loads(r["body"])


def drain(aws, queue_key, max_msgs=1000):
    """Receive all messages from a queue as Lambda SQS records (deleting them)."""
    out = []
    while len(out) < max_msgs:
        r = aws["sqs"].receive_message(QueueUrl=aws[queue_key], MaxNumberOfMessages=10,
                                       AttributeNames=["All"], WaitTimeSeconds=0)
        msgs = r.get("Messages", [])
        if not msgs:
            break
        for m in msgs:
            out.append({"messageId": m["MessageId"], "body": m["Body"], "attributes": m.get("Attributes", {})})
            aws["sqs"].delete_message(QueueUrl=aws[queue_key], ReceiptHandle=m["ReceiptHandle"])
    return out


def new_token():
    return str(uuid.uuid4())
