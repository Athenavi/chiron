package api

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"

	"github.com/athenavi/chiron/internal/db"
)

// admin_ops_redis.go —— 管理后台的 **Redis 运维**端点。
//
// 由 admin_ops.go 按运维对象拆出（L3-4 批次 A，同包纯移动，逐字未改）。
// 与留在原文件的部分怎么分：这里只管 Redis 本身 —— 慢日志（SLOWLOG GET）的
// 结构归一化、以及 FLUSHALL 这类危险操作；数据库侧（DatabaseConfigs /
// DatabaseQuery / DatabaseOptimize 与 quoteIdent）仍在 admin_ops.go。
//
// 归一化那几个辅助（normalizeSlowLog / splitSlowLogCommand / slowLogStringMap /
// asString / asInt64）也在这里：Redis 的 RESP 回复是 []interface{} 套 []byte，
// 每种命令的形状都不同，它们只服务本文件的解析。

// ── Redis 管理（单实例真实操作）──

func (h *AdminHandler) redisDo(ctx context.Context, args ...interface{}) (interface{}, error) {
	if db.Redis == nil {
		return nil, fmt.Errorf("redis unavailable")
	}
	cmd := db.Redis.Do(ctx, args...)
	if cmd.Err() != nil {
		return nil, cmd.Err()
	}
	return cmd.Result()
}

func (h *AdminHandler) RedisSlowLog(w http.ResponseWriter, r *http.Request) {
	res, err := h.redisDo(r.Context(), "SLOWLOG", "GET", 20)
	if err != nil {
		OK(w, map[string]interface{}{"slow_log": []interface{}{}, "error": "redis error"})
		return
	}
	OK(w, map[string]interface{}{"slow_log": normalizeSlowLog(res)})
}

// normalizeSlowLog 把 SLOWLOG GET 的结果规范成**具名字段**。
//
// 为什么需要这一层：Redis 的返回形状随协议与版本而变 ——
//   - RESP2（或 Redis < 7）：每条是**位置数组** `[id, timestamp, duration_us, [args…], client, name]`；
//   - RESP3 / Redis ≥ 7：每条是**映射** `{id, timestamp, duration, command, client-addr, client-name}`。
//
// 而前端 `RedisManagementView.vue` 的表格列是 `Object.keys(第一条)` 生成的 ⇒ 直接丢位置数组过去，
// 表头会变成 `0/1/2/…`（有数据、没列名）。前端已按
// `id / timestamp / duration_us / command / args / client / name` 备好列名与**微秒**单位
// （`SLOW_LABELS` + `common.elapsed_s` = 「耗时 (μs)」）⇒ 这里统一成那套键。
//
// 纯函数（喂 map / 喂数组都能测），与 DB 无关。
func normalizeSlowLog(raw interface{}) []map[string]interface{} {
	items, ok := raw.([]interface{})
	if !ok {
		return []map[string]interface{}{}
	}
	out := make([]map[string]interface{}, 0, len(items))
	for _, it := range items {
		var entry map[string]interface{}
		switch v := it.(type) {
		case []interface{}: // RESP2 / Redis < 7
			if len(v) < 4 {
				continue
			}
			cmd, args := splitSlowLogCommand(v[3])
			entry = map[string]interface{}{
				"id":          asInt64(v[0]),
				"timestamp":   asInt64(v[1]),
				"duration_us": asInt64(v[2]), // SLOWLOG 的耗时字段本身就是微秒
				"command":     cmd,
				"args":        args,
			}
			if len(v) >= 5 {
				entry["client"] = asString(v[4])
			}
			if len(v) >= 6 {
				entry["name"] = asString(v[5])
			}
		default: // RESP3 / Redis ≥ 7：映射
			m := slowLogStringMap(v)
			if len(m) == 0 {
				continue
			}
			cmd, args := splitSlowLogCommand(m["command"])
			entry = map[string]interface{}{
				"id":          asInt64(m["id"]),
				"timestamp":   asInt64(m["timestamp"]),
				"duration_us": asInt64(m["duration"]),
				"command":     cmd,
				"args":        args,
			}
			if c, ok := m["client-addr"]; ok {
				entry["client"] = asString(c)
			}
			if n, ok := m["client-name"]; ok {
				entry["name"] = asString(n)
			}
		}
		if entry != nil {
			out = append(out, entry)
		}
	}
	return out
}

// splitSlowLogCommand 把「参数数组」或「命令行字符串」统一拆成 (命令, 其余参数)。
func splitSlowLogCommand(v interface{}) (string, string) {
	switch t := v.(type) {
	case []interface{}:
		parts := make([]string, 0, len(t))
		for _, p := range t {
			parts = append(parts, asString(p))
		}
		if len(parts) == 0 {
			return "", ""
		}
		return parts[0], strings.Join(parts[1:], " ")
	default:
		fields := strings.Fields(asString(v))
		if len(fields) == 0 {
			return "", ""
		}
		return fields[0], strings.Join(fields[1:], " ")
	}
}

// slowLogStringMap 兼容 go-redis 在两种协议下给出的映射类型（键可能是 interface{}）。
//
// ⚠ 不能叫 `toStringMap`：`market_user.go:261` 已有同名函数（签名是 map[string]interface{} → map[string]string，
// 用途完全不同）—— 第一版就这么撞了，包内重名直接编译失败。
func slowLogStringMap(v interface{}) map[string]interface{} {
	switch t := v.(type) {
	case map[string]interface{}:
		return t
	case map[interface{}]interface{}:
		out := make(map[string]interface{}, len(t))
		for k, val := range t {
			out[fmt.Sprint(k)] = val
		}
		return out
	default:
		return nil
	}
}

func asString(v interface{}) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case []byte:
		return string(t)
	default:
		return fmt.Sprint(t)
	}
}

func asInt64(v interface{}) int64 {
	switch t := v.(type) {
	case nil:
		return 0
	case int64:
		return t
	case int:
		return int64(t)
	case uint64:
		return int64(t)
	case float64:
		return int64(t)
	case string:
		n, _ := strconv.ParseInt(strings.TrimSpace(t), 10, 64)
		return n
	case []byte:
		n, _ := strconv.ParseInt(strings.TrimSpace(string(t)), 10, 64)
		return n
	default:
		return 0
	}
}

func (h *AdminHandler) RedisFlushAll(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Confirm bool `json:"confirm"`
	}
	_ = DecodeJSON(w, r, &body)
	if !body.Confirm {
		BadRequest(w, "confirm=true is required to flush redis")
		return
	}
	if db.Redis == nil {
		logAndRespond(w, fmt.Errorf("redis unavailable"), http.StatusInternalServerError, "flush failed")
		return
	}
	// Cluster 下 FLUSHDB 只作用于被路由到的单个节点：必须逐 master 执行，
	// 否则其余节点数据残留（单机/哨兵只有一个 master，仅执行一次）。
	masters := 0
	if err := db.Redis.ForEachMaster(r.Context(), func(ctx context.Context, node db.RedisClient) error {
		if _, err := node.Do(ctx, "FLUSHDB").Result(); err != nil {
			return err
		}
		masters++
		return nil
	}); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "flush failed")
		return
	}
	slog.Info("redis flushed", "masters", masters)
	OK(w, map[string]interface{}{"status": "flushed", "masters": masters})
}
