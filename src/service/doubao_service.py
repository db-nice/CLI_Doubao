from src.pool.session_pool import DoubaoSession, session_pool
from src.model.conversation_mode import (
    CONVERSATION_MODE_AUTO,
    CONVERSATION_MODE_CONTINUE,
    CONVERSATION_MODE_NEW,
    normalize_conversation_mode,
)
from src.model.session_mode import (
    SESSION_MODE_AUTH,
    SESSION_MODE_GUEST,
    resolve_capture_mapping_mode,
    resolve_effective_guest_flag,
)
from src.service.browser_runtime import browser_runtime
from requests_aws4auth import AWS4Auth
from fastapi import HTTPException
from loguru import logger
import asyncio
import aiohttp
import copy
import httpx
import codecs
import json
import re
import time
import urllib.parse
import uuid
import hashlib
import binascii
import os
from typing import Any

try:
    from curl_cffi import requests as curl_cffi_requests
except ImportError:
    curl_cffi_requests = None

PC_VERSION = "3.11.3"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"
SEC_CH_UA = '"Chromium";v="146", "Not-A.Brand";v="24", "Microsoft Edge";v="146"'
SEC_CH_UA_MOBILE = "?0"
SEC_CH_UA_PLATFORM = '"Windows"'
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"
MAX_SAFE_ANCHOR_INDEX = 9007199254740991
LEGACY_FALLBACK_BOT_ID = "7338286299411103781"
DEFAULT_GUEST_AID = os.getenv("DOUBAO_GUEST_AID", "497858")
DEFAULT_GUEST_REAL_AID = os.getenv("DOUBAO_GUEST_REAL_AID", DEFAULT_GUEST_AID)
GENERATED_GUEST_CAPTURE_CACHE_SOURCE = "runtime_capture"
BROWSER_CAPTURE_STAGE_KEYS = (
    "chain_single",
    "alice_profile_self",
    "alice_user_launch",
    "get_nexpulse",
    "send_rate_limit",
    "chat_completion",
)
BROWSER_CAPTURE_STAGE_PRIORITY = (
    "chat_completion",
    "send_rate_limit",
    "get_nexpulse",
    "alice_user_launch",
    "alice_profile_self",
    "chain_single",
)
BROWSER_CAPTURE_REQUEST_MARKERS = {
    "chain_single": "/im/chain/single",
    "alice_profile_self": "/alice/profile/self",
    "alice_user_launch": "/alice/user/launch",
    "get_nexpulse": "/biz/onboarding/get_nexpulse",
    "send_rate_limit": "/im/message/send_rate_limit",
    "chat_completion": "/chat/completion",
}
REQUEST_HEADER_DEBUG_KEYS = (
    "referer",
    "origin",
    "content-type",
    "agw-js-conv",
    "x-flow-trace",
    "last-event-id",
)
RESPONSE_HEADER_DEBUG_KEYS = (
    "content-type",
    "x-ms-token",
    "x-tt-agw-login",
    "x-tt-logid",
    "x-tt-timestamp",
    "x-tt-trace-id",
    "x-tt-trace-tag",
    "tt_stable",
)

THINK_MODE_FAST = 0
THINK_MODE_REASON = 1
THINK_MODE_DEEP = 3
TEMP_CONVERSATION_ID_PATTERN = re.compile(r"^(local|load)_", re.IGNORECASE)


def _normalize_runtime_conversation_id(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized or normalized == "0":
        return ""
    if TEMP_CONVERSATION_ID_PATTERN.match(normalized):
        return ""
    return normalized


def resolve_think_mode(
    think_mode: int | None,
    use_auto_cot: bool,
    use_deep_think: bool
) -> int:
    if think_mode is not None:
        return think_mode
    if use_deep_think:
        return THINK_MODE_DEEP
    if use_auto_cot:
        return THINK_MODE_REASON
    return THINK_MODE_FAST


def resolve_requested_chat_target(
    *,
    conversation_id: str | None,
    section_id: str | None,
    conversation_mode: str | None = None,
) -> tuple[str | None, str | None, str]:
    new_session_tokens = {None, "", "0"}
    normalized_mode = normalize_conversation_mode(conversation_mode) or CONVERSATION_MODE_AUTO
    requested_conversation_id = _normalize_runtime_conversation_id(conversation_id) or None
    requested_section_id = None if section_id in new_session_tokens else str(section_id)

    if normalized_mode == CONVERSATION_MODE_NEW:
        if requested_conversation_id is not None:
            raise HTTPException(
                status_code=400,
                detail="conversation_mode='new' does not accept conversation_id; use /api/chat/completions/continue for continuation.",
            )
        if requested_section_id is not None:
            raise HTTPException(
                status_code=400,
                detail="conversation_mode='new' does not accept section_id.",
            )
        return None, None, CONVERSATION_MODE_NEW

    if normalized_mode == CONVERSATION_MODE_CONTINUE:
        if requested_conversation_id is None:
            raise HTTPException(
                status_code=400,
                detail="conversation_mode='continue' requires conversation_id.",
            )
        return requested_conversation_id, requested_section_id, CONVERSATION_MODE_CONTINUE

    effective_mode = CONVERSATION_MODE_CONTINUE if requested_conversation_id else CONVERSATION_MODE_NEW
    return requested_conversation_id, requested_section_id, effective_mode


def decode_jsonish(value, max_depth: int = 4):
    current = value
    for _ in range(max_depth):
        if isinstance(current, (dict, list)):
            return current
        if not isinstance(current, str):
            return current
        stripped = current.lstrip("\ufeff").strip()
        if not stripped:
            return ""
        if not (
            (stripped.startswith("{") and stripped.endswith("}")) or
            (stripped.startswith("[") and stripped.endswith("]")) or
            (stripped.startswith('"') and stripped.endswith('"'))
        ):
            return current
        try:
            current = json.loads(stripped)
        except json.JSONDecodeError:
            return current
    return current


def parse_cookie_header(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in str(cookie_header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if not sep or not name:
            continue
        cookies[name] = value
    return cookies


def build_cookie_header(cookie_map: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookie_map.items() if name)


def merge_cookie_headers(base_cookie_header: str, overlay_cookie_header: str) -> str:
    base = parse_cookie_header(base_cookie_header)
    overlay = parse_cookie_header(overlay_cookie_header)
    merged = dict(base)
    merged.update(overlay)
    return build_cookie_header(merged)


def generate_x_flow_trace() -> str:
    return f"04-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01"


def parse_query_params_from_url(url: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(str(url or ""))
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    return {
        key: values[-1]
        for key, values in query.items()
        if values
    }


def parse_query_items_from_url(url: str) -> list[tuple[str, str]]:
    parsed = urllib.parse.urlparse(str(url or ""))
    return urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)


def build_unsigned_query_from_items(items: list[tuple[str, str]]) -> str:
    filtered_items = [(key, value) for key, value in items if key and key != "a_bogus"]
    return urllib.parse.urlencode(filtered_items)


def _filtered_header_map(headers: dict | None, allowed_keys: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(headers, dict):
        return {}
    allowed = {key.lower() for key in allowed_keys}
    filtered: dict[str, str] = {}
    for key, value in headers.items():
        key_text = _non_empty_text(key)
        value_text = _non_empty_text(value)
        if not key_text or not value_text:
            continue
        if key_text.lower() not in allowed:
            continue
        filtered[key_text] = value_text
    return filtered


def _decode_json_text(text: str):
    stripped = str(text or "").lstrip("\ufeff").strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except Exception:
        return None


def _extract_chat_request_meta(body_text: str) -> dict[str, Any]:
    parsed = _decode_json_text(body_text)
    if not isinstance(parsed, dict):
        return {}

    client_meta = decode_jsonish(parsed.get("client_meta"), max_depth=4)
    option = decode_jsonish(parsed.get("option"), max_depth=4)
    messages = decode_jsonish(parsed.get("messages"), max_depth=4)

    meta: dict[str, Any] = {}
    if isinstance(client_meta, dict):
        for key in ("bot_id", "conversation_id", "local_conversation_id", "last_section_id"):
            value = _non_empty_text(client_meta.get(key))
            if value:
                meta[key] = value

    if isinstance(option, dict):
        for key in ("send_message_scene", "unique_key"):
            value = _non_empty_text(option.get(key))
            if value:
                meta[key] = value
        if option.get("need_create_conversation") is not None:
            meta["need_create_conversation"] = bool(option.get("need_create_conversation"))

    if isinstance(messages, list) and messages:
        first_message = decode_jsonish(messages[0], max_depth=4)
        if isinstance(first_message, dict):
            local_message_id = _non_empty_text(first_message.get("local_message_id"))
            if local_message_id:
                meta["local_message_id"] = local_message_id
            content_blocks = decode_jsonish(first_message.get("content_block"), max_depth=4)
            if isinstance(content_blocks, list) and content_blocks:
                first_block = decode_jsonish(content_blocks[0], max_depth=4)
                if isinstance(first_block, dict):
                    block_content = decode_jsonish(first_block.get("content"), max_depth=4)
                    if isinstance(block_content, dict):
                        text_block = decode_jsonish(block_content.get("text_block"), max_depth=4)
                        if isinstance(text_block, dict):
                            prompt_text = _non_empty_text(text_block.get("text"))
                            if prompt_text:
                                meta["prompt_preview"] = prompt_text[:120]

    return meta


def _extract_chain_single_meta(body_text: str) -> dict[str, Any]:
    parsed = _decode_json_text(body_text)
    if not isinstance(parsed, dict):
        return {}
    uplink = decode_jsonish(parsed.get("uplink_body"), max_depth=4)
    if not isinstance(uplink, dict):
        return {}
    chain_body = decode_jsonish(uplink.get("pull_singe_chain_uplink_body"), max_depth=4)
    if not isinstance(chain_body, dict):
        return {}
    meta: dict[str, Any] = {}
    for key in ("conversation_id",):
        value = _non_empty_text(chain_body.get(key))
        if value:
            meta[key] = value
    for key in ("anchor_index", "limit", "direction", "conversation_type"):
        value = chain_body.get(key)
        if value not in (None, ""):
            meta[key] = value
    return meta


def _extract_nexpulse_request_meta(body_text: str) -> dict[str, Any]:
    parsed = _decode_json_text(body_text)
    if not isinstance(parsed, dict):
        return {}
    meta: dict[str, Any] = {}
    bot_id = _non_empty_text(parsed.get("bot_id"))
    if bot_id:
        meta["bot_id"] = bot_id
    scene = parsed.get("scene")
    if scene not in (None, ""):
        meta["scene"] = scene
    return meta


def _deep_clone_json_value(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return copy.deepcopy(value)


def _decode_dict_like(value: Any) -> dict[str, Any]:
    decoded = decode_jsonish(value, max_depth=8)
    if isinstance(decoded, dict):
        return _deep_clone_json_value(decoded)
    return {}


def _decode_list_like(value: Any) -> list[Any]:
    decoded = decode_jsonish(value, max_depth=8)
    if isinstance(decoded, list):
        return _deep_clone_json_value(decoded)
    return []


def _encode_like_template(original_value: Any, updated_value: Any):
    if isinstance(original_value, str):
        return json.dumps(updated_value, ensure_ascii=False, separators=(",", ":"))
    return updated_value


def _extract_chat_request_body_template(body_value: Any) -> dict[str, Any] | None:
    parsed = decode_jsonish(body_value, max_depth=8)
    if not isinstance(parsed, dict):
        return None

    messages = _decode_list_like(parsed.get("messages"))
    first_message = messages[0] if messages and isinstance(messages[0], dict) else {}
    has_modern_shape = (
        any(key in parsed for key in ("client_meta", "option", "ext"))
        or "content_block" in first_message
        or "local_message_id" in first_message
    )
    if not has_modern_shape:
        return None
    return _deep_clone_json_value(parsed)


def _normalize_chat_request_template_for_mode(
    body_value: Any,
    *,
    is_new_conversation: bool,
) -> dict[str, Any] | None:
    template = _extract_chat_request_body_template(body_value)
    if not isinstance(template, dict):
        return None

    messages_template = template.get("messages")
    messages = _decode_list_like(messages_template)
    if messages and isinstance(messages[0], dict):
        first_message = _deep_clone_json_value(messages[0])
        for key in ("message_id", "query_id", "section_id", "conversation_id"):
            first_message.pop(key, None)
        if "content_block" in first_message or "content" not in first_message:
            content_blocks_template = first_message.get("content_block")
            content_blocks = _decode_list_like(content_blocks_template)
            if content_blocks and isinstance(content_blocks[0], dict):
                first_block = _deep_clone_json_value(content_blocks[0])
                first_block["parent_id"] = ""
                first_block["meta_info"] = []
                first_block["append_fields"] = []
                content_blocks[0] = first_block
                first_message["content_block"] = _encode_like_template(content_blocks_template, content_blocks)
        messages[0] = first_message
        template["messages"] = _encode_like_template(messages_template, messages)

    option_template = template.get("option")
    option = _decode_dict_like(option_template)
    if is_new_conversation:
        option["start_seq"] = 0
    template["option"] = _encode_like_template(option_template, option)

    ext_template = template.get("ext")
    ext = _decode_dict_like(ext_template)
    if is_new_conversation:
        ext.pop("resend", None)
    template["ext"] = _encode_like_template(ext_template, ext)
    return template


def _detect_chat_request_message_shape(template: dict[str, Any]) -> str:
    messages = _decode_list_like(template.get("messages"))
    if not messages or not isinstance(messages[0], dict):
        return "content_block"
    first_message = messages[0]
    if "content_block" in first_message:
        return "content_block"
    if "content" in first_message:
        return "content"
    return "content_block"


def _extract_bot_id_from_mapping(mapping: dict | None) -> str:
    if not isinstance(mapping, dict):
        return ""
    session_updates = mapping.get("session_updates")
    if isinstance(session_updates, dict):
        return _non_empty_text(session_updates.get("bot_id"))
    return ""


def _non_empty_text(value) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()


def first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _pick_first_text(*candidates: tuple[object, str]) -> tuple[str, str]:
    for value, source in candidates:
        text = _non_empty_text(value)
        if text:
            return text, source
    return "", ""


def _login_header_is_true(value: object) -> bool:
    return str(value or "").strip() in {"1", "true", "True"}


def _is_usable_chat_referer(value: object) -> bool:
    text = _non_empty_text(value)
    if not text:
        return False
    try:
        parsed = urllib.parse.urlparse(text)
    except Exception:
        return False
    if parsed.scheme != "https" or "doubao.com" not in (parsed.netloc or ""):
        return False
    if not str(parsed.path or "").startswith("/chat"):
        return False
    lowered = text.lower()
    if any(flag in lowered for flag in ("/security/", "/passport/", "/login", "/signup", "/register")):
        return False
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if "from_logout" in query:
        return False
    return True


def _pick_first_usable_chat_referer(*candidates: tuple[object, str]) -> tuple[str, str]:
    for value, source in candidates:
        text = _non_empty_text(value)
        if text and _is_usable_chat_referer(text):
            return text, source
    return "", ""


def _extract_conversation_id_from_chat_referer(value: object) -> str:
    text = _non_empty_text(value)
    if not text or not _is_usable_chat_referer(text):
        return ""
    try:
        parsed = urllib.parse.urlparse(text)
    except Exception:
        return ""
    path = str(parsed.path or "").rstrip("/")
    if path == "/chat":
        return ""
    prefix = "/chat/"
    if not path.startswith(prefix):
        return ""
    return _normalize_runtime_conversation_id(path[len(prefix):])


def resolve_effective_chat_referer(
    requested_conversation_id: str | None,
    manual_overrides: dict[str, str] | None,
    live_manual_url: str | None,
    request_mode: str | None = None,
) -> str:
    manual_overrides = manual_overrides if isinstance(manual_overrides, dict) else {}
    referer, _ = _pick_first_usable_chat_referer(
        (manual_overrides.get("referer"), "manual_overrides.referer"),
        (live_manual_url, "browser_runtime.status.manual_current_url"),
    )
    if requested_conversation_id:
        if _extract_conversation_id_from_chat_referer(referer) == requested_conversation_id:
            return referer
        return f"https://www.doubao.com/chat/{requested_conversation_id}"
    if str(request_mode or "").strip().lower() == "new":
        if referer and not _extract_conversation_id_from_chat_referer(referer):
            return referer
        return "https://www.doubao.com/chat/"
    if referer:
        return referer
    return "https://www.doubao.com/chat/"


def _latest_dict_item(items: list[dict], predicate) -> dict | None:
    candidates = [item for item in items if isinstance(item, dict) and predicate(item)]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: int(item.get("captured_at_ms") or item.get("at") or 0),
    )


def _latest_capture_for_stage(items: list[dict], stage: str) -> dict | None:
    return _latest_dict_item(items, lambda item: item.get("key") == stage)


def _latest_request_call_for_stage(items: list[dict], stage: str) -> dict | None:
    marker = BROWSER_CAPTURE_REQUEST_MARKERS.get(stage)
    if not marker:
        return None
    return _latest_dict_item(
        items,
        lambda item: marker in str(item.get("url") or "") or marker in str(item.get("href") or ""),
    )


def _extract_first_regex_group(patterns: tuple[str, ...], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE | re.DOTALL)
        if match:
            value = _non_empty_text(match.group(1))
            if value:
                return value
    return ""


def _extract_chat_completion_ids(response_text: str) -> dict[str, str]:
    text = str(response_text or "")
    return {
        "conversation_id": _extract_first_regex_group(
            (
                r'"conversation_id"\s*:\s*"([^"]+)"',
                r'\\"conversation_id\\"\s*:\s*\\"([^\\"]+)\\"',
            ),
            text,
        ),
        "message_id": _extract_first_regex_group(
            (
                r'"message_id"\s*:\s*"([^"]+)"',
                r'\\"message_id\\"\s*:\s*\\"([^\\"]+)\\"',
                r'"msgid"\s*:\s*"([^"]+)"',
                r'\\"msgid\\"\s*:\s*\\"([^\\"]+)\\"',
            ),
            text,
        ),
        "section_id": _extract_first_regex_group(
            (
                r'"section_id"\s*:\s*"([^"]+)"',
                r'\\"section_id\\"\s*:\s*\\"([^\\"]+)\\"',
            ),
            text,
        ),
    }


def _summarize_request_call(entry: dict | None, stage: str) -> dict | None:
    if not isinstance(entry, dict):
        return None
    url = str(entry.get("url") or "")
    href = str(entry.get("href") or "")
    body_text = _non_empty_text(entry.get("body"))
    summary = {
        "stage": stage,
        "kind": _non_empty_text(entry.get("kind")),
        "url": url,
        "href": href,
        "method": _non_empty_text(entry.get("method")) or "GET",
        "at": int(entry.get("at") or 0),
        "headers": _filtered_header_map(entry.get("headers"), REQUEST_HEADER_DEBUG_KEYS),
    }
    stack = _non_empty_text(entry.get("stack"))
    if stack:
        summary["stack_preview"] = stack.splitlines()[0][:240]
    query_params = parse_query_params_from_url(url or href)
    if query_params:
        summary["query_params"] = query_params
    href_parts = [part for part in urllib.parse.urlparse(href).path.split("/") if part]
    if len(href_parts) >= 2 and href_parts[0] == "chat":
        conversation_id = href_parts[1].strip()
        if conversation_id and not conversation_id.startswith("local_"):
            summary["conversation_id"] = conversation_id
    if stage == "chat_completion":
        request_meta = _extract_chat_request_meta(body_text)
        if request_meta:
            summary["request_meta"] = request_meta
    elif stage == "get_nexpulse":
        request_meta = _extract_nexpulse_request_meta(body_text)
        if request_meta:
            summary["request_meta"] = request_meta
    elif stage == "chain_single":
        chain_meta = _extract_chain_single_meta(body_text)
        if chain_meta:
            summary["request_meta"] = chain_meta
    return summary


def _summarize_capture_entry(entry: dict | None, stage: str) -> dict | None:
    if not isinstance(entry, dict):
        return None
    url = str(entry.get("url") or "")
    request_headers = entry.get("request_headers")
    response_headers = entry.get("response_headers")
    if not isinstance(request_headers, dict):
        request_headers = {}
    if not isinstance(response_headers, dict):
        response_headers = {}

    request_post_data = _non_empty_text(entry.get("request_post_data"))
    summary: dict[str, Any] = {
        "stage": stage,
        "key": _non_empty_text(entry.get("key")) or stage,
        "source": _non_empty_text(entry.get("source")),
        "url": url,
        "method": _non_empty_text(entry.get("method")) or "GET",
        "captured_at_ms": int(entry.get("captured_at_ms") or 0),
        "request_headers": _filtered_header_map(request_headers, REQUEST_HEADER_DEBUG_KEYS),
        "response_headers": _filtered_header_map(response_headers, RESPONSE_HEADER_DEBUG_KEYS),
    }
    query_params = parse_query_params_from_url(url)
    if query_params:
        summary["query_params"] = query_params

    response_text = str(entry.get("response_text") or "")
    if response_text:
        summary["response_text_preview"] = response_text[:240]

    derived: dict[str, Any] = {}
    if stage == "chain_single":
        request_meta = _extract_chain_single_meta(request_post_data)
        if request_meta:
            derived["request_meta"] = request_meta
        parsed = decode_jsonish(response_text, max_depth=5)
        if isinstance(parsed, dict):
            derived["local_message_id"] = _non_empty_text(parsed.get("local_message_id"))
            suggest_questions = parsed.get("suggest_questions")
            if isinstance(suggest_questions, list):
                derived["suggest_questions_count"] = len(suggest_questions)
                derived["has_suggest_questions"] = bool(suggest_questions)
    elif stage == "alice_profile_self":
        parsed = decode_jsonish(response_text, max_depth=6)
        if isinstance(parsed, dict):
            data = decode_jsonish(parsed.get("data"), max_depth=4)
            if isinstance(data, dict):
                profile_brief = decode_jsonish(data.get("profile_brief"), max_depth=4)
                if isinstance(profile_brief, dict):
                    derived["profile_brief"] = {
                        key: profile_brief.get(key)
                        for key in ("id", "entity_id", "nickname", "user_name", "user_type")
                        if profile_brief.get(key) not in (None, "")
                    }
                profile_bot_info = decode_jsonish(data.get("profile_bot_info"), max_depth=4)
                if isinstance(profile_bot_info, dict):
                    derived["profile_bot_info"] = {
                        key: profile_bot_info.get(key)
                        for key in ("total",)
                        if profile_bot_info.get(key) not in (None, "")
                    }
    elif stage == "alice_user_launch":
        parsed = decode_jsonish(response_text, max_depth=6)
        if isinstance(parsed, dict):
            data = decode_jsonish(parsed.get("data"), max_depth=4)
            if isinstance(data, dict):
                config = decode_jsonish(data.get("config"), max_depth=4)
                if isinstance(config, dict):
                    derived["config"] = {
                        key: config.get(key)
                        for key in (
                            "message_service_id",
                            "ws_app_key",
                            "ws_access_key",
                            "ws_app_id",
                            "ws_product_id",
                            "ws_domain",
                            "im_service_id",
                            "web_id",
                            "ttwid",
                            "action_service_id",
                        )
                        if config.get(key) not in (None, "")
                    }
                    derived["has_audio_token"] = bool(config.get("audio_token"))
                    derived["has_call_token"] = bool(config.get("call_token"))
                    derived["has_enterprise_audio_key"] = bool(config.get("enterprise_audio_app_key"))
                    derived["has_enterprise_call_key"] = bool(config.get("enterprise_call_app_key"))
    elif stage == "get_nexpulse":
        request_meta = _extract_nexpulse_request_meta(request_post_data)
        if request_meta:
            derived["request_meta"] = request_meta
        parsed = decode_jsonish(response_text, max_depth=5)
        if isinstance(parsed, dict):
            for key in ("code", "status_code", "status_desc", "message"):
                value = parsed.get(key)
                if value not in (None, ""):
                    derived[key] = value
            data = decode_jsonish(parsed.get("data"), max_depth=4)
            if isinstance(data, dict):
                derived["has_data"] = True
    elif stage == "send_rate_limit":
        parsed = decode_jsonish(response_text, max_depth=5)
        request_meta = _decode_json_text(request_post_data)
        if isinstance(request_meta, dict):
            derived["request_meta"] = {
                key: request_meta.get(key)
                for key in ("cmd", "sequence_id", "channel", "version")
                if request_meta.get(key) not in (None, "")
            }
        if isinstance(parsed, dict):
            derived["status_code"] = parsed.get("status_code")
            derived["status_desc"] = parsed.get("status_desc")
            downlink = decode_jsonish(parsed.get("downlink_body"), max_depth=4)
            if isinstance(downlink, dict):
                rate_info = decode_jsonish(
                    downlink.get("check_message_send_rate_limit_downlink_body"),
                    max_depth=4,
                )
                if isinstance(rate_info, dict):
                    derived["rate_limit"] = {
                        key: rate_info.get(key)
                        for key in ("is_limit", "limit_time", "limit_tips")
                        if rate_info.get(key) not in (None, "")
                    }
    elif stage == "chat_completion":
        request_meta = _extract_chat_request_meta(request_post_data)
        if request_meta:
            derived["request_meta"] = request_meta
        derived["has_stream_error"] = "event: STREAM_ERROR" in response_text or '"error_code":' in response_text
        derived["has_done"] = "[DONE]" in response_text
        extracted_ids = _extract_chat_completion_ids(response_text)
        if any(extracted_ids.values()):
            derived["ids"] = {key: value for key, value in extracted_ids.items() if value}

    if response_headers.get("x-ms-token"):
        derived["ms_token_from_response"] = response_headers.get("x-ms-token")
    if request_headers.get("x-flow-trace"):
        derived["x_flow_trace"] = request_headers.get("x-flow-trace")

    if derived:
        summary["derived"] = derived
    return summary


def _build_capture_mode_mapping(
    mode: str,
    capture_items: list[dict],
    request_calls: list[dict],
    runtime_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_chat_capture = _latest_capture_for_stage(capture_items, "chat_completion") or {}
    raw_chat_request = _latest_request_call_for_stage(request_calls, "chat_completion") or {}
    capture_summaries: dict[str, Any] = {}
    request_call_summaries: dict[str, Any] = {}
    for stage in BROWSER_CAPTURE_STAGE_KEYS:
        capture = _latest_capture_for_stage(capture_items, stage)
        if capture:
            capture_summaries[stage] = _summarize_capture_entry(capture, stage)
        request_call = _latest_request_call_for_stage(request_calls, stage)
        if request_call:
            request_call_summaries[stage] = _summarize_request_call(request_call, stage)

    selected_stage = ""
    for stage in BROWSER_CAPTURE_STAGE_PRIORITY:
        if capture_summaries.get(stage) or request_call_summaries.get(stage):
            selected_stage = stage
            break
    if not selected_stage:
        selected_stage = BROWSER_CAPTURE_STAGE_PRIORITY[-1]

    chat_capture = capture_summaries.get("chat_completion") or {}
    chat_request = request_call_summaries.get("chat_completion") or {}
    nexpulse_capture = capture_summaries.get("get_nexpulse") or {}
    nexpulse_request = request_call_summaries.get("get_nexpulse") or {}
    send_capture = capture_summaries.get("send_rate_limit") or {}
    send_request = request_call_summaries.get("send_rate_limit") or {}
    launch_capture = capture_summaries.get("alice_user_launch") or {}
    launch_request = request_call_summaries.get("alice_user_launch") or {}
    profile_capture = capture_summaries.get("alice_profile_self") or {}
    profile_request = request_call_summaries.get("alice_profile_self") or {}
    chain_capture = capture_summaries.get("chain_single") or {}
    chain_request = request_call_summaries.get("chain_single") or {}

    chat_query = parse_query_params_from_url(chat_capture.get("url") or chat_request.get("url") or "")
    nexpulse_query = parse_query_params_from_url(nexpulse_capture.get("url") or nexpulse_request.get("url") or "")
    send_query = parse_query_params_from_url(send_capture.get("url") or send_request.get("url") or "")
    launch_query = parse_query_params_from_url(launch_capture.get("url") or launch_request.get("url") or "")
    profile_query = parse_query_params_from_url(profile_capture.get("url") or profile_request.get("url") or "")
    chain_query = parse_query_params_from_url(chain_capture.get("url") or chain_request.get("url") or "")
    chat_request_headers = chat_capture.get("request_headers") if isinstance(chat_capture.get("request_headers"), dict) else {}
    chat_request_headers = dict(chat_request_headers)
    if isinstance(chat_request.get("headers"), dict):
        chat_request_headers = {**chat_request_headers, **chat_request["headers"]}

    request_overrides: dict[str, str] = {}
    request_override_sources: dict[str, str] = {}
    referer, referer_source = _pick_first_usable_chat_referer(
        (chat_request_headers.get("referer"), "chat_completion.request_headers.referer"),
        (chat_capture.get("request_headers", {}).get("referer"), "chat_completion.capture.request_headers.referer"),
        (chat_request.get("href"), "chat_completion.request_call.href"),
        (nexpulse_capture.get("request_headers", {}).get("referer"), "get_nexpulse.capture.request_headers.referer"),
        (nexpulse_request.get("href"), "get_nexpulse.request_call.href"),
        (send_capture.get("request_headers", {}).get("referer"), "send_rate_limit.capture.request_headers.referer"),
        (send_request.get("href"), "send_rate_limit.request_call.href"),
        (runtime_status.get("manual_current_url") if runtime_status else "", "browser_runtime.status.manual_current_url"),
    )
    if referer:
        request_overrides["referer"] = referer
        request_override_sources["referer"] = referer_source

    x_flow_trace, trace_source = _pick_first_text(
        (chat_request_headers.get("x-flow-trace"), "chat_completion.request_headers.x-flow-trace"),
        (chat_capture.get("request_headers", {}).get("x-flow-trace"), "chat_completion.capture.request_headers.x-flow-trace"),
        (send_request.get("headers", {}).get("x-flow-trace"), "send_rate_limit.request_call.headers.x-flow-trace"),
        (send_capture.get("request_headers", {}).get("x-flow-trace"), "send_rate_limit.capture.request_headers.x-flow-trace"),
    )
    if x_flow_trace:
        request_overrides["x_flow_trace"] = x_flow_trace
        request_override_sources["x_flow_trace"] = trace_source

    query_hints: dict[str, Any] = {}
    query_hint_sources: dict[str, str] = {}
    chat_query_items = parse_query_items_from_url(chat_capture.get("url") or chat_request.get("url") or "")
    if chat_query_items:
        ordered_keys = [key for key, _ in chat_query_items if key]
        query_hints["ordered_keys"] = ordered_keys
        query_hint_sources["ordered_keys"] = "chat_completion.url"
        query_map = {key: value for key, value in chat_query_items if key}
        query_hints["has_ms_token"] = "msToken" in query_map
        query_hint_sources["has_ms_token"] = "chat_completion.url"
        captured_a_bogus = _non_empty_text(query_map.get("a_bogus"))
        if captured_a_bogus:
            query_hints["captured_a_bogus"] = captured_a_bogus
            query_hint_sources["captured_a_bogus"] = "chat_completion.url"
        query_hints["captured_unsigned_query"] = build_unsigned_query_from_items(chat_query_items)
        query_hint_sources["captured_unsigned_query"] = "chat_completion.url"
    else:
        query_hints["ordered_keys"] = []
        query_hints["has_ms_token"] = False
        query_hints["captured_a_bogus"] = ""
        query_hints["captured_unsigned_query"] = ""
        query_hint_sources["ordered_keys"] = "none"
        query_hint_sources["has_ms_token"] = "none"
        query_hint_sources["captured_a_bogus"] = "none"
        query_hint_sources["captured_unsigned_query"] = "none"

    body_hints: dict[str, Any] = {}
    body_hint_sources: dict[str, str] = {}
    for template_value, source_name in (
        (
            _extract_chat_request_body_template(raw_chat_capture.get("request_post_data")),
            "chat_completion.capture.request_post_data",
        ),
        (
            _extract_chat_request_body_template(raw_chat_request.get("body")),
            "chat_completion.request_call.body",
        ),
    ):
        if not isinstance(template_value, dict):
            continue
        body_hints["template"] = template_value
        body_hints["source"] = source_name
        body_hint_sources["template"] = source_name
        body_hint_sources["source"] = source_name
        body_hints["message_shape"] = _detect_chat_request_message_shape(template_value)
        body_hint_sources["message_shape"] = source_name
        break

    session_updates: dict[str, str] = {}
    session_update_sources: dict[str, str] = {}

    def _set_session_update(field_name: str, value: object, source: str):
        text = _non_empty_text(value)
        if not text:
            return
        session_updates[field_name] = text
        session_update_sources[field_name] = source

    query_sources = (
        ("chat_completion", chat_query),
        ("get_nexpulse", nexpulse_query),
        ("send_rate_limit", send_query),
        ("alice_user_launch", launch_query),
        ("alice_profile_self", profile_query),
        ("chain_single", chain_query),
    )
    for field_name in ("aid", "real_aid", "device_id", "web_id", "tea_uuid", "web_tab_id", "fp"):
        for source_name, query_map in query_sources:
            if query_map.get(field_name):
                _set_session_update(field_name, query_map.get(field_name), f"{source_name}.url.{field_name}")
                break
    _set_session_update("a_bogus", chat_query.get("a_bogus"), "chat_completion.url.a_bogus")

    _set_session_update(
        "bot_id",
        chat_capture.get("derived", {}).get("request_meta", {}).get("bot_id"),
        "chat_completion.capture.derived.request_meta.bot_id",
    )
    _set_session_update(
        "bot_id",
        chat_request.get("request_meta", {}).get("bot_id"),
        "chat_completion.request_call.request_meta.bot_id",
    )
    _set_session_update(
        "bot_id",
        nexpulse_capture.get("derived", {}).get("request_meta", {}).get("bot_id"),
        "get_nexpulse.capture.derived.request_meta.bot_id",
    )
    _set_session_update(
        "bot_id",
        nexpulse_request.get("request_meta", {}).get("bot_id"),
        "get_nexpulse.request_call.request_meta.bot_id",
    )

    _set_session_update("fp", chat_query.get("fp"), "chat_completion.url.fp")
    _set_session_update("ms_token", chat_query.get("msToken"), "chat_completion.url.msToken")
    _set_session_update("ms_token", nexpulse_query.get("msToken"), "get_nexpulse.url.msToken")
    _set_session_update("ms_token", send_query.get("msToken"), "send_rate_limit.url.msToken")
    _set_session_update("ms_token", launch_query.get("msToken"), "alice_user_launch.url.msToken")
    _set_session_update("ms_token", profile_query.get("msToken"), "alice_profile_self.url.msToken")
    if _login_header_is_true(chat_capture.get("response_headers", {}).get("x-tt-agw-login")):
        _set_session_update("ms_token", chat_capture.get("derived", {}).get("ms_token_from_response"), "chat_completion.capture.response_headers.x-ms-token")
    if _login_header_is_true(send_capture.get("response_headers", {}).get("x-tt-agw-login")):
        _set_session_update("ms_token", send_capture.get("derived", {}).get("ms_token_from_response"), "send_rate_limit.capture.response_headers.x-ms-token")
    if _login_header_is_true(launch_capture.get("response_headers", {}).get("x-tt-agw-login")):
        _set_session_update("ms_token", launch_capture.get("derived", {}).get("ms_token_from_response"), "alice_user_launch.capture.response_headers.x-ms-token")

    extracted_ids = chat_capture.get("derived", {}).get("ids") if isinstance(chat_capture.get("derived"), dict) else {}
    if not isinstance(extracted_ids, dict):
        extracted_ids = {}
    chat_conversation_id = _non_empty_text(extracted_ids.get("conversation_id"))
    if not chat_conversation_id:
        chat_conversation_id, _ = _pick_first_text(
            (chat_request.get("conversation_id"), "chat_completion.request_call.conversation_id"),
            (chat_request.get("href_conversation_id"), "chat_completion.request_call.href_conversation_id"),
        )
    if chat_conversation_id and not chat_conversation_id.startswith("local_"):
        session_updates["room_id"] = chat_conversation_id
        session_update_sources["room_id"] = "chat_completion.response_text.conversation_id"
    elif _non_empty_text(chat_request.get("conversation_id")):
        session_updates["room_id"] = _non_empty_text(chat_request.get("conversation_id"))
        session_update_sources["room_id"] = "chat_completion.request_call.conversation_id"

    selected_capture = capture_summaries.get(selected_stage)
    selected_request_call = request_call_summaries.get(selected_stage)

    notes: list[str] = []
    if selected_stage:
        notes.append(f"selected_stage={selected_stage}")
    if request_overrides.get("referer"):
        notes.append(f"referer_source={request_override_sources.get('referer', '')}")
    if request_overrides.get("x_flow_trace"):
        notes.append(f"x_flow_trace_source={request_override_sources.get('x_flow_trace', '')}")
    if query_hints.get("captured_a_bogus"):
        notes.append("captured_a_bogus comes from the latest chat_completion query.")
    if chat_conversation_id:
        notes.append("room_id was derived from the latest chat_completion conversation id.")

    return {
        "mode": mode,
        "captures": capture_summaries,
        "request_calls": request_call_summaries,
        "selected_capture_key": selected_stage if selected_capture else "",
        "selected_request_call_key": selected_stage if selected_request_call else "",
        "selected_capture": selected_capture,
        "selected_request_call": selected_request_call,
        "request_overrides": request_overrides,
        "request_override_sources": request_override_sources,
        "query_hints": query_hints,
        "query_hint_sources": query_hint_sources,
        "body_hints": body_hints,
        "body_hint_sources": body_hint_sources,
        "session_updates": session_updates,
        "session_update_sources": session_update_sources,
        "notes": notes,
    }


def sanitize_request_cookie_header(cookie_header: str) -> str:
    cookie_map = parse_cookie_header(cookie_header)
    cookie_map.pop("msToken", None)
    return build_cookie_header(cookie_map)


def get_session_fp(session) -> str:
    if getattr(session, "fp", None):
        return str(session.fp)
    return parse_cookie_header(session.cookie).get("s_v_web_id", "")


def get_session_web_tab_id(session) -> str:
    current = getattr(session, "web_tab_id", None)
    if current:
        return str(current)
    session_hash = hashlib.md5(
        f"{session.aid}|{session.device_id}|{session.web_id}".encode("utf-8")
    ).hexdigest()
    session.web_tab_id = (
        f"{session_hash[:8]}-{session_hash[8:12]}-{session_hash[12:16]}-"
        f"{session_hash[16:20]}-{session_hash[20:32]}"
    )
    return str(session.web_tab_id)


def resolve_session_bot_id(session) -> str:
    current_bot_id = _non_empty_text(getattr(session, "bot_id", None))
    if current_bot_id:
        return current_bot_id

    preferred_mapping = get_preferred_browser_capture_mapping(
        runtime_status=browser_runtime.get_status(),
        session=session,
    )
    mapped_bot_id = _extract_bot_id_from_mapping(preferred_mapping)
    if mapped_bot_id:
        session.bot_id = mapped_bot_id
        return mapped_bot_id

    return LEGACY_FALLBACK_BOT_ID


def extract_text_value(value) -> str:
    decoded = decode_jsonish(value, max_depth=6)
    if isinstance(decoded, str):
        return decoded
    if isinstance(decoded, list):
        return "".join(part for part in (extract_text_value(item) for item in decoded) if part)
    if not isinstance(decoded, dict):
        return ""
    for key in ("tts_content", "answer", "text", "content", "delta", "output_text", "output"):
        if key not in decoded:
            continue
        text = extract_text_value(decoded.get(key))
        if text:
            return text
    if decoded.get("message"):
        return extract_text_value(decoded.get("message"))
    if decoded.get("event_data"):
        return extract_text_value(decoded.get("event_data"))
    return ""


def extract_message_payload_text(event_data: dict, message: dict) -> str:
    if not isinstance(message, dict):
        decoded_message = decode_jsonish(message, max_depth=6)
        message = decoded_message if isinstance(decoded_message, dict) else {}
    for key in ("tts_content", "answer", "output_text", "text"):
        text = extract_text_value(event_data.get(key))
        if text:
            return text
    if message_text := extract_history_message_text(message):
        return message_text
    return extract_text_value(message)


def extract_event_identifiers(event_data: dict, message: dict | None = None) -> tuple[str | None, str | None, str | None]:
    message = message if isinstance(message, dict) else {}

    conversation_id = first_non_empty(
        event_data.get("conversation_id"),
        message.get("conversation_id"),
    )
    message_id = first_non_empty(
        event_data.get("message_id"),
        message.get("message_id"),
        message.get("chat_id"),
    )
    section_id = first_non_empty(
        event_data.get("section_id"),
        message.get("section_id"),
    )
    return conversation_id, message_id, section_id


def build_im_params(session) -> str:
    return "&".join([
        "version_code=20800",
        "language=zh",
        "device_platform=web",
        f"aid={session.aid}",
        f"real_aid={session.resolved_real_aid}",
        "pkg_type=release_version",
        f"device_id={session.device_id}",
        f"pc_version={PC_VERSION}",
        f"web_id={session.web_id}",
        f"tea_uuid={session.tea_uuid}",
        "region=",
        "sys_region=",
        "samantha_web=1",
        "use-olympus-account=1",
        f"web_tab_id={get_session_web_tab_id(session)}",
    ])


def build_im_headers(session, referer: str) -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": ACCEPT_LANGUAGE,
        "content-type": "application/json; encoding=utf-8",
        "agw-js-conv": "str",
        "cookie": sanitize_request_cookie_header(session.cookie),
        "origin": "https://www.doubao.com",
        "referer": referer,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": BROWSER_UA,
    }


def build_get_nexpulse_headers(session, referer: str) -> dict[str, str]:
    headers = build_im_headers(session, referer)
    headers["content-type"] = "application/json"
    return headers


def build_get_nexpulse_body(session) -> dict:
    return {
        "bot_id": resolve_session_bot_id(session),
        "scene": 4,
    }


async def request_get_nexpulse(
    session,
    referer: str,
    use_browser_transport: bool = False,
    prefer_manual: bool = True,
    seed_manual_storage: bool = True,
) -> dict:
    url = f"https://www.doubao.com/biz/onboarding/get_nexpulse?{build_im_params(session)}"
    headers = build_get_nexpulse_headers(session, referer)
    body = build_get_nexpulse_body(session)

    if use_browser_transport:
        browser_headers = build_browser_fetch_headers(headers)
        response = await browser_runtime.fetch_text(
            session,
            url=url,
            headers=browser_headers,
            body=body,
            referer=referer,
            prefer_manual=prefer_manual,
            seed_manual_storage=seed_manual_storage,
        )
        response_status = int(response.get("status") or 0)
        text = str(response.get("text") or "")
        if response_status != 200:
            raise HTTPException(
                status_code=response_status or 500,
                detail=f"Doubao get_nexpulse failed: {text}",
            )
        return {
            "url": url,
            "headers": browser_headers,
            "body": body,
            "response": decode_jsonish(text, max_depth=3),
            "transport": "browser",
        }

    async with aiohttp.ClientSession() as aio_session:
        async with aio_session.post(url=url, headers=headers, json=body) as response:
            text = await response.text()
            if x_ms_token := response.headers.get("x-ms-token"):
                session.ms_token = x_ms_token
            if response.status != 200:
                raise HTTPException(
                    status_code=response.status,
                    detail=f"Doubao get_nexpulse failed: {text}",
                )
            return {
                "url": url,
                "headers": headers,
                "body": body,
                "response": decode_jsonish(text, max_depth=3),
            }


def build_send_rate_limit_body() -> dict:
    return {
        "cmd": 2260,
        "uplink_body": {
            "check_message_send_rate_limit_uplink_body": {}
        },
        "sequence_id": str(uuid.uuid4()),
        "channel": 2,
        "version": "1",
    }


async def check_message_send_rate_limit(
    session,
    referer: str,
    use_browser_transport: bool = False,
    prefer_manual: bool = True,
    seed_manual_storage: bool = True,
) -> dict:
    url = f"https://www.doubao.com/im/message/send_rate_limit?{build_im_params(session)}"
    headers = build_im_headers(session, referer)
    body = build_send_rate_limit_body()

    if use_browser_transport:
        browser_headers = build_browser_fetch_headers(headers)
        response = await browser_runtime.fetch_text(
            session,
            url=url,
            headers=browser_headers,
            body=body,
            referer=referer,
            prefer_manual=prefer_manual,
            seed_manual_storage=seed_manual_storage,
        )
        response_status = int(response.get("status") or 0)
        text = str(response.get("text") or "")
        if response_status != 200:
            raise HTTPException(
                status_code=response_status or 500,
                detail=f"Doubao send_rate_limit failed: {text}",
            )
        payload = decode_jsonish(text, max_depth=3)
        if isinstance(payload, dict):
            status_code = payload.get("status_code")
            status_desc = payload.get("status_desc")
            if status_code not in (None, 0):
                raise HTTPException(
                    status_code=429,
                    detail=f"Doubao send_rate_limit rejected: {status_desc or 'unknown'} ({status_code})",
                )
            downlink = decode_jsonish(payload.get("downlink_body"), max_depth=3)
            if isinstance(downlink, dict):
                rate_info = decode_jsonish(
                    downlink.get("check_message_send_rate_limit_downlink_body"),
                    max_depth=3,
                )
                if isinstance(rate_info, dict) and rate_info.get("is_limit"):
                    limit_tips = str(rate_info.get("limit_tips") or "").strip() or "rate limited"
                    limit_time = rate_info.get("limit_time")
                    raise HTTPException(
                        status_code=429,
                        detail=f"Doubao send_rate_limit rejected: {limit_tips} ({limit_time})",
                    )
            return {
                "url": url,
                "headers": browser_headers,
                "body": body,
                "response": payload,
                "transport": "browser",
            }
        return {
            "url": url,
            "headers": browser_headers,
            "body": body,
            "response": text,
            "transport": "browser",
        }

    async with aiohttp.ClientSession() as aio_session:
        async with aio_session.post(url=url, headers=headers, json=body) as response:
            text = await response.text()
            if response.status != 200:
                raise HTTPException(
                    status_code=response.status,
                    detail=f"Doubao send_rate_limit failed: {text}",
                )

            payload = decode_jsonish(text, max_depth=3)
            if isinstance(payload, dict):
                status_code = payload.get("status_code")
                status_desc = payload.get("status_desc")
                if status_code not in (None, 0):
                    raise HTTPException(
                        status_code=429,
                        detail=f"Doubao send_rate_limit rejected: {status_desc or 'unknown'} ({status_code})",
                    )
                downlink = decode_jsonish(payload.get("downlink_body"), max_depth=3)
                if isinstance(downlink, dict):
                    rate_info = decode_jsonish(
                        downlink.get("check_message_send_rate_limit_downlink_body"),
                        max_depth=3,
                    )
                    if isinstance(rate_info, dict) and rate_info.get("is_limit"):
                        limit_tips = str(rate_info.get("limit_tips") or "").strip() or "rate limited"
                        limit_time = rate_info.get("limit_time")
                        raise HTTPException(
                            status_code=429,
                            detail=f"Doubao send_rate_limit rejected: {limit_tips} ({limit_time})",
                        )
                return {
                    "url": url,
                    "headers": headers,
                    "body": body,
                    "response": payload,
                }

            return {
                "url": url,
                "headers": headers,
                "body": body,
                "response": text,
            }


def build_web_chat_completion_params(
    session,
    include_signature: bool = True,
    query_hints: dict | None = None,
) -> str:
    params = {
        "aid": session.aid,
        "device_id": session.device_id,
        "device_platform": "web",
    }
    fp = get_session_fp(session)
    if fp:
        params["fp"] = fp
    params["language"] = "zh"
    params["pc_version"] = PC_VERSION
    params["pkg_type"] = "release_version"
    params["real_aid"] = session.resolved_real_aid
    params["region"] = ""
    params["samantha_web"] = "1"
    params["sys_region"] = ""
    params["tea_uuid"] = session.tea_uuid
    params["use-olympus-account"] = "1"
    params["version_code"] = "20800"
    params["web_id"] = session.web_id
    params["web_tab_id"] = get_session_web_tab_id(session)
    ordered_keys: list[str] = []
    if isinstance(query_hints, dict):
        hint_keys = query_hints.get("ordered_keys") or []
        if isinstance(hint_keys, list):
            ordered_keys = [str(key) for key in hint_keys if str(key).strip()]
    if getattr(session, "ms_token", None):
        params["msToken"] = session.ms_token
    if include_signature and getattr(session, "a_bogus", None):
        params["a_bogus"] = session.a_bogus

    if ordered_keys:
        ordered_items: list[tuple[str, str]] = []
        seen: set[str] = set()
        preferred_keys = list(ordered_keys)
        if include_signature and "a_bogus" in params and "a_bogus" not in preferred_keys:
            preferred_keys.append("a_bogus")
        for key in preferred_keys:
            if key in params:
                ordered_items.append((key, params[key]))
                seen.add(key)
        for key, value in params.items():
            if key not in seen:
                ordered_items.append((key, value))
        return urllib.parse.urlencode(ordered_items)

    return urllib.parse.urlencode(params)


def build_web_chat_completion_headers(
    session,
    referer: str,
    x_flow_trace: str | None = None,
) -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "accept-language": ACCEPT_LANGUAGE,
        "content-type": "application/json",
        "agw-js-conv": "str, str",
        "last-event-id": "undefined",
        "cookie": sanitize_request_cookie_header(session.cookie),
        "origin": "https://www.doubao.com",
        "referer": referer,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": BROWSER_UA,
    }
    trace_value = _non_empty_text(x_flow_trace)
    if trace_value:
        headers["x-flow-trace"] = trace_value
    return headers


def build_browser_fetch_headers(headers: dict[str, str] | None) -> dict[str, str]:
    allowed = {
        "accept",
        "content-type",
        "agw-js-conv",
        "last-event-id",
        "x-flow-trace",
    }
    safe_headers: dict[str, str] = {}
    if not isinstance(headers, dict):
        return safe_headers
    for key, value in headers.items():
        if value in (None, ""):
            continue
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        if normalized_key.lower() not in allowed:
            continue
        safe_headers[normalized_key] = str(value)
    return safe_headers


def get_curl_impersonate_profile() -> str:
    return os.getenv("DOUBAO_CURL_IMPERSONATE", "edge101")


def build_runtime_session_params(session: DoubaoSession) -> dict[str, str]:
    params = session.to_dict()
    params["room_id"] = _normalize_runtime_conversation_id(session.room_id) or "0"
    # Keep the persisted seed trace in session_params for guest-session snapshots
    # and compatibility, but chat sending still uses captured_x_flow_trace only.
    params.pop("guest_capture_cache", None)
    if getattr(session, "a_bogus", None):
        params["a_bogus"] = session.a_bogus
    return params


def persist_session_state(
    session: DoubaoSession,
    conversation_id: str | None = None,
    guest: bool = False,
) -> DoubaoSession:
    normalized_conversation_id = _normalize_runtime_conversation_id(conversation_id)
    normalized_room_id = _normalize_runtime_conversation_id(getattr(session, "room_id", None))
    if normalized_room_id:
        session.room_id = normalized_room_id
    elif normalized_conversation_id:
        session.room_id = normalized_conversation_id
    else:
        session.room_id = "0"
    persisted = (
        session_pool.upsert_guest_session(session)
        if guest
        else session_pool.upsert_auth_session(session)
    )
    if normalized_conversation_id:
        session_pool.set_session(normalized_conversation_id, persisted)
    normalized_persisted_room_id = _normalize_runtime_conversation_id(getattr(persisted, "room_id", None))
    if normalized_persisted_room_id:
        session_pool.set_session(normalized_persisted_room_id, persisted)
    session_pool.save_to_file(guest=guest)
    if guest:
        session_pool.refresh_guest_session_backup(persisted)
    return persisted


def get_cached_conversation_state(conversation_id: str | None) -> dict[str, Any]:
    return session_pool.get_conversation_state(conversation_id)


def cache_conversation_state(
    conversation_id: str | None,
    *,
    section_id: str | None = None,
    latest_index: str | int | None = None,
    override: bool = False,
) -> dict[str, Any]:
    normalized_conversation_id = _normalize_runtime_conversation_id(conversation_id)
    if not normalized_conversation_id:
        return {}
    return session_pool.update_conversation_state(
        normalized_conversation_id,
        section_id=section_id,
        latest_index=latest_index,
        override=override,
    )


def merge_conversation_info_with_cached_state(
    conversation_id: str | None,
    info: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(info or {})
    cached_state = get_cached_conversation_state(conversation_id)
    cached_section_id = _non_empty_text(cached_state.get("section_id"))
    cached_latest_index = cached_state.get("latest_index")

    if cached_section_id:
        merged["last_section_id"] = cached_section_id
    if cached_latest_index not in (None, ""):
        merged["latest_index"] = cached_latest_index

    return merged


def resolve_completion_identifiers(
    *,
    session,
    requested_conversation_id: str | None,
    response_conversation_id: str | None,
    requested_section_id: str | None,
    response_section_id: str | None,
    message_id: str | None,
    runtime_status: dict[str, Any] | None = None,
    request_mode: str | None = None,
    previous_room_id: str | None = None,
) -> tuple[str, str, str]:
    status = runtime_status or browser_runtime.get_status()
    normalized_request_mode = normalize_conversation_mode(request_mode)
    requested_conversation_text = _normalize_runtime_conversation_id(requested_conversation_id)
    previous_room_text = _normalize_runtime_conversation_id(previous_room_id)
    manual_conversation_id = _extract_conversation_id_from_chat_referer(
        status.get("manual_current_url")
    )
    session_room_id = _normalize_runtime_conversation_id(getattr(session, "room_id", None))
    response_conversation_text = _normalize_runtime_conversation_id(response_conversation_id)
    manual_candidate = ""
    session_room_candidate = ""

    if normalized_request_mode == CONVERSATION_MODE_NEW:
        if manual_conversation_id not in {"", "0", previous_room_text}:
            manual_candidate = manual_conversation_id
        if session_room_id not in {"", "0", previous_room_text}:
            session_room_candidate = session_room_id
        resolved_conversation_id = first_non_empty(
            response_conversation_text,
            manual_candidate,
            session_room_candidate,
        ) or "0"
    elif normalized_request_mode == CONVERSATION_MODE_CONTINUE:
        if manual_conversation_id == requested_conversation_text:
            manual_candidate = manual_conversation_id
        if session_room_id == requested_conversation_text:
            session_room_candidate = session_room_id
        resolved_conversation_id = first_non_empty(
            response_conversation_text,
            requested_conversation_text,
            manual_candidate,
            session_room_candidate,
        ) or "0"
    else:
        if manual_conversation_id and (
            manual_conversation_id == requested_conversation_text
            or manual_conversation_id != previous_room_text
        ):
            manual_candidate = manual_conversation_id
        if session_room_id and (
            session_room_id == requested_conversation_text
            or session_room_id != previous_room_text
        ):
            session_room_candidate = session_room_id
        resolved_conversation_id = first_non_empty(
            response_conversation_text,
            manual_candidate,
            requested_conversation_text,
            session_room_candidate,
        ) or "0"
    cached_state = get_cached_conversation_state(resolved_conversation_id)
    cached_section_id = _non_empty_text(cached_state.get("section_id"))
    resolved_section_id = first_non_empty(
        response_section_id,
        requested_section_id,
        cached_section_id,
    ) or "0"
    resolved_message_id = first_non_empty(message_id) or "0"
    return resolved_conversation_id, resolved_message_id, resolved_section_id


def build_completion_response_meta(
    *,
    request_mode: str | None,
    text: str | None,
    image_urls: list[str] | None,
    conversation_id: str | None,
) -> dict[str, Any]:
    normalized_request_mode = normalize_conversation_mode(request_mode)
    has_content = bool(_non_empty_text(text) or list(image_urls or []))
    conversation_created = (
        normalized_request_mode == CONVERSATION_MODE_NEW
        and _non_empty_text(conversation_id) not in {"", "0"}
    )
    follow_up_required = bool(conversation_created and not has_content)
    return {
        "request_mode": normalized_request_mode,
        "follow_up_required": follow_up_required,
        "follow_up_conversation_mode": (
            CONVERSATION_MODE_CONTINUE if follow_up_required else None
        ),
    }


def should_route_guest_request_via_live_manual_send(
    *,
    effective_guest: bool,
    request_mode: str | None,
    session,
    runtime_status: dict[str, Any] | None = None,
) -> bool:
    if not effective_guest or session is None:
        return False
    if normalize_conversation_mode(request_mode) != CONVERSATION_MODE_NEW:
        return False
    if not browser_runtime.is_manual_window_bound_to(session):
        return False
    runtime_status = runtime_status or browser_runtime.get_status()
    return can_bootstrap_capture_via_manual_browser(runtime_status)


def should_retry_auth_continuation_rate_limit(
    *,
    response_status: int,
    response_text: str,
    effective_guest: bool,
    conversation_id: str | None,
) -> bool:
    if effective_guest or conversation_id in (None, "", "0"):
        return False
    if int(response_status or 0) != 429:
        return False
    text = str(response_text or "")
    return (
        "710022002" in text
        or "710022004" in text
        or "Too Many Requests" in text
    )


def _generate_guest_runtime_id() -> str:
    return str((uuid.uuid4().int % 9_000_000_000_000_000_000) + 1_000_000_000_000_000_000)


def build_hidden_guest_seed_session() -> DoubaoSession:
    runtime_id = _generate_guest_runtime_id()
    return DoubaoSession(
        aid=DEFAULT_GUEST_AID,
        real_aid=DEFAULT_GUEST_REAL_AID,
        web_tab_id=None,
        bot_id=LEGACY_FALLBACK_BOT_ID,
        fp=None,
        ms_token=None,
        a_bogus=None,
        cookie="",
        device_id=runtime_id,
        tea_uuid=runtime_id,
        web_id=runtime_id,
        room_id="0",
        x_flow_trace=generate_x_flow_trace(),
    )


def should_start_fresh_guest_manual_verification(
    *,
    effective_guest: bool,
    session: DoubaoSession | None,
    requested_conversation_id: str | None = None,
    session_meta: dict[str, Any] | None = None,
) -> bool:
    if not effective_guest or session is None:
        return False

    if browser_runtime.is_manual_window_bound_to(session):
        return False

    meta = session_meta or {}
    if bool(meta.get("auto_guest_bootstrapped")):
        return True

    normalized_conversation_id = str(requested_conversation_id or "").strip()
    if normalized_conversation_id not in {"", "0"}:
        return False

    room_id = str(getattr(session, "room_id", "") or "").strip()
    if room_id in {"", "0"}:
        return True

    cookie = str(getattr(session, "cookie", "") or "").strip()
    return not cookie


def sync_session_from_browser_runtime_state(
    session,
    runtime_status: dict[str, Any] | None = None,
) -> bool:
    runtime_status = runtime_status or browser_runtime.get_status()
    changed = False
    fp = _non_empty_text(runtime_status.get("manual_fp")) or _non_empty_text(runtime_status.get("fp"))
    ms_token = _non_empty_text(runtime_status.get("manual_ms_token")) or _non_empty_text(runtime_status.get("ms_token"))
    if fp and getattr(session, "fp", None) != fp:
        session.fp = fp
        changed = True
    if ms_token and getattr(session, "ms_token", None) != ms_token:
        session.ms_token = ms_token
        changed = True
    return changed


async def ensure_service_session(
    *,
    requested_conversation_id: str | None,
    effective_guest: bool,
    session_override: dict | None = None,
) -> tuple[DoubaoSession, dict[str, bool]]:
    if session_override:
        return DoubaoSession.from_dict(session_override), {
            "from_override": True,
            "auto_guest_bootstrapped": False,
        }

    session = session_pool.get_session(requested_conversation_id, effective_guest)
    auto_guest_bootstrapped = False

    if session is None and effective_guest:
        session = build_hidden_guest_seed_session()
        try:
            await browser_runtime.ensure_ready(session, seed_manual_storage=False)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Hidden browser guest bootstrap failed: {exc}")
        sync_session_from_browser_runtime_state(session, browser_runtime.get_status())
        sync_session_from_manual_browser_capture(
            session,
            session_mode=SESSION_MODE_GUEST,
            guest=True,
        )
        sync_session_from_browser_runtime_state(session, browser_runtime.get_status())
        session = persist_session_state(
            session,
            requested_conversation_id,
            guest=True,
        )
        auto_guest_bootstrapped = True

    if not session:
        raise HTTPException(status_code=404, detail="No available session config")

    if requested_conversation_id:
        session_pool.set_session(str(requested_conversation_id), session)

    return session, {
        "from_override": False,
        "auto_guest_bootstrapped": auto_guest_bootstrapped,
    }


async def refresh_session_runtime(
    guest: bool = False,
    session_mode: str | None = None,
    conversation_id: str | None = None,
    session_override: dict | None = None,
    persist_session: bool = False,
) -> dict[str, str]:
    effective_guest = resolve_effective_guest_flag(guest, session_mode=session_mode)
    requested_conversation_id = None if conversation_id in (None, "", "0") else conversation_id

    session, session_meta = await ensure_service_session(
        requested_conversation_id=requested_conversation_id,
        effective_guest=effective_guest,
        session_override=session_override,
    )
    should_persist = not session_meta.get("from_override")

    previous_ms_token = getattr(session, "ms_token", None)
    previous_fp = getattr(session, "fp", None)
    previous_web_tab_id = getattr(session, "web_tab_id", None)
    runtime_status_before_ready = browser_runtime.get_status()
    guest_manual_seed = effective_guest and should_seed_guest_runtime_from_manual(runtime_status_before_ready)
    seed_manual_storage = (not effective_guest) or guest_manual_seed
    await browser_runtime.ensure_ready(session, seed_manual_storage=seed_manual_storage)
    manual_live_synced = False
    if not effective_guest or guest_manual_seed:
        manual_live_synced = await browser_runtime.sync_live_manual_session(session)
    manual_capture_synced = sync_session_from_manual_browser_capture(
        session,
        session_mode=session_mode,
        guest=effective_guest,
    )
    runtime_status = browser_runtime.get_status()
    sync_session_from_browser_runtime_state(session, runtime_status)
    changed = (
        getattr(session, "ms_token", None) != previous_ms_token
        or getattr(session, "fp", None) != previous_fp
        or getattr(session, "web_tab_id", None) != previous_web_tab_id
        or manual_live_synced
        or manual_capture_synced
    )

    if requested_conversation_id:
        session_pool.set_session(requested_conversation_id, session)
    if persist_session and should_persist:
        persist_session_state(
            session,
            requested_conversation_id,
            guest=effective_guest,
        )
    elif changed and should_persist and not effective_guest:
        session_pool.save_to_file(guest=False)

    return build_runtime_session_params(session)


def build_browser_capture_mappings(runtime_status: dict[str, Any] | None = None) -> dict[str, Any]:
    captures = browser_runtime.get_captured_requests()
    return {
        "manual": _build_capture_mode_mapping(
            "manual",
            captures.get("manual") or [],
            captures.get("manual_request_calls") or [],
            runtime_status=runtime_status,
        ),
        "hidden": _build_capture_mode_mapping(
            "hidden",
            captures.get("hidden") or [],
            captures.get("hidden_request_calls") or [],
            runtime_status=runtime_status,
        ),
    }


def _mapping_has_signal(mapping: dict | None) -> bool:
    if not isinstance(mapping, dict):
        return False
    return bool(
        mapping.get("selected_capture")
        or mapping.get("selected_request_call")
        or mapping.get("session_updates")
        or mapping.get("request_overrides")
        or mapping.get("query_hints")
        or mapping.get("body_hints")
    )


def _mapping_has_stage_signal(mapping: dict | None, stages: tuple[str, ...]) -> bool:
    if not isinstance(mapping, dict):
        return False
    captures = mapping.get("captures")
    request_calls = mapping.get("request_calls")
    if not isinstance(captures, dict):
        captures = {}
    if not isinstance(request_calls, dict):
        request_calls = {}
    for stage in stages:
        if captures.get(stage) or request_calls.get(stage):
            return True
    return False


def _mapping_has_reusable_guest_chat_signal(mapping: dict | None) -> bool:
    if not isinstance(mapping, dict):
        return False
    if _mapping_has_stage_signal(mapping, ("chat_completion",)):
        return True
    body_hints = mapping.get("body_hints")
    request_overrides = mapping.get("request_overrides")
    if not isinstance(body_hints, dict) or not isinstance(request_overrides, dict):
        return False
    if not isinstance(body_hints.get("template"), dict):
        return False
    return bool(_non_empty_text(request_overrides.get("x_flow_trace")))


def _build_persistable_guest_capture_mapping(mapping: dict | None) -> dict[str, Any]:
    if not isinstance(mapping, dict) or not _mapping_has_reusable_guest_chat_signal(mapping):
        return {}

    persisted_stages = {"chat_completion", "send_rate_limit", "get_nexpulse"}
    captures = mapping.get("captures")
    request_calls = mapping.get("request_calls")
    if not isinstance(captures, dict):
        captures = {}
    if not isinstance(request_calls, dict):
        request_calls = {}

    return {
        "mode": "persisted_guest",
        "generated_by": GENERATED_GUEST_CAPTURE_CACHE_SOURCE,
        "generated_at_ms": int(time.time() * 1000),
        "captures": {
            key: _deep_clone_json_value(value)
            for key, value in captures.items()
            if key in persisted_stages and isinstance(value, dict)
        },
        "request_calls": {
            key: _deep_clone_json_value(value)
            for key, value in request_calls.items()
            if key in persisted_stages and isinstance(value, dict)
        },
        "selected_capture_key": str(mapping.get("selected_capture_key") or ""),
        "selected_request_call_key": str(mapping.get("selected_request_call_key") or ""),
        "selected_capture": _deep_clone_json_value(mapping.get("selected_capture"))
        if isinstance(mapping.get("selected_capture"), dict)
        else None,
        "selected_request_call": _deep_clone_json_value(mapping.get("selected_request_call"))
        if isinstance(mapping.get("selected_request_call"), dict)
        else None,
        "request_overrides": _deep_clone_json_value(mapping.get("request_overrides") or {}),
        "request_override_sources": _deep_clone_json_value(mapping.get("request_override_sources") or {}),
        "query_hints": _deep_clone_json_value(mapping.get("query_hints") or {}),
        "query_hint_sources": _deep_clone_json_value(mapping.get("query_hint_sources") or {}),
        "body_hints": _deep_clone_json_value(mapping.get("body_hints") or {}),
        "body_hint_sources": _deep_clone_json_value(mapping.get("body_hint_sources") or {}),
        "session_updates": _deep_clone_json_value(mapping.get("session_updates") or {}),
        "session_update_sources": _deep_clone_json_value(mapping.get("session_update_sources") or {}),
        "notes": list(mapping.get("notes") or []) + ["persisted_from_guest_capture_cache"],
    }


def _get_persisted_guest_capture_mapping() -> dict[str, Any]:
    session = session_pool.get_session(None, guest=True)
    if session is None:
        return {}
    cached_mapping = getattr(session, "guest_capture_cache", None)
    if (
        not isinstance(cached_mapping, dict)
        or str(cached_mapping.get("generated_by") or "").strip() != GENERATED_GUEST_CAPTURE_CACHE_SOURCE
        or not _mapping_has_reusable_guest_chat_signal(cached_mapping)
    ):
        return {}
    return _deep_clone_json_value(cached_mapping)


def _merge_capture_mappings(primary: dict | None, fallback: dict | None) -> dict[str, Any]:
    primary = primary if isinstance(primary, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    if not _mapping_has_signal(primary):
        return _deep_clone_json_value(fallback or {})
    if not _mapping_has_signal(fallback):
        return _deep_clone_json_value(primary or {})

    merged = _deep_clone_json_value(primary)

    for field_name in ("captures", "request_calls"):
        merged_field = _deep_clone_json_value(fallback.get(field_name) or {})
        if not isinstance(merged_field, dict):
            merged_field = {}
        primary_field = primary.get(field_name) or {}
        if isinstance(primary_field, dict):
            merged_field.update(_deep_clone_json_value(primary_field))
        merged[field_name] = merged_field

    for field_name in (
        "request_overrides",
        "request_override_sources",
        "query_hints",
        "query_hint_sources",
        "body_hints",
        "body_hint_sources",
        "session_updates",
        "session_update_sources",
    ):
        merged_field = _deep_clone_json_value(fallback.get(field_name) or {})
        if not isinstance(merged_field, dict):
            merged_field = {}
        primary_field = primary.get(field_name) or {}
        if isinstance(primary_field, dict):
            merged_field.update(_deep_clone_json_value(primary_field))
        merged[field_name] = merged_field

    if not merged.get("selected_capture") and isinstance(fallback.get("selected_capture"), dict):
        merged["selected_capture"] = _deep_clone_json_value(fallback.get("selected_capture"))
    if not merged.get("selected_request_call") and isinstance(fallback.get("selected_request_call"), dict):
        merged["selected_request_call"] = _deep_clone_json_value(fallback.get("selected_request_call"))
    if not merged.get("selected_capture_key"):
        merged["selected_capture_key"] = str(fallback.get("selected_capture_key") or "")
    if not merged.get("selected_request_call_key"):
        merged["selected_request_call_key"] = str(fallback.get("selected_request_call_key") or "")

    merged_notes = []
    for note in list(fallback.get("notes") or []) + list(primary.get("notes") or []):
        note_text = str(note or "").strip()
        if note_text and note_text not in merged_notes:
            merged_notes.append(note_text)
    if "merged_with_persisted_guest_capture_cache" not in merged_notes:
        merged_notes.append("merged_with_persisted_guest_capture_cache")
    merged["notes"] = merged_notes
    return merged


def should_persist_guest_manual_verification(runtime_status: dict[str, Any] | None = None) -> bool:
    runtime_status = runtime_status or browser_runtime.get_status()
    mappings = build_browser_capture_mappings(runtime_status=runtime_status)
    manual_mapping = mappings.get("manual") or {}
    if not _mapping_has_signal(manual_mapping):
        return False
    return _mapping_has_stage_signal(
        manual_mapping,
        ("chat_completion", "send_rate_limit", "get_nexpulse"),
    )


def should_seed_guest_runtime_from_manual(runtime_status: dict[str, Any] | None = None) -> bool:
    runtime_status = runtime_status or browser_runtime.get_status()
    return should_persist_guest_manual_verification(runtime_status)


def _has_live_manual_browser_session(runtime_status: dict[str, Any] | None) -> bool:
    if not isinstance(runtime_status, dict):
        return False
    if not runtime_status.get("manual_browser_started"):
        return False
    if not runtime_status.get("manual_context_ready"):
        return False
    if not runtime_status.get("manual_page_ready"):
        return False
    return bool(
        runtime_status.get("manual_chat_ready")
        or runtime_status.get("manual_logged_in")
    )


def has_active_manual_browser_window(runtime_status: dict[str, Any] | None) -> bool:
    if not isinstance(runtime_status, dict):
        return False
    return bool(
        runtime_status.get("manual_browser_started")
        and runtime_status.get("manual_context_ready")
        and runtime_status.get("manual_page_ready")
    )


def should_prefer_manual_browser_transport(
    *,
    effective_guest: bool,
    session_mode: str | None = None,
    runtime_status: dict[str, Any] | None = None,
    session=None,
) -> bool:
    if not effective_guest:
        return False
    if session is not None and not browser_runtime.is_manual_window_bound_to(session):
        return False
    runtime_status = runtime_status or browser_runtime.get_status()
    if not _has_live_manual_browser_session(runtime_status):
        return False
    mappings = build_browser_capture_mappings(runtime_status=runtime_status)
    manual_mapping = mappings.get("manual") or {}
    return _mapping_has_stage_signal(
        manual_mapping,
        ("chat_completion", "send_rate_limit", "get_nexpulse"),
    )


def get_preferred_browser_capture_mapping(
    runtime_status: dict[str, Any] | None = None,
    session_mode: str | None = None,
    guest: bool | None = None,
    session=None,
) -> dict[str, Any]:
    runtime_status = runtime_status or browser_runtime.get_status()
    mappings = build_browser_capture_mappings(runtime_status=runtime_status)
    manual_mapping = mappings.get("manual") or {}
    hidden_mapping = mappings.get("hidden") or {}
    mapping_mode = resolve_capture_mapping_mode(session_mode=session_mode, guest=guest)
    persisted_guest_mapping = _get_persisted_guest_capture_mapping() if mapping_mode == SESSION_MODE_GUEST else {}
    manual_session_matches = session is None or browser_runtime.is_manual_window_bound_to(session)
    if not manual_session_matches:
        manual_mapping = {}

    if mapping_mode == SESSION_MODE_GUEST:
        manual_mapping = _merge_capture_mappings(manual_mapping, persisted_guest_mapping)
        hidden_mapping = _merge_capture_mappings(hidden_mapping, persisted_guest_mapping)
        if _mapping_has_stage_signal(
            manual_mapping,
            ("chat_completion", "send_rate_limit", "get_nexpulse"),
        ):
            return manual_mapping
        if _mapping_has_signal(hidden_mapping):
            return hidden_mapping
        if _mapping_has_signal(manual_mapping):
            return manual_mapping
        return persisted_guest_mapping

    if mapping_mode == SESSION_MODE_AUTH:
        if _mapping_has_signal(manual_mapping):
            return manual_mapping
        return {}

    if _has_live_manual_browser_session(runtime_status):
        if _mapping_has_signal(manual_mapping):
            return manual_mapping
        return {}
    if _mapping_has_signal(manual_mapping):
        return manual_mapping
    return hidden_mapping


async def get_runtime_status(
    guest: bool = False,
    session_mode: str | None = None,
    conversation_id: str | None = None,
) -> dict:
    effective_guest = resolve_effective_guest_flag(guest, session_mode=session_mode)
    requested_conversation_id = None if conversation_id in (None, "", "0") else conversation_id
    session = None
    try:
        session, _ = await ensure_service_session(
            requested_conversation_id=requested_conversation_id,
            effective_guest=effective_guest,
            session_override=None,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        session = None
    runtime_status = browser_runtime.get_status()
    guest_manual_seed = effective_guest and should_seed_guest_runtime_from_manual(runtime_status)
    if session and (not effective_guest or guest_manual_seed):
        await browser_runtime.sync_live_manual_session(session)
        runtime_status = browser_runtime.get_status()
    if session:
        runtime_state_synced = sync_session_from_browser_runtime_state(session, runtime_status)
        manual_capture_synced = sync_session_from_manual_browser_capture(
            session,
            session_mode=session_mode,
            guest=effective_guest,
        )
        if effective_guest and (runtime_state_synced or manual_capture_synced):
            session = persist_session_state(
                session,
                requested_conversation_id,
                guest=True,
            )
    session_params = build_runtime_session_params(session) if session else None
    return {
        "browser": runtime_status,
        "curl_impersonate": get_curl_impersonate_profile(),
        "curl_cffi_available": curl_cffi_requests is not None,
        "preheat_all_sessions": os.getenv("DOUBAO_PREHEAT_ALL_SESSIONS", "0") == "1",
        "session_found": session is not None,
        "session_params": session_params,
    }


async def get_browser_capture(
    guest: bool = False,
    session_mode: str | None = None,
    conversation_id: str | None = None,
) -> dict:
    effective_guest = resolve_effective_guest_flag(guest, session_mode=session_mode)
    requested_conversation_id = None if conversation_id in (None, "", "0") else conversation_id
    session = None
    try:
        session, _ = await ensure_service_session(
            requested_conversation_id=requested_conversation_id,
            effective_guest=effective_guest,
            session_override=None,
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        session = None
    runtime_status = browser_runtime.get_status()
    guest_manual_seed = effective_guest and should_seed_guest_runtime_from_manual(runtime_status)
    if session and (not effective_guest or guest_manual_seed):
        await browser_runtime.sync_live_manual_session(session)
        runtime_status = browser_runtime.get_status()
    if session:
        runtime_state_synced = sync_session_from_browser_runtime_state(session, runtime_status)
        manual_capture_synced = sync_session_from_manual_browser_capture(
            session,
            session_mode=session_mode,
            guest=effective_guest,
        )
        if effective_guest and (runtime_state_synced or manual_capture_synced):
            session = persist_session_state(
                session,
                requested_conversation_id,
                guest=True,
            )
    mappings = build_browser_capture_mappings(runtime_status=runtime_status)
    return {
        "browser": runtime_status,
        "session_params": build_runtime_session_params(session) if session else None,
        "captures": {
            "manual": mappings.get("manual", {}).get("captures", {}),
            "hidden": mappings.get("hidden", {}).get("captures", {}),
            "manual_request_calls": mappings.get("manual", {}).get("request_calls", {}),
            "hidden_request_calls": mappings.get("hidden", {}).get("request_calls", {}),
        },
        "mapping": mappings,
    }


def get_manual_browser_request_overrides(
    session_mode: str | None = None,
    guest: bool | None = None,
    session=None,
) -> dict[str, str]:
    mapping = get_preferred_browser_capture_mapping(
        runtime_status=browser_runtime.get_status(),
        session_mode=session_mode,
        guest=guest,
        session=session,
    )
    overrides = mapping.get("request_overrides")
    if not isinstance(overrides, dict):
        return {}
    return {
        key: _non_empty_text(value)
        for key, value in overrides.items()
        if _non_empty_text(value)
    }


def get_captured_chat_x_flow_trace(
    session_mode: str | None = None,
    guest: bool | None = None,
    session=None,
) -> str:
    overrides = get_manual_browser_request_overrides(
        session_mode=session_mode,
        guest=guest,
        session=session,
    )
    return _non_empty_text(overrides.get("x_flow_trace"))


def get_manual_browser_chat_query_hints(
    session_mode: str | None = None,
    guest: bool | None = None,
    session=None,
) -> dict[str, Any]:
    mapping = get_preferred_browser_capture_mapping(
        runtime_status=browser_runtime.get_status(),
        session_mode=session_mode,
        guest=guest,
        session=session,
    )
    query_hints = mapping.get("query_hints")
    if not isinstance(query_hints, dict):
        return {}
    return dict(query_hints)


def get_manual_browser_chat_body_hints(
    session_mode: str | None = None,
    guest: bool | None = None,
    session=None,
) -> dict[str, Any]:
    mapping = get_preferred_browser_capture_mapping(
        runtime_status=browser_runtime.get_status(),
        session_mode=session_mode,
        guest=guest,
        session=session,
    )
    body_hints = mapping.get("body_hints")
    if not isinstance(body_hints, dict):
        return {}
    return _deep_clone_json_value(body_hints)


def build_signed_query_from_capture_hint(unsigned_query: str, query_hints: dict | None) -> tuple[str | None, str | None]:
    if not isinstance(query_hints, dict):
        return None, None
    captured_a_bogus = str(query_hints.get("captured_a_bogus") or "").strip()
    captured_unsigned_query = str(query_hints.get("captured_unsigned_query") or "").strip()
    if not captured_a_bogus or not captured_unsigned_query:
        return None, None
    sanitized_unsigned_query = build_unsigned_query_from_items(
        urllib.parse.parse_qsl(str(unsigned_query or "").strip(), keep_blank_values=True)
    )
    if captured_unsigned_query != sanitized_unsigned_query:
        return None, None

    items = urllib.parse.parse_qsl(sanitized_unsigned_query, keep_blank_values=True)
    items.append(("a_bogus", captured_a_bogus))
    return urllib.parse.urlencode(items), captured_a_bogus


def sync_session_from_manual_browser_capture(
    session,
    session_mode: str | None = None,
    guest: bool | None = None,
) -> bool:
    mapping = get_preferred_browser_capture_mapping(
        runtime_status=browser_runtime.get_status(),
        session_mode=session_mode,
        guest=guest,
        session=session,
    )
    if not _mapping_has_signal(mapping):
        return False

    runtime_status = browser_runtime.get_status()
    mapping_mode = _non_empty_text(mapping.get("mode"))
    live_manual_fp = _non_empty_text(runtime_status.get("manual_fp"))
    live_manual_ms_token = _non_empty_text(
        runtime_status.get("manual_ms_token") or runtime_status.get("ms_token")
    )
    changed = False
    if mapping_mode == "manual":
        if live_manual_fp and getattr(session, "fp", None) != live_manual_fp:
            session.fp = live_manual_fp
            changed = True
        if live_manual_ms_token and getattr(session, "ms_token", None) != live_manual_ms_token:
            session.ms_token = live_manual_ms_token
            changed = True
    session_updates = mapping.get("session_updates")
    if isinstance(session_updates, dict):
        for field_name, new_value in session_updates.items():
            value_text = _non_empty_text(new_value)
            if not value_text:
                continue
            if field_name == "fp" and live_manual_fp:
                continue
            if field_name == "ms_token" and live_manual_ms_token:
                continue
            if getattr(session, field_name, None) != value_text:
                setattr(session, field_name, value_text)
                changed = True

    if guest:
        persisted_mapping = _build_persistable_guest_capture_mapping(mapping)
        if persisted_mapping and getattr(session, "guest_capture_cache", None) != persisted_mapping:
            session.guest_capture_cache = persisted_mapping
            changed = True

    return changed


async def align_manual_browser_for_chat_request(
    session,
    conversation_id: str | None,
    request_mode: str | None = None,
) -> bool:
    runtime_status = browser_runtime.get_status()
    if not runtime_status.get("manual_browser_started"):
        return False

    try:
        await browser_runtime.start_manual_verification(
            session,
            conversation_id=conversation_id,
            force_new_chat=str(request_mode or "").strip().lower() == CONVERSATION_MODE_NEW,
            seed_existing_cookies=False,
            reuse_existing_window=True,
            wait_for_ready=False,
        )
        await browser_runtime.sync_live_manual_session(session)
        return sync_session_from_manual_browser_capture(session) or True
    except Exception as exc:
        logger.debug(f"Failed to align manual browser chat context: {exc}")
        return False


async def preview_chat_completion_request(
    prompt: str = "你好",
    guest: bool = False,
    session_mode: str | None = None,
    conversation_mode: str | None = None,
    conversation_id: str | None = None,
    section_id: str | None = None,
    think_mode: int | None = None,
    use_auto_cot: bool = False,
    use_deep_think: bool = False,
    include_signed_query: bool = True,
    session_override: dict | None = None,
) -> dict:
    effective_guest = resolve_effective_guest_flag(guest, session_mode=session_mode)
    requested_conversation_id, requested_section_id, effective_conversation_mode = resolve_requested_chat_target(
        conversation_id=conversation_id,
        section_id=section_id,
        conversation_mode=conversation_mode,
    )

    session, _ = await ensure_service_session(
        requested_conversation_id=requested_conversation_id,
        effective_guest=effective_guest,
        session_override=session_override,
    )

    conversation_info = None
    latest_index = None
    if requested_conversation_id is not None and not effective_guest:
        try:
            session, conversation_info = await fetch_conversation_info(requested_conversation_id, session=session)
            latest_section_id = conversation_info.get("last_section_id")
            if latest_section_id and not requested_section_id:
                requested_section_id = latest_section_id
            raw_latest_index = conversation_info.get("latest_index")
            if raw_latest_index not in (None, ""):
                try:
                    latest_index = int(raw_latest_index)
                except (TypeError, ValueError):
                    latest_index = None
        except Exception:
            pass

    runtime_status_before_ready = browser_runtime.get_status()
    guest_manual_seed = effective_guest and should_seed_guest_runtime_from_manual(runtime_status_before_ready)
    seed_manual_storage = (not effective_guest) or guest_manual_seed
    manual_live_synced = False
    preview_runtime_refresh_error = ""
    preview_runtime_refresh_skipped = not include_signed_query
    if include_signed_query:
        try:
            await asyncio.wait_for(
                browser_runtime.ensure_ready(session, seed_manual_storage=seed_manual_storage),
                timeout=5,
            )
            if not effective_guest or guest_manual_seed:
                manual_live_synced = await asyncio.wait_for(
                    browser_runtime.sync_live_manual_session(session),
                    timeout=3,
                )
        except Exception as exc:
            preview_runtime_refresh_error = str(exc)
            logger.debug(f"Preview runtime refresh skipped after timeout/error: {exc}")
    elif session and (not effective_guest or guest_manual_seed) and _has_live_manual_browser_session(runtime_status_before_ready):
        try:
            manual_live_synced = await asyncio.wait_for(
                browser_runtime.sync_live_manual_session(session),
                timeout=3,
            )
        except Exception as exc:
            preview_runtime_refresh_error = str(exc)
            logger.debug(f"Preview manual session sync skipped after timeout/error: {exc}")
    runtime_status = browser_runtime.get_status()
    runtime_state_synced = sync_session_from_browser_runtime_state(session, runtime_status)
    manual_capture_synced = sync_session_from_manual_browser_capture(
        session,
        session_mode=session_mode,
        guest=effective_guest,
    )
    if effective_guest and not session_override and (
        manual_live_synced or runtime_state_synced or manual_capture_synced
    ):
        session = persist_session_state(
            session,
            requested_conversation_id,
            guest=True,
        )
    query_hints = get_manual_browser_chat_query_hints(
        session_mode=session_mode,
        guest=effective_guest,
        session=session,
    )
    body_hints = get_manual_browser_chat_body_hints(
        session_mode=session_mode,
        guest=effective_guest,
        session=session,
    )
    capture_mapping = get_preferred_browser_capture_mapping(
        runtime_status=runtime_status,
        session_mode=session_mode,
        guest=effective_guest,
        session=session,
    )

    if getattr(session, "web_tab_id", None) is None:
        get_session_web_tab_id(session)

    effective_think_mode = resolve_think_mode(think_mode, use_auto_cot, use_deep_think)
    manual_overrides = get_manual_browser_request_overrides(
        session_mode=session_mode,
        guest=effective_guest,
        session=session,
    )
    captured_chat_x_flow_trace = get_captured_chat_x_flow_trace(
        session_mode=session_mode,
        guest=effective_guest,
        session=session,
    )
    live_manual_url = str(browser_runtime.get_status().get("manual_current_url") or "").strip()
    referer = resolve_effective_chat_referer(
        requested_conversation_id=requested_conversation_id,
        manual_overrides=manual_overrides,
        live_manual_url=live_manual_url,
        request_mode=effective_conversation_mode,
    )
    onboarding_preflight = None
    if effective_guest:
        onboarding_preflight = {
            "url": f"https://www.doubao.com/biz/onboarding/get_nexpulse?{build_im_params(session)}",
            "headers": build_get_nexpulse_headers(session, referer),
            "body": build_get_nexpulse_body(session),
        }
    preflight = {
        "url": f"https://www.doubao.com/im/message/send_rate_limit?{build_im_params(session)}",
        "headers": build_im_headers(session, referer),
        "body": build_send_rate_limit_body(),
    }
    preflight_chain = []
    if onboarding_preflight:
        preflight_chain.append({"name": "get_nexpulse", **onboarding_preflight})
    preflight_chain.append({"name": "send_rate_limit", **preflight})

    body = build_web_chat_completion_body(
        prompt=prompt,
        think_mode=effective_think_mode,
        conversation_id=requested_conversation_id,
        section_id=requested_section_id,
        latest_index=latest_index,
        session=session,
        attachments=None,
        body_hints=body_hints,
    )
    unsigned_query = build_web_chat_completion_params(
        session,
        include_signature=False,
        query_hints=query_hints,
    )
    unsigned_url = "https://www.doubao.com/chat/completion?" + unsigned_query
    signed_query = None
    signed_url = None
    signing_error = None
    if include_signed_query:
        try:
            signed_query, bogus = build_signed_query_from_capture_hint(unsigned_query, query_hints)
            if signed_query is None:
                signed_query, bogus = await browser_runtime.sign_query(
                    session,
                    unsigned_query,
                    seed_manual_storage=seed_manual_storage,
                )
            session.a_bogus = bogus
            signed_url = "https://www.doubao.com/chat/completion?" + signed_query
        except Exception as exc:
            signing_error = str(exc)

    headers = build_web_chat_completion_headers(
        session,
        referer,
        x_flow_trace=captured_chat_x_flow_trace,
    )
    sanitized_cookie_map = parse_cookie_header(headers.get("cookie", ""))

    core_auth_cookie_names = [
        "sessionid",
        "sessionid_ss",
        "sid_tt",
        "uid_tt",
        "uid_tt_ss",
        "ttwid",
        "passport_csrf_token",
        "passport_csrf_token_default",
        "s_v_web_id",
    ]
    recommended_cookie_names = [
        "sid_guard",
        "session_tlb_tag",
        "sid_ucp_v1",
        "ssid_ucp_v1",
        "use_biz_token",
        "passport_fe_beating_status",
        "flow_ssr_sidebar_expand",
    ]

    missing_runtime_fields = []
    if not session.fp:
        missing_runtime_fields.append("fp")
    if not session.ms_token:
        missing_runtime_fields.append("ms_token")
    if not session.web_tab_id:
        missing_runtime_fields.append("web_tab_id")
    if not captured_chat_x_flow_trace:
        missing_runtime_fields.append("captured_chat_x_flow_trace")
    if not getattr(session, "bot_id", None) and not _extract_bot_id_from_mapping(capture_mapping):
        missing_runtime_fields.append("bot_id")

    notes = []
    if "msToken" not in sanitized_cookie_map:
        notes.append("msToken is intentionally not sent in Cookie; it is sent as a query parameter.")
    if runtime_status.get("manual_fp") and runtime_status.get("manual_fp") != session.fp:
        notes.append("Manual verification fp differs from the current request fp.")
    if runtime_status.get("manual_ms_token") and runtime_status.get("manual_ms_token") != session.ms_token:
        notes.append("Manual verification ms_token differs from the current request ms_token.")
    if manual_live_synced:
        notes.append("Current request cookies and tokens were refreshed from the live manual browser session.")
    if preview_runtime_refresh_skipped:
        notes.append("Preview skipped browser runtime refresh because include_signed_query=false; response uses current captured snapshot only.")
    elif preview_runtime_refresh_error:
        notes.append("Preview browser runtime refresh timed out or failed; response falls back to current captured snapshot.")
    if manual_capture_synced:
        notes.append("Current request parameters were synced from the latest manual browser capture.")
    if conversation_info and requested_section_id:
        notes.append("Continuation mode keeps the caller or runtime-resolved section_id and only backfills it when missing.")
    elif requested_conversation_id is None:
        notes.append("New conversation mode leaves conversation_id and section_id empty in client_meta.")
    if effective_guest:
        notes.append("Observed guest web flow sends /biz/onboarding/get_nexpulse before /im/message/send_rate_limit and /chat/completion.")
    else:
        notes.append("Official web flow sends /im/message/send_rate_limit before /chat/completion.")
    if query_hints.get("captured_a_bogus"):
        notes.append("If the unsigned query matches the latest manual browser chat_completion, the captured long a_bogus is reused.")
    if body_hints.get("template"):
        notes.append("Request body reuses the latest captured browser chat_completion structure and only refreshes prompt, ids, and timing fields.")
    if captured_chat_x_flow_trace:
        notes.append("chat_completion.x-flow-trace is taken from the latest captured browser chat request header.")
    else:
        notes.append("chat_completion.x-flow-trace is currently missing from captured browser headers; preview is informational only until a real chat request is captured.")
    if getattr(session, "bot_id", None):
        notes.append("bot_id is resolved from session or the latest browser capture mapping instead of a direct hard-coded request body value.")

    return {
        "browser": runtime_status,
        "session_params": build_runtime_session_params(session),
        "request": {
            "guest_onboarding": onboarding_preflight,
            "preflight": preflight,
            "preflight_chain": preflight_chain,
            "referer": referer,
            "unsigned_url": unsigned_url,
            "signed_url": signed_url,
            "headers": headers,
            "captured_x_flow_trace": captured_chat_x_flow_trace or None,
            "body": body,
            "body_hint_source": body_hints.get("source"),
            "body_hint_message_shape": body_hints.get("message_shape"),
            "cookie_names": sorted(sanitized_cookie_map.keys()),
            "query": {
                "unsigned": unsigned_query,
                "signed": signed_query,
            },
        },
        "checks": {
            "core_auth_cookies": {name: name in sanitized_cookie_map for name in core_auth_cookie_names},
            "recommended_cookies": {name: name in sanitized_cookie_map for name in recommended_cookie_names},
            "missing_core_auth_cookies": [name for name in core_auth_cookie_names if name not in sanitized_cookie_map],
            "missing_recommended_cookies": [name for name in recommended_cookie_names if name not in sanitized_cookie_map],
            "missing_runtime_fields": missing_runtime_fields,
            "signing_error": signing_error,
            "preview_runtime_refresh_error": preview_runtime_refresh_error or None,
            "capture_mapping": {
                "selected_capture_key": capture_mapping.get("selected_capture_key"),
                "selected_request_call_key": capture_mapping.get("selected_request_call_key"),
                "request_override_sources": capture_mapping.get("request_override_sources", {}),
                "query_hint_sources": capture_mapping.get("query_hint_sources", {}),
                "session_update_sources": capture_mapping.get("session_update_sources", {}),
            },
            "notes": notes,
        },
    }


async def start_manual_verification(
    guest: bool = False,
    conversation_mode: str | None = None,
    conversation_id: str | None = None,
    open_conversation_id: str | None = None,
    session_override: dict | None = None,
    seed_existing_cookies: bool = False,
    reuse_existing_window: bool = True,
    wait_for_ready: bool = False,
    wait_timeout_seconds: int = 300,
    poll_interval_ms: int = 1000,
) -> dict:
    requested_conversation_id, _, effective_conversation_mode = resolve_requested_chat_target(
        conversation_id=conversation_id,
        section_id=None,
        conversation_mode=conversation_mode,
    )
    open_target_conversation_id = None if open_conversation_id in (None, "", "0") else open_conversation_id

    session, _ = await ensure_service_session(
        requested_conversation_id=requested_conversation_id,
        effective_guest=guest,
        session_override=session_override,
    )

    manual_state = await browser_runtime.start_manual_verification(
        session,
        conversation_id=open_target_conversation_id or requested_conversation_id,
        force_new_chat=(
            open_target_conversation_id is None
            and effective_conversation_mode == CONVERSATION_MODE_NEW
        ),
        seed_existing_cookies=seed_existing_cookies,
        reuse_existing_window=reuse_existing_window,
        wait_for_ready=wait_for_ready,
        wait_timeout_seconds=wait_timeout_seconds,
        poll_interval_ms=poll_interval_ms,
    )
    browser_status = browser_runtime.get_status()
    return {
        "open_url": browser_status.get("manual_current_url") or manual_state.get("open_url"),
        "browser": browser_status,
        "session_params": build_runtime_session_params(session),
        "ready": bool(browser_status.get("manual_chat_ready") or manual_state.get("ready")),
        "reused_window": bool(manual_state.get("reused_window")),
    }


async def finish_manual_verification(
    guest: bool = False,
    conversation_id: str | None = None,
    session_override: dict | None = None,
    persist_session: bool = True,
    close_window: bool = True,
) -> dict:
    requested_conversation_id = None if conversation_id in (None, "", "0") else conversation_id

    session, _ = await ensure_service_session(
        requested_conversation_id=requested_conversation_id,
        effective_guest=guest,
        session_override=session_override,
    )

    previous_session_state = session.model_dump()
    runtime_status_before_finish = browser_runtime.get_status()
    guest_manual_persist_allowed = (
        not guest
        or should_persist_guest_manual_verification(runtime_status_before_finish)
    )

    if guest_manual_persist_allowed:
        sync_session_from_manual_browser_capture(session, guest=guest)
    await browser_runtime.finish_manual_verification(session, close_window=close_window)
    if guest_manual_persist_allowed:
        if not close_window:
            sync_session_from_manual_browser_capture(session, guest=guest)
    else:
        for field_name, field_value in previous_session_state.items():
            setattr(session, field_name, field_value)

    if persist_session and guest_manual_persist_allowed:
        session = persist_session_state(
            session,
            requested_conversation_id,
            guest=guest,
        )
    elif requested_conversation_id and guest_manual_persist_allowed:
        session_pool.set_session(requested_conversation_id, session)

    return {
        "persisted": bool(persist_session and guest_manual_persist_allowed),
        "browser": browser_runtime.get_status(),
        "session_params": build_runtime_session_params(session),
    }


async def send_manual_browser_message(
    prompt: str,
    guest: bool = False,
    conversation_id: str | None = None,
    session_override: dict | None = None,
    wait_timeout_seconds: int = 30,
) -> dict:
    requested_conversation_id, _, effective_conversation_mode = resolve_requested_chat_target(
        conversation_id=conversation_id,
        section_id=None,
        conversation_mode=None,
    )

    session, _ = await ensure_service_session(
        requested_conversation_id=requested_conversation_id,
        effective_guest=guest,
        session_override=session_override,
    )
    previous_room_id = _non_empty_text(getattr(session, "room_id", None))

    await align_manual_browser_for_chat_request(
        session,
        requested_conversation_id if effective_conversation_mode == CONVERSATION_MODE_CONTINUE else None,
        request_mode=effective_conversation_mode,
    )
    await browser_runtime.sync_live_manual_session(session)
    result = await browser_runtime.send_manual_prompt(
        session,
        prompt=prompt,
        wait_timeout_seconds=wait_timeout_seconds,
    )
    await browser_runtime.sync_live_manual_session(session)
    sync_session_from_manual_browser_capture(session, guest=guest)

    parsed_text = None
    parsed_images: list[str] = []
    parsed_conversation_id = None
    parsed_message_id = None
    parsed_section_id = None
    browser_status = browser_runtime.get_status()
    manual_url_conversation_id = _extract_conversation_id_from_chat_referer(
        browser_status.get("manual_current_url")
    )
    reusable_manual_url_conversation_id = manual_url_conversation_id
    if (
        effective_conversation_mode == CONVERSATION_MODE_NEW
        and reusable_manual_url_conversation_id in {"", "0", previous_room_id}
    ):
        reusable_manual_url_conversation_id = ""
    chat_capture = result.get("chat_completion")
    if isinstance(chat_capture, dict):
        raw_text = chat_capture.get("response_text")
        if isinstance(raw_text, str) and raw_text.strip():
            try:
                (
                    parsed_text,
                    parsed_images,
                    parsed_conversation_id,
                    parsed_message_id,
                    parsed_section_id,
                ) = await handle_sse_text(raw_text)
                cached_conversation_id = parsed_conversation_id or requested_conversation_id
                if parsed_conversation_id:
                    session.room_id = parsed_conversation_id
                if cached_conversation_id:
                    cache_conversation_state(
                        cached_conversation_id,
                        section_id=parsed_section_id,
                        override=True,
                    )
            except VerifyRequiredException as exc:
                partial_conversation_id = (
                    _non_empty_text(exc.conversation_id)
                    or reusable_manual_url_conversation_id
                    or requested_conversation_id
                )
                partial_section_id = _non_empty_text(exc.section_id)
                if partial_conversation_id:
                    exc.conversation_id = partial_conversation_id
                    session.room_id = partial_conversation_id
                    session_pool.set_session(partial_conversation_id, session)
                    cache_conversation_state(
                        partial_conversation_id,
                        section_id=partial_section_id,
                        override=True,
                    )
                if guest:
                    persist_session_state(session, partial_conversation_id, guest=True)
                else:
                    persist_session_state(session, partial_conversation_id)
                raise
            except Exception:
                if reusable_manual_url_conversation_id:
                    session.room_id = reusable_manual_url_conversation_id
                    session_pool.set_session(reusable_manual_url_conversation_id, session)
                pass

    resolved_conversation_id, resolved_message_id, resolved_section_id = resolve_completion_identifiers(
        session=session,
        requested_conversation_id=requested_conversation_id,
        response_conversation_id=parsed_conversation_id,
        requested_section_id=None,
        response_section_id=parsed_section_id,
        message_id=parsed_message_id,
        runtime_status=browser_status,
        request_mode=effective_conversation_mode,
        previous_room_id=previous_room_id,
    )
    if resolved_conversation_id not in {"", "0"}:
        session.room_id = resolved_conversation_id
        session_pool.set_session(resolved_conversation_id, session)
        cache_conversation_state(
            resolved_conversation_id,
            section_id=resolved_section_id,
            override=True,
        )
    completion_meta = build_completion_response_meta(
        request_mode=effective_conversation_mode,
        text=parsed_text,
        image_urls=parsed_images,
        conversation_id=resolved_conversation_id,
    )

    persist_target_conversation_id = resolved_conversation_id or requested_conversation_id
    if guest:
        persist_session_state(session, persist_target_conversation_id, guest=True)
    else:
        persist_session_state(session, persist_target_conversation_id)

    return {
        "sent": bool(result.get("sent")),
        "capture_count_before": int(result.get("capture_count_before") or 0),
        "capture_count_after": int(result.get("capture_count_after") or 0),
        "chat_completion": chat_capture,
        "text": parsed_text,
        "img_urls": parsed_images,
        "conversation_id": resolved_conversation_id,
        "message_id": resolved_message_id,
        "section_id": resolved_section_id,
        "browser": browser_runtime.get_status(),
        "session_params": build_runtime_session_params(session),
        "completion_meta": completion_meta,
    }


def can_bootstrap_capture_via_manual_browser(runtime_status: dict[str, Any] | None) -> bool:
    if not isinstance(runtime_status, dict):
        return False
    return bool(
        runtime_status.get("manual_browser_started")
        and runtime_status.get("manual_context_ready")
        and runtime_status.get("manual_page_ready")
        and runtime_status.get("manual_chat_ready")
    )


def build_manual_verification_required_detail(
    *,
    message: str,
    manual_window_opened: bool,
    open_url: str | None,
    runtime_status: dict[str, Any] | None,
) -> dict[str, Any]:
    status = runtime_status or {}
    manual_ready = bool(status.get("manual_chat_ready"))
    manual_window_available = bool(
        manual_window_opened
        or (
            status.get("manual_browser_started")
            and status.get("manual_context_ready")
            and status.get("manual_page_ready")
        )
    )
    resolved_open_url = status.get("manual_current_url") or open_url
    return {
        "code": "manual_verification_required",
        "message": message,
        "verification_window_opened": manual_window_available,
        "manual_window_opened": manual_window_available,
        "ready": manual_ready,
        "capture_ready": False,
        "manual_ready": manual_ready,
        "awaiting_user_action": not manual_ready,
        "manual_current_url": status.get("manual_current_url"),
        "open_url": resolved_open_url,
    }


def build_upstream_verification_required_detail(
    exc: "VerifyRequiredException",
    *,
    runtime_status: dict[str, Any] | None,
    manual_window_opened: bool = False,
    open_url: str | None = None,
) -> dict[str, Any]:
    status = runtime_status or {}
    manual_ready = bool(status.get("manual_chat_ready"))
    manual_window_available = bool(
        manual_window_opened
        or (
            status.get("manual_browser_started")
            and status.get("manual_context_ready")
            and status.get("manual_page_ready")
        )
    )
    resolved_open_url = status.get("manual_current_url") or open_url
    detail_message = str(exc)
    if manual_window_available and resolved_open_url:
        detail_message += " Complete verification in the visible browser window and retry."
    return {
        "code": "upstream_verification_required",
        "message": detail_message,
        "error_code": exc.error_code,
        "verify_scene": exc.verify_scene,
        "subtype": exc.subtype,
        "log_id": exc.log_id,
        "conversation_id": exc.conversation_id,
        "section_id": exc.section_id,
        "message_id": exc.message_id,
        "verification_window_opened": manual_window_available,
        "manual_window_opened": manual_window_available,
        "ready": False,
        "capture_ready": False,
        "manual_ready": manual_ready,
        "awaiting_user_action": True,
        "manual_current_url": status.get("manual_current_url"),
        "open_url": resolved_open_url,
        "retry_as_continue": bool(exc.conversation_id),
    }


async def prepare_visible_verification_window_for_upstream_risk(
    *,
    session,
    effective_guest: bool,
    session_mode: str | None,
    request_mode: str | None,
    requested_conversation_id: str | None,
    session_meta: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool, str | None]:
    session_meta = session_meta or {}
    runtime_status_before = browser_runtime.get_status()
    manual_window_opened = False
    manual_open_url = runtime_status_before.get("manual_current_url")
    runtime_status_after = runtime_status_before
    session_changed = False

    try:
        if ((not effective_guest) or should_seed_guest_runtime_from_manual(runtime_status_after)) and await browser_runtime.sync_live_manual_session(session):
            session_changed = True
        if sync_session_from_browser_runtime_state(session, runtime_status_after):
            session_changed = True
        if sync_session_from_manual_browser_capture(
            session,
            session_mode=session_mode,
            guest=effective_guest,
        ):
            session_changed = True
        if session_changed and not session_meta.get("from_override"):
            persist_session_state(
                session,
                requested_conversation_id,
                guest=effective_guest,
            )
    except Exception as exc:
        logger.warning(f"Failed to prepare visible verification window after upstream risk control: {exc}")
        runtime_status_after = browser_runtime.get_status()
        manual_open_url = runtime_status_after.get("manual_current_url") or manual_open_url

    return runtime_status_after, manual_window_opened, manual_open_url


def build_guest_session_rotated_detail(rotation: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": "guest_session_rotated",
        "message": str(rotation.get("message") or "Guest session rotated."),
        "backup_path": rotation.get("backup_path"),
        "backup_conversation_ids": list(rotation.get("backup_conversation_ids") or []),
        "previous_conversation_count": int(rotation.get("previous_conversation_count") or 0),
        "guest_conversation_limit": int(rotation.get("guest_conversation_limit") or 0),
        "verification_window_opened": bool(rotation.get("verification_window_opened")),
        "manual_window_opened": bool(rotation.get("verification_window_opened")),
        "ready": bool(rotation.get("ready")),
        "manual_ready": bool(rotation.get("manual_ready")),
        "capture_ready": bool(rotation.get("capture_ready")),
        "awaiting_user_action": not bool(rotation.get("ready")),
        "open_url": rotation.get("open_url"),
        "manual_current_url": (rotation.get("browser") or {}).get("manual_current_url"),
    }


def build_web_chat_completion_body(
    prompt: str,
    think_mode: int,
    conversation_id: str | None,
    section_id: str | None,
    latest_index: int | None,
    session,
    attachments: list[dict] | None = None,
    body_hints: dict | None = None,
) -> dict:
    local_conversation_id = f"local_{int(uuid.uuid4().int % 10000000000000000)}"
    local_message_id = str(uuid.uuid1())
    is_new_conversation = conversation_id is None
    fp = get_session_fp(session)
    bot_id = resolve_session_bot_id(session)
    create_time_ms = int(time.time() * 1000)
    attachments = _deep_clone_json_value(attachments or [])
    body_template = _normalize_chat_request_template_for_mode(
        (body_hints or {}).get("template"),
        is_new_conversation=is_new_conversation,
    )
    body = _deep_clone_json_value(body_template or {})

    client_meta_template = body.get("client_meta")
    client_meta = _decode_dict_like(client_meta_template)
    client_meta["conversation_id"] = conversation_id or ""
    client_meta["bot_id"] = bot_id
    client_meta["last_section_id"] = section_id or ""
    if latest_index not in (None, ""):
        client_meta["last_message_index"] = latest_index
    else:
        client_meta.pop("last_message_index", None)
    if is_new_conversation:
        client_meta["local_conversation_id"] = local_conversation_id
    else:
        client_meta.pop("local_conversation_id", None)
    body["client_meta"] = _encode_like_template(client_meta_template, client_meta)

    messages_template = body.get("messages")
    messages = _decode_list_like(messages_template)
    first_message = messages[0] if messages and isinstance(messages[0], dict) else {}
    uses_content_block = "content_block" in first_message or "content" not in first_message
    if not isinstance(first_message, dict):
        first_message = {}
    if uses_content_block:
        content_blocks_template = first_message.get("content_block")
        content_blocks = _decode_list_like(content_blocks_template)
        first_block = content_blocks[0] if content_blocks and isinstance(content_blocks[0], dict) else {}
        if not isinstance(first_block, dict):
            first_block = {}
        block_content_template = first_block.get("content")
        block_content = _decode_dict_like(block_content_template)
        text_block_template = block_content.get("text_block")
        text_block = _decode_dict_like(text_block_template)
        text_block["text"] = prompt
        text_block.setdefault("icon_url", "")
        text_block.setdefault("icon_url_dark", "")
        text_block.setdefault("summary", "")
        block_content["text_block"] = _encode_like_template(text_block_template, text_block)
        block_content.setdefault("pc_event_block", "")
        first_block["block_type"] = first_block.get("block_type", 10000)
        first_block["content"] = _encode_like_template(block_content_template, block_content)
        first_block["block_id"] = str(uuid.uuid4())
        first_block["parent_id"] = ""
        first_block["meta_info"] = []
        first_block["append_fields"] = []
        if content_blocks:
            content_blocks[0] = first_block
        else:
            content_blocks = [first_block]
        first_message["local_message_id"] = local_message_id
        first_message["content_block"] = _encode_like_template(content_blocks_template, content_blocks)
        first_message["message_status"] = first_message.get("message_status", 0)
    else:
        content_template = first_message.get("content")
        content_value = decode_jsonish(content_template, max_depth=8)
        if not isinstance(content_value, dict):
            content_value = {}
        content_value["text"] = prompt
        first_message["content"] = _encode_like_template(content_template, content_value)
        first_message["content_type"] = first_message.get("content_type", 2001)
    if attachments or "attachments" in first_message:
        first_message["attachments"] = attachments
    if "references" in first_message:
        first_message["references"] = _deep_clone_json_value(first_message.get("references") or [])
    if messages:
        messages[0] = first_message
    else:
        messages = [first_message]
    body["messages"] = _encode_like_template(messages_template, messages)

    option_template = body.get("option")
    option = _decode_dict_like(option_template)
    option_defaults = {
        "send_message_scene": "",
        "collect_id": "",
        "is_audio": False,
        "answer_with_suggest": False,
        "tts_switch": False,
        "click_clear_context": False,
        "from_suggest": False,
        "is_regen": False,
        "is_replace": False,
        "disable_sse_cache": False,
        "select_text_action": "",
        "resend_for_regen": False,
        "scene_type": 0,
        "regen_query_id": [],
        "edit_query_id": [],
        "regen_instruction": "",
        "no_replace_for_regen": False,
        "message_from": 0,
        "shared_app_name": "",
        "sse_recv_event_options": {"support_chunk_delta": True},
        "is_ai_playground": False,
    }
    for key, default_value in option_defaults.items():
        if key not in option:
            option[key] = _deep_clone_json_value(default_value)
    option["create_time_ms"] = create_time_ms
    option["need_deep_think"] = think_mode
    option["unique_key"] = str(uuid.uuid4())
    option["start_seq"] = 0 if is_new_conversation else option.get("start_seq", 0)
    option["need_create_conversation"] = is_new_conversation
    conversation_init_option_template = option.get("conversation_init_option")
    if is_new_conversation:
        conversation_init_option = _decode_dict_like(conversation_init_option_template)
        conversation_init_option["need_ack_conversation"] = True
        option["conversation_init_option"] = _encode_like_template(
            conversation_init_option_template,
            conversation_init_option or {"need_ack_conversation": True},
        )
    else:
        option.pop("conversation_init_option", None)
    body["option"] = _encode_like_template(option_template, option)

    ext_template = body.get("ext")
    ext = _decode_dict_like(ext_template)
    if "commerce_credit_config_enable" not in ext:
        ext["commerce_credit_config_enable"] = "0"
    ext["use_deep_think"] = str(think_mode)
    ext["sub_conv_firstmet_type"] = "1" if is_new_conversation else "0"
    if fp:
        ext["fp"] = fp
    else:
        ext.pop("fp", None)
    ext_conversation_init_template = ext.get("conversation_init_option")
    if is_new_conversation:
        ext_conversation_init_value = {"need_ack_conversation": True}
        if isinstance(ext_conversation_init_template, str):
            ext["conversation_init_option"] = _encode_like_template(
                ext_conversation_init_template,
                ext_conversation_init_value,
            )
        else:
            ext["conversation_init_option"] = json.dumps(
                ext_conversation_init_value,
                ensure_ascii=False,
                separators=(",", ":"),
            )
    else:
        ext.pop("conversation_init_option", None)
    body["ext"] = _encode_like_template(ext_template, ext)

    return body


def extract_content_blocks_text(content_blocks) -> str:
    decoded_blocks = decode_jsonish(content_blocks, max_depth=6)
    if not isinstance(decoded_blocks, list):
        return ""
    texts = []
    for block in decoded_blocks:
        if not isinstance(block, dict):
            continue
        content = decode_jsonish(block.get("content"), max_depth=6)
        if not isinstance(content, dict):
            continue
        text_block = decode_jsonish(content.get("text_block"), max_depth=6)
        if isinstance(text_block, dict):
            text = extract_text_value(text_block.get("text"))
            if text:
                texts.append(text)
                continue
        text = extract_text_value(content)
        if text:
            texts.append(text)
    return "\n".join(text for text in texts if text).strip()


def extract_history_message_text(message: dict) -> str:
    for key in ("tts_content", "answer", "output_text", "text"):
        text = extract_text_value(message.get(key))
        if text:
            return text

    if content_blocks := extract_content_blocks_text(message.get("content_block")):
        return content_blocks

    if content := extract_text_value(message.get("content")):
        return content

    return ""


def sort_history_message_key(item: dict):
    index = item.get("index_in_conv")
    create_time = item.get("create_time")
    return (
        int(index) if index and str(index).isdigit() else 0,
        int(create_time) if create_time and str(create_time).isdigit() else 0,
    )


def extract_latest_history_assistant_text(node) -> str:
    candidates = []
    walk_history_message_candidates(node, candidates)
    normalized_messages = []
    for candidate in candidates:
        normalized = normalize_history_message(candidate)
        if not normalized:
            continue
        normalized_messages.append(normalized)
    if not normalized_messages:
        return ""
    normalized_messages = sorted(normalized_messages, key=sort_history_message_key)
    for message in reversed(normalized_messages):
        if str(message.get("role", "")) == "assistant" and str(message.get("text", "")).strip():
            return str(message.get("text", "")).strip()
    for message in reversed(normalized_messages):
        if str(message.get("text", "")).strip():
            return str(message.get("text", "")).strip()
    return ""


def extract_sse_event_text(event_data: dict, message: dict, evt_obj: dict | None = None) -> tuple[str, int | None, str]:
    evt_obj = evt_obj if isinstance(evt_obj, dict) else {}
    if not isinstance(message, dict):
        message = {}
    if not isinstance(event_data, dict):
        event_data = {}

    content_type = message.get("content_type")
    if content_type is None:
        content_type = event_data.get("content_type")

    text = extract_message_payload_text(event_data, message)
    if text:
        return text, content_type, "payload"

    for label, node in (
        ("message_history", message),
        ("event_history", event_data),
        ("outer_history", evt_obj),
    ):
        history_text = extract_latest_history_assistant_text(node)
        if history_text:
            return history_text, content_type, label

    for label, node in (
        ("event_text", event_data),
        ("message_text", message),
        ("outer_text", evt_obj),
    ):
        fallback_text = extract_text_value(node)
        if fallback_text:
            return fallback_text, content_type, label

    return "", content_type, ""


def recover_sse_text_from_events(raw_events: list[dict]) -> str:
    best_text = ""
    for item in raw_events:
        if not isinstance(item, dict):
            continue
        text, _, _ = extract_sse_event_text(
            item.get("event_data", {}),
            item.get("message", {}),
            item.get("evt_obj", {}),
        )
        cleaned = str(text or "").strip()
        if cleaned and len(cleaned) >= len(best_text):
            best_text = cleaned
    return best_text


def extract_web_chat_notify_text(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""

    content = decode_jsonish(payload.get("content"), max_depth=6)
    if isinstance(content, dict):
        text = extract_content_blocks_text(content.get("content_block"))
        if text:
            return text
        text = extract_text_value(content.get("tts_content"))
        if text:
            return text

    message = decode_jsonish(payload.get("message"), max_depth=6)
    if isinstance(message, dict):
        text = extract_history_message_text(message)
        if text:
            return text

    return ""


def extract_stream_chunk_patch_text(payload: dict) -> str:
    payload = payload if isinstance(payload, dict) else {}
    patch_ops = decode_jsonish(payload.get("patch_op"), max_depth=6)
    if not isinstance(patch_ops, list):
        return ""

    texts: list[str] = []
    for patch in patch_ops:
        if not isinstance(patch, dict):
            continue
        if patch.get("patch_object") != 1:
            continue
        patch_value = decode_jsonish(patch.get("patch_value"), max_depth=6)
        if not isinstance(patch_value, dict):
            continue
        text = extract_content_blocks_text(patch_value.get("content_block"))
        if text:
            texts.append(text)
    return "".join(texts)


def is_history_message_candidate(candidate) -> bool:
    if not isinstance(candidate, dict):
        return False
    if "message_id" not in candidate:
        return False
    return any(
        key in candidate
        for key in ("content", "content_block", "tts_content", "thinking_content", "index_in_conv", "user_type")
    )


def walk_history_message_candidates(node, results: list[dict]):
    decoded = decode_jsonish(node, max_depth=6)
    if isinstance(decoded, dict):
        if is_history_message_candidate(decoded):
            results.append(decoded)
        for value in decoded.values():
            walk_history_message_candidates(value, results)
        return
    if isinstance(decoded, list):
        for item in decoded:
            walk_history_message_candidates(item, results)


def normalize_history_message(message: dict):
    message_id = str(message.get("message_id") or "").strip()
    if not message_id:
        return None

    text = extract_history_message_text(message)
    if not text:
        return None

    user_type = str(message.get("user_type", ""))
    role = "user" if user_type == "1" else "assistant"
    return {
        "message_id": message_id,
        "role": role,
        "text": text,
        "create_time": str(message.get("create_time")) if message.get("create_time") is not None else None,
        "index_in_conv": str(message.get("index_in_conv")) if message.get("index_in_conv") is not None else None,
        "content_type": message.get("content_type"),
    }


async def fetch_conversation_info(conversation_id: str, session=None):
    session = session or session_pool.get_session(conversation_id) or session_pool.get_session(guest=False)
    if not session:
        raise HTTPException(status_code=404, detail="没有可用的已登录会话，无法恢复历史对话")

    params = build_im_params(session)
    url = "https://www.doubao.com/im/conversation/info?" + params
    body = {
        "cmd": 1110,
        "uplink_body": {
            "get_conv_info_uplink_body": {
                "conversation_id": conversation_id,
                "ext": {"cold_start": "true"},
                "bot_id": resolve_session_bot_id(session),
                "conversation_type": 3,
                "option": {"need_bot_info": True},
            }
        },
        "sequence_id": str(uuid.uuid4()),
        "channel": 2,
        "version": "1",
    }
    headers = build_im_headers(session, f"https://www.doubao.com/chat/{conversation_id}")

    async with aiohttp.ClientSession() as aio_session:
        async with aio_session.post(url, headers=headers, json=body) as response:
            if response.status != 200:
                error_text = await response.text()
                raise HTTPException(status_code=response.status, detail=f"获取会话信息失败: {error_text}")
            payload = await response.json(content_type=None)

    if payload.get("status_code") not in (0, None):
        raise HTTPException(status_code=500, detail=f"获取会话信息失败: {payload.get('status_desc') or payload}")

    info = (
        payload.get("downlink_body", {})
        .get("get_conv_info_downlink_body", {})
        .get("conversation_info")
    )
    if not info:
        raise HTTPException(status_code=500, detail="会话信息响应中缺少 conversation_info")

    read_index = info.get("last_read_index")
    if read_index not in (None, ""):
        await mark_conversation_read(conversation_id, read_index, session=session)

    cache_conversation_state(
        conversation_id,
        section_id=info.get("last_section_id"),
        latest_index=info.get("latest_index"),
        override=False,
    )
    info = merge_conversation_info_with_cached_state(conversation_id, info)

    session_pool.set_session(conversation_id, session)
    return session, info


async def get_conversation_info(conversation_id: str):
    _, info = await fetch_conversation_info(conversation_id)
    return info


async def pull_single_chain_messages(conversation_id: str, anchor_index: int, limit: int, session):
    params = build_im_params(session)
    url = "https://www.doubao.com/im/chain/single?" + params
    body = {
        "cmd": 3100,
        "uplink_body": {
            "pull_singe_chain_uplink_body": {
                "conversation_id": conversation_id,
                "anchor_index": anchor_index,
                "conversation_type": 3,
                "direction": 1,
                "limit": limit,
                "ext": {},
                "filter": {"index_list": []},
            }
        },
        "sequence_id": str(uuid.uuid4()),
        "channel": 2,
        "version": "1",
    }
    headers = build_im_headers(session, f"https://www.doubao.com/chat/{conversation_id}")

    async with aiohttp.ClientSession() as aio_session:
        async with aio_session.post(url, headers=headers, json=body) as response:
            if response.status != 200:
                error_text = await response.text()
                raise HTTPException(status_code=response.status, detail=f"拉取消息链失败: {error_text}")
            payload = await response.json(content_type=None)

    if payload.get("status_code") not in (0, None):
        raise HTTPException(status_code=500, detail=f"拉取消息链失败: {payload.get('status_desc') or payload}")

    return payload


async def get_conversation_messages(conversation_id: str, anchor_index: int | None = None, limit: int = 20):
    session = session_pool.get_session(conversation_id)
    conversation_info = None
    if session is None or anchor_index is None:
        session, conversation_info = await fetch_conversation_info(conversation_id, session=session)

    resolved_anchor_index = anchor_index
    if resolved_anchor_index is None:
        latest_index = conversation_info.get("latest_index") if conversation_info else None
        resolved_anchor_index = int(latest_index) if latest_index not in (None, "") else MAX_SAFE_ANCHOR_INDEX

    try:
        payload = await pull_single_chain_messages(
            conversation_id=conversation_id,
            anchor_index=resolved_anchor_index,
            limit=limit,
            session=session,
        )
    except HTTPException as exc:
        if resolved_anchor_index != MAX_SAFE_ANCHOR_INDEX:
            payload = await pull_single_chain_messages(
                conversation_id=conversation_id,
                anchor_index=MAX_SAFE_ANCHOR_INDEX,
                limit=limit,
                session=session,
            )
        else:
            raise exc

    candidates = []
    walk_history_message_candidates(payload, candidates)
    messages_by_id = {}
    for candidate in candidates:
        normalized = normalize_history_message(candidate)
        if not normalized:
            continue
        messages_by_id[normalized["message_id"]] = normalized

    messages = sorted(messages_by_id.values(), key=sort_history_message_key)
    if conversation_info is None:
        _, conversation_info = await fetch_conversation_info(conversation_id, session=session)

    return {
        "section_id": conversation_info.get("last_section_id"),
        "messages": messages,
    }


async def mark_conversation_read(conversation_id: str, read_index: str | int, session=None):
    session = session or session_pool.get_session(conversation_id) or session_pool.get_session(guest=False)
    if not session:
        return

    params = build_im_params(session)
    url = "https://www.doubao.com/im/message/mark_conv_read?" + params
    body = {
        "cmd": 2100,
        "uplink_body": {
            "mark_conv_read_uplink_body": {
                "conversation_id": conversation_id,
                "conversation_type": 3,
                "read_index": int(read_index),
            }
        },
        "sequence_id": str(uuid.uuid4()),
        "channel": 2,
        "version": "1",
    }
    headers = build_im_headers(session, f"https://www.doubao.com/chat/{conversation_id}")

    async with aiohttp.ClientSession() as aio_session:
        async with aio_session.post(url, headers=headers, json=body) as response:
            if response.status != 200:
                logger.warning(f"标记会话已读失败: {response.status} {await response.text()}")


async def _legacy_chat_completion_unused(
    prompt: str, 
    guest: bool,
    section_id: str = None, 
    conversation_id: str = None, 
    attachments: list[dict] = [], 
    think_mode: int | None = None,
    use_auto_cot: bool = False, 
    use_deep_think: bool = False
):
    # 获取会话配置
    new_session_tokens = {None, "", "0"}
    requested_conversation_id = None if conversation_id in new_session_tokens else conversation_id
    requested_section_id = None if section_id in new_session_tokens else section_id
    session = session_pool.get_session(requested_conversation_id, guest)
    conversation_info = None
    if requested_conversation_id is not None and not guest:
        try:
            session, conversation_info = await fetch_conversation_info(requested_conversation_id, session=session)
            latest_section_id = conversation_info.get("last_section_id")
            if latest_section_id:
                requested_section_id = latest_section_id
        except Exception:
            if session is None or not requested_section_id:
                raise
    if not session:
        raise HTTPException(status_code=404, detail=f"会话配置不存在,请检查 session.config 文件")
    
    latest_index = None
    if conversation_info:
        raw_latest_index = conversation_info.get("latest_index")
        if raw_latest_index not in (None, ""):
            try:
                latest_index = int(raw_latest_index)
            except (TypeError, ValueError):
                latest_index = None

    # ------ PARAMS -------
    params = build_web_chat_completion_params(session)
    
    # ------ URL -------
    url = "https://www.doubao.com/chat/completion?" + params

    # 新版客户端里 cot_switch 与 use_deep_think 联动，这里统一由 think_mode 驱动旧开关。
    effective_think_mode = resolve_think_mode(think_mode, use_auto_cot, use_deep_think)

    body = build_web_chat_completion_body(
        prompt=prompt,
        think_mode=effective_think_mode,
        conversation_id=requested_conversation_id,
        section_id=requested_section_id,
        latest_index=latest_index,
        session=session,
    )

    referer = "https://www.doubao.com/chat/"
    if requested_conversation_id:
        referer = f"https://www.doubao.com/chat/{requested_conversation_id}"
    headers = build_web_chat_completion_headers(session, referer)

    try:
        async with aiohttp.ClientSession() as aio_session:
            async with aio_session.post(url=url, headers=headers, json=body) as response:
                if x_ms_token := response.headers.get("x-ms-token"):
                    session.ms_token = x_ms_token
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"豆包聊天补全失败: {response.status}, 详情: {error_text}")
                try:
                    text, image_urls, response_conversation_id, message_id, response_section_id = await handle_sse(response)
                    cache_conversation_id = response_conversation_id or requested_conversation_id
                    resolved_conversation_id = cache_conversation_id or "0"
                    resolved_section_id = response_section_id or requested_section_id or "0"
                    resolved_message_id = message_id or "0"
                    if not text and not image_urls and not cache_conversation_id:
                        raise Exception("SSE 未返回有效的消息内容或会话标识")
                    if cache_conversation_id:
                        session_pool.set_session(cache_conversation_id, session)
                    return text, image_urls, resolved_conversation_id, resolved_message_id, resolved_section_id
                except LimitedException:
                    session_pool.del_session(session)
                    raise HTTPException(status_code=500, detail="游客会话额度已用完，请更换新 Session")
    except Exception as e:
        raise Exception(f"豆包请求失败: {str(e)}")
    
    # ------ BODY -------
    body = {
        "completion_option": {
            "is_regen": False,
            "with_suggest": False,
            "need_create_conversation": requested_conversation_id is None,
            "launch_stage": 1,
            "use_auto_cot": effective_use_auto_cot,
            "use_deep_think": effective_use_deep_think
        },
        "conversation_id": "0" if requested_conversation_id is None else requested_conversation_id,
        "messages": [
            {
                "content": json.dumps({"text": prompt}),
                "content_type": 2001,
                "attachments": attachments,
                "references": []
            }
        ]
    }
    
    if requested_section_id is not None:
        body["section_id"] = requested_section_id
    
    # 如果是未登录账户，则不需要 local 字段
    if not guest:
        body["local_conversation_id"] = f"local_{int(uuid.uuid4().int % 10000000000000000)}" 
        body["local_message_id"] = str(uuid.uuid4())
    
    # ------ HEADERS -------
    headers = {
        'content-type': 'application/json',
        'accept': 'text/event-stream',
        'agw-js-conv': 'str',
        'cookie': session.cookie,
        'origin': "https://www.doubao.com",
        'referer': f"https://www.doubao.com/chat/{requested_conversation_id or session.room_id}",
        'user-agent': BROWSER_UA,
        "x-flow-trace": session.x_flow_trace
    }
    try:
        async with aiohttp.ClientSession() as aio_session:
            async with aio_session.post(url=url, headers=headers, json=body) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"豆包API对话补全失败: {response.status}, 详情: {error_text}")
                try:
                    # 下一次会话需要同一个session
                    text, image_urls, response_conversation_id, message_id, response_section_id = await handle_sse(response)
                    cache_conversation_id = response_conversation_id or requested_conversation_id
                    resolved_conversation_id = cache_conversation_id or "0"
                    resolved_section_id = response_section_id or requested_section_id or "0"
                    resolved_message_id = message_id or "0"
                    if not text and not image_urls and not cache_conversation_id:
                        raise Exception("SSE 未返回有效的消息内容或会话标识")
                    if cache_conversation_id:
                        session_pool.set_session(cache_conversation_id, session)
                    return text, image_urls, resolved_conversation_id, resolved_message_id, resolved_section_id
                except LimitedException:
                    session_pool.del_session(session)
                    raise HTTPException(status_code=500, detail=f"游客限制5次会话已用完，请重使用新Session")
    except Exception as e:
        raise Exception(f"豆包API请求失败: {str(e)}")


async def handle_sse(response: aiohttp.ClientResponse):
    """Handle both legacy Samantha SSE and the newer web chat/completion SSE."""
    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    conversation_id = ""
    message_id = ""
    section_id = ""
    texts: list[str] = []
    image_urls = []
    full_text = ""
    raw_events = []

    def append_text(piece: str):
        text = str(piece or "")
        if text:
            texts.append(text)

    def parse_event_payload(lines: list[str]):
        data_lines = [line[6:] for line in lines if line.startswith("data: ")]
        if not data_lines:
            return None
        return json.loads("\n".join(data_lines))

    async for chunk in response.content.iter_chunked(1024):
        buffer += decoder.decode(chunk)

        if "tourist conversation reach limited" in buffer:
            raise LimitedException()

        if "event: gateway-error" in buffer:
            error_match = buffer.find("data: {")
            if error_match != -1:
                try:
                    error_data = json.loads(buffer[error_match + 6:].split("\n")[0])
                    raise Exception(f"gateway-error: {error_data.get('code')} - {error_data.get('message')}")
                except Exception:
                    raise Exception(f"gateway-error: {buffer}")

        events = buffer.split("\n\n")
        buffer = events.pop()

        for evt in events:
            lines = evt.strip().split("\n")
            if not any(line.startswith("data: ") for line in lines):
                continue

            try:
                event_name = next((line[7:] for line in lines if line.startswith("event: ")), "")
                evt_obj = parse_event_payload(lines)
                if evt_obj is None:
                    continue

                if event_name == "SSE_HEARTBEAT":
                    continue

                if event_name == "STREAM_ERROR":
                    error_code = evt_obj.get("error_code")
                    error_msg = str(evt_obj.get("error_msg") or "stream error").strip()
                    extra = decode_jsonish(evt_obj.get("extra"), max_depth=6)
                    decision = {}
                    if isinstance(extra, dict):
                        decision = decode_jsonish(extra.get("decision"), max_depth=6)
                        if not isinstance(decision, dict):
                            decision = {}
                    verify_scene = str(decision.get("verify_scene") or "").strip() or None
                    subtype = str(decision.get("subtype") or "").strip() or None
                    log_id = str(decision.get("log_id") or "").strip() or None
                    message = f"Doubao risk control: {error_msg}"
                    if error_code is not None:
                        message += f" ({error_code})"
                    details = []
                    if verify_scene:
                        details.append(f"scene={verify_scene}")
                    if subtype:
                        details.append(f"type={subtype}")
                    if log_id:
                        details.append(f"log_id={log_id}")
                    if details:
                        message += " [" + ", ".join(details) + "]"
                    raise VerifyRequiredException(
                        message=message,
                        error_code=error_code if isinstance(error_code, int) else None,
                        verify_scene=verify_scene,
                        subtype=subtype,
                        log_id=log_id,
                        conversation_id=conversation_id or None,
                        section_id=section_id or None,
                        message_id=message_id or None,
                    )

                if event_name == "SSE_ACK":
                    ack_meta = decode_jsonish(evt_obj.get("ack_client_meta"), max_depth=6)
                    if isinstance(ack_meta, dict):
                        ack_conversation_id = str(ack_meta.get("conversation_id") or "").strip()
                        ack_section_id = str(ack_meta.get("section_id") or "").strip()
                        conversation_info = decode_jsonish(ack_meta.get("conversation_info"), max_depth=6)
                        if not ack_section_id and isinstance(conversation_info, dict):
                            ack_section_id = str(conversation_info.get("last_section_id") or "").strip()
                        if ack_conversation_id:
                            conversation_id = ack_conversation_id
                        if ack_section_id:
                            section_id = ack_section_id
                    query_list = decode_jsonish(evt_obj.get("query_list"), max_depth=6)
                    if isinstance(query_list, list):
                        for item in query_list:
                            if not isinstance(item, dict):
                                continue
                            question_id = str(item.get("question_id") or "").strip()
                            if question_id:
                                message_id = question_id
                                break
                    continue

                if event_name == "FULL_MSG_NOTIFY":
                    message = decode_jsonish(evt_obj.get("message"), max_depth=6)
                    if not isinstance(message, dict):
                        message = {}
                    evt_conversation_id, evt_message_id, evt_section_id = extract_event_identifiers(message, message)
                    if evt_conversation_id:
                        conversation_id = evt_conversation_id
                    if evt_message_id:
                        message_id = evt_message_id
                    if evt_section_id:
                        section_id = evt_section_id
                    if str(message.get("user_type")) == "2":
                        text = extract_history_message_text(message)
                        if text:
                            full_text = text
                    continue

                if event_name == "STREAM_MSG_NOTIFY":
                    meta = decode_jsonish(evt_obj.get("meta"), max_depth=6)
                    if isinstance(meta, dict):
                        meta_conversation_id = str(meta.get("conversation_id") or "").strip()
                        meta_message_id = str(meta.get("message_id") or "").strip()
                        meta_section_id = str(meta.get("section_id") or "").strip()
                        if meta_conversation_id:
                            conversation_id = meta_conversation_id
                        if meta_message_id:
                            message_id = meta_message_id
                        if meta_section_id:
                            section_id = meta_section_id
                    text = extract_web_chat_notify_text(evt_obj)
                    if text:
                        append_text(text)
                    continue

                if event_name == "STREAM_CHUNK":
                    text = extract_stream_chunk_patch_text(evt_obj)
                    if text:
                        append_text(text)
                    continue

                if event_name == "CHUNK_DELTA":
                    text = extract_text_value(evt_obj.get("text"))
                    if text:
                        append_text(text)
                    continue

                if event_name == "SSE_REPLY_END":
                    end_type = evt_obj.get("end_type")
                    if end_type == 1:
                        finish_attr = decode_jsonish(evt_obj.get("msg_finish_attr"), max_depth=6)
                        if isinstance(finish_attr, dict):
                            brief = extract_text_value(finish_attr.get("brief"))
                            if brief:
                                full_text = brief
                            finish_message_id = str(finish_attr.get("msgid") or "").strip()
                            if finish_message_id:
                                message_id = finish_message_id
                    if end_type == 3:
                        text = "".join(texts) or full_text
                        text = text.lstrip("\n").rstrip("\n")
                        logger.debug(f"SSE web stream end: text={len(text)}, images={len(image_urls)}")
                        return text, image_urls, conversation_id, message_id, section_id
                    continue

                event_type = evt_obj.get("event_type")
                event_data = decode_jsonish(evt_obj.get("event_data", "{}"))
                if not isinstance(event_data, dict):
                    event_data = {}
                msg = decode_jsonish(event_data.get("message"), max_depth=6)
                if not isinstance(msg, dict):
                    msg = {}
                raw_events.append({
                    "event_type": event_type,
                    "event_data": event_data,
                    "message": msg,
                    "evt_obj": evt_obj,
                })

                evt_conversation_id, evt_message_id, evt_section_id = extract_event_identifiers(event_data, msg)
                if evt_conversation_id:
                    conversation_id = evt_conversation_id
                if evt_message_id:
                    message_id = evt_message_id
                if evt_section_id:
                    section_id = evt_section_id

                if event_type in (2001, 2005, 2010):
                    text, content_type, text_source = extract_sse_event_text(event_data, msg, evt_obj)
                    if text:
                        if event_type in (2005, 2010) or content_type == 9999:
                            full_text = text
                        else:
                            append_text(text)
                    elif content_type == 2074:
                        logger.debug("SSE skipped unsupported image-only payload")
                    elif event_type == 2010:
                        logger.debug("SSE ignored control/update event without text")
                    elif content_type is None and (evt_conversation_id or evt_message_id or evt_section_id):
                        logger.debug("SSE metadata event had no recoverable text")
                    else:
                        logger.debug(
                            f"SSE ignored empty legacy event: type={event_type}, content_type={content_type}, source={text_source or '-'}"
                        )
                elif event_type == 2002:
                    logger.debug(f"SSE ack legacy ids: conv={conversation_id}, msg={message_id}")
                elif event_type == 2003:
                    text = full_text or "".join(texts)
                    text = text.lstrip("\n").rstrip("\n")
                    if not text:
                        text = recover_sse_text_from_events(raw_events)
                        text = text.lstrip("\n").rstrip("\n")
                    logger.debug(f"SSE legacy stream end: text={len(text)}, images={len(image_urls)}")
                    return text, image_urls, conversation_id, message_id, section_id
                else:
                    logger.debug(f"SSE ignored unknown legacy event_type={event_type}")
            except (VerifyRequiredException, LimitedException):
                raise
            except Exception as e:
                raise Exception(f"SSE parsing failed: {str(e)}")

    buffer += decoder.decode(b"", final=True)
    text = full_text or "".join(texts) or recover_sse_text_from_events(raw_events)
    text = text.lstrip("\n").rstrip("\n")
    if text or conversation_id or message_id or section_id:
        logger.debug(f"SSE end-of-stream fallback: text={len(text)}, images={len(image_urls)}")
        return text, image_urls, conversation_id, message_id, section_id
    return "", image_urls, conversation_id, message_id, section_id


class _InMemoryStreamContent:
    def __init__(self, raw_text: str):
        self._data = str(raw_text or "").encode("utf-8")

    async def iter_chunked(self, chunk_size: int):
        size = max(1, int(chunk_size or 1024))
        for start in range(0, len(self._data), size):
            yield self._data[start:start + size]


class _InMemorySSELikeResponse:
    def __init__(self, raw_text: str):
        self.content = _InMemoryStreamContent(raw_text)


async def handle_sse_text(raw_text: str):
    return await handle_sse(_InMemorySSELikeResponse(raw_text))


async def chat_completion(
    prompt: str,
    guest: bool,
    session_mode: str | None = None,
    conversation_mode: str | None = None,
    section_id: str = None,
    conversation_id: str = None,
    attachments: list[dict] = [],
    think_mode: int | None = None,
    use_auto_cot: bool = False,
    use_deep_think: bool = False,
    session_override: dict | None = None,
):
    effective_guest = resolve_effective_guest_flag(guest, session_mode=session_mode)
    requested_conversation_id, requested_section_id, effective_conversation_mode = resolve_requested_chat_target(
        conversation_id=conversation_id,
        section_id=section_id,
        conversation_mode=conversation_mode,
    )
    session, session_meta = await ensure_service_session(
        requested_conversation_id=requested_conversation_id,
        effective_guest=effective_guest,
        session_override=session_override,
    )
    if (
        effective_guest
        and effective_conversation_mode == CONVERSATION_MODE_NEW
        and not session_meta.get("from_override")
    ):
        from .guest_session_service import maybe_rotate_guest_session_for_new_conversation

        rotation = await maybe_rotate_guest_session_for_new_conversation(
            session,
            open_verification_window=True,
        )
        if rotation is not None:
            raise HTTPException(
                status_code=409,
                detail=build_guest_session_rotated_detail(rotation),
            )
    conversation_info = None

    if requested_conversation_id is not None and not effective_guest:
        try:
            session, conversation_info = await fetch_conversation_info(requested_conversation_id, session=session)
            latest_section_id = conversation_info.get("last_section_id")
            if latest_section_id and not requested_section_id:
                requested_section_id = latest_section_id
        except Exception:
            if session is None or not requested_section_id:
                raise

    latest_index = None
    if conversation_info:
        raw_latest_index = conversation_info.get("latest_index")
        if raw_latest_index not in (None, ""):
            try:
                latest_index = int(raw_latest_index)
            except (TypeError, ValueError):
                latest_index = None

    effective_think_mode = resolve_think_mode(think_mode, use_auto_cot, use_deep_think)

    referer = "https://www.doubao.com/chat/"
    if requested_conversation_id:
        referer = f"https://www.doubao.com/chat/{requested_conversation_id}"

    previous_ms_token = getattr(session, "ms_token", None)
    previous_fp = getattr(session, "fp", None)
    previous_web_tab_id = getattr(session, "web_tab_id", None)
    previous_room_id = getattr(session, "room_id", None)
    browser_state_changed = False

    try:
        runtime_status_before_ready = browser_runtime.get_status()
        guest_manual_seed = effective_guest and should_seed_guest_runtime_from_manual(runtime_status_before_ready)
        seed_manual_storage = (not effective_guest) or guest_manual_seed
        should_force_fresh_guest_manual = should_start_fresh_guest_manual_verification(
            effective_guest=effective_guest,
            session=session,
            requested_conversation_id=requested_conversation_id,
            session_meta=session_meta,
        )
        initial_captured_chat_x_flow_trace = get_captured_chat_x_flow_trace(
            session_mode=session_mode,
            guest=effective_guest,
            session=session,
        )
        should_use_live_manual_send = (
            can_bootstrap_capture_via_manual_browser(runtime_status_before_ready)
            and not should_force_fresh_guest_manual
            and (
                not initial_captured_chat_x_flow_trace
                or should_route_guest_request_via_live_manual_send(
                    effective_guest=effective_guest,
                    request_mode=effective_conversation_mode,
                    session=session,
                    runtime_status=runtime_status_before_ready,
                )
            )
        )
        if should_use_live_manual_send:
            if (
                True
            ):
                manual_result = await send_manual_browser_message(
                    prompt=prompt,
                    guest=effective_guest,
                    conversation_id=requested_conversation_id,
                    session_override=session_override,
                )
                resolved_conversation_id = (
                    manual_result.get("conversation_id")
                    or requested_conversation_id
                    or getattr(session, "room_id", None)
                    or "0"
                )
                resolved_section_id = manual_result.get("section_id") or requested_section_id or "0"
                resolved_message_id = manual_result.get("message_id") or "0"
                response_text = manual_result.get("text")
                response_images = list(manual_result.get("img_urls") or [])
                completion_meta = (
                    dict(manual_result.get("completion_meta") or {})
                    if isinstance(manual_result.get("completion_meta"), dict)
                    else build_completion_response_meta(
                        request_mode=effective_conversation_mode,
                        text=response_text,
                        image_urls=response_images,
                        conversation_id=resolved_conversation_id,
                    )
                )
                if not response_text and not response_images and resolved_conversation_id == "0":
                    raise HTTPException(
                        status_code=502,
                        detail="Manual browser send finished but did not return reusable content or identifiers.",
                    )
                return (
                    response_text or "",
                    response_images,
                    resolved_conversation_id,
                    resolved_message_id,
                    resolved_section_id,
                    manual_result.get("session_params"),
                    completion_meta,
                )

        if not initial_captured_chat_x_flow_trace:
            manual_window_opened = False
            manual_open_url = None
            runtime_status_after_open = runtime_status_before_ready
            auto_launch_blocked = True
            if has_active_manual_browser_window(runtime_status_before_ready):
                manual_open_url = runtime_status_after_open.get("manual_current_url")
                if ((not effective_guest) or guest_manual_seed) and await browser_runtime.sync_live_manual_session(session):
                    browser_state_changed = True
                if sync_session_from_browser_runtime_state(session, runtime_status_after_open):
                    browser_state_changed = True
                if sync_session_from_manual_browser_capture(
                    session,
                    session_mode=session_mode,
                    guest=effective_guest,
                ):
                    browser_state_changed = True
                if browser_state_changed and not session_override:
                    session = persist_session_state(
                        session,
                        requested_conversation_id,
                        guest=effective_guest,
                    )
            raise HTTPException(
                status_code=409,
                detail=build_manual_verification_required_detail(
                    message=(
                        "Missing reusable chat capture. "
                        + (
                            "Automatic visible browser relaunch is paused; use the front-end verification button to reopen it, complete verification, and retry the same send."
                            if auto_launch_blocked
                            else "A visible verification window has been prepared; complete verification there and retry the same send."
                        )
                    ),
                    manual_window_opened=manual_window_opened,
                    open_url=manual_open_url,
                    runtime_status=runtime_status_after_open,
                ),
            )
        browser_state = await browser_runtime.ensure_ready(
            session,
            seed_manual_storage=seed_manual_storage,
        )
        manual_page_aligned = False
        manual_live_synced = False
        if not effective_guest:
            manual_page_aligned = await align_manual_browser_for_chat_request(
                session,
                requested_conversation_id,
                request_mode=effective_conversation_mode,
            )
            manual_live_synced = await browser_runtime.sync_live_manual_session(session)
        elif browser_runtime.is_manual_window_bound_to(session) and _has_live_manual_browser_session(browser_runtime.get_status()):
            manual_page_aligned = await align_manual_browser_for_chat_request(
                session,
                requested_conversation_id,
                request_mode=effective_conversation_mode,
            )
            manual_live_synced = await browser_runtime.sync_live_manual_session(session)
        elif guest_manual_seed:
            manual_live_synced = await browser_runtime.sync_live_manual_session(session)
        if browser_state.get("ms_token") and browser_state["ms_token"] != previous_ms_token:
            browser_state_changed = True
        if browser_state.get("fp") and browser_state["fp"] != previous_fp:
            browser_state_changed = True
        if sync_session_from_browser_runtime_state(session, browser_runtime.get_status()):
            browser_state_changed = True
        if getattr(session, "web_tab_id", None) != previous_web_tab_id:
            browser_state_changed = True
        manual_capture_synced = sync_session_from_manual_browser_capture(
            session,
            session_mode=session_mode,
            guest=effective_guest,
        )
        if manual_live_synced or manual_page_aligned:
            browser_state_changed = True
        prefer_manual_transport = should_prefer_manual_browser_transport(
            effective_guest=effective_guest,
            session_mode=session_mode,
            runtime_status=browser_runtime.get_status(),
            session=session,
        )
        manual_overrides = get_manual_browser_request_overrides(
            session_mode=session_mode,
            guest=effective_guest,
            session=session,
        )
        captured_chat_x_flow_trace = get_captured_chat_x_flow_trace(
            session_mode=session_mode,
            guest=effective_guest,
            session=session,
        )
        query_hints = get_manual_browser_chat_query_hints(
            session_mode=session_mode,
            guest=effective_guest,
            session=session,
        )
        body_hints = get_manual_browser_chat_body_hints(
            session_mode=session_mode,
            guest=effective_guest,
            session=session,
        )
        live_manual_url = str(browser_runtime.get_status().get("manual_current_url") or "").strip()
        referer = resolve_effective_chat_referer(
            requested_conversation_id=requested_conversation_id,
            manual_overrides=manual_overrides,
            live_manual_url=live_manual_url,
            request_mode=effective_conversation_mode,
        )
        if not captured_chat_x_flow_trace:
            raise HTTPException(
                status_code=409,
                detail=build_manual_verification_required_detail(
                    message=(
                        "Missing captured chat x-flow-trace header. "
                        "Complete verification in the visible browser window and retry; "
                        "the first successful live send will capture and persist the runtime data automatically."
                    ),
                    manual_window_opened=False,
                    open_url=None,
                    runtime_status=browser_runtime.get_status(),
                ),
            )
        body = build_web_chat_completion_body(
            prompt=prompt,
            think_mode=effective_think_mode,
            conversation_id=requested_conversation_id,
            section_id=requested_section_id,
            latest_index=latest_index,
            session=session,
            attachments=attachments,
            body_hints=body_hints,
        )

        response = None
        response_text = ""
        response_status = 0
        max_attempts = 2 if requested_conversation_id is not None and not effective_guest else 1
        if effective_guest:
            await request_get_nexpulse(
                session,
                referer,
                use_browser_transport=True,
                prefer_manual=prefer_manual_transport,
                seed_manual_storage=seed_manual_storage,
            )
        for attempt_index in range(max_attempts):
            await check_message_send_rate_limit(
                session,
                referer,
                use_browser_transport=True,
                prefer_manual=prefer_manual_transport,
                seed_manual_storage=seed_manual_storage,
            )
            unsigned_query = build_web_chat_completion_params(
                session,
                include_signature=False,
                query_hints=query_hints,
            )
            signed_query = None
            bogus = None
            try:
                signed_query, bogus = build_signed_query_from_capture_hint(unsigned_query, query_hints)
                if signed_query is None:
                    signed_query, bogus = await browser_runtime.sign_query(
                        session,
                        unsigned_query,
                        seed_manual_storage=seed_manual_storage,
                    )
            except Exception as exc:
                logger.debug(f"Failed to sign chat completion query, falling back to unsigned query: {exc}")
                signed_query = None
                bogus = None
            session.a_bogus = bogus
            url = "https://www.doubao.com/chat/completion?" + (signed_query or unsigned_query)
            headers = build_web_chat_completion_headers(
                session,
                referer,
                x_flow_trace=captured_chat_x_flow_trace,
            )
            browser_fetch_headers = build_browser_fetch_headers(headers)
            response = await browser_runtime.fetch_text(
                session,
                url=url,
                headers=browser_fetch_headers,
                body=body,
                referer=referer,
                prefer_manual=prefer_manual_transport,
                seed_manual_storage=seed_manual_storage,
            )
            response_headers = response.get("headers") if isinstance(response, dict) else {}
            if not isinstance(response_headers, dict):
                response_headers = {}
            x_ms_token = response_headers.get("x-ms-token")
            if x_ms_token:
                if x_ms_token != previous_ms_token:
                    browser_state_changed = True
                session.ms_token = x_ms_token
            if sync_session_from_manual_browser_capture(
                session,
                session_mode=session_mode,
                guest=effective_guest,
            ):
                browser_state_changed = True
            if manual_capture_synced and getattr(session, "web_tab_id", None) != previous_web_tab_id:
                browser_state_changed = True
            response_status = int(response.get("status") or 0) if isinstance(response, dict) else 0
            response_text = str(response.get("text") or "") if isinstance(response, dict) else ""
            if response_status == 200:
                break
            if (
                attempt_index + 1 < max_attempts
                and should_retry_auth_continuation_rate_limit(
                    response_status=response_status,
                    response_text=response_text,
                    effective_guest=effective_guest,
                    conversation_id=requested_conversation_id,
                )
            ):
                await asyncio.sleep(3)
                continue
            raise Exception(f"Doubao chat completion failed: {response_status}, detail: {response_text}")
        try:
            text, image_urls, response_conversation_id, message_id, response_section_id = await handle_sse_text(
                response_text
            )
            resolved_conversation_id, resolved_message_id, resolved_section_id = resolve_completion_identifiers(
                session=session,
                requested_conversation_id=requested_conversation_id,
                response_conversation_id=response_conversation_id,
                requested_section_id=requested_section_id,
                response_section_id=response_section_id,
                message_id=message_id,
                runtime_status=browser_runtime.get_status(),
                request_mode=effective_conversation_mode,
                previous_room_id=previous_room_id,
            )
            cache_conversation_id = None if resolved_conversation_id in {"", "0"} else resolved_conversation_id
            completion_meta = build_completion_response_meta(
                request_mode=effective_conversation_mode,
                text=text,
                image_urls=image_urls,
                conversation_id=resolved_conversation_id,
            )

            if not text and not image_urls and not cache_conversation_id:
                raise Exception("SSE did not return message content or conversation identifiers")

            if cache_conversation_id:
                session.room_id = cache_conversation_id
                session_pool.set_session(cache_conversation_id, session)
                cache_conversation_state(
                    cache_conversation_id,
                    section_id=resolved_section_id,
                    latest_index=latest_index,
                    override=True,
                )
            if getattr(session, "room_id", None) != previous_room_id:
                browser_state_changed = True
            if not session_meta.get("from_override"):
                if effective_guest:
                    session = persist_session_state(
                        session,
                        cache_conversation_id or requested_conversation_id,
                        guest=True,
                    )
                elif browser_state_changed:
                    session_pool.save_to_file(guest=False)

            return (
                text,
                image_urls,
                resolved_conversation_id,
                resolved_message_id,
                resolved_section_id,
                build_runtime_session_params(session),
                completion_meta,
            )
        except LimitedException:
            if effective_guest and not session_meta.get("from_override"):
                from .guest_session_service import reset_guest_session

                rotation = await reset_guest_session(
                    current_session=session,
                    reason="upstream_guest_limit",
                    open_verification_window=True,
                    wait_for_ready=False,
                )
                raise HTTPException(
                    status_code=409,
                    detail=build_guest_session_rotated_detail(rotation),
                )
            session_pool.del_session(session)
            raise HTTPException(status_code=500, detail="Guest conversation limit reached, please refresh session")
        except VerifyRequiredException as exc:
            runtime_status_after_error = browser_runtime.get_status()
            partial_conversation_id, partial_message_id, partial_section_id = resolve_completion_identifiers(
                session=session,
                requested_conversation_id=requested_conversation_id,
                response_conversation_id=_non_empty_text(getattr(exc, "conversation_id", None)),
                requested_section_id=requested_section_id,
                response_section_id=_non_empty_text(getattr(exc, "section_id", None)),
                message_id=_non_empty_text(getattr(exc, "message_id", None)),
                runtime_status=runtime_status_after_error,
                request_mode=effective_conversation_mode,
                previous_room_id=previous_room_id,
            )
            if partial_conversation_id not in {"", "0"}:
                exc.conversation_id = partial_conversation_id
                if partial_message_id not in {"", "0"}:
                    exc.message_id = partial_message_id
                if partial_section_id not in {"", "0"}:
                    exc.section_id = partial_section_id
                session.room_id = partial_conversation_id
                session_pool.set_session(partial_conversation_id, session)
                cache_conversation_state(
                    partial_conversation_id,
                    section_id=partial_section_id,
                    override=True,
                )
                if not session_meta.get("from_override"):
                    session = persist_session_state(
                        session,
                        partial_conversation_id,
                        guest=effective_guest,
                    )
            (
                runtime_status_after_error,
                manual_window_opened,
                manual_open_url,
            ) = await prepare_visible_verification_window_for_upstream_risk(
                session=session,
                effective_guest=effective_guest,
                session_mode=session_mode,
                request_mode=effective_conversation_mode,
                requested_conversation_id=partial_conversation_id or requested_conversation_id,
                session_meta=session_meta,
            )
            raise HTTPException(
                status_code=429,
                detail=build_upstream_verification_required_detail(
                    exc,
                    runtime_status=runtime_status_after_error,
                    manual_window_opened=manual_window_opened,
                    open_url=manual_open_url,
                ),
            )
        except HTTPException:
            raise
    except HTTPException:
        raise
    except Exception as e:
        raise Exception(f"Doubao request failed: {str(e)}")

async def upload_file(file_type: int, file_name: str, file_data: bytes):
    """
    ??/?????????????????
    ???????????????????????????
    """
    logger.warning("upload_file ???????/?????????")
    raise HTTPException(status_code=410, detail="??/?????????")


async def delete_conversation(conversation_id: str) -> tuple[bool, str]:
    session = session_pool.get_session(conversation_id)
    if not session:
        try:
            session, _ = await fetch_conversation_info(conversation_id)
        except Exception:
            raise HTTPException(status_code=404, detail=f"???????:, ??ID: {conversation_id}")

    params = "&".join([
        f"aid={session.aid}",
        f"device_id={session.device_id}",
        "device_platform=web",
        "language=zh",
        f"pc_version={PC_VERSION}",
        "pkg_type=release_version",
        f"real_aid={session.resolved_real_aid}",
        "region=CN",
        "samantha_web=1",
        "sys_region=CN",
        f"tea_uuid={session.tea_uuid}",
        "use-olympus-account=1",
        "version_code=20800",
        f"web_id={session.web_id}",
    ])
    url = "https://www.doubao.com/samantha/thread/delete?" + params
    body = {"conversation_id": conversation_id}
    headers = {
        "cookie": session.cookie,
        "origin": "https://www.doubao.com",
        "referer": "https://www.doubao.com/chat/" + conversation_id,
        "user-agent": BROWSER_UA,
    }

    try:
        async with aiohttp.ClientSession() as aio_session:
            async with aio_session.post(url, headers=headers, json=body) as response:
                if response.status != 200:
                    return False, f"??????: {response.status}"
        return True, ""
    except Exception as e:
        return False, f"????: {str(e)}"


class LimitedException(Exception):
    pass


class VerifyRequiredException(Exception):
    def __init__(
        self,
        message: str,
        error_code: int | None = None,
        verify_scene: str | None = None,
        subtype: str | None = None,
        log_id: str | None = None,
        conversation_id: str | None = None,
        section_id: str | None = None,
        message_id: str | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.verify_scene = verify_scene
        self.subtype = subtype
        self.log_id = log_id
        self.conversation_id = conversation_id
        self.section_id = section_id
        self.message_id = message_id


__all__ = [
    "chat_completion",
    "refresh_session_runtime",
    "get_runtime_status",
    "get_browser_capture",
    "preview_chat_completion_request",
    "start_manual_verification",
    "finish_manual_verification",
    "send_manual_browser_message",
    "get_conversation_info",
    "get_conversation_messages",
    "upload_file",
    "delete_conversation"
] 
