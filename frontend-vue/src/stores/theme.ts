import { defineStore } from 'pinia'
import { ref, watch, computed } from 'vue'

import { t } from '../i18n'
export type ThemeId = 'notion' | 'paper' | 'terminal' | 'aurora'
export type ThemeMode = 'light' | 'dark'
export type ThemePreference = ThemeMode | 'system'

export interface ThemePreset {
  id: ThemeId
  name: string
  description: string
  lightTokens: Record<string, string | number>
  darkTokens: Record<string, string | number>
}

const FONT_SANS = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Helvetica Neue', Helvetica, Arial, sans-serif"

/**
 * 四套主题与 src/style.css 的 [data-theme='<id>-(light|dark)'] 一一对应。
 *
 * 这里的 antd token 只覆盖 antd 组件库自己的取值；自研组件读 style.css 里的 CSS 变量。
 * 两处必须同源：改主题色板时**同时**改 style.css 的对应块，否则会出现
 * 「antd 组件一个颜色、自研组件另一个颜色」的混搭。
 *
 * 关于 aurora：CSS 侧的面板是半透明（玻璃感），这里给 antd 的是**不透明近似色** ——
 * antd 的衍生色（hover/active/border 派生）基于输入值做颜色混合，喂 rgba 会得到不可预期的
 * 结果；因此让 antd 组件不透明、自研组件半透明，色相一致、观感协调。
 */
const THEME_REGISTRY: Record<ThemeId, ThemePreset> = {
  notion: {
    id: 'notion',
    name: t('极简功能'),
    description: t('Notion 风格，高信息密度，清晰排版'),
    lightTokens: {
      colorPrimary: '#0f172a',
      colorInfo: '#0f172a',
      borderRadius: 4,
      colorBgLayout: '#ffffff',
      colorBgContainer: '#ffffff',
      colorBgElevated: '#ffffff',
      colorBorder: '#e2e8f0',
      colorBorderSecondary: '#f1f5f9',
      colorText: '#0f172a',
      colorTextSecondary: '#475569',
      colorTextTertiary: '#64748b',
      controlOutline: 'rgba(15, 23, 42, 0.08)',
      fontFamily: FONT_SANS,
    },
    darkTokens: {
      colorPrimary: '#f1f5f9',
      colorInfo: '#f1f5f9',
      borderRadius: 4,
      colorBgLayout: '#0f172a',
      colorBgContainer: '#1e293b',
      colorBgElevated: '#1e293b',
      colorBorder: '#334155',
      colorBorderSecondary: '#1e293b',
      colorText: '#f8fafc',
      colorTextSecondary: '#cbd5e1',
      colorTextTertiary: '#94a3b8',
      controlOutline: 'rgba(241, 245, 249, 0.1)',
      fontFamily: FONT_SANS,
    },
  },
  paper: {
    id: 'paper',
    name: t('暖纸阅读'),
    description: t('暖纸底 + 琥珀强调，圆角柔和，长文护眼'),
    lightTokens: {
      colorPrimary: '#3d3630',
      colorInfo: '#3d3630',
      borderRadius: 6,
      colorBgLayout: '#fbf8f3',
      colorBgContainer: '#ffffff',
      colorBgElevated: '#ffffff',
      colorBorder: '#e6ddcf',
      colorBorderSecondary: '#efe8dc',
      colorText: '#2a241d',
      colorTextSecondary: '#5c5348',
      colorTextTertiary: '#786f61',
      controlOutline: 'rgba(61, 54, 48, 0.12)',
      fontFamily: FONT_SANS,
    },
    darkTokens: {
      colorPrimary: '#f0e9de',
      colorInfo: '#f0e9de',
      borderRadius: 6,
      colorBgLayout: '#1c1917',
      colorBgContainer: '#292524',
      colorBgElevated: '#322d28',
      colorBorder: '#3f3830',
      colorBorderSecondary: '#2f2a24',
      colorText: '#f5f0e8',
      colorTextSecondary: '#d6cbba',
      colorTextTertiary: '#b3a693',
      controlOutline: 'rgba(240, 233, 222, 0.14)',
      fontFamily: FONT_SANS,
    },
  },
  terminal: {
    id: 'terminal',
    name: t('终端硬核'),
    description: t('直角网格 + 荧光绿，终端观感'),
    lightTokens: {
      colorPrimary: '#0b1512',
      colorInfo: '#0b1512',
      borderRadius: 0,
      colorBgLayout: '#f1f3f2',
      colorBgContainer: '#ffffff',
      colorBgElevated: '#ffffff',
      colorBorder: '#d3dad6',
      colorBorderSecondary: '#e6eae8',
      colorText: '#0b1512',
      colorTextSecondary: '#3a4a44',
      colorTextTertiary: '#5c6f68',
      controlOutline: 'rgba(11, 21, 18, 0.14)',
      fontFamily: FONT_SANS,
    },
    darkTokens: {
      colorPrimary: '#34d399',
      colorInfo: '#34d399',
      borderRadius: 0,
      colorBgLayout: '#060a09',
      colorBgContainer: '#0d1412',
      colorBgElevated: '#0d1412',
      colorBorder: '#223330',
      colorBorderSecondary: '#16211e',
      colorText: '#d9f2ea',
      colorTextSecondary: '#a3c9bd',
      colorTextTertiary: '#7d9c93',
      controlOutline: 'rgba(52, 211, 153, 0.2)',
      fontFamily: FONT_SANS,
    },
  },
  aurora: {
    id: 'aurora',
    name: t('玻璃柔光'),
    description: t('半透明面板 + 低强度发光，轻盈通透'),
    lightTokens: {
      colorPrimary: '#6366f1',
      colorInfo: '#6366f1',
      borderRadius: 12,
      colorBgLayout: '#f7f8ff',
      colorBgContainer: '#ffffff',
      colorBgElevated: '#ffffff',
      colorBorder: 'rgba(99, 102, 241, 0.16)',
      colorBorderSecondary: 'rgba(99, 102, 241, 0.08)',
      colorText: '#1e1b4b',
      colorTextSecondary: '#4c4a6a',
      colorTextTertiary: '#6b6885',
      controlOutline: 'rgba(99, 102, 241, 0.2)',
      fontFamily: FONT_SANS,
    },
    darkTokens: {
      colorPrimary: '#a5b4fc',
      colorInfo: '#a5b4fc',
      borderRadius: 12,
      colorBgLayout: '#0b0a14',
      colorBgContainer: '#1a1830',
      colorBgElevated: '#1a1830',
      colorBorder: 'rgba(139, 92, 246, 0.2)',
      colorBorderSecondary: 'rgba(139, 92, 246, 0.1)',
      colorText: '#f5f3ff',
      colorTextSecondary: '#c7c4e0',
      colorTextTertiary: '#9c98b8',
      controlOutline: 'rgba(165, 180, 252, 0.22)',
      fontFamily: FONT_SANS,
    },
  },
}

export const useThemeStore = defineStore('theme', () => {
  const activeThemeId = ref<ThemeId>('notion')
  const preference = ref<ThemePreference>('system')

  const accent = ref<string | null>(null)

  const systemDarkMql = window.matchMedia('(prefers-color-scheme: dark)')

  const resolvedMode = computed<ThemeMode>(() => {
    if (preference.value === 'system') {
      return systemDarkMql.matches ? 'dark' : 'light'
    }
    return preference.value
  })

  const preset = computed<ThemePreset>(() => THEME_REGISTRY[activeThemeId.value])

  const isDark = computed<boolean>(() => resolvedMode.value === 'dark')

  const antdTokens = computed(() => {
    const tokens = isDark.value ? preset.value.darkTokens : preset.value.lightTokens
    return {
      ...tokens,
      colorPrimary: String(accent.value ?? (isDark.value ? preset.value.darkTokens.colorPrimary : preset.value.lightTokens.colorPrimary)),
      colorInfo: String(accent.value ?? (isDark.value ? preset.value.darkTokens.colorInfo : preset.value.lightTokens.colorInfo)),
    }
  })

  const cssThemeId = computed<string>(() => `${activeThemeId.value}-${resolvedMode.value}`)

  function applyTheme() {
    const root = document.documentElement
    root.setAttribute('data-theme', cssThemeId.value)
    if (isDark.value) {
      root.classList.add('dark')
    } else {
      root.classList.remove('dark')
    }
  }

  function setTheme(themeId: ThemeId) {
    activeThemeId.value = themeId
    localStorage.setItem('chiron-theme', themeId)
    applyTheme()
  }

  function setMode(newMode: ThemePreference) {
    preference.value = newMode
    localStorage.setItem('chiron-theme-pref', newMode)
    applyTheme()
  }

  function toggleMode() {
    setMode(isDark.value ? 'light' : 'dark')
  }

  // toggleTheme 是 toggleMode 的别名，供外部使用
  const toggleTheme = toggleMode

  function setAccent(color: string | null) {
    if (color && /^#[0-9a-fA-F]{6}$/.test(color)) {
      accent.value = color
      localStorage.setItem('chiron-accent', color)
    } else if (color === null) {
      accent.value = null
      localStorage.removeItem('chiron-accent')
    }
  }

  function getAvailableThemes(): ThemePreset[] {
    return Object.values(THEME_REGISTRY)
  }

  // Initialization
  function init() {
    const savedTheme = localStorage.getItem('chiron-theme')
    if (savedTheme && savedTheme in THEME_REGISTRY) {
      activeThemeId.value = savedTheme as ThemeId
    } else if (savedTheme) {
      // 旧版本写过已下线的主题 id（linear / supabase / futuristic）——
      // 回退到默认主题，并清掉脏值，免得每次启动都走一遍这条分支
      localStorage.removeItem('chiron-theme')
    }

    const savedPref = localStorage.getItem('chiron-theme-pref') as ThemePreference | null
    if (savedPref) {
      preference.value = savedPref
    }

    const savedAccent = localStorage.getItem('chiron-accent')
    if (savedAccent && /^#[0-9a-fA-F]{6}$/.test(savedAccent)) {
      accent.value = savedAccent
    }

    applyTheme()
  }

  // Watchers
  watch([activeThemeId, preference, accent], () => {
    applyTheme()
  })

  systemDarkMql.addEventListener('change', (e) => {
    if (preference.value === 'system') {
      applyTheme()
    }
  })

  return {
    activeThemeId,
    preference,
    preset,
    isDark,
    antdTokens,
    cssThemeId,
    accent,
    resolvedMode,
    setTheme,
    setMode,
    toggleMode,
    toggleTheme,
    setAccent,
    getAvailableThemes,
    init,
  }
})
