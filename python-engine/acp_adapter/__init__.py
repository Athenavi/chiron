"""ACP 适配层（方案 03 §5 · 批 I）—— 可选组件，与引擎/评测互不影响。

编辑器侧配置：

```toml
# 以 Zed 为例（其它 ACP 客户端同理）
[agent_servers.chiron]
command = "python"
args = ["-m", "acp_adapter"]
env = { CHIRON_API_KEY = "...", CHIRON_BASE_URL = "http://127.0.0.1:8080" }
```

需要官方 SDK：`pip install agent-client-protocol`（只有本条产品线需要它）。
"""
