/**
 * Reference price catalog (read-only, cross-validated across sources).
 * Kept apart from `billing`: billing answers "what did I spend", this answers
 * "what does the market charge".
 */
export default {
  title: 'Model reference pricing',
  search_placeholder: 'Search model name',
  provider_all: 'All providers',
  refresh: 'Refresh',
  sort_model: 'Model name',
  sort_confidence: 'Verified first',
  sort_input: 'Input price (low → high)',
  sort_output: 'Output price (low → high)',
  sort_mixed: 'Blended 3:1 (low → high)',
  sort_verified: 'Recently verified',
  col_model: 'Model',
  col_provider: 'Provider',
  col_input: 'Input / 1k',
  col_output: 'Output / 1k',
  col_sources: 'Sources',
  col_confidence: 'Confidence',
  col_verified: 'Last synced',
  verified: 'Verified',
  single_source: 'Single source',
  sources_agree: '{n} sources agree',
  sources_differ: '{n} sources differ',
  source_details_hint: 'Per-source prices (input / output per 1k)',
  source_details_missing: 'No per-source details yet (legacy row; wait for the next sync)',
  cross_verified_notice:
    'The catalog includes both cross-validated prices (at least 2 public sources agree within 5%) and single-source reference prices. Expand "Sources" to see each source\u2019s own quote.',
  unit_hint: 'Unit: {unit}',
  not_billed_warning:
    'This page does not drive billing. Actual charges follow the operator-configured unit prices; use this page for model selection and cost estimation only.',
  empty:
    'No cross-validated prices yet. Syncing is off by default — enable MODEL_PRICE_SYNC and wait for one sync cycle.',
  group_verified_n: '{n} verified',
  group_models_n: '{n} models',
  provider_unknown: 'Other providers',
  total: '{n} models',
}
