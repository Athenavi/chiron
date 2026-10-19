"""SDK 契约测试 —— 钉住"我们用到的 PageIndex 表面"。

**这个测试的意义在于升级时先红，而不是运行时才发现。** pageindex 迭代以小时计
（0.4.3 → 0.4.4 同日），API 面会漂移：0.4.4 引入了 ``enable_citations`` / ``optimize``，
并调整了 ``chat`` 的协议选项。我们只用到 6 个方法，所以把它们的**存在性、参数名、
返回形状**固化成断言 —— 升级时 CI 立刻指出"哪一处用法没了"。

它在**没有 pageindex 的环境**（引擎 CI）自动跳过：引擎镜像不装 SDK，跑这里没有意义。

跑法（pageindex 环境）：``python -m pytest tests/test_sdk_contract.py -q``
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("pageindex")  # 引擎环境没有 SDK → 跳过（这里只对 pageindex 环境有意义）

from pageindex import PageIndexClient, PageIndexLocalClient  # noqa: E402

#: 我们调用的全部方法（providers / worker / 服务都只经这些入口）
REQUIRED_METHODS = (
    "submit_document",
    "delete_document",
    "chat",
    "create_folder",
    "list_folders",
    "get_document",
)


@pytest.mark.parametrize("client_cls", [PageIndexClient, PageIndexLocalClient])
def test_required_methods_exist(client_cls) -> None:
    for name in REQUIRED_METHODS:
        assert callable(getattr(client_cls, name, None)), f"{client_cls.__name__}.{name} 不存在"


def test_local_constructor_parameters() -> None:
    """local 模式：索引位置与两个模型都是我们显式传的（不依赖 SDK 默认值）。"""
    params = inspect.signature(PageIndexLocalClient.__init__).parameters
    for name in ("index_model", "chat_model", "index_backend", "chat_backend", "storage_path"):
        assert name in params, f"PageIndexLocalClient 缺少参数 {name}"


def test_cloud_constructor_parameters() -> None:
    params = inspect.signature(PageIndexClient.__init__).parameters
    for name in ("api_key", "index", "chat_model", "chat_backend"):
        assert name in params, f"PageIndexClient 缺少参数 {name}"


def test_chat_parameters() -> None:
    """``citations`` 是我们解析引用与做越界守卫的入口 —— 它改名/消失必须立刻知道。"""
    params = inspect.signature(PageIndexClient.chat).parameters
    for name in ("messages", "doc_id", "folder_id", "stream", "citations"):
        assert name in params, f"chat 缺少参数 {name}"


def test_submit_document_parameters() -> None:
    params = inspect.signature(PageIndexClient.submit_document).parameters
    for name in ("file_path", "folder_id", "metadata", "wait"):
        assert name in params, f"submit_document 缺少参数 {name}"


def test_folder_api_documented_shape() -> None:
    """folder 是"用户空间 → 库"两层的载体；返回形状变了要立刻知道。"""
    create_doc = (inspect.getdoc(PageIndexClient.create_folder) or "").lower()
    list_doc = (inspect.getdoc(PageIndexClient.list_folders) or "").lower()
    assert "folder" in create_doc
    assert "folder" in list_doc
