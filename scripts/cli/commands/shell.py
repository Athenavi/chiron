"""
交互式 Shell 命令（类似 django-admin shell）
"""
import code

import typer

app = typer.Typer(help="交互式 Shell")


@app.command("ipython")
def ipython_shell():
    """启动 IPython shell（如果可用）"""
    try:
        import IPython
        IPython.start_ipython(argv=[])
    except ImportError:
        typer.echo("IPython 未安装，使用标准 Python shell")
        standard_shell()


@app.command("python")
def standard_shell():
    """启动标准 Python 交互式 shell，预加载应用上下文"""
    typer.echo("加载 FastBlog 应用上下文...")
    namespace = {}
    try:
        from src.app import create_app
        from src.setting import ProductionConfig
        app = create_app(ProductionConfig())
        namespace.update({
            "app": app,
            "db": app.extensions.get("sqlalchemy"),
        })
        typer.echo("  已导入: app, db")
    except Exception as e:
        typer.echo(f"  警告: 应用加载失败: {e}")

    typer.echo("启动 Python shell...")
    code.interact(local=namespace, banner="")


@app.callback(invoke_without_command=True)
def shell_default():
    """默认启动标准 shell"""
    standard_shell()
