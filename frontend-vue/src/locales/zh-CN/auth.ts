/**
 * auth 域：登录 / 注册 / 找回密码相关文案（域划分见 ./index.ts）。
 *
 * 为什么这些文案必须显式登记在源语言域文件里，而不能沿用 legacy 的「整句即 key」：
 * vue-i18n 在 **key 不存在于 locale 时直接返回 key 原文、并跳过插值** ——
 * 于是 `$t('{s}s 后重发', { s: 60 })` 会在界面上原样显示成 `{s}s 后重发`。
 * 凡含 {占位符} 的文案，必须在域文件里登记（`common`/`errors` 等域同理）。
 */
export default {
  /** 验证码重发倒计时；{s} = 剩余秒数 */
  resendCountdown: '{s} 秒后重发',
  email: '邮箱',
  newPassword: '新密码',
  confirmNewPassword: '确认新密码',
  login: '登录',
  register: '注册',
  logout: '退出登录',
  username: '用户名',
  password: '密码',
  confirmPassword: '确认密码',
  phone: '手机号',
  verificationCode: '验证码',
  verificationCodeSent: '验证码已发送',
  verificationCodeSendFailed: '验证码发送失败',
  loginFailed: '登录失败',
  registerFailed: '注册失败',
  resetPassword: '重置密码',

}
