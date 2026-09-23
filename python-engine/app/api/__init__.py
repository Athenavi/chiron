"""Python 工具服务 API（Phase 1）。"""

from fastapi import APIRouter

from app.api.agents import router as agents_router
from app.api.capabilities import router as capabilities_router
from app.api.context import router as context_router
from app.api.knowledge import router as knowledge_router
from app.api.memory import router as memory_router
from app.api.plugins import router as plugins_router
from app.api.skills import router as skills_router
from app.api.system import router as system_router
from app.api.tools import router as tools_router
from app.api.unified_executor import router as chat_router
from app.api.workflows import router as workflows_router

api_router = APIRouter()
api_router.include_router(tools_router)
api_router.include_router(workflows_router)
api_router.include_router(agents_router)
api_router.include_router(skills_router)
api_router.include_router(knowledge_router)
api_router.include_router(plugins_router)
api_router.include_router(chat_router)
api_router.include_router(capabilities_router)
api_router.include_router(context_router)
api_router.include_router(memory_router)
api_router.include_router(system_router)
