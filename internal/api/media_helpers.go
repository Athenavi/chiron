package api

import (
	"fmt"
	"net/url"
	"strconv"
)

// wouldCreateCycle reports whether moving node id under newParent would
// form a cycle (moving a node into itself or into its own descendant).
func wouldCreateCycle(getParent func(id string) (string, error), id, newParent string) (bool, error) {
	if id == newParent {
		return true, nil
	}
	// 根目录（空串）没有父节点，直接判定无环
	if newParent == "" {
		return false, nil
	}
	cur := newParent
	for i := 0; i < 100; i++ { // depth guard against corrupt data
		p, err := getParent(cur)
		if err != nil {
			return false, err
		}
		if p == id {
			return true, nil
		}
		if p == "" {
			return false, nil
		}
		cur = p
	}
	return false, fmt.Errorf("parent chain too deep")
}

// collectFolderIDs 的递归展开已下沉为一次递归 CTE（见 media_crud.go 的
// collectSubtreeIDs）：逐层一次 DB 往返在深目录 / 大批量删除时会放大成 N 次查询，
// 且"根目录 parent_id 为 NULL"的口径要散在多处维护。现在归属过滤与环消除都在 SQL 里完成。
//
// maxFolderDepth 是递归展开的深度上限，防止畸形数据（父指针成环）导致无限递归。
const maxFolderDepth = 100


// parsePagination parses page/page_size query params. Defaults 1/50, size cap 200.
func parsePagination(q url.Values) (page, pageSize int) {
	page, _ = strconv.Atoi(q.Get("page"))
	if page < 1 {
		page = 1
	}
	pageSize, _ = strconv.Atoi(q.Get("page_size"))
	if pageSize < 1 {
		pageSize = 50
	}
	if pageSize > 200 {
		pageSize = 200
	}
	return page, pageSize
}
