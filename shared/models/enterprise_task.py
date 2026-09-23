"""
SQLAlchemy 模型定义 - EnterpriseTask
由代码生成器自动生成 (基于 models.yaml / routes.yaml) - 请勿手动修改
生成时间：2026-09-15 09:55:16
"""

from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime, ForeignKey
import uuid
from datetime import datetime

from . import Base  # 使用统一的 Base



class EnterpriseTask(Base):
    """企业任务模型"""
    __tablename__ = 'enterprise_tasks'




    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), doc='任务 ID')

    tenant_id = Column(String(36), ForeignKey('tenants.id'), doc='租户 ID')


    user_id = Column(String(36), default='', doc='用户 ID')

    title = Column(String(255), nullable=True, doc='标题')

    description = Column(Text, nullable=False, doc='描述')


    project = Column(String(128), default='', doc='项目')

    assignee = Column(String(128), default='', doc='负责人')

    priority = Column(String(16), default='medium', doc='优先级')

    status = Column(String(16), default='open', doc='状态')

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
            'title': self.title,
            'description': self.description,
            'project': self.project,
            'assignee': self.assignee,
            'priority': self.priority,
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
        return f'<EnterpriseTask id={self.id}>'


