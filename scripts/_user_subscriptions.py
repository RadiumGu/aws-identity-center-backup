"""_user_subscriptions.py — SigV4-signed wrapper around the private
`user-subscriptions` service (internal codename: Zorn).

This service is NOT registered in boto3 / AWS CLI / CloudFormation. All
calls go through direct HTTPS + SigV4 signing. Wrap the few operations we
need in a small client-shaped object so the rest of the codebase stays
readable.

⚠️ Contract is reverse-engineered and UNSTABLE:
  - Only ListUserSubscriptions is confirmed callable from outside the Kiro
    console (classmethod 2026-02 reverse engineering).
  - list_claims / list_application_claims / create_claim /
    set_overage_config / describe_overage_config call shapes below are
    best-guess: method/verb, path-based Coral, application/json body.
    Expect 500 or UnknownOperation on create_*; handle gracefully and fall
    back to the manual Kiro console flow (docs/KIRO-CUTOVER.md).

Reference: https://dev.classmethod.jp/en/articles/kiro-subscription-backend-zorn/
"""
from __future__ import annotations

import json
from typing import Any, Iterator

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

SERVICE = "user-subscriptions"


class _Resp:
    """Minimal ClientError-compatible response holder."""

    def __init__(self, status: int, body: dict | None, raw: str):
        self.status = status
        self.body = body or {}
        self.raw = raw


class UserSubscriptionsClient:
    """Mimics the small slice of boto3 client API we need."""

    def __init__(self, region: str | None = None, session: boto3.Session | None = None):
        session = session or boto3.Session()
        self.region = region or session.region_name or "us-east-1"
        creds = session.get_credentials()
        if creds is None:
            raise RuntimeError("No AWS credentials available for user-subscriptions client")
        self._creds = creds.get_frozen_credentials()
        self._host = f"service.{SERVICE}.{self.region}.amazonaws.com"

    def _call(self, op: str, payload: dict) -> dict:
        url = f"https://{self._host}/{op}"
        body = json.dumps(payload)
        req = AWSRequest(method="POST", url=url, data=body,
                         headers={"Content-Type": "application/json"})
        SigV4Auth(self._creds, SERVICE, self.region).add_auth(req)
        r = requests.post(req.url, headers=dict(req.headers), data=req.body, timeout=30)
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                return {}
        # Translate HTTP errors into a botocore-like ClientError so callers
        # can keep their existing except-ClientError branches.
        code = "HttpError"
        try:
            j = r.json()
            code = j.get("__type", j.get("Code", code))
            message = j.get("Message") or j.get("message") or r.text
        except ValueError:
            message = r.text
        raise ClientError(
            {"Error": {"Code": code, "Message": message},
             "ResponseMetadata": {"HTTPStatusCode": r.status_code}},
            op,
        )

    # --- operations used by backup/restore scripts ---------------------

    def list_user_subscriptions(self, InstanceArn: str, **kw) -> dict:
        payload = {"instanceArn": InstanceArn,
                   "subscriptionRegion": self.region,
                   "maxResults": kw.get("MaxResults", 1000)}
        if kw.get("NextToken"):
            payload["nextToken"] = kw["NextToken"]
        return self._call("ListUserSubscriptions", payload)

    def list_claims(self, ApplicationArn: str, **kw) -> dict:
        payload = {"applicationArn": ApplicationArn,
                   "maxResults": kw.get("MaxResults", 1000)}
        if kw.get("NextToken"):
            payload["nextToken"] = kw["NextToken"]
        return self._call("ListClaims", payload)

    def list_application_claims(self, ApplicationArn: str, **kw) -> dict:
        payload = {"applicationArn": ApplicationArn,
                   "maxResults": kw.get("MaxResults", 1000)}
        if kw.get("NextToken"):
            payload["nextToken"] = kw["NextToken"]
        return self._call("ListApplicationClaims", payload)

    def describe_overage_config(self, ApplicationArn: str) -> dict:
        return self._call("DescribeOverageConfig", {"applicationArn": ApplicationArn})

    def create_claim(self, ApplicationArn: str, ClaimType: str,
                     ClaimPrincipal: dict, **extra) -> dict:
        payload = {"applicationArn": ApplicationArn,
                   "claimType": ClaimType, "claimPrincipal": ClaimPrincipal}
        payload.update(extra)
        return self._call("CreateClaim", payload)

    def set_overage_config(self, ApplicationArn: str, OverageConfig: dict) -> dict:
        return self._call("SetOverageConfig",
                          {"applicationArn": ApplicationArn,
                           "overageConfig": OverageConfig})


def paginate_private(client_method, result_key: str, **kwargs) -> Iterator[Any]:
    """Paginate a private-API method that returns {'nextToken': ...} envelopes.
    Example:
        for claim in paginate_private(us.list_claims, "Claims",
                                      ApplicationArn=arn):
            ...
    """
    token = None
    while True:
        resp = client_method(**kwargs, **({"NextToken": token} if token else {}))
        for item in resp.get(result_key) or resp.get(result_key[0].lower() + result_key[1:]) or []:
            yield item
        token = resp.get("nextToken") or resp.get("NextToken")
        if not token:
            break
