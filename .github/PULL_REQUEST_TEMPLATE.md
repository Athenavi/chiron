## 这个 PR 做了什么

<!-- 一句话说清。关联 issue 用 "Closes #123"。 -->

## 为什么

<!-- 解决什么问题。避免"重构"/"优化"这类无信息量描述。 -->

## 影响面（勾选并说明）

- [ ] **行为变更**：默认值 / fail-open 与 fail-closed / 对外契约
- [ ] **数据库迁移**：是否向后兼容？滚动升级期间旧副本能否与新 schema 共存？
- [ ] **多副本语义**：是否新增进程内状态、定时任务或缓存？其他副本看不到它会发生什么？
- [ ] **配置项**：新增 / 改名 / 默认值变化；是否同步了 `.env.example` 与文档？
- [ ] **安全边界**：认证 / 授权 / 租户隔离 / 输入校验

## 验证

<!-- 贴你**实际跑过**的命令与结果，不要只写"测试通过"。 -->

```bash
go build -mod=mod ./... && go vet -mod=mod ./... && go test -mod=mod ./internal/...
# 改过 .md 时：
python scripts/check_doc_links.py && python scripts/check_md_tables.py && python scripts/check_source_encoding.py
```

## 备注

<!-- 取舍、明确没做的事、后续计划。 -->
