import asyncio
import json
import os

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse

from src.model.conversation_mode import ConversationRequestMode
from src.model.request import CompletionRequest
from src.model.response import CompletionResponse, SessionParamsResponse
from src.service import chat_completion


router = APIRouter()


def _encode_sse_event(event: str, data: dict | list | str | None = None) -> str:
    payload = "" if data is None else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _get_stream_chunk_size() -> int:
    raw_value = str(os.getenv("DOUBAO_SSE_STREAM_CHUNK_SIZE", "6")).strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        return 6


def _get_stream_chunk_delay() -> float:
    raw_value = str(os.getenv("DOUBAO_SSE_STREAM_CHUNK_DELAY_MS", "60")).strip()
    try:
        return max(0.0, float(raw_value) / 1000.0)
    except ValueError:
        return 0.06


def _chunk_text_for_stream(text: str, chunk_size: int | None = None) -> list[str]:
    normalized = str(text or "")
    if not normalized:
        return []
    safe_chunk_size = max(1, int(chunk_size or _get_stream_chunk_size()))
    return [
        normalized[index:index + safe_chunk_size]
        for index in range(0, len(normalized), safe_chunk_size)
    ]


def _serialize_completion_response(response: CompletionResponse) -> dict:
    return json.loads(response.model_dump_json())


def _build_stream_error_payload(exc: Exception) -> dict:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            message = str(detail.get("message") or detail.get("detail") or detail)
        else:
            message = str(detail)
        return {
            "status": int(exc.status_code or 500),
            "detail": detail,
            "message": message,
        }
    return {
        "status": 500,
        "detail": str(exc),
        "message": str(exc),
    }


async def _build_completion_response(
    completion: CompletionRequest,
    *,
    force_conversation_mode: ConversationRequestMode | None = None,
) -> CompletionResponse:
    result = await chat_completion(
        prompt=completion.prompt,
        guest=completion.guest,
        session_mode=completion.session_mode,
        conversation_mode=force_conversation_mode or completion.conversation_mode,
        conversation_id=completion.conversation_id,
        section_id=completion.section_id,
        attachments=completion.attachments,
        think_mode=completion.think_mode,
        use_auto_cot=completion.use_auto_cot,
        use_deep_think=completion.use_deep_think,
        session_override=completion.session_params.model_dump() if completion.session_params else None,
    )
    if not isinstance(result, tuple):
        raise TypeError(f"Unexpected chat_completion result type: {type(result).__name__}")
    if len(result) == 7:
        text, imgs, conv_id, msg_id, sec_id, session_params, completion_meta = result
    elif len(result) == 6:
        text, imgs, conv_id, msg_id, sec_id, session_params = result
        completion_meta = {}
    else:
        raise ValueError(f"Unexpected chat_completion result length: {len(result)}")
    return CompletionResponse(
        text=text,
        img_urls=imgs,
        conversation_id=conv_id,
        message_id=msg_id,
        messageg_id=msg_id,
        section_id=sec_id,
        session_params=SessionParamsResponse(**session_params) if session_params else None,
        request_mode=completion_meta.get("request_mode") if isinstance(completion_meta, dict) else None,
        follow_up_required=bool(completion_meta.get("follow_up_required")) if isinstance(completion_meta, dict) else False,
        follow_up_conversation_mode=(
            completion_meta.get("follow_up_conversation_mode")
            if isinstance(completion_meta, dict)
            else None
        ),
    )


async def _build_completion_stream(
    completion: CompletionRequest,
    *,
    force_conversation_mode: ConversationRequestMode | None = None,
):
    async def event_generator():
        task = asyncio.create_task(
            _build_completion_response(
                completion,
                force_conversation_mode=force_conversation_mode,
            )
        )
        try:
            yield _encode_sse_event(
                "status",
                {
                    "phase": "preparing",
                    "message": "正在准备流式响应...",
                },
            )

            status_index = 0
            waiting_messages = [
                "正在等待上游返回...",
                "仍在等待上游返回，请保持当前连接...",
            ]
            while not task.done():
                yield _encode_sse_event(
                    "status",
                    {
                        "phase": "waiting",
                        "message": waiting_messages[min(status_index, len(waiting_messages) - 1)],
                    },
                )
                status_index += 1
                await asyncio.sleep(0.8)

            response = await task
            serialized = _serialize_completion_response(response)
            yield _encode_sse_event(
                "meta",
                {
                    "request_mode": serialized.get("request_mode"),
                    "conversation_id": serialized.get("conversation_id"),
                    "section_id": serialized.get("section_id"),
                    "message_id": serialized.get("message_id"),
                    "follow_up_required": bool(serialized.get("follow_up_required")),
                    "follow_up_conversation_mode": serialized.get("follow_up_conversation_mode"),
                },
            )

            full_text = str(serialized.get("text") or "")
            if full_text:
                streamed_text = ""
                chunk_delay = _get_stream_chunk_delay()
                for piece in _chunk_text_for_stream(full_text):
                    streamed_text += piece
                    yield _encode_sse_event(
                        "delta",
                        {
                            "delta": piece,
                            "text": streamed_text,
                        },
                    )
                    if chunk_delay > 0:
                        await asyncio.sleep(chunk_delay)

            image_urls = serialized.get("img_urls") or []
            if image_urls:
                yield _encode_sse_event(
                    "images",
                    {
                        "img_urls": image_urls,
                    },
                )

            yield _encode_sse_event("done", serialized)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield _encode_sse_event("error", _build_stream_error_payload(exc))
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except Exception:
                    pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/completions", response_model=CompletionResponse)
async def api_completions(completion: CompletionRequest = Body()):
    try:
        return await _build_completion_response(completion)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/completions/stream")
async def api_completions_stream(completion: CompletionRequest = Body()):
    return await _build_completion_stream(completion)


@router.post("/completions/new", response_model=CompletionResponse)
async def api_completions_new(completion: CompletionRequest = Body()):
    try:
        return await _build_completion_response(completion, force_conversation_mode="new")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/completions/new/stream")
async def api_completions_new_stream(completion: CompletionRequest = Body()):
    return await _build_completion_stream(completion, force_conversation_mode="new")


@router.post("/completions/continue", response_model=CompletionResponse)
async def api_completions_continue(completion: CompletionRequest = Body()):
    try:
        return await _build_completion_response(completion, force_conversation_mode="continue")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/completions/continue/stream")
async def api_completions_continue_stream(completion: CompletionRequest = Body()):
    return await _build_completion_stream(completion, force_conversation_mode="continue")
