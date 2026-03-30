from fastapi import APIRouter, HTTPException, Query

from src.model.response import (
    ConversationInfoResponse,
    ConversationMessagesResponse,
    DeleteResponse,
)
from src.service import (
    delete_conversation,
    get_conversation_info,
    get_conversation_messages,
)


router = APIRouter()


@router.post("/delete", response_model=DeleteResponse)
async def api_delete(conversation_id: str = Query()):
    try:
        ok, msg = await delete_conversation(conversation_id)
        return DeleteResponse(ok=ok, msg=msg)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/conversation/info", response_model=ConversationInfoResponse)
async def api_conversation_info(conversation_id: str = Query()):
    try:
        info = await get_conversation_info(conversation_id)
        return ConversationInfoResponse(
            conversation_id=conversation_id,
            name=info.get("name") or "继续对话",
            section_id=info.get("last_section_id"),
            latest_index=info.get("latest_index"),
            badge_count=info.get("badge_count"),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/conversation/messages", response_model=ConversationMessagesResponse)
async def api_conversation_messages(
    conversation_id: str = Query(),
    anchor_index: int | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    try:
        data = await get_conversation_messages(
            conversation_id=conversation_id,
            anchor_index=anchor_index,
            limit=limit,
        )
        return ConversationMessagesResponse(
            conversation_id=conversation_id,
            section_id=data.get("section_id"),
            messages=data.get("messages", []),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
