package api

import (
	"bufio"
	"bytes"
	"io"
	"testing"
)

// 分片上传路径必须与直传路径同口径：以**内容**（magic bytes）判定类型，
// 而不是只信 Init 阶段客户端声明的 mime_type。
//
// 攻击面：Init 时声明 image/png，实际分片内容是 HTML/SVG/PE —— 旧实现
// 完全不做内容检测，文件会以图片身份落库并经 /media/s/ 签名 URL 同源输出，
// 构成存储型 XSS / 恶意文件托管。
func TestCheckStreamTypeRejectsDisguisedContent(t *testing.T) {
	cases := []struct {
		name     string
		fileName string
		declared string
		content  []byte
		wantErr  bool
	}{
		{
			name:     "HTML 伪装成 PNG",
			fileName: "evil.html",
			declared: "image/png",
			content:  []byte("<html><body><script>alert(1)</script></body></html>"),
			wantErr:  true,
		},
		{
			name:     "SVG 伪装成 PNG（DetectContentType 判为 text/plain）",
			fileName: "evil.svg",
			declared: "image/png",
			content:  []byte(`<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>`),
			wantErr:  true,
		},
		{
			name:     "PE 伪装成 PNG（magic bytes 只判出 octet-stream，靠扩展名兜底）",
			fileName: "evil.exe",
			declared: "image/png",
			content:  append([]byte("MZ\x90\x00\x03\x00\x00\x00"), make([]byte, 600)...),
			wantErr:  true,
		},
		{
			name:     "声明本身即可执行类型",
			fileName: "payload.bin",
			declared: "application/x-msdownload",
			content:  []byte("\x89PNG\r\n\x1a\n0000000000"),
			wantErr:  true,
		},
		{
			name:     "真正的 PNG 放行",
			fileName: "ok.png",
			declared: "image/png",
			content:  []byte("\x89PNG\r\n\x1a\n0000000000000000000000"),
			wantErr:  false,
		},
		{
			name:     "普通文本放行（不误伤 .txt）",
			fileName: "notes.txt",
			declared: "text/plain",
			content:  []byte("hello world, this is a plain text note"),
			wantErr:  false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			br := bufio.NewReaderSize(bytes.NewReader(tc.content), 4096)
			err := checkStreamType(br, tc.fileName, tc.declared)
			if tc.wantErr && err == nil {
				t.Fatalf("应当拒绝，但放行了")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("应当放行，但被拒绝: %v", err)
			}

			// Peek 不得消费数据：校验后整段内容必须仍可完整读出，
			// 否则 finalize 写进对象存储的文件会缺头部。
			if tc.wantErr {
				return
			}
			rest, readErr := io.ReadAll(br)
			if readErr != nil {
				t.Fatalf("读取剩余数据失败: %v", readErr)
			}
			if !bytes.Equal(rest, tc.content) {
				t.Fatalf("Peek 破坏了数据流：读回 %d 字节，期望 %d 字节", len(rest), len(tc.content))
			}
		})
	}
}

// 小于 512 字节的文件不应因为 Peek 返回 io.EOF 而被误拒。
func TestCheckStreamTypeShortFile(t *testing.T) {
	content := []byte("\x89PNG\r\n\x1a\n")
	br := bufio.NewReaderSize(bytes.NewReader(content), 4096)
	if err := checkStreamType(br, "tiny.png", "image/png"); err != nil {
		t.Fatalf("短文件被误拒: %v", err)
	}
	rest, err := io.ReadAll(br)
	if err != nil {
		t.Fatalf("读取失败: %v", err)
	}
	if !bytes.Equal(rest, content) {
		t.Fatal("短文件内容被破坏")
	}
}
