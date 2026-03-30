from pydantic import BaseModel, Field, model_serializer, model_validator

from .conversation_mode import ConversationRequestMode
import uuid


class SessionParamsResponse(BaseModel):
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
    room_id: str
    x_flow_trace: str


class SessionRefreshResponse(BaseModel):
    ok: bool
    session_params: SessionParamsResponse


class BrowserRuntimeStateResponse(BaseModel):
    edge_executable_path: str
    edge_executable_exists: bool
    playwright_started: bool
    browser_started: bool
    context_ready: bool
    page_ready: bool
    session_bound: bool
    signer_ready: bool
    fp: str | None = None
    ms_token: str | None = None
    current_url: str | None = None
    page_title: str | None = None
    last_error: str | None = None
    manual_browser_started: bool
    manual_context_ready: bool
    manual_page_ready: bool
    manual_session_bound: bool
    manual_fp: str | None = None
    manual_ms_token: str | None = None
    manual_current_url: str | None = None
    manual_page_title: str | None = None
    manual_last_error: str | None = None
    manual_logged_in: bool = False
    manual_chat_ready: bool = False
    hidden_capture_count: int = 0
    manual_capture_count: int = 0
    hidden_signer_call_count: int = 0
    manual_signer_call_count: int = 0
    hidden_request_call_count: int = 0
    manual_request_call_count: int = 0


class RuntimeStatusResponse(BaseModel):
    browser: BrowserRuntimeStateResponse
    curl_impersonate: str
    curl_cffi_available: bool
    preheat_all_sessions: bool
    session_found: bool
    session_params: SessionParamsResponse | None = None


class RequestPreviewResponse(BaseModel):
    ok: bool
    browser: BrowserRuntimeStateResponse
    session_params: SessionParamsResponse
    request: dict
    checks: dict


class RequestFixtureDiffResponse(BaseModel):
    ok: bool
    browser: BrowserRuntimeStateResponse
    session_params: SessionParamsResponse
    preview_request: dict
    fixture: dict
    diffs: dict


class ManualVerifyStartResponse(BaseModel):
    ok: bool
    ready: bool
    reused_window: bool = False
    message: str
    open_url: str | None = None
    browser: BrowserRuntimeStateResponse
    session_params: SessionParamsResponse


class ManualVerifyFinishResponse(BaseModel):
    ok: bool
    persisted: bool
    browser: BrowserRuntimeStateResponse
    session_params: SessionParamsResponse


class ManualSendResponse(BaseModel):
    ok: bool
    sent: bool
    capture_count_before: int
    capture_count_after: int
    chat_completion: dict | None = None
    text: str | None = None
    img_urls: list[str] = Field(default_factory=list)
    conversation_id: str | None = None
    message_id: str | None = None
    section_id: str | None = None
    browser: BrowserRuntimeStateResponse
    session_params: SessionParamsResponse


class RuntimeBootstrapResponse(BaseModel):
    ok: bool
    ready: bool
    capture_ready: bool
    manual_ready: bool
    manual_window_opened: bool = False
    reused_window: bool = False
    awaiting_user_action: bool = False
    message: str
    open_url: str | None = None
    capture_source: str | None = None
    browser: BrowserRuntimeStateResponse
    session_params: SessionParamsResponse


class GuestSessionResetResponse(BaseModel):
    ok: bool
    rotated: bool
    backup_path: str | None = None
    backup_conversation_ids: list[str] = Field(default_factory=list)
    previous_conversation_count: int = 0
    guest_conversation_limit: int = 5
    verification_window_opened: bool = False
    ready: bool = False
    manual_ready: bool = False
    capture_ready: bool = False
    message: str
    open_url: str | None = None
    browser: BrowserRuntimeStateResponse
    session_params: SessionParamsResponse


class GuestSessionSnapshotResponse(BaseModel):
    snapshot_id: str
    source: str
    active: bool = False
    label: str
    room_id: str | None = None
    conversation_count: int = 0
    conversation_ids: list[str] = Field(default_factory=list)
    backup_path: str | None = None
    backup_at_ms: int | None = None
    reason: str | None = None
    metadata: dict | None = None
    session_params: SessionParamsResponse | None = None


class GuestSessionSnapshotsResponse(BaseModel):
    ok: bool
    active_snapshot_id: str | None = None
    snapshots: list[GuestSessionSnapshotResponse] = Field(default_factory=list)


class GuestSessionRestoreResponse(BaseModel):
    ok: bool
    restored: bool
    snapshot_id: str
    source: str | None = None
    conversation_ids: list[str] = Field(default_factory=list)
    conversation_count: int = 0
    displaced_backup_path: str | None = None
    displaced_conversation_ids: list[str] = Field(default_factory=list)
    restored_conversation_id: str | None = None
    restored_conversation_name: str | None = None
    restored_section_id: str | None = None
    history_ready: bool = False
    history_error: str | None = None
    history_messages: list[dict] = Field(default_factory=list)
    verification_window_opened: bool = False
    ready: bool = False
    manual_ready: bool = False
    capture_ready: bool = False
    message: str
    open_url: str | None = None
    browser: BrowserRuntimeStateResponse
    session_params: SessionParamsResponse


class CompletionResponse(BaseModel):
    text: str
    img_urls: list[str] = Field(default_factory=list)
    conversation_id: str
    message_id: str
    messageg_id: str | None = None
    section_id: str
    session_params: SessionParamsResponse | None = None
    request_mode: ConversationRequestMode | None = None
    follow_up_required: bool = False
    follow_up_conversation_mode: ConversationRequestMode | None = None

    @model_validator(mode="after")
    def sync_legacy_message_id(self):
        if not self.messageg_id:
            self.messageg_id = self.message_id
        return self

    @model_serializer(mode="plain")
    def serialize_completion_response(self):
        return {
            "text": self.text,
            "img_urls": list(self.img_urls or []),
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "messageg_id": self.messageg_id or self.message_id,
            "section_id": self.section_id,
            "session_params": self.session_params.model_dump() if self.session_params else None,
            "request_mode": self.request_mode,
            "follow_up_required": self.follow_up_required,
            "follow_up_conversation_mode": self.follow_up_conversation_mode,
        }
    
    
class UploadResponse(BaseModel):
    key: str
    name: str
    type: str
    file_review_state: int
    file_parse_state: int
    identifier: str
    option: dict | None = None
    md5: str | None = None
    size: int | None = None
    

class ImageResponse(BaseModel):
    key: str
    name: str
    option: dict
    type: str = "vlm_image"
    file_review_state: int = 3
    file_parse_state: int = 3
    identifier: str = str(uuid.uuid1())


class FileResponse(BaseModel):
    key: str
    name: str
    md5: str
    size: int
    type: str = "file"
    file_review_state: int = 1
    file_parse_state: int = 3
    identifier: str = str(uuid.uuid1())
    

class DeleteResponse(BaseModel):
    ok: bool
    msg: str


class ConversationInfoResponse(BaseModel):
    conversation_id: str
    name: str
    section_id: str | None = None
    latest_index: str | None = None
    badge_count: str | None = None


class ConversationMessageResponse(BaseModel):
    message_id: str
    role: str
    text: str
    create_time: str | None = None
    index_in_conv: str | None = None
    content_type: int | None = None


class ConversationMessagesResponse(BaseModel):
    conversation_id: str
    section_id: str | None = None
    messages: list[ConversationMessageResponse]
