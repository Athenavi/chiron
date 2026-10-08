package storage

import (
	"context"
	"errors"
	"io"
	"os"
	"strings"
	"sync"
	"testing"
)

// AtomicStore 的存在理由是**热切换后端时不出现"半个 store"**（atomic.Pointer 保证读写不撕裂）。
// 本包此前零测试；这里用假 FileStore 把"委托给谁"和"并发切换下读到的仍是完整后端"钉住。

type fakeStore struct {
	name  string
	calls *[]string
	err   error
}

func (f *fakeStore) record(op string) {
	if f.calls != nil {
		*f.calls = append(*f.calls, f.name+":"+op)
	}
}

func (f *fakeStore) Read(context.Context, string) ([]byte, error) {
	f.record("read")
	if f.err != nil {
		return nil, f.err
	}
	return []byte(f.name), nil
}

func (f *fakeStore) Write(context.Context, string, []byte) error {
	f.record("write")
	return f.err
}

func (f *fakeStore) WriteStream(context.Context, string, io.Reader, int64, os.FileMode) error {
	f.record("writestream")
	return f.err
}

func (f *fakeStore) OpenStream(context.Context, string) (io.ReadCloser, error) {
	f.record("openstream")
	if f.err != nil {
		return nil, f.err
	}
	return io.NopCloser(strings.NewReader(f.name)), nil
}

func (f *fakeStore) Delete(context.Context, string) error {
	f.record("delete")
	return f.err
}

func (f *fakeStore) List(context.Context, string) ([]FileInfo, error) {
	f.record("list")
	if f.err != nil {
		return nil, f.err
	}
	return []FileInfo{{Path: f.name}}, nil
}

func TestAtomicStoreDelegatesToInitialBackend(t *testing.T) {
	var calls []string
	a := NewAtomicStore(&fakeStore{name: "local", calls: &calls})
	ctx := context.Background()

	data, err := a.Read(ctx, "p")
	if err != nil || string(data) != "local" {
		t.Fatalf("Read 应委托初始后端：data=%q err=%v", data, err)
	}
	if err := a.Write(ctx, "p", []byte("x")); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if _, err := a.OpenStream(ctx, "p"); err != nil {
		t.Fatalf("OpenStream: %v", err)
	}
	if err := a.Delete(ctx, "p"); err != nil {
		t.Fatalf("Delete: %v", err)
	}
	infos, err := a.List(ctx, "p")
	if err != nil || len(infos) != 1 || infos[0].Path != "local" {
		t.Fatalf("List 应委托初始后端：%v err=%v", infos, err)
	}

	want := []string{"local:read", "local:write", "local:openstream", "local:delete", "local:list"}
	if strings.Join(calls, ",") != strings.Join(want, ",") {
		t.Fatalf("委托序列不对：\n got %v\nwant %v", calls, want)
	}
}

func TestAtomicStorePropagatesBackendErrors(t *testing.T) {
	boom := errors.New("backend down")
	a := NewAtomicStore(&fakeStore{name: "s3", err: boom})
	if _, err := a.Read(context.Background(), "p"); !errors.Is(err, boom) {
		t.Fatalf("Read 应透出后端错误，实际 %v", err)
	}
	if err := a.Delete(context.Background(), "p"); !errors.Is(err, boom) {
		t.Fatalf("Delete 应透出后端错误，实际 %v", err)
	}
}

func TestAtomicStoreSwapSwitchesBackend(t *testing.T) {
	a := NewAtomicStore(&fakeStore{name: "local"})
	if got := a.Backend(); got != "unknown" {
		t.Fatalf("假后端应报 unknown，实际 %q", got)
	}
	a.Swap(&LocalStore{Root: t.TempDir()})
	if got := a.Backend(); got != "local" {
		t.Fatalf("Swap 后应报 local，实际 %q", got)
	}
	a.Swap(&S3Store{})
	if got := a.Backend(); got != "s3" {
		t.Fatalf("Swap 后应报 s3，实际 %q", got)
	}
	if _, ok := a.LoadRaw().(*S3Store); !ok {
		t.Fatalf("LoadRaw 应给出当前 S3Store，实际 %T", a.LoadRaw())
	}
}

// 核心性质：并发读 + 反复切换后端时，每次读到的必须是**某一个完整后端**的结果
// （atomic.Pointer 的语义保证），既不能空、也不能撕裂。
func TestAtomicStoreSwapIsRaceFreeForReaders(t *testing.T) {
	a := NewAtomicStore(&fakeStore{name: "local"})
	const readers, rounds = 8, 300
	var wg sync.WaitGroup
	errs := make(chan string, readers*rounds)

	for i := 0; i < readers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < rounds; j++ {
				data, err := a.Read(context.Background(), "p")
				if err != nil {
					errs <- "read error: " + err.Error()
					continue
				}
				switch string(data) {
				case "local", "s3":
				default:
					errs <- "撕裂/空结果: " + string(data)
				}
			}
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		for j := 0; j < rounds; j++ {
			if j%2 == 0 {
				a.Swap(&fakeStore{name: "s3"})
			} else {
				a.Swap(&fakeStore{name: "local"})
			}
		}
	}()
	wg.Wait()
	close(errs)
	for msg := range errs {
		t.Fatalf("并发切换下读到异常结果：%s", msg)
	}
}
