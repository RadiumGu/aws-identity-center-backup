#!/usr/bin/env python3
"""
kiro_restore_checklist.py — Print a human-readable restore checklist from a
KiroSubscriptions.json snapshot.

Since Kiro subscription creation has no public API, restoring to a target
account is a MANUAL / console operation. This script produces:

  1. A seat-purchase plan (how many seats of each plan type to buy).
  2. A grouped assignment list (by plan type → users + groups) that the
     target-account admin can paste-follow in the Kiro console:
        Kiro → Users & Groups → Add user / Add group.

Usage:
  python3 kiro_restore_checklist.py --input KiroSubscriptions.json \
      > kiro-restore-checklist.md
"""
import argparse
import json
import sys
from collections import defaultdict


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="KiroSubscriptions.json")
    args = p.parse_args()

    with open(args.input) as f:
        data = json.load(f)

    subs = data["Subscriptions"]
    by_plan = defaultdict(lambda: {"USER": [], "GROUP": []})
    totals = defaultdict(int)

    for s in subs:
        plan = s.get("subscriptionType") or s.get("SubscriptionType") or "UNKNOWN"
        ag = (s.get("aggregated") or s.get("Aggregated") or "USER").upper()
        uname = (s.get("username") or s.get("UserName")
                 or s.get("principalId") or "<unknown>")
        status = (s.get("status") or s.get("Status") or "").upper()
        if status not in ("ACTIVE", "PENDING"):
            continue
        totals[plan] += 1
        bucket = "GROUP" if ag == "GROUP" else "USER"
        by_plan[plan][bucket].append(uname)

    out = []
    out.append(f"# Kiro Subscription Restore Checklist")
    out.append("")
    out.append(f"- Source instance: `{data['InstanceArn']}`")
    out.append(f"- Captured at:     `{data['CapturedAt']}`")
    out.append(f"- Total records:   {len(subs)}")
    out.append("")
    out.append("## Step 1 — Purchase seats on the TARGET account")
    out.append("")
    out.append("Amazon Q Developer / Kiro console → Subscriptions → buy the "
               "matching number of seats for each plan:")
    out.append("")
    out.append("| Plan | Seats to buy |")
    out.append("|------|--------------|")
    for plan, n in sorted(totals.items()):
        out.append(f"| {plan} | {n} |")
    out.append("")
    out.append("## Step 2 — Assign users/groups in the TARGET Kiro console")
    out.append("")
    out.append("Kiro → Users & Groups → *Add user* / *Add group* for each "
               "entry below. User/group names match the Identity Center "
               "names already restored in the earlier steps.")
    out.append("")
    for plan in sorted(by_plan):
        users = sorted(set(by_plan[plan]["USER"]))
        groups = sorted(set(by_plan[plan]["GROUP"]))
        out.append(f"### {plan}")
        out.append("")
        if users:
            out.append(f"*Users ({len(users)}):*")
            for u in users:
                out.append(f"- `{u}`")
            out.append("")
        if groups:
            out.append(f"*Groups ({len(groups)}):*")
            for g in groups:
                out.append(f"- `{g}`")
            out.append("")
        if not users and not groups:
            out.append("_(no active entries)_\n")

    out.append("## Step 3 — Verify")
    out.append("")
    out.append("Re-run `backup_kiro_subscriptions.py` against the TARGET "
               "account and diff against this snapshot. Counts per plan "
               "should match.")
    print("\n".join(out))


if __name__ == "__main__":
    sys.exit(main())
