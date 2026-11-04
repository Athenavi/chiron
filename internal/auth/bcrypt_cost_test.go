package auth

import "testing"

// BCRYPT_COST 的解析口径：空串用默认值；合法区间内取该值；非整数或越界一律回落默认。
//
// 为什么值得断言：成本因子配错的两个方向都有实际代价 —— 调低等于削弱口令哈希
// （可离线暴力破解），调高等于给登录接口装了一台 DoS 放大器。越界值必须**静默回落**
// 而不是拒绝启动，否则一次手滑的部署会直接起不来。
func TestResolveBcryptCost(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want int
	}{
		{"空串取默认", "", defaultBcryptCost},
		{"区间下限", "10", 10},
		{"区间上限", "15", 15},
		{"区间中间", "13", 13},
		{"低于下限回落默认", "9", defaultBcryptCost},
		{"高于上限回落默认", "16", defaultBcryptCost},
		{"远超上限回落默认", "31", defaultBcryptCost},
		{"负数回落默认", "-1", defaultBcryptCost},
		{"零回落默认", "0", defaultBcryptCost},
		{"非整数回落默认", "twelve", defaultBcryptCost},
		{"带空格按非整数处理", " 12 ", defaultBcryptCost},
		{"小数回落默认", "12.5", defaultBcryptCost},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := resolveBcryptCost(tc.raw); got != tc.want {
				t.Fatalf("resolveBcryptCost(%q) = %d, want %d", tc.raw, got, tc.want)
			}
		})
	}
}

// 默认值本身必须落在允许区间内 —— 否则「回落默认」会把越界值重新引进来。
func TestDefaultBcryptCostWithinRange(t *testing.T) {
	if defaultBcryptCost < minBcryptCost || defaultBcryptCost > maxBcryptCost {
		t.Fatalf("defaultBcryptCost=%d 不在 [%d, %d] 内",
			defaultBcryptCost, minBcryptCost, maxBcryptCost)
	}
}
