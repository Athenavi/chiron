<script setup lang="ts">
/**
 * 个人资料页（/profile）。
 *
 * 只负责页面外壳与标题；设置内容全部来自 SettingsPanel —— 与头像弹窗共用
 * 同一份实现，两条入口不会各自漂移。
 */
import { UserOutlined } from '@ant-design/icons-vue'
import SettingsPanel from '../components/settings/SettingsPanel.vue'
</script>

<template>
  <div class="profile-container">
    <div class="profile-header">
      <UserOutlined style="font-size: 24px; color: var(--primary)" />
      <h1>{{ $t('admin.user_settings') }}</h1>
    </div>
    <SettingsPanel />
  </div>
</template>

<style scoped>
.profile-container {
  padding: 24px;
  max-width: 720px;
  margin: 0 auto;
}

.profile-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
}

.profile-header h1 {
  margin: 0;
  font-size: 24px;
  font-weight: 600;
}

@media (max-width: 640px) {
  .profile-container { padding: 16px 12px; }
  .profile-header { gap: 8px; margin-bottom: 16px; }
  .profile-header h1 { font-size: 20px; }
}

@media (max-width: 576px) {
  .profile-container { padding: 12px; }
}

@media (prefers-reduced-motion: reduce) {
  .profile-container { animation: none; transition: none; }
}
</style>
