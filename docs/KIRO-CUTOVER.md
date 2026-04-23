# KIRO-CUTOVER.md — Kiro 订阅切换手册（L2）

> 目标：主账号 Identity Center 故障时，在备用账号恢复 **Kiro 订阅可用性**。
> 前置：L1（IdC users/groups/PS/assignments）已按 `RUNBOOK.md` 恢复完成。

⚠️ 本手册覆盖 **L2 Kiro 订阅层**。不覆盖：
- L3 Kiro 侧用户历史数据（conversation、profile、CodeWhisperer tagging、Q Developer dashboard）—— 需要联系 Kiro 产品团队确认是否可迁移，本项目无法处理。

---

## 0. 前置检查

- [ ] L1 已完成：target IdC 里 users/groups 与 source 一一对应（UserName / DisplayName 一致）
- [ ] `KiroSubscriptions.json` 已在 source 通过 `backup_kiro_subscriptions.py` 导出
- [ ] 已和客户确认：**接受 target 账号独立购买一份 Kiro 企业订阅**（切换期双份费用）
- [ ] 已和 Kiro 产品团队对齐 L3 迁移边界（通常不可迁）

---

## 1. 在 target 账号开通 Kiro 订阅（手工，必须 console 做）

登录 target AWS 账号 → 进入 **Kiro 控制台** → 按 Kiro enterprise 订阅流程：

1. 选择 IAM Identity Center 作为 identity source
2. 绑定到 target Identity Center 实例（`TARGET_IDC_ARN`）
3. 完成订阅开通

这一步让 Kiro 自动完成：
- 创建 IdC Application（带 authentication method / grant / TIP 配置）
- 创建 Service-Linked Roles：`AWSServiceRoleForUserSubscriptions`、`AWSServiceRoleForAmazonQDeveloper`
- 注册 Trusted Identity Propagation application 用于下游（Q Developer / CodeWhisperer / CodeGuru Security）

> 🚫 **不要尝试用 `sso-admin create-application` 手工拼这个 application**。Kiro 会随产品版本调整 TIP / grant / auth method 的组合，手工复原会在下次 Kiro 升级时踩坑。Kiro console 是唯一可持续的方式。

验证：
```bash
aws sso-admin list-applications --instance-arn $TARGET_IDC_ARN \
  | jq '.Applications[] | select(.ApplicationProviderArn | contains("q.amazonaws.com"))'
```
应当返回一条新的 application ARN。

---

## 2. 恢复订阅 claim

```bash
export AWS_PROFILE=target
export AWS_DEFAULT_REGION=<region>
cd /path/to/backups/<YYYY-MM-DD>

# 先 dry-run
python3 ../../scripts/restore_kiro_subscriptions.py \
    --idc-arn $TARGET_IDC_ARN --idc-id $TARGET_IDC_ID --dry-run

# 确认输出中 "would CreateClaim" 的用户/组数量与 source 一致后正式跑
python3 ../../scripts/restore_kiro_subscriptions.py \
    --idc-arn $TARGET_IDC_ARN --idc-id $TARGET_IDC_ID
```

脚本行为：
- 按 `UserName` / `GroupDisplayName` 在 target IdC 反查新的 UserId/GroupId
- 对 Kiro application 批量调 `user-subscriptions:CreateClaim`
- 应用 `SetOverageConfig`（seat 超额计费模式），与 source 保持一致

---

## 3. 验证

### 3.1 Claim 数量对齐
```bash
# source 侧记录的 claim 数
jq '.UserClaims | length, .GroupClaims | length' KiroSubscriptions.json

# target 侧实际创建数
aws user-subscriptions list-claims --application-arn <TARGET_KIRO_APP_ARN> \
  | jq '.Claims | length'
```
数字应当一致。

### 3.2 抽样用户登录测试
- 在 target Kiro console 的用户列表里，抽 3 个普通用户 + 1 个 admin，确认 subscription status = Active
- 这些用户用 target start URL 登录后，能打开 Kiro 客户端、看到订阅生效

### 3.3 TIP 下游验证
Kiro 通过 TIP 把身份透传到 Q Developer / CodeWhisperer。target 账号的这套 TIP 配置是全新的，预期：
- ✅ 用户能重新使用 Kiro 新对话
- ❌ 用户在 source 的历史 conversation / CodeWhisperer profile **通常不会自动迁移**
- ⚠️ 和 Kiro 产品团队确认具体的数据保留策略

---

## 4. 切换完成的信号

- [ ] target IdC 用户/组数等于 source（L1 验证已过）
- [ ] `list-claims` 数量等于 source `KiroSubscriptions.json` 里记录数
- [ ] Overage config 一致
- [ ] 抽样 3+1 用户登录 Kiro 订阅生效
- [ ] DNS / 用户通知已切到 target start URL

---

## 5. 真实 RTO 记录

每次切换（含演练）填写 `docs/cutover-log.md`：

| 字段 | 示例 |
|------|------|
| 演练/真实切换 | 演练 |
| 日期 | 2026-04-23 |
| L1 restore 耗时 | 12 min |
| target Kiro 订阅开通耗时 | 2 hours（含企业支持审核） |
| L2 restore_kiro_subscriptions 耗时 | 3 min（1000 claim） |
| MFA 全员重注册 | ~4 hours（helpdesk 排队） |
| **端到端 RTO** | 约 1 工作日 |

这不是秒级切换。客户的 SLA 假设必须以天为单位。

---

## 6. 回滚

如果 target 订阅 claim 出问题：
- **删除单个 claim 重试：** `aws user-subscriptions delete-claim --claim-id <id>` 再重跑脚本
- **完全重来：** Kiro console → 退订 → 重新订阅（会重建 application），再跑 restore

⚠️ 切勿在 source 仍可用时完全退订 source，这会让所有用户丢 Kiro 访问。切换前两账号保持双活。

---

## 7. 必须和 Kiro 团队对齐的事

在 target 交付给客户之前，以下问题必须有明确答复（写进 `docs/kiro-qa-log.md`）：

1. Kiro 侧对话历史 / 保存的 artifact / CodeWhisperer profile：**跨 AWS 账号可迁移吗？**
2. Q Developer 里的 user-level dashboard 数据跨账号处理方式？
3. SAML/SCIM endpoint 在 Kiro application 重建后是否要客户侧应用重接？
4. 订阅计费周期：source 账号尚在订阅期内，target 账号又买了订阅 —— 是否能按比例退款或冻结 source？

这四条得不到答复，就不要让客户做完整切换 —— 先做成"冷备演练"，不动商务侧。

— 首席架构师 🏛️
