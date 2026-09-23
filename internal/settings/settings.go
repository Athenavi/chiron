// Package settings 提供一个基于 AES-256-GCM 的加密设置存取层。
//
// 功能：
//   - 用部署主密钥（APP_SECRET 派生密钥）对敏感配置（LLM/S3/支付密钥、redis/pg 密码等）
//     做加密后存入 system_settings 表；非敏感配置明文存储。
//   - 提供按分类的 SaveConfig / LoadConfig，供后台「系统设置」与管理端 API 使用。
package settings

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"
)

// 密文格式前缀：`v1:` + base64(nonce || ciphertext)
const (
	cipherVersion = "v1:"
	nonceSize     = 12
)

var (
	// ErrEncryptedKeyNotFound APP_SECRET 为空或无法派生密钥时返回。
	ErrEncryptedKeyNotFound = errors.New("settings encryption key unavailable (APP_SECRET not set)")
)

// Store 为 system_settings 表提供带加密的读写。
type Store struct {
	pool *pgxpool.Pool
	aead cipher.AEAD // 由 APP_SECRET 派生；nil 表示未初始化加密（仅允许非敏感存取）
}

// New 创建 Store。appSecret 为空时返回一个仅支持非敏感存取的 Store（敏感键写入会报错）。
func New(pool *pgxpool.Pool, appSecret string) *Store {
	return &Store{
		pool: pool,
		aead: newAEAD(appSecret),
	}
}

// EncryptEnabled 报告当前主密钥是否可用（可加密敏感配置）。
func (s *Store) EncryptEnabled() bool { return s.aead != nil }

// nullableUser 将空 userID 转为 NULL，避免 uuid 列绑定空字符串报错。
func nullableUser(userID string) interface{} {
	if userID == "" {
		return nil
	}
	return userID
}

// newAEAD 从 APP_SECRET 派生 AES-GCM AEAD；秘密为空返回 nil。
func newAEAD(appSecret string) cipher.AEAD {
	if appSecret == "" {
		return nil
	}
	key := sha256.Sum256([]byte("chiron-settings:" + appSecret))
	block, err := aes.NewCipher(key[:])
	if err != nil {
		return nil
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil
	}
	return aead
}

// EncryptString 将明文编码为可落库的密文串。
func (s *Store) EncryptString(plain string) (string, error) {
	if s.aead == nil {
		return "", ErrEncryptedKeyNotFound
	}
	nonce := make([]byte, nonceSize)
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return "", err
	}
	sealed := s.aead.Seal(nil, nonce, []byte(plain), nil)
	raw := append(nonce, sealed...)
	return cipherVersion + base64.StdEncoding.EncodeToString(raw), nil
}

// DecryptString 解密 settings.EncryptString 产生的密文。
func (s *Store) DecryptString(enc string) (string, error) {
	if s.aead == nil {
		return "", ErrEncryptedKeyNotFound
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(enc, cipherVersion))
	if err != nil {
		return "", err
	}
	if len(raw) < nonceSize {
		return "", errors.New("invalid ciphertext")
	}
	nonce, sealed := raw[:nonceSize], raw[nonceSize:]
	plain, err := s.aead.Open(nil, nonce, sealed, nil)
	if err != nil {
		return "", err
	}
	return string(plain), nil
}

// IsSensitive 判断某个配置键是否为敏感值（需加密入库）。
// 依据：键名包含 password / secret / private_key / api_key / token / dsn。
func IsSensitive(key string) bool {
	k := strings.ToLower(key)
	for _, needle := range []string{"password", "secret", "private_key", "api_key", "dsn", "token"} {
		if strings.Contains(k, needle) {
			return true
		}
	}
	return false
}

// SaveConfig 将某分类的 config 逐键 upsert 到 system_settings。
//   - val 为 nil：删除该键（回落 env/默认值）
//   - 敏感键且 aead 可用：加密后落库，标记 encrypted=true
func (s *Store) SaveConfig(ctx context.Context, category string, config map[string]interface{}, userID string) error {
	if s.pool == nil {
		return errors.New("database unavailable")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	for key, val := range config {
		if val == nil {
			if _, err := tx.Exec(ctx,
				`DELETE FROM system_settings WHERE category=$1 AND key=$2`,
				category, key); err != nil {
				return err
			}
			continue
		}
		valueJSON, err := json.Marshal(val)
		if err != nil {
			return err
		}
		encrypted := false
		value := string(valueJSON)
		if sensitive := IsSensitive(key); sensitive {
			if s.aead == nil {
				return ErrEncryptedKeyNotFound
			}
			enc, err := s.EncryptString(value)
			if err != nil {
				return err
			}
			// 密文以 JSON 字符串形态入库（value 列为 jsonb，裸串无法强转）
			encJSON, err := json.Marshal(enc)
			if err != nil {
				return err
			}
			value = string(encJSON)
			encrypted = true
		}
		// upsert（先 UPDATE，未命中再 INSERT）——**不依赖任何唯一约束**。
		//
		// 这里曾用 `INSERT ... ON CONFLICT (category, key) DO UPDATE`。但 system_settings
		// 的实际表结构（见 migrations/sql/init.sql 与生产库导出）只有 id 主键，
		// (category, key) 上并没有唯一约束，Postgres 于是直接报
		//   42P10: there is no unique or exclusion constraint matching the ON CONFLICT specification
		// 导致**所有**保存路径（后台「系统设置」、后台「支付配置」、LLM Key 等）全军覆没。
		// 条件插入不要求任何 DDL：应用启动不做 schema 变更（改为由发布流程/DBA 管理），
		// 因此这里不能靠"补一个唯一索引"来修，只能不依赖它。
		//
		// 保留 `$3::jsonb`：value 列历史上是 json（不是 jsonb），裸参数会被推断成 text 而报
		//   column "value" is of type json but expression is of type text
		// 显式 cast 到 jsonb 后由 Postgres 做 jsonb→json 的赋值转换，json / jsonb 两种列都能写。
		//
		// 注意：极端并发（同一键首次写入被两个副本同时插入）仍可能产生两行，
		// 因为不再有唯一约束兜底。若需要强保证，请由发布流程补唯一索引后改回 ON CONFLICT。
		if _, err := tx.Exec(ctx,
			`WITH updated AS (
			     UPDATE system_settings
			        SET value=$3::jsonb, encrypted=$4, updated_by=$5, updated_at=NOW()
			      WHERE category=$1 AND key=$2
			     RETURNING 1
			 )
			 INSERT INTO system_settings (category, key, value, encrypted, updated_by, updated_at)
			 SELECT $1, $2, $3::jsonb, $4, $5, NOW()
			  WHERE NOT EXISTS (SELECT 1 FROM updated)`,
			category, key, value, encrypted, nullableUser(userID)); err != nil {
			return fmt.Errorf("upsert setting %s/%s: %w", category, key, err)
		}
	}
	return tx.Commit(ctx)
}

// LoadConfig 读取某分类全部配置，敏感值解密后返回（面向管理员，需鉴权）。
func (s *Store) LoadConfig(ctx context.Context, category string) (map[string]interface{}, error) {
	out := map[string]interface{}{}
	if s.pool == nil {
		return out, nil
	}
	rows, err := s.pool.Query(ctx,
		`SELECT key, value, encrypted FROM system_settings WHERE category=$1`, category)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		var key string
		var raw json.RawMessage
		var encrypted bool
		if err := rows.Scan(&key, &raw, &encrypted); err != nil {
			continue
		}
		// 落库形态是 json.Marshal(val) 的结果（敏感键再整体加密一层），因此必须按
		// JSON 反序列化还原。**不能**用"去掉外层引号"来替代反序列化：那会把值内的
		// 转义序列变成字面字符 —— 典型受害者是 PEM 私钥/公钥，读回后变成
		// "-----BEGIN PRIVATE KEY-----\nMIIE..."（反斜杠 + n，而非真实换行），
		// 于是支付渠道客户端构造时 PEM 解析失败、渠道静默不可用。
		encoded := raw
		if encrypted {
			plain, err := s.DecryptString(strings.Trim(string(raw), `"`))
			if err != nil {
				slog.Warn("settings decrypt failed", "category", category, "key", key, "error", err)
				continue // 无法解密则不返回该键（避免把密文当明文展示）
			}
			encoded = json.RawMessage(plain)
		}
		var val interface{}
		if err := json.Unmarshal(encoded, &val); err != nil {
			// 兼容历史上以裸字符串落库的值
			val = strings.Trim(string(encoded), `"`)
		}
		out[key] = val
	}
	return out, nil
}
