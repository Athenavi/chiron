"""记忆层向量后端的选路与故障可见性（M13）。

对应缺陷「L3 摘要向量没有数据来源」（剖析文档不在仓库里）：过去这两个 store 硬编 Milvus，而 Milvus
默认不启动 ⇒ 语义召回恒空却自称 semantic。这里锁两条规矩：配置未知退回 PG、
后端不可达只在第一次大声。
"""

from __future__ import annotations

import logging

import pytest

from app.config import settings
from app.memory import vector_backend as vb

# ── resolve ───────────────────────────────────────────────────────────


class TestResolve:
    @pytest.mark.parametrize(
        "raw",
        ["pg", "pgvector", "postgres", "postgresql", "PG", "  pg  ", ""],
    )
    def test_pg_aliases_and_empty(self, raw):
        assert vb.resolve(raw) == vb.PG

    @pytest.mark.parametrize("raw", ["milvus", "MILVUS", " milvus "])
    def test_milvus(self, raw):
        assert vb.resolve(raw) == vb.MILVUS

    @pytest.mark.parametrize("raw", ["qdrant", "weaviate", "pinecone", "milvus2"])
    def test_unknown_falls_back_to_pg(self, raw):
        """未知值退回 PG：退回"要额外服务才可用"的那个会把故障变成静默为空。"""
        assert vb.resolve(raw) == vb.PG

    def test_reads_settings_when_raw_omitted(self, monkeypatch):
        monkeypatch.setattr(settings, "memory_vector_backend", "milvus")
        assert vb.resolve() == vb.MILVUS

        monkeypatch.setattr(settings, "memory_vector_backend", "pg")
        assert vb.resolve() == vb.PG


class TestWarnOnce:
    @pytest.fixture(autouse=True)
    def _clear_dedupe_state(self):
        """`_warned` 是模块级状态，用例之间必须隔离，否则"提醒一次"看的是上一个人的账。"""
        vb._warned.clear()
        yield
        vb._warned.clear()

    def test_unknown_value_warns_only_once(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.memory.vector_backend"):
            vb.resolve("qdrant")
            vb.resolve("qdrant")
            vb.resolve("qdrant")

        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_pg_never_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.memory.vector_backend"):
            for raw in ("pg", "pgvector", "postgres", "postgresql", "milvus", ""):
                vb.resolve(raw)

        assert caplog.records == []


# ── parse_vector ──────────────────────────────────────────────────────


class TestParseVector:
    def test_none_means_no_vector(self):
        assert vb.parse_vector(None) is None

    def test_empty_list_means_no_vector(self):
        """空数组与 NULL 同口径：都该让读侧退回"取最近"，而不是算出一个 0 分。"""
        assert vb.parse_vector([]) is None

    def test_list_passthrough(self):
        assert vb.parse_vector([1.5, -2.0]) == [1.5, -2.0]

    def test_tuple_normalized_to_list(self):
        assert vb.parse_vector((0.1, 0.2)) == [0.1, 0.2]

    def test_json_string_decoded(self):
        """替身/旧数据可能给文本形态；生产里 asyncpg 原生给 list，这条路只是兼容。"""
        assert vb.parse_vector("[1, 2, 3]") == [1, 2, 3]

    @pytest.mark.parametrize("raw", ["not json", "", "[1,2", "null"])
    def test_text_without_a_vector_reads_as_no_vector(self, raw):
        """解不出数组、以及解出 `null` —— 都该让读侧退回"取最近"，不是抛或给 0 分。"""
        assert vb.parse_vector(raw) is None


# ── FailureLog ────────────────────────────────────────────────────────


class TestFailureLog:
    """连续失败只大声一次 —— 每请求刷 warning 会把日志冲走，静默 swallow 是原来的病。"""

    def test_first_failure_is_error_then_debug(self, caplog):
        log = vb.FailureLog("Milvus 摘要检索")
        with caplog.at_level(logging.DEBUG, logger="app.memory.vector_backend"):
            log.failure("connect: refused")
            log.failure("connect: refused")
            log.failure("connect: refused")

        levels = [r.levelno for r in caplog.records]
        assert levels == [logging.ERROR, logging.DEBUG, logging.DEBUG]

    def test_success_rearms(self, caplog):
        log = vb.FailureLog("Milvus 摘要写入")
        with caplog.at_level(logging.DEBUG, logger="app.memory.vector_backend"):
            log.failure("insert: boom")
            log.success()
            log.failure("insert: boom again")

        levels = [r.levelno for r in caplog.records]
        assert levels == [logging.ERROR, logging.ERROR]

    def test_success_without_failure_is_silent(self, caplog):
        log = vb.FailureLog("Milvus 摘要写入")
        with caplog.at_level(logging.DEBUG, logger="app.memory.vector_backend"):
            log.success()

        assert caplog.records == []
