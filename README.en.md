[简体中文](README.md) | **English**

# identity-center-backup

Account-level backup & disaster recovery toolkit for AWS IAM Identity Center, plus an end-to-end runbook.

> The repo doubles as an **Agent Skill** — Kiro / Claude Code / OpenClaw can load `SKILL.md` directly and drive the full workflow.
> Install: `git clone https://github.com/RadiumGu/aws-identity-center-backup.git ~/.kiro/skills/identity-center-backup`
> (Claude Code: `~/.claude/skills/`; OpenClaw: `~/.openclaw/skills/` or project-local `skills/`.)

## Scenario

A customer has 1000+ Kiro subscription users signing in through Identity Center in their primary AWS account. They want a standby account that mirrors the whole Identity Center setup — same users / groups / permission sets / assignments, new start URL — so that if the primary account has a catastrophic failure they can cut over with minimal disruption.

## Contents

| Path | Purpose |
|------|---------|
| `docs/RUNBOOK.md` | *End-to-end runbook* (decision tree → backup → target bootstrap → restore → validation → cutover) |
| `scripts/backup_users_groups.py` | Export users / groups / memberships (required for Identity Center built-in directory) |
| `scripts/restore_users_groups.py` | Rebuild users / groups / memberships in the target account |
| `scripts/backup_permission_sets.py` | Export full permission set definitions (managed + customer + inline + boundary + tags) |
| `scripts/restore_permission_sets.py` | Rebuild permission sets in the target account (reconcile semantics) |
| `upstream/mist/` | AWS official sample — account + application *assignments* backup / restore (vendored, MIT-0) |
| `upstream/ic-extensions/` | AWS official CDK solution (Region-Switch, reference only, vendored, MIT-0) |

## Responsibility Split

The official `manage-identity-source-transition` sample (`mist`) only covers *assignments* — not users, groups, memberships, or permission set definitions. This project adds the missing pieces and composes everything into a single pipeline:

```
SOURCE account                    TARGET account
┌──────────────────────┐          ┌──────────────────────┐
│ Identity Center      │          │ Identity Center      │
│                      │          │                      │
│ Users, Groups ───────┼──► .json ┼──► scripts/restore_* │
│ Memberships          │          │                      │
│ Permission Sets ─────┼──► .json ┼──► scripts/restore_* │
│ Assignments ─────────┼──► .json ┼──► upstream/mist/    │
│ App Assignments      │          │         restore.py   │
└──────────────────────┘          └──────────────────────┘
```

## Quick Start

Full details in [`docs/RUNBOOK.md`](docs/RUNBOOK.md). Core three-step flow (SOURCE → TARGET):

```bash
# 1. Back up the source account
export AWS_PROFILE=source && export AWS_DEFAULT_REGION=ap-northeast-1
python3 scripts/backup_users_groups.py     --idc-id $SRC_IDC_ID
python3 scripts/backup_permission_sets.py  --idc-arn $SRC_IDC_ARN
python3 upstream/mist/backup.py            --idc-id $SRC_IDC_ID --idc-arn $SRC_IDC_ARN

# 2. Restore into the target account (always --dry-run first)
export AWS_PROFILE=target && export AWS_DEFAULT_REGION=ap-northeast-1
python3 scripts/restore_users_groups.py    --idc-id $DST_IDC_ID
python3 scripts/restore_permission_sets.py --idc-arn $DST_IDC_ARN
python3 upstream/mist/restore.py           --idc-id $DST_IDC_ID --idc-arn $DST_IDC_ARN
```

## Important Caveats

- **If your identity source is an external IdP (Okta / Entra / Google):** you do *not* need to back up users/groups. Just connect the same IdP to the target Identity Center and let SCIM re-provision. Only permission sets + assignments need DR handling.
- **Do not rename `UserName` or group `DisplayName`** — the scripts rely on them as cross-instance join keys.
- **Customer-managed policies must already exist by the same name** in every member account of the target Organization; otherwise assignment provisioning will fail.
- **Passwords and MFA cannot be migrated.** Users receive an invitation email in the new instance to set a new password and re-enroll MFA.

Full risk list: `docs/RUNBOOK.md` §8.

## License

MIT-0. Vendored upstream directories keep their original licenses — see [`NOTICE`](NOTICE).
