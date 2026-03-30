from typing import Literal


ConversationRequestMode = Literal["auto", "new", "continue"]

CONVERSATION_MODE_AUTO: ConversationRequestMode = "auto"
CONVERSATION_MODE_NEW: ConversationRequestMode = "new"
CONVERSATION_MODE_CONTINUE: ConversationRequestMode = "continue"
CONVERSATION_MODE_VALUES: set[str] = {
    CONVERSATION_MODE_AUTO,
    CONVERSATION_MODE_NEW,
    CONVERSATION_MODE_CONTINUE,
}


def normalize_conversation_mode(conversation_mode: str | None) -> ConversationRequestMode | None:
    normalized = str(conversation_mode or "").strip().lower()
    if normalized in CONVERSATION_MODE_VALUES:
        return normalized  # type: ignore[return-value]
    return None
