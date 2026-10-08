"""数据库迁移 —— **只读**。

决策（[docs/db-migration-entry.md](../../../docs/db-migration-entry.md)）：**Alembic 是唯一迁移入口**，
CLI 只做只读诊断，**不写 schema**。升级 / 回滚属于**发布流程步骤**，请直接执行：

```bash
alembic upgrade head     # 全新库
alembic stamp head       # 已有表结构、只缺 alembic_version 记录的库
```

本文件此前的 `run`（`command.revision(autogenerate=True)` + `upgrade`）与 `downgrade` 已删除：
autogenerate 与「DDL 全部收敛到唯一权威基线迁移」的既定架构**直接冲突**（它会把"当前库长什么样"
反向写进迁移文件），而且会让 CLI 变成**第二入口** —— 两套入口必然版本漂移（见决策记录 §2）。
"""
from __future__ import annotations

import typer

app = typer.Typer(help="数据库迁移（只读；写入请用 alembic）")


@app.command("history")
def migration_history() -> None:
    """查看迁移历史（只读）"""
    typer.echo("迁移历史:")
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError:
        # 应用镜像刻意不装 Python/alembic；迁移环境才装（requirements-migrate.txt）。
        # 失败要**显式**：旧实现只 echo 一句就返回 0，脚本里会被当成成功。
        typer.echo(
            "❌ 未安装 alembic：python -m pip install -r requirements-migrate.txt",
            err=True,
        )
        raise typer.Exit(code=1) from None
    try:
        command.history(Config("alembic.ini"))
    except Exception as e:  # noqa: BLE001 - 只读命令：失败必须非 0 退出，不静默
        typer.echo(f"❌ 获取失败: {e}", err=True)
        raise typer.Exit(code=1) from e
