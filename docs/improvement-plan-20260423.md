# identity-center-backup — 改进计划 (Improvement Plan)

> 基于：`docs/review-20260423.md`（首席架构师首轮 review + Kiro 专项补充）+ `docs/review-20260423-2.md`（Kiro CLI 的评审复核）
> 日期：2026-04-23
> 作者：首席架构师 🏛️
> 目标：把 repo 从"内部草稿"推进到"可交付给客户的 Layer 1 IdC DR 方案"，并给 Layer 2 Kiro 订阅 DR 打底

---

## 0. 本计划如何生成

第一轮 review 列出了 ~20 条问题 + Kiro 专项 4 条。Kiro CLI 复核后：
- 🟢 11 条确认成立，直接采纳
- 🟡 3 条需调整（见下文 §1.3）
- 🆕 2 条遗漏点补入（SCIM token 一年过期、UserName 大小写一致性）

本计划已把这些合并、去重、重新排优先级，直接作为 sprint backlog 执行。

---

## 1. 关键修正（相对于第一轮 review）

### 1.1 ⚠️ Delegated admin 不能解决"管理账号被删/suspend"
Kiro CLI 指出：delegated admin 只是委托日常管理，Identity Center 实例仍在管理账号下。管理账号被删 → 整个 Org + IdC 实例一起挂，delegated admin 救不了。

⇒ RUNBOOK §1 措辞必须改成：
> "Identity Center 管理委托给独立 identity 账号可以降低日常误操作暴露面，但**无法解决管理账号本身故障的场景**。跨 Org DR 的正解是：**external IdP（首选）+ 本 repo 的 PS/assignment 克隆（次选）**。"

### 1.2 ⚠️ 日备份 RPO = 24h 从 P2 升级到 P1
这是合规问题（SOX / 等保 / GDPR：离职员工次日权限未回收），不是运维便利性。缓解成本低（CloudTrail → EventBridge → 增量 Lambda），没理由延后。

### 1.3 ⚠️ paginate() 改 boto3 paginator 降级为"优雅性提升"
当前实现没有逻辑 bug，改成 `client.get_paginator(...)` 是可读性/一致性收益，不是正确性修复。P2 保留但措辞不要让客户误以为现在代码有漏页风险。

---

## 2. 项目定位调整（最先要做，阻塞其它工作）

### Task A1 — 重写 README + RUNBOOK §0，明确三层定位
**优先级：P0 / 预计：0.5 人日**

把项目从"Identity Center DR 工具"重定位为分层方案：

| Layer | 覆盖范围 | 当前状态 |
|-------|---------|---------|
| **L1 — IdC 登录能力 DR** | users / groups / memberships / permission sets / assignments | 本 repo（需修 §3 P0） |
| **L2 — Kiro 订阅 DR** | Kiro Application、`user-subscriptions:Claim`、overage config | 本 repo 待新增（§3 P0） |
| **L3 — Kiro 应用数据** | conversation / profile / CodeWhisperer tagging | ❌ 自建做不到，依赖 Kiro 产品路线图 |

验收：
- `README.md` 首屏就说清"本方案覆盖 L1+L2，L3 依赖 Kiro 产品侧"
- `RUNBOOK.md §0` 决策树 + 覆盖边界声明
- 删除或修正"无缝切换"字样（真实 RTO 天级）

---

## 3. P0 — 一周内必改（阻塞发布）

### Task B1 — 加 `provision_permission_set` 调用
**文件：** `scripts/restore_permission_sets.py`
**做法：**
```python
# reconcile 完所有策略后
resp = sso.provision_permission_set(
    InstanceArn=instance_arn, PermissionSetArn=arn,
    TargetType="ALL_PROVISIONED_ACCOUNTS",
)
# 轮询 describe_permission_set_provisioning_status 直到 SUCCEEDED/FAILED
```
只在 `list_accounts_for_provisioned_permission_set` 返回非空时触发，避免无用调用。
**验收：** 回滚→重跑场景下，target 已有 assignment 的 PS 策略变更确实推送到所有账号。

### Task B2 — account_id 映射支持
**文件：** 新增 `scripts/apply_account_id_map.py` + `docs/account_id_map.example.json`
**做法：** 读取 mist 的 `UserAssignments.json` / `GroupAssignments.json`，按映射表替换 `TargetId`，输出新文件给 mist/restore.py 消费。
**验收：** 跨 Org 场景下 assignment 能 100% 落地；源 account id 在映射表缺失时明确报错而不是静默失败。

### Task B3 — Kiro 订阅 backup/restore（L2 核心）
**文件：** 新增 `scripts/backup_kiro_subscriptions.py` + `scripts/restore_kiro_subscriptions.py`
**做法：**
- backup：调 `user-subscriptions:ListClaims` / `ListApplicationClaims` / `ListUserSubscriptions` + 读 overage config，以 UserName/GroupDisplayName 为 key 持久化
- restore：依赖 target 账号**已经通过 Kiro console 开通订阅**（前置步骤），脚本负责按 UserName/GroupDisplayName 反查 target IdC 的 UserId/GroupId 后批量 `CreateClaim` + `SetOverageConfig`
- 权限：`user-subscriptions:*` + `sso:ListApplications` + `identitystore:ListUsers/ListGroups`
**验收：** sandbox 实测 claim 重发成功，overage config 一致。

### Task B4 — RUNBOOK §0 覆盖边界 + Kiro console 操作手册
**文件：** `docs/RUNBOOK.md`（新节 §0.5 "覆盖边界"）+ `docs/KIRO-CUTOVER.md`（新建）
**内容：**
- 明确声明本方案**不覆盖** L3（Kiro 侧用户历史 / profile）
- KIRO-CUTOVER.md 逐步列出 target 侧 Kiro console 操作：开通订阅 → 让 Kiro 自动创建 application（⚠️ 不要手工用 sso-admin API 拼 application）→ 跑 `restore_kiro_subscriptions.py` → 和 Kiro 团队对齐历史数据迁移
**验收：** 客户照着 KIRO-CUTOVER.md 能把切换动作跑一遍，不遗漏任何 Kiro console 步骤。

### Task B5 — 端到端实测
**优先级：阻塞发布**
**做法：** 准备一对 sandbox 账号（source + target，同 Org 或跨 Org 各一次），把 L1 + L2 完整 backup/restore 跑通，记录：
- 实际 RTO（从"宣布 source 不可用"到"最后一个 sandbox 用户在 target Kiro 登录使用"）
- CMP 预铺实际工作量
- 踩坑清单
**验收：** `docs/e2e-test-20260XXX.md` 报告，列出每一步耗时、失败点、修复方式。未实测前**不能对外发布**。

### Task B6 — 合规 RPO（24h → 增量）
**文件：** 新增 `infra/cdk/incremental-backup-stack.ts` 或至少 `docs/incremental-backup.md` 设计
**做法：** CloudTrail 捕获 `CreateUser/DeleteUser/CreateGroup/.../CreateAccountAssignment/DeleteAccountAssignment` 等事件，EventBridge → Lambda 增量 diff 到 S3。
**验收：** 用户入离职事件 1h 内反映到备份目录（不是代码，但方案要有）。

---

## 4. P1 — 两周内

### Task C1 — 异常捕获收窄
**文件：** `scripts/backup_permission_sets.py`、`scripts/restore_permission_sets.py`
- inline policy / boundary / tags 的 `except Exception:` 改为只吞 `ResourceNotFoundException`（或 AccessDenied 时显式 log.error + 继续）
- `delete_inline_policy` / `delete_permissions_boundary` 的 bare `except ClientError` 同上
**验收：** 模拟 AccessDenied 时脚本报错可见，ResourceNotFound 时静默跳过。

### Task C2 — 真的用上 backoff 或从 requirements 里删
**文件：** `scripts/backup_users_groups.py` + `restore_users_groups.py`
**做法：** 对 `CreateUser` / `CreateGroup` / `CreateGroupMembership` 加 `@backoff.on_exception(backoff.expo, ClientError, max_tries=10, giveup=not_throttling)`。或改用 boto3 `adaptive` retry mode（`Config(retries={"mode": "adaptive", "max_attempts": 10})`）。
**验收：** 1000 用户 batch 压测无 unhandled throttling。

### Task C3 — tag reconcile
**文件：** `scripts/restore_permission_sets.py` update 分支
**做法：** 现有 tag + 期望 tag 做 diff，调用 `TagResource` / `UntagResource`。
**验收：** 更新后 tag 和 backup 一致。

### Task C4 — IAM policy JSON 文件化
**文件：** 新增 `docs/iam/iam-policy-source.json` + `iam-policy-target.json`
**内容：** 把 RUNBOOK §3.1 / §3.2 里散装 action 列表整理成真正可贴的 IAM policy document（不要用 `*` 资源，能按 arn 限缩的就限缩）。RUNBOOK 正文引用过去。
**验收：** `aws iam create-policy --policy-document file://iam-policy-source.json` 不报 syntax error。

### Task C5 — CustomerManagedPolicyManifest.json 输出
**文件：** `scripts/backup_permission_sets.py`（增强）
**做法：** backup 同时生成 `CustomerManagedPolicyManifest.json`，列出所有 `(permission_set_name, policy_name, needs_deployment_to_accounts=[account_id,...])` 对，供 StackSet 预铺使用。
**验收：** 客户能直接用这份清单驱动 StackSet 在 target Org 成员账号上创建同名 policy。

### Task C6 — Applications 备份（至少 checklist）
**文件：** 新增 `scripts/backup_applications.py`
**做法：** 调 `sso-admin:ListApplications` + `DescribeApplication` + `ListApplicationAssignmentConfigurations` + `signin` 相关 API，导出 application 清单 + TTI 配置。即便不能自动重建（Kiro application 必须 console 建），也要给客户一份"target 侧需要手工重建的 application 列表 + 参数"。
**验收：** 输出 `Applications.json` 包含足够信息让运维照着在 target 重建（对 Kiro 除外，Kiro 走 Kiro console）。

### Task C7 — SCIM token 过期监控（🆕 Kiro CLI 补充）
**文件：** `docs/scim-token-ops.md` + 如有 `infra/` 则加 CDK 资源
**做法：**
- RUNBOOK §7.1 日常维护加一条："target 侧 SCIM token 每 11 个月 rotate 一次（不等 AWS 90 天提醒）"
- 订阅 AWS 的 `IAM Identity Center SCIM Token Expiring` EventBridge event，推到 SNS/Slack
**验收：** sandbox 上模拟 token 快过期事件能触发告警。

### Task C8 — UserName 大小写一致性检查（🆕 Kiro CLI 补充）
**文件：** `scripts/backup_users_groups.py`（backup 阶段校验） + `restore_users_groups.py`（restore 前 pre-check）
**做法：** backup 时如果发现 `UserName.lower()` 去重后数量 < 原数量 → 强制报错（source 有大小写冲突的脏数据）；restore 前抽查 target 侧 UserName 大小写一致。
**验收：** 脏数据场景脚本明确失败并列出冲突 UserName。

---

## 5. P2 — 一个月内

| Task | 说明 |
|------|------|
| D1 | paginate() 改用 boto3 paginator（**优雅性提升，非 bug 修复**，措辞避免让客户误以为有漏页） |
| D2 | pytest 覆盖纯函数：`build_create_user_payload`、paginate、memberships 翻译 |
| D3 | `infra/` CDK stack：Lambda + EventBridge + S3 (versioning / Object Lock / CRR) |
| D4 | `compare.py` diff 工具：源 target 双向对账 |
| D5 | 成本 / 时间估算表写进 RUNBOOK §3（target 账号开通、CMP 预铺、Kiro 订阅双份费用等） |
| D6 | DNS 封装层（`login.example.com` CNAME → 活跃 start URL）+ 季度切换演练剧本 |
| D7 | 评估直接用 `aws-iam-identity-center-extensions` 替代自写脚本的可行性 |
| D8 | "如果客户接受 external IdP 方案，本 repo 能省多少工作量"量化分析（🆕 Kiro CLI 建议） |

---

## 6. 迭代节奏与交付物

| Sprint | 目标 | 关键交付 |
|--------|------|---------|
| **Sprint 0（本周）** | 项目定位 + 必要披露 | Task A1、B4、新 README + 三层定位 |
| **Sprint 1（下周）** | 技术 P0 补齐 | B1 / B2 / B3 / B6 代码；可在 sandbox 跑 |
| **Sprint 2（+1 周）** | 端到端实测 | B5 报告；未实测绝不发布 v0.1 |
| **Sprint 3（+2 周）** | P1 全量 | C1–C8；发布 v0.1-rc 给内部 review |
| **Sprint 4（+4 周）** | P2 + 文档打磨 | D1–D8；发布 v0.1 GA |

---

## 7. 发布 gate（三道门）

1. **Gate 1 — 披露完整：** README + RUNBOOK §0 / §0.5 明确覆盖边界（L1/L2/L3），不误导客户
2. **Gate 2 — 技术底线：** B1（provision）+ B2（account_id map）+ B3（Kiro 订阅脚本）+ C1（异常收窄）全部完成
3. **Gate 3 — 实测通过：** Task B5 一对 sandbox 账号端到端走通，真实 RTO 数字进 RUNBOOK

**三道门全过 = v0.1 可以交付给客户**。只过 Gate 1+2 = 内部技术储备，不交付。

---

## 8. 给客户之前必须谈清楚的三件事

1. **SLA 重对齐** — RTO 是天级（Kiro 订阅重发 + MFA 重注册），不是秒级。把"无缝切换"改成"有计划切换"
2. **Kiro 产品侧确认** — TIP 绑定、用户 profile / conversation / CodeWhisperer tagging 能否跨账号迁移？这是 L3 层问题，本 repo 解决不了，需要 Kiro 产品团队答复
3. **成本透明** — target 账号需独立购买 Kiro 企业订阅（1000 seat 双份费用，直到切换完成）+ CMP 在 target Org 成员账号铺设工作量

这三条没谈清之前，任何"识别中心 DR 方案"交付都会在切换当天变成信任事故。

— 首席架构师 🏛️
