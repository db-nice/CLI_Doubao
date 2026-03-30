from typing import Literal


SessionMode = Literal["auth", "guest", "auto"]

SESSION_MODE_AUTH: SessionMode = "auth"
SESSION_MODE_GUEST: SessionMode = "guest"
SESSION_MODE_AUTO: SessionMode = "auto"
SESSION_MODE_VALUES: set[str] = {
    SESSION_MODE_AUTH,
    SESSION_MODE_GUEST,
    SESSION_MODE_AUTO,
}


def normalize_session_mode(session_mode: str | None) -> SessionMode | None:
    normalized = str(session_mode or "").strip().lower()
    if normalized in SESSION_MODE_VALUES:
        return normalized  # type: ignore[return-value]
    return None


def resolve_effective_guest_flag(guest: bool, session_mode: str | None = None) -> bool:
    normalized = normalize_session_mode(session_mode)
    if normalized == SESSION_MODE_GUEST:
        return True
    if normalized == SESSION_MODE_AUTH:
        return False
    return bool(guest)


def resolve_capture_mapping_mode(
    session_mode: str | None = None,
    guest: bool | None = None,
) -> SessionMode:
    normalized = normalize_session_mode(session_mode)
    if normalized:
        return normalized
    if guest is True:
        return SESSION_MODE_GUEST
    if guest is False:
        return SESSION_MODE_AUTH
    return SESSION_MODE_AUTO
