"""
SQLAlchemy 模型定义 - ConversationShare
由代码生成器自动生成 (基于 models.yaml / routes.yaml) - 请勿手动修改
生成时间：2026-09-15 09:55:16
"""

from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime
import uuid
from datetime import datetime

from . import Base  # 使用统一的 Base



class ConversationShare(Base):
    """对话分享模型"""
    __tablename__ = 'conversation_shares'




    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), doc='分享 ID')

    session_id = Column(String(128), nullable=True, doc='会话 ID')

    user_id = Column(String(36), nullable=True, doc='用户 ID')

    title = Column(String(255), default='', doc='标题')

    message_ids = Column(String(255), default='[]', doc='消息 ID 列表')

    created_at = Column(DateTime, default=datetime.utcnow, doc='创建时间')    

    revoked_at = Column(DateTime, nullable=True, doc='撤销时间')    


    def to_dict(self, exclude_sensitive=True):
        """转换为字典

        Args:
            exclude_sensitive: 是否排除敏感字段（密码、密钥、token 等）
        """
        data = {
            'id': self.id,
            'session_id': self.session_id,
            'user_id': self.user_id,
            'title': self.title,
            'message_ids': self.message_ids,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'revoked_at': self.revoked_at.isoformat() if self.revoked_at else None,
        }

        if not exclude_sensitive:
            sensitive_data = {
            }
            data.update(sensitive_data)

        return data

    def __repr__(self):
        """字符串表示"""
        return f'<ConversationShare id={self.id}>'


