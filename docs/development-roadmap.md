# 开发路线图

**待办账本**：只收录**未完成**事项（每条标「依据/验收」），已完成项只留指针，未经验证的推测标「待确认」。已明确不做的见 [多实例部署指南](deployment-multi-instance.md) §10。
**北极星：超越 deepseek-harness** —— 可比性、逐层对照与 6 个切口见 [与 DSH 的差距分析](dsh-gap-analysis.md)；判据以**对外可核实的行为**为准。

## 0. 当前状态

**最近一次全量核验（2026-10-09 · round 68，全部本机实测）**：Go `build`/`vet` 通过 · `go test ./...`（**真实 Redis 8 + 真实 PG** 在场）全绿 · **Go live 子集**（`CHIRON_TEST_POSTGRES_DSN` + `CHIRON_TEST_REDIS_ADDR`）`internal/{api,billing,settings,broadcast,engine}` 全绿 · `ruff check .` **0** · `ruff check scripts/` 0 · `mypy app/ acp_adapter/` 0（**244** 文件）· `pytest -m "not integration"` **1936 passed** · `pytest -m integration` **13 passed（全绿）**（需 Redis + 网关；含 L4-1 kill→重启演练、**双进程跨实例演练**、**SDK 端到端**）· 前端 `npx vitest run` **480 passed**（56 文件）+ `vue-tsc -b` 通过 · 守卫：`check_source_encoding` / `check_tool_policy_parity` / `check_doc_links`（存量基线 8 条）通过 · alembic 单 head `0007_subagent_error_code`。
**增量核验台账**（逐轮记录，原为内联的一行 **126 KB**）：已拆出 → [归档：路线图增量核验台账](archive/roadmap-verification-ledger.md)。本节其余内容（最近一次全量核验、本机做不到的项）仍在本文件内。

**本机做不到的**：**容器相关**（无 `docker compose` 插件、Docker daemon 未运行）⇒ 镜像/容器内自查、以及**沙箱服务的容器化部署**须在能跑 Docker 的环境验证。**CI 尚未实跑**（分支未 push，见 §3）。

**文档精简 + 建 wiki（2026-10-09，用户指示）** —— 用户要求「精简文档，仅保留绝对核心且必要的」，并「参照 langchain-doc.cn 的文件树加 wiki」。**先出逐份清单、经用户逐条确认后才动手** ✓。

**① 差距分析四份：全文归档、摘要留原路径** ✓ —— `dsh-gap-analysis` / `reasonix-gap-analysis` / `reasonix-ui-ux-gap-analysis` / `split-assessment` 的正文移入 `docs/archive/`（修正相对链接 21 处 ✓），**摘要写在原路径** ✓✓ ⇒ **代码注释 14 处 + 规划 + 其它文档的引用零改动、零断链** ✓（比「改名 + 建新页」稳得多）。四份合计 **174,140 → 15,258 字节（9%）** ✓。摘要格式统一为「结论 + 行动项（含状态）+ **章节去向**」—— 章节去向表让顺着 `切口 #1` / `§4.10` / `§2.3` 找的人能跳到归档全文 ✓。

**② 台账 §0 巨型行拆出** ✓：那一行 **126,454 字符**把 104 行的台账撑成 **253,090 字节** ⇒ 拆到 [归档：路线图增量核验台账](archive/roadmap-verification-ledger.md)（**纯插入换行，往返逐字节相等** ✓，可证明无损），台账 **253,090 → 27,383 字节** ✓。

**③ 两份「噪音」其实无需动作** ✓：`python-engine/.pytest_cache/README.md` 与 `workspace/media/…` 的那份**git 根本没跟踪、且已在 `.gitignore`**（`.pytest_cache/` · `workspace/`）⇒ 删了 pytest 还会重建、另一份是用户数据 ⇒ 按「无需动作」结案 ✓。

**④ 新增 wiki** ✓：`docs/wiki/` **7 页**（首页 · 概述 · 安装 · 快速入门 · 设计理念 · 架构 · 贡献），定位是「**导航 + 精炼导语 + 指向 docs**」，**不复制正文** —— 单一事实源仍在 `docs/`，wiki 不会与代码各自漂移 ✓。

**⑤ 同步脚本** ✓：`scripts/sync_github_wiki.py`（**默认预演**，`--push` 才推）—— GitHub Wiki 是**独立仓库且页面扁平** ⇒ 脚本把目录树**编码进页名**（`core/architecture.md` → `Core-Architecture.md`、`README.md` → `Home.md`）、把 wiki 内链换成页名、把指向仓库文件的链接换成 **blob URL**（wiki 服务不了仓库文件），并生成 `_Sidebar.md` / `_Footer.md` ✓。

**⑥ 顺手修一处过期值** ✓：`docs/contributing.md` 写「check:ui **7** 道棘轮」，实测 `frontend-vue/package.json` 是 **8** 道 ✓。

**验收**：八个根守卫 **全 exit 0** ✓（含新增的 wiki 页与归档页 ⇒ 链接/表格/编码都过）· `ruff check scripts/` 通过 ✓ · `check_doc_line_refs.py` **0 条硬错误** ✓ · `docs/*.md` 现 **20 份 / 223,215 字节**（此前仅那 4 份 + 台账就 427 KB）· `docs/archive` 5 份 402,358 字节（可追溯）· `market/skills/**` **未动**（那是产品内容，不是文档）✓。

**方法论**：**「精简」的第一动作是出清单而不是删文件** —— 逐份给「留/移/删 + 理由 + 承重度（代码/规划/CI 引用数）」，由用户勾选后再执行；**移动文档时优先「摘要占原路径」** —— 引用方零改动，比改名后再全局改引用安全得多；**把「内容搬走」和「格式重排」分成两步做**（先原样搬、再可验证地重排，重排必须能证明往返一致）。

**wiki 剩余 23 页全部完成（2026-10-09）** —— 承接上一批的 7 页，本轮补齐其余：**core 8 页**（架构 / 网关 / 引擎 / 前端 / 会话 / transcript / 工具与审批 / 知识库 / 子 Agent —— 含架构页共 9 个条目）· **advanced 5 页**（钩子 / checkpoint / 多智能体 / 安全护栏 / 记忆）· **production 5 页**（部署 / 多实例 / 可观测性 / 错误码 / 测试）· **learn 2 页**（概念速查 / 教程索引）· **reference 3 页**（HTTP 面 / 配置 / 命令行）⇒ wiki 现共 **30 页 / 1,481 行 / 70,556 字节** ✓，首页改成**按分组的完整导航** ✓。

**写页面的纪律**：每页都是「一句定位 + 关键模块表（**真实文件路径**）+ 关键不变量 + 权威来源」，**不复制正文** —— 事实仍在 `docs/`，wiki 只做导航 ✓。**核验方式**：把页面里点名的路径抽出来逐条查存在性 ⇒ **105 + 25 = 130 条全部存在**（0 条编造）✓；自写脚本扫 `docs/**` 全部相对 `.md` 链接 ⇒ **211 条 0 断链** ✓。

**⚠ 顺手修掉上一轮归档留下的一个真 bug** ✓：归档到 `docs/archive/` 后，**指向 `docs/` 之外**（如 `../frontend-vue/src/style.md`）的相对链接**少加了一层 `../`** ⇒ 实际解析成一个**不存在**的路径（把 `docs/` 与 `frontend-vue/src/style.md` 拼起来）✗。上轮的改写只处理了「目标在 `docs/` 下」的情况，漏了「目标在仓库其它目录」的情况 ⇒ 本轮补了一层（3 处）✓。**门禁抓到了它** ✓ —— 这正是 `check_doc_links.py` 该干的事。

**一条好用的做法**：wiki 页里**大胆前向链接尚未写的页** ✓ —— 于是「还缺哪些页」从**记忆问题**变成**门禁能报的错** ✓（本轮就是靠 11 条断链精确列出剩下要写的页，一条不多一条不少 ✓）。

**验收**：八个根守卫 **全 exit 0** ✓ · `ruff check scripts/` 通过 ✓ · `check_doc_line_refs.py` **0 条硬错误** ✓ · `doc_value_sweep.py` 28 条断言 / 2 组（内容搬进归档后自然下降）✓ · 同步预演生成 **31 页**（30 页 + `_Sidebar.md`）✓ · `docs/` 现为 **20 份顶层 + 30 份 wiki + 5 份归档** ✓。

**本次未跑**：**容器相关**（无 `docker compose` 插件、Docker daemon 未运行 ⇒ 镜像/容器内自查、沙箱服务容器化部署）· **Python 3.11 语法门禁** · **空库迁移** · **集成测试**（需真实 PG + Redis + 网关）· **GitHub Wiki 实际推送**（本轮只预演 `scripts/sync_github_wiki.py`，推送需认证）。

## 1. 已完成（只留指针与仍生效的护栏）

| 项 | 指针 | 仍生效的护栏 / 注意 |
|---|---|---|
| L1 国际化 | `b7fea4f`…`283c84b` | `check-i18n-keys` 锁 `en-US/ar` 缺键 = 0；`check-i18n.mjs` 禁止**翻译调用**里写中文字面键（`tr`/`translate`/`i18n.t` 别名；注释与 `.test.ts` 除外）。**两个门禁都在 `check:ui` 里**（7 道棘轮的第 4、5 道）；`check-i18n-keys` **用 esbuild 真编译**而不是正则扫源码（域文件是嵌套对象，扁平化正则一旦写错就会漏键 —— 而漏键正是它要防的事）⇒ **不会对注释假阳性**。**已做变异验证（2026-10-09）**：删掉 en-US 的 `chat.messages` ⇒ **exit 1** 并点名「en-US 语义域缺 1 个键（缺键会让含 `{占位符}` 的文案显示成未插值的 key）：chat.messages」，字节级还原 + SHA256 一致 ⇒ exit 0；两语言**语义域对齐 ✓ · legacy 待翻译 0 条** |
| L2-1/L2-2 mypy strict + 真实栈集成门禁 | `21a96ae`…`bf4860f`（1171→0 / 200 文件）· `578bc90` | 接线手册见 [贡献与验收流程](contributing.md)；⚠ 真实栈 job **未在 CI 实跑过**（见 §3） |
| L3-2 ESLint `no-explicit-any` | `de7cb61`…`22ebf95`（272→0） | 不为清零加 `eslint-disable`；`catch` 取值**分层**契约写在 `utils/apiError.ts` 文件头；余 27 条风格警告不再动 |
| L3-7/L3-8 组件测试 + `SSEProducer` 死代码 | `views/__tests__/WorkflowView.spec.ts`；死代码已删 | 两个 VTU 坑记在测试注释里；[架构与请求链路](architecture.md) §6 已同步 |
| L3-9 工具名单漂移 | 两侧策略表已修 | **不变量**：`scripts/check_tool_policy_parity.py`（已接入 CI 的 `Source encoding` job）+ `tests/test_tool_policy_coverage.py` 的 `LEGACY_TOOL_NAMES`。**2026-10-09 扩到第二族**：**对话模式** —— 校验 **Go `validAgentModes` ↔ 引擎 `AgentMode` 枚举 ↔ `_BASE_MODES` 覆盖**三方一致（`docs/session-runtime-spec.md` §3 声称"与 `_BASE_MODES` 对齐"，此前无断言；现含"枚举加了成员却忘写 base 配置"这一种漂移，两次变异验证过）。**同批扩到第三族**：**模型服务提供商目录** —— Go `llmProviderCatalog` ↔ 引擎 `app/providers/catalog.py`（各 **27** 条且集合相同；`docs/service-providers.md` §1/§10 要求两侧同步，此前无断言；已变异验证：只往 Go 加一个 provider ⇒ 门禁红）。**再扩到第四族**：**故障注入的作用面/故障类型** —— Go `chaosSupportedTargets`/`chaosSupportedFaults` ↔ 引擎 `chaos/injector.py` 的 `SUPPORTED_TARGETS`/`MIDDLEWARE_FAULT_TYPES`（`ent_chaos_handler.go` 写着"**必须一致**"，失败模式是**实验静默不生效**即当初的"假注入"；已变异验证：删掉 Go 侧一个 fault type ⇒ 门禁红）。**再扩到第五族**：**跨语言共享的 Redis 键前缀** —— 要求 `subagent:run:` / `subagent:cancel:ack:` / `engine:instance:` 三个字面量**同时出现在两侧源码里**（`run_affinity.go` ↔ `affinity.py`、`subagent_cancel.go` ↔ `affinity.py`、`run_affinity.go` ↔ `remote.py`）。**为什么需要它**：两侧**各自**都有测试钉住自己那份字面量 ⇒ 只改一侧时"顺手把该侧测试也改了"会让两边都绿，漂移照样上线（归属查不到 / 取消回执没人认领，都是静默失效）。已变异验证：把 Go 侧前缀改名 ⇒ 门禁红并点名该键。**再扩到第六族**：**共享数值常量** —— `defaultBranchKeepTail=4` / `minCondenseMessages=3` ↔ 引擎 `DEFAULT_KEEP_TAIL` / `MIN_COMPRESSIBLE_MESSAGES`（同样是"各写一份 + 各自有测试"的陷阱；已变异验证：把 Python 的 4 改成 5 ⇒ 门禁红）。**再扩到第七族**：**工具授权模式（ask/auto/yolo）** —— 唯一一条**三语言**约定（Go `mode.go` ↔ 引擎 `guards.py` 的 `_VALID_SESSION_MODES` ↔ 前端 `ChatView.vue` 的 `toolsMode` 联合类型），用**集合比对**而非"字面量出现"：已变异验证"**只在一侧新增一个模式**"（naive 检查抓不到的方向）⇒ 门禁红。**2026-10-09 修正（值得记）**：该族最初写的是"**字面量是否出现在文件里**" —— 实测**两次踩坑**：① 那些键在模块 docstring 里也写着 ⇒ 改掉 `rkey("engine:run:")` 后守卫**仍然绿**；② Go `const` 块是 **gofmt 对齐**的（`runRecordPrefix   = "..."`）⇒ 写死单空格的片段**假红**。现改为**比对代码形态的正则**，并补上第 4 个键 **`engine:run:`**（引擎写、网关读的**生产者/消费者**形态，漂移会**静默**降级成"无映射"），两个方向各变异验证过。**2026-10-09 续：把同一教训应用到其余族** —— 新增 `_code_lines()`（**匹配前先剥注释行**），第五、六族都改用它；第六族判据也从"写死单空格"改成 `\s*=\s*` 正则。并**补测了此前只测过单方向的两族**：第三族的 **Python 侧**（改 `catalog.py` 的一个 provider id）与第六族的 **Go 侧**（改常量值）⇒ 均门禁红、均字节级还原。**至此七族都做过"改一侧"的变异验证** |
| DSH 切口 #3（思考 / 用量） | [差距分析](dsh-gap-analysis.md) 切口 #3 | 思考走独立 `thinking`；`usage` 按**次**增量 + `done` 累计（网关按**事件类型**分流以防重复计费，前端 `mergeTurnStats` 合并成本轮一行）；**阶段已决定不做**（主对话无真实状态；子 agent 侧本就有 `subagent.status`） |
| L4-2 CLI 迁移入口 | [决策记录](db-migration-entry.md) | **Alembic 唯一入口**：Go `db migrate`、Python `migrate run`/`downgrade` 均已删，只留**只读**诊断（`db status` / `migrate history`，失败非 0 退出）；`scripts/init.py` 第 6 步保留（跑的是同一份 `alembic.ini`，属部署向导） |
| 沙箱/出站/命令守卫（A 层） | [差距分析](dsh-gap-analysis.md) 切口 #6 | 三处实证逃逸已修：① 白名单模块的**子模块**按**逐级前缀**判（`asyncio.subprocess` 曾可起进程）；② 出站 IP 按**语义归一**（`::ffff:169.254.169.254` 等 IPv4-mapped/6to4/Teredo 曾绕过 SSRF 名单）；③ 插件命令从"只比 basename"改为**裸 basename 或绝对路径**（`../`、UNC 曾可执行攻击者放置/远程的同名二进制，Go 与 Python 两侧同步）。护栏：`tests/test_code_guard_submodule_escape.py` + `tests/test_ssrf.py` + `internal/api/plugin_command_allowlist_test.go`（8 条路径规则：裸 basename / POSIX 绝对 / Windows 绝对放行；**相对穿越 · Windows 穿越 · UNC 共享 · `./npx`** 拒绝；另有"白名单未配置 ⇒ 全部拒绝"的 fail-closed 默认）；④ 远程技能取回（`skill.py::_fetch_skill_json`，install 与 discover 的唯一入口）的体积上限改为**流式**：越界**立即中止并关闭响应**，而不是读完再判（后者只拦解析、拦不住下行流量与内存）——`tests/test_skill_remote_fetch.py` 用"块计数"证明提前停止，并做过变异验证 |
| 执行审计覆盖面（`exec_audit` + `plugin_audit`） | [差距分析](dsh-gap-analysis.md) 切口 #4 | **每条执行路径都要进审计**：补了后台命令 `tool_job`（每个终态，含取消）与 git 工具；plugin audit 抽成可复用的 `app/plugins/audit.py`（`ts` 对齐 UTC+毫秒），运行时拉起插件进程的两处（`mcp/client.py`、`skill/manager.py`）已接入。**覆盖清单机械化**：`tests/test_exec_audit_inventory.py`（起进程位置未归类即失败）；护栏：`tests/test_job_exec_audit.py`、`tests/test_plugin_audit.py`；**多副本集中摄取（N4）**：`POST /v1/internal/audit/exec`（内部令牌、逐条校验部分接受、单批上限 500）+ 引擎侧**有界队列**发货（`EXEC_AUDIT_SHIP_URL`，**默认关**、不阻断执行、满了丢最旧且计数），引擎与 S5e 沙箱服务共用同一 `exec_audit` 路径 ⇒ 多副本审计回到一处可查 |
| `internal/*` 零测试包 | **5 个包 39 条**用例（`id` 8 · `storage` 4 · `enterprise` 6 · `model` 4 · `monitor` 17）—— 此前这 5 个包**零测试文件** | 雪花 ID **并发唯一性** + worker 位布局 + 时钟回退不重号 · `AtomicStore` **并发切换不撕裂** · RBAC 缓存的 **nil ↔ 空切片防越权语义** · `WorkingMemory` 返回拷贝（外部改不动历史）· **环形直方图只保留最后 N 个样本** · **会话成本表容量淘汰 + TTL 过期剔除** · span **父子传播**（同 trace、ParentID 正确）与 `End()` 默认 OK + 真导出 |
| 文档断链守卫 | `scripts/check_doc_links.py` + **CI「Source encoding」job** | 被引用的 `docs/*.md` 必须存在；存量走基线（`scripts/doc_link_baseline.txt`，**只应缩小**：**已 19 → 0（清空，2026-10-09）**），**新增断链即失败**（变异验证过）。口径已排除测试/评测 fixture、合成占位路径、**URL 里出现的 docs/ 路径**（如厂商文档链接），并接受 vendor 下对标项目的同名文档。`--show-known` 可按文档列出引用方，便于逐条还债。**相对链接已纳入（2026-10-09）**：老口径只认 `docs/` 前缀，`[x]` 指向 `y.md` 的相对写法一直是**盲区**；现按**引用方所在目录**解析、**围栏代码块整块跳过**，目标归一成仓库相对路径 ⇒ 与 `docs/` 口径**共用同一份基线**（已做变异验证；当前 133 处前缀引用 + 63 处相对链接，0 新增） |
| Markdown 表格结构守卫 | `scripts/check_md_tables.py` + **CI「Source encoding」job** | 同一表格块内**列数必须一致**（未转义 `\|` 数 == 块首行）；只查被跟踪的 `.md`（跳过 `vendor/` 等）。**列数不一致不会让 Markdown 报错、只会静默错位**，四个既有守卫都不覆盖 —— 实测在本文件 §3 抓到两处（L61 多一个分隔符、L63 少行尾 `\|`）。存量基线当前为**空** ⇒ 新增即失败（已做变异验证） |
| `scripts/` lint 门禁 | 仓库根 `ruff.toml` + CI「Source encoding」job | 历史问题 **8 → 0**（未用变量/未用导入/裸 `except`/该有的 `noqa` 说明），规则集与引擎一致；护栏：CI 跑 `ruff check scripts/` |
| ORM 生成物新鲜度守卫（**2026-10-09 新增**） | `scripts/check_orm_models.py` + **CI「Source encoding」job** | `shared/models/**` 是**生成物**（`scripts/generate_orm_models.py` ← `configs/orm/V1/models.yaml`），却**不在 ruff/mypy 门禁内、也没有任何用例提到它**（全仓只有生成器自己提它）。漂移过一次：`subagent_run.py` 缺 7 列（`inherited_messages` / `retryable` / `output_bytes` / `validator_*` / `error_code`）而迁移 `0004/0006/0007` 里都有 —— 靠人肉核对才发现。**判据**：把生成器**重跑到临时目录**（猴子补丁 `OUTPUT_DIR`，仓库**只读**）再逐文件比对，**只忽略模板的「生成时间」行**（每次运行都不同；第 68 轮就是它造成 69 个文件的纯时间戳噪声）。手写的 `base.py` 走**显式白名单**（而不是"忽略所有多出来的文件"，否则"生成器不再产某个模型"会被静默吞掉）。**已做变异验证**：删一列 ⇒ exit 1 并点名「首个不同在第 85 行」；字节级还原 + SHA256 一致 ⇒ exit 0 |
| L5-2 架构与请求链路 | `65b0558` | 文中命令均本机实测 |
| 对外 SDK（第一方示例客户端） | `clients/python/chiron_client.py` + 同目录 `README.md` | **决定（2026-10-09）：停在示例客户端** —— **不发布、不给兼容性承诺**（README 已写明）。零第三方依赖：登录/注册 · 提交 · SSE 订阅含 `Last-Event-ID` 重连 · 取消。护栏：`tests/test_chiron_client_sdk.py`（8 条契约单测，假 `HTTPConnection` 跑真代码）+ `tests/test_chiron_client_sdk_live.py`（**真实网关端到端**，已实测通过）。⚠ 若将来要发布或承诺兼容，**需重开产品决定**（并补 OpenAPI/JSON-RPC 面） |
| L4-1 run 现场 checkpoint 续跑 | `migrations/versions/0003_agent_runs.py` · `app/agent/{runtime.py,checkpoint.py,resume.py}` · 设计见 [run 现场 checkpoint 续跑设计](run-checkpoint-design.md) | 表 `agent_runs`（含**防脑裂唯一索引**）· 回合末落盘（只写不读，失败只降级恢复粒度）· schema v1 + 热 1h/冷 24h · 续跑/接管 + 启动 reconciler + 指标。护栏：`tests/test_checkpoint.py` · `test_checkpoint_schema_version.py` · `test_run_resume.py`（38 条）**+ 跨进程"硬杀→接管不重放"** `tests/test_resume_kill_drill.py`（integration：真 `AgentRuntime`、真硬杀、断言历史里第 1 回合工具结果恰好一份）。设计里"新增表 `agent_runs`"是**原文过期**（该表在 `0003` 就存在）；原"待拍板 5 问"已在实现中定案。批 5（长逻辑 checkpoint / 跨实例现场迁移）**明确不做** |
| 钩子协议（追平 DSH）批次 1 / 2a / 2b | `python-engine/app/hooks/*` · 设计见 [钩子协议设计](hook-protocol-design.md) | 声明面 `hooks.json`（锚定 matcher、默认空 = 零行为变化）· `UserPromptSubmit`（**只观测**）+ 每事件累计预算 · **`command` 形态（2026-10-09，拍板 (b)）**：独立开关 `hooks_allow_commands`（默认关）+ 部署自备 `hooks_command_allowlist`（**空 = fail-closed**、允许绝对路径）+ 退出码 2 约定 + `exec_audit(tool="hook_command")`。护栏：`test_hooks.py` · `test_hooks_config.py` · **`test_hooks_command.py`（真的起子进程；含变异验证）** · `test_exec_audit_inventory.py`（归类）。**这是 §6「不扩大可执行面」唯一登记在案的例外**（运维级；租户不可达）。剩余：批次 3（`webhook`，待拍板⑤）/ 4（上下文注入，待拍板③） |

## 2. 待办

### L4-3 时间列 `timestamp` → `timestamptz`

- 依据（本机实测 2026-10-08，**2026-10-09 复核一致**）：**142 列** naive（72 表）· **12 列** `timestamptz`（5 表）· VARCHAR 侧**名字即时间的有 3 列** —— 已废弃的 `schema_migrations.applied_at` 以及两个**日期**列 `meeting_notes.date` / `admin_tenant_usage.stat_date`（本机两表为空，语义未能从取值确认；若确为"只有日期"的字段，属 `date` 类型的独立问题）。
- **语义已抽样判定**：存量 naive 值存的是**本地墙钟（+08）**而非 UTC —— 同一轮 run 在 `agent_runs.*`（`timestamptz`）是 `2026-09-30T14:26:40Z`，而 `turns` / `messages`（naive）是 `22:26–22:27`。
- ⚠ **风险不是"哪一侧对"，而是"同一列可能混着两种语义"**：`NOW()`/`CURRENT_TIMESTAMP`（**95** 处）跟**数据库会话时区**，`time.Now()`（**110** 处，含 6 处 `.UTC()`）跟**宿主时区**；两侧不一致时（典型：容器 TZ=UTC + 数据库 `timezone=Asia/Shanghai`）任何单条 `USING … AT TIME ZONE 'X'` 都会把一半的行移错时刻。**已加启动只读诊断**：`internal/db/tz_diagnostic.go` + `warnOnTimezoneMismatch`（不一致即告警；本机实测两侧同为 `+08` ⇒ aligned）。
- 剩余步骤：① 生产先跑同一条诊断（或把两侧时区对齐）；② **抽样确认生产存量语义，不要套用本机结论**；③ 用**影子列回填 → 校验 → 切换**三步走，而不是一次性 `ALTER … USING`；④ ORM 渲染 `DateTime(timezone=True)`（模板现无条件渲染 `DateTime`）。
- 前置：生产时区确认；需可达 PostgreSQL。验收：新迁移单 head + `alembic upgrade head --sql` 通过 + ORM 重新生成 + **迁移注释写清 `USING` 的时区依据**。

### S5-(e) 执行沙箱服务 —— **服务应用已落地**，剩部署形态

- 已落地：`sandbox-service/service.py`（`/v1/internal/exec/run` + health、令牌 fail-closed、**直接复用引擎的 `run_in_sandbox`** 基线与审计、身份随请求恢复）；引擎侧客户端 `app/backends/remote_exec.py` 已随请求带身份；**接线**（`SANDBOX_BACKEND=service` → 分流）抽成 `_apply_sandbox_service_branch()` 并有 4 条分支用例。测试：`tests/test_sandbox_service.py`、`tests/test_sandbox_service_wiring.py`、**`tests/test_sandbox_service_multiprocess.py`（独立进程端到端）**。
- 剩余（**都在代码之外**）：**独立容器**部署（无 docker socket、仅内网、不与引擎共容器）· `SANDBOX_ROOT` **与引擎共享同一工作区卷**（否则命令产物与文件工具分叉）· 白名单租户**真的走服务**的服务侧断言 · 灰度/回滚手册。**真实部署验证不能靠单测替代**。（**审计多副本收集已落地**：引擎与沙箱服务成批送 `POST /v1/internal/audit/exec`。）

### L3-4 巨型文件拆分 —— 批次 1、3、2(类型) 与 4(校验) 已完成

- 已完成（每批都验证**逐函数/定义逐字一致**）：`gateway_router.go` → 11 个 `routes_*.go`（**1254→442**）· `mail_handler.go` → 4 个文件（**1488→695**）· `session/manager.go` → `manager_{crud,messages,queries,branch}.go`（**1198→128**）· `runtime.py` 类型与决策取值 → `runtime_types.py`（**3370→3198**）· `main.py` 的三个启动前置校验 → `app/deps.py`（**2170→2061**）。
- 剩余：`runtime.py` 压缩簇（**必须先按现状重新定界**）· **`main.py` 的 `lifespan` 分段**（关闭段 + **6 个无耦合启动段** ✅ 已抽，`lifespan` 740 → 561 行；剩 5 段有真实耦合，需显式传参/返回句柄）· **前端 composable**（**批次 1 ✅** 执行/轮询簇 → `useWorkflowExecution.ts`，1746 → 1637 行；**批次 2 ✅** 模板市场簇 → `useWorkflowTemplates.ts`，1636 → 1597 行；持久化簇因**注入面 14 个**暂缓，须先抽画布状态；另 **`ChatView`（3429 行）已补冒烟网** 3 条，可开始拆簇）。边界与验收见 [拆分评估](split-assessment.md)；无功能收益，排最后（§4 批次 D）。

## 3. 开放项（待确认，不作为计划依据）

| 项 | 需要什么才能立项 |
|---|---|
| **超越 deepseek-harness 的切口** | 已产出 [与 DSH 的差距分析](dsh-gap-analysis.md)（逐层对照 + 6 个切口 + 未验证清单）与 [多副本语义的对外保证](deployment-multi-instance.md)；每个切口立项前须给出**可复现命令、期望输出与反例** |
| **文档断链：**0 份**（2026-10-09 已清空）—— 原为 5 份被引用的 `docs/*.md` 不存在（已基线化 + 门禁；**当前暂停还债**，优先功能）** | 守卫 `scripts/check_doc_links.py`（**已接 CI**）实测：**原 19 份**（`docs/subagent-design.md` 被 16 个文件引用 · `mail.md` 4 个 …），git 历史里**从未存在过**（不是被删）。**已还 17 条 + 剔除 2 条误报（合计 19 条全部清完）**：① 会话地图的引用**改指**到恢复出来的 [会话地图与分支设计](session-map-branch-design.md)；② 两处（只读副本一致性 / Redis 键前缀）**删引用**（规则本就在代码注释里）；③ 两处"我们引用过某份没入库的文档"的自述**去掉路径**；④ **按代码重建五份**：[会话运行时 spec](session-runtime-spec.md)（7 处引用）· [邮件通道](mail.md)（`mail.go` 里 8 处**带小节号**的引用，重建时**沿用原小节号**）· [模型服务提供商](service-providers.md)（含 OpenCode 的 `x-opencode-session` 一节）· [Agent 安全与可靠性](agent-safety-and-reliability.md)（**§1.4 MCP 收窄** 与 **§4.2 撤销栈诚实优先**两个锚点）· [LLM 密钥管理 DR](llm-provider-key-management-dr.md)（集中派：权威层/keyset/状态机/KeyRing 与 V1→V2 预留）；⑤ **剔除 1 条守卫误报**：有一条"缺失文档"其实是 **Ollama 文档 URL 的一部分**（守卫已加"URL 里的路径不算引用"，故这里不写出那个路径以免又造一条断链）。**剩 0 条**：逐条改引用还是补写文档（`--show-known` 给出每条的引用方）。**逐条建议（2026-10-09，供一次拍板）**：① ~~**`subagent-design.md` ⇒ 重建**~~ **已完成（2026-10-09）**：**17 处**引用且**小节号齐全**（§3.1–§3.4 / §4.2–§4.4 / §5.2 / §6 / §7 / P0），已照同一手法**按代码重建**（沿用原小节号，每条断言带 `file:line`）⇒ **一次清掉 17 处引用**（含本行那处），基线 **7 → 6**；② ~~**`subagent-interaction-redesign.md` ⇒ 重建或删引用**~~ **已按"删引用"处理（2026-10-09）**：守卫计到的 **2 处**（`subagent_cancel.go` · `metrics.py`）都已改为**就地说明**（前者指向活着的 `store.py` 终态白名单；后者把 P1-3 的定义就地写清）⇒ 基线 **6 → 5**。**另记**：该文档还被 **3 个测试文件**（`test_queue_delayed_retry.py` / `test_subagent_approval_forward.py` / `test_subagent_state_metrics.py`）引作"§八「尚未完成」"的出处，而**守卫跳过测试文件**故不计入 —— 这 3 处**也已改为**"该设计文档**未入库**，结论如下"（各文件的背景段落本就自解释，删掉悬空指针零信息损失）；③ **其余 5 份 ⇒ 删引用**（`ent-webhook-design` 1 处 · `floating-panels-design` 2 处 · `multi-session-runtime-plan` 2 处 · `production-readiness-fixes` 1 处 · `turn-lifecycle-and-approval-audit` 1 处）：引用点**本就把规则写在注释里**，与已有先例②"规则本就在代码注释里 ⇒ 删引用"同法；④ ~~**`superpowers/plans/deployment-plan.md` ⇒ 剔除为误报**~~ **已剔除（2026-10-09）**：它出现在 `market/skills/requesting-code-review/SKILL.md` 的 **prompt 模板占位**里（`PLAN_OR_REQUIREMENTS: Task 2 from …`），是**导入技能的示例文本**，不是真引用。**取舍**：重建保住"设计出处"这层可追溯性但要写文档；删引用零成本但丢掉它 |
| CI 首跑（L2-1 的 mypy / L2-2 的 integration） | push 并跑一次 CI：未声明顶层依赖、空库建表路径、pgvector 镜像、网关在 runner 内的启动 |
| 新功能方向 / 前端 e2e（Playwright） | 新功能方向需产品路线图输入；前端当前无 e2e 配置，需确认是否引入浏览器依赖 |
| 多实例**重复投递**（双进程演练发现并已修复，2026-10-08） | `internal/broadcast/hub.go`（新增 `RelayEvent` + `logicalEventID`，`ReplayAfter` 去重）· `internal/api/subagent_events_relay.go` · 引擎侧发布带 `event_id` —— 修复前：中继 `hub.Publish` 既追加共享重放流**又跨实例再广播** ⇒ 实时各收 **2 份**、补发 `[2,2,3,3]`（N 实例 = N 份）。修复：中继改走 `RelayEvent`（只追加本实例流 + 本地 fanout，**不再广播**），**写入端不做互斥**（否则 N-1 个实例的客户端拿不到 `id`），改为**读取端按逻辑身份去重**（`event_id`，退化 payload 哈希）。护栏：`hub_live_test.go::TestLiveReplayDedupesSameLogicalEventAcrossInstances` · `logical_id_test.go` · 双进程演练 `multi_instance_drill.py`（**已接 CI real-stack job**，4 条断言全绿） |
| `internal/api` 路由聚合方式 | 已评估（L3-4）：按业务域拆 `routes_*.go`，见 [拆分评估](split-assessment.md) |
| **遗留的第二套 agent 循环 `app/agent/loop.py`** | 实测：它有自己的 `run_agent`（316 行，也自发 `usage`），经引擎路由 `/v1/agent/run` 暴露；而**产品链路是 `/v1/agent/submit` → `AgentRuntime`（`runtime.py`）**，且 Go 侧 `PythonClient.Run`（唯一会打 `/v1/agent/run` 的调用方）**零调用者** ⇒ 该文件＋该端点是**遗留**。**已做**：① `loop.py` 标为遗留并写明"新能力一律加在 `runtime.py`"；② `/v1/agent/run` 加**一次性告警**，用来观测是否真有外部调用方（有证据再决定删，不靠猜）；③ 把该模块**独有覆盖**的安全性质用例搬到产品链路（`tests/test_runtime_tool_truncation.py`）—— 搬的过程中当场查出 **runtime 的截断 `tool_call` 在守卫之前就已下发**（前端多一张卡、网关把半截 JSON 落库），已修。**待做**：确认无调用方后，连同 `/v1/agent/run`、`PythonClient.Run`（Go 侧零调用者）与 `tests/test_agent.py` 一起删除。 |
| 引擎镜像自包含内置技能 | 现由 compose 只读挂载 + `CHIRON_BUILTIN_SKILLS_PATH` 提供（源缺失会告警）；镜像自包含需把构建上下文改为仓库根并 `COPY market/skills` —— 必须**真实 `docker build` + 容器内自查**，不能用单测替代 |

## 4. 建议顺序

| 批次 | 内容 | 理由 |
|---|---|---|
| C. 跨层设计 | L4-1、L4-2、L4-3 | 需设计评审或可达 PG；L4-2 待用户决策 |
| D. 可维护性 | L3-4 | 无功能收益，放最后 |

> 批次 A（L2-2/L5-2）与批次 B（L1-4）已完成。

## 5. 通用验收口径

（与 CI 一致；逐 job 说明见 [贡献与验收流程](contributing.md)，以 `.github/workflows/ci.yml` 为唯一事实来源）

```bash
# 仓库根
go build -mod=mod ./... && go vet -mod=mod ./... && go test -mod=mod ./... -count=1
python scripts/check_source_encoding.py
python scripts/check_tool_policy_parity.py   # Go ↔ Python 分级表必须同构（L3-9）
python scripts/check_doc_links.py             # 被引用的 docs/*.md 必须存在（新增断链即失败）
ruff check scripts/                           # 仓库根脚本（配置见 ruff.toml）
python -m alembic -c alembic.ini heads        # 必须只有 1 个 head

# 真实栈用例（需可达 PostgreSQL / Redis；CI 由 real-stack job 跑）
go test -mod=mod ./... -count=1 -run 'Live|Diag'   # CHIRON_TEST_POSTGRES_DSN / CHIRON_TEST_REDIS_ADDR
python python-engine/tests/drills/multi_instance_drill.py <网关二进制>  # 跨实例**双进程**演练（CI real-stack job 也跑）

# 本机真实栈：把**网关也起起来**，`pytest -m integration` 才会全绿（配方见
# [贡献与验收流程](contributing.md) 的「本机真实栈」）

# python-engine/
ruff check . && mypy app/ acp_adapter/ && python -m pytest -q -m "not integration"

# frontend-vue/
pnpm install --frozen-lockfile && pnpm run lint && pnpm run build && pnpm run test
```

> `-mod=mod` 不可省略：仓库 `vendor/` 为参考源码（无 `modules.txt`），Go 见 `vendor/` 即报 `inconsistent vendoring`。
