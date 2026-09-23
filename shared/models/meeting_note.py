"""
SQLAlchemy 模型定义 - MeetingNote
由代码生成器自动生成 (基于 models.yaml / routes.yaml) - 请勿手动修改
生成时间：2026-09-15 09:55:16
"""

from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime, ForeignKey
import uuid
from datetime import datetime

from . import Base  # 使用统一的 Base



class MeetingNote(Base):
    """会议记录模型"""
    __tablename__ = 'meeting_notes'




    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), doc='记录 ID')

    tenant_id = Column(String(36), ForeignKey('tenants.id'), doc='租户 ID')


    user_id = Column(String(36), default='', doc='用户 ID')

    title = Column(String(255), nullable=True, doc='标题')

    notes = Column(Text, nullable=False, doc='笔记内容')


    summary = Column(Text, nullable=True, doc='摘要')


    participants = Column(String(255), default='[]', doc='参与者')

    date = Column(String(255), default='CURRENT_DATE', doc='日期')

    created_at = Column(DateTime, default=datetime.utcnow, doc='创建时间')    


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
            'notes': self.notes,
            'summary': self.summary,
            'participants': self.participants,
            'date': self.date,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

        if not exclude_sensitive:
            sensitive_data = {
            }
            data.update(sensitive_data)

        return data

    def __repr__(self):
        """字符串表示"""
        return f'<MeetingNote id={self.id}>'


