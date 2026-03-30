from fastapi import APIRouter

from .endpoints import completions, conversations, runtime


router = APIRouter()

router.include_router(runtime.router, prefix="/chat", tags=["runtime"])
router.include_router(completions.router, prefix="/chat", tags=["completions"])
router.include_router(conversations.router, prefix="/chat", tags=["conversations"])
