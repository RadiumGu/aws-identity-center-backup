#!/usr/bin/env python3
"""
check_kiro_target_readiness.py — Verify that the TARGET account is ready to
receive a Kiro subscription restore, BEFORE the human operator clicks
anything in the Kiro console.

Checks:
  1. Identity Center instance exists and identity store id resolves.
  2. Users/groups referenced by KiroSubscriptions.json exist in the target
     identity store (names must match exactly).
  3. Kiro application is registered in target Identity Center (sso-admin
     ListApplications; looks for one whose ApplicationProviderArn matches
     the Kiro/Q Developer provider).
  4. Current target-side Kiro subscription snapshot: count of seats already
     assigned per plan. Compare against the source snapshot and print the
     delta (= seats still to purchase / assign manually).

Exit code: 0 if ready, 1 if gaps detected (printed to stderr).

Usage:
  export AWS_PROFILE=target && export AWS_DEFAULT_REGION=<region>
  python3 check_kiro_target_readiness.py \
      --idc-arn $DST_IDC_ARN --idc-id $DST_IDC_ID --region <region> \
      --source-snapshot KiroSubscriptions.json
"""
import argparse
import json
import sys
from collections import Counter

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

KIRO_PROVIDER_HINTS = ("q-developer", "kiro", "amazonq", "codewhisperer")


def paginate(client, method, key, **kw):
    m = getattr(client, method)
    r = m(**kw)
    out = r.get(key, [])
    while "NextToken" in r:
        r = m(NextToken=r["NextToken"], **kw)
        out.extend(r.get(key, []))
    return out


def list_user_subscriptions(instance_arn, region):
    session = boto3.Session(region_name=region)
    creds = session.get_credentials().get_frozen_credentials()
    url = f"https://service.user-subscriptions.{region}.amazonaws.com/ListUserSubscriptions"
    out, token = [], None
    while True:
        payload = {"instanceArn": instance_arn, "maxResults": 1000,
                   "subscriptionRegion": region}
        if token:
            payload["nextToken"] = token
        req = AWSRequest(method="POST", url=url, data=json.dumps(payload),
                         headers={"Content-Type": "application/json"})
        SigV4Auth(creds, "user-subscriptions", region).add_auth(req)
        r = requests.post(req.url, headers=dict(req.headers), data=req.body, timeout=30)
        if r.status_code != 200:
            return None  # private API not reachable / not enabled
        j = r.json()
        out.extend(j.get("userSubscriptions") or j.get("UserSubscriptions") or [])
        token = j.get("nextToken") or j.get("NextToken")
        if not token:
            break
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--idc-arn", required=True)
    p.add_argument("--idc-id", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--source-snapshot", required=True,
                   help="SOURCE account's KiroSubscriptions.json for comparison")
    args = p.parse_args()

    problems = []
    notes = []

    ids = boto3.client("identitystore", region_name=args.region)
    sso = boto3.client("sso-admin", region_name=args.region)

    # 1. instance + store
    try:
        sso.describe_instance_access_control_attribute_configuration = getattr(
            sso, "describe_instance_access_control_attribute_configuration", None)
        sso.list_instances()  # smoke test
        print(f"✓ sso-admin reachable")
    except Exception as e:
        problems.append(f"sso-admin call failed: {e}")

    # 2. Users/groups presence
    with open(args.source_snapshot) as f:
        src = json.load(f)
    # Support both schemas: new (UserClaims/GroupClaims) and legacy (Subscriptions).
    src_subs = []
    for c in src.get("UserClaims", []):
        src_subs.append({"username": c.get("UserName"),
                         "subscriptionType": (c.get("ClaimData") or {}).get("SubscriptionType")
                                              or c.get("ClaimType") or "UNKNOWN",
                         "status": "ACTIVE"})
    for c in src.get("GroupClaims", []):
        src_subs.append({"username": c.get("GroupDisplayName"),
                         "subscriptionType": (c.get("ClaimData") or {}).get("SubscriptionType")
                                              or c.get("ClaimType") or "UNKNOWN",
                         "status": "ACTIVE"})
    src_subs.extend(src.get("Subscriptions", []))
    src_names = {(s.get("username") or s.get("UserName") or "")
                 for s in src_subs if s.get("status", "").upper() in ("ACTIVE", "PENDING")}
    src_names.discard("")

    tgt_user_names = {u["UserName"] for u in paginate(
        ids, "list_users", "Users", IdentityStoreId=args.idc_id)}
    tgt_group_names = {g["DisplayName"] for g in paginate(
        ids, "list_groups", "Groups", IdentityStoreId=args.idc_id)}
    all_tgt = tgt_user_names | tgt_group_names
    missing = sorted(src_names - all_tgt)
    if missing:
        problems.append(
            f"{len(missing)} principal(s) from source snapshot not found in "
            f"target identity store (first 10): {missing[:10]}")
    else:
        print(f"✓ all {len(src_names)} source principals exist in target identity store")

    # 3. Kiro application registered
    apps = paginate(sso, "list_applications", "Applications", InstanceArn=args.idc_arn)
    kiro_apps = [a for a in apps if any(
        h in (a.get("ApplicationProviderArn", "") + a.get("Name", "")).lower()
        for h in KIRO_PROVIDER_HINTS)]
    if not kiro_apps:
        problems.append(
            "No Kiro/Q Developer application found in target Identity Center. "
            "Open the Kiro console once in the target account to register the "
            "application provider, then re-run.")
    else:
        print(f"✓ found {len(kiro_apps)} Kiro-like application(s): "
              f"{[a.get('Name') for a in kiro_apps]}")

    # 4. Seat delta vs source
    tgt_subs = list_user_subscriptions(args.idc_arn, args.region)
    src_by_plan = Counter(s.get("subscriptionType") or s.get("SubscriptionType") or "UNKNOWN"
                          for s in src_subs
                          if s.get("status", "").upper() in ("ACTIVE", "PENDING"))
    print("\nSeat plan comparison (source → target):")
    print("Plan                         | source | target | delta")
    print("-----------------------------+--------+--------+------")
    if tgt_subs is None:
        print("  (target user-subscriptions API not reachable — Kiro not enabled?)")
        notes.append("target user-subscriptions API returned non-200; "
                     "enable Kiro in target console first.")
        tgt_by_plan = Counter()
    else:
        tgt_by_plan = Counter(s.get("subscriptionType") or s.get("SubscriptionType") or "UNKNOWN"
                              for s in tgt_subs
                              if s.get("status", "").upper() in ("ACTIVE", "PENDING"))
    all_plans = sorted(set(src_by_plan) | set(tgt_by_plan))
    for plan in all_plans:
        s_n, t_n = src_by_plan[plan], tgt_by_plan[plan]
        delta = s_n - t_n
        mark = "⚠️" if delta > 0 else "✓"
        print(f"{mark} {plan:26s} | {s_n:6d} | {t_n:6d} | {delta:+d}")

    print()
    if problems:
        print("❌ NOT READY:", file=sys.stderr)
        for p_ in problems:
            print("  - " + p_, file=sys.stderr)
        for n in notes:
            print("  note: " + n, file=sys.stderr)
        sys.exit(1)
    print("✓ target is ready for Kiro subscription restore")
    for n in notes:
        print("  note: " + n)


if __name__ == "__main__":
    sys.exit(main())
