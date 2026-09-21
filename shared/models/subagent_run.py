"""
SQLAlchemy 模型定义 - SubagentRun
由代码生成器自动生成 (基于 models.yaml / routes.yaml) - 请勿手动修改
生成时间：2026-09-21 18:33:14
"""

from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime, ForeignKey, JSON
import uuid
from datetime import datetime

from . import Base  # 使用统一的 Base



class SubagentRun(Base):
    """子 Agent 运行（run 级元数据 + L1 精简摘要）模型"""
    __tablename__ = 'subagent_runs'




    id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()), doc='运行 ID（rs_ 前缀）')

    root_session_id = Column(String(128), nullable=True, doc='归属会话 ID（递归树根）')

    turn_id = Column(String(64), nullable=True, doc='归属回合 ID（计费归因）')

    parent_run_id = Column(String(64), nullable=True, doc='父运行 ID（顶层委派为空）')

    depth = Column(Integer, default=1, doc='递归深度（顶层为 1）')


    tenant_id = Column(String(36), nullable=True, doc='租户 ID')

    user_id = Column(String(36), nullable=True, doc='用户 ID')

    agent_id = Column(String(36), ForeignKey('agents.id'), nullable=True, doc='Profile 引用（agents.id）')


    profile_name = Column(String(128), nullable=True, doc='Profile 名（冗余，便于展示与审计）')

    task = Column(Text, nullable=False, doc='委派任务原文')


    status = Column(String(16), nullable=True, doc='状态（queued/running/completed/failed/cancelled/timeout/budget_exceeded）')

    summary = Column(Text, nullable=True, doc='L1 有效消息（经 LLM 整理，唯一进入父上下文的内容）')


    summary_format = Column(String(16), default='markdown', doc='摘要格式')

    artifacts = Column(JSON, default=[], doc='产物引用（[{path,bytes}]）')


    write_paths = Column(JSON, default=[], doc='写入路径声明（并发互斥用）')


    read_only = Column(Boolean, default=False, doc='是否只读运行（剥离写工具）')


    input_tokens = Column(BigInteger, default=0, doc='输入 Tokens')


    output_tokens = Column(BigInteger, default=0, doc='输出 Tokens')


    steps = Column(Integer, default=0, doc='步数')


    cost_cents = Column(Integer, default=0, doc='费用（分）')


    redacted_count = Column(Integer, default=0, doc='入库前命中的敏感片段数')


    error = Column(Text, nullable=True, doc='失败原因（不静默）')


    started_at = Column(DateTime, nullable=True, doc='开始时间')    

    finished_at = Column(DateTime, nullable=True, doc='结束时间')    

    created_at = Column(DateTime, default=datetime.utcnow, doc='创建时间')    


    def to_dict(self, exclude_sensitive=True):
        """转换为字典

        Args:
            exclude_sensitive: 是否排除敏感字段（密码、密钥、token 等）
        """
        data = {
            'id': self.id,
            'root_session_id': self.root_session_id,
            'turn_id': self.turn_id,
            'parent_run_id': self.parent_run_id,
            'depth': self.depth,
            'tenant_id': self.tenant_id,
            'user_id': self.user_id,
            'agent_id': self.agent_id,
            'profile_name': self.profile_name,
            'task': self.task,
            'status': self.status,
            'summary': self.summary,
            'summary_format': self.summary_format,
            'artifacts': self.artifacts,
            'write_paths': self.write_paths,
            'read_only': self.read_only,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'steps': self.steps,
            'cost_cents': self.cost_cents,
            'redacted_count': self.redacted_count,
            'error': self.error,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

        if not exclude_sensitive:
            sensitive_data = {
            }
            data.update(sensitive_data)

        return data

    def __repr__(self):
        """字符串表示"""
        return f'<SubagentRun id={self.id}>'


