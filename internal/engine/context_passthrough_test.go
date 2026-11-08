package engine

import (
	"encoding/json"
	"testing"
)

// PythonEvent 必须把 done 事件里的 context/budget/cache 原样透传：
// submit_handler 用 `Data: evt` 转发，未声明字段会在重新序列化时消失。
func TestPythonEventObservabilityPassthrough(t *testing.T) {
	raw := []byte(`{"type":"done","input_tokens":10,"context":{"used":10,"limit":100,"breakdown":{"system":3}},"budget":{"tokens":{"used":1,"limit":2}},"cache":{"prefix_changed":["system"]}}`)
	var ev PythonEvent
	if err := json.Unmarshal(raw, &ev); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	out, err := json.Marshal(ev)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatalf("unmarshal out: %v", err)
	}
	for _, key := range []string{"context", "budget", "cache"} {
		if got[key] == nil {
			t.Fatalf("%s lost in re-serialization: %s", key, out)
		}
	}
}
