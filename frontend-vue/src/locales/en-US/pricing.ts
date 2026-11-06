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
  sort_input: 'Input price (low → high)',
  sort_output: 'Output price (low → high)',
  sort_mixed: 'Blended 3:1 (low → high)',
  sort_verified: 'Recently verified',
  col_model: 'Model',
  col_provider: 'Provider',
  col_input: 'Input / 1k',
  col_output: 'Output / 1k',
  col_sources: 'Sources',
  col_verified: 'Verified at',
  sources_agree: '{n} sources agree',
  cross_verified_notice:
    'Prices are stored only after cross-validation: at least 2 public pricing sources must agree within a 5% tolerance. The "Sources" column lists the sources that were compared.',
  unit_hint: 'Unit: {unit}',
  not_billed_warning:
    'This page does not drive billing. Actual charges follow the operator-configured unit prices; use this page for model selection and cost estimation only.',
  empty:
    'No cross-validated prices yet. Syncing is off by default — enable MODEL_PRICE_SYNC and wait for one sync cycle.',
  total: '{n} models',
}
