/**
 * 参考价格目录（只读，多源交叉验证）。
 * 与 billing 域分开：billing 是"我花了多少钱"，这里回答"市场上多少钱"。
 */
export default {
  title: '大模型参考价格',
  search_placeholder: '搜索模型名',
  provider_all: '全部厂商',
  refresh: '刷新',
  sort_model: '按模型名',
  sort_confidence: '已验证优先',
  sort_input: '输入价（低→高）',
  sort_output: '输出价（低→高）',
  sort_mixed: '混合价 3:1（低→高）',
  sort_verified: '最近验证',
  col_model: '模型',
  col_provider: '厂商',
  col_input: '输入 / 1k',
  col_output: '输出 / 1k',
  col_sources: '印证来源',
  col_confidence: '可信度',
  col_verified: '最近同步',
  verified: '已验证',
  single_source: '单源',
  sources_agree: '{n} 源一致',
  sources_differ: '{n} 源有分歧',
  source_details_hint: '各源报价（输入 / 输出，每 1k）',
  source_details_missing: '暂无每源明细（旧数据，等待下一次同步）',
  cross_verified_notice:
    '目录同时收录多源印证的价格（≥2 个公开价格源在 5% 容差内一致）与单源参考价；「印证来源」可展开查看每个源各自的报价。',
  unit_hint: '单位 {unit}',
  not_billed_warning: '本页不参与计费。实际扣费以管理员配置的计费单价为准；此处仅供选型与成本估算参考。',
  empty: '还没有经交叉验证的价格数据。同步默认关闭，需要开启 MODEL_PRICE_SYNC 并等待一轮同步完成。',
  group_verified_n: '{n} 个已验证',
  group_models_n: '{n} 个模型',
  provider_unknown: '其他厂商',
  total: '共 {n} 个模型',
}
