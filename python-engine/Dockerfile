# Multi-stage build — Python AI Engine v0.1.260825.01
FROM python:3.14-slim AS base

WORKDIR /app

# 系统依赖（ffmpeg：video_analyze 抽帧需要；**不引 Python 依赖**，见 app/tools/video.py）
# 先在基础镜像上应用 Debian 安全更新：Trivy 按镜像里的**实际包版本**判红，
# 而 python:3.14-slim 的层可能仍带旧 zlib1g（CVE-2026-76844 / CVE-2026-85091，
# 修复版本 1:1.3.dfsg+really1.3.1-1+deb13u1）。`apt-get upgrade` 让重建镜像拿到它。
RUN apt-get update \
    && apt-get -y upgrade \
    && apt-get install -y --no-install-recommends \
    libffi-dev \
    libmagic1 \
    file \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 依赖安装（缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# ⚠ 再单独升一次 setuptools/wheel：Trivy 扫的是镜像里的 dist-info，而 **setuptools 自带的
# `_vendor/`** 里躺着 jaraco.context 5.3.0（CVE-2026-23949）与 wheel 0.45.1（CVE-2026-24049）
# —— 这两个包**不在 requirements.txt 里**，只能靠升级 setuptools 本身把它们换掉。
RUN pip install --no-cache-dir --upgrade "setuptools>=78.1.1" wheel
# ⚠ ensurepip 自带的 wheel 副本（`.../ensurepip/_bundled/`）在 `pip install --upgrade`
# 之后仍是旧的 —— 那个升级只改 site-packages，碰不到这份。ensurepip 只在
# `python -m venv` 建虚拟环境时用得上，运行期不需要，直接删掉（顺带省几 MB）。
RUN rm -rf /usr/local/lib/python3.*/ensurepip/_bundled

# ⚠ 最后把 **pip 自身**移出运行镜像。这是修 Trivy 那 5 个 HIGH 的根因，不是"为了扫干净"。
#
# 证据：Trivy 的 Python analyzer 会把 `site-packages/pip/_vendor/vendor.txt`
# （pip 自带的第三方库清单）**当作已安装包**解析，报出的版本与它逐行吻合：
#     msgpack   1.1.2   → CVE-2026-114613 / GHSA-6v7p-g79w-8964
#     urllib3   2.7.0   → CVE-2026-97687 / CVE-2026-97689
#     setuptools 70.3.0 → CVE-2025-47273
# 而**同一份报告**里 site-packages 真正安装的 `setuptools-84.2.1`、`urllib3-2.8.0`
# 都是 0 漏洞 —— 也就是说这 3 条是 pip 内部 vendored 副本，与运行期无关：
#   * `app/` 不 import pip，也没有任何运行期 `pip install`（已 grep 确认）；
#   * HEALTHCHECK 只用标准库 `urllib.request`；CMD 是 `python -m app.main`；
#   * CI 里的 `pip-audit -r requirements.txt` 跑在 runner 上，不依赖容器内的 pip。
#
# 用 `.trivyignore` 掩盖会让 HIGH 门禁失去意义（下次真有运行期漏洞也会被同一条规则吃掉）；
# 移除 pip 则是根治：既消掉噪音，又符合生产镜像最小化（CIS Docker Benchmark 4.1
# "不要在生产镜像里保留包管理器"）。
#
# 需要进容器调试时装回来：`docker exec -u root <c> python -m ensurepip --upgrade`
# （上一步删了 `_bundled`，所以要走网络），或在 Dockerfile 临时注释掉本行重建。
RUN rm -rf /usr/local/lib/python3.*/site-packages/pip \
           /usr/local/lib/python3.*/site-packages/pip-*.dist-info

# 应用代码（排除 examples/ 目录，防止生产环境意外加载 mock 代码）
COPY app/ app/

# 非 root 用户
RUN useradd -m -u 1000 appuser
USER appuser

# 端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

# 启动
CMD ["python", "-m", "app.main"]
