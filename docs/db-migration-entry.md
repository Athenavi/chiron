# 决策记录：数据库迁移入口（L4-2）

> 决策日期：2026-09-28
> 决策：**CLI 完全不接触迁移；Alembic 是唯一迁移入口。**
> 状态：**三处入口口径已统一**（Go CLI、Python CLI、初始化向导，见 §3/§4）。

## 1. 背景

排查发现项目里存在**三处**会执行迁移的入口，口径不一：

| # | 入口 | 行为 | 与「Alembic 唯一入口」的关系 |
|---|---|---|---|
| 1 | `chiron-cli db migrate`（Go，`cmd/chiron-cli/db.go` → `internal/db/migrate.go`） | shell 出 `python -m alembic upgrade head`，并把 `APP_SECRET` 写入 `.env` | **冲突**：CLI 成了第二入口，且要求目标机有 python + alembic |
| 2 | `chiron migrate`（Python，`scripts/cli/commands/migrate.py`） | `revision`（**autogenerate**）/ `upgrade` / `history` / `downgrade` | **冲突更重**：autogenerate 直接违反「不再 autogenerate、DDL 全部收敛到显式提交的迁移文件」 |
| 3 | `scripts/init.py`（初始化向导） | 第 6 步自动跑 `alembic upgrade head` | 未定：属于部署向导，不是命令行迁移工具 |

第 1 处还带来两个副作用：`.env` 的 `APP_SECRET` 写入只存在于这条路径（grep 确认无第二处），以及迁移文件格式检测 `hasInternalMigrationFiles` 是**死代码**（无调用者）。

## 2. 决策与理由

**CLI 完全不接触迁移。** 理由：

- 迁移是**发布流程步骤**（发布前在有 Python + alembic 的环境执行），不是运行时命令；
- 两套入口必然版本漂移：CLI 里 shell 的 alembic 与运维手动执行的可能是不同版本/不同 ini；
- 应用镜像**刻意不装 Python**（见 `requirements-migrate.txt` 的说明），所以 CLI 迁移在最需要它的生产环境里恰恰不可用，只会给出"代码已升级、迁移未跑"的假象。

## 3. 本次改动（Go CLI 侧）

- 删除 `chiron-cli db migrate` 子命令与 `runDBMigrate`（含 `--dry-run/--sql` 分支）；
- 删除随之失去全部调用者的 `internal/db/migrate.go`（`RunMigrations` + `alembicConfigPath`/`dotEnvPath`/`resolvePythonBinary`）与 `internal/db/migrate_test.go`（只测这几个辅助函数）；
- 删除死代码 `hasInternalMigrationFiles`；
- **保留** `chiron-cli db status`（只读）与 `internal/db/schema_version.go`（网关启动时的 schema 校验，`ParseMigrationHead`/`CheckSchemaVersion`），后者不写任何 schema；
- `db` 子命令组的帮助文本改写为「只读诊断 + 明确指向 Alembic」，并写清两条标准命令。

## 4. 另外两处入口（已决定，2026-10-08）

1. **`scripts/cli/commands/migrate.py` —— 删除全部写操作**。`run`（内含 `command.revision(autogenerate=True)` 与 `upgrade`）与 `downgrade` 已移除，只留只读的 `history`；并且失败改为**非 0 退出**（旧实现只 `echo` 一句就返回 0，脚本里会被当成成功）。autogenerate 与「DDL 全部收敛到唯一权威基线迁移」**直接冲突** —— 它把"当前库长什么样"反向写进迁移文件，必然与显式提交的 DDL 漂移。
2. **`scripts/init.py` 第 6 步 —— 保留**。它执行的是 `python -m alembic -c alembic.ini upgrade head`：**同一份配置、同一个工具**，只是由部署向导代跑一次发布流程步骤。

判据（§2 的理由在这里落地）：**"谁在跑"不构成第二入口，"用另一套实现 / 另一份配置跑"才是。** Go CLI 的问题正是后者 —— 它 shell 出 `python -m alembic`（目标机未必有 python）并顺手写 `.env`；而初始化向导跑的是仓库里那一份 `alembic.ini`，不会与运维手动执行产生版本漂移。

## 5. 运维指引（决策后的标准流程）

```bash
# 全新库
alembic upgrade head

# 已存在表结构、只缺 alembic_version 记录的库
alembic stamp head
```

需要 `python` + `pip install -r requirements-migrate.txt`，并在仓库根目录执行（`alembic.ini` 在此）。查看当前 revision：`chiron-cli db status`（只读）。
