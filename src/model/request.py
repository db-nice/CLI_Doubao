from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .conversation_mode import ConversationRequestMode
from .session_mode import SessionMode


class _CliCompatibleRequestModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SessionParamsRequest(_CliCompatibleRequestModel):
    aid: str
    real_aid: str | None = None
    web_tab_id: str | None = None
    bot_id: str | None = None
    fp: str | None = None
    ms_token: str | None = None
    a_bogus: str | None = None
    cookie: str
    device_id: str
    tea_uuid: str
    web_id: str
    room_id: str = "0"
    x_flow_trace: str


class CompletionRequest(_CliCompatibleRequestModel):
    prompt: str
    guest: bool = False
    session_mode: SessionMode | None = None
    conversation_mode: ConversationRequestMode | None = None
    attachments: list[dict] = Field(default_factory=list)
    conversation_id: str | None = None
    section_id: str | None = None
    think_mode: Literal[0, 1, 3] | None = None
    use_deep_think: bool = False
    use_auto_cot: bool = False
    session_params: SessionParamsRequest | None = None


class SessionRefreshRequest(_CliCompatibleRequestModel):
    guest: bool = False
    session_mode: SessionMode | None = None
    conversation_id: str | None = None
    session_params: SessionParamsRequest | None = None
    persist_session: bool = False


class RequestPreviewRequest(_CliCompatibleRequestModel):
    prompt: str = "你好"
    guest: bool = False
    session_mode: SessionMode | None = None
    conversation_mode: ConversationRequestMode | None = None
    conversation_id: str | None = None
    section_id: str | None = None
    think_mode: Literal[0, 1, 3] | None = None
    use_deep_think: bool = False
    use_auto_cot: bool = False
    include_signed_query: bool = True
    session_params: SessionParamsRequest | None = None


class RequestFixtureDiffRequest(_CliCompatibleRequestModel):
    prompt: str = "你好"
    guest: bool = False
    session_mode: SessionMode | None = None
    conversation_mode: ConversationRequestMode | None = None
    conversation_id: str | None = None
    section_id: str | None = None
    think_mode: Literal[0, 1, 3] | None = None
    use_deep_think: bool = False
    use_auto_cot: bool = False
    include_signed_query: bool = True
    session_params: SessionParamsRequest | None = None
    fixture_path: str | None = r"D:\bot\doubao\1.txt"
    fixture_text: str | None = None


class ManualVerifyStartRequest(_CliCompatibleRequestModel):
    guest: bool = False
    session_mode: SessionMode | None = None
    conversation_mode: ConversationRequestMode | None = None
    conversation_id: str | None = None
    open_conversation_id: str | None = None
    session_params: SessionParamsRequest | None = None
    seed_existing_cookies: bool = False
    reuse_existing_window: bool = True
    wait_for_ready: bool = False
    wait_timeout_seconds: int = 300
    poll_interval_ms: int = 1000


class ManualVerifyFinishRequest(_CliCompatibleRequestModel):
    guest: bool = False
    session_mode: SessionMode | None = None
    conversation_id: str | None = None
    session_params: SessionParamsRequest | None = None
    persist_session: bool = True
    close_window: bool = True


class ManualSendRequest(_CliCompatibleRequestModel):
    prompt: str = "你好"
    guest: bool = False
    session_mode: SessionMode | None = None
    conversation_id: str | None = None
    session_params: SessionParamsRequest | None = None
    wait_timeout_seconds: int = 30


class RuntimeBootstrapRequest(_CliCompatibleRequestModel):
    guest: bool = False
    session_mode: SessionMode | None = None
    conversation_mode: ConversationRequestMode | None = None
    conversation_id: str | None = None
    section_id: str | None = None
    session_params: SessionParamsRequest | None = None
    seed_existing_cookies: bool = True
    reuse_existing_window: bool = True
    wait_for_ready: bool = False
    wait_timeout_seconds: int = 300
    poll_interval_ms: int = 1000


class GuestSessionResetRequest(_CliCompatibleRequestModel):
    guest: bool = True
    session_mode: SessionMode | None = None
    open_verification_window: bool = False
    wait_for_ready: bool = False
    wait_timeout_seconds: int = 300
    poll_interval_ms: int = 1000


class GuestSessionRestoreRequest(_CliCompatibleRequestModel):
    guest: bool = True
    session_mode: SessionMode | None = None
    snapshot_id: str
    open_verification_window: bool = False
    wait_for_ready: bool = False
    wait_timeout_seconds: int = 300
    poll_interval_ms: int = 1000


class AttachmentRequest(_CliCompatibleRequestModel):
    key: str
    name: str
    type: str
    file_review_state: int
    file_parse_state: int
    identifier: str
    option: dict | None = None
    md5: str | None = None
    size: int | None = None


class UploadRequest(_CliCompatibleRequestModel):
    file_type: int
    file_name: str
    file_bytes: bytes
