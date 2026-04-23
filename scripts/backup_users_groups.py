#!/usr/bin/env python3
"""
backup_users_groups.py — Export Identity Center directory objects from SOURCE account.

Outputs three JSON files (by default):
  - Users.json            (identitystore users: UserName, Name, Emails, DisplayName, etc.)
  - Groups.json           (identitystore groups: DisplayName, Description)
  - GroupMemberships.json (UserName <-> GroupDisplayName pairs)

Usage:
  export AWS_PROFILE=source-account
  export AWS_DEFAULT_REGION=<region>
  python3 backup_users_groups.py --idc-id d-1234567890

IAM permissions required:
  identitystore:ListUsers
  identitystore:DescribeUser
  identitystore:ListGroups
  identitystore:ListGroupMemberships

NOTE: If your identity source is an EXTERNAL IdP (Okta / Entra / Google),
      you normally do NOT need this script — just connect the SAME IdP to
      the new Identity Center instance and let SCIM re-provision.
      This script is primarily for the "Identity Center built-in directory" case.
"""
import argparse
import json
import logging
import sys

import boto3

log = logging.getLogger("backup_users_groups")


def paginate(client, method_name, result_key, **kwargs):
    paginator_method = getattr(client, method_name)
    response = paginator_method(**kwargs)
    results = response.get(result_key, [])
    while "NextToken" in response:
        response = paginator_method(NextToken=response["NextToken"], **kwargs)
        results.extend(response.get(result_key, []))
    return results


def export_users(ids_client, store_id):
    users = paginate(ids_client, "list_users", "Users", IdentityStoreId=store_id)
    # ListUsers returns most fields already; describe to ensure full attribute fidelity.
    full = []
    for u in users:
        try:
            d = ids_client.describe_user(IdentityStoreId=store_id, UserId=u["UserId"])
            d.pop("ResponseMetadata", None)
            full.append(d)
        except Exception as exc:  # noqa: BLE001
            log.warning("describe_user failed for %s: %s", u.get("UserName"), exc)
            full.append(u)
    # Task C8: detect UserName case collisions that would break downstream
    # lookups (e.g. mist/restore.py lower-cases UserName as lookup key).
    seen = {}
    collisions = []
    for u in full:
        name = u.get("UserName", "")
        key = name.lower()
        if key in seen and seen[key] != name:
            collisions.append((seen[key], name))
        else:
            seen[key] = name
    if collisions:
        log.error("UserName case collisions detected (will break restore lookups): %s", collisions)
        raise SystemExit(
            "Abort: source has UserName values that collide when lower-cased. "
            "Clean up source before backup. Collisions: " + str(collisions)
        )
    log.info("Exported %d users", len(full))
    return full


def export_groups(ids_client, store_id):
    groups = paginate(ids_client, "list_groups", "Groups", IdentityStoreId=store_id)
    log.info("Exported %d groups", len(groups))
    return groups


def export_memberships(ids_client, store_id, groups):
    memberships = []
    for g in groups:
        members = paginate(
            ids_client,
            "list_group_memberships",
            "GroupMemberships",
            IdentityStoreId=store_id,
            GroupId=g["GroupId"],
        )
        for m in members:
            memberships.append(
                {
                    "GroupId": g["GroupId"],
                    "GroupDisplayName": g["DisplayName"],
                    "MemberId": m["MemberId"],
                }
            )
    log.info("Exported %d memberships", len(memberships))
    return memberships


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--idc-id", required=True, help="Identity Store ID (d-xxxxxxxxxx)")
    p.add_argument("--users-file", default="Users.json")
    p.add_argument("--groups-file", default="Groups.json")
    p.add_argument("--memberships-file", default="GroupMemberships.json")
    p.add_argument("--logging", default="INFO")
    args = p.parse_args()

    logging.basicConfig(level=args.logging, format="%(asctime)s %(levelname)s %(message)s")

    ids = boto3.client("identitystore")

    users = export_users(ids, args.idc_id)
    groups = export_groups(ids, args.idc_id)
    memberships = export_memberships(ids, args.idc_id, groups)

    with open(args.users_file, "w") as f:
        json.dump({"Users": users}, f, indent=2, default=str)
    with open(args.groups_file, "w") as f:
        json.dump({"Groups": groups}, f, indent=2, default=str)
    with open(args.memberships_file, "w") as f:
        json.dump({"GroupMemberships": memberships}, f, indent=2)

    print(
        f"Wrote {args.users_file} ({len(users)} users), "
        f"{args.groups_file} ({len(groups)} groups), "
        f"{args.memberships_file} ({len(memberships)} memberships)"
    )


if __name__ == "__main__":
    sys.exit(main())
