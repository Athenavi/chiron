"""
SQLAlchemy 模型定义 - Payment
由代码生成器自动生成 (基于 models.yaml / routes.yaml) - 请勿手动修改
生成时间：2026-09-15 09:55:16
"""

from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime
import uuid
from datetime import datetime

from . import Base  # 使用统一的 Base



class Payment(Base):
    """支付记录模型"""
    __tablename__ = 'payments'




    id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()), doc='支付 ID')

    user_id = Column(String(36), nullable=True, doc='用户 ID')

    channel = Column(String(16), nullable=True, doc='支付渠道')

    credits = Column(Integer, doc='Credits 数量')


    amount_cents = Column(BigInteger, default=0, doc='金额（分）')


    currency = Column(String(8), default='CNY', doc='货币')

    status = Column(String(16), default='pending', doc='状态')

    qr_code = Column(Text, nullable=True, doc='二维码')


    provider_order_id = Column(String(64), default='', doc='提供商订单 ID')

    trade_no = Column(String(64), default='', doc='交易号')

    created_at = Column(DateTime, default=datetime.utcnow, doc='创建时间')    

    paid_at = Column(DateTime, nullable=True, doc='支付时间')    

    expired_at = Column(DateTime, nullable=True, doc='过期时间')    


    def to_dict(self, exclude_sensitive=True):
        """转换为字典

        Args:
            exclude_sensitive: 是否排除敏感字段（密码、密钥、token 等）
        """
        data = {
            'id': self.id,
            'user_id': self.user_id,
            'channel': self.channel,
            'credits': self.credits,
            'amount_cents': self.amount_cents,
            'currency': self.currency,
            'status': self.status,
            'qr_code': self.qr_code,
            'provider_order_id': self.provider_order_id,
            'trade_no': self.trade_no,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'paid_at': self.paid_at.isoformat() if self.paid_at else None,
            'expired_at': self.expired_at.isoformat() if self.expired_at else None,
        }

        if not exclude_sensitive:
            sensitive_data = {
            }
            data.update(sensitive_data)

        return data

    def __repr__(self):
        """字符串表示"""
        return f'<Payment id={self.id}>'


