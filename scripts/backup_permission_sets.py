#!/usr/bin/env python3
"""
backup_permission_sets.py — Export all permission sets from SOURCE Identity Center.

Outputs PermissionSets.json:
[
  {
    "Name": "...",
    "Description": "...",
    "SessionDuration": "PT1H",
    "RelayState": "...",
    "ManagedPolicies": ["arn:aws:iam::aws:policy/..."],
    "CustomerManagedPolicies": [{"Name": "...", "Path": "/"}],
    "InlinePolicy": "{...}",
    "PermissionsBoundary": {...} | null,
    "Tags": [{"Key":"...","Value":"..."}]
  }, ...
]

Usage:
  export AWS_PROFILE=source-account
  export AWS_DEFAULT_REGION=<region>
  python3 backup_permission_sets.py --idc-arn arn:aws:sso:::instance/ssoins-xxx
"""
import argparse
import json
import logging
import sys

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("backup_permission_sets")


def paginate(client, method_name, result_key, **kwargs):
    m = getattr(client, method_name)
    r = m(**kwargs)
    out = r.get(result_key, [])
    while "NextToken" in r:
        r = m(NextToken=r["NextToken"], **kwargs)
        out.extend(r.get(result_key, []))
    return out


def dump_permission_set(sso, instance_arn, ps_arn):
    ps = sso.describe_permission_set(InstanceArn=instance_arn, PermissionSetArn=ps_arn)["PermissionSet"]

    managed = paginate(
        sso, "list_managed_policies_in_permission_set", "AttachedManagedPolicies",
        InstanceArn=instance_arn, PermissionSetArn=ps_arn,
    )
    cust = paginate(
        sso, "list_customer_managed_policy_references_in_permission_set",
        "CustomerManagedPolicyReferences",
        InstanceArn=instance_arn, PermissionSetArn=ps_arn,
    )
    # Narrow exception handling (Task C1): only swallow "not configured",
    # let AccessDenied / throttling etc. surface.
    _swallow = ("ResourceNotFoundException",)
    try:
        inline = sso.get_inline_policy_for_permission_set(
            InstanceArn=instance_arn, PermissionSetArn=ps_arn,
        ).get("InlinePolicy", "")
    except ClientError as e:
        if e.response["Error"]["Code"] not in _swallow:
            raise
        inline = ""
    try:
        boundary = sso.get_permissions_boundary_for_permission_set(
            InstanceArn=instance_arn, PermissionSetArn=ps_arn,
        ).get("PermissionsBoundary")
    except ClientError as e:
        if e.response["Error"]["Code"] not in _swallow:
            raise
        boundary = None
    tags = paginate(sso, "list_tags_for_resource", "Tags",
                    InstanceArn=instance_arn, ResourceArn=ps_arn)

    return {
        "Name": ps["Name"],
        "Description": ps.get("Description", ""),
        "SessionDuration": ps.get("SessionDuration", "PT1H"),
        "RelayState": ps.get("RelayState", ""),
        "ManagedPolicies": [m["Arn"] for m in managed],
        "CustomerManagedPolicies": cust,
        "InlinePolicy": inline,
        "PermissionsBoundary": boundary,
        "Tags": tags,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--idc-arn", required=True, help="Identity Center instance ARN")
    p.add_argument("--output", default="PermissionSets.json")
    p.add_argument("--logging", default="INFO")
    args = p.parse_args()
    logging.basicConfig(level=args.logging, format="%(asctime)s %(levelname)s %(message)s")

    sso = boto3.client("sso-admin")

    arns = paginate(sso, "list_permission_sets", "PermissionSets", InstanceArn=args.idc_arn)
    log.info("Found %d permission sets", len(arns))

    out = [dump_permission_set(sso, args.idc_arn, a) for a in arns]
    with open(args.output, "w") as f:
        json.dump({"PermissionSets": out}, f, indent=2, default=str)
    print(f"Wrote {args.output} ({len(out)} permission sets)")


if __name__ == "__main__":
    sys.exit(main())
