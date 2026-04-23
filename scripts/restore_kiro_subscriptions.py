#!/usr/bin/env python3
"""
restore_kiro_subscriptions.py — Recreate Kiro subscription claims in TARGET.

PREREQUISITE (manual, cannot be scripted):
  1. In the TARGET AWS account, open the Kiro console and subscribe to Kiro
     enterprise. This causes Kiro to automatically create:
       - an IdC Application (with Auth Method / Grant / TIP configuration)
       - the AWSServiceRoleForUserSubscriptions / AWSServiceRoleForAmazonQDeveloper SLRs
     DO NOT try to recreate this IdC Application via sso-admin APIs — Kiro
     tweaks the authentication/grant layout per release and hand-rolled
     applications will drift.
  2. Run restore_users_groups.py + restore_permission_sets.py on TARGET first
     so that UserName/GroupDisplayName exist in the target IdC.
  3. Then run this script.

Usage:
  export AWS_PROFILE=target-account
  export AWS_DEFAULT_REGION=<region>
  python3 restore_kiro_subscriptions.py --idc-arn arn:aws:sso:::instance/ssoins-NEW \
                                       --idc-id d-NEWxxxxxxxx \
                                       [--dry-run]

IAM permissions required:
  sso:ListApplications
  user-subscriptions:CreateClaim, ListClaims, SetOverageConfig
  identitystore:ListUsers, ListGroups
"""
import argparse
import json
import logging
import sys

import boto3
from botocore.exceptions import ClientError

from _user_subscriptions import UserSubscriptionsClient

log = logging.getLogger("restore_kiro_subscriptions")


def paginate(client, method_name, result_key, **kwargs):
    paginator = client.get_paginator(method_name)
    for page in paginator.paginate(**kwargs):
        yield from page.get(result_key, [])


def find_kiro_application(sso, instance_arn):
    for app in paginate(sso, "list_applications", "Applications", InstanceArn=instance_arn):
        provider = app.get("ApplicationProviderArn", "")
        if "q.amazonaws.com" in provider or "kiro" in provider.lower():
            return app["ApplicationArn"]
    return None


def build_lookups(ids, idc_id):
    users = {u["UserName"]: u["UserId"]
             for u in paginate(ids, "list_users", "Users", IdentityStoreId=idc_id)}
    groups = {g["DisplayName"]: g["GroupId"]
              for g in paginate(ids, "list_groups", "Groups", IdentityStoreId=idc_id)}
    return users, groups


def restore_user_claims(us, app_arn, claims, users_map, dry_run):
    created = skipped = 0
    for c in claims:
        name = c.get("UserName")
        uid = users_map.get(name)
        if not uid:
            log.warning("skip user claim, UserName not in target: %s", name)
            skipped += 1
            continue
        if dry_run:
            log.info("[dry-run] would CreateClaim for user %s", name)
            continue
        try:
            us.create_claim(
                ApplicationArn=app_arn,
                ClaimType=c.get("ClaimType", "UserSubscription"),
                ClaimPrincipal={"UserId": uid},
                **c.get("ClaimData", {}),
            )
            created += 1
        except ClientError as e:
            if e.response["Error"]["Code"] in ("ConflictException", "ResourceAlreadyExistsException"):
                continue
            log.error("create_claim user %s failed: %s", name, e)
    log.info("user claims created: %d, skipped: %d", created, skipped)


def restore_group_claims(us, app_arn, claims, groups_map, dry_run):
    created = skipped = 0
    for c in claims:
        name = c.get("GroupDisplayName")
        gid = groups_map.get(name)
        if not gid:
            log.warning("skip group claim, GroupDisplayName not in target: %s", name)
            skipped += 1
            continue
        if dry_run:
            log.info("[dry-run] would CreateClaim for group %s", name)
            continue
        try:
            us.create_claim(
                ApplicationArn=app_arn,
                ClaimType=c.get("ClaimType", "GroupSubscription"),
                ClaimPrincipal={"GroupId": gid},
                **c.get("ClaimData", {}),
            )
            created += 1
        except ClientError as e:
            if e.response["Error"]["Code"] in ("ConflictException", "ResourceAlreadyExistsException"):
                continue
            log.error("create_claim group %s failed: %s", name, e)
    log.info("group claims created: %d, skipped: %d", created, skipped)


def apply_overage(us, app_arn, cfg, dry_run):
    if not cfg:
        return
    if dry_run:
        log.info("[dry-run] would SetOverageConfig %s", cfg)
        return
    try:
        us.set_overage_config(ApplicationArn=app_arn, OverageConfig=cfg)
        log.info("overage config applied")
    except ClientError as e:
        log.error("set_overage_config failed: %s", e)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--idc-arn", required=True, help="TARGET Identity Center instance ARN")
    p.add_argument("--idc-id", required=True, help="TARGET Identity Store ID")
    p.add_argument("--input", default="KiroSubscriptions.json")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--logging", default="INFO")
    args = p.parse_args()
    logging.basicConfig(level=args.logging, format="%(asctime)s %(levelname)s %(message)s")

    print(
        "\n"
        "================================================================\n"
        "⚠️  user-subscriptions is a PRIVATE AWS API with no public SDK.\n"
        "   External SigV4 calls to CreateClaim / SetOverageConfig have been\n"
        "   observed to return HTTP 500 / UnknownOperation (AWS gating).\n"
        "   If this script fails, follow the MANUAL cutover in\n"
        "   docs/KIRO-CUTOVER.md (Kiro console → Users & Groups → Add).\n"
        "================================================================\n"
    )

    with open(args.input) as f:
        data = json.load(f)

    sso = boto3.client("sso-admin")
    us = UserSubscriptionsClient()
    ids = boto3.client("identitystore")

    app_arn = find_kiro_application(sso, args.idc_arn)
    if not app_arn:
        log.error(
            "No Kiro application in TARGET Identity Center. "
            "Subscribe to Kiro enterprise via the Kiro console FIRST, then re-run."
        )
        sys.exit(2)
    log.info("target Kiro application: %s", app_arn)

    users_map, groups_map = build_lookups(ids, args.idc_id)
    log.info("target IdC: %d users, %d groups indexed", len(users_map), len(groups_map))

    restore_user_claims(us, app_arn, data.get("UserClaims", []), users_map, args.dry_run)
    restore_group_claims(us, app_arn, data.get("GroupClaims", []), groups_map, args.dry_run)
    apply_overage(us, app_arn, data.get("OverageConfig"), args.dry_run)

    print(f"Processed {len(data.get('UserClaims', []))} user + "
          f"{len(data.get('GroupClaims', []))} group subscription claims "
          f"(dry_run={args.dry_run})")


if __name__ == "__main__":
    sys.exit(main())
