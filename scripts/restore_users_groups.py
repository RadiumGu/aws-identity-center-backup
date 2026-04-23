#!/usr/bin/env python3
"""
restore_users_groups.py — Recreate Identity Center users, groups, memberships
on the TARGET account's Identity Center instance.

Idempotent: looks up existing users (by UserName) and groups (by DisplayName)
first; only creates what's missing.

Usage:
  export AWS_PROFILE=target-account
  export AWS_DEFAULT_REGION=<region>
  python3 restore_users_groups.py --idc-id d-NEWxxxxxxxx [--dry-run]

IAM permissions:
  identitystore:ListUsers, ListGroups, ListGroupMemberships
  identitystore:CreateUser, CreateGroup, CreateGroupMembership

Notes:
  - Passwords are NOT migrated (Identity Center built-in directory has no
    password export). Users in the new instance receive an email invitation
    to set their own password on first login.
  - Works best when identity source = "Identity Center directory".
  - If target uses an external IdP, let SCIM sync instead; don't run this.
"""
import argparse
import json
import logging
import sys

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("restore_users_groups")


def paginate(client, method_name, result_key, **kwargs):
    m = getattr(client, method_name)
    r = m(**kwargs)
    out = r.get(result_key, [])
    while "NextToken" in r:
        r = m(NextToken=r["NextToken"], **kwargs)
        out.extend(r.get(result_key, []))
    return out


def current_users(ids, store_id):
    return {u["UserName"]: u["UserId"] for u in paginate(ids, "list_users", "Users", IdentityStoreId=store_id)}


def current_groups(ids, store_id):
    return {g["DisplayName"]: g["GroupId"] for g in paginate(ids, "list_groups", "Groups", IdentityStoreId=store_id)}


def build_create_user_payload(u, store_id):
    """Strip source-only ids and map to CreateUser params."""
    payload = {"IdentityStoreId": store_id, "UserName": u["UserName"]}
    for k in ("Name", "DisplayName", "NickName", "ProfileUrl", "Title",
              "PreferredLanguage", "Locale", "Timezone", "UserType"):
        if u.get(k):
            payload[k] = u[k]
    # Emails / PhoneNumbers / Addresses are list-of-dict; CreateUser accepts them.
    for k in ("Emails", "PhoneNumbers", "Addresses"):
        if u.get(k):
            # strip any server-side metadata if present
            payload[k] = u[k]
    return payload


def restore_users(ids, store_id, users_backup, existing, dry_run):
    created = 0
    for u in users_backup:
        name = u["UserName"]
        if name in existing:
            log.debug("user exists: %s", name)
            continue
        payload = build_create_user_payload(u, store_id)
        if dry_run:
            log.info("[dry-run] would create user %s", name)
            continue
        try:
            resp = ids.create_user(**payload)
            existing[name] = resp["UserId"]
            created += 1
            log.info("created user %s", name)
        except ClientError as e:
            log.error("create_user %s failed: %s", name, e)
    log.info("users created: %d", created)


def restore_groups(ids, store_id, groups_backup, existing, dry_run):
    created = 0
    for g in groups_backup:
        dn = g["DisplayName"]
        if dn in existing:
            continue
        if dry_run:
            log.info("[dry-run] would create group %s", dn)
            continue
        try:
            resp = ids.create_group(
                IdentityStoreId=store_id,
                DisplayName=dn,
                Description=g.get("Description", "") or "",
            )
            existing[dn] = resp["GroupId"]
            created += 1
            log.info("created group %s", dn)
        except ClientError as e:
            log.error("create_group %s failed: %s", dn, e)
    log.info("groups created: %d", created)


def restore_memberships(ids, store_id, memberships, users_backup_by_id, users_map, groups_map, dry_run):
    # memberships reference source user-ids; translate via the backup user list.
    src_uid_to_username = {u["UserId"]: u["UserName"] for u in users_backup_by_id}
    created = 0
    for m in memberships:
        username = src_uid_to_username.get(m["MemberId"].get("UserId") if isinstance(m["MemberId"], dict) else m["MemberId"])
        if not username:
            log.warning("skip membership, unknown source user id: %s", m["MemberId"])
            continue
        target_uid = users_map.get(username)
        target_gid = groups_map.get(m["GroupDisplayName"])
        if not target_uid or not target_gid:
            log.warning("skip membership %s -> %s: not found in target", username, m["GroupDisplayName"])
            continue
        if dry_run:
            log.info("[dry-run] would add %s -> %s", username, m["GroupDisplayName"])
            continue
        try:
            ids.create_group_membership(
                IdentityStoreId=store_id,
                GroupId=target_gid,
                MemberId={"UserId": target_uid},
            )
            created += 1
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConflictException":
                continue
            log.error("create_group_membership %s->%s failed: %s", username, m["GroupDisplayName"], e)
    log.info("memberships created: %d", created)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--idc-id", required=True, help="TARGET Identity Store ID")
    p.add_argument("--users-file", default="Users.json")
    p.add_argument("--groups-file", default="Groups.json")
    p.add_argument("--memberships-file", default="GroupMemberships.json")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--logging", default="INFO")
    args = p.parse_args()

    logging.basicConfig(level=args.logging, format="%(asctime)s %(levelname)s %(message)s")

    with open(args.users_file) as f:
        users_backup = json.load(f)["Users"]
    with open(args.groups_file) as f:
        groups_backup = json.load(f)["Groups"]
    with open(args.memberships_file) as f:
        memberships = json.load(f)["GroupMemberships"]

    ids = boto3.client("identitystore")

    users_map = current_users(ids, args.idc_id)
    groups_map = current_groups(ids, args.idc_id)

    restore_users(ids, args.idc_id, users_backup, users_map, args.dry_run)
    restore_groups(ids, args.idc_id, groups_backup, groups_map, args.dry_run)
    # Refresh maps after creation.
    if not args.dry_run:
        users_map = current_users(ids, args.idc_id)
        groups_map = current_groups(ids, args.idc_id)

    restore_memberships(ids, args.idc_id, memberships, users_backup, users_map, groups_map, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
