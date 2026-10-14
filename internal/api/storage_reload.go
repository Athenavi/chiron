package api

import (
	"context"
	"fmt"
	"log/slog"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/settings"
	"github.com/athenavi/chiron/internal/storage"
)

// settingsCategoryStorage —— 后台「存储」配置所在的 system_settings 分组。
// 与 admin.go 的 UpdateStorage、admin_extras.go 的 SaveSettings 共用同一分类名。
const settingsCategoryStorage = "storage"

// buildStorageStore 由「后台存储配置」构造文件后端。
//
// 键名与 StorageUpdateRequest 的 JSON tag 对齐：backend / s3_endpoint / s3_bucket /
// s3_access_key / s3_secret_key / s3_use_ssl（local 时可带 storage_root）。
func buildStorageStore(cfg map[string]interface{}) (storage.FileStore, error) {
	backend, _ := cfg["backend"].(string)
	switch backend {
	case "local":
		root, _ := cfg["storage_root"].(string)
		if root == "" {
			root = config.LoadAllowUnconfigured().StorageRoot
		}
		return storage.NewStore("local", root, "", "", "", "", false)
	case "s3":
		endpoint, _ := cfg["s3_endpoint"].(string)
		bucket, _ := cfg["s3_bucket"].(string)
		access, _ := cfg["s3_access_key"].(string)
		secret, _ := cfg["s3_secret_key"].(string)
		useSSL, _ := cfg["s3_use_ssl"].(bool)
		if endpoint == "" || bucket == "" || access == "" || secret == "" {
			return nil, fmt.Errorf("s3 backend requires s3_endpoint/s3_bucket/s3_access_key/s3_secret_key")
		}
		return storage.NewStore("s3", "", endpoint, bucket, access, secret, useSSL)
	default:
		return nil, fmt.Errorf("unknown storage backend %q (want local|s3)", backend)
	}
}

// applyStorageFromSettings 从 system_settings 读取 storage 分类并**热切换**文件后端。
//
// 两个调用点：
//   - 网关启动（让后台保存过的配置在重启后依然生效，env 只作初始兜底）；
//   - 跨副本广播订阅者（保存方之外的副本即时一致，无需滚动重启）。
//
// 无配置 / 读取失败 / 构建失败时**保持现状**并记日志 —— 绝不因为一次坏配置把
// 已在服务的后端打掉（fail-safe 到 env 初始化出来的那个后端）。
func applyStorageFromSettings(ctx context.Context, s *settings.Store, atomic *storage.AtomicStore) bool {
	if s == nil || atomic == nil {
		return false
	}
	cfg, err := s.LoadConfig(ctx, settingsCategoryStorage)
	if err != nil {
		slog.Warn("load storage config from settings failed", "error", err)
		return false
	}
	if len(cfg) == 0 {
		return false // 后台未配置：沿用 env 初始化出来的后端
	}
	newStore, err := buildStorageStore(cfg)
	if err != nil {
		slog.Warn("storage config in settings is invalid; keeping current backend", "error", err)
		return false
	}
	previous := atomic.Backend()
	atomic.Swap(newStore)
	slog.Info("storage backend applied from settings", "previous", previous, "current", atomic.Backend())
	return true
}

// storageConfigMap 把一次 UpdateStorage 的请求体规整为可落库 / 可广播的配置 map。
func storageConfigMap(backend, endpoint, bucket, accessKey, secretKey string, useSSL bool) map[string]interface{} {
	return map[string]interface{}{
		"backend":       backend,
		"s3_endpoint":   endpoint,
		"s3_bucket":     bucket,
		"s3_access_key": accessKey,
		"s3_secret_key": secretKey,
		"s3_use_ssl":    useSSL,
	}
}
