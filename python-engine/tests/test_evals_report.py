"""评测报告的**脱敏**（`evals/report.py`）。

报告是**会上传的制品**（CI artifact / 贴进 issue），而失败明细里有两类自由文本：
① 断言 `detail`（带固件文本）② 异常 `error`（可能带 URL / 密钥）。
`vendor/规划.md` §5.1 把"报告脱敏"写成安全边界的一条 ⇒ 这里把它钉住。

两条纪律：
* **复用引擎那份规则集**（`app/subagent/redact.py`），**不新造第二份清单**（两份必然漂移）；
* 评测套件本身**不 import 引擎**（runner 走 HTTP 真实路径，见 `vendor/规划.md` §6），
  所以脱敏是**惰性 import**，引擎不在场时跳过并留痕（fail-soft，但必须可见）。
"""
from __future__ import annotations

from evals.assertions import Check, Evaluation, Observation
from evals.report import summarize
from evals.runner import TaskResult


def _result(*, detail: str = "", error: str = "") -> TaskResult:
    check = Check(kind="final_text_contains", ok=False, detail=detail)
    return TaskResult(
        task_id="t1",
        category="smoke",
        passed=False,
        evaluation=Evaluation(passed=False, success_checks=[check], efficiency_notes=[]),
        observation=Observation(),
        attempt=1,
        error=error,
    )


def test_failure_detail_is_redacted():
    """断言 detail 里的密钥形态不得原样进报告。"""
    summary = summarize("t1", "smoke", [_result(detail="期望含 password=hunter2secret")])

    joined = " ".join(summary.failures)
    assert "hunter2secret" not in joined, joined
    assert "[REDACTED" in joined, joined


def test_error_text_is_redacted():
    """异常文本同样要脱敏 —— 它是最容易夹带 URL / 密钥的那一类。"""
    summary = summarize(
        "t1", "smoke", [_result(error="RuntimeError: openai_key=sk-abcdefghijklmnopqrstuvwxyz")]
    )

    joined = " ".join(summary.failures)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in joined, joined
    assert "[REDACTED" in joined, joined


def test_ordinary_failure_text_is_not_over_redacted():
    """脱敏只认**明确的密钥形态**，不能把正常断言文本也打码（否则报告失去可读性）。"""
    summary = summarize("t1", "smoke", [_result(detail="最终回答缺少「排序」二字")])

    joined = " ".join(summary.failures)
    assert "排序" in joined
    assert "[REDACTED" not in joined
