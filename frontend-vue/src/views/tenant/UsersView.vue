<script setup lang="ts">
/**
 * 租户后台 · 用户管理。
 *
 * 租户管理员查看本租户成员，并为成员分配角色/群组。数据来自 `/v1/ent/users`
 * （后端按 claims.tenant_id 过滤），所以这里不需要、也不应该传 tenant_id。
 */
import { computed, onMounted, ref } from 'vue'
import { Button, Card, Input, InputNumber, Modal, Select, Space, Table, Tag, message } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  listGroups,
  listRoles,
  listUsers,
  setUserGroups,
  setUserRoles,
  type EntGroup,
  type EntRole,
  type EntUser,
} from '../../api/enterprise'
import {
  createTenantInvite,
  listTenantInvites,
  revokeTenantInvite,
  type TenantInvite,
} from '../../api/admin'
import { serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()

const loading = ref(false)
const rows = ref<EntUser[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const search = ref('')
const roles = ref<EntRole[]>([])
const groups = ref<EntGroup[]>([])

const editing = ref<EntUser | null>(null)
const editRoleIds = ref<string[]>([])
const editGroupIds = ref<string[]>([])
const saving = ref(false)

const invites = ref<TenantInvite[]>([])
const inviteLoading = ref(false)
const inviteCreating = ref(false)
const inviteMaxUses = ref(1)
const inviteExpiresDays = ref(7)

interface NamedRef {
  id: string
  name: string
  display_name?: string
}

/** 后端 roles/groups 是 json_agg 出来的对象数组；脏数据直接跳过。 */
function refsOf(value: unknown): NamedRef[] {
  if (!Array.isArray(value)) return []
  return value.filter(
    (x): x is NamedRef => !!x && typeof x === 'object' && typeof (x as NamedRef).id === 'string',
  )
}

const columns = computed(() => [
  { title: t('admin.user_email'), dataIndex: 'email', key: 'email', ellipsis: true },
  { title: t('admin.user_name'), dataIndex: 'name', key: 'name', width: 160 },
  { title: t('admin.role'), key: 'roles', width: 240 },
  { title: t('admin.group'), key: 'groups', width: 220 },
  { title: t('common.action'), key: 'actions', width: 120, fixed: 'right' as const },
])

const roleOptions = computed(() => roles.value.map(r => ({
  value: r.id,
  label: r.display_name || r.name,
})))
const groupOptions = computed(() => groups.value.map(g => ({ value: g.id, label: g.name })))

async function load() {
  loading.value = true
  try {
    const res = await listUsers(search.value.trim(), page.value, pageSize.value)
    rows.value = res.data
    total.value = res.total
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.failed_to_load')))
  } finally {
    loading.value = false
  }
}

async function loadOptions() {
  try {
    const [r, g] = await Promise.all([listRoles(), listGroups()])
    roles.value = r
    groups.value = g
  } catch {
    roles.value = []
    groups.value = []
  }
}

function openEdit(record: EntUser) {
  editing.value = record
  editRoleIds.value = refsOf(record.roles).map(r => r.id)
  editGroupIds.value = refsOf(record.groups).map(g => g.id)
}

function closeEdit() {
  editing.value = null
}

async function save() {
  const user = editing.value
  if (!user) return
  saving.value = true
  try {
    await Promise.all([
      setUserRoles(user.id, editRoleIds.value),
      setUserGroups(user.id, editGroupIds.value),
    ])
    message.success(t('common.saved'))
    editing.value = null
    await load()
  } catch (e) {
    message.error(serverErrorMessage(e, t('common.save_failed')))
  } finally {
    saving.value = false
  }
}

function onSearch() {
  page.value = 1
  void load()
}

function onPageChange(p: number) {
  page.value = p
  void load()
}

const inviteColumns = computed(() => [
  { title: t('admin.invite_code'), dataIndex: 'code', key: 'code', width: 220 },
  { title: t('admin.invite_uses'), key: 'uses', width: 120 },
  { title: t('admin.invite_expires_at'), dataIndex: 'expires_at', key: 'expires_at', width: 180 },
  { title: t('common.action'), key: 'actions', width: 180 },
])

async function loadInvites() {
  inviteLoading.value = true
  try {
    invites.value = await listTenantInvites()
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.failed_to_load')))
  } finally {
    inviteLoading.value = false
  }
}

async function createInvite() {
  inviteCreating.value = true
  try {
    await createTenantInvite(inviteMaxUses.value, inviteExpiresDays.value)
    message.success(t('admin.invite_created'))
    await loadInvites()
  } catch (e) {
    message.error(serverErrorMessage(e, t('common.save_failed')))
  } finally {
    inviteCreating.value = false
  }
}

async function revokeInvite(code: string) {
  try {
    await revokeTenantInvite(code)
    message.success(t('admin.invite_revoked'))
    await loadInvites()
  } catch (e) {
    message.error(serverErrorMessage(e, t('common.save_failed')))
  }
}

async function copyInviteLink(code: string) {
  const link = `${window.location.origin}/register?invite=${encodeURIComponent(code)}`
  try {
    await navigator.clipboard.writeText(link)
    message.success(t('admin.invite_copied'))
  } catch {
    message.info(link)
  }
}

onMounted(() => {
  void load()
  void loadOptions()
  void loadInvites()
})
</script>

<template>
  <div class="tenant-users">
    <div class="users-header">
      <div>
        <h2 class="page-title">
          {{ $t('admin.user_management') }}
        </h2>
        <p class="page-desc">
          {{ $t('admin.user_management_desc') }}
        </p>
      </div>
      <Space>
        <Input
          v-model:value="search"
          :placeholder="$t('admin.search_users')"
          allow-clear
          style="width: 240px"
          @press-enter="onSearch"
        />
        <Button
          :loading="loading"
          @click="onSearch"
        >
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('common.refresh') }}
        </Button>
      </Space>
    </div>

    <Table
      :columns="columns"
      :data-source="rows"
      :loading="loading"
      :pagination="{ current: page, pageSize, total, showSizeChanger: false, onChange: onPageChange }"
      row-key="id"
      size="small"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'roles'">
          <Tag
            v-for="r in refsOf(record.roles)"
            :key="r.id"
            color="blue"
          >
            {{ r.display_name || r.name }}
          </Tag>
          <span
            v-if="!refsOf(record.roles).length"
            class="muted"
          >-</span>
        </template>
        <template v-else-if="column.key === 'groups'">
          <Tag
            v-for="g in refsOf(record.groups)"
            :key="g.id"
          >
            {{ g.name }}
          </Tag>
          <span
            v-if="!refsOf(record.groups).length"
            class="muted"
          >-</span>
        </template>
        <template v-else-if="column.key === 'actions'">
          <Button
            type="link"
            size="small"
            @click="openEdit(record as EntUser)"
          >
            {{ $t('common.edit_2') }}
          </Button>
        </template>
      </template>
    </Table>

    <Card
      :title="$t('admin.invites')"
      class="invite-card"
    >
      <p class="page-desc">
        {{ $t('admin.invites_desc') }}
      </p>
      <div class="invite-toolbar">
        <span>{{ $t('admin.invite_max_uses') }}</span>
        <InputNumber
          v-model:value="inviteMaxUses"
          :min="1"
          :max="1000"
          style="width: 100px"
        />
        <span>{{ $t('admin.invite_expires_days') }}</span>
        <InputNumber
          v-model:value="inviteExpiresDays"
          :min="1"
          :max="365"
          style="width: 100px"
        />
        <Button
          type="primary"
          :loading="inviteCreating"
          @click="createInvite"
        >
          {{ $t('admin.invite_create') }}
        </Button>
      </div>
      <Table
        :columns="inviteColumns"
        :data-source="invites"
        :loading="inviteLoading"
        :pagination="false"
        row-key="code"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'uses'">
            {{ record.uses }} / {{ record.max_uses }}
          </template>
          <template v-else-if="column.key === 'expires_at'">
            {{ record.expires_at ? new Date(record.expires_at).toLocaleString() : '-' }}
          </template>
          <template v-else-if="column.key === 'actions'">
            <Button
              type="link"
              size="small"
              @click="copyInviteLink(record.code)"
            >
              {{ $t('admin.invite_copy_link') }}
            </Button>
            <Button
              type="link"
              size="small"
              danger
              @click="revokeInvite(record.code)"
            >
              {{ $t('admin.invite_revoke') }}
            </Button>
          </template>
        </template>
      </Table>
    </Card>

    <Modal
      :open="!!editing"
      :title="$t('admin.edit_user_roles')"
      :confirm-loading="saving"
      :ok-text="$t('common.save')"
      :cancel-text="$t('common.cancel')"
      @ok="save"
      @cancel="closeEdit"
    >
      <div class="edit-block">
        <label>{{ $t('admin.roles_and_groups') }}</label>
        <Select
          v-model:value="editRoleIds"
          mode="multiple"
          :options="roleOptions"
          :placeholder="$t('admin.role')"
          style="width: 100%"
        />
        <Select
          v-model:value="editGroupIds"
          mode="multiple"
          :options="groupOptions"
          :placeholder="$t('admin.group')"
          style="width: 100%"
        />
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.tenant-users { padding: 4px; }
.users-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.page-title { margin: 0; font-size: 18px; }
.page-desc { margin: 4px 0 0; font-size: 12px; color: var(--text-tertiary); }
.muted { color: var(--text-tertiary); }
.edit-block { display: flex; flex-direction: column; gap: 10px; }
.edit-block label { font-size: 13px; color: var(--text-secondary); }
.invite-card { margin-top: 16px; }
.invite-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
</style>
