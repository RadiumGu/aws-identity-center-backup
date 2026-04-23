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
        except ClientError as e:
            # Only swallow "nothing to delete"; re-raise real errors (AccessDenied etc.).
            if e.response["Error"]["Code"] not in ("ResourceNotFoundException",):
                raise

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
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("ResourceNotFoundException",):
                raise

    # Reconcile tags (Task C3)
    current_tags_list = paginate(
        sso, "list_tags_for_resource", "Tags",
        InstanceArn=instance_arn, ResourceArn=arn,
    )
    current_tags = {t["Key"]: t["Value"] for t in current_tags_list}
    desired_tags = {t["Key"]: t["Value"] for t in (ps.get("Tags") or [])}
    to_add = [{"Key": k, "Value": v} for k, v in desired_tags.items()
              if current_tags.get(k) != v]
    to_remove = [k for k in current_tags if k not in desired_tags]
    if to_add:
        sso.tag_resource(InstanceArn=instance_arn, ResourceArn=arn, Tags=to_add)
    if to_remove:
        sso.untag_resource(InstanceArn=instance_arn, ResourceArn=arn, TagKeys=to_remove)

    # Provision to push policy changes to existing assignments (Task B1).
    # Only needed if the PS is already assigned anywhere; otherwise skip.
    assigned_accounts = paginate(
        sso, "list_accounts_for_provisioned_permission_set", "AccountIds",
        InstanceArn=instance_arn, PermissionSetArn=arn,
    )
    if assigned_accounts:
        log.info("provisioning %s across %d accounts", name, len(assigned_accounts))
        resp = sso.provision_permission_set(
            InstanceArn=instance_arn, PermissionSetArn=arn,
            TargetType="ALL_PROVISIONED_ACCOUNTS",
        )
        req_id = resp["PermissionSetProvisioningStatus"]["RequestId"]
        # Poll until SUCCEEDED/FAILED (bounded).
        for _ in range(60):  # ~5 min max at 5s intervals
            status = sso.describe_permission_set_provisioning_status(
                InstanceArn=instance_arn, ProvisionPermissionSetRequestId=req_id,
            )["PermissionSetProvisioningStatus"]
            if status["Status"] == "SUCCEEDED":
                break
            if status["Status"] == "FAILED":
                log.error("provision %s failed: %s", name, status.get("FailureReason"))
                break
            time.sleep(5)


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
