from fastapi import APIRouter, Body, HTTPException, Query

from src.model.conversation_mode import ConversationRequestMode
from src.model.request import (
    GuestSessionResetRequest,
    GuestSessionRestoreRequest,
    ManualSendRequest,
    ManualVerifyFinishRequest,
    ManualVerifyStartRequest,
    RequestPreviewRequest,
    RuntimeBootstrapRequest,
    SessionRefreshRequest,
)
from src.model.response import (
    BrowserRuntimeStateResponse,
    GuestSessionResetResponse,
    GuestSessionRestoreResponse,
    GuestSessionSnapshotsResponse,
    GuestSessionSnapshotResponse,
    ManualSendResponse,
    ManualVerifyFinishResponse,
    ManualVerifyStartResponse,
    RequestPreviewResponse,
    RuntimeBootstrapResponse,
    RuntimeStatusResponse,
    SessionParamsResponse,
    SessionRefreshResponse,
)
from src.model.session_mode import SessionMode, resolve_effective_guest_flag
from src.service import (
    bootstrap_runtime_capture,
    finish_manual_verification,
    get_guest_conversation_limit,
    get_browser_capture,
    get_runtime_status,
    list_guest_session_snapshots,
    preview_chat_completion_request,
    refresh_session_runtime,
    reset_guest_session,
    restore_guest_session_snapshot,
    send_manual_browser_message,
    start_manual_verification,
)


router = APIRouter()


async def _build_request_preview_response(
    payload: RequestPreviewRequest,
    *,
    force_conversation_mode: ConversationRequestMode | None = None,
) -> RequestPreviewResponse:
    data = await preview_chat_completion_request(
        prompt=payload.prompt,
        guest=payload.guest,
        session_mode=payload.session_mode,
        conversation_mode=force_conversation_mode or payload.conversation_mode,
        conversation_id=payload.conversation_id,
        section_id=payload.section_id,
        think_mode=payload.think_mode,
        use_auto_cot=payload.use_auto_cot,
        use_deep_think=payload.use_deep_think,
        include_signed_query=payload.include_signed_query,
        session_override=payload.session_params.model_dump() if payload.session_params else None,
    )
    return RequestPreviewResponse(
        ok=True,
        browser=BrowserRuntimeStateResponse(**data["browser"]),
        session_params=SessionParamsResponse(**data["session_params"]),
        request=data["request"],
        checks=data["checks"],
    )


@router.get("/runtime/status", response_model=RuntimeStatusResponse)
async def api_runtime_status(
    guest: bool = Query(default=False),
    session_mode: SessionMode | None = Query(default=None),
    conversation_id: str | None = Query(default=None),
):
    try:
        data = await get_runtime_status(
            guest=guest,
            conversation_id=conversation_id,
            session_mode=session_mode,
        )
        return RuntimeStatusResponse(
            browser=BrowserRuntimeStateResponse(**data["browser"]),
            curl_impersonate=data["curl_impersonate"],
            curl_cffi_available=data["curl_cffi_available"],
            preheat_all_sessions=data["preheat_all_sessions"],
            session_found=data["session_found"],
            session_params=SessionParamsResponse(**data["session_params"]) if data["session_params"] else None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/runtime/bootstrap", response_model=RuntimeBootstrapResponse)
async def api_runtime_bootstrap(payload: RuntimeBootstrapRequest = Body()):
    try:
        data = await bootstrap_runtime_capture(
            guest=payload.guest,
            session_mode=payload.session_mode,
            conversation_mode=payload.conversation_mode,
            conversation_id=payload.conversation_id,
            section_id=payload.section_id,
            session_override=payload.session_params.model_dump() if payload.session_params else None,
            seed_existing_cookies=payload.seed_existing_cookies,
            reuse_existing_window=payload.reuse_existing_window,
            wait_for_ready=payload.wait_for_ready,
            wait_timeout_seconds=payload.wait_timeout_seconds,
            poll_interval_ms=payload.poll_interval_ms,
        )
        return RuntimeBootstrapResponse(
            ok=True,
            ready=data["ready"],
            capture_ready=data["capture_ready"],
            manual_ready=data["manual_ready"],
            manual_window_opened=data["manual_window_opened"],
            reused_window=data["reused_window"],
            awaiting_user_action=data["awaiting_user_action"],
            message=data["message"],
            open_url=data.get("open_url"),
            capture_source=data.get("capture_source"),
            browser=BrowserRuntimeStateResponse(**data["browser"]),
            session_params=SessionParamsResponse(**data["session_params"]),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/runtime/guest/reset", response_model=GuestSessionResetResponse)
async def api_guest_session_reset(payload: GuestSessionResetRequest = Body()):
    try:
        effective_guest = resolve_effective_guest_flag(payload.guest, payload.session_mode)
        if not effective_guest:
            raise HTTPException(status_code=400, detail="Guest session reset is only available in guest mode.")

        data = await reset_guest_session(
            current_session=None,
            reason="manual_reset",
            open_verification_window=payload.open_verification_window,
            wait_for_ready=payload.wait_for_ready,
            wait_timeout_seconds=payload.wait_timeout_seconds,
            poll_interval_ms=payload.poll_interval_ms,
        )
        return GuestSessionResetResponse(
            ok=True,
            rotated=data["rotated"],
            backup_path=data.get("backup_path"),
            backup_conversation_ids=data.get("backup_conversation_ids") or [],
            previous_conversation_count=int(data.get("previous_conversation_count") or 0),
            guest_conversation_limit=int(data.get("guest_conversation_limit") or get_guest_conversation_limit()),
            verification_window_opened=bool(data.get("verification_window_opened")),
            ready=bool(data.get("ready")),
            manual_ready=bool(data.get("manual_ready")),
            capture_ready=bool(data.get("capture_ready")),
            message=data["message"],
            open_url=data.get("open_url"),
            browser=BrowserRuntimeStateResponse(**data["browser"]),
            session_params=SessionParamsResponse(**data["session_params"]),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/runtime/guest/snapshots", response_model=GuestSessionSnapshotsResponse)
async def api_guest_session_snapshots(limit_backups: int = Query(default=20, ge=1, le=100)):
    try:
        data = await list_guest_session_snapshots(limit_backups=limit_backups)
        return GuestSessionSnapshotsResponse(
            ok=True,
            active_snapshot_id=data.get("active_snapshot_id"),
            snapshots=[
                GuestSessionSnapshotResponse(
                    snapshot_id=snapshot["snapshot_id"],
                    source=snapshot["source"],
                    active=bool(snapshot.get("active")),
                    label=snapshot["label"],
                    room_id=snapshot.get("room_id"),
                    conversation_count=int(snapshot.get("conversation_count") or 0),
                    conversation_ids=list(snapshot.get("conversation_ids") or []),
                    backup_path=snapshot.get("backup_path"),
                    backup_at_ms=snapshot.get("backup_at_ms"),
                    reason=snapshot.get("reason"),
                    metadata=snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else None,
                    session_params=(
                        SessionParamsResponse(**snapshot["session"])
                        if isinstance(snapshot.get("session"), dict)
                        else None
                    ),
                )
                for snapshot in list(data.get("snapshots") or [])
            ],
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/runtime/guest/restore", response_model=GuestSessionRestoreResponse)
async def api_guest_session_restore(payload: GuestSessionRestoreRequest = Body()):
    try:
        effective_guest = resolve_effective_guest_flag(payload.guest, payload.session_mode)
        if not effective_guest:
            raise HTTPException(status_code=400, detail="Guest session restore is only available in guest mode.")

        data = await restore_guest_session_snapshot(
            snapshot_id=payload.snapshot_id,
            open_verification_window=payload.open_verification_window,
            wait_for_ready=payload.wait_for_ready,
            wait_timeout_seconds=payload.wait_timeout_seconds,
            poll_interval_ms=payload.poll_interval_ms,
        )
        return GuestSessionRestoreResponse(
            ok=True,
            restored=bool(data.get("restored")),
            snapshot_id=str(data.get("snapshot_id") or payload.snapshot_id),
            source=data.get("source"),
            conversation_ids=list(data.get("conversation_ids") or []),
            conversation_count=int(data.get("conversation_count") or 0),
            displaced_backup_path=data.get("displaced_backup_path"),
            displaced_conversation_ids=list(data.get("displaced_conversation_ids") or []),
            restored_conversation_id=data.get("restored_conversation_id"),
            restored_conversation_name=data.get("restored_conversation_name"),
            restored_section_id=data.get("restored_section_id"),
            history_ready=bool(data.get("history_ready")),
            history_error=data.get("history_error"),
            history_messages=list(data.get("history_messages") or []),
            verification_window_opened=bool(data.get("verification_window_opened")),
            ready=bool(data.get("ready")),
            manual_ready=bool(data.get("manual_ready")),
            capture_ready=bool(data.get("capture_ready")),
            message=str(data.get("message") or "游客缓存已恢复。"),
            open_url=data.get("open_url"),
            browser=BrowserRuntimeStateResponse(**data["browser"]),
            session_params=SessionParamsResponse(**data["session_params"]),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/runtime/browser-capture")
async def api_runtime_browser_capture(
    guest: bool = Query(default=False),
    session_mode: SessionMode | None = Query(default=None),
    conversation_id: str | None = Query(default=None),
):
    try:
        return await get_browser_capture(
            guest=guest,
            conversation_id=conversation_id,
            session_mode=session_mode,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/session/refresh", response_model=SessionRefreshResponse)
async def api_session_refresh(payload: SessionRefreshRequest = Body()):
    try:
        session_params = await refresh_session_runtime(
            guest=payload.guest,
            conversation_id=payload.conversation_id,
            session_override=payload.session_params.model_dump() if payload.session_params else None,
            persist_session=payload.persist_session,
            session_mode=payload.session_mode,
        )
        return SessionRefreshResponse(
            ok=True,
            session_params=SessionParamsResponse(**session_params),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/runtime/request-preview", response_model=RequestPreviewResponse)
async def api_request_preview(payload: RequestPreviewRequest = Body()):
    try:
        return await _build_request_preview_response(payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/runtime/request-preview", response_model=RequestPreviewResponse)
async def api_request_preview_get(
    prompt: str = Query(default="你好"),
    guest: bool = Query(default=False),
    session_mode: SessionMode | None = Query(default=None),
    conversation_mode: ConversationRequestMode | None = Query(default=None),
    conversation_id: str | None = Query(default=None),
    section_id: str | None = Query(default=None),
    think_mode: int | None = Query(default=None),
    use_auto_cot: bool = Query(default=False),
    use_deep_think: bool = Query(default=False),
    include_signed_query: bool = Query(default=True),
):
    try:
        return await _build_request_preview_response(
            RequestPreviewRequest(
                prompt=prompt,
                guest=guest,
                session_mode=session_mode,
                conversation_mode=conversation_mode,
                conversation_id=conversation_id,
                section_id=section_id,
                think_mode=think_mode,
                use_auto_cot=use_auto_cot,
                use_deep_think=use_deep_think,
                include_signed_query=include_signed_query,
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/runtime/request-preview/new", response_model=RequestPreviewResponse)
async def api_request_preview_new(payload: RequestPreviewRequest = Body()):
    try:
        return await _build_request_preview_response(payload, force_conversation_mode="new")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/runtime/request-preview/continue", response_model=RequestPreviewResponse)
async def api_request_preview_continue(payload: RequestPreviewRequest = Body()):
    try:
        return await _build_request_preview_response(payload, force_conversation_mode="continue")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/runtime/manual-verify/start", response_model=ManualVerifyStartResponse)
async def api_manual_verify_start(payload: ManualVerifyStartRequest = Body()):
    try:
        effective_guest = resolve_effective_guest_flag(payload.guest, payload.session_mode)
        data = await start_manual_verification(
            guest=effective_guest,
            conversation_mode=payload.conversation_mode,
            conversation_id=payload.conversation_id,
            open_conversation_id=payload.open_conversation_id,
            session_override=payload.session_params.model_dump() if payload.session_params else None,
            seed_existing_cookies=payload.seed_existing_cookies,
            reuse_existing_window=payload.reuse_existing_window,
            wait_for_ready=payload.wait_for_ready,
            wait_timeout_seconds=payload.wait_timeout_seconds,
            poll_interval_ms=payload.poll_interval_ms,
        )
        return ManualVerifyStartResponse(
            ok=True,
            ready=data["ready"],
            reused_window=bool(data.get("reused_window")),
            message=(
                "可见验证窗口已经准备好。"
                "完成验证后，后续第一次真实发送会自动完成 capture 并持久化。"
            ),
            open_url=data.get("open_url"),
            browser=BrowserRuntimeStateResponse(**data["browser"]),
            session_params=SessionParamsResponse(**data["session_params"]),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/runtime/manual-verify/finish", response_model=ManualVerifyFinishResponse)
async def api_manual_verify_finish(payload: ManualVerifyFinishRequest = Body()):
    try:
        effective_guest = resolve_effective_guest_flag(payload.guest, payload.session_mode)
        data = await finish_manual_verification(
            guest=effective_guest,
            conversation_id=payload.conversation_id,
            session_override=payload.session_params.model_dump() if payload.session_params else None,
            persist_session=payload.persist_session,
            close_window=payload.close_window,
        )
        return ManualVerifyFinishResponse(
            ok=True,
            persisted=data["persisted"],
            browser=BrowserRuntimeStateResponse(**data["browser"]),
            session_params=SessionParamsResponse(**data["session_params"]),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/runtime/manual-send", response_model=ManualSendResponse)
async def api_manual_send(payload: ManualSendRequest = Body()):
    try:
        effective_guest = resolve_effective_guest_flag(payload.guest, payload.session_mode)
        data = await send_manual_browser_message(
            prompt=payload.prompt,
            guest=effective_guest,
            conversation_id=payload.conversation_id,
            session_override=payload.session_params.model_dump() if payload.session_params else None,
            wait_timeout_seconds=payload.wait_timeout_seconds,
        )
        return ManualSendResponse(
            ok=True,
            sent=data["sent"],
            capture_count_before=data["capture_count_before"],
            capture_count_after=data["capture_count_after"],
            chat_completion=data.get("chat_completion"),
            text=data.get("text"),
            img_urls=data.get("img_urls"),
            conversation_id=data.get("conversation_id"),
            message_id=data.get("message_id"),
            section_id=data.get("section_id"),
            browser=BrowserRuntimeStateResponse(**data["browser"]),
            session_params=SessionParamsResponse(**data["session_params"]),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
