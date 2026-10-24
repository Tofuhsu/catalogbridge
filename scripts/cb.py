"""Shared helpers for operator / test scripts. Uses your local AWS credentials (never stored in the repo).

Stack outputs are read from CloudFormation, so scripts need no hard-coded resource names.
"""

import json
import os
import secrets
import ssl
import string
import time
import urllib.error
import urllib.request
import uuid
from functools import lru_cache

import boto3

try:  # python.org macOS builds ship without a CA bundle
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()


def urlopen(req, timeout=30):
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL)

REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
STACK = os.environ.get("CB_STACK", "CatalogBridge")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
EVIDENCE = os.path.join(ROOT, "docs", "evidence")


def aws(name):
    return boto3.client(name, region_name=REGION)


@lru_cache(maxsize=None)
def outputs() -> dict:
    stack = aws("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def save_evidence(name: str, data) -> str:
    os.makedirs(EVIDENCE, exist_ok=True)
    path = os.path.join(EVIDENCE, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    return path


# ------------------------------------------------------------------ ephemeral test users

class TestUser:
    """A throwaway Cognito user created by and for automated tests.

    The password is random, generated in memory, never printed or saved, and the
    user is deleted in close(). Requires IAM admin credentials on the user pool.
    """

    def __init__(self, label: str):
        self.idp = aws("cognito-idp")
        self.pool = outputs()["UserPoolId"]
        self.client_id = outputs()["AutomationClientId"]
        self.email = f"cb-test-{label}-{uuid.uuid4().hex[:8]}@example.com"
        pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24)) + "aA1"
        self.idp.admin_create_user(UserPoolId=self.pool, Username=self.email, MessageAction="SUPPRESS",
                                   UserAttributes=[{"Name": "email", "Value": self.email},
                                                   {"Name": "email_verified", "Value": "true"}])
        self.idp.admin_set_user_password(UserPoolId=self.pool, Username=self.email, Password=pw, Permanent=True)
        r = self.idp.admin_initiate_auth(UserPoolId=self.pool, ClientId=self.client_id, AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                                         AuthParameters={"USERNAME": self.email, "PASSWORD": pw})
        del pw
        self.token = r["AuthenticationResult"]["IdToken"]
        self.api = Api(self.token)

    def close(self):
        try:
            self.idp.admin_delete_user(UserPoolId=self.pool, Username=self.email)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Api:
    def __init__(self, token: str):
        self.base = outputs()["ApiUrl"]
        self.token = token

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def upload(self, form, data: bytes, filename="input.csv"):
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in form["fields"].items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                     f"Content-Type: text/csv\r\n\r\n".encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(form["url"], data=b"".join(parts), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urlopen(req, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    # convenience flows -------------------------------------------------
    def create_and_upload(self, data: bytes, currency="USD", supplier="acme", parent=None, name="input.csv"):
        body = {"clientToken": str(uuid.uuid4()), "currency": currency, "supplierId": supplier, "fileName": name}
        if parent:
            body["parentJobId"] = parent
        st, out = self.call("POST", "/jobs", body)
        assert st == 201, (st, out)
        up = self.upload(out["upload"], data, name)
        assert up in (200, 204), up
        return out["job"]["jobId"]

    def wait(self, job_id, until, timeout=180, poll=1.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, out = self.call("GET", f"/jobs/{job_id}")
            if st == 200 and out["job"]["status"] in until:
                return out["job"]
            time.sleep(poll)
        raise TimeoutError(f"job {job_id} did not reach {until}; last={out.get('job', {}).get('status')}")

    def map_and_run(self, job_id, mapping, source="manual"):
        st, out = self.call("GET", f"/jobs/{job_id}")
        v = out["job"]["mappingVersion"]
        st, out = self.call("PUT", f"/jobs/{job_id}/mapping", {"mapping": mapping, "mappingVersion": v, "source": source})
        assert st == 200, (st, out)
        run_id = str(uuid.uuid4())
        st, out = self.call("POST", f"/jobs/{job_id}/run", {"runRequestId": run_id, "mappingVersion": v + 1})
        assert st == 202, (st, out)
        return run_id


def fixture(name) -> bytes:
    with open(os.path.join(ROOT, "fixtures", name), "rb") as f:
        return f.read()


STD = {"sku": "SKU", "title": "Title", "price": "Price", "stock": "Stock"}
