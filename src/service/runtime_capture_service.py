from typing import Any

from src.model.conversation_mode import CONVERSATION_MODE_NEW
from src.model.session_mode import resolve_effective_guest_flag
from src.service.browser_runtime import browser_runtime

from .doubao_service import (
    build_runtime_session_params,
    ensure_service_session,
    get_captured_chat_x_flow_trace,
    get_preferred_browser_capture_mapping,
    persist_session_state,
    resolve_requested_chat_target,
    should_start_fresh_guest_manual_verification,
    should_seed_guest_runtime_from_manual,
    sync_session_from_browser_runtime_state,
    sync_session_from_manual_browser_capture,
)


def _has_active_manual_window(runtime_status: dict[str, Any] | None) -> bool:
    if not isinstance(runtime_status, dict):
        return False
    return bool(
        runtime_status.get("manual_browser_started")
        and runtime_status.get("manual_context_ready")
        and runtime_status.get("manual_page_ready")
    )


def _build_bootstrap_message(
    *,
    capture_ready: bool,
    manual_ready: bool,
    manual_window_opened: bool,
    manual_window_active: bool,
    auto_launch_blocked: bool,
) -> str:
    if capture_ready:
        return "Runtime capture is ready and the request can be sent now."
    if manual_ready:
        return (
            "The visible verification window is ready. "
            "The next live send will reuse this window, capture the request, and persist the runtime state automatically."
        )
    if manual_window_opened:
        return (
            "A visible verification window has been opened. "
            "Complete the verification there and this page can continue waiting for chat readiness."
        )
    if manual_window_active:
        return "Waiting for the visible verification window to become chat-ready."
    if auto_launch_blocked:
        return (
            "Automatic visible browser relaunch is paused. "
            "Use the front-end verification button if you want to reopen the browser manually."
        )
    return "No reusable runtime capture is available yet. Please finish visible verification first."


async def bootstrap_runtime_capture(
    *,
    guest: bool = False,
    session_mode: str | None = None,
    conversation_mode: str | None = None,
    conversation_id: str | None = None,
    section_id: str | None = None,
    session_override: dict[str, Any] | None = None,
    seed_existing_cookies: bool = True,
    reuse_existing_window: bool = True,
    wait_for_ready: bool = False,
    wait_timeout_seconds: int = 300,
    poll_interval_ms: int = 1000,
) -> dict[str, Any]:
    effective_guest = resolve_effective_guest_flag(guest, session_mode=session_mode)
    requested_conversation_id, _, effective_conversation_mode = resolve_requested_chat_target(
        conversation_id=conversation_id,
        section_id=section_id,
        conversation_mode=conversation_mode,
    )
    session, session_meta = await ensure_service_session(
        requested_conversation_id=requested_conversation_id,
        effective_guest=effective_guest,
        session_override=session_override,
    )

    runtime_status = browser_runtime.get_status()
    guest_manual_seed = effective_guest and should_seed_guest_runtime_from_manual(runtime_status)
    should_sync_live_manual = (not effective_guest) or guest_manual_seed
    session_changed = False
    manual_window_opened = False
    reused_window = False
    open_url = None
    auto_launch_blocked = False

    if should_sync_live_manual and await browser_runtime.sync_live_manual_session(session):
        session_changed = True

    runtime_status = browser_runtime.get_status()
    if sync_session_from_browser_runtime_state(session, runtime_status):
        session_changed = True
    if sync_session_from_manual_browser_capture(
        session,
        session_mode=session_mode,
        guest=effective_guest,
    ):
        session_changed = True

    captured_chat_x_flow_trace = get_captured_chat_x_flow_trace(
        session_mode=session_mode,
        guest=effective_guest,
        session=session,
    )
    manual_ready = bool(runtime_status.get("manual_chat_ready"))
    manual_window_active = _has_active_manual_window(runtime_status)
    should_force_fresh_guest_manual = should_start_fresh_guest_manual_verification(
        effective_guest=effective_guest,
        session=session,
        requested_conversation_id=requested_conversation_id,
        session_meta=session_meta,
    )

    if not captured_chat_x_flow_trace and (
        should_force_fresh_guest_manual
        or (not manual_ready and not manual_window_active)
    ):
        manual_state = await browser_runtime.start_manual_verification(
            session,
            conversation_id=requested_conversation_id,
            force_new_chat=effective_conversation_mode == CONVERSATION_MODE_NEW,
            seed_existing_cookies=False if should_force_fresh_guest_manual else seed_existing_cookies,
            reuse_existing_window=False if should_force_fresh_guest_manual else reuse_existing_window,
            wait_for_ready=wait_for_ready,
            wait_timeout_seconds=wait_timeout_seconds,
            poll_interval_ms=poll_interval_ms,
            auto_open=True,
        )
        auto_launch_blocked = bool(manual_state.get("auto_launch_blocked"))
        manual_window_opened = bool(
            manual_state.get("window_available")
            and not manual_state.get("reused_window")
            and not auto_launch_blocked
        )
        reused_window = bool(manual_state.get("reused_window"))
        open_url = manual_state.get("open_url")

        if should_sync_live_manual and await browser_runtime.sync_live_manual_session(session):
            session_changed = True
        runtime_status = browser_runtime.get_status()
        if sync_session_from_browser_runtime_state(session, runtime_status):
            session_changed = True
        if sync_session_from_manual_browser_capture(
            session,
            session_mode=session_mode,
            guest=effective_guest,
        ):
            session_changed = True

        captured_chat_x_flow_trace = get_captured_chat_x_flow_trace(
            session_mode=session_mode,
            guest=effective_guest,
            session=session,
        )
        manual_ready = bool(runtime_status.get("manual_chat_ready"))
        manual_window_active = _has_active_manual_window(runtime_status)

    if session_changed and not session_override:
        session = persist_session_state(
            session,
            requested_conversation_id,
            guest=effective_guest,
        )

    capture_mapping = get_preferred_browser_capture_mapping(
        runtime_status=runtime_status,
        session_mode=session_mode,
        guest=effective_guest,
        session=session,
    )
    capture_ready = bool(captured_chat_x_flow_trace)

    return {
        "ready": bool(capture_ready or manual_ready),
        "capture_ready": capture_ready,
        "manual_ready": manual_ready,
        "manual_window_opened": manual_window_opened,
        "reused_window": reused_window,
        "awaiting_user_action": not (capture_ready or manual_ready),
        "message": _build_bootstrap_message(
            capture_ready=capture_ready,
            manual_ready=manual_ready,
            manual_window_opened=manual_window_opened,
            manual_window_active=manual_window_active,
            auto_launch_blocked=auto_launch_blocked,
        ),
        "open_url": open_url,
        "capture_source": capture_mapping.get("selected_capture_key") or None,
        "browser": runtime_status,
        "session_params": build_runtime_session_params(session),
    }


__all__ = ["bootstrap_runtime_capture"]
