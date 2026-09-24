"""权威基线迁移 —— 以实际运行中的数据库 schema 为唯一事实源。

Revision ID: 0001_authoritative_baseline
Revises: None
Create Date: 2026-09-24

## 为什么是「一个」迁移

本仓库此前的迁移链已与数据库脱节：`migrations/versions/` 里的 11 个迁移只覆盖 12 张表，
而实际数据库有 80 张表 —— 其余表由历史 `init.sql`、Go 侧启动时的 CREATE TABLE，以及一份
**已丢失**的早期迁移共同建立（见旧迁移 5e244b718fd1 的自述）。结果是「代码期望的 schema」
与「数据库真实 schema」无法靠迁移链对齐，只能靠人工比对维护。

因此这里重建为**单一权威迁移**：DDL 直接取自稳定后的数据库（pg_dump --schema-only），
此后所有 schema 变更都必须在此基础上追加新的 Alembic revision。

## 部署与升级

* **全新库**：`alembic upgrade head` 一次建齐全部表；
* **已存在的库**（表已齐全，`alembic_version` 仍是旧 revision）：
  `alembic stamp 0001_authoritative_baseline` —— 只改版本号、不重放 DDL；
* 应用启动只做只读校验（`internal/db/schema_version.go`）：head 与 `alembic_version`
  不一致即拒绝启动（`ALLOW_SCHEMA_DRIFT=true` 可临时放行）。

## 有意排除的表

* `alembic_version` —— 由 Alembic 自己创建和维护；
* `schema_migrations` —— Go 侧旧迁移系统的残留（表内为空，迁移记录从未写入）。
  两条迁移路径并存正是本文件要终结的问题，故不再纳入，并在 downgrade 中不涉及。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0001_authoritative_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 顺序即依赖顺序：先表与序列，后约束与索引（与 pg_dump 输出一致）。
_UPGRADE: list[str] = [
    """CREATE TABLE public.admin_api_call_logs (
    id character varying(36) NOT NULL,
    api_key_id character varying(36),
    model_id character varying(50),
    workflow_id character varying(50),
    endpoint character varying(100),
    method character varying(10),
    request_size_bytes integer,
    response_size_bytes integer,
    duration_ms integer,
    status_code integer,
    retry_count integer,
    input_tokens integer,
    output_tokens integer,
    credits_consumed bigint,
    created_at timestamp without time zone
)""",
    """CREATE TABLE public.admin_api_keys (
    id character varying(36) NOT NULL,
    key_hash character varying(64),
    name character varying(100),
    tenant_id character varying(50),
    user_id character varying(50),
    monthly_quota integer,
    used_count bigint,
    used_credits bigint,
    status character varying(20),
    expires_at timestamp without time zone,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    created_by character varying(50),
    description text,
    allowed_models character varying(255),
    rate_limit_qps integer
)""",
    """CREATE TABLE public.admin_cron_jobs (
    id character varying(36) NOT NULL,
    job_id character varying(50),
    name character varying(100),
    schedule character varying(50),
    last_run_at timestamp without time zone,
    last_run_status character varying(20),
    last_error text,
    next_run_at timestamp without time zone,
    enabled boolean,
    metadata json,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.admin_database_backups (
    id character varying(36) NOT NULL,
    backup_type character varying(20),
    description text,
    file_path character varying(500),
    file_size_mb character varying(255),
    status character varying(20),
    error_message text,
    started_at timestamp without time zone,
    completed_at timestamp without time zone,
    duration_seconds integer,
    created_by character varying(50)
)""",
    """CREATE TABLE public.admin_db_configs (
    id character varying(36) NOT NULL,
    dsn character varying(500),
    host character varying(100),
    port integer,
    dbname character varying(100),
    max_open_connections integer,
    max_idle_connections integer,
    conn_max_lifetime character varying(255),
    status character varying(20),
    last_health_check timestamp without time zone,
    avg_query_time_ms character varying(255),
    database_size_mb character varying(255),
    total_tables integer,
    sequential_scans bigint,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.admin_domains (
    id character varying(36) NOT NULL,
    domain character varying(100),
    tenant_id character varying(50),
    dns_provider character varying(50),
    dns_record_id character varying(100),
    cname_target character varying(200),
    ssl_status character varying(20),
    ssl_expires_at timestamp without time zone,
    auto_renew boolean,
    status character varying(20),
    verified_at timestamp without time zone,
    verified_by character varying(50),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.admin_model_configs (
    id character varying(36) NOT NULL,
    model_id character varying(50),
    display_name character varying(100),
    provider character varying(50),
    priority integer,
    weight integer,
    fallback_chain character varying(255),
    max_rpm integer,
    max_tpm integer,
    concurrent_limit integer,
    status character varying(20),
    is_default boolean,
    input_cost_per_1m character varying(255),
    output_cost_per_1m character varying(255),
    config_json json,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.admin_redis_configs (
    id character varying(36) NOT NULL,
    host character varying(100),
    port integer,
    password_hash character varying(256),
    db_index integer,
    pool_size integer,
    min_idle_connections integer,
    max_conn_age character varying(255),
    status character varying(20),
    last_health_check timestamp without time zone,
    avg_latency_ms character varying(255),
    memory_used_mb character varying(255),
    connected_clients integer,
    hits bigint,
    misses bigint,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.admin_tenant_usage (
    id character varying(36) NOT NULL,
    tenant_id character varying(50),
    stat_date character varying(255),
    api_calls bigint,
    tokens_used bigint,
    credits_consumed bigint,
    storage_mb character varying(255),
    created_at timestamp without time zone
)""",
    """CREATE TABLE public.admin_tenants (
    id character varying(36) NOT NULL,
    tenant_id character varying(50),
    name character varying(100),
    company_name character varying(200),
    contact_email character varying(100),
    contact_phone character varying(20),
    max_api_keys integer,
    max_models integer,
    monthly_quota bigint,
    max_concurrent_sessions integer,
    status character varying(20),
    expires_at timestamp without time zone,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    created_by character varying(50),
    features json
)""",
    """CREATE TABLE public.admin_workflow_executions (
    id character varying(36) NOT NULL,
    workflow_id character varying(50),
    workflow_version integer,
    status character varying(20),
    started_at timestamp without time zone,
    completed_at timestamp without time zone,
    duration_ms integer,
    input_data json,
    output_data json,
    error_message text,
    triggered_by character varying(50),
    node_results json
)""",
    """CREATE TABLE public.admin_workflows (
    id character varying(36) NOT NULL,
    workflow_id character varying(50),
    name character varying(100),
    description text,
    nodes json,
    edges json,
    error_handling_strategy character varying(20),
    timeout_ms integer,
    max_retries integer,
    version integer,
    published_version integer,
    status character varying(20),
    created_by character varying(50),
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    published_at timestamp without time zone
)""",
    """CREATE TABLE public.agent_registry (
    agent_type character varying(32) NOT NULL,
    name character varying(128),
    description text NOT NULL,
    enabled boolean,
    config json,
    created_at timestamp without time zone
)""",
    """CREATE TABLE public.agent_sessions (
    id character varying(128) NOT NULL,
    user_id character varying(36),
    agent_id character varying(36),
    name character varying(128),
    task text NOT NULL,
    status character varying(16),
    result text,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    tenant_id character varying(36)
)""",
    """CREATE TABLE public.agents (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    name character varying(255),
    description text,
    system_prompt text,
    tools json,
    llm_config json,
    max_turns integer,
    timeout_seconds integer,
    enabled boolean,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    user_id character varying(36),
    visibility character varying(16),
    kb_id character varying(255),
    skills json,
    plugins json,
    workflows json,
    kind character varying(16) DEFAULT 'chat'::character varying NOT NULL
)""",
    """CREATE TABLE public.api_keys (
    id character varying(36) NOT NULL,
    user_id character varying(36),
    name character varying(128),
    key_hash character varying(64),
    last_used_at timestamp without time zone,
    expires_at timestamp without time zone,
    created_at timestamp without time zone,
    revoked boolean
)""",
    """CREATE TABLE public.audit_logs (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    action character varying(64),
    resource_type character varying(64),
    resource_id character varying(64),
    details json,
    ip_address character varying(45),
    created_at timestamp without time zone
)""",
    """CREATE TABLE public.billing_records (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    session_id character varying(36),
    input_tokens bigint,
    output_tokens bigint,
    cost_cents integer,
    created_at timestamp without time zone,
    group_id character varying(36),
    turn_id character varying(36)
)""",
    """CREATE TABLE public.conversation_shares (
    id character varying(36) NOT NULL,
    session_id character varying(128),
    user_id character varying(36),
    title character varying(255),
    message_ids character varying(255),
    created_at timestamp without time zone,
    revoked_at timestamp without time zone
)""",
    """CREATE TABLE public.credit_transactions (
    id character varying(36) NOT NULL,
    user_id character varying(36),
    amount integer,
    balance integer,
    reason character varying(64),
    created_at timestamp without time zone,
    turn_id character varying(36)
)""",
    """CREATE TABLE public.cron_jobs (
    id character varying(36) NOT NULL,
    name character varying(128),
    schedule character varying(64),
    task character varying(255),
    enabled boolean,
    last_run_at timestamp without time zone,
    last_status character varying(16),
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    tenant_id character varying(36),
    user_id character varying(36),
    webhook_token character varying(64)
)""",
    """CREATE TABLE public.domains (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    domain character varying(255),
    ssl_status character varying(16),
    verified boolean,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.ent_captcha_config (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    provider character varying(32),
    site_key character varying(256),
    secret_enc text NOT NULL,
    verify_url character varying(512),
    enabled boolean,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.ent_catalog_installs (
    item_id character varying(36) NOT NULL,
    tenant_id character varying(36) NOT NULL,
    enabled boolean,
    installed_at timestamp without time zone
)""",
    """CREATE TABLE public.ent_catalog_items (
    id character varying(36) NOT NULL,
    type character varying(8),
    name character varying(128),
    version character varying(32),
    manifest json,
    status character varying(16),
    created_by character varying(36),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.ent_group_members (
    group_id character varying(36) NOT NULL,
    user_id character varying(36) NOT NULL
)""",
    """CREATE TABLE public.ent_group_roles (
    group_id character varying(36) NOT NULL,
    role_id character varying(36) NOT NULL
)""",
    """CREATE TABLE public.ent_groups (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    name character varying(128),
    description text,
    created_at timestamp without time zone
)""",
    """CREATE TABLE public.ent_mail_config (
    id character varying(36) NOT NULL,
    tenant_id character varying(36) NOT NULL,
    provider character varying(32),
    enabled boolean DEFAULT false NOT NULL,
    smtp_host character varying(255),
    smtp_port integer,
    smtp_username character varying(255),
    smtp_password_enc text,
    smtp_security character varying(16),
    smtp_skip_verify boolean DEFAULT false NOT NULL,
    api_base_url character varying(512),
    api_key_enc text,
    api_channel_id integer,
    api_template_id integer,
    from_address character varying(255),
    from_name character varying(128),
    reply_to character varying(255),
    site_name character varying(128),
    app_base_url character varying(512),
    login_enabled boolean DEFAULT false NOT NULL,
    register_verify boolean DEFAULT false NOT NULL,
    auto_register boolean DEFAULT false NOT NULL,
    reset_enabled boolean DEFAULT false NOT NULL,
    welcome_enabled boolean DEFAULT true NOT NULL,
    code_subject character varying(255),
    code_body text,
    welcome_subject character varying(255),
    welcome_body text,
    reset_subject character varying(255),
    reset_body text,
    code_ttl_seconds integer DEFAULT 300 NOT NULL,
    send_interval_seconds integer DEFAULT 60 NOT NULL,
    daily_limit integer DEFAULT 10 NOT NULL,
    timeout_seconds integer DEFAULT 30 NOT NULL,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.ent_model_policies (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    role_id character varying(36),
    allowed_models character varying(255),
    per_model_limits json,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.ent_model_routes (
    id character varying(36) NOT NULL,
    tenant_id character varying(36) NOT NULL,
    model_id character varying(128) NOT NULL,
    primary_provider character varying(64) NOT NULL,
    fallback_order jsonb DEFAULT '[]'::jsonb,
    provider_config jsonb DEFAULT '{}'::jsonb,
    enabled boolean DEFAULT true,
    priority integer DEFAULT 1,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
)""",
    """CREATE TABLE public.ent_oidc_providers (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    name character varying(64),
    issuer character varying(512),
    client_id character varying(256),
    client_secret_enc text NOT NULL,
    scopes character varying(255),
    enabled boolean,
    auto_provision boolean,
    role_mapping json,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    protocol character varying(16),
    provider_type character varying(32),
    display_name character varying(64),
    icon character varying(64),
    sort_order integer,
    auth_url character varying(512),
    token_url character varying(512),
    userinfo_url character varying(512),
    extra json
)""",
    """CREATE TABLE public.ent_quota_allocations (
    id character varying(36) NOT NULL,
    pool_id character varying(36),
    target_type character varying(10),
    target_id character varying(36),
    amount bigint,
    created_at timestamp without time zone
)""",
    """CREATE TABLE public.ent_quota_pools (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    resource_type character varying(20),
    total_amount bigint,
    period character varying(10),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.ent_roles (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    name character varying(64),
    display_name character varying(128),
    is_builtin boolean,
    permissions character varying(255),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.ent_sms_config (
    tenant_id character varying(36) NOT NULL,
    provider character varying(32) DEFAULT 'aliyun'::character varying NOT NULL,
    sign_name character varying(64) DEFAULT ''::character varying NOT NULL,
    template_id character varying(64) DEFAULT ''::character varying NOT NULL,
    access_key_id character varying(256) DEFAULT ''::character varying NOT NULL,
    secret_enc text DEFAULT ''::text NOT NULL,
    endpoint character varying(512),
    code_ttl_seconds integer DEFAULT 300 NOT NULL,
    send_interval_seconds integer DEFAULT 60 NOT NULL,
    daily_limit integer DEFAULT 10 NOT NULL,
    login_enabled boolean DEFAULT false NOT NULL,
    auto_register boolean DEFAULT false NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now()
)""",
    """CREATE TABLE public.ent_templates (
    id character varying(36) NOT NULL,
    type character varying(16),
    name character varying(128),
    description text NOT NULL,
    payload json,
    published boolean,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.ent_tenant_policies (
    tenant_id character varying(36) NOT NULL,
    privacy_mode boolean,
    data_retention_days integer,
    training_allowed boolean,
    redaction_rules json,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.ent_user_identities (
    id character varying(36) NOT NULL,
    user_id character varying(36),
    provider_id character varying(36),
    subject character varying(256),
    email character varying(255),
    created_at timestamp without time zone
)""",
    """CREATE TABLE public.ent_user_roles (
    user_id character varying(36) NOT NULL,
    role_id character varying(36) NOT NULL
)""",
    """CREATE TABLE public.ent_webhooks (
    id character varying(36) NOT NULL,
    tenant_id character varying(36) NOT NULL,
    name character varying(255) DEFAULT ''::character varying NOT NULL,
    event_types jsonb DEFAULT '[]'::jsonb NOT NULL,
    url text DEFAULT ''::text NOT NULL,
    secret text DEFAULT ''::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    retry_policy jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.enterprise_tasks (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    title character varying(255),
    description text NOT NULL,
    project character varying(128),
    assignee character varying(128),
    priority character varying(16),
    status character varying(16),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.guest_storage (
    client_id character varying(64) NOT NULL,
    storage_id character varying(64),
    created_at timestamp without time zone
)""",
    """CREATE TABLE public.kb_articles (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    title character varying(255),
    content text NOT NULL,
    tags character varying(255),
    category character varying(64),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.knowledge_bases (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    name character varying(255),
    description text,
    type character varying(32),
    visibility character varying(32),
    status character varying(32),
    document_count integer,
    total_size_bytes bigint,
    credits_consumed integer,
    config json,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    doc_count integer
)""",
    """CREATE TABLE public.knowledge_chunks (
    id character varying(36) NOT NULL,
    document_id character varying(36),
    knowledge_base_id character varying(36),
    tenant_id character varying(36),
    chunk_index integer,
    content text NOT NULL,
    metadata json,
    search_vector character varying(255),
    created_at timestamp without time zone
)""",
    """CREATE TABLE public.knowledge_documents (
    id character varying(36) NOT NULL,
    knowledge_base_id character varying(36),
    tenant_id character varying(36),
    user_id character varying(36),
    name character varying(255),
    file_url character varying(1024),
    file_type character varying(32),
    file_size_bytes bigint,
    chunk_count integer,
    status character varying(32),
    error_message text,
    metadata json,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    content character varying(255)
)""",
    """CREATE TABLE public.llm_models (
    id character varying(36) NOT NULL,
    provider character varying(32),
    name character varying(128),
    display_name character varying(128),
    enabled boolean,
    context_window integer,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.llm_provider_keys (
    id character varying(36) NOT NULL,
    provider character varying(50) NOT NULL,
    encrypted_key text NOT NULL,
    key_hash character varying(64) NOT NULL,
    status character varying(20) DEFAULT 'active'::character varying NOT NULL,
    remark text,
    created_at timestamp with time zone,
    updated_at timestamp with time zone
)""",
    """CREATE TABLE public.marketing_campaigns (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    name character varying(255),
    description text NOT NULL,
    campaign_type character varying(32),
    config json,
    status character varying(16),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.media_assets (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    type character varying(16),
    name character varying(255),
    file_url character varying(1024),
    file_path character varying(512),
    mime_type character varying(64),
    thumbnail character varying(512),
    metadata json,
    tags character varying(255),
    category character varying(64),
    size bigint,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    parent_id character varying(64)
)""",
    """CREATE TABLE public.meeting_notes (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    title character varying(255),
    notes text NOT NULL,
    summary text,
    participants character varying(255),
    date character varying(255),
    created_at timestamp without time zone
)""",
    """CREATE TABLE public.memory_summaries (
    id character varying(64) NOT NULL,
    tenant_id character varying(64),
    user_id character varying(64),
    session_id character varying(64),
    content text NOT NULL,
    topics json,
    entities json,
    turn_start integer,
    turn_end integer,
    content_hash character varying(80),
    access_count integer,
    last_accessed_at timestamp without time zone,
    status character varying(16),
    created_at timestamp without time zone
)""",
    """CREATE TABLE public.messages (
    id character varying(36) NOT NULL,
    session_id character varying(36),
    role character varying(16),
    content text NOT NULL,
    tool_calls json,
    created_at timestamp without time zone,
    turn_id character varying(36),
    source character varying(32) DEFAULT ''::character varying NOT NULL
)""",
    """CREATE TABLE public.okrs (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    objective character varying(255),
    key_results json,
    quarter character varying(16),
    status character varying(16),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.payments (
    id character varying(64) NOT NULL,
    user_id character varying(36),
    channel character varying(16),
    credits integer,
    amount_cents bigint,
    currency character varying(8),
    status character varying(16),
    qr_code text,
    provider_order_id character varying(64),
    trade_no character varying(64),
    created_at timestamp without time zone,
    paid_at timestamp without time zone,
    expired_at timestamp without time zone
)""",
    """CREATE TABLE public.session_map_nodes (
    id character varying(64) NOT NULL,
    workspace_id character varying(64) NOT NULL,
    session_id character varying(36),
    parent_node_id character varying(64),
    edge_kind character varying(16) DEFAULT 'manual'::character varying NOT NULL,
    x integer DEFAULT 0 NOT NULL,
    y integer DEFAULT 0 NOT NULL,
    title character varying(255),
    color character varying(16),
    collapsed boolean DEFAULT false NOT NULL,
    hidden boolean DEFAULT false NOT NULL,
    pinned boolean DEFAULT false NOT NULL,
    branch_from_seq integer,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.session_map_workspaces (
    id character varying(64) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    name character varying(255) DEFAULT ''::character varying NOT NULL,
    viewport jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.sessions (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    agent_id character varying(36),
    title character varying(255),
    status character varying(16),
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    pinned boolean,
    tag character varying(64),
    parent_session_id character varying(36),
    branch_from_seq integer,
    alias character varying(64),
    branch_mode character varying(16),
    branch_state character varying(16),
    branch_keep_tail integer
)""",
    """CREATE TABLE public.stripe_payments (
    session_id character varying(128) NOT NULL,
    user_id character varying(36),
    credits integer,
    amount_cents bigint,
    status character varying(16),
    created_at timestamp without time zone,
    completed_at timestamp without time zone
)""",
    """CREATE TABLE public.subagent_run_steps (
    id bigint NOT NULL,
    run_id character varying(64) NOT NULL,
    seq integer NOT NULL,
    kind character varying(16) NOT NULL,
    role character varying(16),
    tool_name character varying(64),
    tool_call_id character varying(128),
    content text,
    truncated boolean DEFAULT false NOT NULL,
    input_tokens integer DEFAULT 0 NOT NULL,
    output_tokens integer DEFAULT 0 NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL
)""",
    """CREATE SEQUENCE public.subagent_run_steps_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1""",
    """ALTER SEQUENCE public.subagent_run_steps_id_seq OWNED BY public.subagent_run_steps.id""",
    """CREATE TABLE public.subagent_runs (
    id character varying(64) NOT NULL,
    root_session_id character varying(128) NOT NULL,
    turn_id character varying(64),
    parent_run_id character varying(64),
    depth integer DEFAULT 1 NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    agent_id character varying(36),
    profile_name character varying(128),
    task text NOT NULL,
    status character varying(16) NOT NULL,
    summary text,
    summary_format character varying(16) DEFAULT 'markdown'::character varying NOT NULL,
    artifacts jsonb DEFAULT '[]'::jsonb,
    write_paths jsonb DEFAULT '[]'::jsonb,
    read_only boolean DEFAULT false NOT NULL,
    input_tokens bigint DEFAULT 0 NOT NULL,
    output_tokens bigint DEFAULT 0 NOT NULL,
    steps integer DEFAULT 0 NOT NULL,
    cost_cents integer DEFAULT 0 NOT NULL,
    redacted_count integer DEFAULT 0 NOT NULL,
    error text,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    rerun_of character varying(64)
)""",
    """CREATE TABLE public.support_tickets (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    subject character varying(255),
    description text NOT NULL,
    priority character varying(16),
    status character varying(16),
    assignee character varying(128),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.system_settings (
    id integer NOT NULL,
    category character varying(32),
    key character varying(64),
    value json,
    updated_at timestamp without time zone,
    updated_by character varying(36),
    encrypted boolean
)""",
    """CREATE SEQUENCE public.system_settings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1""",
    """ALTER SEQUENCE public.system_settings_id_seq OWNED BY public.system_settings.id""",
    """CREATE TABLE public.task_idempotency (
    idempotency_key character varying(255) NOT NULL,
    task_id character varying(64),
    task_type character varying(64),
    tenant_id character varying(64),
    status character varying(16) DEFAULT 'running'::character varying NOT NULL,
    attempt integer DEFAULT 1 NOT NULL,
    result_ref text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
)""",
    """CREATE TABLE public.tasks (
    id character varying(36) NOT NULL,
    user_id character varying(36),
    type character varying(32),
    status character varying(16),
    priority integer,
    payload json,
    result json,
    error text,
    retries integer,
    max_retries integer,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.tenants (
    id character varying(36) NOT NULL,
    name character varying(255),
    created_at timestamp without time zone,
    status character varying(16)
)""",
    """CREATE TABLE public.tool_calls (
    id character varying(36) NOT NULL,
    session_id character varying(36),
    message_id character varying(36),
    tool_name character varying(128),
    input json,
    output text NOT NULL,
    is_error boolean,
    duration_ms bigint,
    created_at timestamp without time zone,
    turn_id character varying(36)
)""",
    """CREATE TABLE public.turns (
    id character varying(36) NOT NULL,
    session_id character varying(36),
    user_id character varying(36),
    status character varying(16),
    error text,
    input_tokens bigint,
    output_tokens bigint,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    created_at timestamp without time zone,
    cached_tokens bigint DEFAULT 0 NOT NULL,
    cache_hit boolean DEFAULT false NOT NULL,
    model character varying(128)
)""",
    """CREATE TABLE public.unified_messages (
    id integer NOT NULL,
    session_id character varying(36),
    role character varying(16),
    content text NOT NULL,
    metadata json,
    error text NOT NULL,
    created_at timestamp without time zone
)""",
    """CREATE SEQUENCE public.unified_messages_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1""",
    """ALTER SEQUENCE public.unified_messages_id_seq OWNED BY public.unified_messages.id""",
    """CREATE TABLE public.unified_sessions (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    title character varying(255),
    mode character varying(16),
    shared_context json,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    runtime jsonb DEFAULT '{}'::jsonb NOT NULL
)""",
    """CREATE TABLE public.uploads (
    id character varying(64) NOT NULL,
    user_id character varying(64),
    name character varying(255),
    size bigint,
    mime_type character varying(64),
    purpose character varying(16),
    parent_id character varying(64),
    category character varying(64),
    chunk_size integer,
    chunk_count integer,
    chunks_received character varying(255),
    status character varying(16),
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    tenant_id character varying(36)
)""",
    """CREATE TABLE public.user_memory_entries (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    slot character varying(64) NOT NULL,
    item_key character varying(255) NOT NULL,
    item_value jsonb,
    confidence integer,
    source character varying(32),
    embedding jsonb,
    access_count integer DEFAULT 0 NOT NULL,
    last_accessed_at timestamp with time zone,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
)""",
    """CREATE TABLE public.user_memory_profile (
    tenant_id character varying(64) NOT NULL,
    user_id character varying(64) NOT NULL,
    slot character varying(32) NOT NULL,
    item_key character varying(128) NOT NULL,
    item_value json,
    confidence integer,
    source character varying(16),
    version integer,
    confirmed_at timestamp without time zone,
    last_referenced_at timestamp without time zone,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.users (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    email character varying(255),
    name character varying(128),
    password_hash character varying(255),
    role character varying(16),
    storage_id character varying(64),
    credits integer DEFAULT 1000,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    phone character varying(32),
    password_set boolean,
    settings json
)""",
    """CREATE TABLE public.wiki_pages (
    id character varying(36) NOT NULL,
    tenant_id character varying(36),
    user_id character varying(36),
    title character varying(255),
    content text NOT NULL,
    tags character varying(255),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.workflow_graphs (
    id character varying(36) NOT NULL,
    name character varying(255),
    user_id character varying(36),
    graph_json json,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """CREATE TABLE public.workflow_instances (
    id character varying(64) NOT NULL,
    user_id character varying(64),
    workflow_id character varying(64),
    workflow_name character varying(255),
    status character varying(16),
    results json,
    error text,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
)""",
    """ALTER TABLE ONLY public.subagent_run_steps ALTER COLUMN id SET DEFAULT nextval('public.subagent_run_steps_id_seq'::regclass)""",
    """ALTER TABLE ONLY public.system_settings ALTER COLUMN id SET DEFAULT nextval('public.system_settings_id_seq'::regclass)""",
    """ALTER TABLE ONLY public.unified_messages ALTER COLUMN id SET DEFAULT nextval('public.unified_messages_id_seq'::regclass)""",
    """ALTER TABLE ONLY public.admin_api_call_logs
    ADD CONSTRAINT admin_api_call_logs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.admin_api_keys
    ADD CONSTRAINT admin_api_keys_key_hash_key UNIQUE (key_hash)""",
    """ALTER TABLE ONLY public.admin_api_keys
    ADD CONSTRAINT admin_api_keys_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.admin_cron_jobs
    ADD CONSTRAINT admin_cron_jobs_job_id_key UNIQUE (job_id)""",
    """ALTER TABLE ONLY public.admin_cron_jobs
    ADD CONSTRAINT admin_cron_jobs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.admin_database_backups
    ADD CONSTRAINT admin_database_backups_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.admin_db_configs
    ADD CONSTRAINT admin_db_configs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.admin_domains
    ADD CONSTRAINT admin_domains_domain_key UNIQUE (domain)""",
    """ALTER TABLE ONLY public.admin_domains
    ADD CONSTRAINT admin_domains_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.admin_model_configs
    ADD CONSTRAINT admin_model_configs_model_id_key UNIQUE (model_id)""",
    """ALTER TABLE ONLY public.admin_model_configs
    ADD CONSTRAINT admin_model_configs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.admin_redis_configs
    ADD CONSTRAINT admin_redis_configs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.admin_tenant_usage
    ADD CONSTRAINT admin_tenant_usage_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.admin_tenants
    ADD CONSTRAINT admin_tenants_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.admin_tenants
    ADD CONSTRAINT admin_tenants_tenant_id_key UNIQUE (tenant_id)""",
    """ALTER TABLE ONLY public.admin_workflow_executions
    ADD CONSTRAINT admin_workflow_executions_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.admin_workflows
    ADD CONSTRAINT admin_workflows_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.admin_workflows
    ADD CONSTRAINT admin_workflows_workflow_id_key UNIQUE (workflow_id)""",
    """ALTER TABLE ONLY public.agent_registry
    ADD CONSTRAINT agent_registry_pkey PRIMARY KEY (agent_type)""",
    """ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.billing_records
    ADD CONSTRAINT billing_records_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.conversation_shares
    ADD CONSTRAINT conversation_shares_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.credit_transactions
    ADD CONSTRAINT credit_transactions_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.cron_jobs
    ADD CONSTRAINT cron_jobs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.domains
    ADD CONSTRAINT domains_domain_key UNIQUE (domain)""",
    """ALTER TABLE ONLY public.domains
    ADD CONSTRAINT domains_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.ent_captcha_config
    ADD CONSTRAINT ent_captcha_config_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.ent_catalog_installs
    ADD CONSTRAINT ent_catalog_installs_pkey PRIMARY KEY (item_id, tenant_id)""",
    """ALTER TABLE ONLY public.ent_catalog_items
    ADD CONSTRAINT ent_catalog_items_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.ent_group_members
    ADD CONSTRAINT ent_group_members_pkey PRIMARY KEY (group_id, user_id)""",
    """ALTER TABLE ONLY public.ent_group_roles
    ADD CONSTRAINT ent_group_roles_pkey PRIMARY KEY (group_id, role_id)""",
    """ALTER TABLE ONLY public.ent_groups
    ADD CONSTRAINT ent_groups_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.ent_mail_config
    ADD CONSTRAINT ent_mail_config_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.ent_mail_config
    ADD CONSTRAINT ent_mail_config_tenant_id_key UNIQUE (tenant_id)""",
    """ALTER TABLE ONLY public.ent_model_policies
    ADD CONSTRAINT ent_model_policies_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.ent_model_routes
    ADD CONSTRAINT ent_model_routes_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.ent_model_routes
    ADD CONSTRAINT ent_model_routes_tenant_id_model_id_key UNIQUE (tenant_id, model_id)""",
    """ALTER TABLE ONLY public.ent_oidc_providers
    ADD CONSTRAINT ent_oidc_providers_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.ent_quota_allocations
    ADD CONSTRAINT ent_quota_allocations_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.ent_quota_pools
    ADD CONSTRAINT ent_quota_pools_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.ent_roles
    ADD CONSTRAINT ent_roles_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.ent_sms_config
    ADD CONSTRAINT ent_sms_config_pkey PRIMARY KEY (tenant_id)""",
    """ALTER TABLE ONLY public.ent_templates
    ADD CONSTRAINT ent_templates_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.ent_tenant_policies
    ADD CONSTRAINT ent_tenant_policies_pkey PRIMARY KEY (tenant_id)""",
    """ALTER TABLE ONLY public.ent_user_identities
    ADD CONSTRAINT ent_user_identities_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.ent_user_roles
    ADD CONSTRAINT ent_user_roles_pkey PRIMARY KEY (user_id, role_id)""",
    """ALTER TABLE ONLY public.ent_webhooks
    ADD CONSTRAINT ent_webhooks_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.enterprise_tasks
    ADD CONSTRAINT enterprise_tasks_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.guest_storage
    ADD CONSTRAINT guest_storage_pkey PRIMARY KEY (client_id)""",
    """ALTER TABLE ONLY public.guest_storage
    ADD CONSTRAINT guest_storage_storage_id_key UNIQUE (storage_id)""",
    """ALTER TABLE ONLY public.kb_articles
    ADD CONSTRAINT kb_articles_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.knowledge_bases
    ADD CONSTRAINT knowledge_bases_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.knowledge_chunks
    ADD CONSTRAINT knowledge_chunks_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.knowledge_documents
    ADD CONSTRAINT knowledge_documents_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.llm_models
    ADD CONSTRAINT llm_models_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.llm_provider_keys
    ADD CONSTRAINT llm_provider_keys_key_hash_key UNIQUE (key_hash)""",
    """ALTER TABLE ONLY public.llm_provider_keys
    ADD CONSTRAINT llm_provider_keys_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.marketing_campaigns
    ADD CONSTRAINT marketing_campaigns_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.media_assets
    ADD CONSTRAINT media_assets_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.meeting_notes
    ADD CONSTRAINT meeting_notes_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.memory_summaries
    ADD CONSTRAINT memory_summaries_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.okrs
    ADD CONSTRAINT okrs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.session_map_nodes
    ADD CONSTRAINT session_map_nodes_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.session_map_workspaces
    ADD CONSTRAINT session_map_workspaces_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.stripe_payments
    ADD CONSTRAINT stripe_payments_pkey PRIMARY KEY (session_id)""",
    """ALTER TABLE ONLY public.subagent_run_steps
    ADD CONSTRAINT subagent_run_steps_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.subagent_runs
    ADD CONSTRAINT subagent_runs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.system_settings
    ADD CONSTRAINT system_settings_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.task_idempotency
    ADD CONSTRAINT task_idempotency_pkey PRIMARY KEY (idempotency_key)""",
    """ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.tenants
    ADD CONSTRAINT tenants_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.tool_calls
    ADD CONSTRAINT tool_calls_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.turns
    ADD CONSTRAINT turns_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.unified_messages
    ADD CONSTRAINT unified_messages_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.unified_sessions
    ADD CONSTRAINT unified_sessions_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.uploads
    ADD CONSTRAINT uploads_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.user_memory_entries
    ADD CONSTRAINT user_memory_entries_identity_uniq UNIQUE (tenant_id, user_id, slot, item_key)""",
    """ALTER TABLE ONLY public.user_memory_entries
    ADD CONSTRAINT user_memory_entries_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.user_memory_profile
    ADD CONSTRAINT user_memory_profile_pkey PRIMARY KEY (tenant_id, user_id, slot, item_key)""",
    """ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_storage_id_key UNIQUE (storage_id)""",
    """ALTER TABLE ONLY public.wiki_pages
    ADD CONSTRAINT wiki_pages_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.workflow_graphs
    ADD CONSTRAINT workflow_graphs_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.workflow_instances
    ADD CONSTRAINT workflow_instances_pkey PRIMARY KEY (id)""",
    """CREATE INDEX agents_kind_idx ON public.agents USING btree (kind)""",
    """CREATE INDEX idx_credit_tx_user ON public.credit_transactions USING btree (user_id, created_at DESC)""",
    """CREATE INDEX idx_payments_provider ON public.payments USING btree (provider_order_id) WHERE ((provider_order_id)::text <> ''::text)""",
    """CREATE INDEX idx_payments_user ON public.payments USING btree (user_id, created_at DESC)""",
    """CREATE INDEX ix_agent_sessions_tenant_user_created ON public.agent_sessions USING btree (tenant_id, user_id, created_at DESC)""",
    """CREATE INDEX ix_audit_logs_tenant_created ON public.audit_logs USING btree (tenant_id, created_at DESC)""",
    """CREATE INDEX ix_ent_webhooks_tenant_enabled ON public.ent_webhooks USING btree (tenant_id, enabled)""",
    """CREATE INDEX ix_kb_documents_kb_tenant ON public.knowledge_documents USING btree (knowledge_base_id, tenant_id)""",
    """CREATE INDEX ix_llm_provider_keys_provider ON public.llm_provider_keys USING btree (provider)""",
    """CREATE INDEX ix_media_assets_tenant_user_parent ON public.media_assets USING btree (tenant_id, user_id, parent_id)""",
    """CREATE INDEX ix_messages_session_created ON public.messages USING btree (session_id, created_at DESC, id DESC)""",
    """CREATE INDEX ix_messages_turn_id ON public.messages USING btree (turn_id)""",
    """CREATE INDEX ix_sessions_parent_session_id ON public.sessions USING btree (parent_session_id)""",
    """CREATE INDEX ix_sessions_tag ON public.sessions USING btree (tag)""",
    """CREATE INDEX ix_task_idempotency_status ON public.task_idempotency USING btree (status)""",
    """CREATE INDEX ix_task_idempotency_status_updated ON public.task_idempotency USING btree (status, updated_at)""",
    """CREATE INDEX ix_task_idempotency_task_id ON public.task_idempotency USING btree (task_id)""",
    """CREATE INDEX ix_tool_calls_turn_id ON public.tool_calls USING btree (turn_id)""",
    """CREATE INDEX ix_turns_session_id ON public.turns USING btree (session_id)""",
    """CREATE INDEX ix_turns_status ON public.turns USING btree (status)""",
    """CREATE INDEX ix_turns_status_created ON public.turns USING btree (status, created_at)""",
    """CREATE INDEX ix_uploads_tenant_user_status ON public.uploads USING btree (tenant_id, user_id, status)""",
    """CREATE INDEX ix_user_memory_entries_lookup ON public.user_memory_entries USING btree (tenant_id, user_id, status)""",
    """CREATE INDEX ix_users_tenant_created ON public.users USING btree (tenant_id, created_at DESC)""",
    """CREATE INDEX session_map_nodes_session_idx ON public.session_map_nodes USING btree (session_id)""",
    """CREATE INDEX session_map_nodes_workspace_idx ON public.session_map_nodes USING btree (workspace_id)""",
    """CREATE INDEX session_map_workspaces_owner_idx ON public.session_map_workspaces USING btree (tenant_id, user_id)""",
    """CREATE UNIQUE INDEX subagent_run_steps_seq_idx ON public.subagent_run_steps USING btree (run_id, seq)""",
    """CREATE INDEX subagent_runs_parent_idx ON public.subagent_runs USING btree (parent_run_id)""",
    """CREATE INDEX subagent_runs_tenant_idx ON public.subagent_runs USING btree (tenant_id, created_at)""",
    """CREATE INDEX subagent_runs_tree_idx ON public.subagent_runs USING btree (root_session_id, created_at)""",
    """CREATE INDEX subagent_runs_turn_idx ON public.subagent_runs USING btree (turn_id)""",
    """CREATE INDEX turns_session_created_idx ON public.turns USING btree (session_id, created_at)""",
    """CREATE UNIQUE INDEX uniq_billing_records_turn ON public.billing_records USING btree (turn_id)""",
    """CREATE UNIQUE INDEX uniq_credit_tx_turn ON public.credit_transactions USING btree (turn_id)""",
    """ALTER TABLE ONLY public.admin_api_call_logs
    ADD CONSTRAINT admin_api_call_logs_api_key_id_fkey FOREIGN KEY (api_key_id) REFERENCES public.admin_api_keys(id)""",
    """ALTER TABLE ONLY public.admin_domains
    ADD CONSTRAINT admin_domains_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.admin_tenants(tenant_id)""",
    """ALTER TABLE ONLY public.admin_tenant_usage
    ADD CONSTRAINT admin_tenant_usage_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.admin_tenants(tenant_id)""",
    """ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id)""",
    """ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.agent_sessions
    ADD CONSTRAINT agent_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.billing_records
    ADD CONSTRAINT billing_records_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id)""",
    """ALTER TABLE ONLY public.billing_records
    ADD CONSTRAINT billing_records_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.billing_records
    ADD CONSTRAINT billing_records_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.credit_transactions
    ADD CONSTRAINT credit_transactions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.domains
    ADD CONSTRAINT domains_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.ent_captcha_config
    ADD CONSTRAINT ent_captcha_config_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.ent_groups
    ADD CONSTRAINT ent_groups_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.ent_mail_config
    ADD CONSTRAINT ent_mail_config_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.ent_model_policies
    ADD CONSTRAINT ent_model_policies_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.ent_roles(id)""",
    """ALTER TABLE ONLY public.ent_model_policies
    ADD CONSTRAINT ent_model_policies_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.ent_oidc_providers
    ADD CONSTRAINT ent_oidc_providers_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.ent_quota_allocations
    ADD CONSTRAINT ent_quota_allocations_pool_id_fkey FOREIGN KEY (pool_id) REFERENCES public.ent_quota_pools(id)""",
    """ALTER TABLE ONLY public.ent_quota_pools
    ADD CONSTRAINT ent_quota_pools_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.ent_roles
    ADD CONSTRAINT ent_roles_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.ent_sms_config
    ADD CONSTRAINT ent_sms_config_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.ent_user_identities
    ADD CONSTRAINT ent_user_identities_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.ent_oidc_providers(id)""",
    """ALTER TABLE ONLY public.ent_user_identities
    ADD CONSTRAINT ent_user_identities_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.ent_webhooks
    ADD CONSTRAINT ent_webhooks_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.enterprise_tasks
    ADD CONSTRAINT enterprise_tasks_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.kb_articles
    ADD CONSTRAINT kb_articles_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.knowledge_bases
    ADD CONSTRAINT knowledge_bases_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.knowledge_bases
    ADD CONSTRAINT knowledge_bases_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.knowledge_chunks
    ADD CONSTRAINT knowledge_chunks_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.knowledge_documents(id)""",
    """ALTER TABLE ONLY public.knowledge_chunks
    ADD CONSTRAINT knowledge_chunks_knowledge_base_id_fkey FOREIGN KEY (knowledge_base_id) REFERENCES public.knowledge_bases(id)""",
    """ALTER TABLE ONLY public.knowledge_chunks
    ADD CONSTRAINT knowledge_chunks_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.knowledge_documents
    ADD CONSTRAINT knowledge_documents_knowledge_base_id_fkey FOREIGN KEY (knowledge_base_id) REFERENCES public.knowledge_bases(id)""",
    """ALTER TABLE ONLY public.knowledge_documents
    ADD CONSTRAINT knowledge_documents_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.knowledge_documents
    ADD CONSTRAINT knowledge_documents_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.marketing_campaigns
    ADD CONSTRAINT marketing_campaigns_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.media_assets
    ADD CONSTRAINT media_assets_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.meeting_notes
    ADD CONSTRAINT meeting_notes_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id)""",
    """ALTER TABLE ONLY public.okrs
    ADD CONSTRAINT okrs_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.session_map_nodes
    ADD CONSTRAINT session_map_nodes_parent_node_id_fkey FOREIGN KEY (parent_node_id) REFERENCES public.session_map_nodes(id) ON DELETE SET NULL""",
    """ALTER TABLE ONLY public.session_map_nodes
    ADD CONSTRAINT session_map_nodes_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE""",
    """ALTER TABLE ONLY public.session_map_nodes
    ADD CONSTRAINT session_map_nodes_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.session_map_workspaces(id) ON DELETE CASCADE""",
    """ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id)""",
    """ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_parent_session_id_fkey FOREIGN KEY (parent_session_id) REFERENCES public.sessions(id) ON DELETE SET NULL""",
    """ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.subagent_run_steps
    ADD CONSTRAINT subagent_run_steps_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.subagent_runs(id) ON DELETE CASCADE""",
    """ALTER TABLE ONLY public.subagent_runs
    ADD CONSTRAINT subagent_runs_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL""",
    """ALTER TABLE ONLY public.support_tickets
    ADD CONSTRAINT support_tickets_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
    """ALTER TABLE ONLY public.turns
    ADD CONSTRAINT turns_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id)""",
    """ALTER TABLE ONLY public.unified_messages
    ADD CONSTRAINT unified_messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.unified_sessions(id)""",
    """ALTER TABLE ONLY public.uploads
    ADD CONSTRAINT uploads_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.wiki_pages
    ADD CONSTRAINT wiki_pages_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES public.tenants(id)""",
    """ALTER TABLE ONLY public.workflow_graphs
    ADD CONSTRAINT workflow_graphs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id)""",
]


_TABLES_IN_CREATION_ORDER = [
    'admin_api_call_logs',
    'admin_api_keys',
    'admin_cron_jobs',
    'admin_database_backups',
    'admin_db_configs',
    'admin_domains',
    'admin_model_configs',
    'admin_redis_configs',
    'admin_tenant_usage',
    'admin_tenants',
    'admin_workflow_executions',
    'admin_workflows',
    'agent_registry',
    'agent_sessions',
    'agents',
    'api_keys',
    'audit_logs',
    'billing_records',
    'conversation_shares',
    'credit_transactions',
    'cron_jobs',
    'domains',
    'ent_captcha_config',
    'ent_catalog_installs',
    'ent_catalog_items',
    'ent_group_members',
    'ent_group_roles',
    'ent_groups',
    'ent_mail_config',
    'ent_model_policies',
    'ent_model_routes',
    'ent_oidc_providers',
    'ent_quota_allocations',
    'ent_quota_pools',
    'ent_roles',
    'ent_sms_config',
    'ent_templates',
    'ent_tenant_policies',
    'ent_user_identities',
    'ent_user_roles',
    'ent_webhooks',
    'enterprise_tasks',
    'guest_storage',
    'kb_articles',
    'knowledge_bases',
    'knowledge_chunks',
    'knowledge_documents',
    'llm_models',
    'llm_provider_keys',
    'marketing_campaigns',
    'media_assets',
    'meeting_notes',
    'memory_summaries',
    'messages',
    'okrs',
    'payments',
    'session_map_nodes',
    'session_map_workspaces',
    'sessions',
    'stripe_payments',
    'subagent_run_steps',
    'subagent_runs',
    'support_tickets',
    'system_settings',
    'task_idempotency',
    'tasks',
    'tenants',
    'tool_calls',
    'turns',
    'unified_messages',
    'unified_sessions',
    'uploads',
    'user_memory_entries',
    'user_memory_profile',
    'users',
    'wiki_pages',
    'workflow_graphs',
    'workflow_instances',
]


def upgrade() -> None:
    for stmt in _UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    # 逆序删除；CASCADE 处理外键依赖（仅限本迁移建立的表）
    for table in reversed(_TABLES_IN_CREATION_ORDER):
        op.execute(f"DROP TABLE IF EXISTS public.{table} CASCADE")

