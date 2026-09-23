create table admin_api_keys
(
    id             varchar(36) not null
        primary key,
    key_hash       varchar(64)
        unique,
    name           varchar(100),
    tenant_id      varchar(50),
    user_id        varchar(50),
    monthly_quota  integer,
    used_count     bigint,
    used_credits   bigint,
    status         varchar(20),
    expires_at     timestamp,
    created_at     timestamp,
    updated_at     timestamp,
    created_by     varchar(50),
    description    text,
    allowed_models varchar(255),
    rate_limit_qps integer
);

create table admin_cron_jobs
(
    id              varchar(36) not null
        primary key,
    job_id          varchar(50)
        unique,
    name            varchar(100),
    schedule        varchar(50),
    last_run_at     timestamp,
    last_run_status varchar(20),
    last_error      text,
    next_run_at     timestamp,
    enabled         boolean,
    metadata        json,
    created_at      timestamp,
    updated_at      timestamp
);

create table admin_database_backups
(
    id               varchar(36) not null
        primary key,
    backup_type      varchar(20),
    description      text,
    file_path        varchar(500),
    file_size_mb     varchar(255),
    status           varchar(20),
    error_message    text,
    started_at       timestamp,
    completed_at     timestamp,
    duration_seconds integer,
    created_by       varchar(50)
);

create table admin_db_configs
(
    id                   varchar(36) not null
        primary key,
    dsn                  varchar(500),
    host                 varchar(100),
    port                 integer,
    dbname               varchar(100),
    max_open_connections integer,
    max_idle_connections integer,
    conn_max_lifetime    varchar(255),
    status               varchar(20),
    last_health_check    timestamp,
    avg_query_time_ms    varchar(255),
    database_size_mb     varchar(255),
    total_tables         integer,
    sequential_scans     bigint,
    created_at           timestamp,
    updated_at           timestamp
);

create table admin_model_configs
(
    id                 varchar(36) not null
        primary key,
    model_id           varchar(50)
        unique,
    display_name       varchar(100),
    provider           varchar(50),
    priority           integer,
    weight             integer,
    fallback_chain     varchar(255),
    max_rpm            integer,
    max_tpm            integer,
    concurrent_limit   integer,
    status             varchar(20),
    is_default         boolean,
    input_cost_per_1m  varchar(255),
    output_cost_per_1m varchar(255),
    config_json        json,
    created_at         timestamp,
    updated_at         timestamp
);

create table admin_redis_configs
(
    id                   varchar(36) not null
        primary key,
    host                 varchar(100),
    port                 integer,
    password_hash        varchar(256),
    db_index             integer,
    pool_size            integer,
    min_idle_connections integer,
    max_conn_age         varchar(255),
    status               varchar(20),
    last_health_check    timestamp,
    avg_latency_ms       varchar(255),
    memory_used_mb       varchar(255),
    connected_clients    integer,
    hits                 bigint,
    misses               bigint,
    created_at           timestamp,
    updated_at           timestamp
);

create table admin_tenants
(
    id                      varchar(36) not null
        primary key,
    tenant_id               varchar(50)
        unique,
    name                    varchar(100),
    company_name            varchar(200),
    contact_email           varchar(100),
    contact_phone           varchar(20),
    max_api_keys            integer,
    max_models              integer,
    monthly_quota           bigint,
    max_concurrent_sessions integer,
    status                  varchar(20),
    expires_at              timestamp,
    created_at              timestamp,
    updated_at              timestamp,
    created_by              varchar(50),
    features                json
);

create table admin_workflow_executions
(
    id               varchar(36) not null
        primary key,
    workflow_id      varchar(50),
    workflow_version integer,
    status           varchar(20),
    started_at       timestamp,
    completed_at     timestamp,
    duration_ms      integer,
    input_data       json,
    output_data      json,
    error_message    text,
    triggered_by     varchar(50),
    node_results     json
);

create table admin_workflows
(
    id                      varchar(36) not null
        primary key,
    workflow_id             varchar(50)
        unique,
    name                    varchar(100),
    description             text,
    nodes                   json,
    edges                   json,
    error_handling_strategy varchar(20),
    timeout_ms              integer,
    max_retries             integer,
    version                 integer,
    published_version       integer,
    status                  varchar(20),
    created_by              varchar(50),
    created_at              timestamp,
    updated_at              timestamp,
    published_at            timestamp
);

create table agent_registry
(
    agent_type  varchar(32) not null
        primary key,
    name        varchar(128),
    description text        not null,
    enabled     boolean,
    config      json,
    created_at  timestamp
);

create table conversation_shares
(
    id          varchar(36) not null
        primary key,
    session_id  varchar(128),
    user_id     varchar(36),
    title       varchar(255),
    message_ids varchar(255),
    created_at  timestamp,
    revoked_at  timestamp
);

create table cron_jobs
(
    id            varchar(36) not null
        primary key,
    name          varchar(128),
    schedule      varchar(64),
    task          varchar(255),
    enabled       boolean,
    last_run_at   timestamp,
    last_status   varchar(16),
    created_at    timestamp,
    updated_at    timestamp,
    tenant_id     varchar(36),
    user_id       varchar(36),
    webhook_token varchar(64)
);

create table ent_catalog_installs
(
    item_id      varchar(36) not null,
    tenant_id    varchar(36) not null,
    enabled      boolean,
    installed_at timestamp,
    primary key (item_id, tenant_id)
);

create table ent_catalog_items
(
    id         varchar(36) not null
        primary key,
    type       varchar(8),
    name       varchar(128),
    version    varchar(32),
    manifest   json,
    status     varchar(16),
    created_by varchar(36),
    created_at timestamp,
    updated_at timestamp
);

create table ent_group_members
(
    group_id varchar(36) not null,
    user_id  varchar(36) not null,
    primary key (group_id, user_id)
);

create table ent_group_roles
(
    group_id varchar(36) not null,
    role_id  varchar(36) not null,
    primary key (group_id, role_id)
);

create table ent_templates
(
    id          varchar(36) not null
        primary key,
    type        varchar(16),
    name        varchar(128),
    description text        not null,
    payload     json,
    published   boolean,
    created_at  timestamp,
    updated_at  timestamp
);

create table ent_tenant_policies
(
    tenant_id           varchar(36) not null
        primary key,
    privacy_mode        boolean,
    data_retention_days integer,
    training_allowed    boolean,
    redaction_rules     json,
    updated_at          timestamp
);

create table ent_user_roles
(
    user_id varchar(36) not null,
    role_id varchar(36) not null,
    primary key (user_id, role_id)
);

create table guest_storage
(
    client_id  varchar(64) not null
        primary key,
    storage_id varchar(64)
        unique,
    created_at timestamp
);

create table llm_models
(
    id             varchar(36) not null
        primary key,
    provider       varchar(32),
    name           varchar(128),
    display_name   varchar(128),
    enabled        boolean,
    context_window integer,
    created_at     timestamp,
    updated_at     timestamp
);

create table memory_summaries
(
    id               varchar(64) not null
        primary key,
    tenant_id        varchar(64),
    user_id          varchar(64),
    session_id       varchar(64),
    content          text        not null,
    topics           json,
    entities         json,
    turn_start       integer,
    turn_end         integer,
    content_hash     varchar(80),
    access_count     integer,
    last_accessed_at timestamp,
    status           varchar(16),
    created_at       timestamp
);

create table payments
(
    id                varchar(64) not null
        primary key,
    user_id           varchar(36),
    channel           varchar(16),
    credits           integer,
    amount_cents      bigint,
    currency          varchar(8),
    status            varchar(16),
    qr_code           text,
    provider_order_id varchar(64),
    trade_no          varchar(64),
    created_at        timestamp,
    paid_at           timestamp,
    expired_at        timestamp
);

create index idx_payments_provider
    on payments (provider_order_id)
    where ((provider_order_id)::text <> ''::text);

create index idx_payments_user
    on payments (user_id asc, created_at desc);

create table schema_migrations
(
    version    bigserial
        primary key,
    name       varchar(255),
    checksum   varchar(128),
    applied_at varchar(255)
);

create table stripe_payments
(
    session_id   varchar(128) not null
        primary key,
    user_id      varchar(36),
    credits      integer,
    amount_cents bigint,
    status       varchar(16),
    created_at   timestamp,
    completed_at timestamp
);

create table system_settings
(
    id         serial
        primary key,
    category   varchar(32),
    key        varchar(64),
    value      json,
    updated_at timestamp,
    updated_by varchar(36),
    encrypted  boolean
);

create table tenants
(
    id         varchar(36) not null
        primary key,
    name       varchar(255),
    created_at timestamp,
    status     varchar(16)
);

create table tool_calls
(
    id          varchar(36) not null
        primary key,
    session_id  varchar(36),
    message_id  varchar(36),
    tool_name   varchar(128),
    input       json,
    output      text        not null,
    is_error    boolean,
    duration_ms bigint,
    created_at  timestamp,
    turn_id     varchar(36)
);

create index ix_tool_calls_turn_id
    on tool_calls (turn_id);

create table unified_sessions
(
    id             varchar(36)               not null
        primary key,
    tenant_id      varchar(36),
    user_id        varchar(36),
    title          varchar(255),
    mode           varchar(16),
    shared_context json,
    created_at     timestamp,
    updated_at     timestamp,
    runtime        jsonb default '{}'::jsonb not null
);

create table user_memory_profile
(
    tenant_id          varchar(64)  not null,
    user_id            varchar(64)  not null,
    slot               varchar(32)  not null,
    item_key           varchar(128) not null,
    item_value         json,
    confidence         integer,
    source             varchar(16),
    version            integer,
    confirmed_at       timestamp,
    last_referenced_at timestamp,
    created_at         timestamp,
    updated_at         timestamp,
    primary key (tenant_id, user_id, slot, item_key)
);

create table workflow_instances
(
    id            varchar(64) not null
        primary key,
    user_id       varchar(64),
    workflow_id   varchar(64),
    workflow_name varchar(255),
    status        varchar(16),
    results       json,
    error         text,
    created_at    timestamp,
    updated_at    timestamp
);

create table admin_api_call_logs
(
    id                  varchar(36) not null
        primary key,
    api_key_id          varchar(36)
        references admin_api_keys,
    model_id            varchar(50),
    workflow_id         varchar(50),
    endpoint            varchar(100),
    method              varchar(10),
    request_size_bytes  integer,
    response_size_bytes integer,
    duration_ms         integer,
    status_code         integer,
    retry_count         integer,
    input_tokens        integer,
    output_tokens       integer,
    credits_consumed    bigint,
    created_at          timestamp
);

create table admin_domains
(
    id             varchar(36) not null
        primary key,
    domain         varchar(100)
        unique,
    tenant_id      varchar(50)
        references admin_tenants (tenant_id),
    dns_provider   varchar(50),
    dns_record_id  varchar(100),
    cname_target   varchar(200),
    ssl_status     varchar(20),
    ssl_expires_at timestamp,
    auto_renew     boolean,
    status         varchar(20),
    verified_at    timestamp,
    verified_by    varchar(50),
    created_at     timestamp,
    updated_at     timestamp
);

create table admin_tenant_usage
(
    id               varchar(36) not null
        primary key,
    tenant_id        varchar(50)
        references admin_tenants (tenant_id),
    stat_date        varchar(255),
    api_calls        bigint,
    tokens_used      bigint,
    credits_consumed bigint,
    storage_mb       varchar(255),
    created_at       timestamp
);

create table agents
(
    id              varchar(36)                                   not null
        primary key,
    tenant_id       varchar(36)
        references tenants,
    name            varchar(255),
    description     text,
    system_prompt   text,
    tools           json,
    llm_config      json,
    max_turns       integer,
    timeout_seconds integer,
    enabled         boolean,
    created_at      timestamp,
    updated_at      timestamp,
    user_id         varchar(36),
    visibility      varchar(16),
    kb_id           varchar(255),
    skills          json,
    plugins         json,
    workflows       json,
    kind            varchar(16) default 'chat'::character varying not null
);

create index agents_kind_idx
    on agents (kind);

create table domains
(
    id         varchar(36) not null
        primary key,
    tenant_id  varchar(36)
        references tenants,
    domain     varchar(255)
        unique,
    ssl_status varchar(16),
    verified   boolean,
    created_at timestamp,
    updated_at timestamp
);

create table ent_captcha_config
(
    id         varchar(36) not null
        primary key,
    tenant_id  varchar(36)
        references tenants,
    provider   varchar(32),
    site_key   varchar(256),
    secret_enc text        not null,
    verify_url varchar(512),
    enabled    boolean,
    created_at timestamp,
    updated_at timestamp
);

create table ent_groups
(
    id          varchar(36) not null
        primary key,
    tenant_id   varchar(36)
        references tenants,
    name        varchar(128),
    description text,
    created_at  timestamp
);

-- 邮件发信配置（后台「邮件配置」）。单租户单行：tenant_id 唯一，
-- 保存路径靠 ON CONFLICT (tenant_id) 做 upsert，因此这里必须是唯一约束。
-- 凭据列 (*_enc) 与 ent_sms_config.secret_enc 同口径：AES-256-GCM 密文。
create table ent_mail_config
(
    id                    varchar(36) not null
        primary key,
    tenant_id             varchar(36) not null
        unique
        references tenants,
    provider              varchar(32),
    enabled               boolean default false not null,
    smtp_host             varchar(255),
    smtp_port             integer,
    smtp_username         varchar(255),
    smtp_password_enc     text,
    smtp_security         varchar(16),
    smtp_skip_verify      boolean default false not null,
    api_base_url          varchar(512),
    api_key_enc           text,
    api_channel_id        integer,
    api_template_id       integer,
    from_address          varchar(255),
    from_name             varchar(128),
    reply_to              varchar(255),
    site_name             varchar(128),
    app_base_url          varchar(512),
    login_enabled         boolean default false not null,
    register_verify       boolean default false not null,
    auto_register         boolean default false not null,
    reset_enabled         boolean default false not null,
    welcome_enabled       boolean default true not null,
    code_subject          varchar(255),
    code_body             text,
    welcome_subject       varchar(255),
    welcome_body          text,
    reset_subject         varchar(255),
    reset_body            text,
    code_ttl_seconds      integer default 300 not null,
    send_interval_seconds integer default 60 not null,
    daily_limit           integer default 10 not null,
    timeout_seconds       integer default 30 not null,
    created_at            timestamp,
    updated_at            timestamp
);

create table ent_oidc_providers
(
    id                varchar(36) not null
        primary key,
    tenant_id         varchar(36)
        references tenants,
    name              varchar(64),
    issuer            varchar(512),
    client_id         varchar(256),
    client_secret_enc text        not null,
    scopes            varchar(255),
    enabled           boolean,
    auto_provision    boolean,
    role_mapping      json,
    created_at        timestamp,
    updated_at        timestamp,
    protocol          varchar(16),
    provider_type     varchar(32),
    display_name      varchar(64),
    icon              varchar(64),
    sort_order        integer,
    auth_url          varchar(512),
    token_url         varchar(512),
    userinfo_url      varchar(512),
    extra             json
);

create table ent_quota_pools
(
    id            varchar(36) not null
        primary key,
    tenant_id     varchar(36)
        references tenants,
    resource_type varchar(20),
    total_amount  bigint,
    period        varchar(10),
    created_at    timestamp,
    updated_at    timestamp
);

create table ent_roles
(
    id           varchar(36) not null
        primary key,
    tenant_id    varchar(36)
        references tenants,
    name         varchar(64),
    display_name varchar(128),
    is_builtin   boolean,
    permissions  varchar(255),
    created_at   timestamp,
    updated_at   timestamp
);

-- 短信发信配置（后台「短信配置」/ 短信验证码登录）。单租户单行：tenant_id 作主键，
-- 同时满足 ON CONFLICT (tenant_id) 的 upsert。列的可空性以 internal/api/sms_handler.go
-- 的读取方式为准：除 endpoint 外都扫进非指针 Go 变量，故必须 NOT NULL + 默认值；
-- 该表的 INSERT 不写 id，因此这里不设 id 列。
create table ent_sms_config
(
    tenant_id             varchar(36) not null
        primary key
        references tenants,
    provider              varchar(32)  default 'aliyun' not null,
    sign_name             varchar(64)  default '' not null,
    template_id           varchar(64)  default '' not null,
    access_key_id         varchar(256) default '' not null,
    secret_enc            text         default '' not null,
    endpoint              varchar(512),
    code_ttl_seconds      integer      default 300 not null,
    send_interval_seconds integer      default 60 not null,
    daily_limit           integer      default 10 not null,
    login_enabled         boolean      default false not null,
    auto_register         boolean      default false not null,
    enabled               boolean      default false not null,
    created_at            timestamp    default now(),
    updated_at            timestamp    default now()
);

create table enterprise_tasks
(
    id          varchar(36) not null
        primary key,
    tenant_id   varchar(36)
        references tenants,
    user_id     varchar(36),
    title       varchar(255),
    description text        not null,
    project     varchar(128),
    assignee    varchar(128),
    priority    varchar(16),
    status      varchar(16),
    created_at  timestamp,
    updated_at  timestamp
);

create table kb_articles
(
    id         varchar(36) not null
        primary key,
    tenant_id  varchar(36)
        references tenants,
    user_id    varchar(36),
    title      varchar(255),
    content    text        not null,
    tags       varchar(255),
    category   varchar(64),
    created_at timestamp,
    updated_at timestamp
);

create table marketing_campaigns
(
    id            varchar(36) not null
        primary key,
    tenant_id     varchar(36)
        references tenants,
    user_id       varchar(36),
    name          varchar(255),
    description   text        not null,
    campaign_type varchar(32),
    config        json,
    status        varchar(16),
    created_at    timestamp,
    updated_at    timestamp
);

create table media_assets
(
    id         varchar(36) not null
        primary key,
    tenant_id  varchar(36)
        references tenants,
    user_id    varchar(36),
    type       varchar(16),
    name       varchar(255),
    file_url   varchar(1024),
    file_path  varchar(512),
    mime_type  varchar(64),
    thumbnail  varchar(512),
    metadata   json,
    tags       varchar(255),
    category   varchar(64),
    size       bigint,
    created_at timestamp,
    updated_at timestamp,
    parent_id  varchar(64)
);

create index ix_media_assets_tenant_user_parent
    on media_assets (tenant_id, user_id, parent_id);

create table meeting_notes
(
    id           varchar(36) not null
        primary key,
    tenant_id    varchar(36)
        references tenants,
    user_id      varchar(36),
    title        varchar(255),
    notes        text        not null,
    summary      text,
    participants varchar(255),
    date         varchar(255),
    created_at   timestamp
);

create table okrs
(
    id          varchar(36) not null
        primary key,
    tenant_id   varchar(36)
        references tenants,
    user_id     varchar(36),
    objective   varchar(255),
    key_results json,
    quarter     varchar(16),
    status      varchar(16),
    created_at  timestamp,
    updated_at  timestamp
);

create table support_tickets
(
    id          varchar(36) not null
        primary key,
    tenant_id   varchar(36)
        references tenants,
    user_id     varchar(36),
    subject     varchar(255),
    description text        not null,
    priority    varchar(16),
    status      varchar(16),
    assignee    varchar(128),
    created_at  timestamp,
    updated_at  timestamp
);

create table unified_messages
(
    id         serial
        primary key,
    session_id varchar(36)
        references unified_sessions,
    role       varchar(16),
    content    text not null,
    metadata   json,
    error      text not null,
    created_at timestamp
);

create table uploads
(
    id              varchar(64) not null
        primary key,
    user_id         varchar(64),
    name            varchar(255),
    size            bigint,
    mime_type       varchar(64),
    purpose         varchar(16),
    parent_id       varchar(64),
    category        varchar(64),
    chunk_size      integer,
    chunk_count     integer,
    chunks_received varchar(255),
    status          varchar(16),
    created_at      timestamp,
    updated_at      timestamp,
    tenant_id       varchar(36)
        references tenants
);

create index ix_uploads_tenant_user_status
    on uploads (tenant_id, user_id, status);

create table users
(
    id            varchar(36) not null
        primary key,
    tenant_id     varchar(36)
        references tenants,
    email         varchar(255),
    name          varchar(128),
    password_hash varchar(255),
    role          varchar(16),
    storage_id    varchar(64)
        unique,
    credits       integer,
    created_at    timestamp,
    updated_at    timestamp,
    phone         varchar(32),
    password_set  boolean,
    settings      json
);

create index ix_users_tenant_created
    on users (tenant_id asc, created_at desc);

create table wiki_pages
(
    id         varchar(36) not null
        primary key,
    tenant_id  varchar(36)
        references tenants,
    user_id    varchar(36),
    title      varchar(255),
    content    text        not null,
    tags       varchar(255),
    created_at timestamp,
    updated_at timestamp
);

create table agent_sessions
(
    id         varchar(128) not null
        primary key,
    user_id    varchar(36)
        references users,
    agent_id   varchar(36)
        references agents,
    name       varchar(128),
    task       text         not null,
    status     varchar(16),
    result     text,
    created_at timestamp,
    updated_at timestamp,
    tenant_id  varchar(36)
        references tenants
);

create index ix_agent_sessions_tenant_user_created
    on agent_sessions (tenant_id asc, user_id asc, created_at desc);

create table api_keys
(
    id           varchar(36) not null
        primary key,
    user_id      varchar(36)
        references users,
    name         varchar(128),
    key_hash     varchar(64),
    last_used_at timestamp,
    expires_at   timestamp,
    created_at   timestamp,
    revoked      boolean
);

create table audit_logs
(
    id            varchar(36) not null
        primary key,
    tenant_id     varchar(36)
        references tenants,
    user_id       varchar(36)
        references users,
    action        varchar(64),
    resource_type varchar(64),
    resource_id   varchar(64),
    details       json,
    ip_address    varchar(45),
    created_at    timestamp
);

create index ix_audit_logs_tenant_created
    on audit_logs (tenant_id asc, created_at desc);

create table credit_transactions
(
    id         varchar(36) not null
        primary key,
    user_id    varchar(36)
        references users,
    amount     integer,
    balance    integer,
    reason     varchar(64),
    created_at timestamp,
    turn_id    varchar(36)
);

create index idx_credit_tx_user
    on credit_transactions (user_id asc, created_at desc);

create unique index uniq_credit_tx_turn
    on credit_transactions (turn_id);

create table ent_model_policies
(
    id               varchar(36) not null
        primary key,
    tenant_id        varchar(36)
        references tenants,
    role_id          varchar(36)
        references ent_roles,
    allowed_models   varchar(255),
    per_model_limits json,
    created_at       timestamp,
    updated_at       timestamp
);

-- 企业 Webhook 订阅（ent_webhook_handler.go 的 CRUD + ent_webhook_dispatcher.go 的投递）。
-- event_types / retry_policy 必须是 jsonb：分发侧用 `event_types::jsonb @> to_jsonb($2::text)`
-- 做包含匹配；写入侧传的是 json.Marshal 出来的字节（pgx 的 JSON 编解码器直接接受）。
-- 其余列 NOT NULL：scanWebhookRow 把除 enabled 外的列全部扫进非指针 Go 变量，
-- NULL 会让 pgx 直接报错。url 用 text：Create 未做长度校验，避免列宽引入额外失败。
create table ent_webhooks
(
    id           varchar(36) not null
        primary key,
    tenant_id    varchar(36) not null
        references tenants,
    name         varchar(255) default '' not null,
    event_types  jsonb        default '[]'::jsonb not null,
    url          text         default '' not null,
    secret       text         default '' not null,
    enabled      boolean      default true not null,
    retry_policy jsonb        default '{}'::jsonb not null,
    created_at   timestamp    default now() not null,
    updated_at   timestamp    default now() not null
);

create index ix_ent_webhooks_tenant_enabled
    on ent_webhooks (tenant_id, enabled);

create table ent_quota_allocations
(
    id          varchar(36) not null
        primary key,
    pool_id     varchar(36)
        references ent_quota_pools,
    target_type varchar(10),
    target_id   varchar(36),
    amount      bigint,
    created_at  timestamp
);

create table ent_user_identities
(
    id          varchar(36) not null
        primary key,
    user_id     varchar(36)
        references users,
    provider_id varchar(36)
        references ent_oidc_providers,
    subject     varchar(256),
    email       varchar(255),
    created_at  timestamp
);

create table knowledge_bases
(
    id               varchar(36) not null
        primary key,
    tenant_id        varchar(36)
        references tenants,
    user_id          varchar(36)
        references users,
    name             varchar(255),
    description      text,
    type             varchar(32),
    visibility       varchar(32),
    status           varchar(32),
    document_count   integer,
    total_size_bytes bigint,
    credits_consumed integer,
    config           json,
    created_at       timestamp,
    updated_at       timestamp,
    doc_count        integer
);

create table sessions
(
    id                varchar(36) not null
        primary key,
    tenant_id         varchar(36)
        references tenants,
    user_id           varchar(36)
        references users,
    agent_id          varchar(36)
        references agents,
    title             varchar(255),
    status            varchar(16),
    created_at        timestamp,
    updated_at        timestamp,
    pinned            boolean,
    tag               varchar(64),
    parent_session_id varchar(36)
                                  references sessions
                                      on delete set null,
    branch_from_seq   integer,
    alias             varchar(64),
    branch_mode       varchar(16),
    branch_state      varchar(16),
    branch_keep_tail  integer
);

create index ix_sessions_tag
    on sessions (tag);

create index ix_sessions_parent_session_id
    on sessions (parent_session_id);

create table tasks
(
    id          varchar(36) not null
        primary key,
    user_id     varchar(36)
        references users,
    type        varchar(32),
    status      varchar(16),
    priority    integer,
    payload     json,
    result      json,
    error       text,
    retries     integer,
    max_retries integer,
    created_at  timestamp,
    updated_at  timestamp
);

create table workflow_graphs
(
    id         varchar(36) not null
        primary key,
    name       varchar(255),
    user_id    varchar(36)
        references users,
    graph_json json,
    created_at timestamp,
    updated_at timestamp
);

create table billing_records
(
    id            varchar(36) not null
        primary key,
    tenant_id     varchar(36)
        references tenants,
    user_id       varchar(36)
        references users,
    session_id    varchar(36)
        references sessions,
    input_tokens  bigint,
    output_tokens bigint,
    cost_cents    integer,
    created_at    timestamp,
    group_id      varchar(36),
    turn_id       varchar(36)
);

create unique index uniq_billing_records_turn
    on billing_records (turn_id);

create table knowledge_documents
(
    id                varchar(36) not null
        primary key,
    knowledge_base_id varchar(36)
        references knowledge_bases,
    tenant_id         varchar(36)
        references tenants,
    user_id           varchar(36)
        references users,
    name              varchar(255),
    file_url          varchar(1024),
    file_type         varchar(32),
    file_size_bytes   bigint,
    chunk_count       integer,
    status            varchar(32),
    error_message     text,
    metadata          json,
    created_at        timestamp,
    updated_at        timestamp,
    content           varchar(255)
);

create index ix_kb_documents_kb_tenant
    on knowledge_documents (knowledge_base_id, tenant_id);

create table messages
(
    id         varchar(36)                               not null
        primary key,
    session_id varchar(36)
        references sessions,
    role       varchar(16),
    content    text                                      not null,
    tool_calls json,
    created_at timestamp,
    turn_id    varchar(36),
    source     varchar(32) default ''::character varying not null
);

comment on column messages.source is '消息来源：空=用户输入；subagent_followup=子 Agent 自动轮注入';

create index ix_messages_turn_id
    on messages (turn_id);

create index ix_messages_session_created
    on messages (session_id asc, created_at desc, id desc);

create table turns
(
    id            varchar(36)           not null
        primary key,
    session_id    varchar(36)
        references sessions,
    user_id       varchar(36),
    status        varchar(16),
    error         text,
    input_tokens  bigint,
    output_tokens bigint,
    started_at    timestamp,
    finished_at   timestamp,
    created_at    timestamp,
    cached_tokens bigint  default 0     not null,
    cache_hit     boolean default false not null,
    model         varchar(128)
);

create index ix_turns_session_id
    on turns (session_id);

create index ix_turns_status
    on turns (status);

create index ix_turns_status_created
    on turns (status, created_at);

create index turns_session_created_idx
    on turns (session_id, created_at);

create table knowledge_chunks
(
    id                varchar(36) not null
        primary key,
    document_id       varchar(36)
        references knowledge_documents,
    knowledge_base_id varchar(36)
        references knowledge_bases,
    tenant_id         varchar(36)
        references tenants,
    chunk_index       integer,
    content           text        not null,
    metadata          json,
    search_vector     varchar(255),
    created_at        timestamp
);

create table llm_provider_keys
(
    id            varchar(36)                                     not null
        primary key,
    provider      varchar(50)                                     not null,
    encrypted_key text                                            not null,
    key_hash      varchar(64)                                     not null
        unique,
    status        varchar(20) default 'active'::character varying not null,
    remark        text,
    created_at    timestamp with time zone,
    updated_at    timestamp with time zone
);

create index ix_llm_provider_keys_provider
    on llm_provider_keys (provider);

create table ent_model_routes
(
    id               varchar(36)  not null
        primary key,
    tenant_id        varchar(36)  not null,
    model_id         varchar(128) not null,
    primary_provider varchar(64)  not null,
    fallback_order   jsonb                    default '[]'::jsonb,
    provider_config  jsonb                    default '{}'::jsonb,
    enabled          boolean                  default true,
    priority         integer                  default 1,
    created_at       timestamp with time zone default now(),
    updated_at       timestamp with time zone default now(),
    unique (tenant_id, model_id)
);

create table task_idempotency
(
    idempotency_key varchar(255)                                                  not null
        primary key,
    task_id         varchar(64),
    task_type       varchar(64),
    tenant_id       varchar(64),
    status          varchar(16)              default 'running'::character varying not null,
    attempt         integer                  default 1                            not null,
    result_ref      text,
    created_at      timestamp with time zone default now(),
    updated_at      timestamp with time zone default now()
);

create index ix_task_idempotency_status
    on task_idempotency (status);

create index ix_task_idempotency_status_updated
    on task_idempotency (status, updated_at);

create index ix_task_idempotency_task_id
    on task_idempotency (task_id);

create table user_memory_entries
(
    id               varchar(64)                                                  not null
        primary key,
    tenant_id        varchar(64)                                                  not null,
    user_id          varchar(64)                                                  not null,
    slot             varchar(64)                                                  not null,
    item_key         varchar(255)                                                 not null,
    item_value       jsonb,
    confidence       integer,
    source           varchar(32),
    embedding        jsonb,
    access_count     integer                  default 0                           not null,
    last_accessed_at timestamp with time zone,
    status           varchar(16)              default 'active'::character varying not null,
    created_at       timestamp with time zone default now()                       not null,
    updated_at       timestamp with time zone default now()                       not null,
    constraint user_memory_entries_identity_uniq
        unique (tenant_id, user_id, slot, item_key)
);

create index ix_user_memory_entries_lookup
    on user_memory_entries (tenant_id, user_id, status);

create table subagent_runs
(
    id              varchar(64)                                       not null
        primary key,
    root_session_id varchar(128)                                      not null,
    turn_id         varchar(64),
    parent_run_id   varchar(64),
    depth           integer     default 1                             not null,
    tenant_id       varchar(36),
    user_id         varchar(36),
    agent_id        varchar(36)
                                                                      references agents
                                                                          on delete set null,
    profile_name    varchar(128),
    task            text                                              not null,
    status          varchar(16)                                       not null,
    summary         text,
    summary_format  varchar(16) default 'markdown'::character varying not null,
    artifacts       jsonb       default '[]'::jsonb,
    write_paths     jsonb       default '[]'::jsonb,
    read_only       boolean     default false                         not null,
    input_tokens    bigint      default 0                             not null,
    output_tokens   bigint      default 0                             not null,
    steps           integer     default 0                             not null,
    cost_cents      integer     default 0                             not null,
    redacted_count  integer     default 0                             not null,
    error           text,
    started_at      timestamp,
    finished_at     timestamp,
    created_at      timestamp   default now()                         not null,
    rerun_of        varchar(64)
);

create index subagent_runs_tree_idx
    on subagent_runs (root_session_id, created_at);

create index subagent_runs_parent_idx
    on subagent_runs (parent_run_id);

create index subagent_runs_turn_idx
    on subagent_runs (turn_id);

create index subagent_runs_tenant_idx
    on subagent_runs (tenant_id, created_at);

create table subagent_run_steps
(
    id            bigserial
        primary key,
    run_id        varchar(64)             not null
        references subagent_runs
            on delete cascade,
    seq           integer                 not null,
    kind          varchar(16)             not null,
    role          varchar(16),
    tool_name     varchar(64),
    tool_call_id  varchar(128),
    content       text,
    truncated     boolean   default false not null,
    input_tokens  integer   default 0     not null,
    output_tokens integer   default 0     not null,
    created_at    timestamp default now() not null
);

create unique index subagent_run_steps_seq_idx
    on subagent_run_steps (run_id, seq);

create table alembic_version
(
    version_num varchar(32) not null
        constraint alembic_version_pkc
            primary key
);

create table session_map_workspaces
(
    id         varchar(64)                                not null
        primary key,
    tenant_id  varchar(36),
    user_id    varchar(36),
    name       varchar(255) default ''::character varying not null,
    viewport   jsonb        default '{}'::jsonb           not null,
    created_at timestamp    default now()                 not null,
    updated_at timestamp    default now()                 not null
);

create index session_map_workspaces_owner_idx
    on session_map_workspaces (tenant_id, user_id);

create table session_map_nodes
(
    id              varchar(64)                                     not null
        primary key,
    workspace_id    varchar(64)                                     not null
        references session_map_workspaces
            on delete cascade,
    session_id      varchar(36)
        references sessions
            on delete cascade,
    parent_node_id  varchar(64)
                                                                    references session_map_nodes
                                                                        on delete set null,
    edge_kind       varchar(16) default 'manual'::character varying not null,
    x               integer     default 0                           not null,
    y               integer     default 0                           not null,
    title           varchar(255),
    color           varchar(16),
    collapsed       boolean     default false                       not null,
    hidden          boolean     default false                       not null,
    pinned          boolean     default false                       not null,
    branch_from_seq integer,
    created_at      timestamp   default now()                       not null,
    updated_at      timestamp   default now()                       not null
);

create index session_map_nodes_workspace_idx
    on session_map_nodes (workspace_id);

create index session_map_nodes_session_idx
    on session_map_nodes (session_id);

