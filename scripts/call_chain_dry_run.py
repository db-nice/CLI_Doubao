#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def load_module(module_name: str, module_path: str):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable_to_load_module:{module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        return handle.read()


def write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def json_dump(data: Dict[str, Any], path: str = "") -> str:
    content = json.dumps(data, ensure_ascii=True, indent=2)
    if path:
        write_text(path, content)
    return content


def gather_text_input(file_path: str = "", inline_text: str = "") -> str:
    if file_path:
        return read_text(file_path)
    return inline_text or ""


def normalize_from_cli_return(router_module, args: argparse.Namespace) -> Dict[str, Any]:
    reply_meta_text = gather_text_input(args.reply_meta_file, args.reply_meta_text)
    stream_text = gather_text_input(args.stream_file, args.stream_text)
    reply_text = gather_text_input(args.reply_file, args.reply_text)

    reply_meta = router_module.parse_reply_meta_text(reply_meta_text) if reply_meta_text else {}
    stream_dump = router_module.parse_stream_dump_text(stream_text) if stream_text else {}
    if reply_text:
        stream_dump = dict(stream_dump)
        stream_dump["reply_text"] = reply_text
    return router_module.merge_sources(reply_meta, stream_dump)


def build_route_check(cli_module, normalized: Dict[str, Any]) -> Dict[str, Any]:
    route_entry = normalized.get("route_entry", {}) if isinstance(normalized.get("route_entry", {}), dict) else {}
    call_chain_text = str(normalized.get("call_chain_text", "") or route_entry.get("chain_text", "") or "")
    conversation_id = str(normalized.get("conversation_id", "") or "")
    section_id = str(normalized.get("section_id", "") or "")
    profile = str(normalized.get("profile", "") or route_entry.get("profile", "") or "")
    targets = list(route_entry.get("targets", [])) if isinstance(route_entry.get("targets", []), list) else []
    class_targets = list(route_entry.get("class_targets", [])) if isinstance(route_entry.get("class_targets", []), list) else []
    method_targets = list(route_entry.get("method_targets", [])) if isinstance(route_entry.get("method_targets", []), list) else []

    resolved = {}
    if call_chain_text:
        resolved = cli_module.resolve_call_chain_route(call_chain_text, preferred_profile=profile or "auto")

    ids_match = bool(
        conversation_id
        and section_id
        and route_entry
        and conversation_id == str(route_entry.get("conversation_id", "") or "")
        and section_id == str(route_entry.get("section_id", "") or "")
    )
    resolved_match = bool(
        resolved
        and conversation_id
        and conversation_id == str(resolved.get("conversation_id", "") or "")
        and profile == str(resolved.get("profile", "") or profile)
    )
    return {
        "name": "route_from_cli_reply",
        "ok": bool(call_chain_text and route_entry and ids_match),
        "call_chain_text": call_chain_text,
        "ids_match_route_entry": ids_match,
        "resolved_store_match": resolved_match,
        "conversation_id": conversation_id,
        "section_id": section_id,
        "profile": profile,
        "chain_key": str(route_entry.get("chain_key", "") or ""),
        "session_name": str(route_entry.get("session_name", "") or ""),
        "targets": targets,
        "class_targets": class_targets,
        "method_targets": method_targets,
        "resolved_preview": {
            "matched_by": str(resolved.get("matched_by", "") or ""),
            "conversation_id": str(resolved.get("conversation_id", "") or ""),
            "section_id": str(resolved.get("section_id", "") or ""),
            "profile": str(resolved.get("profile", "") or ""),
            "session_name": str(resolved.get("session_name", "") or ""),
        } if resolved else {},
    }


def build_command_check(normalized: Dict[str, Any]) -> Dict[str, Any]:
    command_json = str(normalized.get("command_json", "") or "")
    command_items = list(normalized.get("command_items", []))
    allocations = list(normalized.get("allocations", []))
    return {
        "name": "command_json_from_cli_reply",
        "ok": bool(command_json and command_items),
        "command_count": len(command_items),
        "allocation_count": len(allocations),
        "tools": [str(item.get("tool", "") or "") for item in command_items[:6] if isinstance(item, dict)],
        "profiles": [str(item.get("profile", "") or "") for item in command_items[:6] if isinstance(item, dict)],
        "reply_preview": str(normalized.get("reply_text", "") or "")[:240],
        "command_json_preview": command_json[:240],
    }


def run_preset_check(python_exe: str,
                     deployed_cli_path: str,
                     call_chain_text: str,
                     message_text: str,
                     expected_class: str = "") -> Dict[str, Any]:
    if not message_text.strip():
        return {
            "name": "preset_from_message_context",
            "ok": True,
            "skipped": True,
            "reason": "message_text_empty",
        }
    temp_dir = tempfile.mkdtemp(prefix="call_chain_mid_")
    message_path = os.path.join(temp_dir, "message.txt")
    write_text(message_path, message_text)

    command = [
        python_exe,
        deployed_cli_path,
        "--show_preset",
        "--message_file",
        message_path,
    ]
    if call_chain_text:
        command.extend(["--call_chain", call_chain_text])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    stdout = result.stdout or ""
    lower_message = message_text.lower()
    subclasses_requested = any(token in lower_message for token in ("subclasses", "子类", "--smali-dir"))
    contains_route_reuse = "local_ref=" in stdout and "profile=" in stdout
    contains_init_config = "runtime.target_dex=" in stdout and "runtime.allowed_root_count=" in stdout
    contains_dex_step = (
        "java -jar {baksmali} d {target_dex} -o {chain_smali_dir}" in stdout
        or "java -jar {baksmali} d {target_dex} -o {smali_dir}" in stdout
    )
    contains_subclasses_step = (
        (
            "subclasses --smali-dir {chain_smali_dir}" in stdout
            or "subclasses --smali-dir {smali_dir}" in stdout
        )
        and (not expected_class or expected_class in stdout)
    )

    preview_lines: List[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if (
            "dex->smali:" in line
            or "scan-children:" in line
            or "runtime.target_dex=" in line
            or "runtime.allowed_root_count=" in line
            or "local_ref=" in line
        ):
            preview_lines.append(line)

    ok = bool(result.returncode == 0 and contains_route_reuse and contains_init_config)
    if subclasses_requested:
        ok = bool(ok and contains_dex_step and contains_subclasses_step)

    return {
        "name": "preset_from_message_context",
        "ok": ok,
        "skipped": False,
        "subclasses_requested": subclasses_requested,
        "contains_route_reuse": contains_route_reuse,
        "contains_init_config": contains_init_config,
        "contains_dex_step": contains_dex_step,
        "contains_subclasses_step": contains_subclasses_step,
        "preview_lines": preview_lines[:10],
        "stderr_preview": (result.stderr or "").strip()[:400],
        "command": command,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize cli_chat return payloads and dry-run routing checks.")
    parser.add_argument(
        "--workspace-cli",
        default=os.path.join(SCRIPT_DIR, "cli_chat.py"),
        help="Workspace cli_chat.py path.",
    )
    parser.add_argument(
        "--deployed-cli",
        default=os.environ.get("DEPLOYED_CLI_CHAT", os.path.join(SCRIPT_DIR, "cli_chat.py")),
        help="Deployed cli_chat.py path.",
    )
    parser.add_argument(
        "--python",
        dest="python_exe",
        default=sys.executable,
        help="Python executable for the preset check.",
    )
    parser.add_argument("--reply-meta-file", help="cli_chat.py returned meta JSON file.")
    parser.add_argument("--reply-meta-text", help="cli_chat.py returned meta JSON text.")
    parser.add_argument("--stream-file", help="Raw stream dump file with data: lines.")
    parser.add_argument("--stream-text", help="Raw stream dump text.")
    parser.add_argument("--reply-file", help="Plain reply text file.")
    parser.add_argument("--reply-text", help="Plain reply text.")
    parser.add_argument("--message-file", help="Original user message file, used to verify preset context.")
    parser.add_argument("--message-text", help="Original user message text, used to verify preset context.")
    parser.add_argument(
        "-o",
        "--output",
        default=os.path.join(SCRIPT_DIR, "call_chain_dry_run_report.json"),
        help="Write normalized output and dry-run checks to this JSON file.",
    )
    args = parser.parse_args()

    if not any([
        args.reply_meta_file,
        args.reply_meta_text,
        args.stream_file,
        args.stream_text,
        args.reply_file,
        args.reply_text,
    ]):
        raise SystemExit("need_one_of_reply_meta_or_stream_or_reply_text")

    cli_module = load_module("cli_chat_under_test", os.path.abspath(args.workspace_cli))
    router_module = load_module("chat_return_router_under_test", os.path.join(SCRIPT_DIR, "chat_return_router.py"))
    normalized = normalize_from_cli_return(router_module, args)

    message_text = gather_text_input(args.message_file, args.message_text)
    route_check = build_route_check(cli_module, normalized)
    command_check = build_command_check(normalized)

    expected_class = ""
    if normalized.get("route_entry") and isinstance(normalized["route_entry"], dict):
        class_targets = normalized["route_entry"].get("class_targets", [])
        if isinstance(class_targets, list) and class_targets:
            expected_class = str(class_targets[0] or "")
    if not expected_class:
        allocations = normalized.get("allocations", [])
        if isinstance(allocations, list) and allocations:
            expected_class = str(allocations[0].get("hit_class", "") or "")

    preset_check = run_preset_check(
        python_exe=os.path.abspath(args.python_exe),
        deployed_cli_path=os.path.abspath(args.deployed_cli),
        call_chain_text=str(normalized.get("call_chain_text", "") or ""),
        message_text=message_text,
        expected_class=expected_class,
    )

    report = {
        "ok": bool(route_check.get("ok") and command_check.get("ok") and preset_check.get("ok")),
        "inputs": {
            "reply_meta_file": args.reply_meta_file or "",
            "stream_file": args.stream_file or "",
            "reply_file": args.reply_file or "",
            "message_file": args.message_file or "",
        },
        "normalized": normalized,
        "checks": [
            route_check,
            command_check,
            preset_check,
        ],
    }
    print(json_dump(report, args.output))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
