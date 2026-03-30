import os

from src.pool import session_pool
from src.service.browser_runtime import browser_runtime


async def bootstrap_runtime_from_snapshot() -> dict[str, int | bool]:
    await session_pool.fetch_guest_session(0)

    restored_auth_snapshot_count = len(session_pool.auth_sessions)
    preheat_all = os.getenv("DOUBAO_PREHEAT_ALL_SESSIONS", "0") == "1"
    sessions_to_preheat = session_pool.auth_sessions if preheat_all else session_pool.auth_sessions[:1]

    warmed_up = 0
    for session in sessions_to_preheat:
        await browser_runtime.ensure_ready(session, seed_manual_storage=False)
        warmed_up += 1

    if warmed_up:
        session_pool.save_to_file()

    return {
        "restored_auth_snapshot_count": restored_auth_snapshot_count,
        "preheat_all_sessions": preheat_all,
        "warmed_up_session_count": warmed_up,
    }
