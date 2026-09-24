import chat from './chat'
import errors from './errors'
import legacy from './legacy'
import auth from './auth'
import admin from './admin'

/**
 * zh-CN —— **源语言（source of truth）**。
 *
 * 约定：
 *   1) 新增文案先写这里，再补 en-US / ar（缺失时 vue-i18n 会回退到本文件）；
 *   2) 域（domain）按业务划分，与迁移批次对应：common / errors / chat / legacy /
 *      settings / admin / auth …；域文件变多时拆分为 locales/zh-CN/<domain>.ts；
 *   3) key 用 `<域>.<语义>`（如 `common.save`、`errors.quota_exceeded`），
 *      禁止把整句话当 key，也禁止在模板里拼接语义片段。
 *   4) `legacy` 是**存量自动抽取**域（原文即 key，gettext 风格）：新文案不要往里加；
 *      它的语义化改造是独立任务（只改 key，不动文案）。
 */
export default {
  common: {
    confirm: '确定',
    cancel: '取消',
    save: '保存',
    delete: '删除',
    edit: '编辑',
    search: '搜索',
    refresh: '刷新',
    loading: '加载中…',
    empty: '暂无数据',
    retry: '重试',
    close: '关闭',
    copy: '复制',
    copied: '已复制',
    language: '语言',
    unknownError: '未知错误',
  },
  chat,
  errors,
  auth,
  admin,
  legacy,
}
