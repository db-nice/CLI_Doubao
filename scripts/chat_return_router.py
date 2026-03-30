#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Tuple


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import cli_chat as cli  # noqa: E402


ROLE_SLOT_MAP = {
    "tool_dispatch": "command_exec",
    "xref_analysis": "xref_followup",
    "rename_mapping": "rename_followup",
    "rename_tuning": "rename_tuning_followup",
    "naming_finalize": "naming_followup",
    "jadx_code_analyst": "jadx_followup",
}

PROFILE_ROLE_CARD = {
    "tool_dispatch": {
        "role_card": "command_dispatcher",
        "focus": "只负责拼接与转发可执行命令，不扩散分析结论。",
    },
    "xref_analysis": {
        "role_card": "method_xref_analyst",
        "focus": "只围绕当前方法、子依赖、交叉引用和调用链推进。",
    },
    "rename_mapping": {
        "role_card": "rename_mapper",
        "focus": "只处理旧名/新名映射、替换顺序与命中证据。",
    },
    "rename_tuning": {
        "role_card": "rename_tuning_analyst",
        "focus": "只做命名冲突复核、source 身份校验和微调回流，不直接展开整段去混淆代码。",
    },
    "naming_finalize": {
        "role_card": "naming_finalizer",
        "focus": "只收敛最终命名，不重新发散全局分析。",
    },
    "jadx_code_analyst": {
        "role_card": "jadx_code_analyst",
        "focus": "只根据 JADX 代码语义还原类职责、方法用途和文件名。",
    },
}

TOOLCHAIN_RULES = [
    {
        "tools": {"baksmali_xref"},
        "command_type": "method_xref",
        "toolchain_key": "baksmali_xref_chain",
        "toolchain_stage": "trace_method_refs",
        "flow_phase": "trace",
        "flow_rank": 30,
        "executor": "tool_dispatcher",
        "preferred_profile": "xref_analysis",
    },
    {
        "tools": {"baksmali_subclasses", "subclasses"},
        "command_type": "subclass_scan",
        "toolchain_key": "baksmali_subclasses_chain",
        "toolchain_stage": "trace_inheritance",
        "flow_phase": "inheritance",
        "flow_rank": 20,
        "executor": "tool_dispatcher",
        "preferred_profile": "xref_analysis",
    },
    {
        "tools": {"rename_tuning"},
        "command_type": "rename_tuning",
        "toolchain_key": "rename_tuning_chain",
        "toolchain_stage": "tune_name_assignments",
        "flow_phase": "rename_tuning",
        "flow_rank": 12,
        "executor": "tool_dispatcher",
        "preferred_profile": "rename_tuning",
    },
    {
        "tools": {"jadx_named", "jadx_named_chain"},
        "command_type": "jadx_named_decompile",
        "toolchain_key": "jadx_named_chain",
        "toolchain_stage": "recover_named_java_source",
        "flow_phase": "named_decompile",
        "flow_rank": 11,
        "executor": "tool_dispatcher",
        "preferred_profile": "jadx_code_analyst",
    },
    {
        "tools": {"jadx", "jadx_toolchain"},
        "command_type": "jadx_decompile",
        "toolchain_key": "jadx_source_chain",
        "toolchain_stage": "recover_java_source",
        "flow_phase": "decompile",
        "flow_rank": 10,
        "executor": "tool_dispatcher",
        "preferred_profile": "jadx_code_analyst",
    },
    {
        "tools": {"smali_converter"},
        "command_type": "smali_conversion",
        "toolchain_key": "smali_conversion_chain",
        "toolchain_stage": "convert_representation",
        "flow_phase": "convert",
        "flow_rank": 15,
        "executor": "tool_dispatcher",
        "preferred_profile": "tool_dispatch",
    },
    {
        "tools": {"start_powershell"},
        "command_type": "powershell_probe",
        "toolchain_key": "powershell_probe_chain",
        "toolchain_stage": "run_probe_command",
        "flow_phase": "probe",
        "flow_rank": 5,
        "executor": "tool_dispatcher",
        "preferred_profile": "tool_dispatch",
    },
]


def build_rule_result(rule: Dict[str, Any]) -> Dict[str, str]:
    if not isinstance(rule, dict):
        return {
            "command_type": "generic_command",
            "toolchain_key": "generic_followup_chain",
            "toolchain_stage": "manual_followup",
            "flow_phase": "followup",
            "flow_rank": "50",
            "executor": "tool_dispatcher",
            "preferred_profile": "",
        }
    return {
        "command_type": str(rule.get("command_type", "") or "generic_command"),
        "toolchain_key": str(rule.get("toolchain_key", "") or "generic_followup_chain"),
        "toolchain_stage": str(rule.get("toolchain_stage", "") or "manual_followup"),
        "flow_phase": str(rule.get("flow_phase", "") or "followup"),
        "flow_rank": str(rule.get("flow_rank", 50) or 50),
        "executor": str(rule.get("executor", "") or "tool_dispatcher"),
        "preferred_profile": str(rule.get("preferred_profile", "") or ""),
    }


def rule_by_toolchain_key(toolchain_key: str) -> Dict[str, Any]:
    for rule in TOOLCHAIN_RULES:
        if str(rule.get("toolchain_key", "") or "") == toolchain_key:
            return rule
    return {}


def read_text(path: str) -> str:
    with open(path, "rb") as handle:
        data = handle.read()
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def first_nonempty(*values: Any) -> str:
    return cli.first_nonempty(*values)


def unique_texts(values: List[str], limit: int = 12) -> List[str]:
    return cli.unique_keep_order([value for value in values if value], limit=limit)


def parse_reply_meta_text(raw_text: str) -> Dict[str, Any]:
    payload = cli.decode_jsonish(raw_text, max_depth=5)
    if not isinstance(payload, dict):
        match = cli.CODE_BLOCK_PATTERN.search(raw_text or "")
        if match:
            payload = cli.decode_jsonish(match.group(1).strip(), max_depth=5)
    if not isinstance(payload, dict):
        return {}
    route_entry = payload.get("route_entry", {})
    if not isinstance(route_entry, dict):
        route_entry = {}
    command_items = payload.get("command_items", [])
    if not isinstance(command_items, list):
        command_items = []
    return {
        "conversation_id": str(payload.get("conversation_id", "") or ""),
        "section_id": str(payload.get("section_id", "") or ""),
        "profile": str(payload.get("profile", "") or ""),
        "profile_reason": str(payload.get("profile_reason", "") or ""),
        "reply_text": str(payload.get("reply_text", "") or ""),
        "call_chain_text": str(payload.get("call_chain_text", "") or ""),
        "route_entry": route_entry,
        "command_json": str(payload.get("command_json", "") or ""),
        "command_items": [item for item in command_items if isinstance(item, dict)],
    }


def parse_stream_dump_text(raw_text: str) -> Dict[str, Any]:
    conversation_id = ""
    section_id = ""
    reply_text = ""
    last_snapshot_text = ""
    raw_lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    for line in raw_lines:
        stripped = line.strip()
        if not stripped.startswith("data: "):
            continue
        data_str = stripped[6:]
        if data_str == "[DONE]":
            break
        outer = cli.decode_jsonish(data_str, max_depth=5)
        if not isinstance(outer, dict):
            continue
        event_data = cli.decode_jsonish(outer.get("event_data", ""), max_depth=5)
        if not isinstance(event_data, dict):
            event_data = outer

        conversation_id = first_nonempty(
            conversation_id,
            outer.get("conversation_id", ""),
            event_data.get("conversation_id", ""),
        )
        section_id = first_nonempty(
            section_id,
            outer.get("section_id", ""),
            event_data.get("section_id", ""),
        )
        snapshot_text = cli.extract_snapshot_text_from_event(event_data)
        if snapshot_text and len(snapshot_text) >= len(last_snapshot_text):
            last_snapshot_text = snapshot_text

    if last_snapshot_text:
        reply_text = last_snapshot_text

    return {
        "conversation_id": conversation_id,
        "section_id": section_id,
        "reply_text": reply_text,
    }


def infer_command_type(tool: str, command: str) -> Dict[str, str]:
    lowered_tool = (tool or "").strip().lower()
    lowered_command = (command or "").strip().lower()

    if any(token in lowered_command for token in (
        "{chain_jadx_named_dir}",
        "{chain_jadx_analysis_dir}",
        "chain_jadx_named_dir",
        "chain_jadx_analysis_dir",
        "jadx_named_chain",
        "named_export",
        "analysis_pkg",
    )):
        return build_rule_result(rule_by_toolchain_key("jadx_named_chain"))
    if (
        "naming_assignments" in lowered_command
        or "rename_tuning" in lowered_command
        or "命名微调" in (command or "")
        or "重命名校验" in (command or "")
    ):
        return build_rule_result(rule_by_toolchain_key("rename_tuning_chain"))

    for rule in TOOLCHAIN_RULES:
        if lowered_tool in {item.lower() for item in rule["tools"]}:
            return build_rule_result(rule)

    if " xref " in f" {lowered_command} ":
        return build_rule_result(rule_by_toolchain_key("baksmali_xref_chain"))
    if " subclasses " in f" {lowered_command} ":
        return build_rule_result(rule_by_toolchain_key("baksmali_subclasses_chain"))
    if "jadx" in lowered_command:
        return build_rule_result(rule_by_toolchain_key("jadx_source_chain"))
    return build_rule_result({})


def build_role_plan(profile: str, command_type: str) -> Dict[str, str]:
    profile_key = profile if profile in PROFILE_ROLE_CARD else "tool_dispatch"
    card = PROFILE_ROLE_CARD.get(profile_key, PROFILE_ROLE_CARD["tool_dispatch"])
    return {
        "allocated_profile": profile_key,
        "role_card": card["role_card"],
        "role_focus": card["focus"],
        "slot": ROLE_SLOT_MAP.get(profile_key, "generic_followup"),
        "command_type": command_type,
    }


def build_toolchain_registry() -> Dict[str, str]:
    tools = cli.discover_tool_context()
    return {
        "tool_dispatcher": str(tools.get("tool_dispatcher", "") or ""),
        "jadx_toolchain": str(tools.get("jadx_toolchain", "") or ""),
        "jadx": str(tools.get("jadx", "") or ""),
        "baksmali": str(tools.get("baksmali", "") or ""),
        "smali_converter": str(tools.get("smali_converter", "") or ""),
        "start_powershell": str(tools.get("start_powershell", "") or ""),
    }


def resolve_toolchain_entry(tool: str, registry: Dict[str, str]) -> str:
    lowered_tool = (tool or "").strip().lower()
    if lowered_tool in {"jadx", "jadx_toolchain", "jadx_named", "jadx_named_chain"}:
        return registry.get("jadx_toolchain") or registry.get("jadx")
    if lowered_tool in {"baksmali_xref", "baksmali_subclasses", "subclasses"}:
        return registry.get("baksmali")
    if lowered_tool == "smali_converter":
        return registry.get("smali_converter")
    if lowered_tool == "rename_tuning":
        return registry.get("tool_dispatcher", "")
    if lowered_tool == "start_powershell":
        return registry.get("start_powershell")
    return registry.get("tool_dispatcher", "")


def build_execution_groups(allocations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for item in allocations:
        group_key = "|".join([
            str(item.get("allocated_session_name", "") or ""),
            str(item.get("allocated_profile", "") or ""),
            str(item.get("toolchain_key", "") or ""),
        ])
        if group_key not in groups:
            groups[group_key] = {
                "group_key": group_key,
                "allocated_session_name": str(item.get("allocated_session_name", "") or ""),
                "allocated_profile": str(item.get("allocated_profile", "") or ""),
                "role_card": str(item.get("role_card", "") or ""),
                "toolchain_key": str(item.get("toolchain_key", "") or ""),
                "toolchain_stage": str(item.get("toolchain_stage", "") or ""),
                "executor": str(item.get("executor", "") or ""),
                "toolchain_entry": str(item.get("toolchain_entry", "") or ""),
                "command_indexes": [],
                "tools": [],
                "class_targets": [],
                "method_targets": [],
            }
        group = groups[group_key]
        group["command_indexes"].append(int(item.get("index", 0)))
        group["tools"] = unique_texts(group["tools"] + [str(item.get("tool", "") or "")], limit=20)
        group["class_targets"] = unique_texts(group["class_targets"] + list(item.get("class_targets", [])), limit=20)
        group["method_targets"] = unique_texts(group["method_targets"] + list(item.get("method_targets", [])), limit=20)
    return list(groups.values())


def build_execution_flow(allocations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(
        allocations,
        key=lambda item: (
            int(item.get("flow_rank", 999) or 999),
            int(item.get("index", 0) or 0),
        ),
    )
    flow: List[Dict[str, Any]] = []
    previous_step = None
    for flow_index, item in enumerate(ordered, start=1):
        current_step = {
            "flow_index": flow_index,
            "from_command_index": int(item.get("index", 0) or 0),
            "command_type": str(item.get("command_type", "") or ""),
            "flow_phase": str(item.get("flow_phase", "") or ""),
            "tool": str(item.get("tool", "") or ""),
            "toolchain_key": str(item.get("toolchain_key", "") or ""),
            "toolchain_stage": str(item.get("toolchain_stage", "") or ""),
            "executor": str(item.get("executor", "") or ""),
            "depends_on": [previous_step] if previous_step is not None else [],
            "class_targets": list(item.get("class_targets", [])),
            "method_targets": list(item.get("method_targets", [])),
            "command": str(item.get("command", "") or ""),
            "next_query": str(item.get("next_query", "") or ""),
        }
        flow.append(current_step)
        previous_step = flow_index
    return flow


def persist_takeover_routes(call_chain_text: str, allocations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    stored: List[Dict[str, Any]] = []
    if not call_chain_text:
        return stored
    for item in allocations:
        command_type = str(item.get("command_type", "") or "")
        toolchain_key = str(item.get("toolchain_key", "") or "")
        tool_name = str(item.get("tool", "") or "")
        command_text = str(item.get("command", "") or "")
        if command_type == "generic_command" and not tool_name and not command_text.strip():
            continue
        session_suffix = cli.safe_slug(command_type or toolchain_key or str(item.get("tool", "") or "generic"))
        base_session_name = str(item.get("allocated_session_name", "") or "")
        if "__" not in base_session_name and session_suffix:
            base_session_name = f"{base_session_name}__{session_suffix}"
            item["allocated_session_name"] = base_session_name
        stored_entry = cli.remember_role_takeover_route(
            chain_text=call_chain_text,
            profile=str(item.get("allocated_profile", "") or ""),
            command_type=command_type,
            toolchain_key=toolchain_key,
            session_name=base_session_name,
            conversation_id=str(item.get("conversation_id", "") or ""),
            section_id=str(item.get("section_id", "") or ""),
            role_card=str(item.get("role_card", "") or ""),
            tool=str(item.get("tool", "") or ""),
            toolchain_entry=str(item.get("toolchain_entry", "") or ""),
            executor=str(item.get("executor", "") or ""),
            note="flow_takeover",
        )
        if stored_entry:
            stored.append(stored_entry)
    return stored


def build_allocations(command_items: List[Dict[str, Any]],
                      conversation_id: str,
                      section_id: str,
                      route_entry: Dict[str, Any],
                      default_profile: str = "") -> List[Dict[str, Any]]:
    allocations: List[Dict[str, Any]] = []
    base_session_name = str(route_entry.get("session_name", "") or "")
    route_profile = str(route_entry.get("profile", "") or default_profile or "tool_dispatch")
    registry = build_toolchain_registry()

    for index, item in enumerate(command_items):
        tool = str(item.get("tool", "") or "")
        command = str(item.get("command", "") or "")
        inferred = infer_command_type(tool, command)
        preferred_profile = inferred.get("preferred_profile", "")
        profile = str(preferred_profile or item.get("profile", "") or default_profile or route_profile or "tool_dispatch")
        tool = str(item.get("tool", "") or "")
        hit_class = cli.normalize_class_key(str(item.get("hit_class", "") or ""))
        class_targets = cli.extract_class_targets(
            "\n".join(
                value for value in [
                    hit_class,
                    command,
                    str(item.get("store_back", "") or ""),
                    str(item.get("next_query", "") or ""),
                ] if value
            ),
            limit=10,
        )
        method_targets = cli.extract_method_targets(
            "\n".join(
                value for value in [
                    command,
                    str(item.get("store_back", "") or ""),
                    str(item.get("next_query", "") or ""),
                ] if value
            ),
            limit=10,
        )
        role_plan = build_role_plan(profile, inferred["command_type"])
        session_suffix = cli.safe_slug(inferred["command_type"] or inferred["toolchain_key"] or profile)
        if base_session_name:
            allocated_session = f"{base_session_name}__{session_suffix}" if session_suffix else base_session_name
        else:
            allocated_session = f"{conversation_id or 'local'}__{section_id or 'root'}__{session_suffix or cli.safe_slug(profile)}"

        allocations.append({
            "index": index,
            "group_ref": str(route_entry.get("group_ref", "") or ""),
            "slot_ref": str(route_entry.get("slot_ref", "") or ""),
            "slot": role_plan["slot"],
            "allocated_profile": role_plan["allocated_profile"],
            "role_card": role_plan["role_card"],
            "role_focus": role_plan["role_focus"],
            "allocated_session_name": allocated_session,
            "conversation_id": conversation_id,
            "section_id": section_id,
            "tool": tool,
            "command": command,
            "command_type": inferred["command_type"],
            "toolchain_key": inferred["toolchain_key"],
            "toolchain_stage": inferred["toolchain_stage"],
            "flow_phase": inferred.get("flow_phase", ""),
            "flow_rank": int(inferred.get("flow_rank", "999") or 999),
            "executor": inferred["executor"],
            "toolchain_entry": resolve_toolchain_entry(tool, registry),
            "reason": str(item.get("reason", "") or ""),
            "hit_class": hit_class,
            "hit_store": str(item.get("hit_store", "") or ""),
            "store_back": str(item.get("store_back", "") or ""),
            "next_query": str(item.get("next_query", "") or ""),
            "class_targets": class_targets,
            "method_targets": method_targets,
        })
    return allocations


def merge_sources(reply_meta: Dict[str, Any], stream_dump: Dict[str, Any]) -> Dict[str, Any]:
    route_entry = reply_meta.get("route_entry", {}) if isinstance(reply_meta.get("route_entry", {}), dict) else {}
    conversation_id = first_nonempty(
        reply_meta.get("conversation_id", ""),
        route_entry.get("conversation_id", ""),
        stream_dump.get("conversation_id", ""),
    )
    section_id = first_nonempty(
        reply_meta.get("section_id", ""),
        route_entry.get("section_id", ""),
        stream_dump.get("section_id", ""),
    )
    profile = first_nonempty(
        reply_meta.get("profile", ""),
        route_entry.get("profile", ""),
    )
    profile_reason = first_nonempty(
        reply_meta.get("profile_reason", ""),
        route_entry.get("profile_reason", ""),
    )
    call_chain_text = first_nonempty(
        reply_meta.get("call_chain_text", ""),
        route_entry.get("chain_text", ""),
    )
    reply_text = first_nonempty(
        reply_meta.get("reply_text", ""),
        stream_dump.get("reply_text", ""),
    )
    command_json = str(reply_meta.get("command_json", "") or "")
    command_items: List[Dict[str, Any]] = [item for item in reply_meta.get("command_items", []) if cli.is_command_like_item(item)]
    if not command_json:
        command_json = cli.normalize_command_json(reply_text or "") or ""
    if not command_items and command_json:
        parsed = json.loads(command_json)
        command_items = cli.extract_command_items(parsed)
    allocations = build_allocations(command_items, conversation_id, section_id, route_entry, default_profile=profile)
    stored_takeovers = persist_takeover_routes(call_chain_text, allocations)
    execution_groups = build_execution_groups(allocations)
    execution_flow = build_execution_flow(allocations)
    return {
        "conversation_id": conversation_id,
        "section_id": section_id,
        "profile": profile,
        "profile_reason": profile_reason,
        "call_chain_text": call_chain_text,
        "route_entry": route_entry,
        "reply_text": reply_text,
        "command_json": command_json or "",
        "command_items": command_items,
        "allocations": allocations,
        "stored_takeovers": stored_takeovers,
        "execution_groups": execution_groups,
        "execution_flow": execution_flow,
        "stats": {
            "command_count": len(command_items),
            "profiles": sorted({str(item.get("profile", "") or profile) for item in command_items if isinstance(item, dict)}),
            "tools": sorted({str(item.get("tool", "") or "") for item in command_items if isinstance(item, dict) and item.get("tool")}),
            "toolchain_keys": sorted({str(item.get("toolchain_key", "") or "") for item in allocations if isinstance(item, dict) and item.get("toolchain_key")}),
            "role_cards": sorted({str(item.get("role_card", "") or "") for item in allocations if isinstance(item, dict) and item.get("role_card")}),
        },
    }


def dump_json(data: Dict[str, Any], output_path: str = "") -> str:
    text = json.dumps(data, ensure_ascii=True, indent=2)
    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize chat return payloads and build role/session allocations.")
    parser.add_argument("--reply-meta-file", help="Reply meta JSON file.")
    parser.add_argument("--reply-meta-text", help="Reply meta JSON text.")
    parser.add_argument("--stream-file", help="Raw stream dump file with data: lines.")
    parser.add_argument("--stream-text", help="Raw stream dump text.")
    parser.add_argument("--reply-file", help="Plain reply text file with ```json``` block.")
    parser.add_argument("--reply-text", help="Plain reply text.")
    parser.add_argument("-o", "--output", help="Write normalized output JSON.")
    args = parser.parse_args()

    reply_meta_text = ""
    stream_text = ""
    reply_text = ""
    if args.reply_meta_file:
        reply_meta_text = read_text(args.reply_meta_file)
    elif args.reply_meta_text:
        reply_meta_text = args.reply_meta_text
    if args.stream_file:
        stream_text = read_text(args.stream_file)
    elif args.stream_text:
        stream_text = args.stream_text
    if args.reply_file:
        reply_text = read_text(args.reply_file)
    elif args.reply_text:
        reply_text = args.reply_text

    reply_meta = parse_reply_meta_text(reply_meta_text) if reply_meta_text else {}
    stream_dump = parse_stream_dump_text(stream_text) if stream_text else {}
    if reply_text:
        inferred_meta = parse_reply_meta_text(reply_text)
        if inferred_meta:
            reply_meta = inferred_meta
        else:
            stream_dump = dict(stream_dump)
            stream_dump["reply_text"] = reply_text

    merged = merge_sources(reply_meta, stream_dump)
    print(dump_json(merged, args.output or ""))
    return 0 if merged.get("command_json") else 1


if __name__ == "__main__":
    raise SystemExit(main())
