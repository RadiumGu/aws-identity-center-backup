#!/usr/bin/env python3
"""
backup_kiro_subscriptions.py — Snapshot Kiro / Q Developer subscription
assignments from the SOURCE account.

⚠️ This uses an UNDOCUMENTED PRIVATE AWS API (internal codename "Zorn",
service name `user-subscriptions`). It is NOT available in boto3/CLI. We
call it directly via SigV4 signing.

  Reference: https://dev.classmethod.jp/en/articles/kiro-subscription-backend-zorn/

Because this API is private:
  - AWS may change or remove it at any time without notice.
  - Only the READ path (ListUserSubscriptions) is known to work from outside
    the Kiro console. Creation (q:CreateAssignment) is not reliably callable.
  - Use the output as an AUDIT / RESTORE CHECKLIST, not as a deploy input.

Output: KiroSubscriptions.json
  {
    "InstanceArn": "...",
    "Region": "...",
    "CapturedAt": "2026-04-23T15:30:00Z",
    "Subscriptions": [
      {"username":"...", "subscriptionType":"KIRO_ENTERPRISE_PRO",
       "status":"ACTIVE", "aggregated":"GROUP", "activated":"2025-12-12"},
      ...
    ]
  }

Plus a human-readable summary printed to stdout, grouped by subscription
type, so the target-account admin knows how many seats to purchase.

Usage:
  export AWS_PROFILE=source-account
  python3 backup_kiro_subscriptions.py \
      --idc-arn arn:aws:sso:::instance/ssoins-xxxxxxxxxxxxxxxx \
      --region us-east-1
"""
import argparse
import datetime as dt
import json
import sys
from collections import Counter

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

SERVICE = "user-subscriptions"


def call_list_user_subscriptions(instance_arn, region, max_results=1000):
    session = boto3.Session(region_name=region)
    creds = session.get_credentials()
    if creds is None:
        sys.exit("No AWS credentials found (AWS_PROFILE / env vars / IMDS).")
    creds = creds.get_frozen_credentials()

    url = f"https://service.{SERVICE}.{region}.amazonaws.com/ListUserSubscriptions"
    results = []
    next_token = None
    while True:
        payload = {
            "instanceArn": instance_arn,
            "maxResults": max_results,
            "subscriptionRegion": region,
        }
        if next_token:
            payload["nextToken"] = next_token
        body = json.dumps(payload)

        req = AWSRequest(
            method="POST",
            url=url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(creds, SERVICE, region).add_auth(req)
        resp = requests.post(req.url, headers=dict(req.headers), data=req.body, timeout=30)
        if resp.status_code != 200:
            sys.exit(f"ListUserSubscriptions HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        for item in data.get("userSubscriptions", []) or data.get("UserSubscriptions", []):
            results.append(item)
        next_token = data.get("nextToken") or data.get("NextToken")
        if not next_token:
            break
    return results


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--idc-arn", required=True, help="Identity Center instance ARN")
    p.add_argument("--region", required=True,
                   help="Identity Center region (e.g. us-east-1)")
    p.add_argument("--output", default="KiroSubscriptions.json")
    args = p.parse_args()

    subs = call_list_user_subscriptions(args.idc_arn, args.region)

    snapshot = {
        "InstanceArn": args.idc_arn,
        "Region": args.region,
        "CapturedAt": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "Subscriptions": subs,
    }
    with open(args.output, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)

    by_type = Counter()
    by_type_active = Counter()
    aggregated = Counter()
    for s in subs:
        t = s.get("subscriptionType") or s.get("SubscriptionType") or "UNKNOWN"
        st = (s.get("status") or s.get("Status") or "").upper()
        ag = s.get("aggregated") or s.get("Aggregated") or "USER"
        by_type[t] += 1
        if st == "ACTIVE":
            by_type_active[t] += 1
        aggregated[ag] += 1

    print(f"\nWrote {args.output} ({len(subs)} subscriptions)\n")
    print("Subscription type            | total | active")
    print("-----------------------------+-------+-------")
    for t in sorted(by_type):
        print(f"{t:28s} | {by_type[t]:5d} | {by_type_active[t]:5d}")
    print("\nAssignment source:", dict(aggregated))
    print(
        "\nNext step on the TARGET account:\n"
        "  1. Amazon Q Developer / Kiro console → Subscriptions → buy matching\n"
        "     seat counts per plan (numbers above).\n"
        "  2. Run scripts/kiro_restore_checklist.py against this file to\n"
        "     print the per-user/group assignment actions to perform.\n"
    )


if __name__ == "__main__":
    sys.exit(main())
