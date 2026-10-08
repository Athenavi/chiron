package monitor

import (
	"context"
	"encoding/hex"
	"errors"
	"testing"
)

// trace 是**手写**的（不是 OTel 依赖）：ID 生成、父子传播、结束导出。
// 这里钉住"链路会不会断"与"导出是否真的被调用"，并在每处用到全局量时还原现场。

func restoreTracer(t *testing.T) {
	t.Helper()
	prev := GlobalTracer
	t.Cleanup(func() { GlobalTracer = prev })
}

func TestStartRootSpanHasNoParentAndNonZeroIDs(t *testing.T) {
	ctx, span := StartSpan(context.Background(), "root-op", "server")
	if span == nil {
		t.Fatal("StartSpan 返回 nil span")
	}
	if GetSpan(ctx) != span {
		t.Fatal("上下文里应挂着新 span")
	}
	if span.ParentID != (SpanID{}) {
		t.Fatalf("根 span 不应有父：%v", span.ParentID)
	}
	if span.TraceID == (TraceID{}) || span.SpanID == (SpanID{}) {
		t.Fatal("根 span 的 ID 不应全零")
	}
	if len(span.TraceID.String()) != 32 || len(span.SpanID.String()) != 16 {
		t.Fatalf("ID 十六进制长度不对：trace=%q span=%q", span.TraceID, span.SpanID)
	}
	if _, err := hex.DecodeString(span.TraceID.String()); err != nil {
		t.Fatalf("trace id 不是合法 hex：%v", err)
	}
	if span.Tags == nil {
		t.Fatal("Tags 必须已初始化（否则 SetTag 会 panic）")
	}
	span.SetTag("k", "v") // 不应 panic
	if span.Tags["k"] != "v" {
		t.Fatalf("SetTag 未生效：%v", span.Tags)
	}
}

func TestStartSpanInheritsTraceAndLinksParent(t *testing.T) {
	ctx, parent := StartSpan(context.Background(), "parent", "server")
	_, child := StartSpan(ctx, "child", "internal")

	if child.TraceID != parent.TraceID {
		t.Fatalf("子 span 必须继承同一个 trace：%v vs %v", child.TraceID, parent.TraceID)
	}
	if child.ParentID != parent.SpanID {
		t.Fatalf("子 span 的 ParentID 应等于父 span.SpanID：%v vs %v", child.ParentID, parent.SpanID)
	}
	if child.SpanID == parent.SpanID {
		t.Fatal("父子 span 不应共用 span id")
	}
}

func TestGetSpanAndTraceIDFromContext(t *testing.T) {
	if GetSpan(nil) != nil {
		t.Fatal("nil context 应返回 nil span")
	}
	if got := TraceIDFromContext(context.Background()); got != "" {
		t.Fatalf("无 span 的上下文应返回空 trace id，实际 %q", got)
	}

	ctx, span := StartSpan(context.Background(), "op", "internal")
	if got := TraceIDFromContext(ctx); got != span.TraceID.String() {
		t.Fatalf("trace id 不一致：%q vs %q", got, span.TraceID.String())
	}
}

func TestSpanEndSetsDefaultsAndExports(t *testing.T) {
	restoreTracer(t)
	var exported []*Span
	GlobalTracer = NewTracer(func(s *Span) { exported = append(exported, s) })

	_, span := StartSpan(context.Background(), "op", "internal")
	span.End()

	if len(exported) != 1 || exported[0] != span {
		t.Fatalf("End 必须导出一次该 span，实际导出 %d 个", len(exported))
	}
	if span.EndTime.IsZero() {
		t.Fatal("End 应设置 EndTime")
	}
	if span.StatusCode != "OK" {
		t.Fatalf("未显式设状态时应默认 OK，实际 %q", span.StatusCode)
	}

	// 显式设过状态后不得被覆盖
	_, span2 := StartSpan(context.Background(), "op2", "internal")
	span2.SetStatus("ERROR", "boom")
	span2.End()
	if span2.StatusCode != "ERROR" || span2.StatusMsg != "boom" {
		t.Fatalf("显式状态被覆盖：%q/%q", span2.StatusCode, span2.StatusMsg)
	}
}

func TestTraceWrapsFunctionAndRecordsStatus(t *testing.T) {
	restoreTracer(t)
	var exported []*Span
	GlobalTracer = NewTracer(func(s *Span) { exported = append(exported, s) })

	boom := errors.New("kaboom")
	err := Trace(context.Background(), "failing", "internal", func(context.Context) error { return boom })
	if !errors.Is(err, boom) {
		t.Fatalf("Trace 应原样返回业务错误，实际 %v", err)
	}
	if len(exported) != 1 || exported[0].StatusCode != "ERROR" || exported[0].StatusMsg != "kaboom" {
		t.Fatalf("失败调用应记 ERROR + 原因，实际 %+v", exported[0])
	}

	exported = nil
	if err := Trace(context.Background(), "ok", "internal", func(context.Context) error { return nil }); err != nil {
		t.Fatalf("成功调用不应返回错误：%v", err)
	}
	if len(exported) != 1 || exported[0].StatusCode != "OK" {
		t.Fatalf("成功调用应记 OK，实际 %+v", exported[0])
	}
}

func TestSpanAddEvent(t *testing.T) {
	_, span := StartSpan(context.Background(), "op", "internal")
	span.AddEvent("retry", map[string]interface{}{"attempt": 2})
	if len(span.Events) != 1 || span.Events[0].Name != "retry" {
		t.Fatalf("AddEvent 未生效：%+v", span.Events)
	}
	if span.Events[0].Timestamp.IsZero() {
		t.Fatal("事件应带时间戳")
	}
}

func TestCompletedSpansStoreAndLimit(t *testing.T) {
	completedSpansMu.Lock()
	prevSpans, prevMax := completedSpans, maxCompletedSpans
	completedSpans = nil
	maxCompletedSpans = 3
	completedSpansMu.Unlock()
	t.Cleanup(func() {
		completedSpansMu.Lock()
		completedSpans, maxCompletedSpans = prevSpans, prevMax
		completedSpansMu.Unlock()
	})

	for i := 0; i < 5; i++ {
		SpanExport(&Span{Name: "s" + string(rune('0'+i))})
	}

	all := GetCompletedSpans(0)
	if len(all) != 3 {
		t.Fatalf("容量 3 应只留 3 个 span，实际 %d", len(all))
	}
	if all[0].Name != "s2" || all[2].Name != "s4" {
		t.Fatalf("应保留最新 3 个（s2/s3/s4），实际 %q..%q", all[0].Name, all[2].Name)
	}
	if got := GetCompletedSpans(1); len(got) != 1 || got[0].Name != "s4" {
		t.Fatalf("limit=1 应取最新一个，实际 %+v", got)
	}
	if got := GetCompletedSpans(99); len(got) != 3 {
		t.Fatalf("limit 超界应返回全部，实际 %d", len(got))
	}
}

func TestIDGeneratorStaysUnique(t *testing.T) {
	traces := make(map[string]struct{}, 2000)
	spans := make(map[string]struct{}, 2000)
	for i := 0; i < 2000; i++ {
		tr := newTraceID().String()
		sp := newSpanID().String()
		if _, dup := traces[tr]; dup {
			t.Fatalf("trace id 重复：%s", tr)
		}
		if _, dup := spans[sp]; dup {
			t.Fatalf("span id 重复：%s", sp)
		}
		traces[tr] = struct{}{}
		spans[sp] = struct{}{}
	}
}
