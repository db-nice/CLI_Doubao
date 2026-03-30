#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
豆包 API 客户端 - 支持 GUI 与命令行调用
自动初始化会话，每个任务返回 conversation_id
"""

import sys
import argparse
import threading
import queue
import requests
import json
import pickle
import os
import re
import subprocess
import tempfile
import time
import hashlib
import base64
from glob import glob
from typing import Optional, Tuple, List, Dict, Any
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

# ---------- 配置 ----------
BASE_URL = "http://127.0.0.1:8100"
CHAT_ENDPOINT = f"{BASE_URL}/api/chat/completions"
CHAT_ENDPOINT_NEW = f"{CHAT_ENDPOINT}/new"
CHAT_ENDPOINT_CONTINUE = f"{CHAT_ENDPOINT}/continue"
SESSION_FILE = "chat_sessions.pkl"
CALL_CHAIN_ROUTE_FILE = "call_chain_routes.json"
ROLE_TAKEOVER_FILE = "role_takeover_routes.json"
THREAD_GROUP_FILE = "thread_groups.json"
CHAIN_BACKGROUND_FILE = "call_chain_backgrounds.json"
DEFAULT_TOOLCHAIN_SEPARATE_CONVERSATIONS = False
DEFAULT_ROLE_PROMPT_REFRESH_TURNS = 5
DEFAULT_AUTO_FOLLOWUP_AFTER_EXEC = True
DEFAULT_JADX_FORCE_NEW_MEETING = True
DEFAULT_MIRROR_JADX_TO_RENAME = True
DEFAULT_STRICT_TUNING_MESSAGE_SCOPE = True
DEFAULT_NAMING_DEDUP_MODE = "manual"
DEFAULT_RENAME_APPLY_MODE = "hybrid"
DEFAULT_CHAT_REQUEST_TIMEOUT_SEC = 90.0
DEFAULT_CHAT_CONNECT_TIMEOUT_SEC = 15.0
RESET_CHAIN_SENTINEL = "__USE_RESOLVED_CALL_CHAIN__"

CHAIN_TOOLCHAIN_SLOTS = [
    {"profile": "tool_dispatch", "command_type": "chain_root", "toolchain_key": "chain_root"},
    {"profile": "xref_analysis", "command_type": "method_xref", "toolchain_key": "baksmali_xref_chain"},
    {"profile": "xref_analysis", "command_type": "subclass_scan", "toolchain_key": "baksmali_subclasses_chain"},
    {"profile": "jadx_code_analyst", "command_type": "jadx_decompile", "toolchain_key": "jadx_source_chain"},
    {"profile": "rename_mapping", "command_type": "rename_mapping", "toolchain_key": "rename_followup_chain"},
    {"profile": "rename_tuning", "command_type": "rename_tuning", "toolchain_key": "rename_tuning_chain"},
    {"profile": "jadx_code_analyst", "command_type": "jadx_named_decompile", "toolchain_key": "jadx_named_chain"},
    {"profile": "tool_dispatch", "command_type": "smali_conversion", "toolchain_key": "smali_conversion_chain"},
    {"profile": "tool_dispatch", "command_type": "powershell_probe", "toolchain_key": "powershell_probe_chain"},
]
DEFAULT_CHILD_THREAD_TOOLCHAINS = {
    item["toolchain_key"]
    for item in CHAIN_TOOLCHAIN_SLOTS
    if item.get("toolchain_key") and item.get("toolchain_key") != "chain_root"
}
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LINKED_APP_DIR = r"D:\bot\doubao\cli_A"
APP_DIR_ENV_KEYS = ("DOUBAO_CLI_APP_DIR", "DOUBAO_CLI_ASSET_DIR")


def _normalize_bootstrap_path(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.abspath(path).rstrip("\\/")
    except Exception:
        return str(path).rstrip("\\/")


def _append_unique_bootstrap_path(items: List[str], candidate: str) -> None:
    normalized = _normalize_bootstrap_path(candidate)
    lowered = normalized.lower()
    if not normalized:
        return
    if any(existing.lower() == lowered for existing in items):
        return
    items.append(normalized)


def _looks_like_cli_asset_dir(path: str) -> bool:
    normalized = _normalize_bootstrap_path(path)
    if not normalized or not os.path.isdir(normalized):
        return False
    return bool(
        os.path.isfile(os.path.join(normalized, "cli_chat.py"))
        and os.path.isfile(os.path.join(normalized, "tool_dispatcher.py"))
        and os.path.isdir(os.path.join(normalized, "tools"))
    )


def discover_asset_app_dir() -> str:
    candidates: List[str] = []
    for env_key in APP_DIR_ENV_KEYS:
        _append_unique_bootstrap_path(candidates, os.environ.get(env_key, ""))
    if os.path.basename(SCRIPT_DIR).lower() == "_cli_patch":
        _append_unique_bootstrap_path(candidates, DEFAULT_LINKED_APP_DIR)
    _append_unique_bootstrap_path(candidates, SCRIPT_DIR)
    for candidate in candidates:
        if _looks_like_cli_asset_dir(candidate):
            return candidate
    return _normalize_bootstrap_path(SCRIPT_DIR)


APP_DIR = discover_asset_app_dir()


def resolve_helper_script_path(filename: str) -> str:
    local_path = os.path.join(SCRIPT_DIR, filename)
    if os.path.isfile(local_path):
        return local_path
    return os.path.join(APP_DIR, filename)


def iter_tool_dir_candidates() -> List[str]:
    candidates: List[str] = []
    _append_unique_bootstrap_path(candidates, os.path.join(APP_DIR, "tools"))
    _append_unique_bootstrap_path(candidates, os.path.join(SCRIPT_DIR, "tools"))
    return candidates


TOOL_DIR_CANDIDATES = iter_tool_dir_candidates()
TOOLS_DIR = TOOL_DIR_CANDIDATES[0] if TOOL_DIR_CANDIDATES else os.path.join(APP_DIR, "tools")
CLASS_HIT_STORE_DIR = os.path.join(APP_DIR, "class_hit_store")
DEFAULT_CHAIN_OUTPUT_ROOT = os.path.join(APP_DIR, "call_chain_runs")
DEFAULT_CHAIN_INDEX_ROOT = os.path.join(APP_DIR, "call_chain_index")
WORKSPACE_ROOT = os.environ.get("SMALI_MASTER_ROOT", "")
STALE_RUNTIME_MARKERS = (
    "\\_cli_patch\\runtime_test",
    "/_cli_patch/runtime_test",
)
LOCAL_RUNTIME_KEYS = (
    "smali_dir",
    "jadx_out",
    "call_chain_output_root",
    "call_chain_index_root",
)
DEFAULT_LOCAL_THINK_MODE = 1
ERROR_LOCAL_THINK_MODE = 0
JADX_LOCAL_THINK_MODE = 3
DEFAULT_INIT_MSG = "初始化逆向命令协议"
DEFAULT_RESUME_MSG = "继续当前调用链任务，按已分配角色和工具链接管。"
JSON_BLOCK_PATTERN = re.compile(r"```json\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)
SMALI_DESCRIPTOR_PATTERN = re.compile(r"L[\w/$.-]+;")
SMALI_METHOD_DESCRIPTOR_PATTERN = re.compile(r"L[\w/$.-]+;->[\w$<>-]+\([^)]*\)[^\s\"']*")
SMALI_FIELD_DESCRIPTOR_PATTERN = re.compile(r"L[\w/$.-]+;->[\w$<>-]+:[^\s\"']+")
WINDOWS_PATH_PATTERN = re.compile(r"[A-Za-z]:\\[^\s\"']+")
JAVA_CLASS_PATTERN = re.compile(r"\b(?:[A-Za-z_]\w*\.){1,}[A-Z_$][\w$]*\b")
TOOL_WORD_PATTERN = re.compile(r"\b(?:xref|subclasses|jadx|jadx_toolchain|tool_dispatcher|start_powershell|smali_converter|baksmali|smali)\b", re.IGNORECASE)
CODE_BLOCK_PATTERN = re.compile(r"```[a-zA-Z0-9_-]*\s*(.*?)\s*```", re.DOTALL)
RUNTIME_REPLY_META_LINE_PATTERN = re.compile(
    r"^\s*(?:conversation_id|section_id|message_id|messageg_id|reply_id|local_message_id|local_conversation_id)\s*[:=]",
    re.IGNORECASE,
)
DEFAULT_PROFILE = "tool_dispatch"
COMMON_RULE_LINES = [
    "只输出```json```代码块。",
    "不要输出MD统计、表格、伪C、代码转译。",
    "只返回当前工具、执行命令、reason、store_back、next_query。",
    "需要实际执行时，命令要可被 tool_dispatcher.py 直接转发。",
    "命令字符串使用Unicode转义，方便安全解析。",
    "只翻译混淆字段；已去混淆字段不要改。",
    "优先围绕当前方法和子依赖做局部分析，不要全局扫描整个 dex。",
    "如果已提供调用链或本地线程组，只沿用当前 local_thread 和角色卡，不要讨论底层会话ID。",
    "路径、输出目录、工作区都优先使用初始化配置生成的运行时别名，不要写死本地绝对路径。",
    "执行前先判断目录和文件是否存在；需要创建目录时只允许落在 allowed_roots 内。",
    "如果本轮已经形成命名候选，可附带 naming_candidates 或 candidate_names；新名必须避开已记录重名。",
    "如果返回命名映射，优先附带 naming_assignments=[{\"kind\":\"class|method|field\",\"source\":\"旧类/旧方法/旧字段描述\",\"candidate\":\"新名称\",\"owner\":\"Lxx/yy;\"}]，便于本地校验同对象复用。",
    "命名微调阶段不要主动改代码，也不要直接输出去混淆后的整段代码，先返回字段/方法/类的对应关系。",
    "命名微调、smali 改名和 named JADX 收尾阶段，只能使用本条消息里明确出现的 source、owner、candidate，不要根据上下文脑补新的类、方法、字段。",
    "如果要写回 analysis_pkg 或本地新文件，只能围绕本条消息确认过的 naming_assignments 做局部替换，不要补写新的方法、字段或控制流。",
    "baksmali 没有 modify 子命令；如果要执行 smali 改名，先确保 {chain_smali_dir} 已准备好，再使用 python {smali_converter} apply-renames ... 这条路线。",
    "回程字段固定 [OyUt]{旧的路径}{[片段信息]}。",
    "结尾固定 { [ECSA] [现在是用户的回合] }。",
]
PROMPT_PROFILES: Dict[str, Dict[str, Any]] = {
    "tool_dispatch": {
        "label": "命令调度",
        "keywords": ["命令", "工具", "执行", "powershell", "start_powershell", "检索", "调用", "json"],
        "preferred_tools": ["jadx_toolchain", "jadx", "baksmali", "start_powershell", "smali_converter"],
        "init_focus": [
            "先判断该调用哪个工具，再拼可直接执行的命令。",
            "信息不足时，只返回下一条检索命令。",
        ],
        "turn_focus": [
            "优先输出 powershell.exe 或 python 可直接执行命令。",
            "不要给结论分析，先把工具链跑通。",
        ],
    },
    "xref_analysis": {
        "label": "链路分析",
        "keywords": ["引用", "交叉引用", "调用链", "依赖", "解密", "加密", "混淆", "xref", "trace", "flow"],
        "preferred_tools": ["baksmali", "jadx_toolchain", "jadx", "start_powershell"],
        "init_focus": [
            "专注证据链、交叉引用、调用方向、依赖边。",
            "先找方法/类/字段命中，再决定下一跳。",
        ],
        "turn_focus": [
            "输出 xref、subclasses、jadx 检索命令。",
            "如果证据不足，只给继续追踪的命令。",
        ],
    },
    "rename_mapping": {
        "label": "重命名映射",
        "keywords": ["重命名", "rename", "替换", "旧字段", "新字段", "映射", "批量", "replace"],
        "preferred_tools": ["baksmali", "smali_converter", "jadx_toolchain", "start_powershell"],
        "init_focus": [
            "专注旧字段/新字段映射与替换顺序。",
            "只对混淆字段给新名，保留首次命中证据。",
        ],
        "turn_focus": [
            "每次输出可执行替换命令或继续检索命令。",
            "保持旧名、新名、命中次数一致。",
            "如果给出命名候选，确保方法名/类名/字段名不重复，可返回 naming_candidates。",
        ],
    },
    "rename_tuning": {
        "label": "命名微调",
        "keywords": ["微调", "冲突", "重命名校验", "命名校验", "字段冲突", "方法冲突", "类名冲突", "naming_assignments"],
        "preferred_tools": ["jadx_toolchain", "baksmali", "smali_converter", "start_powershell"],
        "init_focus": [
            "只整理旧字段/旧方法/旧类到新名称的映射，不主动输出改写后的代码。",
            "允许同一个 source 复用同一个 candidate；不同 source 不能撞到同一个新名字。",
            "严格按本条消息里出现的 source、owner、candidate 做校验，不要借用上下文补全新的类或方法。",
        ],
        "turn_focus": [
            "优先返回 naming_assignments 和 naming_candidates，保留 source、owner、candidate。",
            "如果本地提示有撞名或 source 身份不清，重新生成唯一名称，不要展开整段去混淆代码。",
            "保留同对象同名复用的命中统计，不要为了去重抹掉历史命中；最终是否去重由运行时配置决定。",
        ],
    },
    "naming_finalize": {
        "label": "结构命名",
        "keywords": ["命名", "类名", "方法名", "字段名", "角色卡", "用途", "结构", "规范", "jadx类", "final"],
        "preferred_tools": ["jadx_toolchain", "jadx", "baksmali", "start_powershell", "smali_converter"],
        "init_focus": [
            "专注类/方法/字段的语义命名与角色定位。",
            "根据用途、库归属、调用位置给稳定命名。",
        ],
        "turn_focus": [
            "优先给命名所需的检索命令，不输出伪C。",
            "命名一旦确定，要和前文保持一致。",
            "如果运行时 dedup 还未开启，先保留命中统计和映射，不要提前把同对象复用也去掉。",
        ],
    },
    "jadx_code_analyst": {
        "label": "JADX分析员",
        "keywords": ["jadx分析员", "代码涵义", "语义还原", "还原文件名", "源码语义", "控制流", "jadx代码", "分析代码"],
        "preferred_tools": ["jadx_toolchain", "jadx", "start_powershell", "baksmali", "smali_converter"],
        "init_focus": [
            "扮演代码JADX分析员，专注代码语义、控制流、字段用途、真实文件名还原。",
            "先拿到类代码或检索命令，再根据代码含义还原文件名与角色。",
        ],
        "turn_focus": [
            "根据代码涵义推动检索，不只盯着重命名表。",
            "优先输出拿到JADX类源码、依赖类、调用位置的命令。",
            "如果已经形成语义命名候选，可返回 naming_candidates 供主线程和重命名线程复用。",
            "在 named JADX 收尾阶段，只能基于本条消息确认的 naming_assignments 写回 analysis_pkg，不要脑补新的类成员或方法实现。",
        ],
    },
}


def find_latest_file(patterns: List[str]) -> str:
    latest_path = ""
    latest_mtime = -1.0
    for pattern in patterns:
        for path in glob(pattern):
            if not os.path.isfile(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_path = path
                latest_mtime = mtime
    return latest_path


def get_chat_request_timeout_sec() -> float:
    raw_value = str(
        os.environ.get(
            "DOUBAO_HTTP_STREAM_IDLE_TIMEOUT_SEC",
            os.environ.get("DOUBAO_HTTP_TIMEOUT_SEC", DEFAULT_CHAT_REQUEST_TIMEOUT_SEC),
        )
        or DEFAULT_CHAT_REQUEST_TIMEOUT_SEC
    ).strip()
    try:
        timeout_value = float(raw_value)
    except Exception:
        timeout_value = float(DEFAULT_CHAT_REQUEST_TIMEOUT_SEC)
    return max(30.0, timeout_value)


def get_chat_connect_timeout_sec() -> float:
    raw_value = str(os.environ.get("DOUBAO_HTTP_CONNECT_TIMEOUT_SEC", DEFAULT_CHAT_CONNECT_TIMEOUT_SEC) or DEFAULT_CHAT_CONNECT_TIMEOUT_SEC).strip()
    try:
        timeout_value = float(raw_value)
    except Exception:
        timeout_value = float(DEFAULT_CHAT_CONNECT_TIMEOUT_SEC)
    return max(5.0, min(timeout_value, 60.0))


def get_chat_request_timeout_tuple() -> Tuple[float, float]:
    return get_chat_connect_timeout_sec(), get_chat_request_timeout_sec()


def find_first_existing(patterns: List[str]) -> str:
    for pattern in patterns:
        matches = sorted(glob(pattern, recursive=True))
        for path in matches:
            if os.path.isfile(path):
                return path
    return ""


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def read_text_flexible(path: str) -> str:
    with open(path, "rb") as handle:
        data = handle.read()
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def decode_base64_text(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    padding = (-len(raw)) % 4
    if padding:
        raw += "=" * padding
    data = base64.b64decode(raw.encode("utf-8"), validate=False)
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def decode_unicode_escape_sequences(text: str) -> str:
    value = str(text or "")
    if "\\u" not in value:
        return value
    def repl(match: re.Match) -> str:
        try:
            return chr(int(str(match.group(1) or "0"), 16))
        except Exception:
            return match.group(0)
    return re.sub(r"\\u([0-9a-fA-F]{4})", repl, value)


def guess_workspace_root_from_target(target_path: str) -> str:
    raw = str(target_path or "").strip()
    if not raw:
        return ""
    absolute_target = os.path.abspath(raw)
    current = absolute_target if os.path.isdir(absolute_target) else os.path.dirname(absolute_target)
    if not current:
        return ""
    markers = ("gradlew", "gradlew.bat", "settings.gradle", "settings.gradle.kts", ".git")
    best = ""
    for _ in range(10):
        if any(os.path.exists(os.path.join(current, marker)) for marker in markers):
            best = current
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        current = parent
    return best or (absolute_target if os.path.isdir(absolute_target) else os.path.dirname(absolute_target))


def normalize_local_path(path: str) -> str:
    if not path:
        return ""
    return _normalize_bootstrap_path(path)


def iter_stale_local_runtime_roots() -> List[str]:
    if normalize_local_path(APP_DIR).lower() == normalize_local_path(SCRIPT_DIR).lower():
        return []
    candidates: List[str] = []
    for raw_path in (
        os.path.join(SCRIPT_DIR, "runtime"),
        os.path.join(SCRIPT_DIR, "call_chain_runs"),
        os.path.join(SCRIPT_DIR, "call_chain_index"),
    ):
        normalized = normalize_local_path(raw_path)
        if normalized:
            candidates.append(normalized)
    return candidates


def path_points_to_patch_runtime(path: str) -> bool:
    lowered = normalize_local_path(path).lower()
    if any(marker in lowered for marker in STALE_RUNTIME_MARKERS):
        return True
    for root in iter_stale_local_runtime_roots():
        lowered_root = root.lower()
        if lowered == lowered_root or lowered.startswith(lowered_root + "\\") or lowered.startswith(lowered_root + "/"):
            return True
    return False


def localize_tool_dispatcher_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    localized = dict(config or {})
    runtime_root = os.path.join(APP_DIR, "runtime")
    local_defaults = {
        "smali_dir": os.path.join(runtime_root, "smali_out"),
        "jadx_out": os.path.join(runtime_root, "jadx_out"),
        "call_chain_output_root": DEFAULT_CHAIN_OUTPUT_ROOT,
        "call_chain_index_root": DEFAULT_CHAIN_INDEX_ROOT,
    }
    for key, default_value in local_defaults.items():
        current = str(localized.get(key, "") or "")
        if not current or path_points_to_patch_runtime(current):
            localized[key] = default_value

    workspace_root = str(localized.get("workspace_root", "") or "").strip()
    if not workspace_root:
        guessed_root = guess_workspace_root_from_target(str(localized.get("target_dex", "") or ""))
        if guessed_root:
            localized["workspace_root"] = guessed_root

    roots: List[str] = []
    raw_roots = localized.get("allowed_roots", [])
    if isinstance(raw_roots, list):
        for raw_path in raw_roots:
            normalized = normalize_local_path(str(raw_path or ""))
            if not normalized or path_points_to_patch_runtime(normalized):
                continue
            roots.append(normalized)
    roots.extend([
        APP_DIR,
        SCRIPT_DIR,
        runtime_root,
        str(localized.get("smali_dir", "") or ""),
        str(localized.get("jadx_out", "") or ""),
        str(localized.get("call_chain_output_root", "") or ""),
        str(localized.get("call_chain_index_root", "") or ""),
        str(localized.get("workspace_root", "") or ""),
    ])
    target_dex = str(localized.get("target_dex", "") or "")
    if target_dex:
        roots.append(os.path.dirname(os.path.abspath(target_dex)))
    localized["allowed_roots"] = unique_keep_order(
        [normalize_local_path(path) for path in roots if normalize_local_path(path)],
        limit=32,
    )
    return localized


def iter_workspace_root_candidates(config: Optional[Dict[str, Any]] = None) -> List[str]:
    config = config if isinstance(config, dict) else {}
    raw_candidates: List[str] = []
    explicit = str(config.get("workspace_root", "") or WORKSPACE_ROOT or "").strip()
    if explicit:
        raw_candidates.append(explicit)

    target_dex = str(config.get("target_dex", "") or "")
    if target_dex:
        current = os.path.abspath(target_dex)
        current = current if os.path.isdir(current) else os.path.dirname(current)
        for _ in range(8):
            if not current:
                break
            raw_candidates.append(current)
            parent = os.path.dirname(current)
            if not parent or parent == current:
                break
            current = parent
        guessed_root = guess_workspace_root_from_target(target_dex)
        if guessed_root:
            raw_candidates.append(guessed_root)

    current = os.getcwd()
    for _ in range(6):
        if not current:
            break
        raw_candidates.append(current)
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        current = parent
    raw_candidates.append(APP_DIR)
    raw_candidates.append(SCRIPT_DIR)

    result: List[str] = []
    seen: set[str] = set()

    def add_candidate(candidate: str) -> None:
        normalized = normalize_local_path(candidate)
        lowered = normalized.lower()
        if not normalized or lowered in seen:
            return
        seen.add(lowered)
        result.append(normalized)

    for candidate in raw_candidates:
        normalized = normalize_local_path(candidate)
        if not normalized:
            continue
        child = os.path.join(normalized, "smali-master")
        if os.path.isdir(child):
            add_candidate(child)
        add_candidate(normalized)
    return result


def resolve_workspace_root(config: Optional[Dict[str, Any]] = None) -> str:
    candidates = iter_workspace_root_candidates(config)
    for candidate in candidates:
        if os.path.isdir(os.path.join(candidate, "baksmali")) or os.path.isdir(os.path.join(candidate, "smali")):
            return candidate
    return candidates[0] if candidates else APP_DIR


def build_workspace_patterns(config: Optional[Dict[str, Any]] = None, *parts: str) -> List[str]:
    return [os.path.join(root, *parts) for root in iter_workspace_root_candidates(config)]


def safe_slug(text: str, limit: int = 48) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("._")
    if not cleaned:
        cleaned = "item"
    return cleaned[:limit]


def normalize_class_key(value: str) -> str:
    raw = (value or "").strip().strip("\"'`")
    if not raw:
        return ""
    if re.match(r"^[A-Za-z]:\\", raw):
        return ""
    if raw.startswith("L") and raw.endswith(";"):
        body = raw[1:-1].replace(".", "/")
        return f"L{body};"
    if raw.startswith("L") and "." in raw and "/" not in raw:
        body = raw[1:]
        if body.endswith(";"):
            body = body[:-1]
        return f"L{body.replace('.', '/')};"
    if raw.endswith(".java"):
        raw = raw[:-5]
    if raw.endswith(".smali"):
        raw = raw[:-6]
    if "\\" in raw:
        raw = raw.replace("\\", "/")
    if raw.startswith("/"):
        raw = raw[1:]
    if "/" in raw and not raw.startswith("L"):
        return f"L{raw};"
    if "." in raw and "/" not in raw:
        return f"L{raw.replace('.', '/')};"
    return raw


def extract_method_targets(text: str, limit: int = 12) -> List[str]:
    if not text:
        return []
    return unique_keep_order(SMALI_METHOD_DESCRIPTOR_PATTERN.findall(text), limit=limit)


def extract_class_targets(text: str, limit: int = 12) -> List[str]:
    if not text:
        return []
    results: List[str] = []
    for method_target in extract_method_targets(text, limit=limit):
        owner = method_target.split("->", 1)[0]
        normalized = normalize_class_key(owner)
        if normalized:
            results.append(normalized)
    for raw in SMALI_DESCRIPTOR_PATTERN.findall(text):
        normalized = normalize_class_key(raw)
        if normalized:
            results.append(normalized)
    for raw in JAVA_CLASS_PATTERN.findall(text):
        normalized = normalize_class_key(raw)
        if normalized:
            results.append(normalized)
    return unique_keep_order(results, limit=limit)


def extract_field_targets(text: str, limit: int = 12) -> List[str]:
    if not text:
        return []
    return unique_keep_order(SMALI_FIELD_DESCRIPTOR_PATTERN.findall(text), limit=limit)


def normalize_call_chain(chain_text: str) -> str:
    if not chain_text:
        return ""
    lines = []
    for line in chain_text.replace("\r", "\n").split("\n"):
        clean = " ".join(line.strip().split())
        if clean:
            lines.append(clean)
    return "\n".join(lines)


def chain_route_path() -> str:
    return os.path.join(APP_DIR, CALL_CHAIN_ROUTE_FILE)


def load_chain_route_store() -> Dict[str, Any]:
    path = chain_route_path()
    if not os.path.exists(path):
        return {"routes": {}, "reset_marks": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("routes", {})
                data.setdefault("reset_marks", {})
                return data
    except Exception:
        pass
    return {"routes": {}, "reset_marks": {}}


def save_chain_route_store(data: Dict[str, Any]) -> str:
    path = chain_route_path()
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return path


def role_takeover_path() -> str:
    return os.path.join(APP_DIR, ROLE_TAKEOVER_FILE)


def load_role_takeover_store() -> Dict[str, Any]:
    path = role_takeover_path()
    if not os.path.exists(path):
        return {"routes": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("routes", {})
                return data
    except Exception:
        pass
    return {"routes": {}}


def save_role_takeover_store(data: Dict[str, Any]) -> str:
    path = role_takeover_path()
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return path


def thread_group_path() -> str:
    return os.path.join(APP_DIR, THREAD_GROUP_FILE)


def load_thread_group_store() -> Dict[str, Any]:
    path = thread_group_path()
    if not os.path.exists(path):
        return {"next_group_index": 1, "groups": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("next_group_index", 1)
                data.setdefault("groups", {})
                return data
    except Exception:
        pass
    return {"next_group_index": 1, "groups": {}}


def save_thread_group_store(data: Dict[str, Any]) -> str:
    path = thread_group_path()
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return path


def chain_background_path() -> str:
    return os.path.join(APP_DIR, CHAIN_BACKGROUND_FILE)


def load_chain_background_store() -> Dict[str, Any]:
    path = chain_background_path()
    if not os.path.exists(path):
        return {"chains": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            if isinstance(data, dict):
                data.setdefault("chains", {})
                return data
    except Exception:
        pass
    return {"chains": {}}


def save_chain_background_store(data: Dict[str, Any]) -> str:
    path = chain_background_path()
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return path


def reset_call_chain_state(chain_text: str,
                           drop_sessions: bool = True,
                           conversation_manager: Optional["ConversationManager"] = None) -> Dict[str, Any]:
    normalized = normalize_call_chain(chain_text)
    if not normalized:
        return {"ok": False, "reason": "empty_call_chain"}

    chain_key = chain_route_key(normalized)
    route_store = load_chain_route_store()
    route_entry = {}
    if isinstance(route_store.get("routes"), dict):
        route_entry = dict(route_store["routes"].pop(chain_key, {}) or {})

    takeover_store = load_role_takeover_store()
    removed_takeovers: List[str] = []
    if isinstance(takeover_store.get("routes"), dict):
        for key, item in list(takeover_store["routes"].items()):
            if isinstance(item, dict) and str(item.get("chain_key", "") or "") == chain_key:
                removed_takeovers.append(str(key))
                takeover_store["routes"].pop(key, None)
        save_role_takeover_store(takeover_store)

    thread_store = load_thread_group_store()
    removed_group = {}
    if isinstance(thread_store.get("groups"), dict):
        removed_group = dict(thread_store["groups"].pop(chain_key, {}) or {})
        save_thread_group_store(thread_store)

    removed_sessions: List[str] = []
    base_session_name = base_chain_session_name(str(route_entry.get("session_name", "") or removed_group.get("base_session_name", "") or ""))
    if drop_sessions:
        conv_manager = conversation_manager or ConversationManager(storage_file=os.path.join(APP_DIR, SESSION_FILE))
        for session_name in list(conv_manager.sessions.keys()):
            if not base_session_name:
                break
            if session_name == base_session_name or session_name.startswith(base_session_name + "__"):
                removed_sessions.append(session_name)
                conv_manager.delete(session_name)

    reset_mark = {}
    if isinstance(route_store, dict):
        marks = route_store.setdefault("reset_marks", {})
        if not isinstance(marks, dict):
            marks = {}
            route_store["reset_marks"] = marks
        previous_mark = marks.get(chain_key, {}) if isinstance(marks.get(chain_key, {}), dict) else {}
        generation = int(previous_mark.get("generation", 0) or 0) + 1
        reset_mark = {
            "chain_key": chain_key,
            "chain_text": normalized,
            "generation": generation,
            "pending": True,
            "updated_at": time.time(),
            "last_session_name": base_session_name,
        }
        marks[chain_key] = reset_mark
        save_chain_route_store(route_store)

    return {
        "ok": True,
        "call_chain_text": normalized,
        "chain_key": chain_key,
        "group_ref": str(removed_group.get("group_ref", "") or ""),
        "base_session_name": base_session_name,
        "new_generation": int(reset_mark.get("generation", 0) or 0),
        "pending_new_meeting": bool(reset_mark.get("pending")),
        "removed_takeover_routes": removed_takeovers,
        "removed_sessions": removed_sessions,
    }


def resolve_thread_slot_by_refs(group_ref: str = "", slot_ref: str = "") -> Dict[str, Any]:
    if not group_ref and not slot_ref:
        return {}
    store = load_thread_group_store()
    groups = store.get("groups", {}) if isinstance(store, dict) else {}
    if not isinstance(groups, dict):
        return {}
    for group in groups.values():
        if not isinstance(group, dict):
            continue
        current_group_ref = str(group.get("group_ref", "") or "")
        if group_ref and current_group_ref != group_ref:
            continue
        slots = group.get("slots", {})
        if not isinstance(slots, dict):
            continue
        for slot_key, slot in slots.items():
            if not isinstance(slot, dict):
                continue
            current_slot_ref = str(slot.get("slot_ref", "") or "")
            if slot_ref and current_slot_ref != slot_ref:
                continue
            return {
                "group_ref": current_group_ref,
                "slot_key": slot_key,
                "chain_text": str(group.get("chain_text", "") or ""),
                "base_session_name": str(group.get("base_session_name", "") or ""),
                **slot,
            }
    return {}


def ensure_thread_group(chain_text: str, base_session_name: str = "") -> Dict[str, Any]:
    normalized = normalize_call_chain(chain_text)
    if not normalized:
        return {}
    store = load_thread_group_store()
    groups = store.setdefault("groups", {})
    chain_key = chain_route_key(normalized)
    group = groups.get(chain_key)
    if isinstance(group, dict):
        group.setdefault("slots", {})
        if base_session_name and not group.get("base_session_name"):
            group["base_session_name"] = base_chain_session_name(base_session_name)
        group["updated_at"] = time.time()
        groups[chain_key] = group
        save_thread_group_store(store)
        return group

    index = int(store.get("next_group_index", 1) or 1)
    group = {
        "group_ref": f"G{index:04d}",
        "chain_key": chain_key,
        "chain_text": normalized,
        "base_session_name": base_chain_session_name(base_session_name),
        "slots": {},
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    groups[chain_key] = group
    store["next_group_index"] = index + 1
    save_thread_group_store(store)
    return group


def use_toolchain_separate_conversations(config: Optional[Dict[str, Any]] = None,
                                         cli_value: Optional[bool] = None) -> bool:
    if cli_value is not None:
        return bool(cli_value)
    value = (config or {}).get("toolchain_separate_conversations", DEFAULT_TOOLCHAIN_SEPARATE_CONVERSATIONS)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "split", "separate")


def remember_thread_slot(chain_text: str,
                         profile: str,
                         command_type: str = "",
                         toolchain_key: str = "",
                         session_name: str = "",
                         conversation_id: str = "",
                         section_id: str = "",
                         note: str = "") -> Dict[str, Any]:
    group = ensure_thread_group(chain_text, base_session_name=session_name)
    if not group:
        return {}
    store = load_thread_group_store()
    groups = store.setdefault("groups", {})
    chain_key = str(group.get("chain_key", "") or "")
    group = groups.get(chain_key, group)
    slots = group.setdefault("slots", {})
    slot_key = "|".join([
        safe_slug(profile or "auto"),
        safe_slug(command_type or "chain_root"),
        safe_slug(toolchain_key or "default"),
    ])
    previous = slots.get(slot_key, {}) if isinstance(slots, dict) else {}
    slot_ref = str(previous.get("slot_ref", "") or "")
    if not slot_ref:
        slot_ref = f"S{len(slots) + 1:02d}"
    entry = dict(previous) if isinstance(previous, dict) else {}
    entry.update({
        "slot_ref": slot_ref,
        "profile": profile or str(previous.get("profile", "") or ""),
        "command_type": command_type or str(previous.get("command_type", "") or ""),
        "toolchain_key": toolchain_key or str(previous.get("toolchain_key", "") or ""),
        "session_name": str(session_name or previous.get("session_name", "") or ""),
        "conversation_id": conversation_id or str(previous.get("conversation_id", "") or ""),
        "section_id": section_id or str(previous.get("section_id", "") or ""),
        "note": note or str(previous.get("note", "") or ""),
        "updated_at": time.time(),
        "message_count": int(previous.get("message_count", 0) or 0) + 1,
    })
    slots[slot_key] = entry
    group["updated_at"] = time.time()
    groups[chain_key] = group
    save_thread_group_store(store)
    return {
        "group_ref": str(group.get("group_ref", "") or ""),
        "slot_key": slot_key,
        **entry,
    }


def initialize_chain_meeting(chain_text: str,
                             session_name: str = "",
                             conversation_id: str = "",
                             section_id: str = "",
                             separate_toolchain_conversations: bool = DEFAULT_TOOLCHAIN_SEPARATE_CONVERSATIONS,
                             toolchain_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    normalized = normalize_call_chain(chain_text)
    if not normalized:
        return {}
    base_name = base_chain_session_name(session_name)
    group = ensure_thread_group(normalized, base_session_name=base_name)
    if not group:
        return {}

    created_slots: List[Dict[str, Any]] = []
    root_slot_ref = ""
    for template in CHAIN_TOOLCHAIN_SLOTS:
        toolchain_key = str(template.get("toolchain_key", "") or "")
        is_root = toolchain_key == "chain_root"
        slot_session_name = base_name
        slot_conversation_id = conversation_id
        slot_section_id = section_id
        if should_split_toolchain_session(
            toolchain_key,
            separate_toolchain_conversations=separate_toolchain_conversations,
            config=toolchain_config,
        ) and not is_root:
            slot_session_name = f"{base_name}__{safe_slug(toolchain_key or template.get('command_type', 'slot'))}"
            slot_conversation_id = ""
            slot_section_id = ""
        created = remember_thread_slot(
            normalized,
            profile=str(template.get("profile", "") or DEFAULT_PROFILE),
            command_type=str(template.get("command_type", "") or ""),
            toolchain_key=toolchain_key,
            session_name=slot_session_name,
            conversation_id=slot_conversation_id,
            section_id=slot_section_id,
            note="chain_meeting_init",
        )
        if created:
            if is_root:
                root_slot_ref = str(created.get("slot_ref", "") or "")
            slot_metadata = {
                "child_thread": not is_root,
                "independent_meeting": bool(
                    not is_root and should_split_toolchain_session(
                        toolchain_key,
                        separate_toolchain_conversations=separate_toolchain_conversations,
                        config=toolchain_config,
                    )
                ),
                "parent_slot_ref": root_slot_ref if not is_root else "",
            }
            if toolchain_key == "jadx_source_chain":
                slot_metadata["child_thread"] = True
                slot_metadata["independent_meeting"] = True
                slot_metadata["bridge_to_profile"] = "rename_mapping"
            if toolchain_key == "rename_followup_chain":
                slot_metadata["child_thread"] = True
                slot_metadata["bridge_from_toolchain"] = "jadx_source_chain"
                slot_metadata["bridge_to_profile"] = "rename_tuning"
            if toolchain_key == "rename_tuning_chain":
                slot_metadata["child_thread"] = True
                slot_metadata["independent_meeting"] = True
                slot_metadata["bridge_from_toolchain"] = "rename_followup_chain"
                slot_metadata["bridge_to_profile"] = "jadx_code_analyst"
            if toolchain_key == "jadx_named_chain":
                slot_metadata["child_thread"] = True
                slot_metadata["independent_meeting"] = True
                slot_metadata["bridge_from_toolchain"] = "rename_tuning_chain"
            created = update_thread_slot_metadata(
                normalized,
                profile=str(template.get("profile", "") or DEFAULT_PROFILE),
                command_type=str(template.get("command_type", "") or ""),
                toolchain_key=toolchain_key,
                metadata=slot_metadata,
            ) or created
            created_slots.append(created)
    return {
        "group_ref": str(group.get("group_ref", "") or ""),
        "session_name": base_name,
        "separate_toolchain_conversations": bool(separate_toolchain_conversations),
        "slots": created_slots,
    }


def resolve_thread_slot(chain_text: str,
                        profile: str = "",
                        command_type: str = "",
                        toolchain_key: str = "") -> Dict[str, Any]:
    normalized = normalize_call_chain(chain_text)
    if not normalized:
        return {}
    store = load_thread_group_store()
    groups = store.get("groups", {}) if isinstance(store, dict) else {}
    group = groups.get(chain_route_key(normalized), {}) if isinstance(groups, dict) else {}
    if not isinstance(group, dict):
        return {}
    slots = group.get("slots", {})
    if not isinstance(slots, dict):
        slots = {}
    wanted_key = "|".join([
        safe_slug(profile or "auto"),
        safe_slug(command_type or "chain_root"),
        safe_slug(toolchain_key or "default"),
    ])
    slot = slots.get(wanted_key)
    if isinstance(slot, dict):
        return {"group_ref": str(group.get("group_ref", "") or ""), "slot_key": wanted_key, **slot}
    for slot_key, item in slots.items():
        if not isinstance(item, dict):
            continue
        if profile and str(item.get("profile", "") or "") != profile:
            continue
        if command_type and str(item.get("command_type", "") or "") != command_type:
            continue
        if toolchain_key and str(item.get("toolchain_key", "") or "") != toolchain_key:
            continue
        return {"group_ref": str(group.get("group_ref", "") or ""), "slot_key": slot_key, **item}
    return {"group_ref": str(group.get("group_ref", "") or "")}


def update_thread_slot_metadata(chain_text: str,
                                profile: str,
                                command_type: str,
                                toolchain_key: str,
                                metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    normalized = normalize_call_chain(chain_text)
    if not normalized:
        return {}
    metadata = metadata if isinstance(metadata, dict) else {}
    store = load_thread_group_store()
    groups = store.get("groups", {}) if isinstance(store, dict) else {}
    if not isinstance(groups, dict):
        return {}
    group = groups.get(chain_route_key(normalized), {})
    if not isinstance(group, dict):
        return {}
    slots = group.get("slots", {})
    if not isinstance(slots, dict):
        return {}
    slot_key = "|".join([
        safe_slug(profile or "auto"),
        safe_slug(command_type or "chain_root"),
        safe_slug(toolchain_key or "default"),
    ])
    slot = slots.get(slot_key, {})
    if not isinstance(slot, dict):
        return {}
    slot.update(metadata)
    slot["updated_at"] = time.time()
    slots[slot_key] = slot
    group["slots"] = slots
    group["updated_at"] = time.time()
    groups[chain_route_key(normalized)] = group
    save_thread_group_store(store)
    return {"group_ref": str(group.get("group_ref", "") or ""), "slot_key": slot_key, **slot}


def list_call_chain_threads(chain_text: str) -> List[Dict[str, Any]]:
    normalized = normalize_call_chain(chain_text)
    if not normalized:
        return []
    store = load_thread_group_store()
    groups = store.get("groups", {}) if isinstance(store, dict) else {}
    if not isinstance(groups, dict):
        return []
    group = groups.get(chain_route_key(normalized), {})
    if not isinstance(group, dict):
        return []
    slots = group.get("slots", {})
    if not isinstance(slots, dict):
        return []
    result: List[Dict[str, Any]] = []
    for slot_key, item in slots.items():
        if not isinstance(item, dict):
            continue
        result.append({
            "group_ref": str(group.get("group_ref", "") or ""),
            "slot_key": slot_key,
            "slot_ref": str(item.get("slot_ref", "") or ""),
            "profile": str(item.get("profile", "") or ""),
            "command_type": str(item.get("command_type", "") or ""),
            "toolchain_key": str(item.get("toolchain_key", "") or ""),
            "session_name": str(item.get("session_name", "") or ""),
            "conversation_id": str(item.get("conversation_id", "") or ""),
            "section_id": str(item.get("section_id", "") or ""),
            "child_thread": bool(item.get("child_thread", False)),
            "independent_meeting": bool(item.get("independent_meeting", False)),
            "bridge_to_profile": str(item.get("bridge_to_profile", "") or ""),
            "bridge_from_toolchain": str(item.get("bridge_from_toolchain", "") or ""),
            "last_bridge_at": item.get("last_bridge_at"),
            "updated_at": item.get("updated_at"),
        })
    result.sort(key=lambda item: (item["slot_ref"], item["toolchain_key"]))
    return result


def command_targets_named_export_output(command_text: str) -> bool:
    lowered = str(command_text or "").lower()
    if not lowered:
        return False
    return any(token in lowered for token in (
        "{chain_jadx_named_dir}",
        "chain_jadx_named_dir",
        "named_export",
    ))


def command_targets_analysis_pkg_output(command_text: str) -> bool:
    lowered = str(command_text or "").lower()
    if not lowered:
        return False
    return any(token in lowered for token in (
        "{chain_jadx_analysis_dir}",
        "chain_jadx_analysis_dir",
        "analysis_pkg",
    ))


def command_targets_named_jadx_output(command_text: str) -> bool:
    lowered = str(command_text or "").lower()
    if not lowered:
        return False
    return bool(
        command_targets_named_export_output(command_text)
        or command_targets_analysis_pkg_output(command_text)
        or "jadx_named_chain" in lowered
    )


def infer_followup_command_type(text: str) -> str:
    lowered = (text or "").lower()
    if command_targets_named_jadx_output(text) or "jadx_named_chain" in lowered:
        return "jadx_named_decompile"
    if "naming_assignments" in lowered or "命名微调" in text or "重命名校验" in text:
        return "rename_tuning"
    if "xref" in lowered or "调用链" in lowered or "交叉引用" in lowered:
        return "method_xref"
    if "subclasses" in lowered or "子类" in lowered or "继承" in lowered:
        return "subclass_scan"
    if "jadx" in lowered or "java代码" in lowered or "反编译" in lowered:
        return "jadx_decompile"
    if "rename" in lowered or "重命名" in lowered or "映射" in lowered:
        return "rename_mapping"
    if "smali" in lowered and "converter" in lowered:
        return "smali_conversion"
    return ""


def infer_followup_command_type_by_type(text: str) -> str:
    lowered = (text or "").lower()
    if command_targets_named_jadx_output(text) or "jadx_named_chain" in lowered:
        return "jadx_named_decompile"
    if "baksmali_xref" in lowered or "trace_method_refs" in lowered:
        return "method_xref"
    if "baksmali_subclasses" in lowered or "trace_inheritance" in lowered:
        return "subclass_scan"
    if "jadx_toolchain" in lowered or "recover_java_source" in lowered:
        return "jadx_decompile"
    return infer_followup_command_type(text)


def infer_toolchain_route(tool: str, command: str, profile_hint: str = "") -> Dict[str, str]:
    tool_value = str(tool or "").strip()
    lowered_tool = tool_value.lower()
    lowered_command = str(command or "").lower()
    lowered_hint = str(profile_hint or "").lower()

    if command_targets_named_jadx_output(command) or "jadx_named_chain" in lowered_command or "jadx_named_chain" in lowered_hint:
        return {"profile": "jadx_code_analyst", "command_type": "jadx_named_decompile", "toolchain_key": "jadx_named_chain"}

    if lowered_tool == "baksmali_xref":
        return {"profile": "xref_analysis", "command_type": "method_xref", "toolchain_key": "baksmali_xref_chain"}
    if lowered_tool in ("baksmali_subclasses", "subclasses"):
        return {"profile": "xref_analysis", "command_type": "subclass_scan", "toolchain_key": "baksmali_subclasses_chain"}
    if lowered_tool in ("jadx", "jadx_toolchain"):
        return {"profile": "jadx_code_analyst", "command_type": "jadx_decompile", "toolchain_key": "jadx_source_chain"}
    if lowered_tool == "smali_converter":
        return {"profile": "tool_dispatch", "command_type": "smali_conversion", "toolchain_key": "smali_conversion_chain"}
    if lowered_tool == "start_powershell":
        return {"profile": "tool_dispatch", "command_type": "powershell_probe", "toolchain_key": "powershell_probe_chain"}

    command_type = infer_followup_command_type_by_type("\n".join([tool_value, lowered_command, profile_hint]))
    if command_type == "method_xref":
        return {"profile": "xref_analysis", "command_type": command_type, "toolchain_key": "baksmali_xref_chain"}
    if command_type == "subclass_scan":
        return {"profile": "xref_analysis", "command_type": command_type, "toolchain_key": "baksmali_subclasses_chain"}
    if command_type == "jadx_named_decompile":
        return {"profile": "jadx_code_analyst", "command_type": command_type, "toolchain_key": "jadx_named_chain"}
    if command_type == "jadx_decompile":
        return {"profile": "jadx_code_analyst", "command_type": command_type, "toolchain_key": "jadx_source_chain"}
    if command_type == "rename_mapping":
        return {"profile": "rename_mapping", "command_type": command_type, "toolchain_key": "rename_followup_chain"}
    if command_type == "rename_tuning":
        return {"profile": "rename_tuning", "command_type": command_type, "toolchain_key": "rename_tuning_chain"}
    if command_type == "smali_conversion":
        return {"profile": "tool_dispatch", "command_type": command_type, "toolchain_key": "smali_conversion_chain"}
    return {
        "profile": profile_hint or DEFAULT_PROFILE,
        "command_type": command_type or "generic_command",
        "toolchain_key": "generic_followup_chain" if not command_type else (command_type + "_chain"),
    }


def remember_command_routes(call_chain_text: str,
                            command_items: List[Dict[str, Any]],
                            base_session_name: str,
                            conversation_id: str = "",
                            section_id: str = "",
                            separate_toolchain_conversations: bool = DEFAULT_TOOLCHAIN_SEPARATE_CONVERSATIONS,
                            toolchain_config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if not call_chain_text or not command_items:
        return []
    stored_routes: List[Dict[str, Any]] = []
    base_name = base_chain_session_name(base_session_name)
    for index, item in enumerate(command_items):
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool", "") or "")
        command = str(item.get("command", "") or "")
        profile_hint = str(item.get("profile", "") or "")
        inferred = infer_toolchain_route(tool, command, profile_hint=profile_hint)
        profile = str(inferred.get("profile", "") or profile_hint or DEFAULT_PROFILE)
        command_type = str(inferred.get("command_type", "") or "generic_command")
        toolchain_key = str(inferred.get("toolchain_key", "") or "generic_followup_chain")
        session_name = base_name
        slot_conversation_id = conversation_id
        slot_section_id = section_id
        if should_split_toolchain_session(
            toolchain_key,
            separate_toolchain_conversations=separate_toolchain_conversations,
            config=toolchain_config,
        ) and toolchain_key and toolchain_key != "chain_root":
            session_name = f"{base_name}__{safe_slug(toolchain_key)}"
            slot_conversation_id = ""
            slot_section_id = ""
        stored = remember_role_takeover_route(
            call_chain_text,
            profile=profile,
            command_type=command_type,
            toolchain_key=toolchain_key,
            session_name=session_name,
            conversation_id=slot_conversation_id,
            section_id=slot_section_id,
            role_card=profile,
            tool=tool,
            toolchain_entry=str(item.get("tool", "") or ""),
            note=f"command_item_{index}",
        )
        if stored:
            stored_routes.append({
                "index": index,
                "tool": tool,
                "profile": profile,
                "command_type": command_type,
                "toolchain_key": toolchain_key,
                "group_ref": str(stored.get("group_ref", "") or ""),
                "slot_ref": str(stored.get("slot_ref", "") or ""),
                "session_name": str(stored.get("session_name", "") or ""),
            })
    return stored_routes


def base_chain_session_name(session_name: str) -> str:
    value = str(session_name or "").strip()
    if "__" in value:
        return value.split("__", 1)[0]
    return value


def role_takeover_key(chain_text: str,
                      profile: str,
                      command_type: str = "",
                      toolchain_key: str = "") -> str:
    parts = [
        chain_route_key(chain_text),
        safe_slug(profile or "auto"),
        safe_slug(command_type or "generic"),
        safe_slug(toolchain_key or "default"),
    ]
    return "|".join(parts)


def resolve_role_takeover_route(chain_text: str,
                                preferred_profile: str = "auto",
                                command_type: str = "",
                                toolchain_key: str = "") -> Dict[str, Any]:
    normalized = normalize_call_chain(chain_text)
    if not normalized:
        return {}
    store = load_role_takeover_store()
    routes = store.get("routes", {}) if isinstance(store, dict) else {}
    matched: List[Dict[str, Any]] = []
    for item in routes.values() if isinstance(routes, dict) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("chain_key", "") or "") != chain_route_key(normalized):
            continue
        item_profile = str(item.get("profile", "") or "")
        item_type = str(item.get("command_type", "") or "")
        item_toolchain = str(item.get("toolchain_key", "") or "")
        item_tool = str(item.get("tool", "") or "")
        if item_type == "generic_command" and not item_tool:
            continue
        score = 0
        if command_type and item_type == command_type:
            score += 20
        if toolchain_key and item_toolchain == toolchain_key:
            score += 20
        if not command_type and preferred_profile and preferred_profile != "auto" and item_profile == preferred_profile:
            score += 5
        if not command_type and not toolchain_key and not preferred_profile:
            score += 1
        if score <= 0:
            continue
        candidate = dict(item)
        candidate["session_name"] = str(candidate.get("session_name", "") or "")
        candidate.update(resolve_thread_slot(
            normalized,
            profile=item_profile,
            command_type=item_type,
            toolchain_key=item_toolchain,
        ))
        candidate["matched_by"] = "role_takeover"
        candidate["score"] = score
        matched.append(candidate)
    matched.sort(key=lambda value: (int(value.get("score", 0) or 0), float(value.get("updated_at", 0) or 0)), reverse=True)
    if matched:
        return matched[0]

    fallback_slot = resolve_thread_slot(
        normalized,
        profile="" if preferred_profile == "auto" else preferred_profile,
        command_type=command_type,
        toolchain_key=toolchain_key,
    )
    if fallback_slot.get("slot_ref"):
        fallback_slot["matched_by"] = "thread_slot"
        fallback_slot["chain_text"] = normalized
        fallback_slot["session_name"] = str(fallback_slot.get("session_name", "") or "")
        return fallback_slot
    return {}


def remember_role_takeover_route(chain_text: str,
                                 profile: str,
                                 command_type: str,
                                 toolchain_key: str,
                                 session_name: str,
                                 conversation_id: str = "",
                                 section_id: str = "",
                                 role_card: str = "",
                                 tool: str = "",
                                 toolchain_entry: str = "",
                                 executor: str = "",
                                 note: str = "") -> Dict[str, Any]:
    normalized = normalize_call_chain(chain_text)
    if not normalized:
        return {}
    store = load_role_takeover_store()
    routes = store.setdefault("routes", {})
    key = role_takeover_key(normalized, profile, command_type, toolchain_key)
    previous = routes.get(key, {}) if isinstance(routes, dict) else {}
    entry = {
        "route_key": key,
        "chain_key": chain_route_key(normalized),
        "chain_text": normalized,
        "profile": profile or str(previous.get("profile", "") or ""),
        "command_type": command_type or str(previous.get("command_type", "") or ""),
        "toolchain_key": toolchain_key or str(previous.get("toolchain_key", "") or ""),
        "session_name": session_name or str(previous.get("session_name", "") or ""),
        "conversation_id": conversation_id or str(previous.get("conversation_id", "") or ""),
        "section_id": section_id or str(previous.get("section_id", "") or ""),
        "role_card": role_card or str(previous.get("role_card", "") or ""),
        "tool": tool or str(previous.get("tool", "") or ""),
        "toolchain_entry": toolchain_entry or str(previous.get("toolchain_entry", "") or ""),
        "executor": executor or str(previous.get("executor", "") or ""),
        "note": note or str(previous.get("note", "") or ""),
        "updated_at": time.time(),
        "message_count": int(previous.get("message_count", 0) or 0) + 1,
    }
    routes[key] = entry
    save_role_takeover_store(store)
    thread_slot = remember_thread_slot(
        normalized,
        profile=entry["profile"],
        command_type=entry["command_type"],
        toolchain_key=entry["toolchain_key"],
        session_name=entry["session_name"],
        conversation_id=entry["conversation_id"],
        section_id=entry["section_id"],
        note=entry["note"],
    )
    if thread_slot:
        entry.update(thread_slot)
    slot_metadata: Dict[str, Any] = {}
    if entry["toolchain_key"] and entry["toolchain_key"] != "chain_root":
        slot_metadata["child_thread"] = True
        slot_metadata["independent_meeting"] = should_split_toolchain_session(
            str(entry["toolchain_key"] or ""),
            separate_toolchain_conversations=False,
            config=load_tool_dispatcher_config(),
        )
    if entry["toolchain_key"] == "jadx_source_chain":
        slot_metadata["child_thread"] = True
        slot_metadata["independent_meeting"] = True
        slot_metadata["bridge_to_profile"] = "rename_mapping"
    if entry["toolchain_key"] == "rename_followup_chain":
        slot_metadata["child_thread"] = True
        slot_metadata["bridge_from_toolchain"] = "jadx_source_chain"
        slot_metadata["bridge_to_profile"] = "rename_tuning"
    if entry["toolchain_key"] == "rename_tuning_chain":
        slot_metadata["child_thread"] = True
        slot_metadata["independent_meeting"] = True
        slot_metadata["bridge_from_toolchain"] = "rename_followup_chain"
        slot_metadata["bridge_to_profile"] = "jadx_code_analyst"
    if entry["toolchain_key"] == "jadx_named_chain":
        slot_metadata["child_thread"] = True
        slot_metadata["independent_meeting"] = True
        slot_metadata["bridge_from_toolchain"] = "rename_tuning_chain"
    if slot_metadata:
        updated_slot = update_thread_slot_metadata(
            normalized,
            profile=entry["profile"],
            command_type=entry["command_type"],
            toolchain_key=entry["toolchain_key"],
            metadata=slot_metadata,
        )
        if updated_slot:
            entry.update(updated_slot)
    return entry


def chain_target_signature(chain_text: str) -> str:
    targets = sorted(set(extract_class_targets(chain_text, limit=24) + extract_method_targets(chain_text, limit=24)))
    return "|".join(targets)


def chain_route_key(chain_text: str) -> str:
    normalized = normalize_call_chain(chain_text)
    digest_source = normalized or chain_target_signature(chain_text)
    return hashlib.sha1(digest_source.encode("utf-8", "ignore")).hexdigest()[:16] if digest_source else ""


def build_chain_session_name(chain_text: str, generation: int = 0) -> str:
    normalized = normalize_call_chain(chain_text)
    seed_target = extract_class_targets(normalized, limit=1)
    seed_name = seed_target[0] if seed_target else "chain"
    base_name = f"chain_{safe_slug(seed_name)}_{chain_route_key(normalized)[:6]}"
    if generation > 0:
        return f"{base_name}_n{generation:02d}"
    return base_name


def build_call_chain_runtime_context(call_chain_text: str,
                                     config: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    normalized = normalize_call_chain(call_chain_text)
    if not normalized:
        return {}
    config = config if isinstance(config, dict) else {}
    chain_key = chain_route_key(normalized)
    base_session = build_chain_session_name(normalized, generation=0)
    chain_slug = safe_slug(base_session, limit=96)
    chain_output_root = os.path.abspath(str(config.get("call_chain_output_root", "") or DEFAULT_CHAIN_OUTPUT_ROOT))
    chain_index_root = os.path.abspath(str(config.get("call_chain_index_root", "") or DEFAULT_CHAIN_INDEX_ROOT))
    base_smali_dir = str(config.get("smali_dir", "") or "")
    base_jadx_out = str(config.get("jadx_out", "") or "")
    work_dir = os.path.join(chain_output_root, chain_slug)
    output_dir = os.path.join(work_dir, "outputs")
    log_dir = os.path.join(work_dir, "logs")
    dispatch_dir = os.path.join(work_dir, "dispatch")
    chat_dir = os.path.join(work_dir, "chat")
    chain_smali_dir = os.path.join(base_smali_dir, "chains", chain_slug) if base_smali_dir else os.path.join(work_dir, "smali")
    chain_renamed_dex = os.path.join(work_dir, "renamed_classes.dex")
    chain_jadx_dir = os.path.join(base_jadx_out, "chains", chain_slug) if base_jadx_out else os.path.join(work_dir, "jadx")
    chain_jadx_named_dir = os.path.join(chain_jadx_dir, "named_export")
    chain_jadx_analysis_dir = os.path.join(chain_jadx_dir, "analysis_pkg")
    index_dir = os.path.join(chain_index_root, chain_slug)
    return {
        "chain_key": chain_key,
        "chain_slug": chain_slug,
        "base_session": base_session,
        "chain_output_root": chain_output_root,
        "chain_index_root": chain_index_root,
        "chain_work_dir": work_dir,
        "chain_output_dir": output_dir,
        "chain_log_dir": log_dir,
        "chain_dispatch_dir": dispatch_dir,
        "chain_chat_dir": chat_dir,
        "chain_smali_dir": chain_smali_dir,
        "chain_renamed_dex": chain_renamed_dex,
        "chain_jadx_dir": chain_jadx_dir,
        "chain_jadx_named_dir": chain_jadx_named_dir,
        "chain_jadx_analysis_dir": chain_jadx_analysis_dir,
        "chain_index_dir": index_dir,
        "chain_index_file": os.path.join(index_dir, "index.json"),
        "chain_global_index_file": os.path.join(chain_index_root, "chains.json"),
    }


def ensure_call_chain_runtime_dirs(call_chain_text: str,
                                   config: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    context = build_call_chain_runtime_context(call_chain_text, config=config)
    for key in (
        "chain_output_root",
        "chain_index_root",
        "chain_work_dir",
        "chain_output_dir",
        "chain_log_dir",
        "chain_dispatch_dir",
        "chain_chat_dir",
        "chain_smali_dir",
        "chain_jadx_dir",
        "chain_jadx_named_dir",
        "chain_jadx_analysis_dir",
        "chain_index_dir",
    ):
        path = str(context.get(key, "") or "")
        if path:
            ensure_dir(path)
    return context


def collect_chain_named_export_files(call_chain_text: str,
                                     config: Optional[Dict[str, Any]] = None,
                                     limit: int = 6) -> List[str]:
    runtime_context = build_call_chain_runtime_context(call_chain_text, config=config)
    named_dir = str(runtime_context.get("chain_jadx_named_dir", "") or "")
    if not named_dir or not os.path.isdir(named_dir):
        return []
    collected: List[str] = []
    for root, _, files in os.walk(named_dir):
        for file_name in sorted(files):
            if not file_name.lower().endswith(".java"):
                continue
            collected.append(os.path.join(root, file_name))
            if len(collected) >= max(1, int(limit or 1)):
                return collected
    return collected


def build_named_jadx_stage_lines(session_data: Optional[Dict[str, Any]],
                                 call_chain_text: str,
                                 config: Optional[Dict[str, Any]] = None) -> List[str]:
    session_data = session_data if isinstance(session_data, dict) else {}
    runtime_context = build_call_chain_runtime_context(call_chain_text, config=config)
    named_dir = str(runtime_context.get("chain_jadx_named_dir", "") or "")
    analysis_dir = str(runtime_context.get("chain_jadx_analysis_dir", "") or "")
    named_files = collect_chain_named_export_files(call_chain_text, config=config, limit=3)
    named_ready = bool(named_files)
    analysis_ready = False
    for raw_block in list(session_data.get("local_runtime_blocks", []))[:6]:
        block = normalize_local_runtime_block(raw_block)
        if not block or str(block.get("toolchain_key", "") or "") != "jadx_named_chain":
            continue
        artifact_paths = [str(path) for path in list(block.get("artifact_paths", []))[:8] if path]
        normalized_artifacts = [path.lower() for path in artifact_paths]
        if str(block.get("status", "") or "").startswith("success"):
            if command_targets_named_export_output(str(raw_block.get("command", "") or "")):
                named_ready = True
            if any("named_export" in path or command_targets_named_export_output(path) for path in normalized_artifacts):
                named_ready = True
            if command_targets_analysis_pkg_output(str(raw_block.get("command", "") or "")):
                analysis_ready = True
            if any("analysis_pkg" in path or command_targets_analysis_pkg_output(path) for path in normalized_artifacts):
                analysis_ready = True
    if not named_ready and not analysis_ready:
        return []
    lines: List[str] = []
    if named_ready:
        lines.append("named_jadx_export=ready")
        lines.append("next_stage=analysis_pkg_only")
        lines.append("do_not_repeat=export-classes->{chain_jadx_named_dir}")
    if analysis_ready:
        lines.append("analysis_pkg_status=ready")
        lines.append("next_stage=return_summary_or_index_update")
    if named_files:
        display_files: List[str] = []
        for path in named_files[:2]:
            display_path = path
            if named_dir and display_path.startswith(named_dir):
                display_path = "{chain_jadx_named_dir}" + display_path[len(named_dir):]
            elif analysis_dir and display_path.startswith(analysis_dir):
                display_path = "{chain_jadx_analysis_dir}" + display_path[len(analysis_dir):]
            display_files.append(shorten_text(display_path.replace("/", "\\"), 140))
        if display_files:
            lines.append("named_sources=" + " | ".join(display_files))
    return lines


def summarize_route_items_for_index(command_routes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summary: List[Dict[str, Any]] = []
    for item in command_routes[:12]:
        if not isinstance(item, dict):
            continue
        summary.append({
            "index": int(item.get("index", 0) or 0),
            "tool": str(item.get("tool", "") or ""),
            "profile": str(item.get("profile", "") or ""),
            "toolchain_key": str(item.get("toolchain_key", "") or ""),
            "session_name": str(item.get("session_name", "") or ""),
            "slot_ref": str(item.get("slot_ref", "") or ""),
        })
    return summary


def summarize_results_for_index(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summary: List[Dict[str, Any]] = []
    for item in results[:20]:
        if not isinstance(item, dict):
            continue
        policy = item.get("policy", {}) if isinstance(item.get("policy", {}), dict) else {}
        path_candidates = list(policy.get("paths", [])) if isinstance(policy.get("paths", []), list) else []
        for raw_text in (str(item.get("store_back", "") or ""), str(item.get("command", "") or "")):
            for path in WINDOWS_PATH_PATTERN.findall(raw_text):
                path_candidates.append(path)
        artifact_paths = unique_keep_order([path for path in path_candidates if path], limit=12)
        summary.append({
            "entry_index": int(item.get("entry_index", 0) or 0),
            "tool": str(item.get("tool", "") or ""),
            "ok": bool(item.get("ok")),
            "judge_status": int(item.get("judge_status", 1) or 1),
            "returncode": int(item.get("returncode", 0) or 0),
            "has_output": bool(item.get("has_output")),
            "stdout_preview": shorten_text(str(item.get("stdout_preview", "") or item.get("stdout", "") or ""), 180),
            "stderr_preview": shorten_text(str(item.get("stderr_preview", "") or item.get("stderr", "") or ""), 180),
            "artifact_preview": shorten_text(str(item.get("artifact_preview", "") or ""), 180),
            "next_query": shorten_text(str(item.get("next_query", "") or ""), 120),
            "artifact_paths": artifact_paths,
        })
    return summary


def update_call_chain_index(call_chain_text: str,
                            config: Dict[str, Any],
                            round_index: int = 0,
                            dispatch_payload: Optional[Dict[str, Any]] = None,
                            command_routes: Optional[List[Dict[str, Any]]] = None,
                            followups: Optional[List[Dict[str, Any]]] = None,
                            root_resume: Optional[Dict[str, Any]] = None,
                            loop_state: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    context = ensure_call_chain_runtime_dirs(call_chain_text, config=config)
    if not context:
        return {}
    index_path = str(context.get("chain_index_file", "") or "")
    global_index_path = str(context.get("chain_global_index_file", "") or "")
    entry = load_json_file(index_path)
    entry.update({
        "chain_text": normalize_call_chain(call_chain_text),
        "chain_key": str(context.get("chain_key", "") or ""),
        "chain_slug": str(context.get("chain_slug", "") or ""),
        "directories": {
            "work_dir": str(context.get("chain_work_dir", "") or ""),
            "output_dir": str(context.get("chain_output_dir", "") or ""),
            "dispatch_dir": str(context.get("chain_dispatch_dir", "") or ""),
            "chat_dir": str(context.get("chain_chat_dir", "") or ""),
            "smali_dir": str(context.get("chain_smali_dir", "") or ""),
            "renamed_dex": str(context.get("chain_renamed_dex", "") or ""),
            "jadx_dir": str(context.get("chain_jadx_dir", "") or ""),
            "jadx_named_dir": str(context.get("chain_jadx_named_dir", "") or ""),
            "jadx_analysis_dir": str(context.get("chain_jadx_analysis_dir", "") or ""),
            "index_dir": str(context.get("chain_index_dir", "") or ""),
        },
        "updated_at": time.time(),
    })
    rounds = list(entry.get("rounds", [])) if isinstance(entry.get("rounds", []), list) else []
    if round_index > 0 and isinstance(dispatch_payload, dict):
        rounds = [item for item in rounds if int(item.get("round", 0) or 0) != round_index]
        rounds.append({
            "round": round_index,
            "dispatch_summary": summarize_results_for_index(list(dispatch_payload.get("results", [])) if isinstance(dispatch_payload.get("results", []), list) else []),
            "command_routes": summarize_route_items_for_index(command_routes or []),
            "review": dict(dispatch_payload.get("review", {}) or {}),
            "followups": [
                {
                    "toolchain_key": str(item.get("toolchain_key", "") or ""),
                    "session_name": str(item.get("session_name", "") or ""),
                    "reply_preview": shorten_text(str(item.get("reply_text", "") or ""), 220),
                    "next_query": shorten_text(str(item.get("next_query", "") or ""), 160),
                }
                for item in (followups or [])[:12]
                if isinstance(item, dict)
            ],
            "root_resume": {
                "session_name": str((root_resume or {}).get("session_name", "") or ""),
                "reply_preview": shorten_text(str((root_resume or {}).get("reply_text", "") or ""), 220),
                "command_count": len((root_resume or {}).get("command_items", []) or []),
            } if root_resume else {},
            "updated_at": time.time(),
        })
    entry["rounds"] = sorted(rounds, key=lambda item: int(item.get("round", 0) or 0))[-20:]
    if isinstance(loop_state, dict):
        entry["loop_state"] = {
            "round_count": int(loop_state.get("round_count", 0) or 0),
            "exhausted": bool(loop_state.get("exhausted", False)),
            "stopped_reason": str(loop_state.get("stopped_reason", "") or ""),
            "max_rounds": int(loop_state.get("max_rounds", 0) or 0),
        }
    save_json_file(index_path, entry)

    global_index = load_json_file(global_index_path)
    chains = global_index.get("chains", {}) if isinstance(global_index.get("chains", {}), dict) else {}
    chains[str(context.get("chain_key", "") or "")] = {
        "chain_text": normalize_call_chain(call_chain_text),
        "chain_slug": str(context.get("chain_slug", "") or ""),
        "index_file": index_path,
        "work_dir": str(context.get("chain_work_dir", "") or ""),
        "updated_at": time.time(),
    }
    global_index["chains"] = chains
    global_index["updated_at"] = time.time()
    save_json_file(global_index_path, global_index)
    return context


def get_chain_reset_mark(chain_text: str) -> Dict[str, Any]:
    normalized = normalize_call_chain(chain_text)
    if not normalized:
        return {}
    store = load_chain_route_store()
    marks = store.get("reset_marks", {}) if isinstance(store, dict) else {}
    if not isinstance(marks, dict):
        return {}
    return dict(marks.get(chain_route_key(normalized), {}) or {})


def update_chain_reset_mark(chain_text: str,
                            pending: bool = True,
                            increment_generation: bool = False) -> Dict[str, Any]:
    normalized = normalize_call_chain(chain_text)
    if not normalized:
        return {}
    store = load_chain_route_store()
    marks = store.setdefault("reset_marks", {})
    if not isinstance(marks, dict):
        marks = {}
        store["reset_marks"] = marks
    key = chain_route_key(normalized)
    previous = marks.get(key, {}) if isinstance(marks.get(key, {}), dict) else {}
    generation = int(previous.get("generation", 0) or 0)
    if increment_generation:
        generation += 1
    marks[key] = {
        "chain_key": key,
        "chain_text": normalized,
        "generation": generation,
        "pending": bool(pending),
        "updated_at": time.time(),
        "last_session_name": str(previous.get("last_session_name", "") or ""),
    }
    save_chain_route_store(store)
    return dict(marks[key])


def merge_class_stats(current: Optional[Dict[str, Any]], class_targets: List[str], step: int = 1) -> Dict[str, int]:
    stats: Dict[str, int] = {}
    if isinstance(current, dict):
        for key, value in current.items():
            try:
                stats[str(key)] = int(value)
            except Exception:
                continue
    for target in class_targets:
        stats[target] = int(stats.get(target, 0)) + step
    return stats


def load_saved_session_index() -> Dict[str, Dict[str, Any]]:
    path = os.path.join(APP_DIR, SESSION_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as handle:
            raw = pickle.load(handle)
    except Exception:
        return {}
    sessions = raw.get("sessions", {}) if isinstance(raw, dict) else {}
    return sessions if isinstance(sessions, dict) else {}


def rank_sessions_for_classes(class_targets: List[str],
                              preferred_profile: str = "auto",
                              limit: int = 5) -> List[Dict[str, Any]]:
    if not class_targets:
        return []
    sessions = load_saved_session_index()
    candidates: List[Dict[str, Any]] = []
    wanted = {target for target in class_targets if target}
    for session_name, session in sessions.items():
        if not isinstance(session, dict):
            continue
        conversation_id = str(session.get("conversation_id", "") or "")
        if not conversation_id:
            continue
        session_profile = str(session.get("profile", "") or "")
        class_stats = session.get("class_stats", {})
        if not isinstance(class_stats, dict):
            class_stats = {}
        overlap_score = 0
        overlap_hits = 0
        for target in wanted:
            hit_count = int(class_stats.get(target, 0) or 0)
            overlap_hits += hit_count
            if hit_count > 0:
                overlap_score += 10 + hit_count
        recent_targets = {normalize_class_key(item) for item in session.get("recent_targets", []) if normalize_class_key(item)}
        overlap_score += len(wanted.intersection(recent_targets)) * 3
        if preferred_profile != "auto" and session_profile == preferred_profile:
            overlap_score += 5
        if overlap_score <= 0:
            continue
        candidates.append({
            "session_name": session_name,
            "conversation_id": conversation_id,
            "section_id": str(session.get("section_id", "") or ""),
            "profile": session_profile or DEFAULT_PROFILE,
            "profile_reason": str(session.get("profile_reason", "") or ""),
            "score": overlap_score,
            "overlap_hits": overlap_hits,
            "updated_at": float(session.get("updated_at", 0) or 0),
            "recent_targets": session.get("recent_targets", [])[:8],
        })
    candidates.sort(key=lambda item: (item["score"], item["updated_at"]), reverse=True)
    return candidates[:limit]


def resolve_call_chain_route(chain_text: str,
                             preferred_profile: str = "auto") -> Dict[str, Any]:
    normalized = normalize_call_chain(chain_text)
    if not normalized:
        return {}
    store = load_chain_route_store()
    routes = store.get("routes", {}) if isinstance(store, dict) else {}
    reset_marks = store.get("reset_marks", {}) if isinstance(store, dict) else {}
    key = chain_route_key(normalized)
    route = routes.get(key) if isinstance(routes, dict) else None
    if isinstance(route, dict):
        route = dict(route)
        route["session_name"] = base_chain_session_name(str(route.get("session_name", "") or ""))
        root_slot = resolve_thread_slot(
            normalized,
            profile=DEFAULT_PROFILE,
            command_type="chain_root",
            toolchain_key="chain_root",
        ) or resolve_thread_slot(
            normalized,
            profile="",
            command_type="chain_root",
            toolchain_key="chain_root",
        )
        if root_slot:
            route["group_ref"] = str(root_slot.get("group_ref", "") or route.get("group_ref", "") or "")
            route["slot_key"] = str(root_slot.get("slot_key", "") or route.get("slot_key", "") or "")
            route["slot_ref"] = str(root_slot.get("slot_ref", "") or route.get("slot_ref", "") or "")
        route["matched_by"] = "chain_key"
        return route

    signature = chain_target_signature(normalized)
    if isinstance(routes, dict):
        for item in routes.values():
            if not isinstance(item, dict):
                continue
            if item.get("target_signature") == signature and signature:
                route = dict(item)
                route["session_name"] = base_chain_session_name(str(route.get("session_name", "") or ""))
                root_slot = resolve_thread_slot(
                    normalized,
                    profile=DEFAULT_PROFILE,
                    command_type="chain_root",
                    toolchain_key="chain_root",
                ) or resolve_thread_slot(
                    normalized,
                    profile="",
                    command_type="chain_root",
                    toolchain_key="chain_root",
                )
                if root_slot:
                    route["group_ref"] = str(root_slot.get("group_ref", "") or route.get("group_ref", "") or "")
                    route["slot_key"] = str(root_slot.get("slot_key", "") or route.get("slot_key", "") or "")
                    route["slot_ref"] = str(root_slot.get("slot_ref", "") or route.get("slot_ref", "") or "")
                route["matched_by"] = "target_signature"
                return route

    reset_mark = reset_marks.get(key, {}) if isinstance(reset_marks, dict) else {}
    if isinstance(reset_mark, dict) and bool(reset_mark.get("pending")):
        generation = int(reset_mark.get("generation", 0) or 0)
        session_name = build_chain_session_name(normalized, generation=generation)
        return {
            "chain_key": key,
            "chain_text": normalized,
            "targets": unique_keep_order(extract_class_targets(normalized, limit=16) + extract_method_targets(normalized, limit=16), limit=24),
            "class_targets": extract_class_targets(normalized, limit=16),
            "method_targets": extract_method_targets(normalized, limit=16),
            "profile": DEFAULT_PROFILE if preferred_profile == "auto" else preferred_profile,
            "profile_reason": "reset_pending_new_meeting",
            "session_name": session_name,
            "matched_by": "reset_mark",
            "force_new_meeting": True,
            "reset_generation": generation,
        }

    ranked = rank_sessions_for_classes(extract_class_targets(normalized, limit=16), preferred_profile=preferred_profile, limit=1)
    if ranked:
        session_match = dict(ranked[0])
        session_match["session_name"] = base_chain_session_name(str(session_match.get("session_name", "") or ""))
        session_match.update(resolve_thread_slot(normalized, profile=str(session_match.get("profile", "") or "")))
        session_match["matched_by"] = "session_stats"
        session_match["chain_text"] = normalized
        session_match["targets"] = extract_class_targets(normalized, limit=16)
        return session_match
    return {}


def remember_call_chain_route(chain_text: str,
                              conversation_id: str,
                              section_id: str,
                              profile: str,
                              profile_reason: str,
                              user_message: str,
                              session_name: str = "",
                              route_source: str = "cli_chat",
                              separate_toolchain_conversations: bool = DEFAULT_TOOLCHAIN_SEPARATE_CONVERSATIONS,
                              toolchain_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    normalized = normalize_call_chain(chain_text)
    if not normalized:
        return {}
    store = load_chain_route_store()
    routes = store.setdefault("routes", {})
    key = chain_route_key(normalized)
    previous = routes.get(key, {}) if isinstance(routes, dict) else {}
    class_targets = extract_class_targets(normalized, limit=16)
    method_targets = extract_method_targets(normalized, limit=16)
    entry = {
        "chain_key": key,
        "chain_text": normalized,
        "target_signature": chain_target_signature(normalized),
        "targets": unique_keep_order(class_targets + method_targets, limit=24),
        "class_targets": class_targets,
        "method_targets": method_targets,
        "conversation_id": conversation_id or str(previous.get("conversation_id", "") or ""),
        "section_id": section_id or str(previous.get("section_id", "") or ""),
        "profile": profile or str(previous.get("profile", DEFAULT_PROFILE) or DEFAULT_PROFILE),
        "profile_reason": profile_reason or str(previous.get("profile_reason", "") or ""),
        "session_name": base_chain_session_name(session_name or str(previous.get("session_name", "") or "")),
        "route_source": route_source,
        "last_user_message": shorten_text(user_message, 200),
        "updated_at": time.time(),
        "message_count": int(previous.get("message_count", 0) or 0) + 1,
    }
    routes[key] = entry
    reset_marks = store.setdefault("reset_marks", {})
    if isinstance(reset_marks, dict):
        previous_mark = reset_marks.get(key, {}) if isinstance(reset_marks.get(key, {}), dict) else {}
        if previous_mark:
            reset_marks[key] = {
                **previous_mark,
                "pending": False,
                "updated_at": time.time(),
                "last_session_name": entry["session_name"],
            }
    save_chain_route_store(store)
    thread_slot = remember_thread_slot(
        normalized,
        profile=DEFAULT_PROFILE,
        command_type="chain_root",
        toolchain_key="chain_root",
        session_name=entry["session_name"],
        conversation_id=entry["conversation_id"],
        section_id=entry["section_id"],
        note="chain_route",
    )
    if thread_slot:
        entry.update(thread_slot)
    initialize_chain_meeting(
        normalized,
        session_name=entry["session_name"],
        conversation_id=entry["conversation_id"],
        section_id=entry["section_id"],
        separate_toolchain_conversations=separate_toolchain_conversations,
        toolchain_config=toolchain_config,
    )
    return entry


def build_chain_hint_lines(user_message: str,
                           chain_text: str,
                           tools: Optional[Dict[str, str]] = None,
                           route_entry: Optional[Dict[str, Any]] = None) -> List[str]:
    tools = tools or {}
    source = f"{user_message}\n{chain_text}".lower()
    lines: List[str] = []
    if route_entry:
        profile_value = str(route_entry.get("profile", "") or "")
        if profile_value:
            lines.append(f"resume_profile={profile_value}")
    if "subclasses" in source or "--smali-dir" in source:
        class_targets = extract_class_targets(user_message, limit=4)
        class_hint = class_targets[0] if class_targets else "Lpkg/Cls;"
        lines.append("subclasses 只接受 smali 目录；若当前输入是 dex/apk，先解到 {chain_smali_dir} 再扫子类。")
        lines.append(f"scan-children=java -jar {{baksmali}} subclasses --smali-dir {{chain_smali_dir}} --class \"{class_hint}\" -o {{chain_output_dir}}\\children.txt")
    if chain_text or "xref" in source:
        lines.append("阶段顺序=先对当前调用链做 JADX/xref 缩小范围，再做命名微调；若需要改名，先 smali apply-renames，smali 完成后再跑 named JADX，最后写入 analysis_pkg 并回主线程。")
        lines.append("只处理当前选中的调用链类，不要整包导出，不要把未命中的依赖类一起拉进来。")
        lines.append("只返回 command JSON 和简短 reason；不要返回 conversation_id、section_id、message_id 等中间标识。")
        lines.append("允许在命令中使用 {jadx_toolchain}、{smali_converter}、{chain_*} 这类运行时别名；本地会在执行前替换成当前链路路径。")
        lines.append("source_jadx_output={chain_jadx_dir}")
        lines.append("named_jadx_output={chain_jadx_named_dir}")
        lines.append("analysis_pkg_output={chain_jadx_analysis_dir}")
    return lines

def class_store_path(class_key: str) -> str:
    normalized = normalize_class_key(class_key)
    digest = hashlib.sha1(normalized.encode("utf-8", "ignore")).hexdigest()[:12]
    slug = safe_slug(normalized.replace("/", "_").replace(";", ""))
    return os.path.join(ensure_dir(CLASS_HIT_STORE_DIR), f"{slug}_{digest}.json")


def load_json_file(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json_file(path: str, data: Dict[str, Any]) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def extract_primary_fragment(text: str, limit: int = 480) -> str:
    if not text:
        return ""
    block_match = CODE_BLOCK_PATTERN.search(text)
    if block_match:
        return shorten_text(block_match.group(1).strip(), limit)
    return shorten_text(text.strip(), limit)


def normalize_candidate_name(raw_value: Any) -> str:
    text = str(raw_value or "").strip().strip("`\"'[]{}()")
    if not text:
        return ""
    if any(token in text for token in ("\\", "/", ";", "->", "=>")):
        return ""
    match = re.search(r"[A-Za-z_][A-Za-z0-9_$]{2,}", text)
    if not match:
        return ""
    candidate = match.group(0)
    lowered = candidate.lower()
    if lowered in {"json", "tool", "reason", "command", "next_query", "store_back", "profile"}:
        return ""
    return candidate


def build_candidate_class_descriptor(source_descriptor: str, candidate_name: str) -> str:
    source_class = normalize_class_key(source_descriptor)
    candidate = normalize_candidate_name(candidate_name)
    if not source_class or not candidate:
        return ""
    descriptor_body = source_class[1:-1]
    package_part, _, _ = descriptor_body.rpartition("/")
    target_body = f"{package_part}/{candidate}" if package_part else candidate
    return f"L{target_body};"


def collect_renamed_class_targets_from_item(item: Optional[Dict[str, Any]], limit: int = 4) -> List[str]:
    item = item if isinstance(item, dict) else {}
    results: List[str] = []
    for assignment in list(item.get("naming_assignments", []) or [])[:32]:
        if not isinstance(assignment, dict):
            continue
        if str(assignment.get("kind", "") or "").strip().lower() != "class":
            continue
        target_descriptor = build_candidate_class_descriptor(
            str(assignment.get("source", "") or ""),
            str(assignment.get("candidate", "") or ""),
        )
        if target_descriptor:
            results.append(target_descriptor)
    return unique_keep_order(results, limit=limit)


def empty_candidate_bucket() -> Dict[str, List[str]]:
    return {"class": [], "method": [], "field": [], "general": []}


def merge_candidate_bucket(base: Optional[Dict[str, List[str]]],
                           extra: Optional[Dict[str, List[str]]],
                           limit: int = 24) -> Dict[str, List[str]]:
    result = empty_candidate_bucket()
    for source in (base or {}, extra or {}):
        if not isinstance(source, dict):
            continue
        for key in result.keys():
            values = source.get(key, [])
            if not isinstance(values, list):
                continue
            result[key] = unique_keep_order(result[key] + [normalize_candidate_name(item) for item in values], limit=limit)
    return {key: value for key, value in result.items() if value}


def _append_candidate_names(bucket: Dict[str, List[str]], bucket_name: str, raw_value: Any) -> None:
    target = bucket.setdefault(bucket_name, [])
    values: List[Any]
    if isinstance(raw_value, list):
        values = raw_value
    else:
        values = [raw_value]
    for item in values:
        candidate = normalize_candidate_name(item)
        if candidate:
            target.append(candidate)


def extract_candidate_names_from_command_items(command_items: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    bucket = empty_candidate_bucket()
    for item in command_items:
        if not isinstance(item, dict):
            continue
        naming_candidates = item.get("naming_candidates")
        if isinstance(naming_candidates, dict):
            for raw_key, raw_value in naming_candidates.items():
                key = str(raw_key or "").lower()
                if "class" in key:
                    _append_candidate_names(bucket, "class", raw_value)
                elif "method" in key:
                    _append_candidate_names(bucket, "method", raw_value)
                elif "field" in key:
                    _append_candidate_names(bucket, "field", raw_value)
                else:
                    _append_candidate_names(bucket, "general", raw_value)
        candidate_names = item.get("candidate_names")
        if isinstance(candidate_names, list):
            _append_candidate_names(bucket, "general", candidate_names)
        for key, raw_value in item.items():
            lowered = str(key or "").lower()
            if lowered in {"naming_candidates", "candidate_names"}:
                continue
            if lowered in {"new_class", "class_name", "new_class_name"} or ("class" in lowered and "new" in lowered):
                _append_candidate_names(bucket, "class", raw_value)
            elif lowered in {"new_method", "method_name", "new_method_name"} or ("method" in lowered and "new" in lowered):
                _append_candidate_names(bucket, "method", raw_value)
            elif lowered in {"new_field", "field_name", "new_field_name"} or ("field" in lowered and "new" in lowered):
                _append_candidate_names(bucket, "field", raw_value)
            elif lowered in {"new_name", "rename_to", "alias", "candidate_name"}:
                _append_candidate_names(bucket, "general", raw_value)
    return {key: unique_keep_order(value, limit=12) for key, value in bucket.items() if value}


def extract_candidate_names_from_text(reply_text: str) -> Dict[str, List[str]]:
    bucket = empty_candidate_bucket()
    for pattern, bucket_name in (
        (r"(?:new_class|class_name|新类名|类名)\s*[:=：]\s*([A-Za-z_][A-Za-z0-9_$]{2,})", "class"),
        (r"(?:new_method|method_name|新方法名|方法名)\s*[:=：]\s*([A-Za-z_][A-Za-z0-9_$]{2,})", "method"),
        (r"(?:new_field|field_name|新字段名|字段名)\s*[:=：]\s*([A-Za-z_][A-Za-z0-9_$]{2,})", "field"),
        (r"(?:rename_to|new_name|命名为|建议名)\s*[:=：]\s*([A-Za-z_][A-Za-z0-9_$]{2,})", "general"),
    ):
        for match in re.findall(pattern, str(reply_text or ""), flags=re.IGNORECASE):
            _append_candidate_names(bucket, bucket_name, match)
    return {key: unique_keep_order(value, limit=12) for key, value in bucket.items() if value}


def collect_candidate_names(reply_text: str, command_items: Optional[List[Dict[str, Any]]] = None) -> Dict[str, List[str]]:
    return merge_candidate_bucket(
        extract_candidate_names_from_text(reply_text),
        extract_candidate_names_from_command_items(command_items or []),
        limit=16,
    )


def bucket_from_name_assignments(assignments: Optional[List[Dict[str, Any]]]) -> Dict[str, List[str]]:
    bucket = empty_candidate_bucket()
    for item in assignments or []:
        if not isinstance(item, dict):
            continue
        bucket_name = str(item.get("bucket", "") or "general")
        if bucket_name not in bucket:
            bucket_name = "general"
        candidate = normalize_candidate_name(item.get("candidate"))
        if candidate:
            bucket[bucket_name].append(candidate)
    return {
        key: unique_keep_order(value, limit=16)
        for key, value in bucket.items()
        if value
    }


def collect_json_payloads_from_text(reply_text: str) -> List[Any]:
    text = str(reply_text or "")
    payloads: List[Any] = []
    seen: set[str] = set()

    def add_payload(raw_value: Any) -> None:
        decoded = decode_jsonish(raw_value, max_depth=6)
        if not isinstance(decoded, (dict, list)):
            return
        try:
            signature = json.dumps(decoded, ensure_ascii=True, sort_keys=True)
        except TypeError:
            signature = repr(decoded)
        if signature in seen:
            return
        seen.add(signature)
        payloads.append(decoded)

    stripped = text.lstrip("\ufeff").strip()
    if stripped.startswith("{") or stripped.startswith("["):
        add_payload(stripped)
    for match in JSON_BLOCK_PATTERN.finditer(text):
        add_payload(str(match.group(1) or "").strip())
    return payloads


def normalize_name_assignment_bucket(raw_bucket: Any) -> str:
    lowered = str(raw_bucket or "").strip().lower()
    if not lowered:
        return "general"
    if "class" in lowered or "类" in lowered:
        return "class"
    if "method" in lowered or "func" in lowered or "方法" in lowered:
        return "method"
    if "field" in lowered or "member" in lowered or "字段" in lowered:
        return "field"
    return "general"


def normalize_name_source_key(raw_value: Any,
                              bucket: str = "general",
                              owner_class: str = "") -> str:
    text = str(raw_value or "").strip().strip("`\"'")
    if not text:
        return ""
    if bucket == "class":
        normalized = normalize_class_key(text)
        return normalized or text
    if bucket == "method":
        matches = extract_method_targets(text, limit=1)
        if matches:
            return matches[0]
        if owner_class and "->" not in text and normalize_candidate_name(text):
            return f"{owner_class}->{text}"
        return text
    if bucket == "field":
        matches = extract_field_targets(text, limit=1)
        if matches:
            return matches[0]
        if owner_class and "->" not in text:
            return f"{owner_class}->{text}"
        return text
    return text


def infer_owner_class_from_source(source_key: str, fallback_owner: str = "") -> str:
    normalized_fallback = normalize_class_key(fallback_owner)
    text = str(source_key or "").strip()
    if not text:
        return normalized_fallback
    if "->" in text:
        normalized = normalize_class_key(text.split("->", 1)[0])
        if normalized:
            return normalized
    normalized = normalize_class_key(text)
    if normalized:
        return normalized
    matches = extract_class_targets(text, limit=1)
    if matches:
        return matches[0]
    return normalized_fallback


def build_name_assignment(bucket: str,
                          candidate: Any,
                          source_key: Any = "",
                          owner_class: Any = "",
                          source_name: Any = "",
                          origin: str = "") -> Dict[str, str]:
    normalized_bucket = normalize_name_assignment_bucket(bucket)
    normalized_candidate = normalize_candidate_name(candidate)
    normalized_owner = normalize_class_key(str(owner_class or ""))
    normalized_source = normalize_name_source_key(source_key, bucket=normalized_bucket, owner_class=normalized_owner)
    normalized_owner = infer_owner_class_from_source(normalized_source, fallback_owner=normalized_owner)
    if normalized_bucket == "class" and not normalized_source:
        normalized_source = normalized_owner or normalize_candidate_name(source_name)
    if normalized_bucket != "general" and not normalized_source:
        return {}
    if not normalized_candidate:
        return {}
    return {
        "bucket": normalized_bucket,
        "candidate": normalized_candidate,
        "source_key": normalized_source,
        "owner_class": normalized_owner,
        "source_name": shorten_text(str(source_name or source_key or ""), 120),
        "origin": origin or "",
    }


def merge_name_assignments(base: Optional[List[Dict[str, Any]]],
                           extra: Optional[List[Dict[str, Any]]],
                           limit: int = 96) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    seen: set[str] = set()
    for source in (base or [], extra or []):
        for raw in source:
            if not isinstance(raw, dict):
                continue
            item = build_name_assignment(
                raw.get("bucket") or raw.get("kind") or raw.get("type") or raw.get("name_type") or "",
                raw.get("candidate") or raw.get("new_name") or raw.get("rename_to") or raw.get("name") or "",
                source_key=raw.get("source_key") or raw.get("source") or raw.get("old_name") or raw.get("target") or "",
                owner_class=raw.get("owner_class") or raw.get("owner") or raw.get("hit_class") or "",
                source_name=raw.get("source_name") or raw.get("old_name") or raw.get("source") or "",
                origin=str(raw.get("origin", "") or raw.get("note", "") or ""),
            )
            if not item:
                continue
            signature = json.dumps(item, ensure_ascii=True, sort_keys=True)
            if signature in seen:
                continue
            seen.add(signature)
            result.append(item)
            if len(result) >= limit:
                return result
    return result


def build_name_stat_record(bucket: Any,
                           candidate: Any,
                           source_key: Any = "",
                           owner_class: Any = "",
                           hits: int = 1) -> Dict[str, Any]:
    normalized_bucket = normalize_name_assignment_bucket(bucket)
    normalized_candidate = normalize_candidate_name(candidate)
    normalized_owner = normalize_class_key(str(owner_class or ""))
    normalized_source = normalize_name_source_key(source_key, bucket=normalized_bucket, owner_class=normalized_owner)
    normalized_owner = infer_owner_class_from_source(normalized_source, fallback_owner=normalized_owner)
    if not normalized_candidate:
        return {}
    return {
        "bucket": normalized_bucket,
        "candidate": normalized_candidate,
        "source_key": normalized_source,
        "owner_class": normalized_owner,
        "hits": max(1, int(hits or 1)),
        "updated_at": time.time(),
    }


def merge_name_stat_records(base: Optional[List[Dict[str, Any]]],
                            extra: Optional[List[Dict[str, Any]]],
                            limit: int = 192) -> List[Dict[str, Any]]:
    merged: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for source in (base or [], extra or []):
        for raw in source:
            if not isinstance(raw, dict):
                continue
            item = build_name_stat_record(
                raw.get("bucket") or raw.get("kind") or raw.get("type") or "",
                raw.get("candidate") or raw.get("new_name") or raw.get("rename_to") or raw.get("name") or "",
                source_key=raw.get("source_key") or raw.get("source") or "",
                owner_class=raw.get("owner_class") or raw.get("owner") or "",
                hits=int(raw.get("hits", 1) or 1),
            )
            if not item:
                continue
            key = (
                str(item.get("bucket", "") or ""),
                str(item.get("candidate", "") or ""),
                str(item.get("source_key", "") or ""),
                str(item.get("owner_class", "") or ""),
            )
            existing = merged.get(key)
            if existing is None:
                merged[key] = item
            else:
                existing["hits"] = int(existing.get("hits", 0) or 0) + int(item.get("hits", 1) or 1)
                existing["updated_at"] = max(float(existing.get("updated_at", 0) or 0), float(item.get("updated_at", 0) or 0))
    result = list(merged.values())
    result.sort(
        key=lambda item: (
            -int(item.get("hits", 0) or 0),
            str(item.get("bucket", "") or ""),
            str(item.get("candidate", "") or ""),
            str(item.get("source_key", "") or ""),
        )
    )
    return result[:limit]


def build_name_stat_records(candidate_names: Optional[Dict[str, List[str]]],
                            assignments: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    stats: List[Dict[str, Any]] = []
    for item in merge_name_assignments([], assignments or [], limit=96):
        stats.append(build_name_stat_record(
            item.get("bucket"),
            item.get("candidate"),
            source_key=item.get("source_key"),
            owner_class=item.get("owner_class"),
            hits=1,
        ))
    assigned_pairs = {
        (
            str(item.get("bucket", "") or ""),
            str(item.get("candidate", "") or ""),
        )
        for item in stats
        if isinstance(item, dict)
    }
    for bucket_name, values in merge_candidate_bucket({}, candidate_names or {}, limit=24).items():
        for candidate in values:
            if (bucket_name, candidate) in assigned_pairs:
                continue
            stats.append(build_name_stat_record(bucket_name, candidate, hits=1))
    return [item for item in stats if item]


def extract_name_assignments_from_payload(payload: Any,
                                          fallback_owner: str = "",
                                          origin: str = "payload") -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []
    if isinstance(payload, list):
        for item in payload:
            records = merge_name_assignments(records, extract_name_assignments_from_payload(item, fallback_owner=fallback_owner, origin=origin), limit=96)
        return records
    if not isinstance(payload, dict):
        return records

    raw_assignments = payload.get("naming_assignments")
    if isinstance(raw_assignments, (list, dict)):
        records = merge_name_assignments(records, extract_name_assignments_from_payload(raw_assignments, fallback_owner=fallback_owner, origin=origin), limit=96)

    explicit_bucket = normalize_name_assignment_bucket(
        payload.get("bucket") or payload.get("kind") or payload.get("type") or payload.get("name_type") or ""
    )
    bucket_candidates = [
        ("class", payload.get("new_class") or payload.get("new_class_name") or payload.get("class_name")),
        ("method", payload.get("new_method") or payload.get("new_method_name") or payload.get("method_name")),
        ("field", payload.get("new_field") or payload.get("new_field_name") or payload.get("field_name")),
    ]
    for bucket_name, candidate_value in bucket_candidates:
        if not candidate_value:
            continue
        record = build_name_assignment(
            bucket_name,
            candidate_value,
            source_key=payload.get(f"old_{bucket_name}") or payload.get(f"source_{bucket_name}") or payload.get("source") or payload.get("target") or "",
            owner_class=payload.get("owner_class") or payload.get("owner") or payload.get("hit_class") or fallback_owner,
            source_name=payload.get(f"old_{bucket_name}") or payload.get(f"source_{bucket_name}") or payload.get("target") or "",
            origin=origin,
        )
        if record:
            records = merge_name_assignments(records, [record], limit=96)

    general_candidate = payload.get("candidate") or payload.get("new_name") or payload.get("rename_to")
    if general_candidate:
        record = build_name_assignment(
            explicit_bucket,
            general_candidate,
            source_key=payload.get("source_key") or payload.get("source") or payload.get("old_name") or payload.get("target") or "",
            owner_class=payload.get("owner_class") or payload.get("owner") or payload.get("hit_class") or fallback_owner,
            source_name=payload.get("source_name") or payload.get("old_name") or payload.get("source") or "",
            origin=origin,
        )
        if record:
            records = merge_name_assignments(records, [record], limit=96)
    return records


def build_fallback_name_assignments(candidate_names: Dict[str, List[str]],
                                    call_chain_text: str = "",
                                    route_item: Optional[Dict[str, Any]] = None,
                                    reply_text: str = "",
                                    command_items: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, str]]:
    route_item = route_item if isinstance(route_item, dict) else {}
    command_items = command_items or []
    source_text = "\n".join([
        call_chain_text,
        reply_text,
        str(route_item.get("command", "") or ""),
        str(route_item.get("next_query", "") or ""),
        "\n".join(str(item.get("hit_class", "") or "") for item in command_items if isinstance(item, dict)),
    ])
    class_sources = unique_keep_order(
        [normalize_class_key(str(route_item.get("hit_class", "") or ""))]
        + extract_class_targets(source_text, limit=4),
        limit=4,
    )
    class_sources = [item for item in class_sources if item]
    method_sources = extract_method_targets(source_text, limit=4)
    field_sources = extract_field_targets(source_text, limit=4)
    fallback: List[Dict[str, str]] = []
    if len(class_sources) == 1:
        fallback.extend(
            build_name_assignment("class", candidate, source_key=class_sources[0], owner_class=class_sources[0], origin="fallback")
            for candidate in candidate_names.get("class", [])
        )
    if len(method_sources) == 1:
        fallback.extend(
            build_name_assignment("method", candidate, source_key=method_sources[0], owner_class=class_sources[0] if class_sources else "", origin="fallback")
            for candidate in candidate_names.get("method", [])
        )
    if len(field_sources) == 1:
        fallback.extend(
            build_name_assignment("field", candidate, source_key=field_sources[0], owner_class=class_sources[0] if class_sources else "", origin="fallback")
            for candidate in candidate_names.get("field", [])
        )
    return merge_name_assignments([], [item for item in fallback if item], limit=48)


def collect_naming_assignments(reply_text: str,
                               command_items: Optional[List[Dict[str, Any]]] = None,
                               call_chain_text: str = "",
                               route_item: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    command_items = command_items or []
    route_item = route_item if isinstance(route_item, dict) else {}
    records: List[Dict[str, str]] = []
    fallback_owner = normalize_class_key(str(route_item.get("hit_class", "") or ""))

    for payload in collect_json_payloads_from_text(reply_text):
        records = merge_name_assignments(records, extract_name_assignments_from_payload(payload, fallback_owner=fallback_owner, origin="reply_json"), limit=96)
    for item in command_items:
        if not isinstance(item, dict):
            continue
        item_owner = normalize_class_key(str(item.get("hit_class", "") or fallback_owner))
        records = merge_name_assignments(records, extract_name_assignments_from_payload(item, fallback_owner=item_owner, origin="command_item"), limit=96)

    candidate_names = collect_candidate_names(reply_text, command_items)
    if not records and candidate_names:
        records = merge_name_assignments(
            records,
            build_fallback_name_assignments(
                candidate_names,
                call_chain_text=call_chain_text,
                route_item=route_item,
                reply_text=reply_text,
                command_items=command_items,
            ),
            limit=96,
        )
    return records


def build_background_summary(reply_text: str,
                             command_items: Optional[List[Dict[str, Any]]] = None,
                             toolchain_key: str = "",
                             result: Optional[Dict[str, Any]] = None,
                             call_chain_text: str = "") -> str:
    command_items = command_items or []
    runtime_config = load_tool_dispatcher_config()
    parts: List[str] = []
    if toolchain_key:
        parts.append(f"phase={toolchain_key}")
    if isinstance(result, dict):
        hit_class = normalize_class_key(str(result.get("hit_class", "") or ""))
        if hit_class:
            parts.append(f"class={hit_class}")
        stdout_text = str(result.get("stdout", "") or "").strip()
        if stdout_text:
            try:
                stdout_payload = json.loads(stdout_text)
            except Exception:
                stdout_payload = {}
            if isinstance(stdout_payload, dict):
                matches = stdout_payload.get("matches", [])
                if isinstance(matches, list) and matches:
                    first_match = matches[0] if isinstance(matches[0], dict) else {}
                    match_path = shorten_text(
                        localize_runtime_paths_in_text(
                            str(first_match.get("path", "") or ""),
                            config=runtime_config,
                            call_chain_text=call_chain_text,
                        ),
                        120,
                    )
                    line_start = str(first_match.get("line_start", "") or "").strip()
                    snippet = sanitize_analysis_fragment(
                        str(first_match.get("snippet", "") or ""),
                        limit=180,
                        call_chain_text=call_chain_text,
                        config=runtime_config,
                    )
                    class_key = normalize_class_key(str(first_match.get("class_key", "") or ""))
                    match_parts = [part for part in [
                        f"jadx_class={class_key}" if class_key else "",
                        f"line={line_start}" if line_start else "",
                        f"path={match_path}" if match_path else "",
                        f"snippet={snippet}" if snippet else "",
                    ] if part]
                    if match_parts:
                        parts.append(" | ".join(match_parts))
        artifact_preview = sanitize_analysis_fragment(
            str(result.get("artifact_preview", "") or ""),
            limit=180,
            call_chain_text=call_chain_text,
            config=runtime_config,
        )
        if artifact_preview:
            parts.append(f"artifact={artifact_preview}")
    for item in command_items[:3]:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool", "") or "")
        reason = shorten_text(str(item.get("reason", "") or ""), 100)
        next_query = shorten_text(str(item.get("next_query", "") or ""), 120)
        hit_class = normalize_class_key(str(item.get("hit_class", "") or ""))
        item_parts = [part for part in [
            f"tool={tool}" if tool else "",
            f"class={hit_class}" if hit_class else "",
            f"reason={reason}" if reason else "",
            f"next={next_query}" if next_query else "",
        ] if part]
        if item_parts:
            parts.append(" | ".join(item_parts))
    if not parts:
        fragment = sanitize_analysis_fragment(reply_text, limit=220, call_chain_text=call_chain_text, config=runtime_config)
        if fragment:
            parts.append(fragment)
    return shorten_text(" || ".join(part for part in parts if part), 360)


def remember_chain_background(call_chain_text: str,
                              toolchain_key: str,
                              session_name: str,
                              reply_text: str = "",
                              command_items: Optional[List[Dict[str, Any]]] = None,
                              result: Optional[Dict[str, Any]] = None,
                              profile: str = "") -> Dict[str, Any]:
    normalized = normalize_call_chain(call_chain_text)
    if not normalized:
        return {}
    summary = build_background_summary(
        reply_text,
        command_items=command_items,
        toolchain_key=toolchain_key,
        result=result,
        call_chain_text=normalized,
    )
    if not summary:
        return {}
    name_assignments = collect_naming_assignments(
        reply_text,
        command_items=command_items,
        call_chain_text=normalized,
    )
    candidates = merge_candidate_bucket(
        collect_candidate_names(reply_text, command_items=command_items),
        bucket_from_name_assignments(name_assignments),
        limit=16,
    )
    targets = unique_keep_order(
        extract_targets(normalized, limit=8)
        + extract_targets(summary, limit=8)
        + extract_targets(reply_text, limit=8),
        limit=10,
    )
    entry = {
        "toolchain_key": toolchain_key or "",
        "profile": profile or "",
        "session_name": session_name or "",
        "summary": summary,
        "targets": targets,
        "candidate_names": candidates,
        "name_assignments": name_assignments,
        "updated_at": time.time(),
    }
    store = load_chain_background_store()
    chains = store.setdefault("chains", {})
    chain_key = chain_route_key(normalized)
    chain_entry = chains.get(chain_key, {}) if isinstance(chains.get(chain_key, {}), dict) else {}
    chain_entry.setdefault("chain_key", chain_key)
    chain_entry.setdefault("chain_text", normalized)
    chain_entry.setdefault("background_entries", [])
    chain_entry.setdefault("used_names", empty_candidate_bucket())
    chain_entry.setdefault("used_name_records", [])
    chain_entry.setdefault("name_stat_records", [])
    signature = json.dumps(
        {
            "toolchain_key": entry["toolchain_key"],
            "session_name": entry["session_name"],
            "summary": entry["summary"],
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    existing: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in chain_entry.get("background_entries", []):
        if not isinstance(raw, dict):
            continue
        raw_signature = json.dumps(
            {
                "toolchain_key": str(raw.get("toolchain_key", "") or ""),
                "session_name": str(raw.get("session_name", "") or ""),
                "summary": str(raw.get("summary", "") or ""),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        if raw_signature == signature or raw_signature in seen:
            continue
        seen.add(raw_signature)
        existing.append(raw)
    chain_entry["background_entries"] = [entry] + existing[:23]
    chain_entry["used_names"] = merge_candidate_bucket(chain_entry.get("used_names", {}), candidates, limit=48)
    chain_entry["used_name_records"] = merge_name_assignments(
        chain_entry.get("used_name_records", []),
        name_assignments,
        limit=120,
    )
    chain_entry["name_stat_records"] = merge_name_stat_records(
        chain_entry.get("name_stat_records", []),
        build_name_stat_records(candidates, name_assignments),
        limit=240,
    )
    chain_entry["updated_at"] = time.time()
    chains[chain_key] = chain_entry
    save_chain_background_store(store)
    return entry


def get_chain_background_fragments(call_chain_text: str,
                                   toolchain_key: str = "",
                                   limit: int = 4) -> List[str]:
    normalized = normalize_call_chain(call_chain_text)
    if not normalized:
        return []
    store = load_chain_background_store()
    chains = store.get("chains", {}) if isinstance(store, dict) else {}
    chain_entry = chains.get(chain_route_key(normalized), {}) if isinstance(chains, dict) else {}
    if not isinstance(chain_entry, dict):
        return []
    fragments: List[str] = []
    for raw in chain_entry.get("background_entries", []):
        if not isinstance(raw, dict):
            continue
        if toolchain_key and str(raw.get("toolchain_key", "") or "") != toolchain_key:
            continue
        summary = sanitize_analysis_fragment(
            str(raw.get("summary", "") or ""),
            limit=220,
            call_chain_text=normalized,
            config=load_tool_dispatcher_config(),
        )
        if summary:
            fragments.append(summary)
        if len(fragments) >= limit:
            break
    return fragments


def get_chain_used_names(call_chain_text: str) -> Dict[str, List[str]]:
    normalized = normalize_call_chain(call_chain_text)
    if not normalized:
        return {}
    store = load_chain_background_store()
    chains = store.get("chains", {}) if isinstance(store, dict) else {}
    chain_entry = chains.get(chain_route_key(normalized), {}) if isinstance(chains, dict) else {}
    if not isinstance(chain_entry, dict):
        return {}
    used_names = chain_entry.get("used_names", {})
    return merge_candidate_bucket(used_names, {}, limit=24)


def get_chain_used_name_records(call_chain_text: str) -> List[Dict[str, str]]:
    normalized = normalize_call_chain(call_chain_text)
    if not normalized:
        return []
    store = load_chain_background_store()
    chains = store.get("chains", {}) if isinstance(store, dict) else {}
    chain_entry = chains.get(chain_route_key(normalized), {}) if isinstance(chains, dict) else {}
    if not isinstance(chain_entry, dict):
        return []
    return merge_name_assignments(chain_entry.get("used_name_records", []), [], limit=120)


def get_chain_name_stat_records(call_chain_text: str) -> List[Dict[str, Any]]:
    normalized = normalize_call_chain(call_chain_text)
    if not normalized:
        return []
    store = load_chain_background_store()
    chains = store.get("chains", {}) if isinstance(store, dict) else {}
    chain_entry = chains.get(chain_route_key(normalized), {}) if isinstance(chains, dict) else {}
    if not isinstance(chain_entry, dict):
        return []
    return merge_name_stat_records(chain_entry.get("name_stat_records", []), [], limit=120)


def build_name_conflict_report(conflict_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_bucket: Dict[str, List[str]] = {}
    normalized_items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in conflict_items:
        if not isinstance(raw, dict):
            continue
        bucket_name = normalize_name_assignment_bucket(raw.get("bucket"))
        candidate = normalize_candidate_name(raw.get("candidate"))
        if not candidate:
            continue
        item = {
            "bucket": bucket_name,
            "candidate": candidate,
            "reason": str(raw.get("reason", "") or ""),
            "source_key": str(raw.get("source_key", "") or ""),
            "conflict_sources": unique_keep_order([str(value or "") for value in raw.get("conflict_sources", []) if str(value or "")], limit=8),
        }
        signature = json.dumps(item, ensure_ascii=True, sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        normalized_items.append(item)
        by_bucket.setdefault(bucket_name, []).append(candidate)
    return {
        "by_bucket": {key: unique_keep_order(value, limit=8) for key, value in by_bucket.items() if value},
        "items": normalized_items,
    }


def detect_name_conflicts(call_chain_text: str,
                          candidate_names: Optional[Dict[str, List[str]]],
                          name_assignments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    candidate_names = merge_candidate_bucket({}, candidate_names or {}, limit=16)
    assignments = merge_name_assignments([], name_assignments or [], limit=64)
    if not candidate_names and not assignments:
        return {"by_bucket": {}, "items": []}

    existing_names = get_chain_used_names(call_chain_text)
    existing_records = get_chain_used_name_records(call_chain_text)
    existing_lookup: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
    for item in existing_records:
        if not isinstance(item, dict):
            continue
        key = (
            normalize_name_assignment_bucket(item.get("bucket")),
            normalize_candidate_name(item.get("candidate")),
        )
        if not key[1]:
            continue
        existing_lookup.setdefault(key, []).append(item)

    conflict_items: List[Dict[str, Any]] = []
    current_lookup: Dict[Tuple[str, str], List[str]] = {}
    for item in assignments:
        bucket_name = normalize_name_assignment_bucket(item.get("bucket"))
        candidate = normalize_candidate_name(item.get("candidate"))
        source_key = str(item.get("source_key", "") or "")
        if not candidate:
            continue
        current_key = (bucket_name, candidate)
        existing_sources = current_lookup.setdefault(current_key, [])
        if source_key:
            if existing_sources and source_key not in existing_sources:
                conflict_items.append({
                    "bucket": bucket_name,
                    "candidate": candidate,
                    "reason": "duplicate_in_batch",
                    "source_key": source_key,
                    "conflict_sources": list(existing_sources),
                })
            if source_key not in existing_sources:
                existing_sources.append(source_key)
        else:
            current_lookup.setdefault(current_key, existing_sources)

        historical_records = list(existing_lookup.get(current_key, []))
        if bucket_name != "general":
            historical_records.extend(existing_lookup.get(("general", candidate), []))
        if source_key:
            foreign_sources = unique_keep_order(
                [
                    str(record.get("source_key", "") or "")
                    for record in historical_records
                    if str(record.get("source_key", "") or "") and str(record.get("source_key", "") or "") != source_key
                ],
                limit=8,
            )
            if foreign_sources:
                conflict_items.append({
                    "bucket": bucket_name,
                    "candidate": candidate,
                    "reason": "cross_target_collision",
                    "source_key": source_key,
                    "conflict_sources": foreign_sources,
                })
            elif candidate in set(existing_names.get(bucket_name, [])) | set(existing_names.get("general", [])) and not historical_records:
                conflict_items.append({
                    "bucket": bucket_name,
                    "candidate": candidate,
                    "reason": "historical_name_without_identity",
                    "source_key": source_key,
                })
        elif candidate in set(existing_names.get(bucket_name, [])) | set(existing_names.get("general", [])) or historical_records:
            conflict_items.append({
                "bucket": bucket_name,
                "candidate": candidate,
                "reason": "missing_source_identity",
                "source_key": source_key,
                "conflict_sources": [
                    str(record.get("source_key", "") or "")
                    for record in historical_records
                    if str(record.get("source_key", "") or "")
                ],
            })

    if candidate_names:
        assigned_pairs = {
            (
                normalize_name_assignment_bucket(item.get("bucket")),
                normalize_candidate_name(item.get("candidate")),
            )
            for item in assignments
            if isinstance(item, dict) and normalize_candidate_name(item.get("candidate"))
        }
        for bucket_name, values in candidate_names.items():
            existing_values = set(existing_names.get(bucket_name, [])) | set(existing_names.get("general", []))
            for candidate in values:
                pair = (bucket_name, candidate)
                if pair in assigned_pairs:
                    continue
                if candidate in existing_values:
                    conflict_items.append({
                        "bucket": bucket_name,
                        "candidate": candidate,
                        "reason": "missing_source_identity",
                        "source_key": "",
                    })
    return build_name_conflict_report(conflict_items)


def build_name_conflict_message(call_chain_text: str,
                                conflicts: Dict[str, Any],
                                toolchain_key: str = "") -> str:
    lines = [
        "检测到本轮命名存在撞名或 source 身份不清，请重新返回唯一映射。",
    ]
    if toolchain_key:
        lines.append(f"工具链={toolchain_key}")
    if call_chain_text:
        lines.append(f"调用链={call_chain_text}")
    bucket_map = conflicts.get("by_bucket", {}) if isinstance(conflicts.get("by_bucket", {}), dict) else {}
    for bucket_name, values in bucket_map.items():
        if values:
            lines.append(f"重复{bucket_name}名={' | '.join(values[:6])}")
    detail_items = conflicts.get("items", []) if isinstance(conflicts.get("items", []), list) else []
    for item in detail_items[:6]:
        reason = str(item.get("reason", "") or "")
        candidate = str(item.get("candidate", "") or "")
        source_key = str(item.get("source_key", "") or "")
        conflict_sources = " | ".join(item.get("conflict_sources", [])[:3]) if isinstance(item.get("conflict_sources", []), list) else ""
        lines.append(f"冲突={reason} candidate={candidate} source={source_key or '-'} others={conflict_sources or '-'}")
    lines.append("要求：允许同一个 source 继续复用同一个 candidate；不同 source 不可复用同名。")
    lines.append("请只返回 naming_assignments 和 naming_candidates，不要主动输出改写代码或整段去混淆代码。")
    lines.append("严格限制：只处理本条消息里明确列出的 source、owner、candidate，不要根据上下文补新的类、方法、字段。")
    return "\n".join(lines)

def load_class_hits(class_key: str) -> Dict[str, Any]:
    normalized = normalize_class_key(class_key)
    if not normalized:
        return {}
    path = class_store_path(normalized)
    data = load_json_file(path)
    if not data:
        data = {
            "class_key": normalized,
            "aliases": [],
            "hit_count": 0,
            "sources": [],
            "updated_at": 0,
        }
    return data


def save_class_hits(data: Dict[str, Any]) -> str:
    class_key = normalize_class_key(str(data.get("class_key", "")))
    if not class_key:
        return ""
    data["class_key"] = class_key
    data["aliases"] = unique_keep_order(list(data.get("aliases", [])), limit=12)
    data["sources"] = list(data.get("sources", []))[-30:]
    data["hit_count"] = len(data["sources"])
    data["updated_at"] = time.time()
    path = class_store_path(class_key)
    save_json_file(path, data)
    return path


def record_class_hit(class_key: str,
                     source_path: str = "",
                     fragment: str = "",
                     tool: str = "",
                     command: str = "",
                     alias: str = "",
                     note: str = "") -> str:
    normalized = normalize_class_key(class_key)
    if not normalized:
        return ""
    data = load_class_hits(normalized)
    entry = {
        "source_path": source_path,
        "fragment": shorten_text(fragment, 480),
        "tool": tool,
        "command": shorten_text(command, 240),
        "note": shorten_text(note, 120),
        "ts": time.time(),
    }
    signature = json.dumps(entry, ensure_ascii=True, sort_keys=True)
    existing = {json.dumps(item, ensure_ascii=True, sort_keys=True) for item in data.get("sources", [])}
    if signature not in existing:
        data.setdefault("sources", []).append(entry)
    aliases = data.get("aliases", [])
    if alias:
        aliases.append(alias)
    if source_path:
        aliases.append(source_path)
    data["aliases"] = unique_keep_order(aliases, limit=12)
    return save_class_hits(data)


def collect_class_hit_prompt_lines(targets: List[str], limit_classes: int = 3) -> List[str]:
    lines: List[str] = []
    seen_keys: List[str] = []
    for target in targets:
        class_key = normalize_class_key(target)
        if not class_key or not (class_key.startswith("L") and class_key.endswith(";")) or class_key in seen_keys:
            continue
        seen_keys.append(class_key)
        data = load_class_hits(class_key)
        if not data.get("sources"):
            continue
        sources = data["sources"][-2:]
        lines.append(f"class={class_key} hits={data.get('hit_count', len(data['sources']))}")
        for source in sources:
            fragment = shorten_text(str(source.get("fragment", "")), 120)
            if fragment:
                lines.append(f"片段={fragment}")
        if len(seen_keys) >= limit_classes:
            break
    return lines


def auto_record_hits_from_message(text: str, source_label: str = "chat_user") -> List[str]:
    targets = [normalize_class_key(target) for target in extract_targets(text, limit=10)]
    targets = [target for target in targets if target.startswith("L") and target.endswith(";")]
    if not targets:
        return []

    lowered = (text or "").lower()
    has_path = bool(WINDOWS_PATH_PATTERN.findall(text or ""))
    has_code = bool(CODE_BLOCK_PATTERN.search(text or ""))
    has_hit_word = any(token in lowered for token in ["命中", "找到", "定位到", "位于", ".java", ".smali", "路径:", "path:"])
    if not (has_path or has_code or has_hit_word):
        return []

    fragment = extract_primary_fragment(text)
    source_path = ""
    path_matches = WINDOWS_PATH_PATTERN.findall(text or "")
    if path_matches:
        source_path = path_matches[0]

    stored_paths: List[str] = []
    for target in targets:
        stored_path = record_class_hit(
            class_key=target,
            source_path=source_path,
            fragment=fragment,
            tool=source_label,
            note="auto_record",
        )
        if stored_path:
            stored_paths.append(stored_path)
    return stored_paths


def discover_tool_context(config: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    config = localize_tool_dispatcher_config(config if isinstance(config, dict) else load_tool_dispatcher_config())
    jadx_patterns: List[str] = []
    for tool_dir in TOOL_DIR_CANDIDATES:
        jadx_patterns.extend([
            os.path.join(tool_dir, "**", "jadx.bat"),
            os.path.join(tool_dir, "**", "jadx.exe"),
            os.path.join(tool_dir, "**", "jadx-cli*.jar"),
            os.path.join(tool_dir, "**", "jadx*.jar"),
        ])
    jadx_path = find_first_existing(jadx_patterns) or os.path.join(TOOLS_DIR, "jadx.jar")
    baksmali_path = find_latest_file([
        *build_workspace_patterns(config, "baksmali", "build", "libs", "baksmali-*-fat.jar"),
    ]) or os.path.join(resolve_workspace_root(config), "baksmali", "build", "libs", "baksmali-fat.jar")
    smali_path = find_latest_file([
        *build_workspace_patterns(config, "smali", "build", "libs", "smali-*-fat.jar"),
    ]) or os.path.join(resolve_workspace_root(config), "smali", "build", "libs", "smali-fat.jar")

    return {
        "jadx": jadx_path,
        "jadx_toolchain": resolve_helper_script_path("jadx_toolchain.py"),
        "tool_dispatcher": resolve_helper_script_path("tool_dispatcher.py"),
        "baksmali": baksmali_path,
        "smali": smali_path,
        "start_powershell": resolve_helper_script_path("start_powershell.py"),
        "smali_converter": resolve_helper_script_path("smali_converter.py"),
    }


def shorten_text(text: str, limit: int = 120) -> str:
    text = " ".join(((text or "").replace("\ufeff", "")).strip().split())
    if len(text) <= limit:
        return text
    return text[:limit - 3] + "..."


def unique_keep_order(values: List[str], limit: int = 6) -> List[str]:
    result: List[str] = []
    for value in values:
        if not value or value in result:
            continue
        result.append(value)
        if len(result) >= limit:
            break
    return result


def extract_targets(text: str, limit: int = 6) -> List[str]:
    if not text:
        return []
    matches: List[str] = []
    matches.extend(extract_method_targets(text, limit=limit))
    matches.extend(SMALI_DESCRIPTOR_PATTERN.findall(text))
    matches.extend(WINDOWS_PATH_PATTERN.findall(text))
    matches.extend(JAVA_CLASS_PATTERN.findall(text))
    matches.extend(match.group(0) for match in TOOL_WORD_PATTERN.finditer(text))
    return unique_keep_order([shorten_text(item, 72) for item in matches], limit=limit)


def extract_attention_notes(text: str, limit: int = 3) -> List[str]:
    source = text or ""
    lowered = source.lower()
    markers = ["注意力:", "注意力：", "专注:", "专注：", "记住:", "记住：", "额外约束:", "额外约束：", "focus:", "focus："]
    notes: List[str] = []

    for marker in markers:
        start = lowered.find(marker.lower())
        if start == -1:
            continue
        content = source[start + len(marker):].strip()
        if content:
            notes.append(shorten_text(content, 80))
    return unique_keep_order(notes, limit=limit)


def is_error_like_text(text: str) -> bool:
    lowered = (text or "").lower()
    error_tokens = [
        "error",
        "exception",
        "traceback",
        "failed",
        "stderr",
        "warning",
        "错误",
        "失败",
        "异常",
        "日志",
    ]
    return any(token in lowered for token in error_tokens)


def resolve_local_chat_mode(profile: str,
                            text: str = "",
                            feedback_kind: str = "") -> Dict[str, Any]:
    lowered = (text or "").lower()
    if feedback_kind == "error" or is_error_like_text(text):
        return {
            "think_mode": ERROR_LOCAL_THINK_MODE,
            "use_auto_cot": False,
            "use_deep_think": False,
            "mode_label": "error_fast",
        }
    if profile == "jadx_code_analyst" or "jadx" in lowered:
        return {
            "think_mode": JADX_LOCAL_THINK_MODE,
            "use_auto_cot": True,
            "use_deep_think": False,
            "mode_label": "jadx_finalize",
        }
    return {
        "think_mode": DEFAULT_LOCAL_THINK_MODE,
        "use_auto_cot": True,
        "use_deep_think": False,
        "mode_label": "default_trace",
    }


def classify_prompt_profile(user_message: str,
                            current_profile: str = DEFAULT_PROFILE,
                            forced_profile: str = "auto") -> Tuple[str, str]:
    if forced_profile and forced_profile != "auto" and forced_profile in PROMPT_PROFILES:
        return forced_profile, f"手动指定:{PROMPT_PROFILES[forced_profile]['label']}"

    text = (user_message or "").lower()
    scores: Dict[str, int] = {key: 0 for key in PROMPT_PROFILES}
    reasons: Dict[str, List[str]] = {key: [] for key in PROMPT_PROFILES}

    for profile_id, profile in PROMPT_PROFILES.items():
        for keyword in profile["keywords"]:
            if keyword.lower() in text:
                scores[profile_id] += 2
                reasons[profile_id].append(keyword)

    if "jadx" in text and ("命名" in user_message or "类" in user_message):
        scores["naming_finalize"] += 3
        reasons["naming_finalize"].append("jadx+命名")
    if "jadx" in text and any(token in user_message for token in ["代码", "语义", "文件名", "还原", "控制流"]):
        scores["jadx_code_analyst"] += 4
        reasons["jadx_code_analyst"].append("jadx+代码语义")
    if "引用" in user_message or "调用链" in user_message:
        scores["xref_analysis"] += 3
        reasons["xref_analysis"].append("引用/调用链")
    if "旧字段" in user_message or "新字段" in user_message:
        scores["rename_mapping"] += 3
        reasons["rename_mapping"].append("旧字段/新字段")

    best_profile = max(scores, key=scores.get)
    if scores[best_profile] == 0:
        return current_profile or DEFAULT_PROFILE, "沿用当前模式"

    if current_profile in PROMPT_PROFILES and current_profile != DEFAULT_PROFILE and scores[best_profile] < 3:
        return current_profile, f"弱命中，沿用:{PROMPT_PROFILES[current_profile]['label']}"

    matched = ",".join(unique_keep_order(reasons[best_profile], limit=4)) or "自动匹配"
    return best_profile, matched


def build_name_stat_lines(stat_records: Optional[List[Dict[str, Any]]], limit: int = 4) -> List[str]:
    lines: List[str] = []
    for item in list(stat_records or [])[:limit]:
        if not isinstance(item, dict):
            continue
        bucket_name = str(item.get("bucket", "") or "general")
        candidate = normalize_candidate_name(item.get("candidate"))
        if not candidate:
            continue
        hits = max(1, int(item.get("hits", 1) or 1))
        source_key = shorten_text(str(item.get("source_key", "") or ""), 60)
        if source_key:
            lines.append(f"{bucket_name}:{candidate}@{source_key} x{hits}")
        else:
            lines.append(f"{bucket_name}:{candidate} x{hits}")
    return lines


def build_memory_lines(session_data: Optional[Dict[str, Any]]) -> List[str]:
    if not session_data:
        return []
    lines: List[str] = []
    primary_goal = shorten_text(session_data.get("primary_goal", ""), 96)
    if primary_goal:
        lines.append(f"goal={primary_goal}")
    attention_notes = session_data.get("attention_notes", [])
    if attention_notes:
        lines.append(f"focus={' | '.join(attention_notes[:3])}")
    recent_targets = session_data.get("recent_targets", [])
    if recent_targets:
        lines.append(f"targets={' | '.join(recent_targets[:4])}")
    recent_commands = session_data.get("recent_commands", [])
    if recent_commands:
        lines.append(f"commands={' | '.join(recent_commands[:2])}")
    analysis_fragments = [
        fragment for fragment in session_data.get("analysis_fragments", [])
        if sanitize_analysis_fragment(str(fragment))
    ]
    if analysis_fragments:
        lines.append(f"fragments={' | '.join(analysis_fragments[:2])}")
    chain_backgrounds = [
        fragment for fragment in session_data.get("call_chain_backgrounds", [])
        if sanitize_analysis_fragment(str(fragment))
    ]
    if chain_backgrounds:
        lines.append(f"chain_bg={' | '.join(chain_backgrounds[:2])}")
    used_names = session_data.get("used_names", {}) if isinstance(session_data.get("used_names", {}), dict) else {}
    for bucket_name in ("class", "method", "field"):
        values = [normalize_candidate_name(item) for item in used_names.get(bucket_name, [])]
        values = [item for item in values if item]
        if values:
            lines.append(f"used_{bucket_name}={' | '.join(values[:4])}")
    name_stat_lines = [str(item) for item in session_data.get("name_stat_lines", []) if str(item).strip()]
    if name_stat_lines:
        lines.append(f"name_stats={' | '.join(name_stat_lines[:3])}")
    return lines


def normalize_local_runtime_block(raw_block: Any) -> Dict[str, Any]:
    if not isinstance(raw_block, dict):
        return {}
    output_preview = shorten_text(
        first_nonempty(
            str(raw_block.get("output_preview", "") or ""),
            str(raw_block.get("artifact_preview", "") or ""),
            str(raw_block.get("stdout_preview", "") or ""),
            str(raw_block.get("stdout", "") or ""),
        ),
        220,
    )
    error_preview = shorten_text(
        first_nonempty(
            str(raw_block.get("error_preview", "") or ""),
            str(raw_block.get("stderr_preview", "") or ""),
            str(raw_block.get("stderr", "") or ""),
        ),
        220,
    )
    target_refs = unique_keep_order(
        [item for item in (
            normalize_class_key(str(value))
            for value in list(raw_block.get("target_refs", []))[:8]
        ) if item]
        + extract_targets(str(raw_block.get("call_chain_text", "") or ""), limit=4)
        + extract_targets(str(raw_block.get("next_query", "") or ""), limit=4),
        limit=6,
    )
    block = {
        "kind": str(raw_block.get("kind", "") or "tool_result"),
        "tool": shorten_text(str(raw_block.get("tool", "") or ""), 48),
        "profile": str(raw_block.get("profile", "") or ""),
        "command_type": str(raw_block.get("command_type", "") or ""),
        "toolchain_key": str(raw_block.get("toolchain_key", "") or ""),
        "status": str(raw_block.get("status", "") or ""),
        "judge_status": int(raw_block.get("judge_status", 0) or 0),
        "returncode": int(raw_block.get("returncode", 0) or 0),
        "has_output": bool(raw_block.get("has_output")),
        "hit_class": normalize_class_key(str(raw_block.get("hit_class", "") or "")),
        "next_query": shorten_text(str(raw_block.get("next_query", "") or ""), 120),
        "output_preview": output_preview,
        "error_preview": error_preview,
        "target_refs": target_refs,
        "artifact_paths": unique_keep_order([str(path) for path in list(raw_block.get("artifact_paths", []))[:8] if path], limit=8),
        "bridge_target": str(raw_block.get("bridge_target", "") or ""),
        "updated_at": float(raw_block.get("updated_at", time.time()) or time.time()),
    }
    return {
        key: value for key, value in block.items()
        if value not in ("", [], None, {}) or key in {"kind", "status", "judge_status", "has_output", "updated_at"}
    }


def build_local_runtime_result_block(result: Dict[str, Any],
                                     route_item: Optional[Dict[str, Any]] = None,
                                     call_chain_text: str = "") -> Dict[str, Any]:
    route_item = route_item if isinstance(route_item, dict) else {}
    judge_status = int(result.get("judge_status", 1) or 1)
    has_output = bool(result.get("has_output"))
    if judge_status != 0:
        status = "error"
    elif has_output:
        status = "success_output"
    else:
        status = "success_no_output"
    return normalize_local_runtime_block({
        "kind": "tool_result",
        "tool": str(result.get("tool", "") or route_item.get("tool", "") or ""),
        "profile": str(route_item.get("profile", "") or result.get("profile", "") or ""),
        "command_type": str(route_item.get("command_type", "") or ""),
        "toolchain_key": str(route_item.get("toolchain_key", "") or ""),
        "status": status,
        "judge_status": judge_status,
        "returncode": int(result.get("returncode", 0) or 0),
        "has_output": has_output,
        "hit_class": str(result.get("hit_class", "") or ""),
        "next_query": str(result.get("next_query", "") or ""),
        "stdout_preview": str(result.get("stdout_preview", "") or result.get("artifact_preview", "") or result.get("stdout", "") or ""),
        "stderr_preview": str(result.get("stderr_preview", "") or result.get("stderr", "") or ""),
        "artifact_paths": list(result.get("artifact_paths", []))[:8] if isinstance(result.get("artifact_paths", []), list) else [],
        "target_refs": list(route_item.get("targets", [])) + extract_targets(call_chain_text, limit=6),
        "call_chain_text": call_chain_text,
        "updated_at": time.time(),
    })


def build_local_runtime_lines(session_data: Optional[Dict[str, Any]]) -> List[str]:
    if not session_data:
        return []
    lines: List[str] = []
    for raw_block in list(session_data.get("local_runtime_blocks", []))[:3]:
        block = normalize_local_runtime_block(raw_block)
        if not block:
            continue
        phase = first_nonempty(
            str(block.get("toolchain_key", "") or ""),
            str(block.get("command_type", "") or ""),
            str(block.get("tool", "") or ""),
        )
        parts = [
            f"kind={block.get('kind', 'tool_result')}",
            f"phase={phase or '-'}",
            f"status={block.get('status', '-')}",
        ]
        if block.get("hit_class"):
            parts.append(f"hit={block.get('hit_class')}")
        targets = list(block.get("target_refs", []))
        if targets:
            parts.append(f"targets={' / '.join(targets[:3])}")
        if block.get("next_query"):
            parts.append(f"next={block.get('next_query')}")
        artifact_paths = [str(path) for path in list(block.get("artifact_paths", []))[:8] if path]
        if str(block.get("toolchain_key", "") or "") == "jadx_named_chain" and str(block.get("status", "") or "").startswith("success"):
            lowered_artifacts = [path.lower() for path in artifact_paths]
            if any("named_export" in path or command_targets_named_export_output(path) for path in lowered_artifacts):
                parts.append("named_export=ready")
                parts.append("next_stage=analysis_pkg")
            if any("analysis_pkg" in path or command_targets_analysis_pkg_output(path) for path in lowered_artifacts):
                parts.append("analysis_pkg=ready")
        preview = first_nonempty(
            str(block.get("error_preview", "") or ""),
            str(block.get("output_preview", "") or ""),
        )
        if preview:
            parts.append(f"preview={preview}")
        if block.get("bridge_target"):
            parts.append(f"bridge={block.get('bridge_target')}")
        lines.append(" | ".join(parts))
    return lines


def infer_profile_from_local_runtime(session_data: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    if not isinstance(session_data, dict):
        return "", ""
    blocks = list(session_data.get("local_runtime_blocks", []))
    if not blocks:
        return "", ""
    for raw_block in blocks[:3]:
        block = normalize_local_runtime_block(raw_block)
        toolchain_key = str(block.get("toolchain_key", "") or "")
        tool = str(block.get("tool", "") or "")
        if toolchain_key == "jadx_named_chain":
            return "jadx_code_analyst", "本地结构:jadx_named_chain"
        if toolchain_key == "rename_tuning_chain":
            return "rename_tuning", "本地结构:rename_tuning_chain"
        if toolchain_key == "jadx_source_chain" or tool == "jadx":
            return "jadx_code_analyst", "本地结构:jadx_source_chain"
        if toolchain_key in {"rename_followup_chain", "rename_mapping_chain"}:
            return "rename_mapping", "本地结构:rename_followup_chain"
        if toolchain_key in {"baksmali_xref_chain", "baksmali_subclasses_chain"} or tool in {"baksmali_xref", "subclasses", "baksmali_subclasses"}:
            return "xref_analysis", f"本地结构:{toolchain_key or tool}"
    return "", ""


def build_session_context_payload(stored_session: Optional[Dict[str, Any]],
                                  active_route_entry: Optional[Dict[str, Any]],
                                  call_chain_text: str,
                                  message: str,
                                  profile: str,
                                  profile_reason: str,
                                  focus_note: str = "",
                                  toolchain_config: Optional[Dict[str, Any]] = None,
                                  force_refresh: bool = False,
                                  suppress_local_context: bool = False) -> Dict[str, Any]:
    stored_session = stored_session if isinstance(stored_session, dict) else {}
    active_route_entry = active_route_entry if isinstance(active_route_entry, dict) else {}
    refresh_turns = get_role_prompt_refresh_turns(toolchain_config)
    previous_user_turns = int(stored_session.get("user_turn_count", 0) or 0)
    next_user_turn = previous_user_turns + 1
    last_prompt_refresh_turn = int(stored_session.get("last_prompt_refresh_user_turn", 0) or 0)
    role_prompt_refresh_due = bool(
        force_refresh
        or next_user_turn <= 1
        or (next_user_turn - last_prompt_refresh_turn) >= refresh_turns
    )
    chain_backgrounds = [] if suppress_local_context else get_chain_background_fragments(call_chain_text, limit=4)
    used_names = {} if suppress_local_context else get_chain_used_names(call_chain_text)
    name_stat_lines = [] if suppress_local_context else build_name_stat_lines(get_chain_name_stat_records(call_chain_text), limit=6)
    chain_runtime = build_call_chain_runtime_context(call_chain_text, config=toolchain_config)
    return {
        "profile": profile,
        "profile_reason": profile_reason,
        "thread_group_ref": "" if suppress_local_context else str(active_route_entry.get("group_ref", "") or stored_session.get("thread_group_ref", "") or ""),
        "thread_slot_ref": "" if suppress_local_context else str(active_route_entry.get("slot_ref", "") or stored_session.get("thread_slot_ref", "") or ""),
        "primary_goal": shorten_text(str(active_route_entry.get("last_user_message", "") or stored_session.get("primary_goal", "") or message), 160),
        "attention_notes": unique_keep_order(list(stored_session.get("attention_notes", [])) + ([focus_note] if focus_note else []), limit=4),
        "recent_targets": unique_keep_order(
            list(stored_session.get("recent_targets", []))
            + list(active_route_entry.get("targets", []))
            + extract_targets(call_chain_text, limit=8)
            + extract_targets(message, limit=8),
            limit=8,
        ),
        "recent_commands": [] if suppress_local_context else unique_keep_order(
            list(stored_session.get("recent_commands", []))
            + (list(active_route_entry.get("recent_commands", []))[:4] if isinstance(active_route_entry, dict) else []),
            limit=6,
        ),
        "analysis_fragments": [] if suppress_local_context else unique_keep_order(list(stored_session.get("analysis_fragments", [])), limit=4),
        "call_chain_backgrounds": chain_backgrounds,
        "used_names": used_names,
        "name_stat_lines": name_stat_lines,
        "call_chain_runtime": {
            key: str(chain_runtime.get(key, "") or "")
            for key in (
                "chain_work_dir",
                "chain_output_dir",
                "chain_smali_dir",
                "chain_jadx_dir",
                "chain_jadx_named_dir",
                "chain_jadx_analysis_dir",
                "chain_index_file",
            )
            if chain_runtime.get(key)
        },
        "local_runtime_blocks": [] if suppress_local_context else [
            block
            for block in (
                normalize_local_runtime_block(item)
                for item in list(stored_session.get("local_runtime_blocks", []))[:4]
            )
            if block
        ],
        "user_turn_count": next_user_turn,
        "last_prompt_refresh_user_turn": last_prompt_refresh_turn,
        "role_prompt_refresh_due": role_prompt_refresh_due,
        "role_prompt_refresh_turns": refresh_turns,
    }

def build_runtime_prompt(user_message: str,
                         session_data: Optional[Dict[str, Any]] = None,
                         forced_profile: str = "auto",
                         is_initial: bool = False,
                         call_chain_text: str = "",
                         route_entry: Optional[Dict[str, Any]] = None) -> Tuple[str, str, str]:
    toolchain_config = load_tool_dispatcher_config()
    tools = discover_tool_context(toolchain_config)
    current_profile = (session_data or {}).get("profile", DEFAULT_PROFILE)
    profile_id, profile_reason = classify_prompt_profile(user_message, current_profile, forced_profile)
    if forced_profile in ("", "auto"):
        local_profile, local_reason = infer_profile_from_local_runtime(session_data)
        if local_profile and profile_reason == "沿用当前模式":
            profile_id, profile_reason = local_profile, local_reason
    profile = PROMPT_PROFILES[profile_id]
    focus_lines = profile["init_focus"] if is_initial else profile["turn_focus"]
    memory_lines = build_memory_lines(session_data)
    local_runtime_lines = build_local_runtime_lines(session_data)
    named_jadx_stage_lines = build_named_jadx_stage_lines(session_data, call_chain_text, config=toolchain_config)
    prompt_targets = unique_keep_order(
        extract_targets(user_message, limit=8) + list((session_data or {}).get("recent_targets", [])),
        limit=8,
    )
    class_hit_lines = collect_class_hit_prompt_lines(prompt_targets)
    chain_hint_lines = build_chain_hint_lines(user_message, call_chain_text, tools=tools, route_entry=route_entry)
    config_lines = build_toolchain_config_lines(toolchain_config)
    alias_lines = build_runtime_alias_lines(toolchain_config, tools, call_chain_text=call_chain_text)
    send_full_header = bool(is_initial or (session_data or {}).get("role_prompt_refresh_due"))

    lines = [
        "[角色]",
        "你是逆向检索助手，只返回可执行命令。",
        f"[模式]{profile['label']}",
        f"[匹配]{profile_reason}",
    ]
    if send_full_header:
        lines.append("[规则]")
        lines.extend(COMMON_RULE_LINES)
    else:
        lines.append("[续聊规则]")
        lines.append("沿用已有角色卡、JSON字段和工具协议，只围绕本轮本地结果继续。")
    if is_initial:
        lines.append("[首轮动作]")
        lines.append("先判断任务分类，再只返回当前最合适的一条或多条命令JSON。")
        lines.append("如果目标类未命中，先给JADX或xref检索命令，不直接命名。")
    if memory_lines:
        lines.append("[记忆]")
        lines.extend(memory_lines)
    if local_runtime_lines:
        lines.append("[本地结构]")
        lines.extend(local_runtime_lines)
    if class_hit_lines:
        lines.append("[类命中片段]")
        lines.extend(class_hit_lines)
    if call_chain_text:
        lines.append("[调用链]")
        lines.append(call_chain_text)
    if chain_hint_lines:
        lines.append("[命令提示]")
        lines.extend(chain_hint_lines)
    if alias_lines and send_full_header:
        lines.append("[运行时别名]")
        lines.extend(alias_lines)
    if config_lines and send_full_header:
        lines.append("[初始化配置]")
        lines.extend(config_lines)
    if named_jadx_stage_lines:
        lines.append("[NamedJADX阶段]")
        lines.extend(named_jadx_stage_lines)
    lines.append("[本轮专注]")
    lines.extend(focus_lines)
    if send_full_header:
        lines.append("[JSON字段]")
        lines.append("{\"profile\":\"tool_dispatch|xref_analysis|rename_mapping|rename_tuning|naming_finalize|jadx_code_analyst\",\"tool\":\"jadx_toolchain|jadx|baksmali_xref|baksmali_subclasses|start_powershell|smali_converter\",\"command\":\"powershell.exe ...\",\"reason\":\"...\",\"store_back\":\"[OyUt]{旧的路径}{[片段信息]}\",\"hit_class\":\"Lxx/yy;\",\"hit_store\":\"class_hit_store/...json\",\"next_query\":\"...\",\"naming_candidates\":{\"class\":[\"...\"],\"method\":[\"...\"],\"field\":[\"...\"]},\"naming_assignments\":[{\"kind\":\"field\",\"source\":\"Lxx/yy;->a:I\",\"candidate\":\"userId\",\"owner\":\"Lxx/yy;\"}]}")
        lines.append("[工具]")
        for tool_name in profile["preferred_tools"]:
            tool_value = tools.get(tool_name, "")
            if tool_value:
                lines.append(f"{tool_name}={tool_value}")
        tool_dispatcher_path = tools.get("tool_dispatcher", "")
        if tool_dispatcher_path:
            lines.append(f"tool_dispatcher={tool_dispatcher_path}")
    lines.append("[用户请求]")
    lines.append(user_message.strip() or DEFAULT_INIT_MSG)
    lines.append("[结尾]")
    lines.append("{")
    lines.append("[ECSA]")
    lines.append("[现在是用户的回合]")
    lines.append("}")
    return "\n".join(lines), profile_id, profile_reason


def build_initial_prompt(user_message: str,
                         forced_profile: str = "auto",
                         call_chain_text: str = "",
                         route_entry: Optional[Dict[str, Any]] = None) -> str:
    prompt, _, _ = build_runtime_prompt(
        user_message,
        session_data=None,
        forced_profile=forced_profile,
        is_initial=True,
        call_chain_text=call_chain_text,
        route_entry=route_entry,
    )
    return prompt


def is_command_like_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    for key in ("tool", "command", "next_query", "store_back", "hit_class"):
        value = str(item.get(key, "") or "").strip()
        if value:
            return True
    return False


def extract_command_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if is_command_like_item(item)]
    if isinstance(payload, dict):
        nested = payload.get("commands")
        if isinstance(nested, list):
            return [item for item in nested if is_command_like_item(item)]
        if is_command_like_item(payload):
            return [payload]
    return []


def collect_command_items_from_text(reply_text: str) -> List[Dict[str, Any]]:
    text = str(reply_text or "")
    items: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def signature_for_item(item: Dict[str, Any]) -> str:
        core = {
            "profile": str(item.get("profile", "") or "").strip(),
            "tool": str(item.get("tool", "") or "").strip(),
            "command": str(item.get("command", "") or "").strip(),
            "store_back": str(item.get("store_back", "") or "").strip(),
            "hit_class": str(item.get("hit_class", "") or "").strip(),
            "hit_store": str(item.get("hit_store", "") or "").strip(),
            "next_query": str(item.get("next_query", "") or "").strip(),
        }
        try:
            return json.dumps(core, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return repr(core)

    def add_items(payload: Any) -> None:
        for item in extract_command_items(payload):
            signature = signature_for_item(item)
            if signature in seen:
                continue
            seen.add(signature)
            items.append(item)

    matches = list(JSON_BLOCK_PATTERN.finditer(text))
    if matches:
        for match in matches:
            raw_json = match.group(1).strip()
            if not raw_json:
                continue
            try:
                parsed = json.loads(raw_json)
            except json.JSONDecodeError:
                continue
            add_items(parsed)
        return items

    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return []
        add_items(parsed)
    return items


def extract_flag_value(command_text: str, flag_name: str) -> str:
    if not command_text or not flag_name:
        return ""
    pattern = re.compile(rf"(?:^|\s){re.escape(flag_name)}\s+(\"[^\"]+\"|'[^']+'|\S+)")
    match = pattern.search(command_text)
    if not match:
        return ""
    value = str(match.group(1) or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def extract_first_flag_value(command_text: str, *flag_names: str) -> str:
    for flag_name in flag_names:
        value = extract_flag_value(command_text, flag_name)
        if value:
            return value
    return ""


def extract_flag_values(command_text: str, flag_name: str, limit: int = 8) -> List[str]:
    if not command_text or not flag_name:
        return []
    pattern = re.compile(
        rf"(?:^|\s){re.escape(flag_name)}(?:\s+|=)(\"[^\"]+\"|'[^']+'|[^\s]+)"
    )
    values: List[str] = []
    for match in pattern.finditer(command_text):
        value = str(match.group(1) or "").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        value = value.strip()
        if value:
            values.append(value)
    return unique_keep_order(values, limit=limit)


def normalize_archive_sources_alias_flags(command_text: str) -> str:
    text = decode_unicode_escape_sequences(str(command_text or ""))
    if not text:
        return ""
    text = text.replace("--mirror-package", "--sync-package")
    text = text.replace("--update-index", "--refresh-index")
    return text


def command_requests_smali_apply_renames(tool: str, command_text: str) -> bool:
    lowered_tool = str(tool or "").strip().lower()
    lowered_command = f" {str(command_text or '').lower()} "
    if not lowered_command.strip():
        return False
    if any(token in lowered_command for token in (" --rename-class ", " --rename-method ", " --rename-field ")):
        return True
    if lowered_tool == "baksmali" and " modify " in lowered_command:
        return True
    if lowered_tool == "smali_converter" and " apply-renames " in lowered_command:
        return True
    return False


def build_smali_apply_renames_command(command_text: str,
                                      config: Optional[Dict[str, Any]] = None,
                                      call_chain_text: str = "") -> str:
    class_values = extract_flag_values(command_text, "--rename-class", limit=32)
    method_values = extract_flag_values(command_text, "--rename-method", limit=64)
    field_values = extract_flag_values(command_text, "--rename-field", limit=64)
    if not class_values and not method_values and not field_values:
        return ""

    chain_runtime = build_call_chain_runtime_context(call_chain_text, config=config)
    raw_input = extract_first_flag_value(command_text, "-i", "--input")
    raw_output = extract_first_flag_value(command_text, "-o", "--output")
    chain_smali_dir = normalize_runtime_root(str(chain_runtime.get("chain_smali_dir", "") or ""))
    default_output = "{chain_smali_dir}" if chain_runtime.get("chain_smali_dir") else "{smali_dir}"
    output_value = raw_output or default_output
    if chain_smali_dir and normalize_runtime_root(output_value).lower() == chain_smali_dir.lower():
        output_value = default_output

    input_value = raw_input or "{target_dex}"
    if (
        not raw_input
        or (chain_smali_dir and normalize_runtime_root(raw_input).lower() == chain_smali_dir.lower())
        or (raw_output and normalize_runtime_root(raw_input).lower() == normalize_runtime_root(raw_output).lower())
    ):
        input_value = "{target_dex}"

    command_parts = [
        "python",
        "{smali_converter}",
        "apply-renames",
        "-i",
        input_value,
        "-o",
        output_value,
        "--baksmali-jar",
        "{baksmali}",
    ]
    if chain_runtime.get("chain_renamed_dex"):
        command_parts.extend([
            "--smali-jar",
            "{smali}",
            "--rebuilt-dex",
            "{chain_renamed_dex}",
        ])
    for value in class_values:
        command_parts.extend(["--class-rename", f"\"{value}\""])
    for value in method_values:
        command_parts.extend(["--method-rename", f"\"{value}\""])
    for value in field_values:
        command_parts.extend(["--field-rename", f"\"{value}\""])
    return " ".join(command_parts)


def build_smali_apply_renames_command_from_assignments(name_assignments: Optional[List[Dict[str, Any]]],
                                                       config: Optional[Dict[str, Any]] = None,
                                                       call_chain_text: str = "") -> str:
    assignments = merge_name_assignments([], name_assignments or [], limit=96)
    if not assignments:
        return ""

    class_values: List[str] = []
    method_values: List[str] = []
    field_values: List[str] = []
    for item in assignments:
        if not isinstance(item, dict):
            continue
        bucket = normalize_name_assignment_bucket(item.get("bucket"))
        candidate = normalize_candidate_name(item.get("candidate"))
        source_key = str(item.get("source_key", "") or "")
        if not candidate or not source_key:
            continue
        if bucket == "class":
            normalized_source = normalize_class_key(source_key)
            if not normalized_source:
                continue
            source_name = normalized_source[1:-1].split("/")[-1]
            if candidate == source_name:
                continue
            class_values.append(f"{normalized_source}={candidate}")
        elif bucket == "method":
            method_targets = extract_method_targets(source_key, limit=1)
            if not method_targets:
                continue
            method_target = method_targets[0]
            source_name = method_target.split("->", 1)[-1].split("(", 1)[0]
            if candidate == source_name:
                continue
            method_values.append(f"{method_target}={candidate}")
        elif bucket == "field":
            field_targets = extract_field_targets(source_key, limit=1)
            if not field_targets:
                continue
            field_target = field_targets[0]
            source_name = field_target.split("->", 1)[-1].split(":", 1)[0]
            if candidate == source_name:
                continue
            field_values.append(f"{field_target}={candidate}")
    class_values = unique_keep_order(class_values, limit=32)
    method_values = unique_keep_order(method_values, limit=64)
    field_values = unique_keep_order(field_values, limit=64)
    if not class_values and not method_values and not field_values:
        return ""

    chain_runtime = build_call_chain_runtime_context(call_chain_text, config=config)
    output_value = "{chain_smali_dir}" if chain_runtime.get("chain_smali_dir") else "{smali_dir}"
    command_parts = [
        "python",
        "{smali_converter}",
        "apply-renames",
        "-i",
        "{target_dex}",
        "-o",
        output_value,
        "--baksmali-jar",
        "{baksmali}",
    ]
    if chain_runtime.get("chain_renamed_dex"):
        command_parts.extend([
            "--smali-jar",
            "{smali}",
            "--rebuilt-dex",
            "{chain_renamed_dex}",
        ])
    for value in class_values:
        command_parts.extend(["--class-rename", f"\"{value}\""])
    for value in method_values:
        command_parts.extend(["--method-rename", f"\"{value}\""])
    for value in field_values:
        command_parts.extend(["--field-rename", f"\"{value}\""])
    return " ".join(command_parts)


def build_local_smali_apply_command_item(call_chain_text: str,
                                         name_assignments: Optional[List[Dict[str, Any]]],
                                         config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    assignments = merge_name_assignments([], name_assignments or [], limit=96)
    command_text = build_smali_apply_renames_command_from_assignments(
        assignments,
        config=config,
        call_chain_text=call_chain_text,
    )
    if not command_text:
        return {}
    focus_classes = unique_keep_order(
        [
            infer_owner_class_from_source(str(item.get("source_key", "") or ""), fallback_owner=str(item.get("owner_class", "") or ""))
            for item in assignments
            if isinstance(item, dict)
        ] + extract_class_targets(call_chain_text, limit=4),
        limit=4,
    )
    raw_assignments = [
        {
            "kind": str(item.get("bucket", "") or ""),
            "source": str(item.get("source_key", "") or ""),
            "candidate": str(item.get("candidate", "") or ""),
            "owner": str(item.get("owner_class", "") or ""),
        }
        for item in assignments[:32]
        if isinstance(item, dict)
    ]
    return {
        "profile": "tool_dispatch",
        "tool": "smali_converter",
        "command": command_text,
        "reason": "本地根据稳定 naming_assignments 先执行 smali apply-renames，待 renamed_dex 就绪后再触发 named JADX。",
        "next_query": "smali 改名完成后，只对当前调用链类执行 named JADX，并回主线程整理结果。",
        "hit_class": focus_classes[0] if focus_classes else "",
        "naming_assignments": raw_assignments,
    }


def extract_focus_class_targets_from_text(text: str, limit: int = 6) -> List[str]:
    if not text:
        return []
    results: List[str] = []
    token_pattern = re.compile(r"(L[\w/$.-]+;|(?:[A-Za-z_]\w*\.){1,}[A-Z_$][\w$]*)")

    for method_target in extract_method_targets(text, limit=limit * 2):
        owner = normalize_class_key(method_target.split("->", 1)[0])
        if owner:
            results.append(owner)

    for raw_line in str(text).splitlines():
        stripped = raw_line.strip().strip("`\"'")
        if not stripped or "->" in stripped:
            continue
        for match in token_pattern.findall(stripped):
            normalized = normalize_class_key(match)
            if normalized:
                results.append(normalized)
    return unique_keep_order(results, limit=limit)


def collect_focus_class_targets(command_text: str,
                                item: Optional[Dict[str, Any]] = None,
                                call_chain_text: str = "",
                                limit: int = 4) -> List[str]:
    normalized_item = item if isinstance(item, dict) else {}
    candidates: List[str] = []

    hit_class = normalize_class_key(str(normalized_item.get("hit_class", "") or ""))
    if hit_class:
        candidates.append(hit_class)

    for flag_name in ("--target_class", "--class", "--class-query", "-class", "-c"):
        candidates.extend(extract_flag_values(command_text, flag_name, limit=limit * 2))

    target_method = extract_first_flag_value(command_text, "--target_method", "--method", "-method", "-m")
    if target_method:
        candidates.extend(extract_focus_class_targets_from_text(target_method, limit=limit))

    candidates.extend(extract_focus_class_targets_from_text(command_text, limit=limit * 2))
    candidates.extend(extract_focus_class_targets_from_text(str(normalized_item.get("store_back", "") or ""), limit=limit))
    candidates.extend(extract_focus_class_targets_from_text(call_chain_text, limit=limit * 2))

    normalized_targets: List[str] = []
    for candidate in candidates:
        normalized = normalize_class_key(str(candidate or ""))
        if normalized:
            normalized_targets.append(normalized)
    return unique_keep_order(normalized_targets, limit=limit)


def build_narrow_jadx_export_command(target_classes: List[str],
                                     config: Optional[Dict[str, Any]] = None,
                                     call_chain_text: str = "",
                                     output_alias: str = "") -> str:
    chain_runtime = build_call_chain_runtime_context(call_chain_text, config=config)
    resolved_output_alias = str(output_alias or "").strip()
    if resolved_output_alias and not resolved_output_alias.startswith("{") and chain_runtime.get(resolved_output_alias):
        resolved_output_alias = "{" + resolved_output_alias + "}"
    if not resolved_output_alias:
        resolved_output_alias = "{chain_jadx_dir}" if chain_runtime.get("chain_jadx_dir") else "{jadx_out}"
    input_alias = "{target_dex}"
    renamed_dex = str(chain_runtime.get("chain_renamed_dex", "") or "")
    if (
        resolved_output_alias == "{chain_jadx_named_dir}"
        and renamed_dex
        and os.path.exists(renamed_dex)
    ):
        input_alias = "{chain_renamed_dex}"
    command_parts = [
        "python",
        "{jadx_toolchain}",
        "export-classes",
        "-i",
        input_alias,
        "-o",
        resolved_output_alias,
    ]
    for class_name in unique_keep_order(target_classes, limit=6):
        command_parts.extend(["-c", f"\"{class_name}\""])
    command_parts.extend([
        "--record",
        "--with-snippet",
        "--max-lines",
        "80",
        "--limit-per-class",
        "2",
    ])
    return " ".join(command_parts)


def command_requests_wide_jadx_export(tool: str, command_text: str) -> bool:
    lowered = f" {str(command_text or '').lower()} "
    if not lowered.strip():
        return False
    if any(token in lowered for token in (" export-classes ", " find-class ", " show-hit ", " record-hit ", " doctor ")):
        return False

    if tool in {"jadx_toolchain", "jadx"}:
        if " decompile " in lowered:
            return True
        if tool == "jadx":
            return True
        return any(token in lowered for token in (
            " jadx.bat ",
            " jadx.exe ",
            " -d ",
            " --output-dir ",
            "--output-dir=",
        ))

    if tool == "smali_converter":
        if " -to java " not in lowered and " --target java " not in lowered and "--target=java" not in lowered:
            return False
        input_value = extract_first_flag_value(command_text, "-i", "--input").lower()
        return (
            "{target_dex}" in command_text
            or input_value.endswith((".dex", ".apk", ".jar", ".aar"))
            or ".dex " in lowered
            or ".apk " in lowered
            or ".jar " in lowered
        )

    return False


def normalize_tool_specific_command(item: Dict[str, Any],
                                    config: Optional[Dict[str, Any]] = None,
                                    call_chain_text: str = "") -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    normalized_item = dict(item)
    tool = str(normalized_item.get("tool", "") or "").strip().lower()
    command_text = decode_unicode_escape_sequences(str(normalized_item.get("command", "") or ""))
    normalized_item["command"] = command_text
    if tool not in {"jadx_toolchain", "jadx"}:
        return normalized_item

    lowered = command_text.lower()
    valid_subcommand = any(token in lowered for token in (" find-class", " decompile", " doctor", " show-hit", " record-hit"))
    unsupported_jadx_shape = any(token in lowered for token in (
        "--target_class", "--target_method",
        " -class ", " -method ", " -dex ", " -out ", " -index ", " -local_chain ",
        "--class ", "--method ", "--dex ", "--out ", "--index ", "--local_chain ",
        "--chain_work_dir", "--chain_jadx_dir", " chain_work_dir", " chain_jadx_dir",
    ))
    if valid_subcommand and not unsupported_jadx_shape:
        return normalized_item

    target_class = (
        extract_first_flag_value(command_text, "--target_class", "--class", "-class", "-c")
        or str(normalized_item.get("hit_class", "") or "")
    )
    if not target_class:
        class_targets = extract_class_targets(call_chain_text, limit=1)
        target_class = class_targets[0] if class_targets else ""
    target_class = normalize_class_key(target_class)
    if not target_class:
        return normalized_item

    chain_runtime = build_call_chain_runtime_context(call_chain_text, config=config)
    output_alias = "{chain_jadx_dir}" if chain_runtime.get("chain_jadx_dir") else "{jadx_out}"
    command_parts = [
        "python",
        "{jadx_toolchain}",
        "find-class",
        "-i",
        "{target_dex}",
        "-c",
        f"\"{target_class}\"",
        "-o",
        output_alias,
        "--record",
        "--with-snippet",
        "--max-lines",
        "80",
        "--limit",
        "3",
    ]
    target_method = extract_first_flag_value(command_text, "--target_method", "--method", "-method", "-m")
    if target_method:
        normalized_item["next_query"] = first_nonempty(
            str(normalized_item.get("next_query", "") or ""),
            f"继续检查 {target_class} 中与 {target_method} 相关的实现和语义背景",
        )
    normalized_item["command"] = " ".join(command_parts)
    return normalized_item


def normalize_tool_specific_command(item: Dict[str, Any],
                                    config: Optional[Dict[str, Any]] = None,
                                    call_chain_text: str = "") -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {}

    normalized_item = dict(item)
    tool = str(normalized_item.get("tool", "") or "").strip().lower()
    command_text = normalize_archive_sources_alias_flags(str(normalized_item.get("command", "") or ""))
    normalized_item["command"] = command_text
    if command_requests_smali_apply_renames(tool, command_text):
        rewritten_command = build_smali_apply_renames_command(
            command_text,
            config=config,
            call_chain_text=call_chain_text,
        )
        if rewritten_command:
            normalized_item["tool"] = "smali_converter"
            normalized_item["command"] = rewritten_command
            tool = "smali_converter"
            command_text = rewritten_command

    if tool not in {"jadx_toolchain", "jadx", "smali_converter", "baksmali"}:
        return normalized_item

    lowered_command = command_text.lower()
    focus_classes = collect_focus_class_targets(
        command_text,
        item=normalized_item,
        call_chain_text=call_chain_text,
        limit=4,
    )
    chain_runtime = build_call_chain_runtime_context(call_chain_text, config=config)
    renamed_class_targets = collect_renamed_class_targets_from_item(normalized_item, limit=4)
    prefers_renamed_dex = bool(
        str(chain_runtime.get("chain_renamed_dex", "") or "")
        and os.path.exists(str(chain_runtime.get("chain_renamed_dex", "") or ""))
        and (
            "{chain_renamed_dex}" in command_text
            or "chain_renamed_dex" in command_text
            or command_targets_named_export_output(command_text)
            or " rerun-named " in f" {lowered_command} "
        )
    )
    if prefers_renamed_dex and renamed_class_targets:
        focus_classes = renamed_class_targets
    if focus_classes and not normalize_class_key(str(normalized_item.get("hit_class", "") or "")):
        normalized_item["hit_class"] = focus_classes[0]

    lowered = lowered_command
    legacy_jadx_shape = any(token in lowered for token in (
        "--target_class", "--target_method",
        " -class ", " -method ", " -dex ", " -out ", " -index ", " -local_chain ",
        "--class ", "--method ", "--dex ", "--out ", "--index ", "--local_chain ",
        "--chain_work_dir", "--chain_jadx_dir", "--chain_jadx_named_dir", "--chain_jadx_analysis_dir",
        " chain_work_dir", " chain_jadx_dir", " chain_jadx_named_dir", " chain_jadx_analysis_dir",
        " rerun-named ", " --analysis-dir ",
    ))

    should_rewrite = False
    if tool in {"jadx_toolchain", "jadx"}:
        if any(token in lowered for token in (" show-hit ", " record-hit ", " doctor ")):
            return normalized_item
        should_rewrite = legacy_jadx_shape or command_requests_wide_jadx_export(tool, command_text)
    elif tool in {"smali_converter", "baksmali"}:
        should_rewrite = command_requests_wide_jadx_export(tool, command_text)

    if not should_rewrite or not focus_classes:
        return normalized_item

    normalized_item["tool"] = "jadx_toolchain"
    normalized_item["command"] = build_narrow_jadx_export_command(
        focus_classes,
        config=config,
        call_chain_text=call_chain_text,
        output_alias="{chain_jadx_named_dir}" if command_targets_named_export_output(command_text) else "",
    )

    target_method = extract_first_flag_value(command_text, "--target_method", "--method", "-method", "-m")
    if target_method:
        normalized_item["next_query"] = first_nonempty(
            str(normalized_item.get("next_query", "") or ""),
            f"继续检查 {focus_classes[0]} 中与 {target_method} 相关的实现和语义背景",
        )
    return normalized_item


def canonicalize_command_text_to_aliases(command_text: str,
                                         config: Optional[Dict[str, Any]] = None,
                                         tools: Optional[Dict[str, str]] = None,
                                         call_chain_text: str = "") -> str:
    text = normalize_archive_sources_alias_flags(str(command_text or ""))
    if not text:
        return ""
    alias_map = build_runtime_alias_map(
        config or load_tool_dispatcher_config(),
        tools=tools or discover_tool_context(config),
        call_chain_text=call_chain_text,
    )
    replacements: List[Tuple[str, str]] = []
    for alias_name, alias_value in alias_map.items():
        normalized_value = normalize_runtime_root(alias_value)
        if not normalized_value:
            continue
        replacements.append((normalized_value, "{" + alias_name + "}"))
        replacements.append((str(alias_value), "{" + alias_name + "}"))
    seen_values: set[str] = set()
    ordered: List[Tuple[str, str]] = []
    for source_value, alias_token in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if not source_value or source_value in seen_values:
            continue
        seen_values.add(source_value)
        ordered.append((source_value, alias_token))
    canonical = text
    for source_value, alias_token in ordered:
        canonical = canonical.replace(source_value, alias_token)
    return canonical


def canonicalize_command_items(command_items: List[Dict[str, Any]],
                               config: Optional[Dict[str, Any]] = None,
                               tools: Optional[Dict[str, str]] = None,
                               call_chain_text: str = "") -> List[Dict[str, Any]]:
    canonical_items: List[Dict[str, Any]] = []
    for item in command_items:
        if not isinstance(item, dict):
            continue
        normalized_item = normalize_tool_specific_command(item, config=config, call_chain_text=call_chain_text) or dict(item)
        normalized_item["command"] = canonicalize_command_text_to_aliases(
            str(normalized_item.get("command", "") or ""),
            config=config,
            tools=tools,
            call_chain_text=call_chain_text,
        )
        if normalized_item.get("command") != str(item.get("command", "") or ""):
            normalized_item["command"] = canonicalize_command_text_to_aliases(
                str(normalized_item.get("command", "") or ""),
                config=config,
                tools=tools,
                call_chain_text=call_chain_text,
            )
        canonical_items.append(normalized_item)
    return canonical_items


def build_loop_command_signature(item: Dict[str, Any],
                                 config: Optional[Dict[str, Any]] = None,
                                 tools: Optional[Dict[str, str]] = None,
                                 call_chain_text: str = "") -> str:
    if not isinstance(item, dict):
        return ""
    normalized_item = normalize_tool_specific_command(
        dict(item),
        config=config,
        call_chain_text=call_chain_text,
    ) or dict(item)
    command_text = canonicalize_command_text_to_aliases(
        str(normalized_item.get("command", "") or ""),
        config=config,
        tools=tools,
        call_chain_text=call_chain_text,
    )
    core = {
        "tool": str(normalized_item.get("tool", "") or item.get("tool", "") or "").strip().lower(),
        "command": command_text.strip(),
        "hit_class": normalize_class_key(str(normalized_item.get("hit_class", "") or item.get("hit_class", "") or "")),
    }
    try:
        return json.dumps(core, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return repr(core)


def filter_loop_duplicate_commands(command_items: List[Dict[str, Any]],
                                   command_routes: List[Dict[str, Any]],
                                   completed_signatures: Optional[set[str]] = None,
                                   batch_signatures: Optional[set[str]] = None,
                                   config: Optional[Dict[str, Any]] = None,
                                   tools: Optional[Dict[str, str]] = None,
                                   call_chain_text: str = "") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    kept_items: List[Dict[str, Any]] = []
    kept_routes: List[Dict[str, Any]] = []
    skipped_items: List[Dict[str, Any]] = []
    completed_signatures = completed_signatures if isinstance(completed_signatures, set) else set()
    batch_signatures = batch_signatures if isinstance(batch_signatures, set) else set()

    for index, item in enumerate(command_items):
        if not isinstance(item, dict):
            continue
        signature = build_loop_command_signature(
            item,
            config=config,
            tools=tools,
            call_chain_text=call_chain_text,
        )
        if signature and (signature in completed_signatures or signature in batch_signatures):
            skipped_items.append({
                "tool": str(item.get("tool", "") or ""),
                "command": str(item.get("command", "") or ""),
                "hit_class": str(item.get("hit_class", "") or ""),
                "signature": signature,
            })
            continue
        if signature:
            batch_signatures.add(signature)
        kept_items.append(item)
        if index < len(command_routes) and isinstance(command_routes[index], dict):
            kept_routes.append(command_routes[index])

    return kept_items, kept_routes, skipped_items


def normalize_command_json(reply_text: str,
                           config: Optional[Dict[str, Any]] = None,
                           tools: Optional[Dict[str, str]] = None,
                           call_chain_text: str = "") -> Optional[str]:
    command_items = canonicalize_command_items(
        collect_command_items_from_text(reply_text),
        config=config,
        tools=tools,
        call_chain_text=call_chain_text,
    )
    if not command_items:
        return None
    if len(command_items) == 1:
        normalized_payload: Any = command_items[0]
    else:
        normalized_payload = command_items
    return json.dumps(normalized_payload, ensure_ascii=False, indent=2)


def dispatch_command_json(command_json: str,
                          output_path: str = "",
                          stop_on_error: bool = False) -> Tuple[bool, str, Dict[str, Any]]:
    dispatcher_path = resolve_helper_script_path("tool_dispatcher.py")
    if not command_json:
        return False, "未提取到命令JSON", {}
    if not os.path.exists(dispatcher_path):
        return False, f"未找到工具执行器: {dispatcher_path}", {}

    fd, temp_json_path = tempfile.mkstemp(prefix="cli_chat_cmd_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(command_json)
            handle.write("\n")

        command = [sys.executable or "python", dispatcher_path, "--json-file", temp_json_path, "--pretty"]
        if output_path:
            command.extend(["--output", output_path])
        if stop_on_error:
            command.append("--stop-on-error")

        try:
            result = subprocess.run(
                command,
                cwd=APP_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except Exception as exc:
            return False, str(exc), {}

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        payload_text = stdout or stderr
        payload_data: Dict[str, Any] = {}
        if stdout:
            try:
                parsed = json.loads(stdout)
                if isinstance(parsed, dict):
                    payload_data = parsed
            except json.JSONDecodeError:
                payload_data = {}
        return result.returncode == 0, payload_text, payload_data
    finally:
        try:
            os.remove(temp_json_path)
        except OSError:
            pass


def build_command_review_result(entry: Dict[str, Any],
                                entry_index: int,
                                reason: str,
                                detail: str,
                                policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    command = str(entry.get("command", "") or "")
    stderr_text = detail or reason
    return {
        "tool": str(entry.get("tool", "") or ""),
        "profile": str(entry.get("profile", "") or ""),
        "reason": str(entry.get("reason", "") or ""),
        "hit_class": str(entry.get("hit_class", "") or ""),
        "hit_store": str(entry.get("hit_store", "") or ""),
        "next_query": str(entry.get("next_query", "") or ""),
        "store_back": str(entry.get("store_back", "") or ""),
        "command": command,
        "entry_index": entry_index,
        "started_at": time.time(),
        "finished_at": time.time(),
        "duration_sec": 0.0,
        "ok": False,
        "returncode": 1,
        "judge_status": 1,
        "stdout": "",
        "stderr": stderr_text,
        "stdout_preview": "",
        "stderr_preview": shorten_text(stderr_text, 300),
        "has_output": True,
        "policy": policy or {
            "blocked": True,
            "reason": reason,
        },
        "review_stage": "pre_exec_gate",
    }


def review_command_items_for_runtime(command_items: List[Dict[str, Any]],
                                     config: Dict[str, Any],
                                     tools: Optional[Dict[str, str]] = None,
                                     call_chain_text: str = "") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    tools = tools or discover_tool_context(config)
    alias_map = build_runtime_alias_map(config, tools=tools, call_chain_text=call_chain_text)
    roots = runtime_review_roots(config)
    strict_roots_only = parse_bool_like(config.get("review_initialized_roots_only", True), True)
    approved_items: List[Dict[str, Any]] = []
    rejected_results: List[Dict[str, Any]] = []
    review_items: List[Dict[str, Any]] = []

    for index, item in enumerate(command_items):
        if not isinstance(item, dict):
            continue
        normalized_item = dict(item)
        normalized_item["entry_index"] = int(item.get("entry_index", index) or index)
        original_command = str(normalized_item.get("command", "") or "")
        expanded_command, missing_aliases = expand_runtime_aliases(original_command, alias_map)
        normalized_item["command"] = expanded_command
        command_paths = unique_keep_order(WINDOWS_PATH_PATTERN.findall(expanded_command or ""), limit=24)
        outside_paths = [path for path in command_paths if strict_roots_only and not path_within_runtime_roots(path, roots)]
        review_item = {
            "entry_index": normalized_item["entry_index"],
            "tool": str(normalized_item.get("tool", "") or ""),
            "missing_aliases": missing_aliases,
            "outside_paths": outside_paths,
            "paths": command_paths,
            "approved": not bool(missing_aliases or outside_paths),
        }
        review_items.append(review_item)

        if missing_aliases or outside_paths:
            detail_parts = ["本地审查拒绝：后续命令只能使用已初始化目录或运行时别名。"]
            if missing_aliases:
                detail_parts.append("缺失别名=" + " | ".join(missing_aliases[:6]))
            if outside_paths:
                detail_parts.append("越界路径=" + " | ".join(outside_paths[:6]))
            detail_parts.append("允许别名={target_dex|smali_dir|jadx_out|call_chain_output_root|call_chain_index_root|baksmali|smali|jadx|jadx_toolchain|smali_converter|app_dir|workspace_root|chain_work_dir|chain_output_dir|chain_smali_dir|chain_renamed_dex|chain_jadx_dir|chain_jadx_named_dir|chain_jadx_analysis_dir|chain_index_file}")
            rejected_results.append(
                build_command_review_result(
                    normalized_item,
                    entry_index=normalized_item["entry_index"],
                    reason="runtime_review_failed",
                    detail=" ; ".join(detail_parts),
                    policy={
                        "blocked": True,
                        "reason": "runtime_review_failed",
                        "missing_aliases": missing_aliases,
                        "outside_paths": outside_paths,
                        "allowed_root_count": len(roots),
                    },
                )
            )
            continue

        approved_items.append(normalized_item)

    summary = {
        "review_stage": "pre_exec_gate",
        "approved_count": len(approved_items),
        "rejected_count": len(rejected_results),
        "strict_initialized_roots_only": strict_roots_only,
        "allowed_root_count": len(roots),
        "runtime_aliases": [
            name for name in [
                "target_dex", "smali_dir", "jadx_out", "call_chain_output_root", "call_chain_index_root",
                "baksmali", "smali", "jadx", "jadx_toolchain", "smali_converter",
                "tool_dispatcher", "workspace_root", "app_dir", "chain_work_dir", "chain_output_dir",
                "chain_smali_dir", "chain_renamed_dex", "chain_jadx_dir", "chain_jadx_named_dir", "chain_jadx_analysis_dir", "chain_index_file"
            ] if alias_map.get(name)
        ],
        "items": review_items,
    }
    return approved_items, rejected_results, summary


def review_command_items_for_runtime(command_items: List[Dict[str, Any]],
                                     config: Dict[str, Any],
                                     tools: Optional[Dict[str, str]] = None,
                                     call_chain_text: str = "") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    tools = tools or discover_tool_context(config)
    alias_map = build_runtime_alias_map(config, tools=tools, call_chain_text=call_chain_text)
    roots = runtime_review_roots(config)
    strict_roots_only = parse_bool_like(config.get("review_initialized_roots_only", True), True)
    approved_items: List[Dict[str, Any]] = []
    rejected_results: List[Dict[str, Any]] = []
    review_items: List[Dict[str, Any]] = []

    for index, item in enumerate(command_items):
        if not isinstance(item, dict):
            continue
        normalized_item = dict(item)
        normalized_item["entry_index"] = int(item.get("entry_index", index) or index)
        original_command = str(normalized_item.get("command", "") or "")
        expanded_command, missing_aliases = expand_runtime_aliases(original_command, alias_map)
        normalized_item["command"] = expanded_command
        command_paths = unique_keep_order(WINDOWS_PATH_PATTERN.findall(expanded_command or ""), limit=24)
        outside_paths = [path for path in command_paths if strict_roots_only and not path_within_runtime_roots(path, roots)]

        focus_classes = collect_focus_class_targets(
            original_command,
            item=normalized_item,
            call_chain_text=call_chain_text,
            limit=4,
        )
        chain_scoped = bool(
            call_chain_text.strip()
            or normalize_class_key(str(normalized_item.get("hit_class", "") or ""))
            or focus_classes
        )
        wide_jadx_export = chain_scoped and command_requests_wide_jadx_export(
            str(normalized_item.get("tool", "") or "").strip().lower(),
            expanded_command,
        )

        review_item = {
            "entry_index": normalized_item["entry_index"],
            "tool": str(normalized_item.get("tool", "") or ""),
            "missing_aliases": missing_aliases,
            "outside_paths": outside_paths,
            "paths": command_paths,
            "focus_classes": focus_classes,
            "wide_jadx_export": wide_jadx_export,
            "approved": not bool(missing_aliases or outside_paths or wide_jadx_export),
        }
        review_items.append(review_item)

        if wide_jadx_export:
            detail_parts = [
                "本地审查拒绝：调用链场景下禁止整包或整 dex 的 JADX Java 导出，只能导出当前选中的调用链类。"
            ]
            if focus_classes:
                detail_parts.append("聚焦类=" + " | ".join(focus_classes[:4]))
                detail_parts.append(
                    "建议命令="
                    + build_narrow_jadx_export_command(
                        focus_classes,
                        config=config,
                        call_chain_text=call_chain_text,
                        output_alias="{chain_jadx_named_dir}" if command_targets_named_export_output(original_command or expanded_command) else "",
                    )
                )
            else:
                detail_parts.append("缺少可导出的目标类，请先提供 hit_class、target_class 或有效的调用链类描述。")
            rejected_results.append(
                build_command_review_result(
                    normalized_item,
                    entry_index=normalized_item["entry_index"],
                    reason="wide_jadx_export_blocked",
                    detail=" ; ".join(detail_parts),
                    policy={
                        "blocked": True,
                        "reason": "wide_jadx_export_blocked",
                        "focus_classes": focus_classes,
                    },
                )
            )
            continue

        if missing_aliases or outside_paths:
            detail_parts = ["本地审查拒绝：后续命令只能使用已初始化目录或运行时别名。"]
            if missing_aliases:
                detail_parts.append("缺失别名=" + " | ".join(missing_aliases[:6]))
            if outside_paths:
                detail_parts.append("越界路径=" + " | ".join(outside_paths[:6]))
            detail_parts.append("允许别名={target_dex|smali_dir|jadx_out|call_chain_output_root|call_chain_index_root|baksmali|smali|jadx|jadx_toolchain|smali_converter|app_dir|workspace_root|chain_work_dir|chain_output_dir|chain_smali_dir|chain_renamed_dex|chain_jadx_dir|chain_index_file}")
            rejected_results.append(
                build_command_review_result(
                    normalized_item,
                    entry_index=normalized_item["entry_index"],
                    reason="runtime_review_failed",
                    detail=" ; ".join(detail_parts),
                    policy={
                        "blocked": True,
                        "reason": "runtime_review_failed",
                        "missing_aliases": missing_aliases,
                        "outside_paths": outside_paths,
                        "allowed_root_count": len(roots),
                    },
                )
            )
            continue

        approved_items.append(normalized_item)

    summary = {
        "review_stage": "pre_exec_gate",
        "approved_count": len(approved_items),
        "rejected_count": len(rejected_results),
        "strict_initialized_roots_only": strict_roots_only,
        "allowed_root_count": len(roots),
        "runtime_aliases": [
            name for name in [
                "target_dex", "smali_dir", "jadx_out", "call_chain_output_root", "call_chain_index_root",
                "baksmali", "smali", "jadx", "jadx_toolchain", "smali_converter",
                "tool_dispatcher", "workspace_root", "app_dir", "chain_work_dir", "chain_output_dir",
                "chain_smali_dir", "chain_renamed_dex", "chain_jadx_dir", "chain_jadx_named_dir", "chain_jadx_analysis_dir", "chain_index_file"
            ] if alias_map.get(name)
        ],
        "items": review_items,
    }
    return approved_items, rejected_results, summary


def merge_dispatch_payload_with_review(entries: List[Dict[str, Any]],
                                       dispatch_payload: Dict[str, Any],
                                       rejected_results: List[Dict[str, Any]],
                                       review_summary: Dict[str, Any],
                                       config: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(dispatch_payload or {})
    existing_results = list(payload.get("results", [])) if isinstance(payload.get("results", []), list) else []
    merged_results = list(existing_results) + list(rejected_results)
    merged_results.sort(key=lambda item: int(item.get("entry_index", 0) or 0))
    payload["entries"] = entries
    payload["results"] = merged_results
    payload["count"] = len(merged_results)
    payload["ok"] = bool(merged_results) and all(bool(item.get("ok")) for item in merged_results)
    payload["config"] = config
    payload["review"] = review_summary
    return payload


def run_reviewed_dispatch(command_items: List[Dict[str, Any]],
                          config: Dict[str, Any],
                          tools: Optional[Dict[str, str]] = None,
                          call_chain_text: str = "",
                          output_path: str = "",
                          stop_on_error: bool = False) -> Tuple[bool, str, Dict[str, Any], List[Dict[str, Any]]]:
    approved_items, rejected_results, review_summary = review_command_items_for_runtime(
        command_items,
        config,
        tools=tools,
        call_chain_text=call_chain_text,
    )
    dispatch_ok = False
    dispatch_payload: Dict[str, Any] = {}
    dispatch_text = ""

    if approved_items:
        approved_payload = json.dumps(approved_items if len(approved_items) != 1 else approved_items[0], ensure_ascii=False, indent=2)
        dispatch_ok, dispatch_text, dispatch_payload = dispatch_command_json(
            approved_payload,
            output_path=output_path,
            stop_on_error=stop_on_error,
        )
    merged_payload = merge_dispatch_payload_with_review(
        entries=command_items,
        dispatch_payload=dispatch_payload,
        rejected_results=rejected_results,
        review_summary=review_summary,
        config=config,
    )
    merged_ok = bool(merged_payload.get("ok")) and not rejected_results
    merged_text = json.dumps(merged_payload, ensure_ascii=False, indent=2)
    return merged_ok and dispatch_ok if approved_items else False, merged_text, merged_payload, approved_items


def format_dispatch_summary(payload: Dict[str, Any]) -> str:
    if not payload:
        return ""
    lines = [
        f"tool_dispatcher ok={payload.get('ok', False)} count={payload.get('count', 0)}",
    ]
    for item in list(payload.get("results", []))[:4]:
        tool = shorten_text(str(item.get("tool", "")), 32)
        command = shorten_text(str(item.get("command", "")), 96)
        status = "ok" if item.get("ok") else "fail"
        lines.append(f"{status} | {tool} | {command}")
        preview = shorten_text(str(item.get("stderr_preview") or item.get("stdout_preview") or ""), 140)
        if preview:
            lines.append(f"  {preview}")
    return "\n".join(lines)


def decode_jsonish(value: Any, max_depth: int = 3) -> Any:
    current = value
    for _ in range(max_depth):
        if isinstance(current, (dict, list)):
            return current
        if not isinstance(current, str):
            return current
        stripped = current.lstrip("\ufeff").strip()
        if not stripped:
            return ""
        if not ((stripped.startswith("{") and stripped.endswith("}")) or
                (stripped.startswith("[") and stripped.endswith("]")) or
                (stripped.startswith("\"") and stripped.endswith("\""))):
            return current
        try:
            current = json.loads(stripped)
        except json.JSONDecodeError:
            return current
    return current


def extract_transport_shell_payload(value: Any) -> Dict[str, Any]:
    decoded = decode_jsonish(value, max_depth=6)
    if not isinstance(decoded, dict):
        return {}
    allowed_keys = {
        "text", "img_urls", "images",
        "conversation_id", "section_id",
        "message_id", "messageg_id", "reply_id",
        "local_message_id", "local_conversation_id",
    }
    keys = {str(key) for key in decoded.keys()}
    if not keys or not keys.issubset(allowed_keys):
        return {}
    if str(decoded.get("text", "") or "").strip():
        return {}
    return decoded


def is_transport_shell_text(value: Any) -> bool:
    return bool(extract_transport_shell_payload(value))


def sanitize_reply_text_for_runtime(text: Any) -> str:
    source = str(text or "")
    if not source:
        return ""
    stripped_source = source.strip()
    if stripped_source and is_transport_shell_text(stripped_source):
        return ""
    kept_lines: List[str] = []
    for raw_line in source.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if kept_lines and kept_lines[-1] != "":
                kept_lines.append("")
            continue
        if is_transport_shell_text(stripped):
            continue
        if RUNTIME_REPLY_META_LINE_PATTERN.match(stripped):
            continue
        kept_lines.append(raw_line)
    return "\n".join(kept_lines).strip()


def sanitize_analysis_fragment(value: str,
                              limit: int = 160,
                              call_chain_text: str = "",
                              config: Optional[Dict[str, Any]] = None) -> str:
    fragment = extract_primary_fragment(value, limit=max(limit * 2, 320))
    if not fragment:
        return ""
    if is_transport_shell_text(fragment):
        return ""
    fragment = localize_runtime_paths_in_text(fragment, config=config, call_chain_text=call_chain_text)
    return shorten_text(fragment, limit)


def first_nonempty(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def collect_images_from_event(event_data: Dict[str, Any]) -> List[str]:
    images: List[str] = []
    for key in ("img_urls", "images"):
        value = event_data.get(key)
        if isinstance(value, list):
            images.extend(str(item) for item in value if item)
    message = event_data.get("message")
    if isinstance(message, dict):
        value = message.get("img_urls")
        if isinstance(value, list):
            images.extend(str(item) for item in value if item)
    return unique_keep_order(images, limit=20)


def extract_text_value(value: Any) -> str:
    decoded = decode_jsonish(value, max_depth=5)
    if isinstance(decoded, str):
        return decoded
    if isinstance(decoded, list):
        parts: List[str] = []
        for item in decoded:
            piece = extract_text_value(item)
            if piece:
                parts.append(piece)
        return "".join(parts)
    if not isinstance(decoded, dict):
        return ""

    for key in ("tts_content", "text", "answer", "content", "delta", "output_text", "output"):
        direct = decoded.get(key)
        if direct:
            direct_text = extract_text_value(direct) if not isinstance(direct, str) else extract_text_value(direct)
            if direct_text:
                return direct_text
    if "message" in decoded:
        return extract_message_text(decoded.get("message"))
    if "data" in decoded:
        return extract_text_value(decoded.get("data"))
    return ""


def extract_message_text(message: Any) -> str:
    if not message:
        return ""
    decoded = decode_jsonish(message, max_depth=5)
    if isinstance(decoded, str):
        return decoded
    if not isinstance(decoded, dict):
        return ""

    for key in ("tts_content", "text", "answer", "content", "delta", "output_text", "output"):
        value = decoded.get(key)
        if value:
            text_value = extract_text_value(value)
            if text_value:
                return text_value
    return ""


def extract_snapshot_text_from_event(event_data: Dict[str, Any]) -> str:
    nested_event = decode_jsonish(event_data.get("event_data", ""), max_depth=5) if isinstance(event_data, dict) else {}
    if isinstance(nested_event, dict) and nested_event is not event_data:
        nested_text = extract_snapshot_text_from_event(nested_event)
        if nested_text:
            return nested_text

    snapshot_keys = ("tts_content", "answer", "output_text", "text")
    for key in snapshot_keys:
        value = event_data.get(key)
        if value:
            text_value = extract_text_value(value)
            if text_value:
                return text_value

    message = event_data.get("message")
    if message:
        text_value = extract_message_text(message)
        if text_value:
            return text_value
    return ""


def extract_text_piece_from_event(event_data: Dict[str, Any], assembled_text: str) -> Tuple[str, bool]:
    snapshot_text = extract_snapshot_text_from_event(event_data)
    if snapshot_text:
        if snapshot_text.startswith(assembled_text):
            return snapshot_text[len(assembled_text):], True
        return snapshot_text, True

    for key in ("text", "content", "delta", "output"):
        value = event_data.get(key)
        if value:
            text_value = extract_text_value(value)
            if text_value.startswith(assembled_text) and key in ("text", "content", "output"):
                return text_value[len(assembled_text):], True
            return text_value, key in ("text", "content", "output")
    return "", False


def iter_text_candidates(payload: Any,
                         seen: Optional[Dict[str, bool]] = None,
                         depth: int = 0) -> List[str]:
    if seen is None:
        seen = {}
    if depth > 8 or payload is None:
        return []

    results: List[str] = []
    marker = f"{type(payload).__name__}:{repr(payload)[:400]}"
    if marker in seen:
        return []
    seen[marker] = True

    if isinstance(payload, str):
        text = payload.strip()
        if text:
            results.append(text)
        decoded = decode_jsonish(text, max_depth=4)
        if decoded is not payload:
            results.extend(iter_text_candidates(decoded, seen=seen, depth=depth + 1))
        match = CODE_BLOCK_PATTERN.search(text)
        if match:
            results.append(match.group(0).strip())
        return results

    if isinstance(payload, list):
        for item in payload:
            results.extend(iter_text_candidates(item, seen=seen, depth=depth + 1))
        return results

    if not isinstance(payload, dict):
        return results

    for key in ("tts_content", "answer", "output_text", "text", "content", "delta", "output"):
        value = payload.get(key)
        if value:
            text_value = extract_text_value(value)
            if text_value:
                results.append(text_value)
            results.extend(iter_text_candidates(value, seen=seen, depth=depth + 1))

    for key in ("event_data", "message", "conversation"):
        value = payload.get(key)
        if value:
            results.extend(iter_text_candidates(value, seen=seen, depth=depth + 1))

    return results


def recover_text_from_raw_events(raw_events: List[str]) -> Tuple[str, str]:
    candidates: List[str] = []
    for raw_event in raw_events:
        if not raw_event:
            continue
        candidates.extend(iter_text_candidates(raw_event))
        decoded_outer = decode_jsonish(raw_event, max_depth=6)
        if decoded_outer is not raw_event:
            candidates.extend(iter_text_candidates(decoded_outer))

    best_text = ""
    best_source = ""
    for candidate in candidates:
        text = (candidate or "").strip()
        if not text:
            continue
        source = "plain_text"
        if "```json" in text.lower():
            source = "raw_event_code_block"
        elif "\"tool\"" in text or "\"command\"" in text:
            source = "raw_event_json"
        elif "[ecsa]" in text.lower() or "[oyut]" in text.lower():
            source = "raw_event_protocol"
        score = (
            1000 if source == "raw_event_code_block" else
            800 if source == "raw_event_json" else
            600 if source == "raw_event_protocol" else
            100
        ) + len(text)
        best_score = (
            1000 if best_source == "raw_event_code_block" else
            800 if best_source == "raw_event_json" else
            600 if best_source == "raw_event_protocol" else
            100
        ) + len(best_text)
        if score > best_score:
            best_text = text
            best_source = source
    return best_text, best_source


def build_reply_meta(reply_text: str,
                     conversation_id: Optional[str],
                     section_id: Optional[str],
                     profile: str = "",
                     profile_reason: str = "",
                     images: Optional[List[str]] = None,
                     call_chain_text: str = "",
                     route_entry: Optional[Dict[str, Any]] = None,
                     stream_meta: Optional[Dict[str, Any]] = None,
                     local_mode: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    stream_meta = stream_meta if isinstance(stream_meta, dict) else {}
    local_mode = local_mode if isinstance(local_mode, dict) else {}
    effective_reply_text = str(reply_text or "")
    reply_source = "reply_text" if effective_reply_text else ""
    if not effective_reply_text:
        recovered_text = str(stream_meta.get("recovered_text", "") or "")
        if recovered_text:
            effective_reply_text = recovered_text
            reply_source = str(stream_meta.get("reply_source", "") or "stream_recovered")
    toolchain_config = load_tool_dispatcher_config()
    tools = discover_tool_context(toolchain_config)
    command_json = normalize_command_json(
        effective_reply_text or "",
        config=toolchain_config,
        tools=tools,
        call_chain_text=str(call_chain_text or ""),
    ) or ""
    command_items: List[Dict[str, Any]] = canonicalize_command_items(
        collect_command_items_from_text(effective_reply_text or ""),
        config=toolchain_config,
        tools=tools,
        call_chain_text=str(call_chain_text or ""),
    )
    payload = {
        "profile": profile or "",
        "profile_reason": profile_reason or "",
        "reply_text": effective_reply_text,
        "reply_source": reply_source,
    }
    if call_chain_text:
        payload["call_chain_text"] = call_chain_text
    if images:
        payload["images"] = images
    if command_items:
        payload["command_json"] = command_json
        payload["command_count"] = len(command_items)
        payload["command_tools"] = unique_keep_order(
            [str(item.get("tool", "") or "") for item in command_items if item.get("tool")],
            limit=12,
        )
        payload["command_profiles"] = unique_keep_order(
            [str(item.get("profile", "") or "") for item in command_items if item.get("profile")],
            limit=12,
        )
        payload["command_items"] = command_items
    return payload


def safe_console_text(text: Any) -> str:
    value = str(text or "")
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return value.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except Exception:
        return value


def print_json_block(payload: Any) -> None:
    print("\n```json")
    print(safe_console_text(json.dumps(payload, ensure_ascii=False, indent=2)))
    print("```")


def load_resume_payload(raw_text: str) -> Dict[str, Any]:
    text = (raw_text or "").lstrip("﻿").strip()
    if not text:
        return {}

    def attach_local_ids(payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        route_entry = payload.get("route_entry", {}) if isinstance(payload.get("route_entry"), dict) else {}
        group_ref = str(payload.get("thread_group_ref", "") or route_entry.get("group_ref", "") or "")
        slot_ref = str(payload.get("thread_slot_ref", "") or route_entry.get("slot_ref", "") or "")
        resolved = resolve_thread_slot_by_refs(group_ref, slot_ref)
        if resolved:
            if not payload.get("conversation_id"):
                payload["conversation_id"] = str(resolved.get("conversation_id", "") or "")
            if not payload.get("section_id"):
                payload["section_id"] = str(resolved.get("section_id", "") or "")
            if not payload.get("call_chain_text") and resolved.get("chain_text"):
                payload["call_chain_text"] = str(resolved.get("chain_text", "") or "")
        return payload

    payload = decode_jsonish(text, max_depth=5)
    if isinstance(payload, dict):
        return attach_local_ids(payload)
    match = CODE_BLOCK_PATTERN.search(text)
    if match:
        payload = decode_jsonish(match.group(1).strip(), max_depth=5)
        if isinstance(payload, dict):
            return attach_local_ids(payload)
    return {}

def tool_dispatcher_config_path() -> str:
    return os.path.join(APP_DIR, "tool_dispatcher_config.json")


def load_tool_dispatcher_config() -> Dict[str, Any]:
    defaults = {
        "workspace_root": "",
        "toolchain_separate_conversations": DEFAULT_TOOLCHAIN_SEPARATE_CONVERSATIONS,
        "role_prompt_refresh_turns": DEFAULT_ROLE_PROMPT_REFRESH_TURNS,
        "auto_followup_after_exec": DEFAULT_AUTO_FOLLOWUP_AFTER_EXEC,
        "auto_followup_max_rounds": 8,
        "jadx_force_new_meeting": DEFAULT_JADX_FORCE_NEW_MEETING,
        "mirror_jadx_to_rename": DEFAULT_MIRROR_JADX_TO_RENAME,
        "strict_tuning_message_scope": DEFAULT_STRICT_TUNING_MESSAGE_SCOPE,
        "naming_dedup_mode": DEFAULT_NAMING_DEDUP_MODE,
        "rename_apply_mode": DEFAULT_RENAME_APPLY_MODE,
        "review_initialized_roots_only": True,
        "call_chain_output_root": DEFAULT_CHAIN_OUTPUT_ROOT,
        "call_chain_index_root": DEFAULT_CHAIN_INDEX_ROOT,
    }
    path = tool_dispatcher_config_path()
    if not os.path.exists(path):
        return localize_tool_dispatcher_config(dict(defaults))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            if not isinstance(data, dict):
                return localize_tool_dispatcher_config(dict(defaults))
            for key, value in defaults.items():
                data.setdefault(key, value)
            return localize_tool_dispatcher_config(data)
    except Exception:
        return localize_tool_dispatcher_config(dict(defaults))


def parse_bool_like(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def normalize_runtime_root(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.abspath(path).rstrip("\\/")
    except Exception:
        return str(path).rstrip("\\/")


def runtime_review_roots(config: Optional[Dict[str, Any]] = None) -> List[str]:
    config = config if isinstance(config, dict) else {}
    roots: List[str] = []
    for key in ("smali_dir", "jadx_out", "call_chain_output_root", "call_chain_index_root"):
        value = str(config.get(key, "") or "")
        if value:
            roots.append(value)
    target_dex = str(config.get("target_dex", "") or "")
    if target_dex:
        roots.append(os.path.dirname(target_dex))
    roots.extend(list(config.get("allowed_roots", [])) if isinstance(config.get("allowed_roots", []), list) else [])
    return unique_keep_order([normalize_runtime_root(path) for path in roots if normalize_runtime_root(path)], limit=24)


def path_within_runtime_roots(path: str, roots: List[str]) -> bool:
    normalized_path = normalize_runtime_root(path)
    lowered_path = normalized_path.lower()
    for root in roots:
        normalized_root = normalize_runtime_root(root)
        if not normalized_root:
            continue
        lowered_root = normalized_root.lower()
        if lowered_path == lowered_root or lowered_path.startswith(lowered_root + "\\") or lowered_path.startswith(lowered_root + "/"):
            return True
    return False


def build_runtime_alias_pairs(config: Optional[Dict[str, Any]] = None,
                              call_chain_text: str = "") -> List[Tuple[str, str]]:
    localized_config = localize_tool_dispatcher_config(config if isinstance(config, dict) else load_tool_dispatcher_config())
    alias_map = build_runtime_alias_map(
        localized_config,
        tools=discover_tool_context(localized_config),
        call_chain_text=call_chain_text,
    )
    pairs: List[Tuple[str, str]] = []
    for alias_name in (
        "chain_index_file",
        "chain_index_dir",
        "chain_jadx_analysis_dir",
        "chain_jadx_named_dir",
        "chain_jadx_dir",
        "chain_smali_dir",
        "chain_output_dir",
        "chain_work_dir",
        "call_chain_index_root",
        "call_chain_output_root",
        "jadx_out",
        "smali_dir",
        "tool_dispatcher",
        "jadx_toolchain",
        "workspace_root",
        "app_dir",
    ):
        alias_value = normalize_runtime_root(str(alias_map.get(alias_name, "") or ""))
        if alias_value:
            pairs.append((alias_value, "{" + alias_name + "}"))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


def localize_runtime_path_value(path: str,
                                alias_pairs: Optional[List[Tuple[str, str]]] = None,
                                config: Optional[Dict[str, Any]] = None,
                                call_chain_text: str = "") -> str:
    candidate = str(path or "")
    normalized = normalize_runtime_root(candidate)
    if not normalized:
        return candidate
    alias_pairs = alias_pairs or build_runtime_alias_pairs(config=config, call_chain_text=call_chain_text)
    lowered_candidate = normalized.lower()
    for root, alias_name in alias_pairs:
        lowered_root = root.lower()
        if lowered_candidate == lowered_root:
            return alias_name
        if lowered_candidate.startswith(lowered_root + "\\") or lowered_candidate.startswith(lowered_root + "/"):
            return alias_name + normalized[len(root):]

    localized_config = localize_tool_dispatcher_config(config if isinstance(config, dict) else load_tool_dispatcher_config())
    stale_aliases = (
        ("\\_cli_patch\\runtime_test\\smali", "{smali_dir}"),
        ("\\_cli_patch\\runtime_test\\jadx", "{jadx_out}"),
        ("\\_cli_patch\\runtime_test\\chain_runs", "{call_chain_output_root}"),
        ("\\_cli_patch\\runtime_test\\chain_index", "{call_chain_index_root}"),
    )
    for marker, alias_name in stale_aliases:
        marker_index = lowered_candidate.find(marker)
        if marker_index == -1:
            continue
        target_root = normalize_runtime_root(str(localized_config.get(alias_name.strip("{}"), "") or ""))
        if not target_root:
            continue
        suffix = normalized[marker_index + len(marker):]
        return alias_name + suffix
    return candidate


def localize_runtime_paths_in_text(text: str,
                                   config: Optional[Dict[str, Any]] = None,
                                   call_chain_text: str = "") -> str:
    source = str(text or "")
    if not source:
        return ""
    alias_pairs = build_runtime_alias_pairs(config=config, call_chain_text=call_chain_text)
    return WINDOWS_PATH_PATTERN.sub(
        lambda match: localize_runtime_path_value(
            match.group(0),
            alias_pairs=alias_pairs,
            config=config,
            call_chain_text=call_chain_text,
        ),
        source,
    )


def build_runtime_alias_map(config: Optional[Dict[str, Any]] = None,
                            tools: Optional[Dict[str, str]] = None,
                            call_chain_text: str = "") -> Dict[str, str]:
    config = localize_tool_dispatcher_config(config if isinstance(config, dict) else {})
    tools = tools or discover_tool_context(config)
    workspace_root = resolve_workspace_root(config)
    alias_map: Dict[str, str] = {
        "app_dir": APP_DIR,
        "workspace_root": workspace_root,
    }
    for key in ("target_dex", "smali_dir", "jadx_out", "call_chain_output_root", "call_chain_index_root"):
        value = str(config.get(key, "") or "")
        if value:
            alias_map[key] = value
    for key, value in tools.items():
        if value:
            alias_map[key] = value
    roots = runtime_review_roots(config)
    for index, root in enumerate(roots[:8], start=1):
        alias_map[f"allowed_root_{index}"] = root
    chain_context = build_call_chain_runtime_context(call_chain_text, config=config)
    for key in (
        "chain_work_dir",
        "chain_output_dir",
        "chain_log_dir",
        "chain_dispatch_dir",
        "chain_chat_dir",
        "chain_smali_dir",
        "chain_renamed_dex",
        "chain_jadx_dir",
        "chain_jadx_named_dir",
        "chain_jadx_analysis_dir",
        "chain_index_dir",
        "chain_index_file",
    ):
        value = str(chain_context.get(key, "") or "")
        if value:
            alias_map[key] = value
    return alias_map


def expand_runtime_aliases(text: str, alias_map: Dict[str, str]) -> Tuple[str, List[str]]:
    missing: List[str] = []

    def repl(match: re.Match) -> str:
        key = str(match.group(1) or "").strip()
        value = str(alias_map.get(key, "") or "")
        if not value:
            if key not in missing:
                missing.append(key)
            return match.group(0)
        return value

    expanded = re.sub(r"\{([A-Za-z0-9_.-]+)\}", repl, text or "")
    return expanded, missing


def build_runtime_alias_lines(config: Dict[str, Any], tools: Dict[str, str], call_chain_text: str = "") -> List[str]:
    alias_map = build_runtime_alias_map(config, tools=tools, call_chain_text=call_chain_text)
    aliases = [
        name for name in [
            "target_dex", "smali_dir", "jadx_out", "call_chain_output_root", "call_chain_index_root",
            "baksmali", "smali", "jadx", "jadx_toolchain", "smali_converter",
            "tool_dispatcher", "workspace_root", "app_dir", "chain_work_dir", "chain_output_dir",
            "chain_smali_dir", "chain_renamed_dex", "chain_jadx_dir", "chain_jadx_named_dir", "chain_jadx_analysis_dir", "chain_index_file"
        ]
        if alias_map.get(name)
    ]
    if not aliases:
        return []
    return [
        "Use runtime aliases instead of hardcoded local paths when possible.",
        "runtime_aliases={" + " | ".join(aliases) + "}",
    ]


def get_role_prompt_refresh_turns(config: Optional[Dict[str, Any]] = None) -> int:
    try:
        turns = int((config or {}).get("role_prompt_refresh_turns", DEFAULT_ROLE_PROMPT_REFRESH_TURNS) or DEFAULT_ROLE_PROMPT_REFRESH_TURNS)
    except Exception:
        turns = DEFAULT_ROLE_PROMPT_REFRESH_TURNS
    return max(1, turns)


def should_auto_followup_after_exec(config: Optional[Dict[str, Any]] = None) -> bool:
    return parse_bool_like((config or {}).get("auto_followup_after_exec", DEFAULT_AUTO_FOLLOWUP_AFTER_EXEC), DEFAULT_AUTO_FOLLOWUP_AFTER_EXEC)


def should_mirror_jadx_to_rename(config: Optional[Dict[str, Any]] = None) -> bool:
    return parse_bool_like((config or {}).get("mirror_jadx_to_rename", DEFAULT_MIRROR_JADX_TO_RENAME), DEFAULT_MIRROR_JADX_TO_RENAME)


def use_strict_tuning_message_scope(config: Optional[Dict[str, Any]] = None) -> bool:
    return parse_bool_like((config or {}).get("strict_tuning_message_scope", DEFAULT_STRICT_TUNING_MESSAGE_SCOPE), DEFAULT_STRICT_TUNING_MESSAGE_SCOPE)


def get_naming_dedup_mode(config: Optional[Dict[str, Any]] = None) -> str:
    mode = str((config or {}).get("naming_dedup_mode", DEFAULT_NAMING_DEDUP_MODE) or DEFAULT_NAMING_DEDUP_MODE).strip().lower()
    if mode not in {"off", "manual", "final_only", "always"}:
        return DEFAULT_NAMING_DEDUP_MODE
    return mode


def get_rename_apply_mode(config: Optional[Dict[str, Any]] = None) -> str:
    mode = str((config or {}).get("rename_apply_mode", DEFAULT_RENAME_APPLY_MODE) or DEFAULT_RENAME_APPLY_MODE).strip().lower()
    if mode not in {"hybrid", "analysis_only", "smali_first"}:
        return DEFAULT_RENAME_APPLY_MODE
    return mode


def should_force_new_meeting_for_toolchain(toolchain_key: str, config: Optional[Dict[str, Any]] = None) -> bool:
    if toolchain_key in DEFAULT_CHILD_THREAD_TOOLCHAINS:
        if toolchain_key == "jadx_source_chain":
            return parse_bool_like((config or {}).get("jadx_force_new_meeting", DEFAULT_JADX_FORCE_NEW_MEETING), DEFAULT_JADX_FORCE_NEW_MEETING)
        return True
    return False


def should_split_toolchain_session(toolchain_key: str,
                                   separate_toolchain_conversations: bool = DEFAULT_TOOLCHAIN_SEPARATE_CONVERSATIONS,
                                   config: Optional[Dict[str, Any]] = None) -> bool:
    if toolchain_key == "chain_root":
        return False
    if separate_toolchain_conversations:
        return True
    if toolchain_key in DEFAULT_CHILD_THREAD_TOOLCHAINS:
        return True
    return should_force_new_meeting_for_toolchain(toolchain_key, config=config)


def build_toolchain_config_lines(config: Dict[str, Any]) -> List[str]:
    if not config:
        return []
    lines: List[str] = []
    lines.append(f"runtime.review_initialized_roots_only={'true' if parse_bool_like(config.get('review_initialized_roots_only', True), True) else 'false'}")
    lines.append(f"runtime.strict_tuning_message_scope={'true' if use_strict_tuning_message_scope(config) else 'false'}")
    return lines


def should_use_quick_feedback(dispatch_payload: Dict[str, Any]) -> bool:
    results = list(dispatch_payload.get("results", []))
    joined = " ".join(
        shorten_text(str(item.get("stdout_preview", "") or item.get("stderr_preview", "")), 160)
        for item in results[:4]
    )
    return len(joined) <= 800


def assess_result_adequacy(result: Dict[str, Any],
                           route_item: Optional[Dict[str, Any]] = None,
                           call_chain_text: str = "") -> Dict[str, Any]:
    route_item = route_item if isinstance(route_item, dict) else {}
    output_text = str(
        result.get("stdout", "")
        or result.get("artifact_preview", "")
        or result.get("stderr", "")
        or ""
    )
    output_preview = shorten_text(output_text, 240)
    judge_status = int(result.get("judge_status", 1) or 1)
    has_output = bool(result.get("has_output"))
    toolchain_key = str(route_item.get("toolchain_key", "") or "")
    if judge_status != 0:
        return {"ok": False, "reason": "tool_error", "preview": output_preview}
    if not has_output:
        return {"ok": True, "reason": "no_output_but_success", "preview": output_preview}
    if toolchain_key not in {"baksmali_xref_chain", "baksmali_subclasses_chain", "jadx_source_chain"}:
        return {"ok": True, "reason": "non_checked_toolchain", "preview": output_preview}

    extracted = extract_targets(output_text, limit=8)
    expected = unique_keep_order(
        list(route_item.get("targets", [])) + extract_targets(call_chain_text, limit=8),
        limit=8,
    )
    overlap = set(extracted).intersection(expected)
    lowered = output_text.lower()
    weak_markers = ["not found", "0 results", "no matches", "empty", "error:"]
    if len(output_text.strip()) < 24 or (expected and not overlap and any(token in lowered for token in weak_markers)):
        return {
            "ok": False,
            "reason": "insufficient_output",
            "preview": output_preview,
            "expected_targets": expected[:4],
            "found_targets": extracted[:4],
        }
    return {"ok": True, "reason": "sufficient_output", "preview": output_preview}


def build_exec_feedback_message(dispatch_payload: Dict[str, Any],
                                config: Dict[str, Any],
                                call_chain_text: str = "") -> Tuple[str, str]:
    results = list(dispatch_payload.get("results", []))
    error_results = [item for item in results if int(item.get("judge_status", 1)) != 0]
    success_results = [
        item for item in results
        if int(item.get("judge_status", 1)) == 0 and bool(item.get("has_output"))
    ]
    if error_results:
        focus = str(config.get("focus_on_error", "只根据本地错误日志修正命令，不重开新链路。"))
        payload_lines = [
            "本地执行失败，请基于错误日志继续修正命令。",
            f"专注修正: {focus}",
        ]
        if call_chain_text:
            payload_lines.append(f"调用链: {call_chain_text}")
        for item in error_results[:3]:
            payload_lines.append(f"tool={item.get('tool', '')}")
            payload_lines.append(f"command={item.get('command', '')}")
            payload_lines.append(f"stderr={item.get('stderr', '') or item.get('stdout', '')}")
        return "\n".join(payload_lines), "error"

    if success_results:
        focus = str(config.get("focus_on_success", "继续沿用当前调用链和角色卡。"))
        payload_lines = [
            "本地执行成功，请基于执行输出继续推进调用链分析。",
            f"专注保持: {focus}",
        ]
        if call_chain_text:
            payload_lines.append(f"调用链: {call_chain_text}")
        for item in success_results[:3]:
            payload_lines.append(f"tool={item.get('tool', '')}")
            payload_lines.append(f"command={item.get('command', '')}")
            payload_lines.append(f"stdout={item.get('stdout', '') or item.get('stderr', '')}")
        return "\n".join(payload_lines), "success"

    return "", "none"


def build_result_followup_message(result: Dict[str, Any],
                                  route_item: Optional[Dict[str, Any]] = None,
                                  call_chain_text: str = "") -> Tuple[str, str]:
    route_item = route_item if isinstance(route_item, dict) else {}
    tool = str(result.get("tool", "") or route_item.get("tool", "") or "")
    toolchain_key = str(route_item.get("toolchain_key", "") or "")
    command_type = str(route_item.get("command_type", "") or "")
    command = str(result.get("command", "") or "")
    next_query = str(result.get("next_query", "") or "")
    output_text = str(
        result.get("stdout", "")
        or result.get("artifact_preview", "")
        or result.get("stderr", "")
        or ""
    ).strip()
    preview_text = shorten_text(output_text, 2800) if output_text else ""
    try:
        judge_status = int(result.get("judge_status", 1))
    except Exception:
        judge_status = 1
    has_output = bool(result.get("has_output"))
    phase_label = first_nonempty(toolchain_key, command_type, tool)
    adequacy = assess_result_adequacy(result, route_item=route_item, call_chain_text=call_chain_text)

    if judge_status != 0:
        lines = [
            f"本地工具链报错，继续修正当前链路。",
            f"工具链={phase_label}",
        ]
        if call_chain_text:
            lines.append(f"调用链={call_chain_text}")
        if command:
            lines.append(f"命令={command}")
        if preview_text:
            lines.append(f"错误日志={preview_text}")
        return "\n".join(lines), "error"

    artifact_paths = list(result.get("artifact_paths", [])) if isinstance(result.get("artifact_paths", []), list) else []
    lowered_command = str(command or "").lower()
    if toolchain_key == "jadx_named_chain" and has_output and " record-hit " in f" {lowered_command} ":
        lines = [
            "本地 analysis_pkg 与 record-hit 已闭环，named JADX 子线程不要再次返回 record-hit、archive-sources 或 export-classes。",
            f"工具链={phase_label}",
        ]
        if call_chain_text:
            lines.append(f"调用链={call_chain_text}")
        if artifact_paths:
            lines.append("产物=" + " | ".join(shorten_text(str(path), 160) for path in artifact_paths[:4]))
        lines.append("下一步=回主线程整理总结，不再重复执行同一条命令。")
        return "\n".join(lines), "success"

    if is_smali_roundtrip_result(route_item=route_item, result=result):
        lines = [
            "本地 smali 改名链路已完成，本轮不要重跑同一条 smali 命令。",
            f"工具链={phase_label}",
        ]
        if call_chain_text:
            lines.append(f"调用链={call_chain_text}")
        renamed_dex_paths = [
            str(path) for path in artifact_paths
            if str(path).lower().endswith(".dex")
        ]
        if renamed_dex_paths:
            lines.append("renamed_dex=" + " | ".join(shorten_text(path, 180) for path in renamed_dex_paths[:2]))
            lines.append("下一步=让 named JADX 优先基于 renamed_dex 重跑当前选中的调用链类。")
        elif artifact_paths:
            lines.append("产物=" + " | ".join(shorten_text(str(path), 160) for path in artifact_paths[:4]))
        return "\n".join(lines), "success"

    if toolchain_key == "jadx_named_chain" and has_output:
        named_export_files = collect_chain_named_export_files(call_chain_text, limit=3)
        named_export_done = bool(
            command_targets_named_export_output(command)
            or any(command_targets_named_export_output(str(path)) or "named_export" in str(path).lower() for path in artifact_paths)
            or named_export_files
        )
        analysis_pkg_done = bool(
            command_targets_analysis_pkg_output(command)
            or any(command_targets_analysis_pkg_output(str(path)) or "analysis_pkg" in str(path).lower() for path in artifact_paths)
        )
        if named_export_done and not analysis_pkg_done:
            lines = [
                "本地 named_export 已完成，禁止重复执行 export-classes。",
                f"工具链={phase_label}",
            ]
            if call_chain_text:
                lines.append(f"调用链={call_chain_text}")
            lines.append("下一步=只基于 {chain_jadx_named_dir} 已导出的源码整理分析结果，并保存到 {chain_jadx_analysis_dir}。")
            lines.append("如果需要命令，只返回写入 analysis_pkg 或更新 {chain_index_file} 的命令，不要再返回 JADX 导出命令。")
            if next_query:
                lines.append(f"原下一步={next_query}")
            named_sources: List[str] = []
            runtime_context = build_call_chain_runtime_context(call_chain_text)
            named_dir = str(runtime_context.get("chain_jadx_named_dir", "") or "")
            for path in named_export_files[:2]:
                display_path = str(path)
                if named_dir and display_path.startswith(named_dir):
                    display_path = "{chain_jadx_named_dir}" + display_path[len(named_dir):]
                named_sources.append(shorten_text(display_path.replace("/", "\\"), 160))
            if named_sources:
                lines.append("现有源码=" + " | ".join(named_sources))
            elif artifact_paths:
                lines.append("产物=" + " | ".join(shorten_text(str(path), 160) for path in artifact_paths[:4]))
            if preview_text:
                lines.append(f"当前输出摘要={shorten_text(preview_text, 1200)}")
            return "\n".join(lines), "success"
        if analysis_pkg_done:
            lines = [
                "本地 analysis_pkg 已写入，继续整理摘要并回主线程，不要重复导出或重复写入。",
                f"工具链={phase_label}",
            ]
            if call_chain_text:
                lines.append(f"调用链={call_chain_text}")
            if artifact_paths:
                lines.append("产物=" + " | ".join(shorten_text(str(path), 160) for path in artifact_paths[:4]))
            return "\n".join(lines), "success"

    if has_output and preview_text:
        lines = [
            f"本地工具链执行成功，继续分析当前输出。",
            f"工具链={phase_label}",
        ]
        if call_chain_text:
            lines.append(f"调用链={call_chain_text}")
        if next_query:
            lines.append(f"下一步={next_query}")
        lines.append(f"输出={preview_text}")
        if artifact_paths:
            lines.append("产物=" + " | ".join(str(path) for path in artifact_paths[:4]))
        return "\n".join(lines), "success"

    lines = [
        f"本地工具链执行成功但无输出，请根据状态继续推进。",
        f"工具链={phase_label}",
        f"returncode={result.get('returncode', '')}",
    ]
    if call_chain_text:
        lines.append(f"调用链={call_chain_text}")
    if command:
        lines.append(f"命令={command}")
    if next_query:
        lines.append(f"下一步={next_query}")
    return "\n".join(lines), "success"


def resolve_command_route_for_result(result_index: int,
                                     result: Dict[str, Any],
                                     command_routes: List[Dict[str, Any]],
                                     call_chain_text: str) -> Dict[str, Any]:
    target_index = int(result.get("entry_index", result_index) or result_index)
    for item in command_routes:
        if not isinstance(item, dict):
            continue
        if int(item.get("index", -1) or -1) == target_index:
            return dict(item)

    tool = str(result.get("tool", "") or "")
    command = str(result.get("command", "") or "")
    inferred = infer_toolchain_route(tool, command, profile_hint=str(result.get("profile", "") or ""))
    resolved = resolve_role_takeover_route(
        call_chain_text,
        preferred_profile=str(inferred.get("profile", "") or DEFAULT_PROFILE),
        command_type=str(inferred.get("command_type", "") or ""),
        toolchain_key=str(inferred.get("toolchain_key", "") or ""),
    )
    route = {
        "index": result_index,
        "tool": tool,
        "profile": str(inferred.get("profile", "") or result.get("profile", "") or DEFAULT_PROFILE),
        "command_type": str(inferred.get("command_type", "") or ""),
        "toolchain_key": str(inferred.get("toolchain_key", "") or ""),
    }
    route.update(resolved)
    return route


def ensure_session_binding(conv_manager: "ConversationManager",
                           session_name: str,
                           profile: str,
                           profile_reason: str,
                           group_ref: str = "",
                           slot_ref: str = "",
                           conversation_id: str = "",
                           section_id: str = "") -> None:
    if not session_name:
        return
    if session_name in conv_manager.sessions:
        conv_manager.update_ids(session_name, conversation_id, section_id)
        conv_manager.set_profile(session_name, profile, reason=profile_reason)
    else:
        conv_manager.create(session_name, conversation_id, section_id, profile=profile, profile_reason=profile_reason)
    if group_ref or slot_ref:
        conv_manager.set_thread_refs(session_name, group_ref=group_ref, slot_ref=slot_ref)


def sync_jadx_result_to_rename_session(call_chain_text: str,
                                       route_item: Dict[str, Any],
                                       result: Dict[str, Any],
                                       followup_reply: str,
                                       background_summary: str,
                                       conv_manager: "ConversationManager",
                                       profile_reason: str,
                                       toolchain_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not call_chain_text or not should_mirror_jadx_to_rename(toolchain_config):
        return {}

    rename_route = resolve_role_takeover_route(
        call_chain_text,
        preferred_profile="rename_mapping",
        command_type="rename_mapping",
        toolchain_key="rename_followup_chain",
    )
    session_name = str(rename_route.get("session_name", "") or "")
    if not session_name:
        base_name = base_chain_session_name(str(route_item.get("session_name", "") or "chain"))
        session_name = f"{base_name}__rename_followup_chain"
        rename_route = remember_role_takeover_route(
            call_chain_text,
            profile="rename_mapping",
            command_type="rename_mapping",
            toolchain_key="rename_followup_chain",
            session_name=session_name,
            conversation_id="",
            section_id="",
            role_card="rename_mapping",
            tool="rename_mapping",
            toolchain_entry="rename_followup_chain",
            note="jadx_result_bridge",
        )

    ensure_session_binding(
        conv_manager,
        session_name=session_name,
        profile="rename_mapping",
        profile_reason=profile_reason,
        group_ref=str(rename_route.get("group_ref", "") or ""),
        slot_ref=str(rename_route.get("slot_ref", "") or ""),
        conversation_id=str(rename_route.get("conversation_id", "") or ""),
        section_id=str(rename_route.get("section_id", "") or ""),
    )

    bridge_parts = [
        f"JADX结果桥接",
        f"工具={result.get('tool', '')}",
    ]
    if result.get("hit_class"):
        bridge_parts.append(f"类={result.get('hit_class')}")
    if background_summary:
        bridge_parts.append(f"背景={background_summary}")
    bridge_output = first_nonempty(
        background_summary,
        str(result.get("stdout", "") or ""),
        str(result.get("stderr", "") or ""),
        followup_reply,
    )
    if bridge_output:
        bridge_parts.append(bridge_output)
    bridge_fragment = " | ".join(part for part in bridge_parts if part)
    bridge_targets = extract_class_targets(call_chain_text, limit=8) + extract_class_targets(bridge_output, limit=8)
    conv_manager.remember_local_runtime_block(session_name, {
        "kind": "bridge",
        "tool": str(result.get("tool", "") or route_item.get("tool", "") or "jadx"),
        "profile": "rename_mapping",
        "command_type": "rename_mapping",
        "toolchain_key": "rename_followup_chain",
        "status": "bridged",
        "judge_status": 0,
        "returncode": int(result.get("returncode", 0) or 0),
        "has_output": bool(bridge_output),
        "hit_class": str(result.get("hit_class", "") or ""),
        "next_query": str(result.get("next_query", "") or ""),
        "output_preview": bridge_output,
        "target_refs": bridge_targets,
        "bridge_target": "rename_mapping",
        "updated_at": time.time(),
    })
    conv_manager.append_analysis_fragment(session_name, bridge_fragment, targets=bridge_targets)
    update_thread_slot_metadata(
        call_chain_text,
        profile="rename_mapping",
        command_type="rename_mapping",
        toolchain_key="rename_followup_chain",
        metadata={
            "bridge_from_toolchain": "jadx_source_chain",
            "last_bridge_at": time.time(),
            "child_thread": True,
            "independent_meeting": False,
        },
    )
    return {
        "session_name": session_name,
        "group_ref": str(rename_route.get("group_ref", "") or ""),
        "slot_ref": str(rename_route.get("slot_ref", "") or ""),
    }


def maybe_regenerate_duplicate_names(client: "DoubaoClient",
                                     conv_manager: "ConversationManager",
                                     session_name: str,
                                     stored_session: Dict[str, Any],
                                     route_item: Dict[str, Any],
                                     call_chain_text: str,
                                     reply_text: str,
                                     profile: str,
                                     profile_reason: str,
                                     focus_note: str,
                                     toolchain_config: Dict[str, Any]) -> Tuple[str, Dict[str, List[str]], Dict[str, Any]]:
    if profile not in {"jadx_code_analyst", "rename_mapping", "rename_tuning", "naming_finalize"}:
        return reply_text, {}, {}
    command_items = collect_command_items_from_text(reply_text)
    name_assignments = collect_naming_assignments(
        reply_text,
        command_items,
        call_chain_text=call_chain_text,
        route_item=route_item,
    )
    candidate_names = merge_candidate_bucket(
        collect_candidate_names(reply_text, command_items),
        bucket_from_name_assignments(name_assignments),
        limit=16,
    )
    if not candidate_names and not name_assignments:
        return reply_text, {}, {}
    conflicts = detect_name_conflicts(call_chain_text, candidate_names, name_assignments=name_assignments)
    if not conflicts.get("items"):
        return reply_text, candidate_names, {"name_assignments": name_assignments}

    conflict_message = build_name_conflict_message(
        call_chain_text,
        conflicts,
        toolchain_key=str(route_item.get("toolchain_key", "") or ""),
    )
    regen_profile = "rename_tuning"
    regen_reason = "命名冲突回流"
    session_context = build_session_context_payload(
        stored_session=stored_session,
        active_route_entry=route_item,
        call_chain_text=call_chain_text,
        message=conflict_message,
        profile=regen_profile,
        profile_reason=regen_reason,
        focus_note=focus_note,
        toolchain_config=toolchain_config,
        force_refresh=False,
    )
    local_mode = resolve_local_chat_mode(regen_profile, text=conflict_message, feedback_kind="success")
    original_state = (client.think_mode, client.use_auto_cot, client.use_deep_think)
    client.think_mode = int(local_mode["think_mode"])
    client.use_auto_cot = bool(local_mode["use_auto_cot"])
    client.use_deep_think = bool(local_mode["use_deep_think"])
    regenerated_reply, _, new_conv_id, new_sec_id = client.send_message(
        conflict_message,
        guest=False,
        conversation_id=str(stored_session.get("conversation_id", "") or ""),
        section_id=str(stored_session.get("section_id", "") or ""),
        stream=True,
        apply_initial_prompt=bool(session_context.get("role_prompt_refresh_due")),
        forced_profile=regen_profile,
        session_data=session_context,
        call_chain_text=call_chain_text,
        route_entry=route_item,
    )
    client.think_mode, client.use_auto_cot, client.use_deep_think = original_state

    ensure_session_binding(
        conv_manager,
        session_name=session_name,
        profile=regen_profile,
        profile_reason=regen_reason,
        group_ref=str(route_item.get("group_ref", "") or ""),
        slot_ref=str(route_item.get("slot_ref", "") or ""),
        conversation_id=new_conv_id or str(stored_session.get("conversation_id", "") or ""),
        section_id=new_sec_id or str(stored_session.get("section_id", "") or ""),
    )
    conv_manager.remember_user_context(session_name, conflict_message, regen_profile, regen_reason)
    conv_manager.remember_assistant_reply(session_name, regenerated_reply)
    regenerated_command_items = collect_command_items_from_text(regenerated_reply)
    regenerated_assignments = collect_naming_assignments(
        regenerated_reply,
        regenerated_command_items,
        call_chain_text=call_chain_text,
        route_item=route_item,
    )
    regenerated_candidates = merge_candidate_bucket(
        collect_candidate_names(regenerated_reply, regenerated_command_items),
        bucket_from_name_assignments(regenerated_assignments),
        limit=16,
    )
    remaining_conflicts = detect_name_conflicts(
        call_chain_text,
        regenerated_candidates,
        name_assignments=regenerated_assignments,
    )
    regen_payload = {
        "requested": True,
        "conflicts": conflicts,
        "remaining_conflicts": remaining_conflicts,
        "name_assignments": regenerated_assignments or name_assignments,
        "reply_text": regenerated_reply,
    }
    if remaining_conflicts.get("items"):
        conv_manager.append_analysis_fragment(
            session_name,
            "命名冲突仍未解决，请继续避免重名: " + " | ".join(
                f"{bucket}={' / '.join(values[:4])}" for bucket, values in (
                    remaining_conflicts.get("by_bucket", {}) if isinstance(remaining_conflicts.get("by_bucket", {}), dict) else {}
                ).items()
            ),
            targets=extract_targets(call_chain_text, limit=6),
        )
    return regenerated_reply or reply_text, regenerated_candidates or candidate_names, regen_payload


def build_name_assignment_prompt_lines(assignments: List[Dict[str, Any]], limit: int = 8) -> List[str]:
    lines: List[str] = []
    for item in assignments[:limit]:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"{item.get('bucket', 'general')} | source={item.get('source_key', '-') or '-'} | "
            f"candidate={item.get('candidate', '-') or '-'} | owner={item.get('owner_class', '-') or '-'}"
        )
    return lines


def command_requests_smali_mutation(tool: str, command_text: str) -> bool:
    lowered_tool = str(tool or "").strip().lower()
    normalized_command = normalize_archive_sources_alias_flags(str(command_text or ""))
    lowered = normalized_command.lower()
    if lowered_tool == "smali_converter":
        if " apply-renames " in f" {lowered} ":
            return True
        if any(token in lowered for token in (" --rename-class ", " --rename-method ", " --rename-field ")):
            return True
        return bool(
            any(token in lowered for token in (" -to smali ", " --target smali ", "--target=smali"))
            and any(token in lowered for token in ("{chain_smali_dir}", "chain_smali_dir", "{smali_dir}", "smali_dir"))
        )
    smali_scope = any(token in lowered for token in (
        "{chain_smali_dir}",
        "{smali_dir}",
        "chain_smali_dir",
        "smali_dir",
        ".smali",
    ))
    if not smali_scope:
        return False
    if any(token in lowered for token in ("subclasses", "select-string", "get-content", "show-hit", "find-class", "export-classes")):
        return False
    return any(token in lowered for token in (
        "set-content",
        "add-content",
        "copy-item",
        "move-item",
        "rename-item",
        "replace",
        "smali_converter",
        " rename ",
    ))


def collect_smali_mutation_commands(command_items: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for item in command_items or []:
        if not isinstance(item, dict):
            continue
        if command_requests_smali_mutation(str(item.get("tool", "") or ""), str(item.get("command", "") or "")):
            result.append(item)
    return result


def store_pending_named_jadx_state(call_chain_text: str,
                                   candidate_names: Dict[str, List[str]],
                                   name_assignments: List[Dict[str, Any]],
                                   background_summary: str = "",
                                   reason: str = "") -> Dict[str, Any]:
    return update_thread_slot_metadata(
        call_chain_text,
        profile="rename_tuning",
        command_type="rename_tuning",
        toolchain_key="rename_tuning_chain",
        metadata={
            "pending_named_jadx": True,
            "pending_named_jadx_candidate_names": dict(candidate_names or {}),
            "pending_named_jadx_name_assignments": [dict(item) for item in list(name_assignments or [])[:32] if isinstance(item, dict)],
            "pending_named_jadx_background_summary": str(background_summary or ""),
            "pending_named_jadx_reason": str(reason or ""),
            "pending_named_jadx_updated_at": time.time(),
        },
    )


def resolve_pending_named_jadx_state(call_chain_text: str) -> Dict[str, Any]:
    slot = resolve_thread_slot(
        call_chain_text,
        profile="rename_tuning",
        command_type="rename_tuning",
        toolchain_key="rename_tuning_chain",
    )
    if not isinstance(slot, dict) or not bool(slot.get("pending_named_jadx", False)):
        return {}
    return {
        "candidate_names": dict(slot.get("pending_named_jadx_candidate_names", {}) or {}),
        "name_assignments": list(slot.get("pending_named_jadx_name_assignments", []) or []),
        "background_summary": str(slot.get("pending_named_jadx_background_summary", "") or ""),
        "reason": str(slot.get("pending_named_jadx_reason", "") or ""),
        "updated_at": slot.get("pending_named_jadx_updated_at"),
    }


def clear_pending_named_jadx_state(call_chain_text: str) -> Dict[str, Any]:
    return update_thread_slot_metadata(
        call_chain_text,
        profile="rename_tuning",
        command_type="rename_tuning",
        toolchain_key="rename_tuning_chain",
        metadata={
            "pending_named_jadx": False,
            "pending_named_jadx_candidate_names": {},
            "pending_named_jadx_name_assignments": [],
            "pending_named_jadx_background_summary": "",
            "pending_named_jadx_reason": "",
            "pending_named_jadx_updated_at": time.time(),
        },
    )


def should_launch_named_jadx_now(command_items: Optional[List[Dict[str, Any]]],
                                 config: Optional[Dict[str, Any]] = None,
                                 route_item: Optional[Dict[str, Any]] = None) -> bool:
    mode = get_rename_apply_mode(config)
    if mode == "analysis_only":
        return True
    if collect_smali_mutation_commands(command_items):
        return False
    if mode == "smali_first":
        return str((route_item or {}).get("toolchain_key", "") or "") == "smali_conversion_chain"
    return True


def is_smali_roundtrip_result(route_item: Optional[Dict[str, Any]],
                              result: Optional[Dict[str, Any]]) -> bool:
    route_item = route_item if isinstance(route_item, dict) else {}
    result = result if isinstance(result, dict) else {}
    if int(result.get("judge_status", 1) or 1) != 0:
        return False
    tool = str(result.get("tool", "") or route_item.get("tool", "") or "").lower()
    command_text = str(result.get("command", "") or route_item.get("command", "") or "")
    if command_requests_smali_mutation(tool, command_text):
        return True
    for path in list(result.get("artifact_paths", [])) if isinstance(result.get("artifact_paths", []), list) else []:
        lowered = str(path or "").lower()
        if ".smali" in lowered or "\\runtime\\smali_out\\" in lowered or "\\smali\\" in lowered:
            return True
    return False


def send_bridge_followup_message(client: "DoubaoClient",
                                 conv_manager: "ConversationManager",
                                 call_chain_text: str,
                                 route_item: Dict[str, Any],
                                 profile: str,
                                 profile_reason: str,
                                 command_type: str,
                                 toolchain_key: str,
                                 message: str,
                                 focus_note: str,
                                 toolchain_config: Dict[str, Any],
                                 bridge_target: str = "",
                                 bridge_preview: str = "",
                                 bridge_targets: Optional[List[str]] = None,
                                 note: str = "bridge_followup") -> Dict[str, Any]:
    bridge_route = resolve_role_takeover_route(
        call_chain_text,
        preferred_profile=profile,
        command_type=command_type,
        toolchain_key=toolchain_key,
    )
    session_name = str(bridge_route.get("session_name", "") or "")
    if not session_name:
        base_name = base_chain_session_name(str(route_item.get("session_name", "") or "chain"))
        session_name = f"{base_name}__{safe_slug(toolchain_key or command_type or profile)}"
        remember_role_takeover_route(
            call_chain_text,
            profile=profile,
            command_type=command_type,
            toolchain_key=toolchain_key,
            session_name=session_name,
            conversation_id="",
            section_id="",
            role_card=profile,
            tool=str(bridge_target or profile or route_item.get("tool", "")),
            toolchain_entry=toolchain_key,
            note=note,
        )
        bridge_route = resolve_role_takeover_route(
            call_chain_text,
            preferred_profile=profile,
            command_type=command_type,
            toolchain_key=toolchain_key,
        )
    ensure_session_binding(
        conv_manager,
        session_name=session_name,
        profile=profile,
        profile_reason=profile_reason,
        group_ref=str(bridge_route.get("group_ref", "") or ""),
        slot_ref=str(bridge_route.get("slot_ref", "") or ""),
        conversation_id=str(bridge_route.get("conversation_id", "") or ""),
        section_id=str(bridge_route.get("section_id", "") or ""),
    )
    if bridge_preview:
        conv_manager.remember_local_runtime_block(session_name, {
            "kind": "bridge",
            "tool": bridge_target or str(route_item.get("tool", "") or profile),
            "profile": profile,
            "command_type": command_type,
            "toolchain_key": toolchain_key,
            "status": "bridged",
            "judge_status": 0,
            "returncode": 0,
            "has_output": True,
            "output_preview": bridge_preview,
            "target_refs": list(bridge_targets or []),
            "bridge_target": bridge_target or profile,
            "updated_at": time.time(),
        })
        conv_manager.append_analysis_fragment(session_name, bridge_preview, targets=list(bridge_targets or []))
    stored_session = dict(conv_manager.sessions.get(session_name, {}) or {})
    session_context = build_session_context_payload(
        stored_session=stored_session,
        active_route_entry=bridge_route,
        call_chain_text=call_chain_text,
        message=message,
        profile=profile,
        profile_reason=profile_reason,
        focus_note=focus_note,
        toolchain_config=toolchain_config,
        force_refresh=not bool(stored_session.get("conversation_id")),
    )
    local_mode = resolve_local_chat_mode(profile, text=message, feedback_kind="success")
    original_state = (client.think_mode, client.use_auto_cot, client.use_deep_think)
    client.think_mode = int(local_mode["think_mode"])
    client.use_auto_cot = bool(local_mode["use_auto_cot"])
    client.use_deep_think = bool(local_mode["use_deep_think"])
    reply_text, _, new_conv_id, new_sec_id = client.send_message(
        message,
        guest=False,
        conversation_id=str(stored_session.get("conversation_id", "") or ""),
        section_id=str(stored_session.get("section_id", "") or ""),
        stream=True,
        apply_initial_prompt=bool(not stored_session.get("conversation_id") or session_context.get("role_prompt_refresh_due")),
        forced_profile=profile,
        session_data=session_context,
        call_chain_text=call_chain_text,
        route_entry=bridge_route,
    )
    client.think_mode, client.use_auto_cot, client.use_deep_think = original_state
    ensure_session_binding(
        conv_manager,
        session_name=session_name,
        profile=profile,
        profile_reason=profile_reason,
        group_ref=str(bridge_route.get("group_ref", "") or ""),
        slot_ref=str(bridge_route.get("slot_ref", "") or ""),
        conversation_id=new_conv_id or str(stored_session.get("conversation_id", "") or ""),
        section_id=new_sec_id or str(stored_session.get("section_id", "") or ""),
    )
    updated_route = remember_role_takeover_route(
        call_chain_text,
        profile=profile,
        command_type=command_type,
        toolchain_key=toolchain_key,
        session_name=session_name,
        conversation_id=new_conv_id or str(stored_session.get("conversation_id", "") or ""),
        section_id=new_sec_id or str(stored_session.get("section_id", "") or ""),
        role_card=profile,
        tool=str(bridge_target or profile or route_item.get("tool", "")),
        toolchain_entry=toolchain_key,
        note=note,
    )
    if updated_route:
        bridge_route = updated_route
        update_thread_slot_metadata(
            call_chain_text,
            profile=profile,
            command_type=command_type,
            toolchain_key=toolchain_key,
            metadata={
                "last_bridge_at": time.time(),
            },
        )
    conv_manager.remember_user_context(session_name, message, profile, profile_reason)
    conv_manager.remember_assistant_reply(session_name, reply_text)
    if session_context.get("role_prompt_refresh_due"):
        conv_manager.mark_prompt_refresh(session_name, session_context.get("user_turn_count"))
    return {
        "route_entry": bridge_route,
        "session_name": session_name,
        "reply_text": reply_text,
        "conversation_id": new_conv_id or str(stored_session.get("conversation_id", "") or ""),
        "section_id": new_sec_id or str(stored_session.get("section_id", "") or ""),
    }


def maybe_launch_rename_tuning_bridge(client: "DoubaoClient",
                                      conv_manager: "ConversationManager",
                                      route_item: Dict[str, Any],
                                      call_chain_text: str,
                                      candidate_names: Dict[str, List[str]],
                                      name_assignments: List[Dict[str, Any]],
                                      background_summary: str,
                                      focus_note: str,
                                      toolchain_config: Dict[str, Any]) -> Dict[str, Any]:
    toolchain_key = str(route_item.get("toolchain_key", "") or "")
    if toolchain_key in {"rename_tuning_chain", "jadx_named_chain"}:
        return {}
    if not candidate_names and not name_assignments:
        return {}

    message_lines = [
        "进入命名微调子线程。",
        "只返回 naming_assignments 和 naming_candidates，不要输出命令，不要直接输出改写代码。",
        "允许同一个 source 继续复用同一个 candidate；不同 source 不能共用同名。",
    ]
    if use_strict_tuning_message_scope(toolchain_config):
        message_lines.append("严格限制：只对本条消息里出现的 source、owner、candidate 做校验，不要根据上下文补新的类、方法、字段。")
    if call_chain_text:
        message_lines.append(f"调用链={call_chain_text}")
    if background_summary:
        message_lines.append(f"背景={background_summary}")
    if candidate_names:
        message_lines.append("候选=" + json.dumps(candidate_names, ensure_ascii=False))
    if name_assignments:
        message_lines.append("已有映射:")
        message_lines.extend(build_name_assignment_prompt_lines(name_assignments, limit=8))
    message_lines.append("如果 source 身份不清，请补全 source；如果撞名，请重新给唯一名。")
    bridge_preview = shorten_text("\n".join(message_lines), 520)
    bridge_targets = extract_targets(call_chain_text, limit=8)
    bridge_result = send_bridge_followup_message(
        client=client,
        conv_manager=conv_manager,
        call_chain_text=call_chain_text,
        route_item=route_item,
        profile="rename_tuning",
        profile_reason="bridge:rename_tuning",
        command_type="rename_tuning",
        toolchain_key="rename_tuning_chain",
        message="\n".join(message_lines),
        focus_note=focus_note,
        toolchain_config=toolchain_config,
        bridge_target="rename_tuning",
        bridge_preview=bridge_preview,
        bridge_targets=bridge_targets,
        note="rename_tuning_bridge",
    )
    if not bridge_result.get("reply_text"):
        return {}
    tuning_route = dict(bridge_result.get("route_entry", {}) or {})
    refreshed_session = dict(conv_manager.sessions.get(str(bridge_result.get("session_name", "") or ""), {}) or {})
    tuned_reply, tuned_candidates, regen_payload = maybe_regenerate_duplicate_names(
        client=client,
        conv_manager=conv_manager,
        session_name=str(bridge_result.get("session_name", "") or ""),
        stored_session=refreshed_session,
        route_item=tuning_route,
        call_chain_text=call_chain_text,
        reply_text=str(bridge_result.get("reply_text", "") or ""),
        profile="rename_tuning",
        profile_reason="bridge:rename_tuning",
        focus_note=focus_note,
        toolchain_config=toolchain_config,
    )
    tuned_command_items = collect_command_items_from_text(tuned_reply)
    tuned_assignments = collect_naming_assignments(
        tuned_reply,
        tuned_command_items,
        call_chain_text=call_chain_text,
        route_item=tuning_route,
    )
    tuned_candidates = merge_candidate_bucket(
        tuned_candidates,
        bucket_from_name_assignments(tuned_assignments),
        limit=16,
    )
    background_entry = remember_chain_background(
        call_chain_text,
        toolchain_key="rename_tuning_chain",
        session_name=str(bridge_result.get("session_name", "") or ""),
        reply_text=tuned_reply,
        command_items=tuned_command_items,
        result=None,
        profile="rename_tuning",
    )
    if background_entry.get("summary"):
        conv_manager.append_analysis_fragment(
            str(bridge_result.get("session_name", "") or ""),
            str(background_entry.get("summary", "") or ""),
            targets=list(background_entry.get("targets", [])),
        )
    remaining_conflicts = regen_payload.get("remaining_conflicts", {}) if isinstance(regen_payload, dict) else {}
    stable = not bool((remaining_conflicts.get("items", []) if isinstance(remaining_conflicts, dict) else []))
    return {
        "session_name": str(bridge_result.get("session_name", "") or ""),
        "conversation_id": str(bridge_result.get("conversation_id", "") or ""),
        "section_id": str(bridge_result.get("section_id", "") or ""),
        "route_entry": tuning_route,
        "reply_text": tuned_reply,
        "candidate_names": tuned_candidates,
        "name_assignments": tuned_assignments or regen_payload.get("name_assignments", []),
        "remaining_conflicts": remaining_conflicts,
        "stable": stable and bool(tuned_candidates or tuned_assignments),
        "chain_background_summary": str(background_entry.get("summary", "") or ""),
    }


def maybe_launch_named_jadx_bridge(client: "DoubaoClient",
                                   conv_manager: "ConversationManager",
                                   route_item: Dict[str, Any],
                                   call_chain_text: str,
                                   candidate_names: Dict[str, List[str]],
                                   name_assignments: List[Dict[str, Any]],
                                   background_summary: str,
                                   focus_note: str,
                                   toolchain_config: Dict[str, Any]) -> Dict[str, Any]:
    if str(route_item.get("toolchain_key", "") or "") != "rename_tuning_chain":
        return {}
    if not candidate_names and not name_assignments:
        return {}
    runtime_context = build_call_chain_runtime_context(call_chain_text, config=toolchain_config)
    message_lines = [
        "进入新的 JADX 子线程。",
        "先只返回当前调用链选中类的窄导出命令 JSON，输出到 {chain_jadx_named_dir}。",
        "不要导出整个 dex，不要带无关依赖。",
        "拿到新的 JADX 代码后，再结合当前命名和背景继续整理，并把整理后的代码保存到 {chain_jadx_analysis_dir}，目录结构必须和包名一致。",
    ]
    if use_strict_tuning_message_scope(toolchain_config):
        message_lines.append("严格限制：analysis_pkg 只能基于本条消息确认过的 naming_assignments 和本地源码做整理，不要脑补新的方法、字段、类。")
    if call_chain_text:
        message_lines.append(f"调用链={call_chain_text}")
    if background_summary:
        message_lines.append(f"背景={background_summary}")
    if candidate_names:
        message_lines.append("稳定命名候选=" + json.dumps(candidate_names, ensure_ascii=False))
    if name_assignments:
        message_lines.append("稳定映射:")
        message_lines.extend(build_name_assignment_prompt_lines(name_assignments, limit=10))
    if runtime_context.get("chain_jadx_named_dir"):
        message_lines.append("named_dir={chain_jadx_named_dir}")
    if runtime_context.get("chain_jadx_analysis_dir"):
        message_lines.append("analysis_dir={chain_jadx_analysis_dir}")
    renamed_dex = str(runtime_context.get("chain_renamed_dex", "") or "")
    if renamed_dex and os.path.exists(renamed_dex):
        message_lines.append("renamed_dex={chain_renamed_dex}")
        message_lines.append("如果 renamed_dex 已就绪，named JADX 这轮优先基于 renamed_dex 重新导出当前选中的调用链类，不要退回原始 target_dex。")
    bridge_preview = shorten_text("\n".join(message_lines), 520)
    bridge_targets = extract_targets(call_chain_text, limit=8)
    bridge_result = send_bridge_followup_message(
        client=client,
        conv_manager=conv_manager,
        call_chain_text=call_chain_text,
        route_item=route_item,
        profile="jadx_code_analyst",
        profile_reason="bridge:jadx_named_chain",
        command_type="jadx_named_decompile",
        toolchain_key="jadx_named_chain",
        message="\n".join(message_lines),
        focus_note=focus_note,
        toolchain_config=toolchain_config,
        bridge_target="jadx_named_chain",
        bridge_preview=bridge_preview,
        bridge_targets=bridge_targets,
        note="jadx_named_bridge",
    )
    if not bridge_result.get("reply_text"):
        return {}
    named_command_items = collect_command_items_from_text(str(bridge_result.get("reply_text", "") or ""))
    background_entry = remember_chain_background(
        call_chain_text,
        toolchain_key="jadx_named_chain",
        session_name=str(bridge_result.get("session_name", "") or ""),
        reply_text=str(bridge_result.get("reply_text", "") or ""),
        command_items=named_command_items,
        result=None,
        profile="jadx_code_analyst",
    )
    if background_entry.get("summary"):
        conv_manager.append_analysis_fragment(
            str(bridge_result.get("session_name", "") or ""),
            str(background_entry.get("summary", "") or ""),
            targets=list(background_entry.get("targets", [])),
        )
    return {
        "session_name": str(bridge_result.get("session_name", "") or ""),
        "conversation_id": str(bridge_result.get("conversation_id", "") or ""),
        "section_id": str(bridge_result.get("section_id", "") or ""),
        "route_entry": dict(bridge_result.get("route_entry", {}) or {}),
        "reply_text": str(bridge_result.get("reply_text", "") or ""),
        "candidate_names": candidate_names,
        "name_assignments": name_assignments,
        "chain_background_summary": str(background_entry.get("summary", "") or ""),
    }


def continue_toolchain_followups(client: "DoubaoClient",
                                 conv_manager: "ConversationManager",
                                 dispatch_payload: Dict[str, Any],
                                 command_routes: List[Dict[str, Any]],
                                 call_chain_text: str,
                                 toolchain_config: Dict[str, Any],
                                 fallback_profile: str,
                                 fallback_profile_reason: str,
                                 focus_note: str = "") -> List[Dict[str, Any]]:
    if not should_auto_followup_after_exec(toolchain_config):
        return []

    results = list(dispatch_payload.get("results", []))
    followups: List[Dict[str, Any]] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            continue
        route_item = resolve_command_route_for_result(index, result, command_routes, call_chain_text)
        profile = str(route_item.get("profile", "") or fallback_profile or DEFAULT_PROFILE)
        profile_reason = f"toolchain:{route_item.get('toolchain_key', '') or route_item.get('command_type', '') or route_item.get('tool', '')}"
        session_name = str(route_item.get("session_name", "") or "")
        if not session_name:
            base_name = base_chain_session_name(str((command_routes[:1] or [{}])[0].get("session_name", "") or "chain"))
            suffix = safe_slug(str(route_item.get("toolchain_key", "") or route_item.get("command_type", "") or profile))
            session_name = f"{base_name}__{suffix}" if suffix else base_name
        ensure_session_binding(
            conv_manager,
            session_name=session_name,
            profile=profile,
            profile_reason=profile_reason,
            group_ref=str(route_item.get("group_ref", "") or ""),
            slot_ref=str(route_item.get("slot_ref", "") or ""),
            conversation_id=str(route_item.get("conversation_id", "") or ""),
            section_id=str(route_item.get("section_id", "") or ""),
        )
        conv_manager.remember_local_runtime_block(
            session_name,
            build_local_runtime_result_block(result, route_item=route_item, call_chain_text=call_chain_text),
        )
        stored_session = dict(conv_manager.sessions.get(session_name, {}) or {})
        followup_message, feedback_kind = build_result_followup_message(result, route_item=route_item, call_chain_text=call_chain_text)
        if not followup_message:
            continue
        session_context = build_session_context_payload(
            stored_session=stored_session,
            active_route_entry=route_item,
            call_chain_text=call_chain_text,
            message=followup_message,
            profile=profile,
            profile_reason=profile_reason,
            focus_note=focus_note,
            toolchain_config=toolchain_config,
            force_refresh=not bool(stored_session.get("conversation_id")),
        )
        local_mode = resolve_local_chat_mode(profile, text=followup_message, feedback_kind=feedback_kind)
        original_state = (client.think_mode, client.use_auto_cot, client.use_deep_think)
        client.think_mode = int(local_mode["think_mode"])
        client.use_auto_cot = bool(local_mode["use_auto_cot"])
        client.use_deep_think = bool(local_mode["use_deep_think"])
        reply_text, _, new_conv_id, new_sec_id = client.send_message(
            followup_message,
            guest=False,
            conversation_id=str(stored_session.get("conversation_id", "") or ""),
            section_id=str(stored_session.get("section_id", "") or ""),
            stream=True,
            apply_initial_prompt=bool(not stored_session.get("conversation_id") or session_context.get("role_prompt_refresh_due")),
            forced_profile=profile,
            session_data=session_context,
            call_chain_text=call_chain_text,
            route_entry=route_item,
        )
        client.think_mode, client.use_auto_cot, client.use_deep_think = original_state

        ensure_session_binding(
            conv_manager,
            session_name=session_name,
            profile=profile,
            profile_reason=profile_reason,
            group_ref=str(route_item.get("group_ref", "") or ""),
            slot_ref=str(route_item.get("slot_ref", "") or ""),
            conversation_id=new_conv_id or str(stored_session.get("conversation_id", "") or ""),
            section_id=new_sec_id or str(stored_session.get("section_id", "") or ""),
        )
        conv_manager.remember_user_context(session_name, followup_message, profile, profile_reason)
        conv_manager.remember_assistant_reply(session_name, reply_text)
        if session_context.get("role_prompt_refresh_due"):
            conv_manager.mark_prompt_refresh(session_name, session_context.get("user_turn_count"))

        refreshed_session = dict(conv_manager.sessions.get(session_name, {}) or {})
        reply_text, candidate_names, regenerate_payload = maybe_regenerate_duplicate_names(
            client=client,
            conv_manager=conv_manager,
            session_name=session_name,
            stored_session=refreshed_session,
            route_item=route_item,
            call_chain_text=call_chain_text,
            reply_text=reply_text,
            profile=profile,
            profile_reason=profile_reason,
            focus_note=focus_note,
            toolchain_config=toolchain_config,
        )
        final_command_items = collect_command_items_from_text(reply_text)
        name_assignments = collect_naming_assignments(
            reply_text,
            final_command_items,
            call_chain_text=call_chain_text,
            route_item=route_item,
        )
        candidate_names = merge_candidate_bucket(
            candidate_names,
            bucket_from_name_assignments(name_assignments),
            limit=16,
        )
        background_entry = remember_chain_background(
            call_chain_text,
            toolchain_key=str(route_item.get("toolchain_key", "") or ""),
            session_name=session_name,
            reply_text=reply_text,
            command_items=final_command_items,
            result=result,
            profile=profile,
        )
        if background_entry.get("summary"):
            conv_manager.append_analysis_fragment(
                session_name,
                str(background_entry.get("summary", "") or ""),
                targets=list(background_entry.get("targets", [])),
            )

        updated_route = remember_role_takeover_route(
            call_chain_text,
            profile=profile,
            command_type=str(route_item.get("command_type", "") or ""),
            toolchain_key=str(route_item.get("toolchain_key", "") or ""),
            session_name=session_name,
            conversation_id=new_conv_id or str(stored_session.get("conversation_id", "") or ""),
            section_id=new_sec_id or str(stored_session.get("section_id", "") or ""),
            role_card=profile,
            tool=str(result.get("tool", "") or route_item.get("tool", "") or ""),
            toolchain_entry=str(route_item.get("tool", "") or ""),
            note="auto_exec_followup",
        )

        rename_bridge: Dict[str, Any] = {}
        if str(route_item.get("toolchain_key", "") or "") == "jadx_source_chain":
            rename_bridge = sync_jadx_result_to_rename_session(
                call_chain_text,
                route_item=updated_route or route_item,
                result=result,
                followup_reply=reply_text,
                background_summary=str(background_entry.get("summary", "") or ""),
                conv_manager=conv_manager,
                profile_reason=fallback_profile_reason,
                toolchain_config=toolchain_config,
            )

        tuning_bridge = maybe_launch_rename_tuning_bridge(
            client=client,
            conv_manager=conv_manager,
            route_item=updated_route or route_item,
            call_chain_text=call_chain_text,
            candidate_names=candidate_names,
            name_assignments=name_assignments or regenerate_payload.get("name_assignments", []),
            background_summary=str(background_entry.get("summary", "") or ""),
            focus_note=focus_note,
            toolchain_config=toolchain_config,
        )
        named_jadx_bridge: Dict[str, Any] = {}
        auto_smali_followup: Dict[str, Any] = {}
        tuning_command_items = collect_command_items_from_text(str(tuning_bridge.get("reply_text", "") or ""))
        stable_name_assignments = list(
            tuning_bridge.get("name_assignments", [])
            or name_assignments
            or regenerate_payload.get("name_assignments", [])
        )
        pending_smali_commands = collect_smali_mutation_commands(list(final_command_items) + tuning_command_items)
        if tuning_bridge.get("stable"):
            if (
                get_rename_apply_mode(toolchain_config) != "analysis_only"
                and not pending_smali_commands
                and not is_smali_roundtrip_result(updated_route or route_item, result)
            ):
                auto_smali_command_item = build_local_smali_apply_command_item(
                    call_chain_text,
                    stable_name_assignments,
                    config=toolchain_config,
                )
                if auto_smali_command_item:
                    pending_smali_commands = [auto_smali_command_item]
                    smali_route = resolve_role_takeover_route(
                        call_chain_text,
                        preferred_profile="tool_dispatch",
                        command_type="smali_conversion",
                        toolchain_key="smali_conversion_chain",
                    )
                    auto_smali_session_name = str(
                        smali_route.get("session_name", "")
                        or tuning_bridge.get("session_name", "")
                        or session_name
                    )
                    auto_smali_followup = {
                        "index": index,
                        "tool": "smali_converter",
                        "profile": "tool_dispatch",
                        "toolchain_key": "smali_conversion_chain",
                        "command_type": "smali_conversion",
                        "thread_group_ref": str(smali_route.get("group_ref", "") or ""),
                        "thread_slot_ref": str(smali_route.get("slot_ref", "") or ""),
                        "session_name": auto_smali_session_name,
                        "submodule_session_name": auto_smali_session_name,
                        "submodule_profile": "tool_dispatch",
                        "submodule_toolchain_key": "smali_conversion_chain",
                        "conversation_id": str(smali_route.get("conversation_id", "") or ""),
                        "section_id": str(smali_route.get("section_id", "") or ""),
                        "feedback_kind": "success",
                        "reply_text": json.dumps(auto_smali_command_item, ensure_ascii=False, indent=2),
                        "candidate_names": dict(tuning_bridge.get("candidate_names", {}) or {}),
                        "name_assignments": stable_name_assignments,
                        "chain_background_summary": "本地已根据稳定 naming_assignments 自动排入 smali apply-renames，等待 renamed_dex 完成后再触发 named JADX。",
                    }
            if pending_smali_commands:
                store_pending_named_jadx_state(
                    call_chain_text,
                    candidate_names=dict(tuning_bridge.get("candidate_names", {}) or {}),
                    name_assignments=stable_name_assignments,
                    background_summary=str(tuning_bridge.get("chain_background_summary", "") or background_entry.get("summary", "") or ""),
                    reason="await_smali_roundtrip",
                )
            elif should_launch_named_jadx_now(pending_smali_commands, config=toolchain_config, route_item=updated_route or route_item):
                named_jadx_bridge = maybe_launch_named_jadx_bridge(
                    client=client,
                    conv_manager=conv_manager,
                    route_item=dict(tuning_bridge.get("route_entry", {}) or {}),
                    call_chain_text=call_chain_text,
                    candidate_names=dict(tuning_bridge.get("candidate_names", {}) or {}),
                    name_assignments=stable_name_assignments,
                    background_summary=str(tuning_bridge.get("chain_background_summary", "") or background_entry.get("summary", "") or ""),
                    focus_note=focus_note,
                    toolchain_config=toolchain_config,
                )
                if named_jadx_bridge.get("reply_text"):
                    clear_pending_named_jadx_state(call_chain_text)
            else:
                store_pending_named_jadx_state(
                    call_chain_text,
                    candidate_names=dict(tuning_bridge.get("candidate_names", {}) or {}),
                    name_assignments=stable_name_assignments,
                    background_summary=str(tuning_bridge.get("chain_background_summary", "") or background_entry.get("summary", "") or ""),
                    reason="await_smali_roundtrip",
                )
        if not named_jadx_bridge:
            pending_named_jadx = resolve_pending_named_jadx_state(call_chain_text)
            if pending_named_jadx and is_smali_roundtrip_result(updated_route or route_item, result):
                pending_tuning_route = resolve_role_takeover_route(
                    call_chain_text,
                    preferred_profile="rename_tuning",
                    command_type="rename_tuning",
                    toolchain_key="rename_tuning_chain",
                )
                named_jadx_bridge = maybe_launch_named_jadx_bridge(
                    client=client,
                    conv_manager=conv_manager,
                    route_item=dict(pending_tuning_route or {}),
                    call_chain_text=call_chain_text,
                    candidate_names=dict(pending_named_jadx.get("candidate_names", {}) or {}),
                    name_assignments=list(pending_named_jadx.get("name_assignments", []) or []),
                    background_summary=str(pending_named_jadx.get("background_summary", "") or background_entry.get("summary", "") or ""),
                    focus_note=focus_note,
                    toolchain_config=toolchain_config,
                )
                if named_jadx_bridge.get("reply_text"):
                    clear_pending_named_jadx_state(call_chain_text)

        followups.append({
            "index": index,
            "tool": str(result.get("tool", "") or ""),
            "profile": profile,
            "toolchain_key": str(route_item.get("toolchain_key", "") or ""),
            "command_type": str(route_item.get("command_type", "") or ""),
            "thread_group_ref": str((updated_route or route_item).get("group_ref", "") or ""),
            "thread_slot_ref": str((updated_route or route_item).get("slot_ref", "") or ""),
            "session_name": session_name,
            "submodule_session_name": session_name,
            "submodule_profile": profile,
            "submodule_toolchain_key": str(route_item.get("toolchain_key", "") or ""),
            "conversation_id": new_conv_id or str(stored_session.get("conversation_id", "") or ""),
            "section_id": new_sec_id or str(stored_session.get("section_id", "") or ""),
            "feedback_kind": feedback_kind,
            "reply_text": reply_text,
            "candidate_names": candidate_names,
            "name_assignments": name_assignments or regenerate_payload.get("name_assignments", []),
            "chain_background_summary": str(background_entry.get("summary", "") or ""),
            "chain_name_regenerate": regenerate_payload,
            "rename_bridge": rename_bridge,
            "rename_tuning_bridge": tuning_bridge,
            "named_jadx_bridge": named_jadx_bridge,
        })
        if tuning_bridge.get("reply_text"):
            tuning_route = dict(tuning_bridge.get("route_entry", {}) or {})
            followups.append({
                "index": index,
                "tool": "rename_tuning",
                "profile": "rename_tuning",
                "toolchain_key": "rename_tuning_chain",
                "command_type": "rename_tuning",
                "thread_group_ref": str(tuning_route.get("group_ref", "") or ""),
                "thread_slot_ref": str(tuning_route.get("slot_ref", "") or ""),
                "session_name": str(tuning_bridge.get("session_name", "") or ""),
                "submodule_session_name": str(tuning_bridge.get("session_name", "") or ""),
                "submodule_profile": "rename_tuning",
                "submodule_toolchain_key": "rename_tuning_chain",
                "conversation_id": str(tuning_bridge.get("conversation_id", "") or ""),
                "section_id": str(tuning_bridge.get("section_id", "") or ""),
                "feedback_kind": "success",
                "reply_text": str(tuning_bridge.get("reply_text", "") or ""),
                "candidate_names": dict(tuning_bridge.get("candidate_names", {}) or {}),
                "name_assignments": list(tuning_bridge.get("name_assignments", []) or []),
                "chain_background_summary": str(tuning_bridge.get("chain_background_summary", "") or ""),
                "chain_name_regenerate": {
                    "requested": True,
                    "remaining_conflicts": dict(tuning_bridge.get("remaining_conflicts", {}) or {}),
                },
            })
        if auto_smali_followup:
            followups.append(auto_smali_followup)
        if named_jadx_bridge.get("reply_text"):
            named_route = dict(named_jadx_bridge.get("route_entry", {}) or {})
            followups.append({
                "index": index,
                "tool": "jadx_named_bridge",
                "profile": "jadx_code_analyst",
                "toolchain_key": "jadx_named_chain",
                "command_type": "jadx_named_decompile",
                "thread_group_ref": str(named_route.get("group_ref", "") or ""),
                "thread_slot_ref": str(named_route.get("slot_ref", "") or ""),
                "session_name": str(named_jadx_bridge.get("session_name", "") or ""),
                "submodule_session_name": str(named_jadx_bridge.get("session_name", "") or ""),
                "submodule_profile": "jadx_code_analyst",
                "submodule_toolchain_key": "jadx_named_chain",
                "conversation_id": str(named_jadx_bridge.get("conversation_id", "") or ""),
                "section_id": str(named_jadx_bridge.get("section_id", "") or ""),
                "feedback_kind": "success",
                "reply_text": str(named_jadx_bridge.get("reply_text", "") or ""),
                "candidate_names": dict(named_jadx_bridge.get("candidate_names", {}) or {}),
                "name_assignments": list(named_jadx_bridge.get("name_assignments", []) or []),
                "chain_background_summary": str(named_jadx_bridge.get("chain_background_summary", "") or ""),
            })
    return followups


def collect_next_command_batch_from_followups(call_chain_text: str,
                                              followups: List[Dict[str, Any]],
                                              separate_toolchain_conversations: bool,
                                              toolchain_config: Optional[Dict[str, Any]] = None,
                                              completed_signatures: Optional[set[str]] = None,
                                              tools: Optional[Dict[str, str]] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    next_items: List[Dict[str, Any]] = []
    next_routes: List[Dict[str, Any]] = []
    skipped_items: List[Dict[str, Any]] = []
    batch_signatures: set[str] = set()
    for followup in followups:
        if not isinstance(followup, dict):
            continue
        reply_text = str(followup.get("reply_text", "") or "")
        command_items = collect_command_items_from_text(reply_text)
        if not command_items:
            continue
        base_session_name = str(
            followup.get("submodule_session_name", "")
            or followup.get("session_name", "")
            or ""
        )
        remembered = remember_command_routes(
            call_chain_text,
            command_items,
            base_session_name=base_session_name,
            conversation_id=str(followup.get("conversation_id", "") or ""),
            section_id=str(followup.get("section_id", "") or ""),
            separate_toolchain_conversations=separate_toolchain_conversations,
            toolchain_config=toolchain_config,
        )
        filtered_items, filtered_routes, filtered_skipped = filter_loop_duplicate_commands(
            command_items,
            remembered,
            completed_signatures=completed_signatures,
            batch_signatures=batch_signatures,
            config=toolchain_config,
            tools=tools,
            call_chain_text=call_chain_text,
        )
        next_items.extend(filtered_items)
        next_routes.extend(filtered_routes)
        skipped_items.extend(filtered_skipped)
    return next_items, next_routes, skipped_items


def continue_main_thread_after_children(client: "DoubaoClient",
                                        conv_manager: "ConversationManager",
                                        followups: List[Dict[str, Any]],
                                        call_chain_text: str,
                                        fallback_profile: str,
                                        fallback_profile_reason: str,
                                        toolchain_config: Dict[str, Any],
                                        focus_note: str = "") -> Dict[str, Any]:
    child_followups = [
        item for item in followups
        if isinstance(item, dict)
        and str(item.get("toolchain_key", "") or "") != "chain_root"
        and str(item.get("reply_text", "") or "").strip()
    ]
    if not child_followups:
        return {}

    root_route = resolve_role_takeover_route(
        call_chain_text,
        preferred_profile=fallback_profile or DEFAULT_PROFILE,
        command_type="chain_root",
        toolchain_key="chain_root",
    )
    session_name = str(root_route.get("session_name", "") or build_chain_session_name(call_chain_text))
    ensure_session_binding(
        conv_manager,
        session_name=session_name,
        profile=fallback_profile or DEFAULT_PROFILE,
        profile_reason=fallback_profile_reason,
        group_ref=str(root_route.get("group_ref", "") or ""),
        slot_ref=str(root_route.get("slot_ref", "") or ""),
        conversation_id=str(root_route.get("conversation_id", "") or ""),
        section_id=str(root_route.get("section_id", "") or ""),
    )

    stored_session = dict(conv_manager.sessions.get(session_name, {}) or {})
    summary_lines = [
        "子线程本轮已完成，请主线程基于子线程整理后的结果继续当前调用链，不要切到其他目录。",
        f"调用链={call_chain_text}",
    ]
    for item in child_followups[:3]:
        summary_lines.append(f"子线程={item.get('toolchain_key', '') or item.get('profile', '')}")
        summary_lines.append(
            f"整理结果={shorten_text(str(item.get('chain_background_summary', '') or item.get('reply_text', '') or ''), 1200)}"
        )
        rename_bridge = item.get("rename_bridge", {}) if isinstance(item.get("rename_bridge", {}), dict) else {}
        if rename_bridge.get("session_name"):
            summary_lines.append(f"已桥接到重命名会话={rename_bridge.get('session_name')}")
    summary_lines.append("请继续输出下一批命令JSON，优先处理含义、混淆关系和后续依赖。")
    summary_lines.append("不要回显 conversation_id、section_id、message_id 等中间标识。")
    summary_message = "\n".join(summary_lines)

    session_context = build_session_context_payload(
        stored_session=stored_session,
        active_route_entry=root_route,
        call_chain_text=call_chain_text,
        message=summary_message,
        profile=fallback_profile or DEFAULT_PROFILE,
        profile_reason=fallback_profile_reason,
        focus_note=focus_note,
        toolchain_config=toolchain_config,
        force_refresh=not bool(stored_session.get("conversation_id")),
    )
    local_mode = resolve_local_chat_mode(fallback_profile or DEFAULT_PROFILE, text=summary_message, feedback_kind="success")
    original_state = (client.think_mode, client.use_auto_cot, client.use_deep_think)
    client.think_mode = int(local_mode["think_mode"])
    client.use_auto_cot = bool(local_mode["use_auto_cot"])
    client.use_deep_think = bool(local_mode["use_deep_think"])
    reply_text, _, new_conv_id, new_sec_id = client.send_message(
        summary_message,
        guest=False,
        conversation_id=str(stored_session.get("conversation_id", "") or ""),
        section_id=str(stored_session.get("section_id", "") or ""),
        stream=True,
        apply_initial_prompt=bool(not stored_session.get("conversation_id") or session_context.get("role_prompt_refresh_due")),
        forced_profile=fallback_profile or DEFAULT_PROFILE,
        session_data=session_context,
        call_chain_text=call_chain_text,
        route_entry=root_route,
    )
    client.think_mode, client.use_auto_cot, client.use_deep_think = original_state

    ensure_session_binding(
        conv_manager,
        session_name=session_name,
        profile=fallback_profile or DEFAULT_PROFILE,
        profile_reason=fallback_profile_reason,
        group_ref=str(root_route.get("group_ref", "") or ""),
        slot_ref=str(root_route.get("slot_ref", "") or ""),
        conversation_id=new_conv_id or str(stored_session.get("conversation_id", "") or ""),
        section_id=new_sec_id or str(stored_session.get("section_id", "") or ""),
    )
    conv_manager.remember_user_context(session_name, summary_message, fallback_profile or DEFAULT_PROFILE, fallback_profile_reason)
    conv_manager.remember_assistant_reply(session_name, reply_text)
    if session_context.get("role_prompt_refresh_due"):
        conv_manager.mark_prompt_refresh(session_name, session_context.get("user_turn_count"))

    root_commands = collect_command_items_from_text(reply_text)
    root_background = remember_chain_background(
        call_chain_text,
        toolchain_key="chain_root",
        session_name=session_name,
        reply_text=reply_text,
        command_items=root_commands,
        result=None,
        profile=fallback_profile or DEFAULT_PROFILE,
    )
    if root_background.get("summary"):
        conv_manager.append_analysis_fragment(
            session_name,
            str(root_background.get("summary", "") or ""),
            targets=list(root_background.get("targets", [])),
        )
    root_routes = remember_command_routes(
        call_chain_text,
        root_commands,
        base_session_name=session_name,
        conversation_id=new_conv_id or str(stored_session.get("conversation_id", "") or ""),
        section_id=new_sec_id or str(stored_session.get("section_id", "") or ""),
        separate_toolchain_conversations=use_toolchain_separate_conversations(toolchain_config, False),
        toolchain_config=toolchain_config,
    ) if root_commands else []

    return {
        "session_name": session_name,
        "reply_text": reply_text,
        "command_items": root_commands,
        "command_routes": root_routes,
    }


def get_auto_followup_max_rounds(config: Optional[Dict[str, Any]] = None) -> int:
    try:
        rounds = int((config or {}).get("auto_followup_max_rounds", 8) or 8)
    except Exception:
        rounds = 8
    return max(1, min(rounds, 20))


def run_recursive_toolchain_loop(client: "DoubaoClient",
                                 conv_manager: "ConversationManager",
                                 initial_command_items: List[Dict[str, Any]],
                                 initial_command_routes: List[Dict[str, Any]],
                                 call_chain_text: str,
                                 toolchain_config: Dict[str, Any],
                                 separate_toolchain_conversations: bool,
                                 fallback_profile: str,
                                 fallback_profile_reason: str,
                                 focus_note: str = "",
                                 output_path: str = "",
                                 stop_on_error: bool = False) -> Dict[str, Any]:
    tools = discover_tool_context(toolchain_config)
    chain_runtime = ensure_call_chain_runtime_dirs(call_chain_text, config=toolchain_config) if call_chain_text else {}
    pending_items = list(initial_command_items)
    pending_routes = list(initial_command_routes)
    rounds: List[Dict[str, Any]] = []
    max_rounds = get_auto_followup_max_rounds(toolchain_config)
    completed_signatures: set[str] = set()

    for round_index in range(1, max_rounds + 1):
        if not pending_items:
            break

        default_round_output = ""
        if chain_runtime:
            default_round_output = os.path.join(
                str(chain_runtime.get("chain_dispatch_dir", "") or ""),
                f"dispatch_round_{round_index:02d}.json",
            )
        round_output_path = output_path if round_index == 1 and output_path else default_round_output
        dispatch_ok, dispatch_text, dispatch_payload, approved_items = run_reviewed_dispatch(
            pending_items,
            toolchain_config,
            tools=tools,
            call_chain_text=call_chain_text,
            output_path=round_output_path,
            stop_on_error=stop_on_error,
        )
        followups = continue_toolchain_followups(
            client=client,
            conv_manager=conv_manager,
            dispatch_payload=dispatch_payload,
            command_routes=pending_routes,
            call_chain_text=call_chain_text,
            toolchain_config=toolchain_config,
            fallback_profile=fallback_profile,
            fallback_profile_reason=fallback_profile_reason,
            focus_note=focus_note,
        ) if call_chain_text else []
        for result in list(dispatch_payload.get("results", [])) if isinstance(dispatch_payload.get("results", []), list) else []:
            if not isinstance(result, dict):
                continue
            if not bool(result.get("ok")) or int(result.get("judge_status", 1) or 1) != 0:
                continue
            signature = build_loop_command_signature(
                result,
                config=toolchain_config,
                tools=tools,
                call_chain_text=call_chain_text,
            )
            if signature:
                completed_signatures.add(signature)
        next_items, next_routes, duplicate_skips = collect_next_command_batch_from_followups(
            call_chain_text,
            followups,
            separate_toolchain_conversations=separate_toolchain_conversations,
            toolchain_config=toolchain_config,
            completed_signatures=completed_signatures,
            tools=tools,
        ) if call_chain_text else ([], [], [])
        root_resume: Dict[str, Any] = {}
        if call_chain_text and not next_items:
            root_resume = continue_main_thread_after_children(
                client=client,
                conv_manager=conv_manager,
                followups=followups,
                call_chain_text=call_chain_text,
                fallback_profile=fallback_profile,
                fallback_profile_reason=fallback_profile_reason,
                toolchain_config=toolchain_config,
                focus_note=focus_note,
            )
            next_items, next_routes, root_duplicate_skips = filter_loop_duplicate_commands(
                list(root_resume.get("command_items", [])),
                list(root_resume.get("command_routes", [])),
                completed_signatures=completed_signatures,
                config=toolchain_config,
                tools=tools,
                call_chain_text=call_chain_text,
            )
            duplicate_skips.extend(root_duplicate_skips)
            root_resume["command_items"] = list(next_items)
            root_resume["command_routes"] = list(next_routes)
            if root_duplicate_skips:
                root_resume["duplicate_skips"] = root_duplicate_skips

        if call_chain_text:
            update_call_chain_index(
                call_chain_text,
                toolchain_config,
                round_index=round_index,
                dispatch_payload=dispatch_payload,
                command_routes=pending_routes,
                followups=followups,
                root_resume=root_resume,
            )

        rounds.append({
            "round": round_index,
            "dispatch_ok": dispatch_ok,
            "approved_count": len(approved_items),
            "review": dict(dispatch_payload.get("review", {}) or {}),
            "dispatch_payload": dispatch_payload,
            "dispatch_text": dispatch_text,
            "followups": followups,
            "root_resume": root_resume,
            "next_command_count": len(next_items),
            "duplicate_skip_count": len(duplicate_skips),
            "duplicate_skips": duplicate_skips[:8],
            "next_submodule_routes": [
                {
                    "session_name": str(item.get("session_name", "") or ""),
                    "toolchain_key": str(item.get("toolchain_key", "") or ""),
                    "profile": str(item.get("profile", "") or ""),
                    "slot_ref": str(item.get("slot_ref", "") or ""),
                }
                for item in next_routes[:8]
                if isinstance(item, dict)
            ],
        })

        pending_items = next_items
        pending_routes = next_routes
        if not next_items:
            break

    exhausted = not pending_items
    loop_payload = {
        "rounds": rounds,
        "round_count": len(rounds),
        "exhausted": exhausted,
        "max_rounds": max_rounds,
        "stopped_reason": "command_list_exhausted" if exhausted else "max_rounds_reached",
    }
    if exhausted:
        loop_payload["next_step_hint"] = "当前调用链已收束；下一轮可手动传入新的 --call_chain 或 --call_chain_file 切到下一条链继续分析。"
    if call_chain_text:
        index_context = update_call_chain_index(
            call_chain_text,
            toolchain_config,
            loop_state=loop_payload,
        )
        if index_context:
            loop_payload["chain_index_file"] = str(index_context.get("chain_index_file", "") or "")
            loop_payload["chain_work_dir"] = str(index_context.get("chain_work_dir", "") or "")
    return loop_payload


# ---------- 会话管理器 ----------
class ConversationManager:
    """管理多个对话的标识（conversation_id, section_id）"""

    def __init__(self, storage_file=SESSION_FILE):
        self.storage_file = storage_file
        self.sessions = {}          # name -> session metadata
        self.current_session = None
        self.load()

    def _default_session(self, conversation_id: str = "", section_id: str = "") -> Dict[str, Any]:
        now = time.time()
        return {
            "conversation_id": conversation_id or "",
            "section_id": section_id or "",
            "profile": DEFAULT_PROFILE,
            "profile_reason": "",
            "thread_group_ref": "",
            "thread_slot_ref": "",
            "primary_goal": "",
            "attention_notes": [],
            "recent_targets": [],
            "recent_commands": [],
            "analysis_fragments": [],
            "local_runtime_blocks": [],
            "class_stats": {},
            "visible_messages": [],
            "user_turn_count": 0,
            "assistant_turn_count": 0,
            "last_prompt_refresh_user_turn": 0,
            "created_at": now,
            "updated_at": now,
        }

    def _normalize_session(self, raw_session: Any) -> Dict[str, Any]:
        if isinstance(raw_session, tuple):
            session = self._default_session(raw_session[0] if len(raw_session) > 0 else "",
                                            raw_session[1] if len(raw_session) > 1 else "")
        elif isinstance(raw_session, dict):
            session = self._default_session(raw_session.get("conversation_id", ""), raw_session.get("section_id", ""))
            session.update(raw_session)
        else:
            session = self._default_session()

        session["attention_notes"] = unique_keep_order(list(session.get("attention_notes", [])), limit=4)
        session["recent_targets"] = unique_keep_order(list(session.get("recent_targets", [])), limit=8)
        session["recent_commands"] = unique_keep_order(list(session.get("recent_commands", [])), limit=6)
        session["analysis_fragments"] = unique_keep_order(
            [
                cleaned for cleaned in
                (sanitize_analysis_fragment(str(item)) for item in session.get("analysis_fragments", []))
                if cleaned
            ],
            limit=6,
        )
        local_runtime_blocks: List[Dict[str, Any]] = []
        seen_runtime_blocks: set[str] = set()
        for item in session.get("local_runtime_blocks", []):
            normalized_block = normalize_local_runtime_block(item)
            if not normalized_block:
                continue
            signature = json.dumps(normalized_block, ensure_ascii=True, sort_keys=True)
            if signature in seen_runtime_blocks:
                continue
            seen_runtime_blocks.add(signature)
            local_runtime_blocks.append(normalized_block)
            if len(local_runtime_blocks) >= 6:
                break
        session["local_runtime_blocks"] = local_runtime_blocks
        class_stats = session.get("class_stats", {})
        session["class_stats"] = class_stats if isinstance(class_stats, dict) else {}
        session["visible_messages"] = list(session.get("visible_messages", []))
        try:
            session["user_turn_count"] = int(session.get("user_turn_count", 0) or 0)
        except Exception:
            session["user_turn_count"] = 0
        try:
            session["assistant_turn_count"] = int(session.get("assistant_turn_count", 0) or 0)
        except Exception:
            session["assistant_turn_count"] = 0
        try:
            session["last_prompt_refresh_user_turn"] = int(session.get("last_prompt_refresh_user_turn", 0) or 0)
        except Exception:
            session["last_prompt_refresh_user_turn"] = 0
        session["updated_at"] = session.get("updated_at", time.time())
        session["created_at"] = session.get("created_at", session["updated_at"])
        if session.get("profile") not in PROMPT_PROFILES:
            session["profile"] = DEFAULT_PROFILE
        return session

    def load(self):
        if os.path.exists(self.storage_file):
            try:
                with open(self.storage_file, 'rb') as f:
                    data = pickle.load(f)
                    raw_sessions = data.get('sessions', {})
                    self.sessions = {
                        name: self._normalize_session(session)
                        for name, session in raw_sessions.items()
                    }
                    self.current_session = data.get('current_session')
            except EOFError:
                self.sessions = {}
                self.current_session = None
            except Exception as e:
                print(f"加载会话文件失败: {e}")

    def save(self):
        try:
            with open(self.storage_file, 'wb') as f:
                pickle.dump({'sessions': self.sessions, 'current_session': self.current_session}, f)
        except Exception as e:
            print(f"保存会话文件失败: {e}")

    def create(self, name: str, conversation_id: str, section_id: str,
               profile: str = DEFAULT_PROFILE, profile_reason: str = ""):
        session = self._default_session(conversation_id, section_id)
        session["profile"] = profile if profile in PROMPT_PROFILES else DEFAULT_PROFILE
        session["profile_reason"] = profile_reason
        self.sessions[name] = session
        self.save()

    def list(self) -> List[str]:
        return list(self.sessions.keys())

    def switch(self, name: str) -> Tuple[str, str]:
        if name not in self.sessions:
            raise ValueError(f"会话 '{name}' 不存在")
        self.current_session = name
        self.save()
        session = self.sessions[name]
        return session.get("conversation_id", ""), session.get("section_id", "")

    def current(self) -> Optional[Tuple[str, str]]:
        if self.current_session is None:
            return None
        session = self.sessions.get(self.current_session)
        if not session:
            return None
        return session.get("conversation_id", ""), session.get("section_id", "")

    def current_data(self) -> Optional[Dict[str, Any]]:
        if self.current_session is None:
            return None
        return self.sessions.get(self.current_session)

    def delete(self, name: str):
        if name in self.sessions:
            del self.sessions[name]
            if self.current_session == name:
                self.current_session = None
            self.save()

    def rename(self, old_name: str, new_name: str):
        if old_name not in self.sessions:
            return
        if new_name in self.sessions:
            return
        self.sessions[new_name] = self.sessions.pop(old_name)
        if self.current_session == old_name:
            self.current_session = new_name
        self.save()

    def update_ids(self, name: str, conversation_id: str, section_id: str):
        if name not in self.sessions:
            return
        session = self.sessions[name]
        session["conversation_id"] = conversation_id or session.get("conversation_id", "")
        session["section_id"] = section_id or session.get("section_id", "")
        session["updated_at"] = time.time()
        self.save()

    def set_profile(self, name: str, profile: str, reason: str = ""):
        if name not in self.sessions or profile not in PROMPT_PROFILES:
            return
        session = self.sessions[name]
        session["profile"] = profile
        if reason:
            session["profile_reason"] = reason
        session["updated_at"] = time.time()
        self.save()

    def set_thread_refs(self, name: str, group_ref: str = "", slot_ref: str = ""):
        if name not in self.sessions:
            return
        session = self.sessions[name]
        if group_ref:
            session["thread_group_ref"] = group_ref
        if slot_ref:
            session["thread_slot_ref"] = slot_ref
        session["updated_at"] = time.time()
        self.save()

    def append_visible_message(self, name: str, sender: str, text: str, is_user: bool):
        if name not in self.sessions:
            return
        session = self.sessions[name]
        session["visible_messages"].append({
            "sender": sender,
            "text": text,
            "is_user": is_user,
            "ts": time.time(),
        })
        session["visible_messages"] = session["visible_messages"][-200:]
        session["updated_at"] = time.time()
        self.save()

    def get_visible_messages(self, name: str) -> List[Dict[str, Any]]:
        if name not in self.sessions:
            return []
        return list(self.sessions[name].get("visible_messages", []))

    def remember_user_context(self, name: str, user_message: str, profile: str, profile_reason: str = ""):
        if name not in self.sessions:
            return
        session = self.sessions[name]
        if profile in PROMPT_PROFILES:
            session["profile"] = profile
        if profile_reason:
            session["profile_reason"] = profile_reason
        if user_message and user_message != DEFAULT_INIT_MSG and not session.get("primary_goal"):
            session["primary_goal"] = shorten_text(user_message, 160)
        session["attention_notes"] = unique_keep_order(
            session.get("attention_notes", []) + extract_attention_notes(user_message),
            limit=4,
        )
        session["recent_targets"] = unique_keep_order(
            session.get("recent_targets", []) + extract_targets(user_message),
            limit=8,
        )
        session["class_stats"] = merge_class_stats(session.get("class_stats", {}), extract_class_targets(user_message, limit=12))
        session["user_turn_count"] = int(session.get("user_turn_count", 0) or 0) + 1
        session["updated_at"] = time.time()
        self.save()

    def remember_assistant_reply(self, name: str, reply_text: str):
        if name not in self.sessions or not reply_text:
            return
        if is_transport_shell_text(reply_text):
            return
        session = self.sessions[name]
        targets = extract_targets(reply_text, limit=4)
        if targets:
            session["recent_targets"] = unique_keep_order(session.get("recent_targets", []) + targets, limit=8)
        session["class_stats"] = merge_class_stats(session.get("class_stats", {}), extract_class_targets(reply_text, limit=12))

        command_json = normalize_command_json(reply_text)
        command_marks: List[str] = []
        if command_json:
            try:
                parsed = json.loads(command_json)
                items = parsed if isinstance(parsed, list) else [parsed]
                for item in items[:3]:
                    if not isinstance(item, dict):
                        continue
                    tool = shorten_text(str(item.get("tool", "")), 32)
                    command = shorten_text(str(item.get("command", "")), 72)
                    if tool or command:
                        command_marks.append(f"{tool}:{command}")
                    hit_class = normalize_class_key(str(item.get("hit_class", "")))
                    if hit_class:
                        session["recent_targets"] = unique_keep_order(
                            session.get("recent_targets", []) + [hit_class],
                            limit=8,
                        )
                        record_class_hit(
                            class_key=hit_class,
                            source_path=str(item.get("hit_store", "")),
                            fragment=str(item.get("store_back", "")),
                            tool=tool or "assistant_plan",
                            command=str(item.get("command", "")),
                            note="assistant_plan",
                        )
            except json.JSONDecodeError:
                command_marks.append(shorten_text(command_json, 96))

        if command_marks:
            session["recent_commands"] = unique_keep_order(session.get("recent_commands", []) + command_marks, limit=6)
        else:
            fragment = sanitize_analysis_fragment(reply_text)
            if fragment:
                session["analysis_fragments"] = unique_keep_order(
                    session.get("analysis_fragments", []) + [fragment],
                    limit=6,
                )
        session["assistant_turn_count"] = int(session.get("assistant_turn_count", 0) or 0) + 1
        session["updated_at"] = time.time()
        self.save()

    def mark_prompt_refresh(self, name: str, user_turn_count: Optional[int] = None):
        if name not in self.sessions:
            return
        session = self.sessions[name]
        if user_turn_count is None:
            user_turn_count = int(session.get("user_turn_count", 0) or 0)
        session["last_prompt_refresh_user_turn"] = int(user_turn_count or 0)
        session["updated_at"] = time.time()
        self.save()

    def append_analysis_fragment(self, name: str, fragment: str, targets: Optional[List[str]] = None):
        if name not in self.sessions:
            return
        cleaned = sanitize_analysis_fragment(fragment, limit=220)
        session = self.sessions[name]
        if cleaned:
            session["analysis_fragments"] = unique_keep_order(
                list(session.get("analysis_fragments", [])) + [cleaned],
                limit=8,
            )
        if targets:
            normalized_targets = [target for target in (normalize_class_key(item) for item in targets) if target]
            if normalized_targets:
                session["recent_targets"] = unique_keep_order(
                    list(session.get("recent_targets", [])) + normalized_targets,
                    limit=10,
                )
                session["class_stats"] = merge_class_stats(session.get("class_stats", {}), normalized_targets)
        session["updated_at"] = time.time()
        self.save()

    def remember_local_runtime_block(self, name: str, runtime_block: Optional[Dict[str, Any]]):
        if name not in self.sessions:
            return
        block = normalize_local_runtime_block(runtime_block)
        if not block:
            return
        session = self.sessions[name]
        existing = list(session.get("local_runtime_blocks", []))
        signatures: List[str] = []
        normalized_blocks: List[Dict[str, Any]] = []
        block_signature = json.dumps(block, ensure_ascii=True, sort_keys=True)
        for item in existing:
            normalized_item = normalize_local_runtime_block(item)
            if not normalized_item:
                continue
            signature = json.dumps(normalized_item, ensure_ascii=True, sort_keys=True)
            if signature == block_signature or signature in signatures:
                continue
            signatures.append(signature)
            normalized_blocks.append(normalized_item)
        session["local_runtime_blocks"] = [block] + normalized_blocks[:5]
        session["updated_at"] = time.time()
        self.save()


# ---------- API 客户端 ----------
class DoubaoClient:
    def __init__(self, base_url: str = BASE_URL,
                 use_auto_cot: bool = False, use_deep_think: bool = False,
                 think_mode: Optional[int] = None):
        self.base_url = base_url
        self.chat_endpoint = f"{base_url}/api/chat/completions"
        self.chat_endpoint_new = f"{self.chat_endpoint}/new"
        self.chat_endpoint_continue = f"{self.chat_endpoint}/continue"
        self.use_auto_cot = use_auto_cot
        self.use_deep_think = use_deep_think
        self.think_mode = think_mode
        self.request_timeout_sec = get_chat_request_timeout_sec()
        self.connect_timeout_sec = get_chat_connect_timeout_sec()
        self.request_timeout = get_chat_request_timeout_tuple()
        self.last_stream_meta: Dict[str, Any] = {}

    @staticmethod
    def _normalize_id(value: Optional[str]) -> str:
        normalized = str(value or "").strip()
        if not normalized or normalized == "0":
            return ""
        return normalized

    def _resolve_conversation_mode(self, conversation_id: Optional[str]) -> str:
        return "continue" if self._normalize_id(conversation_id) else "new"

    def _resolve_chat_endpoint(self, conversation_id: Optional[str]) -> str:
        return self.chat_endpoint_continue if self._resolve_conversation_mode(conversation_id) == "continue" else self.chat_endpoint_new

    def _post_chat_request(self, endpoint: str, payload: Dict[str, Any], stream: bool):
        resp = requests.post(endpoint, json=payload, stream=stream, timeout=self.request_timeout)
        if not resp.encoding or resp.encoding.lower() in {"iso-8859-1", "us-ascii"}:
            resp.encoding = "utf-8"
        return resp

    @staticmethod
    def _extract_detail_payload(response) -> Dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        detail = payload.get("detail")
        if isinstance(detail, dict):
            normalized = dict(detail)
            normalized["_status"] = response.status_code
            return normalized
        return payload

    def _format_error_message(self, response) -> str:
        detail_payload = self._extract_detail_payload(response)
        if detail_payload:
            message = str(
                detail_payload.get("message")
                or detail_payload.get("detail")
                or detail_payload.get("code")
                or ""
            ).strip()
            code = str(detail_payload.get("code") or "").strip()
            error_code = str(detail_payload.get("error_code") or "").strip()
            open_url = str(detail_payload.get("open_url") or "").strip()
            parts = []
            if message:
                parts.append(message)
            elif code:
                parts.append(code)
            if code and code not in message:
                parts.append(f"[code={code}]")
            if error_code:
                parts.append(f"[error_code={error_code}]")
            if open_url:
                parts.append(f"[open_url={open_url}]")
            if parts:
                return " ".join(parts)
        raw_body = (response.text or "").strip()
        return raw_body or f"HTTP {response.status_code}"

    def _build_payload(self, prompt: str, guest: bool,
                       conversation_id: Optional[str] = None,
                       section_id: Optional[str] = None) -> Dict[str, Any]:
        normalized_conversation_id = self._normalize_id(conversation_id)
        normalized_section_id = self._normalize_id(section_id)
        payload = {
            "prompt": prompt,
            "attachments": [],
            "use_auto_cot": self.use_auto_cot,
            "use_deep_think": self.use_deep_think,
            "guest": guest,
            "session_mode": "guest" if guest else "auth",
            "conversation_mode": self._resolve_conversation_mode(normalized_conversation_id),
        }
        if self.think_mode is not None:
            payload["think_mode"] = self.think_mode
        if normalized_conversation_id:
            payload["conversation_id"] = normalized_conversation_id
        if normalized_section_id:
            payload["section_id"] = normalized_section_id
        return payload

    def _parse_stream(self, response, on_text=None):
        full_text = []
        images = []
        conv_id = None
        sec_id = None
        last_snapshot_text = ""
        raw_events: List[str] = []
        event_types: List[str] = []

        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            raw_events.append(data_str)
            if data_str == "[DONE]":
                break

            try:
                outer = json.loads(data_str)
            except json.JSONDecodeError:
                if on_text:
                    on_text(data_str)
                full_text.append(data_str)
                continue

            event_data_raw = outer.get("event_data", "")
            event_data = decode_jsonish(event_data_raw)
            if not isinstance(event_data, dict):
                event_data = outer if isinstance(outer, dict) else {}

            conv_id = first_nonempty(
                conv_id,
                outer.get("conversation_id", "") if isinstance(outer, dict) else "",
                event_data.get("conversation_id", ""),
                event_data.get("conversation", {}).get("conversation_id", "") if isinstance(event_data.get("conversation"), dict) else "",
                event_data.get("message", {}).get("conversation_id", "") if isinstance(event_data.get("message"), dict) else "",
            )
            sec_id = first_nonempty(
                sec_id,
                outer.get("section_id", "") if isinstance(outer, dict) else "",
                event_data.get("section_id", ""),
                event_data.get("conversation", {}).get("section_id", "") if isinstance(event_data.get("conversation"), dict) else "",
                event_data.get("message", {}).get("section_id", "") if isinstance(event_data.get("message"), dict) else "",
            )

            for image in collect_images_from_event(event_data):
                if image and image not in images:
                    images.append(image)

            assembled_text = "".join(full_text)
            snapshot_text = extract_snapshot_text_from_event(event_data)
            if snapshot_text and len(snapshot_text) >= len(last_snapshot_text):
                last_snapshot_text = snapshot_text
            text_piece, is_snapshot = extract_text_piece_from_event(event_data, assembled_text)
            if text_piece:
                if on_text:
                    on_text(text_piece)
                if is_snapshot and assembled_text and not text_piece:
                    pass
                else:
                    full_text.append(text_piece)

            event_type = str(event_data.get("event_type", outer.get("event_type", "")))
            if event_type:
                event_types.append(event_type)
            if event_data.get("is_finish", False) or event_type in {"2002", "finish", "done"}:
                break

        final_text = sanitize_reply_text_for_runtime("".join(full_text))
        reply_source = "stream_text" if final_text else ""
        if not final_text and last_snapshot_text:
            final_text = sanitize_reply_text_for_runtime(last_snapshot_text)
            reply_source = "snapshot_text"
        if not final_text:
            recovered_text, recovered_source = recover_text_from_raw_events(raw_events)
            if recovered_text:
                final_text = sanitize_reply_text_for_runtime(recovered_text)
                reply_source = recovered_source or "raw_event_recovered"
        self.last_stream_meta = {
            "raw_event_count": len(raw_events),
            "event_types": unique_keep_order(event_types, limit=20),
            "snapshot_text": sanitize_reply_text_for_runtime(last_snapshot_text),
            "recovered_text": final_text or "",
            "reply_source": reply_source,
            "raw_events": raw_events[-80:],
        }
        return final_text, images, conv_id, sec_id

    def _send_message_legacy(self, prompt: str, guest: bool = False,
                     conversation_id: Optional[str] = None,
                     section_id: Optional[str] = None,
                     stream: bool = True,
                     apply_initial_prompt: bool = False,
                     forced_profile: str = "auto",
                     session_data: Optional[Dict[str, Any]] = None,
                     call_chain_text: str = "",
                     route_entry: Optional[Dict[str, Any]] = None,
                     on_text=None) -> Tuple[str, List[str], Optional[str], Optional[str]]:
        if apply_initial_prompt:
            prompt, _, _ = build_runtime_prompt(prompt, session_data=session_data,
                                               forced_profile=forced_profile, is_initial=True,
                                               call_chain_text=call_chain_text, route_entry=route_entry)
        elif session_data is not None:
            prompt, _, _ = build_runtime_prompt(prompt, session_data=session_data,
                                               forced_profile=forced_profile, is_initial=False,
                                               call_chain_text=call_chain_text, route_entry=route_entry)
        current_conversation_id = self._normalize_id(conversation_id)
        current_section_id = self._normalize_id(section_id)
        allow_retry_without_session = bool(current_conversation_id)

        try:
            for _ in range(3):
                endpoint = self._resolve_chat_endpoint(current_conversation_id)
                payload = self._build_payload(prompt, guest, current_conversation_id, current_section_id)
                resp = self._post_chat_request(endpoint, payload, stream=stream)
            if not resp.encoding or resp.encoding.lower() in {"iso-8859-1", "us-ascii"}:
                resp.encoding = "utf-8"
            retryable_missing_session = False
            if resp.status_code >= 400:
                error_body = resp.text or ""
                retryable_missing_session = (
                    not guest
                    and bool(conversation_id)
                    and ("会话配置不存在" in error_body or "session.config" in error_body)
                )
                if retryable_missing_session:
                    payload = self._build_payload(prompt, guest, None, None)
                    resp = requests.post(self.chat_endpoint, json=payload, stream=stream, timeout=self.request_timeout)
                    if not resp.encoding or resp.encoding.lower() in {"iso-8859-1", "us-ascii"}:
                        resp.encoding = "utf-8"
                resp.raise_for_status()

            if stream and "text/event-stream" in resp.headers.get("Content-Type", ""):
                final_text, images, new_conv_id, new_sec_id = self._parse_stream(resp, on_text)
                if new_conv_id and (not conversation_id or retryable_missing_session):
                    conversation_id = new_conv_id
                if new_sec_id and (not section_id or retryable_missing_session):
                    section_id = new_sec_id
                return final_text, images, conversation_id, section_id
            else:
                raw_body = resp.text or ""
                data: Dict[str, Any] = {}
                try:
                    parsed = resp.json()
                    if isinstance(parsed, dict):
                        data = parsed
                except Exception:
                    data = {}

                text = ""
                reply_source = ""
                if data:
                    text = extract_snapshot_text_from_event(data)
                    if text:
                        reply_source = "json_snapshot"
                if not text and data:
                    recovered_text, recovered_source = recover_text_from_raw_events([json.dumps(data, ensure_ascii=False)])
                    if recovered_text:
                        text = recovered_text
                        reply_source = recovered_source or "json_recovered"
                if not text and raw_body:
                    recovered_text, recovered_source = recover_text_from_raw_events([raw_body])
                    if recovered_text:
                        text = recovered_text
                        reply_source = recovered_source or "body_recovered"
                text = sanitize_reply_text_for_runtime(text)

                images = list(data.get("img_urls", [])) if isinstance(data.get("img_urls", []), list) else []
                if not images and data:
                    images = collect_images_from_event(data)
                self.last_stream_meta = {
                    "raw_event_count": 0,
                    "event_types": [],
                    "snapshot_text": text or "",
                    "recovered_text": text or "",
                    "reply_source": reply_source or "json_text",
                    "response_mode": "json",
                    "response_keys": sorted(list(data.keys()))[:20] if isinstance(data, dict) else [],
                    "raw_body_preview": raw_body[:4000],
                }
                if not guest:
                    if "conversation_id" in data and data["conversation_id"]:
                        conversation_id = data["conversation_id"]
                    if "section_id" in data and data["section_id"]:
                        section_id = data["section_id"]
                return text, images, conversation_id, section_id

        except Exception as e:
            return f"[错误] {e}", [], None, None


# ---------- GUI 应用 ----------
    def send_message(self, prompt: str, guest: bool = False,
                     conversation_id: Optional[str] = None,
                     section_id: Optional[str] = None,
                     stream: bool = True,
                     apply_initial_prompt: bool = False,
                     forced_profile: str = "auto",
                     session_data: Optional[Dict[str, Any]] = None,
                     call_chain_text: str = "",
                     route_entry: Optional[Dict[str, Any]] = None,
                     on_text=None) -> Tuple[str, List[str], Optional[str], Optional[str]]:
        if apply_initial_prompt:
            prompt, _, _ = build_runtime_prompt(
                prompt,
                session_data=session_data,
                forced_profile=forced_profile,
                is_initial=True,
                call_chain_text=call_chain_text,
                route_entry=route_entry,
            )
        elif session_data is not None:
            prompt, _, _ = build_runtime_prompt(
                prompt,
                session_data=session_data,
                forced_profile=forced_profile,
                is_initial=False,
                call_chain_text=call_chain_text,
                route_entry=route_entry,
            )

        current_conversation_id = self._normalize_id(conversation_id)
        current_section_id = self._normalize_id(section_id)
        allow_retry_without_session = bool(current_conversation_id)

        try:
            for _ in range(3):
                endpoint = self._resolve_chat_endpoint(current_conversation_id)
                payload = self._build_payload(prompt, guest, current_conversation_id, current_section_id)
                resp = self._post_chat_request(endpoint, payload, stream=stream)

                detail_payload = self._extract_detail_payload(resp)
                detail_conversation_id = self._normalize_id(detail_payload.get("conversation_id"))
                detail_section_id = self._normalize_id(detail_payload.get("section_id"))
                if detail_conversation_id:
                    current_conversation_id = detail_conversation_id
                if detail_section_id:
                    current_section_id = detail_section_id

                if resp.status_code >= 400:
                    error_body = resp.text or ""
                    retryable_missing_session = (
                        allow_retry_without_session
                        and bool(current_conversation_id)
                        and ("session.config" in error_body or "会话配置不存在" in error_body)
                    )
                    if retryable_missing_session:
                        current_conversation_id = ""
                        current_section_id = ""
                        allow_retry_without_session = False
                        continue
                    if bool(detail_payload.get("retry_as_continue")) and current_conversation_id:
                        continue
                    raise RuntimeError(f"Doubao request failed: {self._format_error_message(resp)}")

                content_type = resp.headers.get("Content-Type", "")
                if stream and "text/event-stream" in content_type:
                    final_text, images, new_conv_id, new_sec_id = self._parse_stream(resp, on_text)
                    current_conversation_id = self._normalize_id(new_conv_id) or current_conversation_id
                    current_section_id = self._normalize_id(new_sec_id) or current_section_id
                    return final_text, images, current_conversation_id or None, current_section_id or None

                raw_body = resp.text or ""
                data: Dict[str, Any] = {}
                try:
                    parsed = resp.json()
                    if isinstance(parsed, dict):
                        data = parsed
                except Exception:
                    data = {}

                data_conversation_id = self._normalize_id(data.get("conversation_id"))
                data_section_id = self._normalize_id(data.get("section_id"))
                if data_conversation_id:
                    current_conversation_id = data_conversation_id
                if data_section_id:
                    current_section_id = data_section_id

                text = ""
                reply_source = ""
                if data:
                    text = extract_snapshot_text_from_event(data)
                    if text:
                        reply_source = "json_snapshot"
                if not text and data:
                    recovered_text, recovered_source = recover_text_from_raw_events([json.dumps(data, ensure_ascii=False)])
                    if recovered_text:
                        text = recovered_text
                        reply_source = recovered_source or "json_recovered"
                if not text and raw_body:
                    recovered_text, recovered_source = recover_text_from_raw_events([raw_body])
                    if recovered_text:
                        text = recovered_text
                        reply_source = recovered_source or "body_recovered"
                text = sanitize_reply_text_for_runtime(text)

                images = list(data.get("img_urls", [])) if isinstance(data.get("img_urls", []), list) else []
                if not images and data:
                    images = collect_images_from_event(data)
                self.last_stream_meta = {
                    "raw_event_count": 0,
                    "event_types": [],
                    "snapshot_text": text or "",
                    "recovered_text": text or "",
                    "reply_source": reply_source or "json_text",
                    "response_mode": "json",
                    "response_keys": sorted(list(data.keys()))[:20] if isinstance(data, dict) else [],
                    "raw_body_preview": raw_body[:4000],
                }

                follow_up_required = bool(data.get("follow_up_required")) or bool(detail_payload.get("follow_up_required"))
                follow_up_mode = str(
                    data.get("follow_up_conversation_mode")
                    or detail_payload.get("follow_up_conversation_mode")
                    or ""
                ).strip()
                should_continue_after_new = (
                    endpoint == self.chat_endpoint_new
                    and bool(current_conversation_id)
                    and (
                        follow_up_required
                        or follow_up_mode == "continue"
                        or (not text and not images)
                    )
                )
                if should_continue_after_new:
                    continue

                return text, images, current_conversation_id or None, current_section_id or None

            raise RuntimeError("Doubao request failed: exhausted compatibility retries")

        except Exception as e:
            return f"[错误] {e}", [], current_conversation_id or None, current_section_id or None

class ChatGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("豆包API客户端")
        self.root.geometry("1000x700")

        self.conv_manager = ConversationManager()
        self.client = DoubaoClient()

        self.msg_queue = queue.Queue()
        self.current_ai_reply = ""

        self._create_widgets()
        self._refresh_session_list()
        self._restore_current_session_messages()
        self._update_client_config()
        self._process_queue()

    def _create_widgets(self):
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 左侧会话面板
        left_frame = ttk.Frame(main_frame, width=250)
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=5, pady=5)

        header_frame = ttk.Frame(left_frame)
        header_frame.pack(fill=tk.X)
        ttk.Label(header_frame, text="会话列表", font=('Arial', 12, 'bold')).pack(side=tk.LEFT)
        ttk.Button(header_frame, text="新建", command=self._new_conversation).pack(side=tk.RIGHT, padx=2)

        self.login_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(left_frame, text="已登录状态", variable=self.login_var,
                        command=self._on_login_toggle).pack(anchor=tk.W, pady=5)

        self.session_listbox = tk.Listbox(left_frame, height=20)
        self.session_listbox.pack(fill=tk.BOTH, expand=True)
        self.session_listbox.bind('<<ListboxSelect>>', self._on_session_select)

        btn_frame = ttk.Frame(left_frame)
        btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(btn_frame, text="删除会话", command=self._delete_conversation).pack(side=tk.LEFT, padx=2)

        # 右侧聊天区域
        right_frame = ttk.Frame(main_frame)
        right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.title_label = ttk.Label(right_frame, text="当前会话: 新会话", font=('Arial', 10, 'bold'))
        self.title_label.pack(anchor=tk.W, pady=5)

        self.chat_display = scrolledtext.ScrolledText(right_frame, wrap=tk.WORD, state=tk.DISABLED)
        self.chat_display.pack(fill=tk.BOTH, expand=True)

        self.chat_display.tag_config("user", foreground="blue", font=('Arial', 10, 'bold'))
        self.chat_display.tag_config("ai", foreground="green", font=('Arial', 10, 'bold'))
        self.chat_display.tag_config("system", foreground="gray")

        # 输入区域
        input_frame = ttk.Frame(right_frame)
        input_frame.pack(fill=tk.X, pady=5)

        options_frame = ttk.Frame(input_frame)
        options_frame.pack(fill=tk.X)

        self.auto_cot_var = tk.BooleanVar(value=False)
        self.deep_think_var = tk.BooleanVar(value=False)
        self.auto_exec_var = tk.BooleanVar(value=False)
        self.raw_send_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="自动CoT", variable=self.auto_cot_var,
                        command=self._update_client_config).pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(options_frame, text="深度思考", variable=self.deep_think_var,
                        command=self._update_client_config).pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(options_frame, text="自动执行JSON", variable=self.auto_exec_var).pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(options_frame, text="GUI直发", variable=self.raw_send_var).pack(side=tk.LEFT, padx=5)

        ttk.Label(options_frame, text="模式覆盖:").pack(side=tk.LEFT, padx=(10,2))
        self.think_mode_var = tk.StringVar(value="")
        mode_combo = ttk.Combobox(options_frame, textvariable=self.think_mode_var,
                                  values=["", "0", "1", "3"], width=10, state="readonly")
        mode_combo.pack(side=tk.LEFT)
        mode_combo.bind('<<ComboboxSelected>>', lambda e: self._update_client_config())

        self.message_entry = tk.Text(input_frame, height=3, wrap=tk.WORD)
        self.message_entry.pack(fill=tk.X, pady=5)
        self.message_entry.bind('<Control-Return>', lambda e: self._send_message())

        btn_send = ttk.Button(input_frame, text="发送", command=self._send_message)
        btn_send.pack(side=tk.RIGHT, padx=5)

        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def _update_client_config(self):
        self.client.use_auto_cot = self.auto_cot_var.get()
        self.client.use_deep_think = self.deep_think_var.get()
        mode = self.think_mode_var.get()
        self.client.think_mode = int(mode) if mode.isdigit() else None

    def _current_session_name(self) -> Optional[str]:
        return self.conv_manager.current_session

    def _clear_chat_display(self):
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.delete("1.0", tk.END)
        self.chat_display.config(state=tk.DISABLED)

    def _restore_current_session_messages(self):
        self._clear_chat_display()
        session_name = self._current_session_name()
        if not session_name:
            return
        for message in self.conv_manager.get_visible_messages(session_name):
            self._display_message(
                message.get("sender", "系统"),
                message.get("text", ""),
                is_user=message.get("is_user", False),
                persist=False,
            )

    def _profile_label_for_current(self) -> str:
        session = self.conv_manager.current_data()
        if not session:
            return PROMPT_PROFILES[DEFAULT_PROFILE]["label"]
        profile = session.get("profile", DEFAULT_PROFILE)
        return PROMPT_PROFILES.get(profile, PROMPT_PROFILES[DEFAULT_PROFILE])["label"]

    def _refresh_session_list(self):
        self.session_listbox.delete(0, tk.END)
        sessions = self.conv_manager.list()
        for s in sessions:
            self.session_listbox.insert(tk.END, s)
        current = self.conv_manager.current_session
        if current and current in sessions:
            idx = sessions.index(current)
            self.session_listbox.selection_set(idx)
            self.title_label.config(text=f"当前会话: {current} [{self._profile_label_for_current()}]")
        else:
            self.title_label.config(text="当前会话: 新会话")

    def _on_session_select(self, event):
        selection = self.session_listbox.curselection()
        if not selection:
            return
        name = self.session_listbox.get(selection[0])
        try:
            self.conv_manager.switch(name)
            self._refresh_session_list()
            self._restore_current_session_messages()
            self._display_message("系统", f"已切换到会话: {name}", is_user=False, persist=False)
        except Exception as e:
            self._display_message("系统", f"切换失败: {e}", is_user=False, persist=False)

    def _new_conversation(self):
        """新建会话，并自动发送默认初始化消息以获取会话ID"""
        # 清空当前会话标识
        self.conv_manager.current_session = None
        self.conv_manager.save()
        self._clear_chat_display()
        self._refresh_session_list()
        self._display_message("系统", "正在自动初始化新会话...", is_user=False, persist=False)

        # 自动发送默认消息
        self._send_auto_init()

    def _send_auto_init(self):
        """自动发送默认消息以获取会话ID"""
        self.status_var.set("初始化中...")
        self.message_entry.config(state=tk.DISABLED)

        # 获取当前会话标识（此时应为 None）
        conv_id = None
        sec_id = None
        guest = not self.login_var.get()
        if self.conv_manager.current_session:
            conv_id, sec_id = self.conv_manager.current()

        # 启动后台线程发送消息
        threading.Thread(
            target=self._send_thread,
            args=(DEFAULT_INIT_MSG, guest, conv_id, sec_id, True, True, DEFAULT_PROFILE, None, None),
            daemon=True,
        ).start()

    def _delete_conversation(self):
        selection = self.session_listbox.curselection()
        if not selection:
            return
        name = self.session_listbox.get(selection[0])
        self.conv_manager.delete(name)
        self._refresh_session_list()
        self._display_message("系统", f"会话 {name} 已删除", is_user=False)

    def _on_login_toggle_legacy(self):
        if not self.login_var.get():
            self.conv_manager.current_session = None
            self.conv_manager.save()
            self._refresh_session_list()
            self._display_message("系统", "已切换为未登录状态，仅单轮对话", is_user=False, persist=False)
        else:
            self._display_message("系统", "已切换为登录状态，支持多轮对话", is_user=False, persist=False)
        self._update_client_config()

    def _on_login_toggle(self):
        self._refresh_session_list()
        if not self.login_var.get():
            self._display_message("系统", "已切换为访客状态，当前会话会继续复用并持久化。", is_user=False, persist=False)
        else:
            self._display_message("系统", "已切换为登录状态，继续沿用当前会话。", is_user=False, persist=False)
        self._update_client_config()

    def _display_message(self, sender: str, text: str, is_user: bool = True,
                         persist: bool = True, session_name: Optional[str] = None):
        self.chat_display.config(state=tk.NORMAL)
        if is_user:
            self.chat_display.insert(tk.END, f"你：\n", "user")
        else:
            self.chat_display.insert(tk.END, f"{sender}：\n", "ai")
        self.chat_display.insert(tk.END, f"{text}\n\n")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)
        if persist:
            target_name = session_name or self._current_session_name()
            if target_name:
                self.conv_manager.append_visible_message(target_name, sender, text, is_user)

    def _append_ai_chunk(self, chunk: str, final: bool = False):
        if not hasattr(self, '_ai_message_started') or not self._ai_message_started:
            self.chat_display.config(state=tk.NORMAL)
            self.chat_display.insert(tk.END, f"AI：\n", "ai")
            self._ai_message_started = True
            self._ai_chunks = []
        self._ai_chunks.append(chunk)
        self.chat_display.insert(tk.END, chunk, "ai")
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)
        if final:
            full_reply = "".join(self._ai_chunks or [])
            self.chat_display.config(state=tk.NORMAL)
            self.chat_display.insert(tk.END, "\n\n")
            self.chat_display.config(state=tk.DISABLED)
            session_name = self._current_session_name()
            if session_name and full_reply:
                self.conv_manager.append_visible_message(session_name, "AI", full_reply, False)
                self.conv_manager.remember_assistant_reply(session_name, full_reply)
            self._ai_message_started = False
            self._ai_chunks = None

    def _send_message(self):
        msg = self.message_entry.get("1.0", tk.END).strip()
        if not msg:
            return
        auto_record_hits_from_message(msg, source_label="gui_user")
        self.message_entry.delete("1.0", tk.END)
        self._display_message("你", msg, is_user=True)
        self.status_var.set("发送中...")
        self.message_entry.config(state=tk.DISABLED)

        conv_id = None
        sec_id = None
        guest = not self.login_var.get()
        session_name = self._current_session_name()
        session_data = dict(self.conv_manager.current_data() or {}) if session_name else None
        if self.conv_manager.current_session:
            conv_id, sec_id = self.conv_manager.current()

        apply_initial_prompt = conv_id is None and sec_id is None and not self.raw_send_var.get()
        profile_id, profile_reason = classify_prompt_profile(
            msg,
            (session_data or {}).get("profile", DEFAULT_PROFILE),
            "auto",
        )
        if session_name:
            self.conv_manager.remember_user_context(session_name, msg, profile_id, profile_reason)
            session_data = dict(self.conv_manager.current_data() or {})
            self._refresh_session_list()

        threading.Thread(
            target=self._send_thread,
            args=(msg, guest, conv_id, sec_id, False, apply_initial_prompt, profile_id, session_data, session_name, self.raw_send_var.get()),
            daemon=True,
        ).start()

    def _send_thread(self, prompt: str, guest: bool, conv_id: Optional[str], sec_id: Optional[str],
                     is_auto_init: bool = False, apply_initial_prompt: bool = False,
                     forced_profile: str = "auto",
                     session_data: Optional[Dict[str, Any]] = None,
                     session_name: Optional[str] = None,
                     raw_send: bool = False):
        def on_text(chunk, final=False):
            self.msg_queue.put(("chunk", chunk, final))

        reply, images, new_conv_id, new_sec_id = self.client.send_message(
            prompt, guest=guest, conversation_id=conv_id, section_id=sec_id,
            stream=True,
            apply_initial_prompt=apply_initial_prompt,
            forced_profile=forced_profile,
            session_data=None if raw_send else session_data,
            on_text=on_text
        )

        if images:
            self.msg_queue.put(("images", images, None))

        if new_conv_id:
            self.msg_queue.put(("ids", new_conv_id, new_sec_id, is_auto_init, session_name, forced_profile))

        self.msg_queue.put(("finish", reply, session_name))
        self.msg_queue.put(("enable_input", None, None))

    def _dispatch_reply_thread(self, reply_text: str, session_name: Optional[str]):
        command_json = normalize_command_json(reply_text)
        if not command_json:
            return
        ok, _, payload = dispatch_command_json(command_json, stop_on_error=False)
        summary = format_dispatch_summary(payload)
        if not summary:
            summary = "tool_dispatcher 未返回可解析结果"
        self.msg_queue.put(("dispatch_result", ok, summary, session_name))

    def _process_queue(self):
        try:
            while True:
                item = self.msg_queue.get_nowait()
                action = item[0]
                params = list(item[1:])
                while len(params) < 5:
                    params.append(None)
                data1, data2, data3, data4, data5 = params[:5]

                if action == "chunk":
                    self._append_ai_chunk(data1, data2)
                elif action == "images":
                    for url in data1:
                        self._display_message("图片", url, is_user=False)
                elif action == "ids":
                    # data1: new_conv_id, data2: new_sec_id, data3: is_auto_init, data4: session_name, data5: forced_profile
                    if data4 and data4 in self.conv_manager.sessions:
                        self.conv_manager.update_ids(data4, data1, data2)
                    elif not self.conv_manager.current_session:
                        name = f"会话_{data1[-8:]}"
                        self.conv_manager.create(name, data1, data2, profile=data5 or DEFAULT_PROFILE)
                        self.conv_manager.current_session = name
                        self.conv_manager.save()
                        self._refresh_session_list()
                        msg = f"已自动创建会话: {name}" if not data3 else f"新会话 {name} 已初始化"
                        self._display_message("系统", msg, is_user=False, persist=False)
                elif action == "finish":
                    if hasattr(self, '_ai_message_started') and self._ai_message_started:
                        self._append_ai_chunk("", final=True)
                    elif data1 and data2:
                        self.conv_manager.append_visible_message(data2, "AI", data1, False)
                        self.conv_manager.remember_assistant_reply(data2, data1)
                    if data1 and not str(data1).startswith("[错误]") and self.auto_exec_var.get():
                        threading.Thread(
                            target=self._dispatch_reply_thread,
                            args=(data1, data2),
                            daemon=True,
                        ).start()
                    if data1 and data1.startswith("[错误]"):
                        self._display_message("错误", data1, is_user=False)
                elif action == "dispatch_result":
                    title = "工具执行" if data1 else "工具执行失败"
                    self._display_message(title, data2, is_user=False, session_name=data3)
                elif action == "enable_input":
                    self.message_entry.config(state=tk.NORMAL)
                    self.status_var.set("就绪")
        except queue.Empty:
            pass
        self.root.after(100, self._process_queue)

    def run(self):
        self.root.mainloop()


# ---------- 命令行模式 ----------
def cli_mode():
    parser = argparse.ArgumentParser(description="豆包API客户端命令行模式")
    parser.add_argument("--message", "-m", help="要发送的消息（若不提供且无会话ID，则自动发送默认初始化消息）")
    parser.add_argument("--message_file", help="从文件读取完整消息，避免 PowerShell 对 $ 和引号改写")
    parser.add_argument("--message_b64", help="从 base64 读取完整消息，避免 cmd/PowerShell 吃掉特殊字符")
    parser.add_argument("--resume_meta_file", help="从已保存的回复元信息 JSON/LOG 中恢复会话ID和调用链")
    parser.add_argument("--resume_meta_text", help="直接传入已保存的回复元信息 JSON/LOG 文本")
    parser.add_argument("--conversation_id", help="会话ID")
    parser.add_argument("--section_id", help="分段ID")
    parser.add_argument("--guest", action="store_true", help="访客模式（单轮）")
    parser.add_argument("--auto_cot", action="store_true", help="使用自动CoT")
    parser.add_argument("--deep_think", action="store_true", help="使用深度思考")
    parser.add_argument("--think_mode", type=int, choices=[0, 1, 3], help="思考模式 0/1/3")
    parser.add_argument("--no_preset", action="store_true", help="禁用首次逆向命令提示词")
    parser.add_argument("--show_preset", action="store_true", help="打印首次逆向命令提示词后退出")
    parser.add_argument("--extract_json", action="store_true", help="从回复中提取并规范化```json```代码块")
    parser.add_argument("--json_file", help="将提取出的命令JSON写入文件")
    parser.add_argument("--auto_exec", action="store_true", help="将回复里的命令JSON交给独立 tool_dispatcher.py 自动执行")
    parser.add_argument("--exec_output", help="将 tool_dispatcher 的执行结果写入 JSON 文件")
    parser.add_argument("--exec_stop_on_error", action="store_true", help="自动执行时遇错即停")
    parser.add_argument("--reply_meta_json", action="store_true", help="????????????????? JSON")
    parser.add_argument("--call_chain", help="调用链文本，用于自动分配已存角色卡和会话")
    parser.add_argument("--call_chain_file", help="从文件读取调用链文本")
    parser.add_argument("--call_chain_b64", help="从 base64 读取调用链文本，避免 cmd/PowerShell 把 >、$、() 当特殊字符")
    parser.add_argument("--show_chain_route", help="查看指定调用链当前绑定的会话/角色卡")
    parser.add_argument("--show_chain_candidates", action="store_true", help="查看调用链的历史候选会话")
    parser.add_argument("--reset_chain_route", nargs="?", const=RESET_CHAIN_SENTINEL,
                        help="关闭并重置指定调用链对应的本地会话/角色路由；不传值时复用 --call_chain/--call_chain_file/--resume_meta")
    parser.add_argument("--reset_chain_route_and_boot", nargs="?", const=RESET_CHAIN_SENTINEL,
                        help="重置指定调用链后，立即通过网络请求创建新的会议；不传值时复用 --call_chain/--call_chain_file/--resume_meta")
    parser.add_argument("--keep_chain_sessions", action="store_true", help="重置调用链时保留旧会话记录，只删除路由绑定")
    parser.add_argument("--reset_chain_and_run", action="store_true", help="先重置当前调用链绑定，再立刻按新会议继续运行")
    parser.add_argument("--profile", default="auto",
                        choices=["auto"] + list(PROMPT_PROFILES.keys()),
                        help="手动指定提示词分类")
    parser.add_argument("--focus_note", help="额外隐藏专注提示，会跟随每轮一起发送")
    parser.add_argument("--raw_send", action="store_true", help="纯直发模式，不拼提示词模板，直接发送用户输入")
    parser.add_argument("--record_hit_class", help="手动记录命中的类")
    parser.add_argument("--record_hit_text", help="记录命中的片段文本")
    parser.add_argument("--record_hit_file", help="从文件读取命中片段文本")
    parser.add_argument("--record_hit_source", help="命中来源路径或标识")
    parser.add_argument("--record_hit_tool", default="cli_manual", help="命中来源工具")
    parser.add_argument("--show_hit_class", help="查看已存储的类命中档案")
    toolchain_session_group = parser.add_mutually_exclusive_group()
    toolchain_session_group.add_argument("--toolchain_separate_conversations",
                                         dest="toolchain_separate_conversations",
                                         action="store_true",
                                         help="???????????????????????")
    toolchain_session_group.add_argument("--share_toolchain_conversations",
                                         dest="toolchain_separate_conversations",
                                         action="store_false",
                                         help="???????????????????????")
    parser.set_defaults(toolchain_separate_conversations=None)
    for action in getattr(parser, "_actions", []):
        if getattr(action, "dest", "") == "guest":
            action.help = "访客模式（支持新建会话和续聊）"
            break
    args = parser.parse_args()

    resume_payload: Dict[str, Any] = {}
    if args.resume_meta_file:
        resume_payload = load_resume_payload(read_text_flexible(args.resume_meta_file))
    elif args.resume_meta_text:
        resume_payload = load_resume_payload(args.resume_meta_text)

    call_chain_text = args.call_chain or ""
    if args.call_chain_file:
        call_chain_text = read_text_flexible(args.call_chain_file).lstrip("\ufeff")
    elif args.call_chain_b64:
        call_chain_text = decode_base64_text(args.call_chain_b64).lstrip("\ufeff")
    if not call_chain_text and resume_payload:
        call_chain_text = str(
            resume_payload.get("call_chain_text", "")
            or resume_payload.get("route_entry", {}).get("chain_text", "")
            or ""
        )
    call_chain_text = normalize_call_chain(call_chain_text)
    reset_boot_result: Dict[str, Any] = {}

    def resolve_reset_chain_arg(value: Optional[str]) -> str:
        if value is None:
            return ""
        if value == RESET_CHAIN_SENTINEL:
            return call_chain_text
        return normalize_call_chain(value)

    if args.reset_chain_route_and_boot:
        call_chain_text = resolve_reset_chain_arg(args.reset_chain_route_and_boot)
        if not call_chain_text:
            print("错误：--reset_chain_route_and_boot 需要调用链；可配合 --call_chain_file 或 --resume_meta_file 使用")
            return
        reset_boot_result = reset_call_chain_state(
            call_chain_text,
            drop_sessions=not args.keep_chain_sessions,
            conversation_manager=ConversationManager(storage_file=os.path.join(APP_DIR, SESSION_FILE)),
        )

    if args.show_hit_class:
        data = load_class_hits(args.show_hit_class)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    if args.show_chain_route:
        chain_to_show = normalize_call_chain(args.show_chain_route)
        route = resolve_call_chain_route(chain_to_show, preferred_profile=args.profile)
        payload = {
            "call_chain_text": chain_to_show,
            "route_entry": route,
            "thread_slots": list_call_chain_threads(chain_to_show),
        }
        if args.show_chain_candidates:
            payload["session_candidates"] = rank_sessions_for_classes(
                extract_class_targets(chain_to_show, limit=16),
                preferred_profile=args.profile,
                limit=5,
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if args.reset_chain_route:
        chain_to_reset = resolve_reset_chain_arg(args.reset_chain_route)
        if not chain_to_reset:
            print("错误：--reset_chain_route 需要调用链；可配合 --call_chain_file 或 --resume_meta_file 使用")
            return
        result = reset_call_chain_state(
            chain_to_reset,
            drop_sessions=not args.keep_chain_sessions,
            conversation_manager=ConversationManager(storage_file=os.path.join(APP_DIR, SESSION_FILE)),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.record_hit_class:
        record_text = args.record_hit_text or ""
        if args.record_hit_file:
            record_text = read_text_flexible(args.record_hit_file).lstrip("\ufeff")
        stored_path = record_class_hit(
            class_key=args.record_hit_class,
            source_path=args.record_hit_source or "",
            fragment=record_text,
            tool=args.record_hit_tool,
            note="manual_record",
        )
        print(stored_path or "")
        return

    preview_message = args.message or ""
    if args.message_file:
        preview_message = read_text_flexible(args.message_file).lstrip("\ufeff")
    elif args.message_b64:
        preview_message = decode_base64_text(args.message_b64).lstrip("\ufeff")
    if not preview_message and resume_payload:
        preview_message = str(
            resume_payload.get("next_query", "")
            or resume_payload.get("last_user_message", "")
            or ""
        ).strip()

    if args.reset_chain_and_run and call_chain_text:
        reset_call_chain_state(
            call_chain_text,
            drop_sessions=not args.keep_chain_sessions,
            conversation_manager=ConversationManager(storage_file=os.path.join(APP_DIR, SESSION_FILE)),
        )

    route_entry = resolve_call_chain_route(call_chain_text, preferred_profile=args.profile) if call_chain_text else {}
    toolchain_config = load_tool_dispatcher_config()
    toolchain_separate_conversations = use_toolchain_separate_conversations(toolchain_config, args.toolchain_separate_conversations)
    takeover_hint_text = "\n".join(
        value for value in [
            preview_message,
            call_chain_text,
            str(resume_payload.get("reply_text", "") or ""),
            str(resume_payload.get("command_json", "") or ""),
            str(resume_payload.get("profile", "") or ""),
        ] if value
    )
    requested_command_type = infer_followup_command_type_by_type(takeover_hint_text)
    takeover_entry = resolve_role_takeover_route(
        call_chain_text,
        preferred_profile="",
        command_type=requested_command_type,
        toolchain_key="",
    ) if call_chain_text else {}
    active_route_entry = takeover_entry if takeover_entry else route_entry
    ignore_resume_ids = bool(reset_boot_result)
    effective_conversation_id = (
        args.conversation_id
        or str(active_route_entry.get("conversation_id", "") or "")
        or ("" if ignore_resume_ids else str(resume_payload.get("conversation_id", "") or ""))
    )
    effective_section_id = (
        args.section_id
        or str(active_route_entry.get("section_id", "") or "")
        or ("" if ignore_resume_ids else str(resume_payload.get("section_id", "") or ""))
    )
    effective_profile = args.profile
    if effective_profile == "auto":
        if active_route_entry.get("profile"):
            effective_profile = str(active_route_entry.get("profile"))
        elif resume_payload.get("profile"):
            effective_profile = str(resume_payload.get("profile"))

    # 创建客户端
    client = DoubaoClient(use_auto_cot=args.auto_cot,
                          use_deep_think=args.deep_think,
                          think_mode=args.think_mode)

    # 决定消息内容
    message = preview_message
    if not message and resume_payload:
        message = str(
            resume_payload.get("next_query", "")
            or resume_payload.get("last_user_message", "")
            or ""
        ).strip()
    if not message and not effective_conversation_id:
        # 未提供消息且无会话ID，自动初始化
        message = DEFAULT_INIT_MSG
        print(f"未指定消息，自动初始化会话（发送: {message}）")
    elif not message and effective_conversation_id:
        message = DEFAULT_RESUME_MSG

    if not message:
        print("错误：必须提供 --message 或已有会话ID（--conversation_id）")
        sys.exit(1)

    auto_record_hits_from_message(message, source_label="cli_user")

    apply_initial_prompt = (not args.no_preset and not args.raw_send and not effective_conversation_id and not effective_section_id)
    initial_profile, profile_reason = classify_prompt_profile(message, DEFAULT_PROFILE, effective_profile)
    if active_route_entry and args.profile == "auto" and active_route_entry.get("profile"):
        initial_profile = str(active_route_entry.get("profile"))
        route_source = str(active_route_entry.get("matched_by", "stored_route") or "stored_route")
        profile_reason = f"chain_resume:{route_source}"
    local_mode = resolve_local_chat_mode(initial_profile, text=message)
    if args.think_mode is not None:
        client.think_mode = args.think_mode
    else:
        client.think_mode = int(local_mode["think_mode"])
    client.use_auto_cot = True if args.auto_cot else bool(local_mode["use_auto_cot"])
    client.use_deep_think = True if args.deep_think else bool(local_mode["use_deep_think"])

    conv_manager = ConversationManager(storage_file=os.path.join(APP_DIR, SESSION_FILE))
    stored_session = {}
    active_session_seed = str(active_route_entry.get("session_name", "") or route_entry.get("session_name", "") or "")
    stored_session_name = ""
    for candidate in unique_keep_order([active_session_seed, base_chain_session_name(active_session_seed)], limit=2):
        if candidate and candidate in conv_manager.sessions:
            stored_session_name = candidate
            stored_session = dict(conv_manager.sessions.get(candidate, {}) or {})
            break
    session_context = None
    if not args.no_preset and not args.raw_send:
        session_context = build_session_context_payload(
            stored_session=stored_session,
            active_route_entry=active_route_entry,
            call_chain_text=call_chain_text,
            message=message,
            profile=initial_profile,
            profile_reason=profile_reason,
            focus_note=args.focus_note or "",
            toolchain_config=toolchain_config,
            force_refresh=apply_initial_prompt,
            suppress_local_context=bool(reset_boot_result),
        )


    if args.show_preset:
        prompt, _, _ = build_runtime_prompt(
            message,
            session_data=session_context,
            forced_profile=effective_profile,
            is_initial=apply_initial_prompt,
            call_chain_text=call_chain_text,
            route_entry=active_route_entry,
        )
        print(prompt)
        return

    if reset_boot_result:
        print_json_block({"reset_boot": reset_boot_result})

    # 实时打印回调
    printed_chunks = {"any": False}

    def print_chunk(chunk, final=False):
        if chunk:
            printed_chunks["any"] = True
        print(safe_console_text(chunk), end="", flush=True)
        if final:
            print()

    # 发送消息
    print("AI: ", end="", flush=True)
    reply, images, conv_id, sec_id = client.send_message(
        message,
        guest=args.guest,
        conversation_id=effective_conversation_id,
        section_id=effective_section_id,
        stream=True,
        apply_initial_prompt=apply_initial_prompt,
        forced_profile=effective_profile,
        session_data=None if args.raw_send else session_context,
        call_chain_text=call_chain_text,
        route_entry=active_route_entry,
        on_text=print_chunk
    )
    stream_meta = dict(getattr(client, "last_stream_meta", {}) or {})
    recovered_reply = str(stream_meta.get("recovered_text", "") or "")
    if not reply and recovered_reply:
        reply = recovered_reply
    if reply and not printed_chunks["any"]:
        safe_reply = safe_console_text(reply)
        print(safe_reply, end="" if safe_reply.endswith("\n") else "\n", flush=True)

    # 打印图片
    if images:
        print("\n图片链接:")
        for url in images:
            print(f"  {url}")

    # 输出会话标识
    if conv_id and not call_chain_text:
        print(f"\n会话ID: {conv_id}")
    if sec_id and not call_chain_text:
        print(f"分段ID: {sec_id}")

    if call_chain_text and conv_id and not args.guest:
        session_name_seed = str(active_route_entry.get("session_name", "") or route_entry.get("session_name", "") or "")
        active_toolchain_key = str(active_route_entry.get("toolchain_key", "") or "")
        active_command_type = str(active_route_entry.get("command_type", "") or "")
        use_active_child_session = bool(
            session_name_seed
            and active_toolchain_key
            and active_toolchain_key != "chain_root"
            and should_split_toolchain_session(
                active_toolchain_key,
                separate_toolchain_conversations=toolchain_separate_conversations,
                config=toolchain_config,
            )
        )
        if use_active_child_session:
            session_name = session_name_seed
        else:
            session_name = base_chain_session_name(session_name_seed)
        if not session_name:
            seed_target = extract_class_targets(call_chain_text, limit=1)
            seed_name = seed_target[0] if seed_target else "chain"
            session_name = f"chain_{safe_slug(seed_name)}_{chain_route_key(call_chain_text)[:6]}"

        conv_manager = ConversationManager(storage_file=os.path.join(APP_DIR, SESSION_FILE))
        if session_name in conv_manager.sessions:
            conv_manager.update_ids(session_name, conv_id, sec_id or "")
            conv_manager.set_profile(session_name, initial_profile, reason=profile_reason)
        else:
            conv_manager.create(session_name, conv_id, sec_id or "", profile=initial_profile, profile_reason=profile_reason)
        conv_manager.remember_user_context(session_name, f"{call_chain_text}\n{message}", initial_profile, profile_reason)
        conv_manager.remember_assistant_reply(session_name, reply)
        if session_context and session_context.get("role_prompt_refresh_due"):
            conv_manager.mark_prompt_refresh(session_name, session_context.get("user_turn_count"))
        root_session_name = base_chain_session_name(
            str(route_entry.get("session_name", "") or session_name or build_chain_session_name(call_chain_text))
        )
        route_entry = remember_call_chain_route(
            call_chain_text,
            (conv_id if not use_active_child_session else ""),
            ((sec_id or "") if not use_active_child_session else ""),
            DEFAULT_PROFILE,
            "chain_root",
            user_message=message,
            session_name=root_session_name,
            route_source="cli_chat",
            separate_toolchain_conversations=toolchain_separate_conversations,
            toolchain_config=toolchain_config,
        )
        if route_entry:
            conv_manager.set_thread_refs(
                session_name,
                group_ref=str(route_entry.get("group_ref", "") or ""),
                slot_ref=str(
                    active_route_entry.get("slot_ref", "") or route_entry.get("slot_ref", "") or ""
                ),
            )
            local_group_ref = str(route_entry.get("group_ref", "") or "")
            local_slot_ref = str(active_route_entry.get("slot_ref", "") or route_entry.get("slot_ref", "") or "")
            if (local_group_ref or local_slot_ref) and str(os.environ.get("DOUBAO_SHOW_LOCAL_THREAD", "") or "").strip():
                print(f"\nlocal_thread: {local_group_ref or '-'} / {local_slot_ref or '-'}")

        takeover_command_type = active_command_type
        takeover_toolchain_key = active_toolchain_key
        if takeover_command_type and takeover_command_type != "chain_root":
            takeover_session_name = session_name
            if should_split_toolchain_session(
                takeover_toolchain_key,
                separate_toolchain_conversations=toolchain_separate_conversations,
                config=toolchain_config,
            ):
                takeover_session_name = str(active_route_entry.get("session_name", "") or "") or (
                    f"{root_session_name}__{safe_slug(takeover_toolchain_key)}"
                )
            remember_role_takeover_route(
                call_chain_text,
                profile=initial_profile,
                command_type=takeover_command_type,
                toolchain_key=takeover_toolchain_key,
                session_name=takeover_session_name,
                conversation_id=conv_id,
                section_id=sec_id or "",
                role_card=initial_profile,
                note="cli_chat_followup",
            )


    command_routes_summary: List[Dict[str, Any]] = []
    if call_chain_text:
        command_items_for_routes = collect_command_items_from_text(reply)
        if command_items_for_routes:
            route_seed_name = (
                session_name if 'session_name' in locals() and session_name else
                str((route_entry or active_route_entry or {}).get("session_name", "") or "")
            )
            command_routes_summary = remember_command_routes(
                call_chain_text,
                command_items_for_routes,
                base_session_name=route_seed_name,
                conversation_id=conv_id or "",
                section_id=sec_id or "",
                separate_toolchain_conversations=toolchain_separate_conversations,
                toolchain_config=toolchain_config,
            )

    reply_meta: Dict[str, Any] = {}
    if args.reply_meta_json or (apply_initial_prompt and not args.guest):
        reply_meta = build_reply_meta(
            reply_text=reply,
            conversation_id=conv_id,
            section_id=sec_id,
            profile=initial_profile,
            profile_reason=profile_reason,
            images=images,
            call_chain_text=call_chain_text,
            route_entry=route_entry or active_route_entry,
            stream_meta=stream_meta,
            local_mode=local_mode,
        )
        if command_routes_summary:
            reply_meta["command_routes"] = command_routes_summary
        print_json_block(reply_meta)

    # 错误处理
    if reply.startswith("[错误]"):
        print(f"\n{reply}")

    command_json = None
    if args.extract_json or args.json_file or args.auto_exec:
        command_json = str(reply_meta.get("command_json", "") or "") or normalize_command_json(
            reply,
            config=toolchain_config,
            tools=discover_tool_context(toolchain_config),
            call_chain_text=call_chain_text,
        )
    if args.extract_json or args.json_file:
        command_json = str(reply_meta.get("command_json", "") or "") or normalize_command_json(
            reply,
            config=toolchain_config,
            tools=discover_tool_context(toolchain_config),
            call_chain_text=call_chain_text,
        )
        if command_json is None:
            print("\n未提取到```json```代码块")
        else:
            if args.extract_json:
                print("\n```json")
                print(safe_console_text(command_json))
                print("```")
            if args.json_file:
                with open(args.json_file, "w", encoding="utf-8") as f:
                    f.write(command_json + "\n")
                print(f"\n命令JSON已写入: {args.json_file}")

    if args.auto_exec:
        if reply.startswith("[??]"):
            print("\n????????????????")
            sys.exit(1)
        if command_json is None:
            print("\n??????JSON???????")
            sys.exit(1)
        initial_command_items = collect_command_items_from_text(command_json)
        if not initial_command_items:
            print("\n??????JSON???????")
            sys.exit(1)

        ok = False
        dispatch_payload: Dict[str, Any] = {}
        if not args.guest and call_chain_text:
            conv_manager = ConversationManager(storage_file=os.path.join(APP_DIR, SESSION_FILE))
            loop_payload = run_recursive_toolchain_loop(
                client=client,
                conv_manager=conv_manager,
                initial_command_items=initial_command_items,
                initial_command_routes=command_routes_summary,
                call_chain_text=call_chain_text,
                toolchain_config=toolchain_config,
                separate_toolchain_conversations=toolchain_separate_conversations,
                fallback_profile=initial_profile,
                fallback_profile_reason=profile_reason,
                focus_note=args.focus_note or "",
                output_path=args.exec_output or "",
                stop_on_error=args.exec_stop_on_error,
            )
            rounds = list(loop_payload.get("rounds", []))
            if rounds:
                dispatch_payload = dict(rounds[0].get("dispatch_payload", {}) or {})
                ok = bool(loop_payload.get("exhausted", False))
                print("\n```json")
                print(safe_console_text(json.dumps(dispatch_payload, ensure_ascii=False, indent=2)))
                print("```")
                for round_item in rounds[1:]:
                    print(f"\n[auto_exec round {round_item.get('round', 0)}]")
                    print("\n```json")
                    print(safe_console_text(json.dumps(round_item.get("dispatch_payload", {}), ensure_ascii=False, indent=2)))
                    print("```")
                for round_item in rounds:
                    for item in list(round_item.get("followups", [])):
                        reply_text = str(item.get("reply_text", "") or "").strip()
                        if not reply_text:
                            continue
                        print(f"\nAI(exec->{item.get('toolchain_key', '') or item.get('profile', '')}):")
                        print(safe_console_text(reply_text))
                print_json_block({
                    "auto_exec_loop": {
                        "round_count": loop_payload.get("round_count", 0),
                        "exhausted": loop_payload.get("exhausted", False),
                        "stopped_reason": loop_payload.get("stopped_reason", ""),
                        "max_rounds": loop_payload.get("max_rounds", 0),
                        "chain_index_file": loop_payload.get("chain_index_file", ""),
                        "chain_work_dir": loop_payload.get("chain_work_dir", ""),
                        "next_step_hint": loop_payload.get("next_step_hint", ""),
                    }
                })
            if args.exec_output:
                print(f"\n???????: {args.exec_output}")
        else:
            ok, dispatch_text, dispatch_payload, _ = run_reviewed_dispatch(
                initial_command_items,
                toolchain_config,
                tools=discover_tool_context(toolchain_config),
                call_chain_text=call_chain_text,
                output_path=args.exec_output or "",
                stop_on_error=args.exec_stop_on_error,
            )
            print("\n```json")
            print(safe_console_text(dispatch_text))
            print("```")
            if args.exec_output:
                print(f"\n???????: {args.exec_output}")

        if not args.guest and conv_id and not call_chain_text:
            feedback_message, feedback_kind = build_exec_feedback_message(
                dispatch_payload,
                toolchain_config,
                call_chain_text=call_chain_text,
            )
            if feedback_message:
                feedback_context = build_session_context_payload(
                    stored_session=stored_session,
                    active_route_entry=route_entry,
                    call_chain_text=call_chain_text,
                    message=feedback_message,
                    profile=initial_profile,
                    profile_reason=profile_reason,
                    focus_note=args.focus_note or "",
                    toolchain_config=toolchain_config,
                    force_refresh=False,
                )
                original_think_mode = client.think_mode
                original_auto_cot = client.use_auto_cot
                original_deep_think = client.use_deep_think
                feedback_mode = resolve_local_chat_mode(
                    initial_profile,
                    text=feedback_message,
                    feedback_kind=feedback_kind,
                )
                client.think_mode = int(feedback_mode["think_mode"])
                client.use_auto_cot = bool(feedback_mode["use_auto_cot"])
                client.use_deep_think = bool(feedback_mode["use_deep_think"])
                feedback_reply, _, _, _ = client.send_message(
                    feedback_message,
                    guest=False,
                    conversation_id=conv_id,
                    section_id=sec_id,
                    stream=True,
                    apply_initial_prompt=bool(feedback_context.get("role_prompt_refresh_due")),
                    forced_profile=initial_profile,
                    session_data=feedback_context,
                    call_chain_text=call_chain_text,
                    route_entry=route_entry,
                )
                client.think_mode = original_think_mode
                client.use_auto_cot = original_auto_cot
                client.use_deep_think = original_deep_think
                if feedback_reply:
                    print("\nAI(exec):")
                    print(safe_console_text(feedback_reply))
        if not ok:
            sys.exit(1)


# ---------- 程序入口 ----------
if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli_mode()
    else:
        app = ChatGUI()
        app.run()
