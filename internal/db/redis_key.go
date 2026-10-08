package db

import "os"

// Redis 键统一前缀（N2）：
// 多套环境(dev/prod/隔离)共用同一 Redis 时以 REDIS_KEY_PREFIX 隔离键空间。
// 默认空 = 既有行为,存量兼容。所有 Redis key/channel/stream 名必须经 RedisKey() 组装。

// RedisKey 给 Redis key/channel/stream 加统一前缀，返回带前缀的完整名。
//
// 刻意不做缓存：包级变量（internal/api 的 semaphoreKeyPrefix、retentionLockKey、
// llmKeysHashPrefix、llmKeysVerKey、llmKeysChangedChannel、rpa registryKeyPrefix）
// 会在 main() 之前的**包初始化**阶段调用本函数，而 .env 是由 config.loadDotEnv()
// 在运行时注入的（compose/K8s 直接来自进程环境，不受影响）。若在此缓存首次取值，
// 这些键会永久丢掉 REDIS_KEY_PREFIX —— 多环境共用同一 Redis 时 `llm:keys:`（LLM
// 密钥缓存）、`sem:agent`、`retention:lock` 会跨环境串键。一次 os.Getenv 的开销
// 远小于一次 Redis 往返，故每次直接读取。
func RedisKey(name string) string {
	return os.Getenv("REDIS_KEY_PREFIX") + name
}
