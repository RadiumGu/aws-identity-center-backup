# Identity Center Backup & DR Runbook

> 场景：一个 AWS 账号下有 1000+ Kiro 订阅用户通过 Identity Center 登录，客户担心账号级故障，需要把 Identity Center 里的 users / groups / memberships / permission sets / account assignments 整套复制到另一个备用账号，备用账号使用新的 start URL，保证名字不重复、故障时能顶上。

本 runbook 覆盖：
1. 前置评估（决定走哪条路径）
2. 基线备份（每天/每周自动跑，出数据）
3. 新账号初始化（Identity Center 启用 + IAM 委托）
4. 数据恢复（users → groups → memberships → permission sets → assignments）
5. 验证 + 切换 + 回滚

---

## 0. 覆盖边界（必读）

本方案是分层 DR：

- **L1 — IdC 登录能力 DR**（本 runbook + `scripts/backup_*` / `restore_*` + `upstream/mist`）
- **L2 — Kiro 订阅 DR**（`scripts/*_kiro_subscriptions.py` + `docs/KIRO-CUTOVER.md`）
- **L3 — Kiro 侧用户历史数据**（conversation / profile / CodeWhisperer tagging）：**自建方案做不到，依赖 Kiro 产品路线图**

⚠️ **不要把 L1 当成完整 DR 方案交付**。
切换当天如果只跑 L1：用户能登 AWS 但 Kiro 里没订阅、没历史数据。这不是「无缝切换」。

**真实 RTO 下限 > 1 工作日**：
- target Kiro 订阅开通（含企业支持审核）
- 1000 用户 MFA 重注册
- CMP 跨账号预铺
- Kiro application 重建（Kiro console 自动）

### 0.1 架构前置建议（和客户先谈这个）

Identity Center 自身已经是 region 级高可用（底层跨 AZ），所谓「账号级故障」的真实触发场景：

| 场景 | delegated admin 能救？ | external IdP 能救？ |
|------|----------------------|---------------------|
| Org 管理账号被误删 / suspend | ⚠️ 不能（IdC 实例仍在管理账号下，跟着挂） | ✅ 能（换新 Org + 接同一 IdP） |
| 管理账号 root 被锁 | ✅ 能（成员账号仍可操作） | ✅ 能 |
| 合规冻结 | 取决于冻结范围 | 取决于冻结范围 |

真正有效的架构改造路径（强推，**给客户先谈这个，再谈备份**）：

1. **Identity Center 身份源改用 external IdP**（Okta / Entra / Google）—— 这是**跨 Org DR 唯一真正优雅的路径**。IdC 降级成 SAML consumer，target 连接同一 IdP 后 SCIM 自动同步用户/组
2. **启用 Organizations delegated administrator**：把 Identity Center 的日常管理委托给专用 identity account。⚠️ 这**只降低日常误操作暴露面**，不解决管理账号本身被删/suspend 的场景
3. **Permission Sets + Assignments 全部 IaC 化**（参考 `upstream/ic-extensions/` 或 `aws-samples/single-stage-aws-iam-identity-center-pipeline`，CI 驱动）
4. **Kiro 订阅 claim 定期备份到 S3**（跑 `scripts/backup_kiro_subscriptions.py` + EventBridge）

如果客户同意 1+2+3+4，就不需要「账号级克隆 Identity Center」；灾难时把 IdP 指向新 identity account、跑一次 L2 Kiro 订阅恢复即可。

以下流程针对 **客户坚持要克隆一套 Identity Center + Kiro 订阅到新账号** 的场景。

---

## 0.2 决策树：先判断身份源

Identity Center 支持三种 identity source，*灾备方案完全不同*：

| 身份源 | Users/Groups 恢复方式 | 用不用本 repo 的脚本 |
|--------|-----------------------|----------------------|
| *External IdP (Okta / Entra / Google)* | 把同一个 IdP 再连一次到新实例，SCIM 自动同步 | ❌ 不需要 `backup_users_groups.py`，只需要 permission sets + assignments |
| *Active Directory (AD Connector)* | 新账号同样连到 AD | ❌ 同上 |
| *Identity Center 自建目录* | 必须脚本导出 + 重建 | ✅ 全套脚本都要用 |

> 🔑 *行动项*：找客户确认身份源。AWS 控制台 → IAM Identity Center → Settings → "Identity source" 一栏。

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
pip install -r requirements.txt          # boto3 / botocore / backoff
```

> 不推荐 AWS Cloud9（已于 2024 对新客户停售）。用 *AWS CloudShell* / EC2 / 本地工作站均可。

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

## 6.X Kiro 订阅（Kiro Power / Q Developer Pro 席位）

> 关键点：*Kiro 席位分配没有公开 API*。备份 → 购买 → 手动分配 三步走。

Kiro 控制台里 "Users" / "Groups" tab 展示的订阅状态走的是一个私有内部服务 *`user-subscriptions`*（代号 *Zorn*），不在 AWS CLI / boto3 / CloudFormation 里。

### 6.X.1 SOURCE 备份

```bash
python3 ../../scripts/backup_kiro_subscriptions.py \
    --idc-arn $SRC_IDC_ARN \
    --region $SRC_REGION
# 输出 KiroSubscriptions.json + 按 plan 分类的席位总数
IAM 权限：调用方身份需要能读 Kiro 控制台（实际权限检查在私有 API，用的角色通常是 AdminstratorAccess 或同等权限）。
```

### 6.X.2 生成 TARGET 端手动执行清单

```bash
python3 ../../scripts/kiro_restore_checklist.py \
    --input KiroSubscriptions.json \
    > kiro-restore-checklist.md
```

这份 Markdown 清单包含：
- *Step 1* 按 plan 类型（`KIRO_ENTERPRISE_PRO` / `KIRO_ENTERPRISE` / `Q_DEVELOPER_*` 等）的席位数量
- *Step 2* 每种 plan 对应的 users + groups 列表，直接对照 Kiro 控制台 "Add user / Add group" 操作

### 6.X.3 TARGET 手动执行

0. **就绪性检查**（建议在手动操作前跑）：
   ```bash
   export AWS_PROFILE=<target> AWS_DEFAULT_REGION=<region>
   python3 ../../scripts/check_kiro_target_readiness.py \
       --idc-arn $DST_IDC_ARN --idc-id $DST_IDC_ID --region <region> \
       --source-snapshot KiroSubscriptions.json
   # 检查：Target Kiro 已启用 / users+groups 名字能对上 / 席位缺口数量
   ```
1. **购买席位**：Amazon Q Developer / Kiro 控制台 → *Subscriptions* → 按清单 Step 1 的数字购买。仅控制台（或 Marketplace）可操作，无 API。
2. **分配 users / groups**：Kiro → *Users & Groups* → *Add user* / *Add group*，按 Step 2 名单添加。
   - 因为 users/groups 前面已经由 `restore_users_groups.py` 或 SCIM 在 target Identity Center 里建好了，名字对得上。
3. **验证**：再跑一遍 `backup_kiro_subscriptions.py` 打到 target，和 SOURCE 快照按 plan 计数对比。

### 6.X.4 已知限制 / 风险

- `user-subscriptions` 是 *私有 / 未文档化 API*，AWS 随时可能变更或下线。备份脚本随诗可能突然不能用 — 出问题时做两件事：在 Kiro 控制台手动导出用户列表作为后备；或者指望 AWS 转公共。
- `q:CreateAssignment` / `UpdateAssignment` 存在但从外部 SigV4 调用会返回 500（黑盒了解结果），*无法自动化批量分配*，只能控制台/点击。
- 席位购买非免费 — 购买前确认账号账单/额度。
- PENDING 状态的用户（未首次登录）迁移后仍需用户自己在 TARGET 侧完成首登。

### 6.X.5 实验性：尝试自动化 assignment

`scripts/try_kiro_create_assignment.py` 会尝试 4 种可能的私有 Coral 协议变种（q 路径 / q X-Amz-Target / user-subscriptions CreateUserSubscription / user-subscriptions CreateClaim），把请求和响应写到日志。授权闭合之前返回 500，失败就继续走 6.X.3 的手动流程。取论文化之后可直接批量调用。

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
| *Target 已用 external IdP* | SCIM 会在后台写 users/groups，和 restore 脚本冲突 | 不要跑 `restore_users_groups.py`，只跑 permission-sets + assignments |
| 现有 assignment 重提交 permission set | `update_permission_set` 后 AWS 建议 `provision_permission_set` | 首次迁移 target 无活动 assignment 不涉及；延续更新时需 `sso-admin provision-permission-set --target-type ALL_PROVISIONED_ACCOUNTS` |
| Trusted Token Issuers / Applications 本身配置 | `upstream/mist` 只备份 *已存在 application* 的 assignments，不备份 application 本身与 TTI 配置 | target 侧先重建 applications（SAML/OAuth/SCIM endpoint）再跑 `mist/restore.py` |
| upstream mist `except:` bare 异常 | 诊断不友好 | 出问题时直接看 boto3 调用参数 / IAM 权限，不依赖他的错误文案 |
| AWS Cloud9 已 EOL (2024) | mist README 仍建议用 Cloud9 | 用 *CloudShell* / EC2 / 本地工作站 代替 |

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
