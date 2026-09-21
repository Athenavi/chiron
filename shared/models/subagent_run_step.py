"""
SQLAlchemy 模型定义 - SubagentRunStep
由代码生成器自动生成 (基于 models.yaml / routes.yaml) - 请勿手动修改
生成时间：2026-09-21 18:33:14
"""

from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime, ForeignKey
from datetime import datetime

from . import Base  # 使用统一的 Base



class SubagentRunStep(Base):
    """子 Agent 完整调用过程（L0 逐 step）模型"""
    __tablename__ = 'subagent_run_steps'




    id = Column(Integer, primary_key=True, autoincrement=True, doc='自增 ID')

    run_id = Column(String(64), ForeignKey('subagent_runs.id'), doc='所属运行 ID')


    seq = Column(Integer, doc='步序（run 内唯一）')


    kind = Column(String(16), nullable=True, doc='步骤类型（message/reasoning/tool_call/tool_result/notice/error）')

    role = Column(String(16), nullable=True, doc='消息角色')

    tool_name = Column(String(64), nullable=True, doc='工具名')

    tool_call_id = Column(String(128), nullable=True, doc='工具调用 ID')

    content = Column(Text, nullable=True, doc='单步内容（截断上限 32KB）')


    truncated = Column(Boolean, default=False, doc='内容是否被截断')


    input_tokens = Column(Integer, default=0, doc='输入 Tokens')


    output_tokens = Column(Integer, default=0, doc='输出 Tokens')


    created_at = Column(DateTime, default=datetime.utcnow, doc='创建时间')    


    def to_dict(self, exclude_sensitive=True):
        """转换为字典

        Args:
            exclude_sensitive: 是否排除敏感字段（密码、密钥、token 等）
        """
        data = {
            'id': self.id,
            'run_id': self.run_id,
            'seq': self.seq,
            'kind': self.kind,
            'role': self.role,
            'tool_name': self.tool_name,
            'tool_call_id': self.tool_call_id,
            'content': self.content,
            'truncated': self.truncated,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

        if not exclude_sensitive:
            sensitive_data = {
            }
            data.update(sensitive_data)

        return data

    def __repr__(self):
        """字符串表示"""
        return f'<SubagentRunStep id={self.id}>'


