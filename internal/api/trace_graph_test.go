package api

import (
	"testing"
	"time"
)

func span(id, parent, kind string, ts int) TraceSpan {
	return TraceSpan{
		TraceID:      "t1",
		SpanID:       id,
		ParentSpanID: parent,
		Kind:         kind,
		SpanName:     kind,
		Timestamp:    time.Unix(int64(ts), 0),
	}
}

func TestBuildTraceGraphNestsByParent(t *testing.T) {
	spans := []TraceSpan{
		span("t1:run", "", "run", 1),
		span("t1:turn:1", "t1:run", "turn", 2),
		span("t1:turn:1:llm", "t1:turn:1", "llm", 3),
		span("t1:turn:1:tool:x", "t1:turn:1", "tool", 4),
	}
	g := BuildTraceGraph("t1", "ten", spans)
	if g.NodeCount != 4 || g.OrphanCount != 0 {
		t.Fatalf("unexpected counts: %+v", g)
	}
	if len(g.Roots) != 1 {
		t.Fatalf("want single root, got %d", len(g.Roots))
	}
	root := g.Roots[0]
	if root.SpanID != "t1:run" {
		t.Fatalf("root must be the run span, got %s", root.SpanID)
	}
	if len(root.Children) != 1 || root.Children[0].SpanID != "t1:turn:1" {
		t.Fatalf("turn must be nested under run: %+v", root.Children)
	}
	if len(root.Children[0].Children) != 2 {
		t.Fatalf("llm+tool must nest under turn, got %d", len(root.Children[0].Children))
	}
}

func TestBuildTraceGraphCountsOrphans(t *testing.T) {
	spans := []TraceSpan{
		span("t1:run", "", "run", 1),
		span("t1:turn:1", "t1:missing", "turn", 2), // 父不在本 trace 内
	}
	g := BuildTraceGraph("t1", "ten", spans)
	if g.OrphanCount != 1 {
		t.Fatalf("want 1 orphan, got %d", g.OrphanCount)
	}
	// 孤儿被提升为根，仍然出现在图里（不丢节点）。
	if len(g.Roots) != 2 {
		t.Fatalf("orphan must be promoted to a root, got %d roots", len(g.Roots))
	}
}

func TestBuildTraceGraphHandlesLegacySpansWithoutIDs(t *testing.T) {
	// 历史 span 没有 span_id：按扁平处理，全部作为根 —— 不伪造父子关系。
	legacy := []TraceSpan{
		{TraceID: "t1", SpanName: "llm_call", Timestamp: time.Unix(1, 0)},
		{TraceID: "t1", SpanName: "tool:read_file", Timestamp: time.Unix(2, 0)},
	}
	g := BuildTraceGraph("t1", "ten", legacy)
	if len(g.Roots) != 2 || g.OrphanCount != 0 {
		t.Fatalf("legacy spans must be flat roots, got %+v", g)
	}
}

func TestBuildTraceGraphSortsChildrenByTime(t *testing.T) {
	spans := []TraceSpan{
		span("t1:run", "", "run", 1),
		span("t1:c2", "t1:run", "tool", 30),
		span("t1:c1", "t1:run", "tool", 10),
	}
	g := BuildTraceGraph("t1", "ten", spans)
	kids := g.Roots[0].Children
	if len(kids) != 2 || kids[0].SpanID != "t1:c1" || kids[1].SpanID != "t1:c2" {
		t.Fatalf("children must be ordered by timestamp: %+v", kids)
	}
}

func TestBuildTraceGraphEmpty(t *testing.T) {
	g := BuildTraceGraph("t1", "ten", nil)
	if g.NodeCount != 0 || len(g.Roots) != 0 {
		t.Fatalf("empty input must give an empty graph")
	}
}
