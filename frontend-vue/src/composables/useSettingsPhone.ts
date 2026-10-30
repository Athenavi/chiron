import { ref } from 'vue'
import { message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { getSmsBind, bindPhone, unbindPhone, sendSmsCode, isValidPhone } from '../api/auth'
import { errorStatus, serverErrorMessage } from '../utils/apiError'
import { useSmsCountdown } from './useSmsCountdown'

/**
 * 设置面板的「手机号绑定」子域。
 *
 * 从 `SettingsPanel.vue` 抽出，逻辑逐字未改。抽它的理由是这里有本面板唯一的状态机：
 * 发码成功后才启动 60 秒倒计时（以服务端下发的 `interval` 为准），倒计时期间发码按钮禁用。
 *
 * 两条口径值得记住：
 * - **`loadPhone` 失败要静默**：短信服务未配置时后端不可达，此时应把绑定入口藏起来，
 *   而不是给用户报错 —— 报错等于告诉用户"系统坏了"，实际只是没开这个功能。
 * - **发码与绑定的失败分类不同**：发码看 429（太频繁）/403（未开通短信），
 *   绑定只看 409（该号码已绑其他账号），其余走服务端原文。
 */
export function useSettingsPhone() {
  const { t } = useI18n()

  const phone = ref('')
  const phoneBound = ref(false)
  const phoneLoading = ref(false)
  const phoneForm = ref({ phone: '', code: '' })
  const phoneBinding = ref(false)
  const unbindingPhone = ref(false)
  const sendingCode = ref(false)
  const { remaining: phoneCountdown, start: startPhoneCountdown } = useSmsCountdown(60)

  async function loadPhone() {
    phoneLoading.value = true
    try {
      const res = await getSmsBind()
      phone.value = res.phone || ''
      phoneBound.value = !!res.bound
    } catch {
      // 短信服务未配置/不可达时静默隐藏绑定表单
      phoneBound.value = false
    } finally {
      phoneLoading.value = false
    }
  }

  async function handleSendBindCode() {
    const p = phoneForm.value.phone.trim()
    if (!isValidPhone(p)) {
      message.warning(t('common.please_enter_a_valid_phone_number'))
      return
    }
    sendingCode.value = true
    try {
      const res = await sendSmsCode({ phone: p, purpose: 'bind' })
      startPhoneCountdown(res.interval || 60)
      message.success(t('auth.verificationCodeSent'))
    } catch (e) {
      const status = errorStatus(e)
      const apiErr = serverErrorMessage(e, '')
      if (status === 429) {
        message.error(t('common.sending_too_frequently_please_try_again_later'))
      } else if (status === 403) {
        message.error(apiErr || t('common.sms_service_not_enabled'))
      } else {
        message.error(apiErr || t('auth.verificationCodeSendFailed'))
      }
    } finally {
      sendingCode.value = false
    }
  }

  async function handleBindPhone() {
    const { phone: p, code } = phoneForm.value
    if (!isValidPhone(p) || !code.trim()) {
      message.warning(t('auth.please_enter_phone_number_and_verification_code'))
      return
    }
    phoneBinding.value = true
    try {
      await bindPhone({ phone: p.trim(), code: code.trim() })
      message.success(t('common.phone_number_bound_successfully'))
      phoneForm.value = { phone: '', code: '' }
      await loadPhone()
    } catch (e) {
      const status = errorStatus(e)
      const apiErr = serverErrorMessage(e, '')
      if (status === 409) {
        message.error(t('auth.this_phone_number_is_bound_to_another_account'))
      } else {
        message.error(apiErr || t('errors.binding_failed'))
      }
    } finally {
      phoneBinding.value = false
    }
  }

  async function handleUnbindPhone() {
    unbindingPhone.value = true
    try {
      await unbindPhone()
      message.success(t('common.phone_number_unbound'))
      await loadPhone()
    } catch (e) {
      message.error(serverErrorMessage(e, t('errors.failed_to_unbind')))
    } finally {
      unbindingPhone.value = false
    }
  }

  return {
    phone,
    phoneBound,
    phoneLoading,
    phoneForm,
    phoneBinding,
    unbindingPhone,
    sendingCode,
    phoneCountdown,
    loadPhone,
    handleSendBindCode,
    handleBindPhone,
    handleUnbindPhone,
  }
}
