/**
 * 任务域：定时任务、任务派发、统一任务。
 */
export default {
  task: "## 任务",
  task_2: "任务",
  task_name: "任务名称",
  task_json: "任务 JSON",
  task_input: "任务输入",
  task_type_examples: "任务类型示例：",
  agent_task: "（Agent 任务）",
  task_queued: "任务排队中…",
  active_tasks: "活跃任务",
  new_task: "新建任务",
  please_enter_a_task: "请输入任务",
  task_dispatched_executing: "任务已派发，正在执行…",
  describe_the_task_to_complete: "描述要完成的任务…",
  dispatch_task: "派发任务",
  task_submitted_results_shown_on_the_conversation_page: "任务已提交，正在对话页展示结果",
  click_new_agent_at_the_top_right_to_define_its_prompt_and_tools_then_start_dispatching_tasks: "点击右上角「新建 Agent」，定义提示词与工具，开始派发任务",
  after_dispatching_a_task_from_my_agents_run_results_appear_here: "从「我的 Agent」派发任务后，运行结果将在此显示",

  // 定时任务
  scheduled_tasks: "定时任务",
  new_scheduled_task: "新建定时任务",
  edit_scheduled_task: "编辑定时任务",
  scheduled_task_created: "已创建定时任务",
  scheduled_task_updated: "已更新定时任务",
  scheduled_task_deleted: "已删除定时任务",
  no_scheduled_tasks_yet: "暂无定时任务",
  click_new_task_to_create_a_scheduled_automation: "点击「新建任务」创建定时自动化任务",
  confirm_deleting_this_scheduled_task: "确定删除该定时任务？",
  manual_trigger: "手动触发",
  triggered_name_task_runs_asynchronously: "已触发「{name}」，任务将异步执行",
  webhook_trigger_url_copied_to_clipboard: "Webhook 触发地址已复制到剪贴板",
  copy_webhook_rotates_token: "列表不再保存 Webhook 明文 token。继续将重新生成一个新 token，旧触发地址立即失效（正在使用它的外部集成会收到 403）。确定继续？",

  // 统一任务 / 编排
  unified_task: "统一任务",
  unified_task_2: "（统一任务）",
  unified_task_messages_cleared: "已清空统一任务消息",
  orchestrating: "编排中",
  orchestrating_executing_subtasks: "正在编排/执行子任务...",
  the_unified_task_session_is_ready_just_send_a_message_to_continue_asking: "统一任务会话已就绪，直接发送消息即可继续追问",
  natural_language_tasks_auto_orchestrate_five_workstations: "自然语言任务 自动编排五大工作台",
  quick_command_unified_task_natural_language_execute: "quick command 快速命令 统一任务 自然语言 执行",
  agent_agentic_collaboration_task: "agent 智能体 协同 任务",

  // 子 Agent / 后台任务
  steps: "步数",
  sub_agents_consume_tokens_independently_cost_multiplies_when_multiple_subtasks_run_in_parallel: "子 Agent 会独立消耗 token，多个子任务并行时花销成倍增加。",
  subtask_complete_main_agent_auto_summarizes: "子任务完成 主 Agent 自动汇总",
  job_finished_main_agent_auto_continues: "后台任务结束 主 Agent 自动续跑",
  superagent_takeover_turn: "Super-agent 接管轮",
  superagent_takeover_prompt: "Super-agent 接管指令",
  watchdog_nudge_prompt: "系统提醒：这一轮似乎卡住了",
  an_organization_task_is_already_running: "已有整理任务在运行中",

  // 资源提及
  no_matching_resources_knowledge_base_agent_skill_plugin: "没有匹配的资源（知识库 / Agent / 技能 / 插件）",
}
