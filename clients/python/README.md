# Chiron Python 客户端（第一方示例）

零第三方依赖的最小客户端：登录/注册 → 提交一次对话 → 订阅会话事件流（含断线重连）→ 取消运行。

> **兼容性状态：未承诺。** 这是**第一方示例客户端**，接口可能随网关演进。是否对外发布、
> 以及是否给出兼容性承诺，是一个产品决定（见 [开发路线图](../../docs/development-roadmap.md) §2「对外 SDK」）。

## 用法

```python
from chiron_client import ChironClient

client = ChironClient("http://127.0.0.1:8080")
client.login("you@example.com", "your-password")   # 也可 ChironClient(url, token="...")

session_id = "my-session-1"
client.submit(session_id, "帮我看看这个仓库")        # 202 收下即返回（不是流）

for event in client.stream_events(session_id):
    if event.type == "text":
        print(event.data.get("content", ""), end="")
    if event.type == "done":
        break
```

断线重连（补发缺口后转实时）：

```python
last_id = None
for event in client.stream_events(session_id, last_event_id=last_id):
    last_id = event.id or last_id          # 每帧的 id 就是可回传的流 ID
```

取消：

```python
client.cancel(session_id)
```

## 两个容易踩的点（实测）

1. **`/v1/agent/submit` 是"收下即返回"**（202）：网关随后异步执行，**事件不在这条响应里** ——
   必须另外订阅 `GET /v1/events`。把它当 SSE 流读会一直等不到东西。
2. **SSE 的 `data` 是信封**：`{"id","type","data":{...业务负载...},"session_id"}` ——
   业务字段在 `data.data`，顶层只有 type/session_id/id。本客户端已经帮你拆开（`event.data` 即业务负载）。

## 测试

- 契约单测（假 `HTTPConnection`，不打网络）：`python-engine/tests/test_chiron_client_sdk.py`
- 真实网关端到端（integration）：`python-engine/tests/test_chiron_client_sdk_live.py`
  —— 需要网关可达（`CHIRON_GATEWAY_URL`，默认 `http://127.0.0.1:8080`）；若实例关闭注册/要求邮箱验证码，
  用例会退化为自签令牌（需要 `JWT_SECRET`/`APP_SECRET`），SSE 与取消仍然真实验证。
