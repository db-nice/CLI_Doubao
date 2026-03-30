#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple


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
CONFIG_PATH = os.path.join(APP_DIR, "tool_dispatcher_config.json")
JSON_BLOCK_PATTERN = re.compile(r"```json\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)
WINDOWS_PATH_PATTERN = re.compile(r"[A-Za-z]:\\[^\"'\s]+")
POWERSHELL_PATH = os.environ.get(
    "POWERSHELL_PATH",
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
)
CMD_PATH = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
SENSITIVE_COMMAND_TOKENS = [
    "remove-item",
    "del ",
    "erase ",
    "rmdir",
    "rd ",
    "rm ",
    "git reset",
    "git clean",
    "format ",
]
POWERSHELL_SCRIPT_HINTS = (
    "get-childitem",
    "select-string",
    "where-object",
    "foreach-object",
    "sort-object",
    "measure-object",
    "out-file",
    "copy-item",
    "move-item",
    "remove-item",
    "new-item",
    "set-content",
    "get-content",
    "test-path",
    "join-path",
    "write-output",
    "write-host",
    "$env:",
    "$null",
)
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


def default_config() -> Dict[str, Any]:
    runtime_root = os.path.join(APP_DIR, "runtime")
    allowed_roots = [
        APP_DIR,
        runtime_root,
    ]
    if _normalize_bootstrap_path(APP_DIR).lower() != _normalize_bootstrap_path(SCRIPT_DIR).lower():
        allowed_roots.append(SCRIPT_DIR)
    return {
        "target_dex": "",
        "smali_dir": os.path.join(runtime_root, "smali_out"),
        "jadx_out": os.path.join(runtime_root, "jadx_out"),
        "workspace_root": "",
        "call_chain_output_root": os.path.join(APP_DIR, "call_chain_runs"),
        "call_chain_index_root": os.path.join(APP_DIR, "call_chain_index"),
        "allowed_roots": allowed_roots,
        "quick_think_mode": 0,
        "focus_on_success": "继续沿用当前调用链和角色卡，只根据本地输出推进下一条命令。",
        "focus_on_error": "只根据本地错误日志修正命令和路径，不重开新链路，不偏离当前角色卡。",
        "role_prompt_refresh_turns": 5,
        "auto_followup_after_exec": True,
        "auto_followup_max_rounds": 8,
        "jadx_force_new_meeting": True,
        "mirror_jadx_to_rename": True,
        "strict_tuning_message_scope": True,
        "naming_dedup_mode": "manual",
        "rename_apply_mode": "hybrid",
        "toolchain_separate_conversations": False,
        "review_initialized_roots_only": True,
    }


def normalize_root(path: str) -> str:
    if not path:
        return ""
    return _normalize_bootstrap_path(path)


def iter_stale_local_runtime_roots() -> List[str]:
    if normalize_root(APP_DIR).lower() == normalize_root(SCRIPT_DIR).lower():
        return []
    candidates: List[str] = []
    for raw_path in (
        os.path.join(SCRIPT_DIR, "runtime"),
        os.path.join(SCRIPT_DIR, "call_chain_runs"),
        os.path.join(SCRIPT_DIR, "call_chain_index"),
    ):
        normalized = normalize_root(raw_path)
        if normalized:
            candidates.append(normalized)
    return candidates


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        return handle.read()


def write_text(path: str, content: str) -> None:
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


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


def path_points_to_patch_runtime(path: str) -> bool:
    lowered = normalize_root(path).lower()
    if any(marker in lowered for marker in STALE_RUNTIME_MARKERS):
        return True
    for root in iter_stale_local_runtime_roots():
        lowered_root = root.lower()
        if lowered == lowered_root or lowered.startswith(lowered_root + "\\") or lowered.startswith(lowered_root + "/"):
            return True
    return False


def localize_runtime_config(config: Dict[str, Any]) -> Dict[str, Any]:
    defaults = default_config()
    localized = dict(config or {})

    for key in LOCAL_RUNTIME_KEYS:
        current = str(localized.get(key, "") or "")
        if not current or path_points_to_patch_runtime(current):
            localized[key] = defaults.get(key, "")

    workspace_root = str(localized.get("workspace_root", "") or "").strip()
    if not workspace_root:
        guessed_root = guess_workspace_root_from_target(str(localized.get("target_dex", "") or ""))
        if guessed_root:
            localized["workspace_root"] = guessed_root

    roots: List[str] = []
    for raw_path in localized.get("allowed_roots", []) if isinstance(localized.get("allowed_roots", []), list) else []:
        normalized = normalize_root(str(raw_path or ""))
        if not normalized or path_points_to_patch_runtime(normalized):
            continue
        roots.append(normalized)
    roots.extend(str(path) for path in defaults.get("allowed_roots", []) if path)
    for key in LOCAL_RUNTIME_KEYS + ("workspace_root",):
        value = str(localized.get(key, "") or "")
        if value:
            roots.append(value)
    target_dex = str(localized.get("target_dex", "") or "")
    if target_dex:
        roots.append(os.path.dirname(target_dex))
    localized["allowed_roots"] = sorted(set(normalize_root(path) for path in roots if path))
    return localized


def load_config() -> Dict[str, Any]:
    config = default_config()
    if not os.path.exists(CONFIG_PATH):
        config["allowed_roots"] = [normalize_root(path) for path in config.get("allowed_roots", []) if path]
        return config
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except Exception:
        loaded = {}
    if isinstance(loaded, dict):
        config.update(loaded)
    config = localize_runtime_config(config)
    config["allowed_roots"] = [normalize_root(path) for path in config.get("allowed_roots", []) if path]
    return config


def save_config(config: Dict[str, Any]) -> str:
    ensure_dir(os.path.dirname(CONFIG_PATH))
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=True, indent=2)
    return CONFIG_PATH


def extract_json_text(raw_text: str) -> str:
    if not raw_text:
        return ""
    stripped = raw_text.lstrip("\ufeff").strip()
    if stripped.startswith("{") or stripped.startswith("["):
        return stripped
    match = JSON_BLOCK_PATTERN.search(raw_text)
    if match:
        return match.group(1).strip()
    return stripped


def collect_entry_payloads(raw_text: str) -> List[Dict[str, Any]]:
    payloads: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add_payload(candidate_text: str) -> None:
        candidate_text = (candidate_text or "").strip()
        if not candidate_text:
            return
        try:
            parsed = json.loads(candidate_text)
        except json.JSONDecodeError:
            return
        if isinstance(parsed, dict):
            items = [parsed]
        elif isinstance(parsed, list):
            items = [item for item in parsed if isinstance(item, dict)]
        else:
            return
        for item in items:
            try:
                signature = json.dumps(item, ensure_ascii=False, sort_keys=True)
            except TypeError:
                signature = repr(item)
            if signature in seen:
                continue
            seen.add(signature)
            payloads.append(item)

    stripped = raw_text.lstrip("\ufeff").strip()
    if stripped.startswith("{") or stripped.startswith("["):
        add_payload(stripped)
    for match in JSON_BLOCK_PATTERN.finditer(raw_text or ""):
        add_payload(match.group(1))
    return payloads


def parse_entries(raw_text: str) -> List[Dict[str, Any]]:
    payload = extract_json_text(raw_text)
    if not payload:
        raise ValueError("未提取到命令 JSON")

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        preview = shorten_text(payload, 160)
        raise ValueError(f"命令 JSON 解析失败: {exc}; payload={preview}") from exc
    if isinstance(parsed, dict):
        items = [parsed]
    elif isinstance(parsed, list):
        items = parsed
    else:
        raise ValueError("JSON 顶层必须是对象或数组")

    result: List[Dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 个命令不是对象")
        result.append(item)
    return result


def shorten_text(text: str, limit: int = 240) -> str:
    value = " ".join((text or "").strip().split())
    if len(value) <= limit:
        return value
    return value[:limit - 3] + "..."


def resolve_cwd(cwd_value: Optional[str]) -> str:
    if not cwd_value:
        return APP_DIR
    if os.path.isabs(cwd_value):
        return cwd_value
    return os.path.abspath(os.path.join(APP_DIR, cwd_value))


def extract_command_paths(command: str) -> List[str]:
    return sorted(set(WINDOWS_PATH_PATTERN.findall(command or "")))


def extract_store_back_paths(store_back: str) -> List[str]:
    text = str(store_back or "")
    paths = list(WINDOWS_PATH_PATTERN.findall(text))
    oyut_match = re.search(r"\[OyUt\]\{([^{}]+)\}\{", text, flags=re.IGNORECASE)
    if oyut_match:
        candidate = str(oyut_match.group(1) or "").strip()
        if candidate:
            paths.append(candidate)
    return sorted(set(path for path in paths if path))


def collect_artifact_paths(entry: Dict[str, Any], exec_result: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []
    for raw_text in (
        str(entry.get("command", "") or ""),
        str(entry.get("store_back", "") or ""),
        str(exec_result.get("stdout", "") or ""),
        str(exec_result.get("stderr", "") or ""),
        str(entry.get("hit_store", "") or ""),
    ):
        candidates.extend(WINDOWS_PATH_PATTERN.findall(raw_text))
    candidates.extend(extract_store_back_paths(str(entry.get("store_back", "") or "")))
    existing: List[str] = []
    for candidate in candidates:
        path = normalize_root(candidate)
        if not path:
            continue
        if os.path.exists(path) and path not in existing:
            existing.append(path)
    return existing


def build_artifact_preview(paths: List[str], limit: int = 2) -> str:
    previews: List[str] = []
    for path in paths[:limit]:
        try:
            if os.path.isdir(path):
                entries = sorted(os.listdir(path))[:5]
                previews.append(f"{path} => dir[{', '.join(entries)}]")
                continue
            size = os.path.getsize(path)
            if size > 1024 * 512:
                previews.append(f"{path} => file(size={size})")
                continue
            content = read_text(path).strip()
            if content:
                previews.append(f"{path} => {shorten_text(content, 220)}")
            else:
                previews.append(f"{path} => file(empty)")
        except Exception as exc:
            previews.append(f"{path} => preview_error:{exc}")
    return " | ".join(previews)


def is_sensitive_command(command: str) -> bool:
    lowered = f" {(command or '').lower()} "
    return any(token in lowered for token in SENSITIVE_COMMAND_TOKENS)


def path_within_roots(path: str, roots: List[str]) -> bool:
    normalized_path = normalize_root(path)
    for root in roots:
        normalized_root = normalize_root(root)
        if normalized_root and normalized_path.lower().startswith(normalized_root.lower()):
            return True
    return False


def check_command_policy(command: str, config: Dict[str, Any]) -> Dict[str, Any]:
    paths = extract_command_paths(command)
    roots = [normalize_root(path) for path in config.get("allowed_roots", []) if path]
    sensitive = is_sensitive_command(command)
    outside_paths = [path for path in paths if not path_within_roots(path, roots)]
    blocked = False
    reason = ""
    if sensitive:
        if not paths:
            blocked = True
            reason = "sensitive_command_without_explicit_path"
        elif outside_paths:
            blocked = True
            reason = "sensitive_command_outside_allowed_roots"
    return {
        "blocked": blocked,
        "reason": reason,
        "sensitive": sensitive,
        "paths": paths,
        "outside_paths": outside_paths,
        "allowed_roots": roots,
    }


def looks_like_powershell_script(command: str) -> bool:
    stripped = str(command or "").strip()
    if not stripped or os.name != "nt":
        return False
    lowered = stripped.lower()
    if lowered.startswith(("powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd ", "cmd.exe")):
        return False
    if lowered.startswith(("python ", "python.exe", "py ", "java ", "java.exe")):
        return False
    if stripped.startswith(("& ", ". ", "$")):
        return True
    if re.match(r"^[\"']?[A-Za-z]:\\", stripped) or stripped.startswith((".\\", "..\\", "\\\\")):
        return False
    return any(token in lowered for token in POWERSHELL_SCRIPT_HINTS)


def run_shell_command(command: str, cwd: Optional[str] = None) -> Tuple[bool, Dict[str, Any]]:
    exec_cwd = resolve_cwd(cwd)
    try:
        if os.name == "nt":
            if looks_like_powershell_script(command):
                exec_command = [
                    POWERSHELL_PATH,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ]
            else:
                exec_command = [
                    CMD_PATH,
                    "/d",
                    "/s",
                    "/c",
                    command,
                ]
            result = subprocess.run(
                exec_command,
                cwd=exec_cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        else:
            result = subprocess.run(
                command,
                cwd=exec_cwd,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
    except Exception as exc:
        return False, {
            "ok": False,
            "cwd": exec_cwd,
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
        }

    return result.returncode == 0, {
        "ok": result.returncode == 0,
        "cwd": exec_cwd,
        "returncode": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
    }


def execute_entry(entry: Dict[str, Any], config: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    tool = str(entry.get("tool", "")).strip()
    command = str(entry.get("command", "")).strip()
    cwd = entry.get("cwd")
    started_at = time.time()
    policy = check_command_policy(command, config)

    result: Dict[str, Any] = {
        "tool": tool,
        "profile": str(entry.get("profile", "")),
        "reason": str(entry.get("reason", "")),
        "hit_class": str(entry.get("hit_class", "")),
        "hit_store": str(entry.get("hit_store", "")),
        "next_query": str(entry.get("next_query", "")),
        "store_back": str(entry.get("store_back", "")),
        "command": command,
        "entry_index": index,
        "started_at": started_at,
        "policy": policy,
        "judge_status": 0,
    }

    if not command:
        result.update({
            "ok": False,
            "returncode": -1,
            "judge_status": 1,
            "stdout": "",
            "stderr": "命令为空，无法执行",
            "finished_at": time.time(),
            "has_output": False,
        })
        return result

    if policy.get("blocked"):
        result.update({
            "ok": False,
            "returncode": 1,
            "judge_status": 1,
            "stdout": "",
            "stderr": f"命令已被策略拦截: {policy.get('reason', 'blocked')}",
            "finished_at": time.time(),
            "has_output": True,
        })
        result["stdout_preview"] = ""
        result["stderr_preview"] = shorten_text(result["stderr"], 300)
        result["duration_sec"] = round(result["finished_at"] - started_at, 3)
        return result

    ok, exec_result = run_shell_command(command, cwd=str(cwd) if cwd else None)
    result.update(exec_result)
    artifact_paths = collect_artifact_paths(entry, exec_result)
    artifact_preview = build_artifact_preview(artifact_paths)
    result["judge_status"] = 0 if ok else 1
    result["artifact_paths"] = artifact_paths
    result["artifact_preview"] = artifact_preview
    result["has_output"] = bool(
        (result.get("stdout", "") or "").strip()
        or (result.get("stderr", "") or "").strip()
        or artifact_preview
        or artifact_paths
    )
    result["stdout_preview"] = shorten_text(result.get("stdout", "") or artifact_preview, 300)
    result["stderr_preview"] = shorten_text(result.get("stderr", ""), 300)
    result["finished_at"] = time.time()
    result["duration_sec"] = round(result["finished_at"] - started_at, 3)
    return result


def build_output_payload(entries: List[Dict[str, Any]], results: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": all(item.get("ok") for item in results) if results else False,
        "count": len(results),
        "config": config,
        "entries": entries,
        "results": results,
    }


def read_input_payload(args: argparse.Namespace) -> str:
    if args.json_text:
        return args.json_text
    if args.json_file:
        return read_text(args.json_file)
    if args.response_file:
        return read_text(args.response_file)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValueError("必须提供 --json-text、--json-file、--response-file 或通过 stdin 输入")


def build_init_config(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_config()
    warnings: List[str] = []
    if args.target_dex:
        config["target_dex"] = os.path.abspath(args.target_dex)
    if args.smali_dir:
        config["smali_dir"] = os.path.abspath(args.smali_dir)
        try:
            ensure_dir(config["smali_dir"])
        except OSError as exc:
            warnings.append(f"smali_dir_create_failed:{exc}")
    if args.jadx_out:
        config["jadx_out"] = os.path.abspath(args.jadx_out)
        try:
            ensure_dir(config["jadx_out"])
        except OSError as exc:
            warnings.append(f"jadx_out_create_failed:{exc}")
    if args.workspace_root:
        config["workspace_root"] = os.path.abspath(args.workspace_root)
    elif not str(config.get("workspace_root", "") or "").strip():
        guessed_root = guess_workspace_root_from_target(str(config.get("target_dex", "") or ""))
        if guessed_root:
            config["workspace_root"] = guessed_root
    if args.chain_output_root:
        config["call_chain_output_root"] = os.path.abspath(args.chain_output_root)
    if args.chain_index_root:
        config["call_chain_index_root"] = os.path.abspath(args.chain_index_root)
    for key in ("call_chain_output_root", "call_chain_index_root"):
        value = str(config.get(key, "") or "")
        if value:
            try:
                ensure_dir(value)
            except OSError as exc:
                warnings.append(f"{key}_create_failed:{exc}")
    if args.quick_think_mode is not None:
        config["quick_think_mode"] = int(args.quick_think_mode)
    if args.strict_tuning_message_scope is not None:
        config["strict_tuning_message_scope"] = bool(args.strict_tuning_message_scope)
    if args.naming_dedup_mode:
        config["naming_dedup_mode"] = str(args.naming_dedup_mode).strip().lower()
    if args.rename_apply_mode:
        config["rename_apply_mode"] = str(args.rename_apply_mode).strip().lower()
    roots = list(config.get("allowed_roots", []))
    for item in args.allow_root or []:
        roots.append(item)
    if config.get("smali_dir"):
        roots.append(config["smali_dir"])
    if config.get("jadx_out"):
        roots.append(config["jadx_out"])
    if config.get("target_dex"):
        roots.append(os.path.dirname(config["target_dex"]))
    if config.get("workspace_root"):
        roots.append(config["workspace_root"])
    if config.get("call_chain_output_root"):
        roots.append(config["call_chain_output_root"])
    if config.get("call_chain_index_root"):
        roots.append(config["call_chain_index_root"])
    config["allowed_roots"] = sorted(set(normalize_root(path) for path in roots if path))
    if warnings:
        config["init_warnings"] = warnings
    return localize_runtime_config(config)


def main() -> int:
    parser = argparse.ArgumentParser(description="命令 JSON 执行分发器")
    parser.add_argument("--json-text", help="直接传入 JSON 字符串")
    parser.add_argument("--json-file", help="JSON 文件路径")
    parser.add_argument("--response-file", help="原始回复文本文件，自动提取```json```代码块")
    parser.add_argument("-o", "--output", help="将执行结果写入文件")
    parser.add_argument("--pretty", action="store_true", help="格式化输出 JSON")
    parser.add_argument("--stop-on-error", action="store_true", help="遇到错误时停止执行后续命令")
    parser.add_argument("--init-config", action="store_true", help="初始化或更新本地工具链配置")
    parser.add_argument("--show-config", action="store_true", help="显示当前工具链配置")
    parser.add_argument("--target-dex", help="初始化时记录当前要分析的 dex/apk 路径")
    parser.add_argument("--smali-dir", help="初始化时记录默认 smali 输出目录")
    parser.add_argument("--jadx-out", help="初始化时记录默认 jadx 输出目录")
    parser.add_argument("--workspace-root", help="初始化时记录工作区根目录，供运行时别名与提示词使用")
    parser.add_argument("--chain-output-root", help="初始化时记录调用链产物根目录")
    parser.add_argument("--chain-index-root", help="初始化时记录调用链索引根目录")
    parser.add_argument("--allow-root", action="append", help="允许敏感命令访问的根目录，可重复传入")
    parser.add_argument("--quick-think-mode", type=int, help="回传给 chat 时的快速模式编号，默认 0")
    parser.add_argument("--strict-tuning-message-scope", dest="strict_tuning_message_scope", action="store_true", help="命名微调和 named JADX 只允许使用当前消息明确给出的 source/owner/candidate")
    parser.add_argument("--no-strict-tuning-message-scope", dest="strict_tuning_message_scope", action="store_false", help="关闭当前消息范围约束")
    parser.add_argument("--naming-dedup-mode", choices=["off", "manual", "final_only", "always"], help="命名统计和最终去重策略")
    parser.add_argument("--rename-apply-mode", choices=["hybrid", "analysis_only", "smali_first"], help="命名后是直接分析还是等待 smali 改名完成再跑 named JADX")
    parser.set_defaults(strict_tuning_message_scope=None)
    args = parser.parse_args()

    if args.show_config:
        print(json.dumps(load_config(), ensure_ascii=True, indent=2))
        return 0

    if args.init_config:
        config = build_init_config(args)
        path = save_config(config)
        print(json.dumps({"config_path": path, "config": config}, ensure_ascii=True, indent=2))
        return 0

    raw_text = read_input_payload(args)
    entries = parse_entries(raw_text)
    config = load_config()

    results: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries):
        exec_result = execute_entry(entry, config, index=index)
        results.append(exec_result)
        if args.stop_on_error and not exec_result.get("ok"):
            break

    payload = build_output_payload(entries, results, config)
    output_text = json.dumps(payload, ensure_ascii=True, indent=2 if args.pretty or args.output else None)

    if args.output:
        write_text(args.output, output_text + "\n")

    print(output_text)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
