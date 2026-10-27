# 真实组件构建（PageIndex / LangExtract）

> 目标：把 PageIndex / LangExtract 的 **独立 venv** 打成组件包（zip + manifest），
> 供 `desktop-local/app/components/manager.py` 安装、启动、停止。
>
> 脚本：`desktop-local/scripts/build_component.py`
> 依赖清单：`desktop-local/scripts/components/{pageindex,langextract}.requirements.txt`

## 1. 为什么必须独立 venv

| 组件 | 关键约束 |
|---|---|
| PageIndex | `pageindex==0.4.4` → `litellm` → **`openai<3`**，与引擎的 `openai 3.x` 冲突 |
| LangExtract | `langextract==1.7.1` → `pandas` / `numpy` / `google-genai`，体积大 |

两者都**不能**装进 `desktop-local` 的 core venv。构建脚本为每个组件创建独立环境，
只把运行时和依赖树打进组件目录；core 通过 `components/<name>/<version>/manifest.json`
启动它。

## 2. 构建

```bash
# 先准备一个用于构建的 Python（3.11+）
python desktop-local/scripts/build_component.py \
  --name pageindex \
  --out desktop-local/dist \
  --python python

python desktop-local/scripts/build_component.py \
  --name langextract \
  --out desktop-local/dist \
  --python python
```

产物：

```
desktop-local/dist/
  pageindex-0.4.4.zip
  pageindex-0.4.4.manifest.json
  langextract-1.7.1.zip
  langextract-1.7.1.manifest.json
```

`manifest.json` 关键字段：

```json
{
  "name": "pageindex",
  "version": "0.4.4",
  "entry": "python",
  "args": ["-m", "app.main"],
  "files": [{"path": "...", "sha256": "..."}],
  "sha256": "<bundle>"
}
```

## 3. 安装与启动

组件源由环境变量指定（本地目录或 HTTP 基址）：

```bash
export CHIRON_COMPONENT_BASE_URL=<构建产物目录或 HTTP 基址>
```

然后调用 core 的组件 API（桌面端由 `components.cjs` 转发）：

```
POST /v1/components/pageindex/install
POST /v1/components/pageindex/start
POST /v1/components/pageindex/stop
DELETE /v1/components/pageindex
```

安装时逐个文件校验 sha256，并拒绝 `..` / 绝对路径 / 符号链接；启动时下发
`CHIRON_DATA_DIR`、`CHIRON_INFERENCE_PROXY`、`CHIRON_COMPONENT_PORT` 等环境变量。

## 4. 已知限制

- 没有内置组件仓库与签名校验：`sha256` 与源同源，源被篡改无法防。
- 无断点续传 / 镜像回退。
- 锁只在单进程内；两个 core 实例可并发改同一 `components/` 目录。
- 组件崩溃不自动拉起；core 退出不自动 `stop_all`。
- 健康检查只支持 HTTP GET。
- 真实 PageIndex / LangExtract 包尚未在本机完成一次完整“构建 → 安装 → 启动 → 调用”；
  `tests/test_component_build.py` 用假包覆盖了打包与校验路径。
