# identity-center-backup

AWS IAM Identity Center 账号级备份与灾备（DR）工具集 + Runbook。

> 同时是一个 *Agent Skill*：Kiro / Claude Code / OpenClaw 可直接加载 `SKILL.md` 驱动全流程。
> 安装：`git clone https://github.com/RadiumGu/aws-identity-center-backup.git ~/.kiro/skills/identity-center-backup`
> （Claude Code: `~/.claude/skills/`；OpenClaw: `~/.openclaw/skills/` 或项目 `skills/`）

## 场景

客户有 1000+ Kiro 订阅用户通过 Identity Center 登录主账号，需要在另一个
备用账号克隆一套 Identity Center（新 start URL、同样的 users/groups/permission
sets/assignments），主账号故障时可无缝切换。

## 内容

| 路径 | 作用 |
|------|------|
| `docs/RUNBOOK.md` | *端到端操作手册*（从前置评估 → 备份 → 新账号初始化 → 恢复 → 验证 → 切换） |
| `scripts/backup_users_groups.py` | 导出 users / groups / memberships（Identity Center 自建目录场景下必需） |
| `scripts/restore_users_groups.py` | 在目标账号重建 users / groups / memberships |
| `scripts/backup_permission_sets.py` | 导出 permission sets 完整定义（managed + customer + inline + boundary + tags） |
| `scripts/restore_permission_sets.py` | 在目标账号重建 permission sets |
| `upstream/mist/` | AWS 官方 sample — account + application *assignments* 的 backup/restore（直接复用） |
| `upstream/ic-extensions/` | AWS 官方更完整的 CDK 方案（Region-Switch，参考用） |

## 分工策略

AWS 官方 `manage-identity-source-transition` sample（mist）只覆盖 *assignments*，
不覆盖 users/groups/permission-sets 本身。本项目用自写脚本补齐这一段，
和 mist 的 `backup.py`/`restore.py` 组合使用，形成完整链路：

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

## 快速开始

参考 [`docs/RUNBOOK.md`](docs/RUNBOOK.md)。核心三步（SOURCE → TARGET）：

```bash
# 1. 源账号备份
export AWS_PROFILE=source && export AWS_DEFAULT_REGION=ap-northeast-1
python3 scripts/backup_users_groups.py     --idc-id $SRC_IDC_ID
python3 scripts/backup_permission_sets.py  --idc-arn $SRC_IDC_ARN
python3 upstream/mist/backup.py            --idc-id $SRC_IDC_ID --idc-arn $SRC_IDC_ARN

# 2. 目标账号恢复（建议先 --dry-run）
export AWS_PROFILE=target && export AWS_DEFAULT_REGION=ap-northeast-1
python3 scripts/restore_users_groups.py    --idc-id $DST_IDC_ID
python3 scripts/restore_permission_sets.py --idc-arn $DST_IDC_ARN
python3 upstream/mist/restore.py           --idc-id $DST_IDC_ID --idc-arn $DST_IDC_ARN
```

## 重要前提

- *如果身份源是 external IdP（Okta/Entra/Google）*：不需要备份 users/groups，
  直接把同一个 IdP 再接到目标 Identity Center，SCIM 会自动同步。只需做
  permission sets + assignments 的 DR。
- *UserName / Group DisplayName 不能改*：脚本依赖这两个字段做跨实例关联。
- *Customer managed policies 必须在目标 Org 各成员账号已存在同名 policy*，
  否则 assignment 阶段失败。
- *密码和 MFA 不可迁移*：用户在新实例首次登录走邀请邮件 → 重设密码 + 重注册 MFA。

完整风险清单见 `docs/RUNBOOK.md` §8。
