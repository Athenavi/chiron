package db

import (
	"context"
	"fmt"
)

// ParseHashReply 把 HGETALL 之类命令的返回解析成 map[string]string。
//
// ⚠️ **go-redis v9 的 `Do(ctx, "HGETALL", key)` 返回 `map[interface{}]interface{}`**，
// 而 v8 时代是扁平 `[]interface{}`。若调用方还按 v8 断言 `res.Val().([]interface{})`，
// 会拿到 `ok == false` 且**不报错** —— 静默失败。
//
// 这正是「模型发现从未运行、/v1/models 永远返回空」的根因：
// internal/api/model_discovery.go 的 keysetActiveProviders 解析不出任何 provider，
// 于是发现循环一次都没进过（日志里连一条 warn 都没有）。
//
// 这里一次性兼容三种形态（v9 map / RESP3 map[string]interface{} / 老式扁平数组），
// 调用方不必再各自写脆弱的类型断言。
func ParseHashReply(reply interface{}) map[string]string {
	out := make(map[string]string)
	switch v := reply.(type) {
	case map[interface{}]interface{}:
		for key, val := range v {
			out[fmt.Sprint(key)] = fmt.Sprint(val)
		}
	case map[string]interface{}:
		for key, val := range v {
			out[key] = fmt.Sprint(val)
		}
	case map[string]string:
		for key, val := range v {
			out[key] = val
		}
	case []interface{}:
		for i := 0; i+1 < len(v); i += 2 {
			out[fmt.Sprint(v[i])] = fmt.Sprint(v[i+1])
		}
	case []string:
		for i := 0; i+1 < len(v); i += 2 {
			out[v[i]] = v[i+1]
		}
	}
	return out
}

// HashAll 读一个 HASH 的全部字段，是跨 go-redis 版本的统一入口。
// rdb 为 nil 时返回空 map（而非报错），便于调用方静默降级。
func HashAll(ctx context.Context, rdb RedisClient, key string) (map[string]string, error) {
	if rdb == nil {
		return map[string]string{}, nil
	}
	res := rdb.Do(ctx, "HGETALL", key)
	if err := res.Err(); err != nil {
		return nil, err
	}
	return ParseHashReply(res.Val()), nil
}
