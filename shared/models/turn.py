"""
SQLAlchemy 模型定义 - Turn
由代码生成器自动生成 (基于 models.yaml / routes.yaml) - 请勿手动修改
生成时间：2026-10-09 07:12:02
"""

from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime, ForeignKey
import uuid
from datetime import datetime

from . import Base  # 使用统一的 Base



class Turn(Base):
    """对话回合（turn）状态机模型"""
    __tablename__ = 'turns'




    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), doc='回合 ID')

    session_id = Column(String(36), ForeignKey('sessions.id'), doc='会话 ID')


    user_id = Column(String(36), nullable=True, doc='用户 ID')

    status = Column(String(16), default='created', doc='状态（created/running/completed/failed/cancelled）')

    error = Column(Text, nullable=True, doc='失败原因（不静默）')


    input_tokens = Column(BigInteger, default=0, doc='输入 Tokens')


    output_tokens = Column(BigInteger, default=0, doc='输出 Tokens')


    started_at = Column(DateTime, default=datetime.utcnow, doc='开始时间')    

    finished_at = Column(DateTime, nullable=True, doc='结束时间')    

    created_at = Column(DateTime, default=datetime.utcnow, doc='创建时间')    

    model = Column(String(128), nullable=True, doc='该回合使用的模型')

    cache_hit = Column(Boolean, default=False, doc='是否命中提示缓存（NOT NULL DEFAULT false）')


    cached_tokens = Column(BigInteger, default=0, doc='命中的缓存 token 数（bigint NOT NULL DEFAULT 0）')



    def to_dict(self, exclude_sensitive=True):
        """转换为字典

        Args:
            exclude_sensitive: 是否排除敏感字段（密码、密钥、token 等）
        """
        data = {
            'id': self.id,
            'session_id': self.session_id,
            'user_id': self.user_id,
            'status': self.status,
            'error': self.error,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'model': self.model,
            'cache_hit': self.cache_hit,
            'cached_tokens': self.cached_tokens,
        }

        if not exclude_sensitive:
            sensitive_data = {
            }
            data.update(sensitive_data)

        return data

    def __repr__(self):
        """字符串表示"""
        return f'<Turn id={self.id}>'


