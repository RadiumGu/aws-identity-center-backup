# SCIM Token 运维手册

> AWS IAM Identity Center 的 SCIM access token 有效期 **最长 1 年**，过期后外部 IdP（Okta/Entra/Google）到 IdC 的用户同步**立即停止**。AWS 只在到期前 90 天发 event 提醒。

## 为什么这是 DR 场景的隐患

- 如果客户走 **external IdP → target IdC** 的 DR 方案，target 侧的 SCIM token 平时不用
- 没人主动 rotate → 某天 token 静默过期
- 灾难发生日切到 target，用户/组已经和 source 差了几周 → 登进去权限错乱

## 运维要求

1. **主动 rotate：** target 侧 SCIM token 每 **11 个月** rotate 一次，不等 AWS 的 90 天提醒
2. **告警订阅：** EventBridge 订阅 `IAM Identity Center SCIM Token Expiring` 事件，推到 SNS/Slack

### EventBridge 规则示例

```json
{
  "source": ["aws.sso"],
  "detail-type": ["SCIM Token Expiration Notification"]
}
```

Lambda target 或 SNS → 告警通道。

### Rotate 流程（target 侧）

1. IdC console → Settings → Identity source → "Automatic provisioning" → Generate new token
2. 把新 token 复制到 external IdP（Okta/Entra）的 SCIM 配置
3. 在 IdP 侧触发一次全量同步
4. 确认 target IdC 里用户/组数量 = source 一致
5. 删除旧 token

### 在 RUNBOOK 里的引用

本文档在 `RUNBOOK.md §7.1 日常维护` 被引用。每个 target 账号都应有独立的 SCIM token 日历提醒。

---

## 检查清单（季度 review 用）

- [ ] target 侧 SCIM token 剩余有效期 > 6 个月
- [ ] EventBridge 规则存在且指向活跃告警通道
- [ ] 上次 rotate 时间 ≤ 11 个月前
- [ ] 最近一次"灾备演练"测试过 target IdC 的 SCIM 同步链路
