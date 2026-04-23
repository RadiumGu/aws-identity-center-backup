---
name: identity-center-backup
description: Back up and restore AWS IAM Identity Center across accounts for account-level disaster recovery. Use when the user wants to clone Identity Center (users, groups, group memberships, permission sets, account/application assignments) from a source AWS account to a standby/DR account, migrate Identity Center between accounts or Organizations, or set up periodic backups of Identity Center state. Triggers on phrases like "Identity Center backup", "SSO backup", "IAM Identity Center DR", "clone Identity Center", "migrate SSO users and groups to a new account", or "account-level Identity Center failover".
---

# Identity Center Backup & DR

End-to-end toolkit + runbook to back up an AWS IAM Identity Center instance and
restore it into another account for account-level disaster recovery. Combines
purpose-built scripts (users/groups/memberships/permission-sets) with the
official AWS sample for assignments.

## When to Use

Use this skill when the user asks to:
- Back up Identity Center (SSO) users, groups, permission sets, assignments
- Clone/migrate Identity Center from one AWS account to another
- Set up account-level DR for Identity Center (standby account takes over)
- Change the Identity Center start URL while preserving users and permissions

Do NOT use for: cross-region failover within the same account (use
`aws-iam-identity-center-extensions` Region-Switch instead), or in-account IAM
user migration.

## Critical First Question: Identity Source

Before anything else, ask the user which identity source the source Identity
Center uses (Console → IAM Identity Center → Settings → Identity source):

| Source | Path |
|--------|------|
| External IdP (Okta / Entra / Google) | Connect the SAME IdP to the target Identity Center; SCIM re-provisions users/groups. **Skip** `backup_users_groups.py`; still run permission-sets and assignments scripts. |
| Active Directory | Connect target to the same AD. Skip users/groups backup. |
| Identity Center built-in directory | Run the full pipeline below. |

This decision changes the whole plan — do not proceed until it is answered.

## Workflow

The pipeline has three independent data slices; each has a backup + restore
script. Run backup on SOURCE, copy JSON files, run restore on TARGET.

```
SOURCE ──backup──►  Users.json, Groups.json, GroupMemberships.json
                    PermissionSets.json
                    UserAssignments.json, GroupAssignments.json, AppAssignments.json
                             │
                             ▼
TARGET ──restore──►  identitystore CreateUser/Group/Membership
                    sso-admin CreatePermissionSet + reconcile policies
                    sso-admin CreateAccountAssignment / CreateApplicationAssignment
```

### Inputs you must gather

- `SOURCE_IDC_ARN`, `SOURCE_IDC_ID`, `SOURCE_REGION`, source AWS profile
- `TARGET_IDC_ARN`, `TARGET_IDC_ID`, `TARGET_REGION`, target AWS profile
- Customized target start URL (Settings → Access portal URL)
- Confirmation that customer-managed IAM policies referenced by permission sets
  exist under the same names in the TARGET Organization's member accounts

### Backup (run against SOURCE)

```bash
export AWS_PROFILE=<source> AWS_DEFAULT_REGION=<region>
mkdir -p backups/$(date +%F) && cd backups/$(date +%F)
python3 ../../scripts/backup_users_groups.py    --idc-id $SRC_IDC_ID
python3 ../../scripts/backup_permission_sets.py --idc-arn $SRC_IDC_ARN
python3 ../../upstream/mist/backup.py           --idc-id $SRC_IDC_ID --idc-arn $SRC_IDC_ARN
# Kiro / Q Developer subscription snapshot (private API, audit/checklist only)
python3 ../../scripts/backup_kiro_subscriptions.py --idc-arn $SRC_IDC_ARN --region <region>
python3 ../../scripts/kiro_restore_checklist.py  --input KiroSubscriptions.json \
        > kiro-restore-checklist.md
```

### Restore (run against TARGET, always `--dry-run` first)

```bash
export AWS_PROFILE=<target> AWS_DEFAULT_REGION=<region>
python3 ../../scripts/restore_users_groups.py    --idc-id $DST_IDC_ID --dry-run
python3 ../../scripts/restore_permission_sets.py --idc-arn $DST_IDC_ARN --dry-run
# review logs, then drop --dry-run
python3 ../../scripts/restore_users_groups.py    --idc-id $DST_IDC_ID
python3 ../../scripts/restore_permission_sets.py --idc-arn $DST_IDC_ARN
python3 ../../upstream/mist/restore.py           --idc-id $DST_IDC_ID --idc-arn $DST_IDC_ARN

# Kiro subscription restore is MANUAL — follow kiro-restore-checklist.md:
#   1. Amazon Q Developer / Kiro console → Subscriptions → purchase seats
#      per the plan/seat table (Step 1).
#   2. Kiro → Users & Groups → Add user / Add group per Step 2.
# Then snapshot the target for verification:
python3 ../../scripts/backup_kiro_subscriptions.py --idc-arn $DST_IDC_ARN --region <region>
```

### Validation

After restore, re-run the backup scripts against TARGET and diff record counts
against SOURCE. Pick 3 users + 1 admin and test login via the new start URL.
Re-bind Kiro (or any downstream SCIM consumer) to the target Identity Center.

## Script Semantics (important)

- All `restore_*` scripts are **idempotent** and use `UserName` / group
  `DisplayName` / permission-set `Name` as cross-instance join keys.
  **Never rename these attributes** between backup and restore, or the link
  breaks silently.
- `restore_permission_sets.py` is **reconcile**, not append — managed,
  customer-managed, inline, boundary, and tags are all synced to match the
  backup exactly.
- Customer-managed policies are referenced by name only. If a member account
  in the target Org lacks that IAM policy, the corresponding account
  assignment will fail at provision time.
- Assignments reference accounts by AWS account ID. If TARGET lives in a
  different Organization with different account IDs, you must map them
  (currently a manual step — see RUNBOOK §9 TODO).

## Known Limitations (surface these to the user)

- Passwords and MFA devices cannot be exported — users will receive an
  invitation email and must re-enroll MFA in the new instance.
- **Kiro / Q Developer subscription assignment has no public API.** The
  source-side snapshot uses the private `user-subscriptions` ("Zorn") API
  via SigV4; the target-side restore is a MANUAL console flow (purchase
  seats in Amazon Q Developer / Kiro console, then Add user / Add group).
  The `kiro_restore_checklist.py` script produces the operator checklist.
- Application assignments for third-party apps (incl. Kiro itself) may cache
  UserId/GroupId; downstream apps typically need re-binding, not just
  restore. Coordinate with the app vendor.
- 1000+ users stay well under service quotas but near `identitystore`
  CreateUser TPS limits; boto3 retries handle it, but expect ~2–5 minutes
  for the users step.
- `upstream/mist/restore.py` requires users/groups to exist in TARGET first,
  so ordering is users → permission-sets → assignments.

## References

- `docs/RUNBOOK.md` — full end-to-end runbook (9 sections: decision tree,
  architecture, prerequisites, backup, restore, validation, cutover,
  rollback, limitations, TODO). Read this before executing in production.
- `upstream/mist/` — vendored aws-samples
  `manage-identity-source-transition-for-aws-iam-identity-center` (MIT-0).
- `upstream/ic-extensions/` — vendored aws-samples
  `aws-iam-identity-center-extensions` CDK solution (MIT-0); consult for a
  heavier, fully IaC-managed Region-Switch style implementation.

## Checklist Before Declaring Done

- [ ] Identity source confirmed; correct pipeline branch chosen
- [ ] Both `--dry-run` runs reviewed and clean
- [ ] Backup JSONs archived to versioned S3 (if running as a scheduled job)
- [ ] Target Identity Center start URL customized and documented
- [ ] Customer-managed IAM policies pre-deployed across target Org member
      accounts
- [ ] Post-restore counts match source; sampled logins succeed
- [ ] Downstream apps (Kiro, etc.) re-bound to target Identity Center
- [ ] Rollback procedure (RUNBOOK §7.3) communicated to the operator
