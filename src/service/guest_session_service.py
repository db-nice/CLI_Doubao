import os
from typing import Any

from src.model.conversation_mode import CONVERSATION_MODE_CONTINUE, CONVERSATION_MODE_NEW
from src.pool.session_pool import DoubaoSession, session_pool
from src.service.browser_runtime import browser_runtime

from .doubao_service import (
    build_hidden_guest_seed_session,
    build_runtime_session_params,
    get_conversation_info,
    get_conversation_messages,
    persist_session_state,
    start_manual_verification,
    should_seed_guest_runtime_from_manual,
    sync_session_from_browser_runtime_state,
    sync_session_from_manual_browser_capture,
)


def get_guest_conversation_limit() -> int:
    raw_value = str(os.getenv("DOUBAO_GUEST_CONVERSATION_LIMIT", "5")).strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        return 5


def get_guest_conversation_count(session: DoubaoSession | None) -> int:
    return len(session_pool.get_session_conversation_ids(session))


async def list_guest_session_snapshots(limit_backups: int = 20) -> dict[str, Any]:
    current_session = session_pool.get_session(None, guest=True)
    runtime_status = browser_runtime.get_status()
    session_changed = False
    if current_session and should_seed_guest_runtime_from_manual(runtime_status):
        if await browser_runtime.sync_live_manual_session(current_session):
            session_changed = True
        runtime_status = browser_runtime.get_status()
        if sync_session_from_browser_runtime_state(current_session, runtime_status):
            session_changed = True
        if sync_session_from_manual_browser_capture(
            current_session,
            session_mode="guest",
            guest=True,
        ):
            session_changed = True
        if session_changed:
            persist_session_state(
                current_session,
                getattr(current_session, "room_id", None),
                guest=True,
            )
    snapshots = session_pool.list_guest_session_snapshots(limit_backups=limit_backups)
    active_snapshot_id = None
    for snapshot in snapshots:
        if snapshot.get("active"):
            active_snapshot_id = snapshot.get("snapshot_id")
            break
    return {
        "active_snapshot_id": active_snapshot_id,
        "snapshots": snapshots,
    }


def _normalize_conversation_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if not normalized or normalized == "0":
        return None
    return normalized


def _resolve_snapshot_primary_conversation_id(
    session: DoubaoSession | None,
    conversation_ids: list[str] | None,
) -> str | None:
    room_id = _normalize_conversation_id(getattr(session, "room_id", None))
    normalized_ids = [
        item
        for item in (_normalize_conversation_id(value) for value in (conversation_ids or []))
        if item
    ]
    if room_id and room_id in normalized_ids:
        return room_id
    if room_id:
        return room_id
    return normalized_ids[0] if normalized_ids else None


async def _build_restored_history_payload(conversation_id: str | None) -> dict[str, Any]:
    if not conversation_id:
        return {
            "restored_conversation_id": None,
            "restored_conversation_name": None,
            "restored_section_id": None,
            "history_ready": False,
            "history_error": None,
            "history_messages": [],
        }

    try:
        info = await get_conversation_info(conversation_id)
        message_payload = await get_conversation_messages(
            conversation_id=conversation_id,
            limit=20,
        )
        return {
            "restored_conversation_id": conversation_id,
            "restored_conversation_name": info.get("name") or "继续对话",
            "restored_section_id": message_payload.get("section_id") or info.get("last_section_id"),
            "history_ready": True,
            "history_error": None,
            "history_messages": list(message_payload.get("messages") or []),
        }
    except Exception as exc:
        return {
            "restored_conversation_id": conversation_id,
            "restored_conversation_name": None,
            "restored_section_id": None,
            "history_ready": False,
            "history_error": str(exc),
            "history_messages": [],
        }


async def reset_guest_session(
    *,
    current_session: DoubaoSession | None = None,
    reason: str = "manual_reset",
    open_verification_window: bool = False,
    wait_for_ready: bool = False,
    wait_timeout_seconds: int = 300,
    poll_interval_ms: int = 1000,
) -> dict[str, Any]:
    if current_session is None:
        current_session = session_pool.get_session(None, guest=True)

    previous_conversation_count = get_guest_conversation_count(current_session)
    backup_path, backup_conversation_ids = session_pool.backup_guest_session(
        current_session,
        reason=reason,
        metadata={
            "previous_conversation_count": previous_conversation_count,
        },
    )
    session_pool.clear_session_bindings(current_session, clear_conversation_state=True)
    session_pool.remove_guest_session(current_session)
    session_pool.save_to_file(guest=True)

    if open_verification_window:
        await browser_runtime.close()

    new_session = persist_session_state(
        build_hidden_guest_seed_session(),
        conversation_id=None,
        guest=True,
    )

    verification = None
    browser_state = browser_runtime.get_status()
    if open_verification_window:
        verification = await start_manual_verification(
            guest=True,
            conversation_mode=CONVERSATION_MODE_NEW,
            conversation_id=None,
            session_override=new_session.model_dump(),
            seed_existing_cookies=False,
            reuse_existing_window=False,
            wait_for_ready=wait_for_ready,
            wait_timeout_seconds=wait_timeout_seconds,
            poll_interval_ms=poll_interval_ms,
        )
        browser_state = verification["browser"]

    return {
        "rotated": True,
        "backup_path": backup_path,
        "backup_conversation_ids": backup_conversation_ids,
        "previous_conversation_count": previous_conversation_count,
        "guest_conversation_limit": get_guest_conversation_limit(),
        "verification_window_opened": bool(open_verification_window),
        "ready": bool(verification["ready"]) if verification else False,
        "manual_ready": bool(verification["ready"]) if verification else False,
        "capture_ready": False,
        "open_url": verification.get("open_url") if verification else None,
        "message": (
            "A fresh guest session has been created. Complete visible verification if the browser shows a challenge, then retry the send."
            if open_verification_window
            else "A fresh guest session has been created. Open the verification browser later from the front end if needed."
        ),
        "browser": browser_state,
        "session_params": build_runtime_session_params(new_session),
    }


async def restore_guest_session_snapshot(
    *,
    snapshot_id: str,
    open_verification_window: bool = False,
    wait_for_ready: bool = False,
    wait_timeout_seconds: int = 300,
    poll_interval_ms: int = 1000,
) -> dict[str, Any]:
    snapshot = session_pool.get_guest_session_snapshot(snapshot_id)
    if not snapshot:
        raise ValueError(f"Guest snapshot not found: {snapshot_id}")

    target_session = DoubaoSession.from_dict(dict(snapshot.get("session") or {}))
    target_conversation_ids = list(snapshot.get("conversation_ids") or [])
    current_session = session_pool.get_session(None, guest=True)

    displaced_backup_path = None
    displaced_conversation_ids: list[str] = []
    if current_session is not None and not session_pool._sessions_match(current_session, target_session):
        displaced_backup_path, displaced_conversation_ids = session_pool.backup_guest_session(
            current_session,
            reason="restore_switch",
            metadata={
                "restore_target_snapshot_id": snapshot_id,
            },
        )

    session_pool.clear_session_bindings(current_session, clear_conversation_state=False)
    session_pool.remove_guest_session(current_session)
    session_pool.save_to_file(guest=True)

    if open_verification_window:
        await browser_runtime.close()

    restored_session = persist_session_state(
        target_session,
        conversation_id=target_session.room_id if str(target_session.room_id or "").strip() not in {"", "0"} else None,
        guest=True,
    )
    for conversation_id in target_conversation_ids:
        if conversation_id and conversation_id != "0":
            session_pool.set_session(str(conversation_id), restored_session)
    session_pool.save_to_file(guest=True)

    restored_conversation_id = _resolve_snapshot_primary_conversation_id(
        restored_session,
        target_conversation_ids,
    )
    history_payload = await _build_restored_history_payload(restored_conversation_id)

    verification = None
    browser_state = browser_runtime.get_status()
    open_conversation_id = str(getattr(restored_session, "room_id", "") or "").strip()
    request_mode = (
        CONVERSATION_MODE_CONTINUE
        if open_conversation_id and open_conversation_id != "0"
        else CONVERSATION_MODE_NEW
    )
    if open_verification_window:
        verification = await start_manual_verification(
            guest=True,
            conversation_mode=request_mode,
            conversation_id=open_conversation_id if request_mode == CONVERSATION_MODE_CONTINUE else None,
            open_conversation_id=open_conversation_id if request_mode == CONVERSATION_MODE_CONTINUE else None,
            session_override=restored_session.model_dump(),
            seed_existing_cookies=True,
            reuse_existing_window=False,
            wait_for_ready=wait_for_ready,
            wait_timeout_seconds=wait_timeout_seconds,
            poll_interval_ms=poll_interval_ms,
        )
        browser_state = verification["browser"]

    return {
        "restored": True,
        "snapshot_id": snapshot.get("snapshot_id"),
        "source": snapshot.get("source"),
        "conversation_ids": target_conversation_ids,
        "conversation_count": len(target_conversation_ids),
        "restored_conversation_id": history_payload.get("restored_conversation_id"),
        "restored_conversation_name": history_payload.get("restored_conversation_name"),
        "restored_section_id": history_payload.get("restored_section_id"),
        "history_ready": bool(history_payload.get("history_ready")),
        "history_error": history_payload.get("history_error"),
        "history_messages": list(history_payload.get("history_messages") or []),
        "verification_window_opened": bool(open_verification_window),
        "ready": bool(verification["ready"]) if verification else False,
        "manual_ready": bool(verification["ready"]) if verification else False,
        "capture_ready": False,
        "open_url": verification.get("open_url") if verification else None,
        "message": (
            "Guest cache restored. If the browser shows a challenge, complete verification there and then continue sending."
            if open_verification_window
            else "Guest cache restored. Conversation history is ready to load in the front end; open the verification browser only if you need live sending."
        ),
        "browser": browser_state,
        "session_params": build_runtime_session_params(restored_session),
        "displaced_backup_path": displaced_backup_path,
        "displaced_conversation_ids": displaced_conversation_ids,
    }


async def maybe_rotate_guest_session_for_new_conversation(
    session: DoubaoSession | None,
    *,
    open_verification_window: bool = False,
) -> dict[str, Any] | None:
    conversation_count = get_guest_conversation_count(session)
    if conversation_count < get_guest_conversation_limit():
        return None
    return await reset_guest_session(
        current_session=session,
        reason="conversation_limit_reached",
        open_verification_window=open_verification_window,
        wait_for_ready=False,
    )


__all__ = [
    "get_guest_conversation_count",
    "get_guest_conversation_limit",
    "list_guest_session_snapshots",
    "maybe_rotate_guest_session_for_new_conversation",
    "reset_guest_session",
    "restore_guest_session_snapshot",
]
