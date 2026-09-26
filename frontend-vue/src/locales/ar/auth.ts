/**
 * auth 域译文（ar，RTL）—— 登录 / 注册 / 找回密码。
 *
 * 键与 zh-CN/auth.ts 一一对应（scripts/check-i18n-keys.mjs 会强制校验）。
 * 含 {占位符} 的文案必须登记：vue-i18n 在键不存在时会原样返回 key 并跳过插值。
 */
export default {
  /** 验证码重发倒计时；{s} = 剩余秒数 */
  resendCountdown: 'إعادة الإرسال بعد {s} ثانية',
  email: 'البريد الإلكتروني',
  newPassword: 'كلمة المرور الجديدة',
  confirmNewPassword: 'تأكيد كلمة المرور الجديدة',
  login: 'تسجيل الدخول',
  register: 'تسجيل',
  logout: 'تسجيل الخروج',
  username: 'اسم المستخدم',
  password: 'كلمة المرور',
  confirmPassword: 'تأكيد كلمة المرور',
  phone: 'رقم الهاتف',
  verificationCode: 'رمز التحقق',
  verificationCodeSent: 'تم إرسال رمز التحقق',
  verificationCodeSendFailed: 'فشل إرسال رمز التحقق',
  loginFailed: 'فشل تسجيل الدخول',
  registerFailed: 'فشل التسجيل',
  resetPassword: 'إعادة تعيين كلمة المرور',

}
