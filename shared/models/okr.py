"""
SQLAlchemy 模型定义 - Okr
由代码生成器自动生成 (基于 models.yaml / routes.yaml) - 请勿手动修改
生成时间：2026-09-15 09:55:16
"""

from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime, ForeignKey, JSON
import uuid
from datetime import datetime

from . import Base  # 使用统一的 Base



class Okr(Base):
    """OKR 目标模型"""
    __tablename__ = 'okrs'




    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), doc='OKR ID')

    tenant_id = Column(String(36), ForeignKey('tenants.id'), doc='租户 ID')


    user_id = Column(String(36), default='', doc='用户 ID')

    objective = Column(String(255), nullable=True, doc='目标')

    key_results = Column(JSON, default=[], doc='关键结果（JSONB）')


    quarter = Column(String(16), default='', doc='季度')

    status = Column(String(16), default='active', doc='状态')

    created_at = Column(DateTime, default=datetime.utcnow, doc='创建时间')    

    updated_at = Column(DateTime, default=datetime.utcnow, doc='更新时间')    


    def to_dict(self, exclude_sensitive=True):
        """转换为字典

        Args:
            exclude_sensitive: 是否排除敏感字段（密码、密钥、token 等）
        """
        data = {
            'id': self.id,
            'tenant_id': self.tenant_id,
            'user_id': self.user_id,
            'objective': self.objective,
            'key_results': self.key_results,
            'quarter': self.quarter,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

        if not exclude_sensitive:
            sensitive_data = {
            }
            data.update(sensitive_data)

        return data

    def __repr__(self):
        """字符串表示"""
        return f'<Okr id={self.id}>'


