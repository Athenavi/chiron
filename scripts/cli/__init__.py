import typer

app = typer.Typer(
    name="Chiron-CLI",
    help="命令行管理工具",
    add_completion=False,
    rich_markup_mode="rich",
)

# 注册子命令
from .commands import migrate, shell  # noqa: E402 - 必须在 app 构造之后注册子命令

app.add_typer(migrate.app, name="migrate", help="数据库迁移（只读；写入请用 alembic）")
app.add_typer(shell.app, name="shell", help="交互式 Shell")


def main():
    app()


if __name__ == "__main__":
    main()
