#!/usr/bin/env python3
"""
precheck_account_map.py — Validate and optionally rewrite the AWS account
IDs embedded in mist's UserAssignments.json / GroupAssignments.json before
running upstream/mist/restore.py in the TARGET account.

Why this exists
---------------
`upstream/mist/restore.py` calls `sso-admin CreateAccountAssignment` with
the `TargetId` (AWS account id) taken verbatim from the backup JSON. If
TARGET lives in the same AWS Organization as SOURCE and shares account
ids, that's fine. But in an account-level DR scenario the TARGET is
usually a separate account (often a separate Org) where the business
account ids are different. Without rewriting, every assignment silently
"succeeds" against the wrong or non-existent account id.

This script runs BEFORE mist/restore.py and either:
  1. Validates that every TargetId in the backup exists in TARGET's
     Organization (same-Org case), or
  2. Rewrites TargetId via an --account-map JSON the operator provides
     (cross-Org case), emitting a `*.mapped.json` for mist to consume.

If the operator provides neither and there are account-id mismatches,
the script aborts with a clear listing so mist/restore doesn't silently
misfire.

Usage
-----
# Same-Org (TARGET is in the same Org as SOURCE)
export AWS_PROFILE=<target-org-management>
python3 precheck_account_map.py \
    --user-assignments UserAssignments.json \
    --group-assignments GroupAssignments.json \
    --mode validate

# Cross-Org (TARGET is a different Org, map source→target account ids)
python3 precheck_account_map.py \
    --user-assignments UserAssignments.json \
    --group-assignments GroupAssignments.json \
    --mode rewrite \
    --account-map account_id_map.json \
    --output-suffix .mapped

# account_id_map.json is a flat object:
#   { "111111111111": "999999999999",
#     "222222222222": "888888888888" }
"""
import argparse
import copy
import json
import logging
import sys

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("precheck_account_map")


def extract_target_ids(assignments_json: dict) -> set[str]:
    """mist schema: { "<root-key>": { "<name>": [ {"<ps-arn>":"<acct>"} ] } }"""
    out = set()
    root = next(iter(assignments_json.values())) if assignments_json else {}
    if not isinstance(root, dict):
        return out
    for _name, items in root.items():
        for item in items or []:
            for _ps_arn, acct in (item or {}).items():
                if acct:
                    out.add(str(acct))
    return out


def list_org_account_ids() -> set[str]:
    """Enumerate all account ids reachable from the current Org. Requires
    organizations:ListAccounts permission, usually granted to the Org
    management account or a delegated admin."""
    org = boto3.client("organizations")
    out = set()
    token = None
    while True:
        kwargs = {"NextToken": token} if token else {}
        r = org.list_accounts(**kwargs)
        for a in r.get("Accounts", []):
            out.add(a["Id"])
        token = r.get("NextToken")
        if not token:
            break
    return out


def rewrite_assignments(doc: dict, mapping: dict[str, str]) -> tuple[dict, list[str]]:
    doc = copy.deepcopy(doc)
    unmapped = []
    root = next(iter(doc.values())) if doc else {}
    for _name, items in (root or {}).items():
        for item in items or []:
            for ps_arn, acct in list((item or {}).items()):
                acct_s = str(acct)
                if acct_s in mapping:
                    item[ps_arn] = mapping[acct_s]
                else:
                    unmapped.append(acct_s)
    return doc, sorted(set(unmapped))


def load(path: str) -> dict | None:
    if not path:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("%s not found, skipping", path)
        return None


def save(path: str, doc: dict) -> None:
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--user-assignments", default="UserAssignments.json")
    p.add_argument("--group-assignments", default="GroupAssignments.json")
    p.add_argument("--mode", choices=["validate", "rewrite"], default="validate")
    p.add_argument("--account-map", help="JSON {source_id: target_id} — required for --mode rewrite")
    p.add_argument("--output-suffix", default=".mapped",
                   help="Suffix added before .json when writing rewritten files")
    p.add_argument("--logging", default="INFO")
    args = p.parse_args()

    logging.basicConfig(level=args.logging, format="%(asctime)s %(levelname)s %(message)s")

    user_doc = load(args.user_assignments)
    group_doc = load(args.group_assignments)
    if not user_doc and not group_doc:
        log.error("Neither assignments file could be loaded; nothing to check")
        return 2

    source_ids = set()
    for doc in (user_doc, group_doc):
        if doc:
            source_ids |= extract_target_ids(doc)
    log.info("found %d distinct AWS account ids in backup: %s",
             len(source_ids), sorted(source_ids))

    if args.mode == "validate":
        try:
            org_ids = list_org_account_ids()
        except ClientError as e:
            log.error("organizations:ListAccounts failed: %s. "
                      "If TARGET is not an Org management/delegated-admin "
                      "account, rerun with --mode rewrite --account-map ...", e)
            return 3
        missing = sorted(source_ids - org_ids)
        if missing:
            log.error("%d account id(s) present in backup but NOT in the "
                      "target Organization (mist/restore.py would silently "
                      "misfire on these). Missing: %s",
                      len(missing), missing)
            log.error("Either (a) share the SAME Org with source, "
                      "or (b) re-run this script with --mode rewrite "
                      "--account-map <file> to remap account ids.")
            return 1
        log.info("all %d account ids are present in the target Organization",
                 len(source_ids))
        return 0

    # mode == rewrite
    if not args.account_map:
        log.error("--mode rewrite requires --account-map <file>")
        return 2
    mapping = load(args.account_map) or {}
    if not isinstance(mapping, dict) or not mapping:
        log.error("--account-map must be a non-empty {source_id: target_id} JSON")
        return 2
    mapping = {str(k): str(v) for k, v in mapping.items()}

    any_unmapped = False
    for path, doc in (
        (args.user_assignments, user_doc),
        (args.group_assignments, group_doc),
    ):
        if not doc:
            continue
        new_doc, unmapped = rewrite_assignments(doc, mapping)
        if unmapped:
            any_unmapped = True
            log.error("%s: %d source account id(s) have no mapping: %s",
                      path, len(unmapped), unmapped)
        out_path = path.replace(".json", args.output_suffix + ".json")
        save(out_path, new_doc)
        log.info("wrote %s", out_path)

    if any_unmapped:
        log.error("Some source account ids were not in --account-map. "
                  "Fill them in and re-run.")
        return 1
    log.info("rewrite complete; feed the *.mapped.json files to mist/restore.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
