"""
SQLAlchemy 模型定义 - Session
由代码生成器自动生成 (基于 models.yaml / routes.yaml) - 请勿手动修改
生成时间：2026-10-09 07:12:02
"""

from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime, ForeignKey
import uuid
from datetime import datetime

from . import Base  # 使用统一的 Base



class Session(Base):
    """会话模型"""
    __tablename__ = 'sessions'




    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), doc='会话 ID')

    tenant_id = Column(String(36), ForeignKey('tenants.id'), doc='租户 ID')


    user_id = Column(String(36), ForeignKey('users.id'), nullable=True, doc='用户 ID')


    agent_id = Column(String(36), ForeignKey('agents.id'), nullable=True, doc='Agent ID')


    title = Column(String(255), default='', doc='标题')

    status = Column(String(16), default='active', doc='状态')

    created_at = Column(DateTime, default=datetime.utcnow, doc='创建时间')    

    updated_at = Column(DateTime, default=datetime.utcnow, doc='更新时间')    

    pinned = Column(Boolean, default=False, doc='是否置顶')


    tag = Column(String(64), nullable=True, doc='会话标签（前端分类筛选用，持久化到 DB）')

    parent_session_id = Column(String(36), nullable=True, doc='源会话（血缘）—— 分支特性')

    alias = Column(String(64), nullable=True, doc='会话别名')

    branch_from_seq = Column(Integer, doc='分叉点，保留到源会话的第几条消息（含该条，1 基）')


    branch_mode = Column(String(16), nullable=True, doc='分支模式 truncate / condense')

    branch_state = Column(String(16), nullable=True, doc='分支状态 NULL(truncate) / pending / ready / failed')

    branch_keep_tail = Column(Integer, doc='condense 下保留的原文条数（truncate 写 NULL）')



    def to_dict(self, exclude_sensitive=True):
        """转换为字典

        Args:
            exclude_sensitive: 是否排除敏感字段（密码、密钥、token 等）
        """
        data = {
            'id': self.id,
            'tenant_id': self.tenant_id,
            'user_id': self.user_id,
            'agent_id': self.agent_id,
            'title': self.title,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'pinned': self.pinned,
            'tag': self.tag,
            'parent_session_id': self.parent_session_id,
            'alias': self.alias,
            'branch_from_seq': self.branch_from_seq,
            'branch_mode': self.branch_mode,
            'branch_state': self.branch_state,
            'branch_keep_tail': self.branch_keep_tail,
        }

        if not exclude_sensitive:
            sensitive_data = {
            }
            data.update(sensitive_data)

        return data

    def __repr__(self):
        """字符串表示"""
        return f'<Session id={self.id}>'


