package api

import (
	"errors"
	"strings"
	"testing"
)

// 长路径解析的单元用例。
//
// 为什么这些用例值得单独写：`splitAssetPath` 是"模型给的字符串"与"数据库里的层级"
// 之间**唯一**的边界。它错了不会报错，只会安静地落错层级 —— 而"长路径没正确处理"
// 正是 2026-11-03 生产报告的原话。
func TestSplitAssetPath(t *testing.T) {
	t.Run("把路径拆成文件夹链与文件名", func(t *testing.T) {
		folders, name, err := splitAssetPath("reports/2024/q3.md")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if name != "q3.md" {
			t.Errorf("name = %q, want %q", name, "q3.md")
		}
		if got := strings.Join(folders, "/"); got != "reports/2024" {
			t.Errorf("folders = %q, want %q", got, "reports/2024")
		}
	})

	t.Run("纯文件名没有文件夹链", func(t *testing.T) {
		folders, name, err := splitAssetPath("README.md")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(folders) != 0 {
			t.Errorf("folders = %v, want empty", folders)
		}
		if name != "README.md" {
			t.Errorf("name = %q", name)
		}
	})

	t.Run("允许中文与空格（文件名是用户内容，不该被收紧正则误伤）", func(t *testing.T) {
		folders, name, err := splitAssetPath("季度报告/2024 Q3 总结.md")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(folders) != 1 || folders[0] != "季度报告" {
			t.Errorf("folders = %v", folders)
		}
		if name != "2024 Q3 总结.md" {
			t.Errorf("name = %q", name)
		}
	})

	t.Run("去掉首尾空白", func(t *testing.T) {
		_, name, err := splitAssetPath("  a/b.md  ")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if name != "b.md" {
			t.Errorf("name = %q", name)
		}
	})

	// 以下每一条都对应一种真实危害，不是凑数的边界。
	invalid := []struct {
		name string
		path string
		why  string
	}{
		{"空串", "", "没有文件名"},
		{"仅空白", "   ", "没有文件名"},
		{"反斜杠", `reports\q3.md`, "同一路径在 Windows/POSIX 拆法不同，会落成两处"},
		{"绝对路径", "/etc/passwd.md", "越出媒体库根"},
		{"父目录穿越", "../secret.md", "'..' 是路径穿越的经典形态"},
		{"中间父目录", "a/../../b.md", "同上"},
		{"当前目录", "./a.md", "无意义，且暗示调用方把路径当文件系统路径"},
		{"连续斜杠", "a//b.md", "实际层级与用户看到的段数不符"},
		{"尾随斜杠", "reports/2024/", "没有文件名，无法落成资产"},
		{"单斜杠", "/", "没有文件名"},
		{"控制字符", "a/\x01b.md", "污染日志与下载头"},
		{"DEL", "a/\x7fb.md", "同上"},
	}
	for _, tc := range invalid {
		t.Run("拒绝:"+tc.name, func(t *testing.T) {
			_, _, err := splitAssetPath(tc.path)
			if err == nil {
				t.Fatalf("splitAssetPath(%q) 应当拒绝（%s），却返回成功", tc.path, tc.why)
			}
			if !errors.Is(err, errInvalidAssetPath) {
				t.Errorf("错误应可用 errors.Is 判定为 errInvalidAssetPath，got %v", err)
			}
		})
	}

	t.Run("拒绝超深层级", func(t *testing.T) {
		deep := strings.Repeat("d/", maxAssetFolderDepth+1) + "f.md"
		if _, _, err := splitAssetPath(deep); err == nil {
			t.Fatalf("超过 %d 层应当被拒绝", maxAssetFolderDepth)
		}
	})

	t.Run("恰好达到深度上限应当接受", func(t *testing.T) {
		atLimit := strings.Repeat("d/", maxAssetFolderDepth) + "f.md"
		folders, _, err := splitAssetPath(atLimit)
		if err != nil {
			t.Fatalf("恰好 %d 层应当接受，got %v", maxAssetFolderDepth, err)
		}
		if len(folders) != maxAssetFolderDepth {
			t.Errorf("folder depth = %d, want %d", len(folders), maxAssetFolderDepth)
		}
	})

	t.Run("拒绝超长单段", func(t *testing.T) {
		long := strings.Repeat("x", maxAssetSegmentChars+1) + ".md"
		if _, _, err := splitAssetPath(long); err == nil {
			t.Fatalf("超过 %d 字符的段应当被拒绝", maxAssetSegmentChars)
		}
	})

	t.Run("错误信息截短超长段，不把整行日志吞掉", func(t *testing.T) {
		long := strings.Repeat("x", 2000)
		_, _, err := splitAssetPath("a/" + long)
		if err == nil {
			t.Fatal("应当报错")
		}
		if len([]rune(err.Error())) > 300 {
			t.Errorf("错误信息过长（%d 字符），会撑爆日志行", len([]rune(err.Error())))
		}
	})
}

// 文件夹路径解析。与 `splitAssetPath` 的关键差异：**整串都是目录**，且允许尾随斜杠。
//
// 这正是"文件夹创建没被正确处理"的修复点：`POST /v1/media/folders` 原先直接把
// `a/b/c` 当一个名字含斜杠的目录插库，用户在媒体库里看到一个点不开的怪条目。
func TestSplitFolderPath(t *testing.T) {
	t.Run("多级路径拆成目录段", func(t *testing.T) {
		parts, err := splitFolderPath("reports/2024/q3")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if got := strings.Join(parts, "/"); got != "reports/2024/q3" {
			t.Errorf("parts = %q", got)
		}
		if len(parts) != 3 {
			t.Errorf("段数 = %d, 期望 3（每一段都是一级目录）", len(parts))
		}
	})

	t.Run("单级目录", func(t *testing.T) {
		parts, err := splitFolderPath("inbox")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(parts) != 1 || parts[0] != "inbox" {
			t.Errorf("parts = %v", parts)
		}
	})

	t.Run("允许尾随斜杠（复制来的路径常带）", func(t *testing.T) {
		// 与 splitAssetPath 相反：建目录时 `reports/2024/` 是合法表达
		parts, err := splitFolderPath("reports/2024/")
		if err != nil {
			t.Fatalf("尾随斜杠应被接受（只是冗余表达），got %v", err)
		}
		if got := strings.Join(parts, "/"); got != "reports/2024" {
			t.Errorf("parts = %q", got)
		}
	})

	t.Run("允许中文与空格", func(t *testing.T) {
		parts, err := splitFolderPath("季度报告/2024 Q3")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(parts) != 2 || parts[0] != "季度报告" {
			t.Errorf("parts = %v", parts)
		}
	})

	// 与文件路径共用同一套段校验，因此下面这些必须同样被拒。
	invalid := []struct {
		name string
		path string
	}{
		{"空串", ""},
		{"仅空白", "   "},
		{"仅斜杠", "/"},
		{"反斜杠", `a\b`},
		{"绝对路径", "/etc"},
		{"父目录穿越", "../x"},
		{"中间父目录", "a/../../b"},
		{"当前目录", "./a"},
		{"连续斜杠", "a//b"},
		{"控制字符", "a/\x01b"},
	}
	for _, tc := range invalid {
		t.Run("拒绝:"+tc.name, func(t *testing.T) {
			if _, err := splitFolderPath(tc.path); err == nil {
				t.Fatalf("splitFolderPath(%q) 应当拒绝", tc.path)
			} else if !errors.Is(err, errInvalidAssetPath) {
				t.Errorf("应可用 errors.Is 判定，got %v", err)
			}
		})
	}

	t.Run("拒绝超深层级", func(t *testing.T) {
		deep := strings.TrimSuffix(strings.Repeat("d/", maxAssetFolderDepth+1), "/")
		if _, err := splitFolderPath(deep); err == nil {
			t.Fatalf("超过 %d 层应当被拒绝", maxAssetFolderDepth)
		}
	})

	t.Run("恰好达到深度上限应当接受", func(t *testing.T) {
		atLimit := strings.TrimSuffix(strings.Repeat("d/", maxAssetFolderDepth), "/")
		parts, err := splitFolderPath(atLimit)
		if err != nil {
			t.Fatalf("恰好 %d 层应当接受，got %v", maxAssetFolderDepth, err)
		}
		if len(parts) != maxAssetFolderDepth {
			t.Errorf("段数 = %d", len(parts))
		}
	})
}
