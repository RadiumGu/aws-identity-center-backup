# ADR-001: Incremental cross-account Identity Center DR via EventBridge → S3 relay

> Status: *Proposed* — awaiting client sign-off before Phase 1 implementation
> Date: 2026-04-23
> Author: 编程猫 💻 (aws-coder)
> Supersedes: RUNBOOK §2 footnote "每日 Lambda 备份" (too coarse for compliance RPO)

## 1. Context

Current repo ships scripts for *point-in-time* backup + restore of
Identity Center (L1) and Kiro subscriptions (L2). It does not answer:

1. How do we keep the TARGET account's DR data current between the last
   full backup and "now"? A daily cron gives RPO = 24h, which fails
   typical SOX / 等保 / GDPR checks when a just-off-boarded employee's
   access lingers for a day.
2. Should TARGET be warm (applies changes immediately) or cold (holds
   data, applies on cutover)?
3. How do we move data across account boundaries safely and auditably?

The improvement-plan task B6 mandates "incremental backup". This ADR
captures the architecture that answers all three together.

## 2. Decision

Adopt a **cold-DR + near-real-time data replication** model:

- **Hot side (SOURCE identity account):** CloudTrail → EventBridge →
  Lambda captures every mutation and writes an incremental snapshot +
  an append-only event log to an S3 bucket (SSE-KMS, versioning, Object
  Lock, cross-region replication).
- **Cold side (TARGET DR account):** a mirror bucket receives objects
  via CRR. A TARGET Lambda triggered by `s3:ObjectCreated` consumes
  them. Default behavior is `dry-run`: reconcile but do not mutate
  target Identity Center, write a diff report to CloudWatch Logs.
- **Cutover:** an operator flips an SSM Parameter `/dr/mode` to
  `ACTIVE`. The same Lambda, on the next S3 event, runs the full
  `restore_*.py` pipeline against target. No code redeploy required.

RPO target: **≤ 15 minutes** (CloudTrail delivery ≤ 5 min + EventBridge
≤ 1 min + Lambda + S3 CRR SLA 15 min P99; typically single-digit
minutes).

Kiro subscriptions (L2) are excluded from the real-time path. The
`user-subscriptions` service is private, CreateClaim has no reliable
external API, and the target account needs seats purchased by a human
anyway. L2 continues to run as a daily full snapshot.

## 3. Why not the alternatives

### 3.1 Warm dual-write (realtime replicate to target Identity Center)
Rejected. Requires Kiro subscriptions paid in both accounts continuously
(doubled cost), creates an always-live attack surface equal to SOURCE,
and propagates SOURCE bugs to TARGET within seconds. The business need
is account-level DR, not active-active HA.

### 3.2 Direct cross-account Lambda invocation (no S3)
Rejected. Couples SOURCE and TARGET runtime health, gives no audit
evidence chain, and the target-side security perimeter must trust
SOURCE credentials. S3 with SSE-KMS + Object Lock decouples both.

### 3.3 Daily-only full backup
Rejected per B6. RPO = 24h is not defensible for employee-leaves-today
scenarios.

### 3.4 Using the private user-subscriptions API for L2 realtime
Rejected. Verified to return 500/UnknownOperation on external
CreateClaim calls (audit-20260423.md §3.2). Not reliable enough to
automate; keep L2 as a daily snapshot + manual cutover via Kiro console.

## 4. Architecture

```
SOURCE account (identity account)                    TARGET account (DR)
┌──────────────────────────────────────┐            ┌──────────────────────────┐
│ Identity Center                      │            │ Identity Center (cold)   │
│   │                                  │            │                          │
│   │ CloudTrail (management events)   │            │                          │
│   ▼                                  │            │                          │
│ EventBridge                          │            │                          │
│   │ rules: sso/identitystore APIs    │            │                          │
│   ▼                                  │            │                          │
│ Lambda: incremental_snapshot.py ─────┼──► S3 ─CRR─┼──► S3 (DR bucket)        │
│   • event.detail → resource id       │            │         │                │
│   • describe_* the one affected      │            │         ▼                │
│     resource only                    │            │ EventBridge (S3 Created) │
│   • write events/YYYY-MM-DD/*.json   │            │   │                      │
│                                      │            │   ▼                      │
│ Lambda: full_snapshot.py (daily cron)│            │ Lambda: sync_state.py    │
│   • existing scripts/backup_*.py     │            │   default dry-run        │
│   • write snapshots/full/…           │            │   SSM /dr/mode=ACTIVE →  │
│                                      │            │   run restore_*.py       │
│ S3 bucket                            │            │                          │
│  + SSE-KMS (CMK in SOURCE)           │            │ CloudWatch Dashboard +   │
│  + versioning                        │            │  SNS alarms              │
│  + Object Lock (compliance mode)     │            │                          │
│  + CRR to TARGET bucket              │            │                          │
└──────────────────────────────────────┘            └──────────────────────────┘
```

### 4.1 EventBridge rule pattern (source side)

```json
{
  "source": ["aws.sso", "aws.sso-directory", "aws.identitystore"],
  "detail-type": ["AWS API Call via CloudTrail"],
  "detail": {
    "eventName": [
      "CreateUser", "UpdateUser", "DeleteUser",
      "CreateGroup", "UpdateGroup", "DeleteGroup",
      "CreateGroupMembership", "DeleteGroupMembership",
      "CreatePermissionSet", "UpdatePermissionSet", "DeletePermissionSet",
      "AttachManagedPolicyToPermissionSet",
      "DetachManagedPolicyFromPermissionSet",
      "PutInlinePolicyToPermissionSet",
      "DeleteInlinePolicyFromPermissionSet",
      "AttachCustomerManagedPolicyReferenceToPermissionSet",
      "DetachCustomerManagedPolicyReferenceFromPermissionSet",
      "PutPermissionsBoundaryToPermissionSet",
      "DeletePermissionsBoundaryFromPermissionSet",
      "TagResource", "UntagResource",
      "CreateAccountAssignment", "DeleteAccountAssignment",
      "CreateApplicationAssignment", "DeleteApplicationAssignment",
      "ProvisionPermissionSet"
    ]
  }
}
```

⚠️ The SCIM provisioner surfaces under `aws.sso-directory`, not
`aws.sso`. Missing that source drops every Okta/Entra-originated change.

### 4.2 Three snapshot layers in S3

| Prefix | Trigger | Contents | Use |
|--------|---------|----------|-----|
| `events/YYYY-MM-DD/events.jsonl` | every mutation | append-only CloudTrail event payloads | audit trail, RPO evidence |
| `snapshots/incremental/<ts>.json` | every mutation | `describe_*` of just the touched resource | diff / replay a specific change |
| `snapshots/full/YYYY-MM-DD.json` | daily cron | full output of existing `scripts/backup_*.py` | baseline for disaster restore |

### 4.3 Cross-account transport

Use S3 Cross-Region Replication (or same-region replication when SOURCE
and TARGET share region) with these non-negotiables:

- SSE-KMS using a CMK in SOURCE; TARGET bucket uses its own CMK.
  Replication role holds `kms:Decrypt` on source CMK and
  `kms:Encrypt` / `GenerateDataKey` on target CMK. **Do not** share
  one KMS key across accounts — it fights with Object Lock and breaks
  key rotation.
- Bucket policies pin `aws:SourceAccount` / `aws:SourceArn` to prevent
  the confused-deputy pattern on replication role.
- Object Lock on BOTH buckets (compliance mode, 90-day default
  retention tunable).
- Replication metrics + SNS alarm on lag > 15 min.

### 4.4 TARGET Lambda state machine

```
s3:ObjectCreated event
      │
      ▼
read /dr/mode from SSM Parameter Store
      │
      ├─ "STANDBY"  (default) → run reconcile in dry-run, emit CloudWatch
      │                         metric `dr.drift.<resource>`, log to
      │                         Logs; no mutation
      │
      └─ "ACTIVE"   (cutover) → map snapshot → restore_*.py equivalent,
                                actually mutate target Identity Center,
                                publish progress to SNS
```

Changing mode is a single API call by the on-call engineer. No code or
CDK redeploy. Pair with an SCP that blocks direct writes to target
Identity Center when mode is `STANDBY` so humans can't race the Lambda.

## 5. Out of scope for this ADR

- Kiro L2 subscription realtime sync (private API gated).
- L3 Kiro conversation / profile / CodeWhisperer tagging migration —
  still product-team problem.
- Active-active user login across accounts (explicitly rejected).
- DNS-layer start-URL failover — separate ADR (see §7 TODO).

## 6. Phasing

| Phase | Deliverable | Effort | Gate |
|-------|-------------|--------|------|
| 0 | *Event inspector*: deploy a minimal EventBridge → HTTP destination into a sandbox, collect 1-week sample of real SCIM / console events, confirm which fields are present and reliable | 0.5 day | none |
| 1 | `infra/cdk/source-event-tap-stack.ts` — bucket, KMS, CloudTrail trail confirm, EventBridge rule, SOURCE incremental Lambda | 2 days | Phase 0 sample validated |
| 2 | `infra/cdk/dr-relay-stack.ts` — TARGET bucket, CMK, CRR role, TARGET Lambda in dry-run mode, CloudWatch dashboard + alarms | 1.5 days | Phase 1 writes land in source bucket |
| 3 | `infra/cdk/cutover-switch-stack.ts` — SSM Parameter, TARGET Lambda ACTIVE mode wiring, SCP for STANDBY-write-block | 1 day | Phase 2 dry-run reports match reality |
| 4 | E2E sandbox: simulate 10 employee lifecycle events, measure time from source API call to TARGET S3 object, compare against RPO 15 min | 1 day | pass/fail against SLA |

Total: **5.5–6 person-days** including sandbox validation.

## 7. Open questions / next ADRs

- ADR-002: DNS/CNAME layer for start-URL failover (`login.example.com`
  → active Identity Center start URL). Alternate path avoids the need
  for end-users to change IDE-cached URLs after cutover.
- ADR-003: Kiro L2 product-side DR (dependency on Kiro PM team).
- ADR-004: CMP manifest pipeline — use `CustomerManagedPolicyManifest.json`
  (Task C5) to StackSet-deploy required policies into TARGET Org
  member accounts before cutover.

## 8. Decision record

- [ ] Client reviewed and signed off (date / name)
- [ ] Phase 0 sample captured and attached to this ADR
- [ ] Phase 1 merged (PR link)
- [ ] Phase 2 merged
- [ ] Phase 3 merged
- [ ] Phase 4 E2E report attached

Flip to *Accepted* once §8 items 1–2 are done. Flip to *Implemented*
after Phase 4.
