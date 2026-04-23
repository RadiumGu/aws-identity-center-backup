#!/usr/bin/env python3
"""
try_kiro_create_assignment.py — EXPERIMENTAL: attempt to automate Kiro seat
assignment via the private `q.amazonaws.com` CreateAssignment API.

Status: BEST-EFFORT / SPECULATIVE. The classmethod reverse-engineering note
(Feb 2026) observed `q:CreateAssignment` in CloudTrail but could not invoke
it externally — got 500 / UnknownOperation. This script tries several
plausible Coral protocol variants so that, when AWS changes the gating, we
can flip back to automation quickly. It writes full request/response to a
log file for forensic use.

If ALL variants fail: fall back to the manual Kiro console flow documented
in `docs/RUNBOOK.md` §6.X.

Usage:
  python3 try_kiro_create_assignment.py \
      --idc-arn $DST_IDC_ARN --region <region> \
      --principal-id <userId|groupId> --principal-type USER \
      --subscription-type KIRO_ENTERPRISE_PRO \
      --log out/try-assignment.log

Run one assignment first; if it succeeds, iterate the rest from the
KiroSubscriptions.json mapping.
"""
import argparse
import json
import sys
from datetime import datetime

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def attempt(service, region, method, url, headers, body, creds, log):
    req = AWSRequest(method=method, url=url, data=body, headers=headers)
    SigV4Auth(creds, service, region).add_auth(req)
    try:
        r = requests.request(method, req.url, headers=dict(req.headers),
                             data=req.body, timeout=30)
        status, text = r.status_code, r.text
    except Exception as e:  # noqa: BLE001
        status, text = -1, f"exception: {e}"
    log.write(f"\n=== {datetime.utcnow().isoformat()}Z {method} {url}\n")
    log.write(f"headers: {dict(headers)}\n")
    log.write(f"body:    {body}\n")
    log.write(f"status:  {status}\n")
    log.write(f"resp:    {text[:2000]}\n")
    return status, text


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--idc-arn", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--principal-id", required=True,
                   help="Identity Center user or group id")
    p.add_argument("--principal-type", required=True, choices=["USER", "GROUP"])
    p.add_argument("--subscription-type", required=True,
                   help="e.g. KIRO_ENTERPRISE_PRO / KIRO_ENTERPRISE / Q_DEVELOPER_PRO")
    p.add_argument("--log", default="try-assignment.log")
    args = p.parse_args()

    session = boto3.Session(region_name=args.region)
    creds = session.get_credentials().get_frozen_credentials()

    base_body = {
        "instanceArn": args.idc_arn,
        "principalId": args.principal_id,
        "principalType": args.principal_type,
        "subscriptionType": args.subscription_type,
    }

    logf = open(args.log, "a")
    logf.write(f"\n\n##### run {datetime.utcnow().isoformat()}Z #####\n")

    # Variant A: q.amazonaws.com Coral path (what CloudTrail showed)
    attempt(
        "q", args.region, "POST",
        f"https://q.{args.region}.amazonaws.com/CreateAssignment",
        {"Content-Type": "application/json"},
        json.dumps(base_body), creds, logf,
    )

    # Variant B: q with X-Amz-Target (Coral AWS/JSON style)
    attempt(
        "q", args.region, "POST",
        f"https://q.{args.region}.amazonaws.com/",
        {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AmazonQ.CreateAssignment",
        },
        json.dumps(base_body), creds, logf,
    )

    # Variant C: user-subscriptions CreateUserSubscription (guess by symmetry
    # with ListUserSubscriptions)
    attempt(
        "user-subscriptions", args.region, "POST",
        f"https://service.user-subscriptions.{args.region}.amazonaws.com/CreateUserSubscription",
        {"Content-Type": "application/json"},
        json.dumps(base_body), creds, logf,
    )

    # Variant D: user-subscriptions CreateClaim (observed ListApplicationClaims
    # / ListClaims — claim may be the noun)
    attempt(
        "user-subscriptions", args.region, "POST",
        f"https://service.user-subscriptions.{args.region}.amazonaws.com/CreateClaim",
        {"Content-Type": "application/json"},
        json.dumps(base_body), creds, logf,
    )

    logf.close()
    print(f"Tried 4 variants. Full request/response log in {args.log}.")
    print("If any returned HTTP 200 — congrats, update runbook with that variant.")
    print("Otherwise fall back to the manual Kiro console flow "
          "(docs/RUNBOOK.md §6.X).")


if __name__ == "__main__":
    sys.exit(main())
