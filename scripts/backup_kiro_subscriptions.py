#!/usr/bin/env python3
"""
backup_kiro_subscriptions.py — Export Kiro enterprise subscription claims from SOURCE.

Kiro subscriptions are stored as *claims* in the `user-subscriptions` service,
keyed by Identity Center UserId / GroupId. Because UserId/GroupId change when
Identity Center is rebuilt in a target account, we persist claims keyed by
UserName / GroupDisplayName for cross-account restore.

Outputs:
  KiroSubscriptions.json
    {
      "SourceApplicationArn": "...",
      "OverageConfig": {...} | null,
      "UserClaims":  [ {"UserName": "...", "ClaimType": "...", "ClaimData": {...}} ],
      "GroupClaims": [ {"GroupDisplayName": "...", "ClaimType": "...", "ClaimData": {...}} ]
    }

Usage:
  export AWS_PROFILE=source-account
  export AWS_DEFAULT_REGION=<region>
  python3 backup_kiro_subscriptions.py --idc-arn arn:aws:sso:::instance/ssoins-xxx

IAM permissions required:
  sso:ListApplications, DescribeApplication
  user-subscriptions:ListClaims, ListApplicationClaims, ListUserSubscriptions
  identitystore:DescribeUser, DescribeGroup, ListUsers, ListGroups

NOTE: This script exports the *subscription binding*, not Kiro-side user data
      (conversation history, CodeWhisperer profile, Q Developer tagging). Those
      live in Kiro/CodeWhisperer/Q backends and are NOT portable across
      accounts. Confirm with the Kiro product team what, if anything, can
      be migrated at cutover time.
"""
import argparse
import json
import logging
import sys

import boto3
from botocore.exceptions import ClientError

from _user_subscriptions import UserSubscriptionsClient, paginate_private

log = logging.getLogger("backup_kiro_subscriptions")


def paginate(client, method_name, result_key, **kwargs):
    paginator = client.get_paginator(method_name)
    for page in paginator.paginate(**kwargs):
        yield from page.get(result_key, [])


def find_kiro_application(sso, instance_arn):
    """Kiro subscribes create an IdC application; identify it by provider ARN.
    Product id for Kiro/Q Developer currently surfaces as 'q' in
    ApplicationProviderArn. If Kiro rebrands, adjust the filter here.
    """
    for app in paginate(sso, "list_applications", "Applications", InstanceArn=instance_arn):
        provider = app.get("ApplicationProviderArn", "")
        if "q.amazonaws.com" in provider or "kiro" in provider.lower():
            log.info("found Kiro application: %s (%s)", app["ApplicationArn"], provider)
            return app["ApplicationArn"]
    return None


def export_claims(us_client, app_arn, idc_id, ids_client):
    """List claims for the Kiro application and rewrite UserId/GroupId to
    UserName/GroupDisplayName so the data is portable across IdC instances.
    """
    user_claims = []
    group_claims = []

    try:
        claims = list(paginate_private(us_client.list_claims, "Claims",
                                       ApplicationArn=app_arn))
    except ClientError as e:
        # Fallback: some regions expose ListApplicationClaims instead.
        if e.response["Error"]["Code"] in ("InvalidParameterException",
                                           "OperationNotSupportedException",
                                           "UnknownOperation",
                                           "HttpError"):
            log.warning("list_claims failed (%s), trying list_application_claims",
                        e.response["Error"]["Code"])
            claims = list(paginate_private(us_client.list_application_claims, "Claims",
                                           ApplicationArn=app_arn))
        else:
            raise

    for c in claims:
        principal = c.get("ClaimPrincipal") or c
        user_id = principal.get("UserId") or c.get("UserId")
        group_id = principal.get("GroupId") or c.get("GroupId")
        entry = {
            "ClaimType": c.get("ClaimType", "UserSubscription"),
            "ClaimData": {k: v for k, v in c.items()
                          if k not in ("UserId", "GroupId", "ClaimPrincipal", "CreatedAt", "ClaimId")},
        }
        try:
            if user_id:
                u = ids_client.describe_user(IdentityStoreId=idc_id, UserId=user_id)
                entry["UserName"] = u["UserName"]
                user_claims.append(entry)
            elif group_id:
                g = ids_client.describe_group(IdentityStoreId=idc_id, GroupId=group_id)
                entry["GroupDisplayName"] = g["DisplayName"]
                group_claims.append(entry)
        except ClientError as e:
            log.warning("describe failed for claim %s: %s", c.get("ClaimId"), e)

    log.info("exported %d user claims, %d group claims", len(user_claims), len(group_claims))
    return user_claims, group_claims


def export_overage_config(us_client, app_arn):
    try:
        resp = us_client.describe_overage_config(ApplicationArn=app_arn)
        cfg = resp.get("OverageConfig")
        log.info("overage config: %s", cfg)
        return cfg
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ResourceNotFoundException", "HttpError"):
            return None
        raise
    except AttributeError:
        # API name variant; boto3 version may not have it
        log.warning("describe_overage_config not available in this boto3 version")
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--idc-arn", required=True, help="Identity Center instance ARN")
    p.add_argument("--idc-id", required=True, help="Identity Store ID (d-xxxxxxxxxx)")
    p.add_argument("--output", default="KiroSubscriptions.json")
    p.add_argument("--logging", default="INFO")
    args = p.parse_args()
    logging.basicConfig(level=args.logging, format="%(asctime)s %(levelname)s %(message)s")

    sso = boto3.client("sso-admin")
    us = UserSubscriptionsClient()
    ids = boto3.client("identitystore")

    app_arn = find_kiro_application(sso, args.idc_arn)
    if not app_arn:
        log.error("No Kiro application found in this Identity Center instance. "
                  "Is Kiro enterprise subscribed here?")
        sys.exit(2)

    user_claims, group_claims = export_claims(us, app_arn, args.idc_id, ids)
    overage = export_overage_config(us, app_arn)

    out = {
        "SourceApplicationArn": app_arn,
        "OverageConfig": overage,
        "UserClaims": user_claims,
        "GroupClaims": group_claims,
    }
    with open(args.output, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"Wrote {args.output}: {len(user_claims)} user claims, "
          f"{len(group_claims)} group claims, overage={'set' if overage else 'none'}")


if __name__ == "__main__":
    sys.exit(main())
