# Identity Center Backup & DR Runbook

> 场景：一个 AWS 账号下有 1000+ Kiro 订阅用户通过 Identity Center 登录，客户担心账号级故障，需要把 Identity Center 里的 users / groups / memberships / permission sets / account assignments 整套复制到另一个备用账号，备用账号使用新的 start URL，保证名字不重复、故障时能顶上。

本 runbook 覆盖：
1. 前置评估（决定走哪条路径）
2. 基线备份（每天/每周自动跑，出数据）
3. 新账号初始化（Identity Center 启用 + IAM 委托）
4. 数据恢复（users → groups → memberships → permission sets → assignments）
5. 验证 + 切换 + 回滚

---

## 0. 决策树：先判断身份源

Identity Center 支持三种 identity source，*灾备方案完全不同*：

| 身份源 | Users/Groups 恢复方式 | 用不用本 repo 的脚本 |
|--------|-----------------------|----------------------|
| *External IdP (Okta / Entra / Google)* | 把同一个 IdP 再连一次到新实例，SCIM 自动同步 | ❌ 不需要 `backup_users_groups.py`，只需要 permission sets + assignments |
| *Active Directory (AD Connector)* | 新账号同样连到 AD | ❌ 同上 |
| *Identity Center 自建目录* | 必须脚本导出 + 重建 | ✅ 全套脚本都要用 |

> 🔑 *行动项*：找客户确认身份源。AWS 控制台 → IAM Identity Center → Settings → "Identity source" 一栏。

---

## 1. 架构建议（重要）

Identity Center 自身已经是 region-级高可用服务（底层跨 AZ），所谓"账号级故障"的实际触发场景通常是：
- Org 管理账号被误删除 / 被 suspend
- 账号凭证被吊销 / 合规冻结
- 管理账号 root 被锁

*强烈建议*架构上做一次性修正（再谈备份）：

1. *启用 Organizations delegated administrator*：把 Identity Center 的管理委托给 Org 里一个专用的 *identity account*，业务账号不碰 Identity Center。即便业务账号出事，登录链路不受影响。
2. *Identity Center 自身不要放主账号*：同理。
3. *Permission Sets + Assignments 全部 IaC 化*：用 `aws-samples/single-stage-aws-iam-identity-center-pipeline` 或本 repo 里的 JSON 备份，CI 驱动。

如果客户同意按上面修正架构，就不需要"账号级克隆 Identity Center"了，只要灾难时把 Org 换到 standby Org 或新建的 identity account 重新托管。

以下流程针对 *客户坚持要克隆一套 Identity Center 到新账号* 的场景。

---

## 2. 目录结构

```
/home/ubuntu/tech/identity-center-backup
├── README.md                              # 项目说明
├── docs/
│   ├── RUNBOOK.md                         # 本文件
│   └── ARCHITECTURE.md                    # 架构权衡记录（TODO）
├── scripts/
│   ├── backup_users_groups.py             # 导出 users/groups/memberships
│   ├── restore_users_groups.py            # 新账号重建 users/groups/memberships
│   ├── backup_permission_sets.py          # 导出 permission sets 定义
│   └── restore_permission_sets.py         # 新账号重建 permission sets
└── upstream/
    ├── mist/                              # aws-samples: assignment 备份/恢复
    └── ic-extensions/                     # aws-samples: 更完整的 Region-Switch 方案（CDK）
```

**AWS 官方已有组件的职责分工：**
- `upstream/mist/backup.py` + `restore.py` — 负责 *assignments*（UserAssignments / GroupAssignments / AppAssignments）
- 本 repo `scripts/` — 补齐 *users / groups / memberships / permission sets* 这一段官方 sample 不覆盖的部分

---

## 3. 前置条件

### 3.1 源账号 (SOURCE)
- Identity Center 已启用，获取：
  - `SOURCE_IDC_ARN` = `arn:aws:sso:::instance/ssoins-xxxxxxxxxx`（控制台 → Settings 顶部）
  - `SOURCE_IDC_ID`  = `d-xxxxxxxxxx`（Identity Store ID，同页面）
  - `SOURCE_REGION`（Identity Center 所在 region，通常是管理账号主 region）
- 备份运行角色 IAM 权限：
  ```
  identitystore:ListUsers, DescribeUser, ListGroups, ListGroupMemberships
  sso:ListAccountAssignmentsForPrincipal, ListApplications,
  sso:ListApplicationAssignments, ListPermissionSets, DescribePermissionSet,
  sso:ListManagedPoliciesInPermissionSet,
  sso:ListCustomerManagedPolicyReferencesInPermissionSet,
  sso:GetInlinePolicyForPermissionSet,
  sso:GetPermissionsBoundaryForPermissionSet,
  sso:ListTagsForResource
  organizations:ListAccounts
  ```

### 3.2 目标账号 (TARGET)
- *新建一个独立 AWS 账号*（或者新 Organization）
- 在目标账号同 region（或选定新 region）启用 Identity Center
- 自定义 start URL: IAM Identity Center → Settings → *Access portal URL* → Customize
  - 取一个与源不重复的 subdomain，如 `acme-dr.awsapps.com/start`
- 获取 `TARGET_IDC_ARN` / `TARGET_IDC_ID`
- 恢复运行角色 IAM 权限：
  ```
  identitystore:ListUsers, ListGroups, ListGroupMemberships,
  identitystore:CreateUser, CreateGroup, CreateGroupMembership
  sso:ListPermissionSets, DescribePermissionSet, CreatePermissionSet,
  sso:UpdatePermissionSet, AttachManagedPolicyToPermissionSet,
  sso:DetachManagedPolicyFromPermissionSet,
  sso:AttachCustomerManagedPolicyReferenceToPermissionSet,
  sso:DetachCustomerManagedPolicyReferenceFromPermissionSet,
  sso:PutInlinePolicyToPermissionSet,
  sso:DeleteInlinePolicyFromPermissionSet,
  sso:PutPermissionsBoundaryToPermissionSet,
  sso:DeletePermissionsBoundaryFromPermissionSet,
  sso:ListTagsForResource, TagResource,
  sso:CreateAccountAssignment, ListAccountAssignments,
  sso:ProvisionPermissionSet,
  sso:CreateApplicationAssignment
  iam:CreateServiceLinkedRole, AttachRolePolicy, GetRole
  organizations:ListAccounts, DescribeAccount
  ```

### 3.3 本地环境
```bash
cd /home/ubuntu/tech/identity-center-backup
python3 -m venv .venv && source .venv/bin/activate
pip install boto3 backoff
```

---

## 4. 备份流程（SOURCE 账号）

```bash
cd /home/ubuntu/tech/identity-center-backup
source .venv/bin/activate

export AWS_PROFILE=source-prod          # 源账号
export AWS_DEFAULT_REGION=<SOURCE_REGION>
export SOURCE_IDC_ARN=arn:aws:sso:::instance/ssoins-xxxxxxxxxxxxxxxx
export SOURCE_IDC_ID=d-xxxxxxxxxx

mkdir -p backups/$(date +%F)
cd backups/$(date +%F)

# 4.1 Users / Groups / Memberships
python3 ../../scripts/backup_users_groups.py --idc-id $SOURCE_IDC_ID

# 4.2 Permission Sets 定义
python3 ../../scripts/backup_permission_sets.py --idc-arn $SOURCE_IDC_ARN

# 4.3 Account / Application Assignments (AWS 官方 sample)
python3 ../../upstream/mist/backup.py --idc-id $SOURCE_IDC_ID --idc-arn $SOURCE_IDC_ARN

ls -la
# Users.json  Groups.json  GroupMemberships.json
# PermissionSets.json
# UserAssignments.json  GroupAssignments.json  AppAssignments.json
```

*建议*：把上述脚本封装成一个 Lambda，EventBridge 定时（每天）跑，输出写到 S3 加版本控制 + 跨区域复制。

### 4.4 自动化示例（EventBridge + Lambda）
- Lambda container image 基于 `python:3.12-slim`，COPY 这三个脚本进去
- 环境变量注入 `IDC_ARN` / `IDC_ID` / `S3_BUCKET`
- 执行成功后 `aws s3 cp *.json s3://$S3_BUCKET/$(date +%F)/`
- S3 桶开启 versioning + CRR + Object Lock（合规）

---

## 5. 恢复流程（TARGET 账号）

### 5.1 准备
```bash
export AWS_PROFILE=target-dr            # 目标账号
export AWS_DEFAULT_REGION=<TARGET_REGION>
export TARGET_IDC_ARN=arn:aws:sso:::instance/ssoins-yyyyyyyyyyyyyyyy
export TARGET_IDC_ID=d-yyyyyyyyyy

cd /home/ubuntu/tech/identity-center-backup/backups/<YYYY-MM-DD>
```

### 5.2 Dry run（强烈建议先跑）

```bash
python3 ../../scripts/restore_users_groups.py --idc-id $TARGET_IDC_ID --dry-run
python3 ../../scripts/restore_permission_sets.py --idc-arn $TARGET_IDC_ARN --dry-run
```

### 5.3 Step 1 — 重建 Users / Groups / Memberships

```bash
python3 ../../scripts/restore_users_groups.py --idc-id $TARGET_IDC_ID
```

⚠️ 1000+ 用户恢复后：
- 每个用户会收到 AWS 发的 *invitation email*，要求设置密码
- 如果 SOURCE 用了 external IdP，这一步跳过，改为在 TARGET 重新连同样的 IdP
- API 限流风险：identitystore CreateUser ≈ 10 TPS，1000 用户约 2 分钟，脚本默认无显式 sleep（boto3 retry 够用），若遇 throttle 降级到 5 TPS

### 5.4 Step 2 — 重建 Permission Sets

```bash
python3 ../../scripts/restore_permission_sets.py --idc-arn $TARGET_IDC_ARN
```

⚠️ 注意点：
- *Customer managed policies*：脚本只是 *引用* 同名 policy，前提是同名 IAM policy 必须已经存在于所有被 assign 的成员账号里。如果没有，assignment 阶段会失败。先跑 IaC 把这些 policy 在 TARGET Org 所有成员账号铺一遍。
- *Permissions boundary*：同理。

### 5.5 Step 3 — 恢复 Account Assignments（用官方 mist/restore.py）

```bash
python3 ../../upstream/mist/restore.py --idc-id $TARGET_IDC_ID --idc-arn $TARGET_IDC_ARN
```

这一步会：
- 按 `UserName` / `DisplayName` 在 TARGET 里查出新的 UserId / GroupId
- 按 Permission Set Name 在 TARGET 里查出新的 PS ARN
- 创建 account assignments（UserName→Account→PS、Group→Account→PS）
- 创建 application assignments

---

## 6. 验证

### 6.1 计数核对
```bash
# 用户数、组数、成员关系数、permission sets 数、assignments 数都要对上
python3 <<'PY'
import json
for f in ["Users","Groups","GroupMemberships","PermissionSets","UserAssignments","GroupAssignments"]:
    try:
        d = json.load(open(f+".json"))
        k = list(d.keys())[0]
        print(f"{f:20s} {len(d[k])}")
    except Exception as e:
        print(f"{f}: {e}")
PY
```
在 TARGET 账号用同样脚本（`backup_*`）再抓一份，对比计数。

### 6.2 抽样登录测试
- 挑 3 个普通用户 + 1 个 admin，用新 start URL 登录
- 切换到目标账号某个应有权限的 AWS account，验证能 assume、能看资源
- 验证 Kiro 侧看到的用户列表（Kiro 用 SCIM 对接时需要在 Kiro 控制台重绑定 TARGET 实例）

### 6.3 Kiro 侧重新绑定
- Kiro subscription → Identity provider → 切换到 TARGET Identity Center
- 如果 Kiro 按 UserId/GroupId 缓存授权，这里可能需要 Kiro 侧做 re-enroll；*这是客户和 Kiro 团队要对齐的点*，本 runbook 之外

---

## 7. 切换 / 回滚

### 7.1 日常（两边都活）
- SOURCE 为主，TARGET 仅备份 + 每日对账
- 所有用户只知道 SOURCE 的 start URL
- TARGET 的 start URL 不对外发布（或发给少量 SRE 做演练）

### 7.2 故障切换
1. 宣布 SOURCE 不可用
2. 最后一次跑 `backup_*` + `restore_*`（如果 SOURCE 还能读）
3. 通知用户切到 TARGET start URL
4. Kiro 侧切换到 TARGET 的 identity source

### 7.3 回滚
- 如果 TARGET 部署异常（例如 permission set 丢字段），回滚方式：
  - TARGET 的 Identity Center 启用后可以 *Delete* 整个实例重来（⚠️ 清空 users/groups），再跑一次 restore
  - 或者只删有问题的 permission sets，重跑 `restore_permission_sets.py`

---

## 8. 已知局限 / 风险

| 项 | 说明 | 缓解 |
|----|------|------|
| 密码不可迁移 | Identity Center 自建目录无导出密码的 API | 用户邮件重设；或强烈建议改用 external IdP |
| MFA 设备不可迁移 | 同上 | 用户重新注册 MFA |
| Customer managed policies 要预铺 | restore_permission_sets 只引用名字 | 提前用 IaC 在 TARGET Org 所有成员账号部署同名 policy |
| Applications 复杂对接 | SCIM/SAML 第三方应用（如 Kiro 本身）要重接 | 对每个 application 单独梳理 |
| UserId/GroupId 变化 | TARGET 重建后 ID 全新 | 依赖 UserName / DisplayName 做关联，所以*严禁改名* |
| Cross-Org account assignments | 如果 TARGET 是新 Org、account IDs 不同 | Assignments 的 TargetId (AWS account id) 要做映射，当前脚本假设 account id 一一对应 |
| 限流 | 1000 用户接近 TPS 上限 | 脚本已 boto3 默认 retry；必要时加 sleep |

---

## 9. 下一步 (TODO)

- [ ] 实测一遍端到端（需要一对测试账号）
- [ ] 把 backup 脚本打包成 Lambda + EventBridge CDK stack（放 `infra/`）
- [ ] 写 `account_id_map.json` 支持，让 assignments 跨 Org 时 TargetId 做映射
- [ ] 加 `compare.py`：对比两边状态，输出 diff 报告
- [ ] 集成 `aws-iam-identity-center-extensions` 的 Region-Switch 方案做备选路线评估

---

## 附录：参考资料

- AWS 博客：<https://aws.amazon.com/blogs/security/managing-identity-source-transition-for-aws-iam-identity-center/>
- aws-samples/manage-identity-source-transition-for-aws-iam-identity-center — `upstream/mist/`
- aws-samples/aws-iam-identity-center-extensions — `upstream/ic-extensions/`（Region-Switch 模块最完整）
- aws-samples/single-stage-aws-iam-identity-center-pipeline — IaC pipeline 方案
- hellerda/aws-sso-admin-tools — 一些有用的 CLI 操作参考
