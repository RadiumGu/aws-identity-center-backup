# Sprint 0 + Sprint 1 执行摘要（2026-04-23）

基于 `docs/improvement-plan-20260423.md` 完成的第一批落地。

## ✅ 已完成

### 代码层

| Task | 文件 | 说明 |
|------|------|------|
| B1 provision_permission_set | `scripts/restore_permission_sets.py` | PS 策略 reconcile 后，若存在已分配账号则调用 `provision_permission_set` + 轮询 `describe_permission_set_provisioning_status`（最多 5 分钟）推送变更 |
| C1 异常收窄（restore） | `scripts/restore_permission_sets.py` | `delete_inline_policy` / `delete_permissions_boundary` 的 bare `except ClientError: pass` → 只吞 `ResourceNotFoundException`，AccessDenied 等会 raise |
| C1 异常收窄（backup） | `scripts/backup_permission_sets.py` | `get_inline_policy` / `get_permissions_boundary` / `list_tags` 的 `except Exception:` → 同上收窄 |
| C3 tag reconcile | `scripts/restore_permission_sets.py` | update 分支现在用 `TagResource` + `UntagResource` 做 diff，不再「只进不出」 |
| C8 UserName case 校验 | `scripts/backup_users_groups.py` | backup 阶段检测 `UserName.lower()` 冲突，有冲突时直接 SystemExit 并列出冲突项 |
| B3 Kiro 订阅 backup | `scripts/backup_kiro_subscriptions.py`（新增） | 导出 `user-subscriptions:Claim`，按 UserName/GroupDisplayName 持久化（可跨账号）+ overage config |
| B3 Kiro 订阅 restore | `scripts/restore_kiro_subscriptions.py`（新增） | 前置：target 已 Kiro console 订阅；按 UserName/GroupDisplayName 在 target IdC 反查 UserId/GroupId，批量 `CreateClaim` + `SetOverageConfig` |

语法校验：`python3 -m py_compile scripts/*.py` 全部通过。
**真实 API 行为需 sandbox 实测（Task B5，未完成）。**

### 文档层

| 文件 | 内容 |
|------|------|
| `README.md`（顶部重写） | 三层定位表 + 真实 RTO 说明 + 交付前 SLA 对齐提醒 |
| `docs/RUNBOOK.md §0`（新增） | 覆盖边界声明 + §0.1 架构前置建议（external IdP = 跨 Org DR 正解；delegated admin 不救"管理账号被删/suspend"） |
| `docs/KIRO-CUTOVER.md`（新增） | L2 切换完整手册：target 侧订阅开通 → 应用重建（走 Kiro console，**不要** `sso-admin create-application`）→ claim restore → 验证 → 和 Kiro 团队对齐 QA |
| `docs/scim-token-ops.md`（新增） | SCIM token 一年过期运维：11 个月主动 rotate + EventBridge 订阅过期事件 |
| `docs/iam/iam-policy-source.json`（新增） | 源账号最小 IAM policy，可直接 `aws iam create-policy --policy-document file://...` |
| `docs/iam/iam-policy-target.json`（新增） | 目标账号最小 IAM policy，含 `user-subscriptions:*` + `sso:ProvisionPermissionSet` + `sso:TagResource/UntagResource` |

## 🟡 未完成（需下一轮或需 sandbox 条件）

| Task | 阻塞原因 |
|------|---------|
| B2 account_id 映射脚本 | 需要确认跨 Org 映射表格式 + sandbox 验证 |
| B5 端到端实测 | **阻塞发布**，需一对 sandbox 账号 |
| B6 增量备份 CDK | 需先定义 S3 bucket / IAM 架构 |
| C2 backoff 真用上 | 简单工作，可下轮补；1000 用户量级未实测之前优先级不高 |
| C5 CustomerManagedPolicyManifest.json 输出 | 需扩展 `backup_permission_sets.py` 拿 `list_accounts_for_provisioned_permission_set` 数据 |
| C6 Applications backup | 需确认 Kiro application 之外还有哪些 SAML/SCIM app，sandbox 调 API 看 schema |
| D 系列（P2） | 未动，按计划一个月内 |

## 🟥 未过发布 gate

- Gate 1（披露完整）✅ 已过
- Gate 2（技术底线）🟡 B1/B3/C1/C3/C8 已完成；B2 未完成
- Gate 3（实测通过）❌ 未做

**结论：当前状态可作为 internal v0.1-rc（技术储备）给同事 review，不能交付给客户。下一轮重点是 B2 + B5。**

## 下一步建议

1. **立刻做：** 让客户同意先走架构前置建议（external IdP + IaC 化），如果同意，这个 repo 的交付价值就从「DR 工具」变成「定期 backup + 紧急恢复工具」，范围大幅缩小
2. **若客户坚持 clone 方案：** 准备 sandbox 账号对，完成 B2 + B5
3. **同步启动：** 找 Kiro 产品团队，就 KIRO-CUTOVER.md §7 四个问题约会议

— 首席架构师 🏛️
