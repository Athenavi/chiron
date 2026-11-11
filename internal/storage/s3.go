package storage

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// S3Store implements FileStore backed by an S3-compatible object store (MinIO / AWS S3).
type S3Store struct {
	client   *minio.Client
	bucket   string
	prefix   string // optional subdirectory prefix within the bucket
	endpoint string // used to construct external URLs
}

// NewS3Store creates a new S3Store connected to the given endpoint.
// endpoint: S3-compatible endpoint (e.g. "play.min.io" or "s3.amazonaws.com").
// bucket:   the bucket name (created if it doesn't exist).
// prefix:   optional prefix (subdirectory) — all paths are relative to this.
// creds:    (accessKey, secretKey, token) — token may be empty for static creds.
func NewS3Store(endpoint, bucket, prefix, accessKey, secretKey, token string, useSSL bool) (*S3Store, error) {
	if bucket == "" {
		return nil, fmt.Errorf("bucket is required")
	}

	client, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(accessKey, secretKey, token),
		Secure: useSSL,
	})
	if err != nil {
		return nil, fmt.Errorf("minio client: %w", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	// Ensure bucket exists (idempotent)
	if err := client.MakeBucket(ctx, bucket, minio.MakeBucketOptions{}); err != nil {
		// Check if bucket already exists
		exists, errExists := client.BucketExists(ctx, bucket)
		if errExists != nil {
			return nil, fmt.Errorf("ensure bucket %q: %w", bucket, errExists)
		}
		if !exists {
			return nil, fmt.Errorf("ensure bucket %q: %w", bucket, err)
		}
	}

	return &S3Store{
		client:   client,
		bucket:   bucket,
		prefix:   prefix,
		endpoint: endpoint,
	}, nil
}

func (s *S3Store) Read(ctx context.Context, path string) ([]byte, error) {
	objPath := s.objectPath(path)

	obj, err := s.client.GetObject(ctx, s.bucket, objPath, minio.GetObjectOptions{})
	if err != nil {
		return nil, fmt.Errorf("s3 get object %q: %w", objPath, err)
	}
	defer obj.Close()

	data, err := io.ReadAll(obj)
	if err != nil {
		return nil, fmt.Errorf("s3 read object %q: %w", objPath, err)
	}

	return data, nil
}

func (s *S3Store) Write(ctx context.Context, path string, data []byte) error {
	objPath := s.objectPath(path)

	r := bytes.NewReader(data)
	_, err := s.client.PutObject(ctx, s.bucket, objPath, r, int64(len(data)), minio.PutObjectOptions{
		ContentType: detectContentType(path),
	})
	if err != nil {
		return fmt.Errorf("s3 put object %q: %w", objPath, err)
	}

	return nil
}

// WriteStream 流式上传对象；size 未知传 -1（minio 自动分块）。
// perm 在对象存储语义下无意义，忽略。
func (s *S3Store) WriteStream(ctx context.Context, path string, r io.Reader, size int64, perm os.FileMode) error {
	objPath := s.objectPath(path)
	if _, err := s.client.PutObject(ctx, s.bucket, objPath, r, size, minio.PutObjectOptions{
		ContentType: detectContentType(path),
	}); err != nil {
		return fmt.Errorf("s3 put object %q: %w", objPath, err)
	}
	return nil
}

// OpenStream 返回对象读取流；先 Stat 一次，让"对象不存在"在打开阶段即暴露。
func (s *S3Store) OpenStream(ctx context.Context, path string) (io.ReadCloser, error) {
	objPath := s.objectPath(path)
	obj, err := s.client.GetObject(ctx, s.bucket, objPath, minio.GetObjectOptions{})
	if err != nil {
		return nil, fmt.Errorf("s3 get object %q: %w", objPath, err)
	}
	if _, err := obj.Stat(); err != nil {
		_ = obj.Close()
		return nil, fmt.Errorf("s3 stat object %q: %w", objPath, err)
	}
	return obj, nil
}

func (s *S3Store) Delete(ctx context.Context, path string) error {
	objPath := s.objectPath(path)

	err := s.client.RemoveObject(ctx, s.bucket, objPath, minio.RemoveObjectOptions{})
	if err != nil {
		return fmt.Errorf("s3 remove object %q: %w", objPath, err)
	}

	return nil
}

func (s *S3Store) List(ctx context.Context, prefix string) ([]FileInfo, error) {
	objPrefix := s.objectPath(prefix)

	var files []FileInfo
	for obj := range s.client.ListObjects(ctx, s.bucket, minio.ListObjectsOptions{
		Prefix:    objPrefix,
		Recursive: false,
	}) {
		if obj.Err != nil {
			return nil, fmt.Errorf("s3 list objects: %w", obj.Err)
		}
		if len(obj.Key) == 0 {
			continue
		}
		files = append(files, FileInfo{
			Path:     s.stripPrefix(obj.Key),
			Size:     obj.Size,
			IsDir:    obj.Key[len(obj.Key)-1] == '/',
			Modified: obj.LastModified.Format("2006-01-02T15:04:05Z"),
		})
	}

	if files == nil {
		files = []FileInfo{}
	}
	return files, nil
}

// PresignedPutURL returns a presigned URL for uploading an object via PUT.
// The URL expires after ttl. This enables client-side direct upload to S3.
func (s *S3Store) PresignedPutURL(ctx context.Context, path string, ttl time.Duration) (string, error) {
	objPath := s.objectPath(path)
	u, err := s.client.PresignedPutObject(ctx, s.bucket, objPath, ttl)
	if err != nil {
		return "", fmt.Errorf("presigned put url: %w", err)
	}
	return u.String(), nil
}

// PresignedGetURL returns a presigned URL for downloading an object.
// The URL expires after ttl. Enables temporary share links.
func (s *S3Store) PresignedGetURL(ctx context.Context, path string, ttl time.Duration) (string, error) {
	objPath := s.objectPath(path)
	u, err := s.client.PresignedGetObject(ctx, s.bucket, objPath, ttl, nil)
	if err != nil {
		return "", fmt.Errorf("s3 presigned get object %q: %w", objPath, err)
	}
	return u.String(), nil
}

// ObjectURL returns the public HTTP URL for an object.
func (s *S3Store) ObjectURL(path string) string {
	objPath := s.objectPath(path)
	scheme := "https"
	if s.endpoint == "localhost" || s.endpoint == "127.0.0.1" {
		scheme = "http"
	}
	return fmt.Sprintf("%s://%s/%s/%s", scheme, s.endpoint, s.bucket, objPath)
}

// objectPath joins the store prefix with the user-provided path.
func (s *S3Store) objectPath(path string) string {
	if s.prefix == "" {
		return filepath.ToSlash(path)
	}
	return filepath.ToSlash(s.prefix + "/" + path)
}

// stripPrefix removes the store prefix from an S3 object key.
func (s *S3Store) stripPrefix(key string) string {
	if s.prefix == "" {
		return key
	}
	expected := s.prefix + "/"
	if strings.HasPrefix(key, expected) {
		return key[len(expected):]
	}
	return key
}

// ObjectKeyFromURL 把 ObjectURL 生成的公开 URL 反解回**调用方视角**的对象键
// （即已去掉 store prefix，可直接喂给 OpenStream/Read/Delete）。
//
// 存在意义：调用方拿到的是 file_url 字符串，而校验/读取需要的是对象键。
// 若在 API 层自己拼 "endpoint/bucket/" 前缀，就等于把 S3Store 的内部布局
// 复制一份出去 —— 换 bucket、加 prefix 时两处会静默漂移，且校验会被绕过。
//
// 只有当 scheme 与 host 都与本 store 完全一致、且路径确实落在本 bucket 下时才返回 true；
// 其余一律 false（调用方应据此拒绝，而不是退化成「跳过校验」）。
func (s *S3Store) ObjectKeyFromURL(rawURL string) (string, bool) {
	u, err := url.Parse(rawURL)
	if err != nil || u.Scheme == "" || u.Host == "" {
		return "", false
	}
	base, err := url.Parse(s.ObjectURL(""))
	if err != nil {
		return "", false
	}
	if u.Scheme != base.Scheme || u.Host != base.Host {
		return "", false
	}
	key := strings.TrimPrefix(u.Path, "/")
	bucketPrefix := s.bucket + "/"
	if !strings.HasPrefix(key, bucketPrefix) {
		return "", false
	}
	key = strings.TrimPrefix(key, bucketPrefix)
	// 对象键里可能出现 %XX（中文文件名）；先解码再交给存储层。
	if unescaped, uerr := url.PathUnescape(key); uerr == nil {
		key = unescaped
	}
	if key == "" || strings.Contains(key, "..") {
		return "", false
	}
	return s.stripPrefix(key), true
}

func detectContentType(path string) string {
	ext := filepath.Ext(path)
	switch ext {
	case ".html", ".htm":
		return "text/html"
	case ".css":
		return "text/css"
	case ".js":
		return "application/javascript"
	case ".json":
		return "application/json"
	case ".png":
		return "image/png"
	case ".jpg", ".jpeg":
		return "image/jpeg"
	case ".gif":
		return "image/gif"
	case ".svg":
		return "image/svg+xml"
	case ".pdf":
		return "application/pdf"
	case ".md":
		return "text/markdown"
	case ".txt":
		return "text/plain"
	case ".yaml", ".yml":
		return "application/x-yaml"
	case ".zip":
		return "application/zip"
	case ".gz", ".tar.gz":
		return "application/gzip"
	default:
		return "application/octet-stream"
	}
}
