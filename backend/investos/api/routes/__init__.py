from fastapi import APIRouter

from .activity import router as activity_router
from .agent import router as agent_router
from .automation import router as automation_router
from .benchmark import router as benchmark_router
from .chat import router as chat_router
from .dashboard import router as dashboard_router
from .decision import router as decision_router
from .discoveries import router as discoveries_router
from .graph import router as graph_router
from .historical import router as historical_router
from .ingestion import router as ingestion_router
from .integrations import router as integrations_router
from .integrity import router as integrity_router
from .lesson import router as lesson_router
from .opportunity import router as opportunity_router
from .portfolio import router as portfolio_router
from .profile import router as profile_router
from .pruning import router as pruning_router
from .reasoning import router as reasoning_router
from .review import router as review_router
from .risk import router as risk_router
from .setup import router as setup_router
from .shadow import router as shadow_router
from .source import router as source_router
from .timeline import router as timeline_router
from .verification import router as verification_router
from .watcher import router as watcher_router

api_router = APIRouter()
api_router.include_router(portfolio_router)
api_router.include_router(ingestion_router)
api_router.include_router(timeline_router)
api_router.include_router(chat_router)
api_router.include_router(dashboard_router)
api_router.include_router(profile_router)
api_router.include_router(integrations_router)
api_router.include_router(shadow_router)
api_router.include_router(setup_router)
api_router.include_router(source_router)
api_router.include_router(decision_router)
api_router.include_router(verification_router)
api_router.include_router(automation_router)
api_router.include_router(benchmark_router)
api_router.include_router(risk_router)
api_router.include_router(agent_router)
api_router.include_router(reasoning_router)
api_router.include_router(review_router)
api_router.include_router(lesson_router)
api_router.include_router(opportunity_router)
api_router.include_router(graph_router)
api_router.include_router(pruning_router, prefix="/pruning", tags=["pruning"])
api_router.include_router(discoveries_router)
api_router.include_router(activity_router)
api_router.include_router(integrity_router)
api_router.include_router(watcher_router)
api_router.include_router(historical_router)
