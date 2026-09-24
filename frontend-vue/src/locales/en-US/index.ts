import chat from './chat'
import errors from './errors'
import legacy from './legacy'
import auth from './auth'
import admin from './admin'

/**
 * en-US。键与 zh-CN 一一对应（zh-CN 为源语言）；缺失的键会自动回退到 zh-CN。
 * legacy 目前为空（存量抽取的模板文案待翻译），见 locales/en-US/legacy.ts 的说明。
 */
export default {
  common: {
    confirm: 'OK',
    cancel: 'Cancel',
    save: 'Save',
    delete: 'Delete',
    edit: 'Edit',
    search: 'Search',
    refresh: 'Refresh',
    loading: 'Loading…',
    empty: 'No data',
    retry: 'Retry',
    close: 'Close',
    copy: 'Copy',
    copied: 'Copied',
    language: 'Language',
    unknownError: 'Unknown error',
  },
  chat,
  errors,
  auth,
  admin,
  legacy,
}
