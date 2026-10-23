/**
 * 统一提示封装（UI 精修 §8）。
 *
 * 存在理由：antd `message` 默认停留 3s，且"成功/错误"的时长散落在各组件里。这里把两档
 * 收敛成常量 —— 错误要能读完（4s），其余 2.5s 即可（§13 已定参数，优先于 §8 的初稿）。
 *
 * 不做去重：同一秒内的多条错误交给 antd 自己堆叠，去重会吞掉真正不同的信息。
 */
import { message } from 'ant-design-vue'

/** 成功 / 提醒 / 信息：2.5s */
export const NOTIFY_OK_DURATION = 2.5
/** 错误：4s —— 错误需要读完再消失 */
export const NOTIFY_ERROR_DURATION = 4

export const notify = {
  success: (content: string) => message.success({ content, duration: NOTIFY_OK_DURATION }),
  warning: (content: string) => message.warning({ content, duration: NOTIFY_OK_DURATION }),
  info: (content: string) => message.info({ content, duration: NOTIFY_OK_DURATION }),
  error: (content: string) => message.error({ content, duration: NOTIFY_ERROR_DURATION }),
}
