"""
SQLAlchemy 模型定义 - UnifiedSession
由代码生成器自动生成 (基于 models.yaml / routes.yaml) - 请勿手动修改
生成时间：2026-10-09 07:14:28
"""

from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime, JSON
import uuid
from datetime import datetime

from . import Base  # 使用统一的 Base



class UnifiedSession(Base):
    """统一会话模型"""
    __tablename__ = 'unified_sessions'




    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), doc='会话 ID')

    tenant_id = Column(String(36), nullable=True, doc='租户 ID')

    user_id = Column(String(36), nullable=True, doc='用户 ID')

    title = Column(String(255), default='', doc='标题')

    mode = Column(String(16), default='auto', doc='模式')

    shared_context = Column(JSON, default={}, doc='共享上下文（JSONB）')


    created_at = Column(DateTime, default=datetime.utcnow, doc='创建时间')    

    updated_at = Column(DateTime, default=datetime.utcnow, doc='更新时间')    

    runtime = Column(JSON, default={}, doc='运行期状态（jsonb NOT NULL DEFAULT 空对象）')



    def to_dict(self, exclude_sensitive=True):
        """转换为字典

        Args:
            exclude_sensitive: 是否排除敏感字段（密码、密钥、token 等）
        """
        data = {
            'id': self.id,
            'tenant_id': self.tenant_id,
            'user_id': self.user_id,
            'title': self.title,
            'mode': self.mode,
            'shared_context': self.shared_context,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'runtime': self.runtime,
        }

        if not exclude_sensitive:
            sensitive_data = {
            }
            data.update(sensitive_data)

        return data

    def __repr__(self):
        """字符串表示"""
        return f'<UnifiedSession id={self.id}>'


