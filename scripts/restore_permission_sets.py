#!/usr/bin/env python3
"""
restore_permission_sets.py — Recreate permission sets on TARGET Identity Center.

Idempotent: if a permission set with the same Name exists, it is updated;
otherwise created.

Usage:
  export AWS_PROFILE=target-account
  export AWS_DEFAULT_REGION=<region>
  python3 restore_permission_sets.py --idc-arn arn:aws:sso:::instance/ssoins-NEW [--dry-run]
"""
import argparse
import json
import logging
import sys
import time

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("restore_permission_sets")


def paginate(client, method_name, result_key, **kwargs):
    m = getattr(client, method_name)
    r = m(**kwargs)
    out = r.get(result_key, [])
    while "NextToken" in r:
        r = m(NextToken=r["NextToken"], **kwargs)
        out.extend(r.get(result_key, []))
    return out


def existing_permission_sets(sso, instance_arn):
    arns = paginate(sso, "list_permission_sets", "PermissionSets", InstanceArn=instance_arn)
    out = {}
    for a in arns:
        ps = sso.describe_permission_set(InstanceArn=instance_arn, PermissionSetArn=a)["PermissionSet"]
        out[ps["Name"]] = a
    return out


def apply_permission_set(sso, instance_arn, ps, existing, dry_run):
    name = ps["Name"]
    if name in existing:
        arn = existing[name]
        log.info("permission set exists, updating: %s", name)
        if not dry_run:
            sso.update_permission_set(
                InstanceArn=instance_arn, PermissionSetArn=arn,
                Description=ps.get("Description") or name,
                SessionDuration=ps.get("SessionDuration", "PT1H"),
                RelayState=ps.get("RelayState", "") or "",
            )
    else:
        log.info("creating permission set: %s", name)
        if dry_run:
            return
        resp = sso.create_permission_set(
            InstanceArn=instance_arn, Name=name,
            Description=ps.get("Description") or name,
            SessionDuration=ps.get("SessionDuration", "PT1H"),
            RelayState=ps.get("RelayState", "") or "",
            Tags=ps.get("Tags", []) or [],
        )
        arn = resp["PermissionSet"]["PermissionSetArn"]
        existing[name] = arn

    if dry_run:
        return

    # Reconcile managed policies
    current_managed = {m["Arn"] for m in paginate(
        sso, "list_managed_policies_in_permission_set", "AttachedManagedPolicies",
        InstanceArn=instance_arn, PermissionSetArn=arn)}
    desired_managed = set(ps.get("ManagedPolicies", []))
    for a in desired_managed - current_managed:
        sso.attach_managed_policy_to_permission_set(
            InstanceArn=instance_arn, PermissionSetArn=arn, ManagedPolicyArn=a)
    for a in current_managed - desired_managed:
        sso.detach_managed_policy_from_permission_set(
            InstanceArn=instance_arn, PermissionSetArn=arn, ManagedPolicyArn=a)

    # Reconcile customer-managed policies
    current_cust = {(c["Name"], c.get("Path", "/")) for c in paginate(
        sso, "list_customer_managed_policy_references_in_permission_set",
        "CustomerManagedPolicyReferences",
        InstanceArn=instance_arn, PermissionSetArn=arn)}
    desired_cust = {(c["Name"], c.get("Path", "/")) for c in ps.get("CustomerManagedPolicies", [])}
    for n, p in desired_cust - current_cust:
        try:
            sso.attach_customer_managed_policy_reference_to_permission_set(
                InstanceArn=instance_arn, PermissionSetArn=arn,
                CustomerManagedPolicyReference={"Name": n, "Path": p})
        except ClientError as e:
            log.warning("attach customer-managed policy %s failed: %s (policy must exist in member accounts)", n, e)
    for n, p in current_cust - desired_cust:
        sso.detach_customer_managed_policy_reference_from_permission_set(
            InstanceArn=instance_arn, PermissionSetArn=arn,
            CustomerManagedPolicyReference={"Name": n, "Path": p})

    # Inline policy
    inline = ps.get("InlinePolicy") or ""
    if inline:
        sso.put_inline_policy_to_permission_set(
            InstanceArn=instance_arn, PermissionSetArn=arn, InlinePolicy=inline)
    else:
        try:
            sso.delete_inline_policy_from_permission_set(
                InstanceArn=instance_arn, PermissionSetArn=arn)
        except ClientError:
            pass

    # Permissions boundary
    boundary = ps.get("PermissionsBoundary")
    if boundary:
        sso.put_permissions_boundary_to_permission_set(
            InstanceArn=instance_arn, PermissionSetArn=arn,
            PermissionsBoundary=boundary)
    else:
        try:
            sso.delete_permissions_boundary_from_permission_set(
                InstanceArn=instance_arn, PermissionSetArn=arn)
        except ClientError:
            pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--idc-arn", required=True, help="TARGET Identity Center instance ARN")
    p.add_argument("--input", default="PermissionSets.json")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--logging", default="INFO")
    args = p.parse_args()
    logging.basicConfig(level=args.logging, format="%(asctime)s %(levelname)s %(message)s")

    with open(args.input) as f:
        perm_sets = json.load(f)["PermissionSets"]

    sso = boto3.client("sso-admin")
    existing = existing_permission_sets(sso, args.idc_arn)

    for ps in perm_sets:
        try:
            apply_permission_set(sso, args.idc_arn, ps, existing, args.dry_run)
            time.sleep(0.2)  # gentle pacing vs throttling
        except Exception as e:  # noqa: BLE001
            log.error("permission set %s failed: %s", ps["Name"], e)

    print(f"Processed {len(perm_sets)} permission sets (dry_run={args.dry_run})")


if __name__ == "__main__":
    sys.exit(main())
