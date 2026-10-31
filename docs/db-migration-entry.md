# 数据库迁移入口

> 决策：**CLI 完全不接触迁移；Alembic 是唯一迁移入口。**

## 1. 规则

| 入口 | 行为 | 结论 |
|---|---|---|
| `chiron-cli db status`（Go） | 只读，读 `alembic_version` | **保留** |
| 网关启动校验 `internal/db/schema_version.go` | 只读校验（`ParseMigrationHead` / `CheckSchemaVersion`），不写 schema | **保留** |
| `scripts/cli/commands/migrate.py` | 只读 `history`；失败非 0 退出 | **保留（只读）** |
| `scripts/init.py` 第 6 步 | 部署向导代跑 `python -m alembic -c alembic.ini upgrade head` | **保留** |

理由：

- 迁移是**发布流程步骤**（发布前在有 Python + alembic 的环境执行），不是运行时命令；
- 两套入口必然版本漂移：CLI 里 shell 的 alembic 与运维手动执行的可能是不同版本/不同 ini；
- 应用镜像**刻意不装 Python**（见 `requirements-migrate.txt` 的说明），所以 CLI 迁移在最需要它的
  生产环境里恰恰不可用，只会给出"代码已升级、迁移未跑"的假象。

判据：**"谁在跑"不构成第二入口，"用另一套实现 / 另一份配置跑"才是。** Go CLI 曾 shell 出
`python -m alembic`（目标机未必有 python）并顺手写 `.env`，因此被删除；初始化向导跑的是仓库里那一份
`alembic.ini`，不会与运维手动执行产生版本漂移。

## 2. 禁止项

- **不新增**任何会执行 `upgrade` / `downgrade` / `revision` 的 CLI 子命令；
- **不再 autogenerate**：DDL 全部收敛到显式提交的迁移文件（`0001_authoritative_baseline` 及其后续
  revision）。autogenerate 把"当前库长什么样"反向写进迁移文件，必然与显式提交的 DDL 漂移。

## 3. 运维指引（标准流程）

```bash
# 全新库
alembic upgrade head

# 已存在表结构、只缺 alembic_version 记录的库
alembic stamp head
```

需要 `python` + `pip install -r requirements-migrate.txt`，并在仓库根目录执行（`alembic.ini` 在此）。
查看当前 revision：`chiron-cli db status`（只读）。
