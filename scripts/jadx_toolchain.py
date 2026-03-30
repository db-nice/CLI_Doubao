#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from glob import glob
from typing import Any, Dict, List, Optional, Tuple


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(SCRIPT_DIR, "tools")
CLASS_HIT_STORE_DIR = os.path.join(SCRIPT_DIR, "class_hit_store")
JAVA_PATH = os.environ.get("JAVA_PATH", "java")
WINDOWS_PATH_PATTERN = re.compile(r"^[A-Za-z]:\\")
CLASS_DECLARATION_PATTERN = re.compile(r"\b(class|interface|enum)\s+([A-Za-z_$][\w$]*)")
PACKAGE_DECLARATION_PATTERN = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;", re.MULTILINE)
ARCHIVE_MANIFEST_NAME = "._archive_manifest.json"


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def shorten_text(text: str, limit: int = 240) -> str:
    value = " ".join((text or "").strip().split())
    if len(value) <= limit:
        return value
    return value[:limit - 3] + "..."


def unique_keep_order(values: List[str], limit: int = 12) -> List[str]:
    result: List[str] = []
    for value in values:
        if not value or value in result:
            continue
        result.append(value)
        if len(result) >= limit:
            break
    return result


def safe_slug(text: str, limit: int = 48) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("._")
    return (cleaned or "item")[:limit]


def find_first_existing(patterns: List[str]) -> str:
    for pattern in patterns:
        for path in sorted(glob(pattern, recursive=True)):
            if os.path.isfile(path):
                return path
    return ""


def run_command(command: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except Exception as exc:
        return False, str(exc)

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        return False, stderr or stdout or f"命令执行失败: {' '.join(command)}"
    return True, stdout or stderr


def discover_jadx_path() -> str:
    return os.environ.get("JADX_PATH") or find_first_existing([
        os.path.join(TOOLS_DIR, "**", "jadx.bat"),
        os.path.join(TOOLS_DIR, "**", "jadx.exe"),
        os.path.join(TOOLS_DIR, "**", "jadx-cli*.jar"),
        os.path.join(TOOLS_DIR, "**", "jadx*.jar"),
    ]) or os.path.join(TOOLS_DIR, "jadx.jar")


def validate_jadx_cli(jadx_path: str) -> Optional[str]:
    if not jadx_path:
        return "未找到 JADX 路径"
    if not os.path.exists(jadx_path):
        return f"未找到 JADX 路径：{jadx_path}"

    lower = jadx_path.lower()
    if lower.endswith((".bat", ".cmd", ".exe")):
        return None
    if not lower.endswith(".jar"):
        return "JADX 路径必须是 jadx.bat、jadx.exe 或 jadx-cli.jar"

    try:
        with zipfile.ZipFile(jadx_path, "r") as archive:
            names = set(archive.namelist())
            if "jadx/cli/JadxCLI.class" in names:
                return None
            manifest = ""
            if "META-INF/MANIFEST.MF" in names:
                manifest = archive.read("META-INF/MANIFEST.MF").decode("utf-8", "ignore")
            if "Main-Class: jadx.cli.JadxCLI" in manifest:
                return None
    except zipfile.BadZipFile:
        return f"JADX 文件不是有效 zip/jar：{jadx_path}"

    return f"当前 JADX 资产不可直接执行：{jadx_path}"


def build_jadx_command(jadx_path: str, input_path: str, output_dir: str) -> List[str]:
    lower = jadx_path.lower()
    if lower.endswith(".jar"):
        return [JAVA_PATH, "-jar", jadx_path, "--config", "none", "-d", output_dir, input_path]
    return [jadx_path, "--config", "none", "-d", output_dir, input_path]


def build_jadx_env() -> Dict[str, str]:
    env = dict(os.environ)
    config_dir = ensure_dir(os.path.join(SCRIPT_DIR, ".jadx_env", "config"))
    cache_dir = ensure_dir(os.path.join(SCRIPT_DIR, ".jadx_env", "cache"))
    tmp_dir = ensure_dir(os.path.join(SCRIPT_DIR, ".jadx_env", "tmp"))
    ensure_dir(os.path.join(config_dir, "plugins", "dropins"))
    env["JADX_CONFIG_DIR"] = config_dir
    env["JADX_CACHE_DIR"] = cache_dir
    env["JADX_TMP_DIR"] = tmp_dir
    return env


def normalize_class_key(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if WINDOWS_PATH_PATTERN.match(raw):
        return ""
    if raw.startswith("L") and raw.endswith(";"):
        return f"L{raw[1:-1].replace('.', '/')};"
    if raw.endswith(".java"):
        raw = raw[:-5]
    if raw.endswith(".smali"):
        raw = raw[:-6]
    raw = raw.replace("\\", "/")
    if raw.startswith("/"):
        raw = raw[1:]
    if raw.startswith("sources/"):
        raw = raw[len("sources/"):]
    if raw.startswith("src/"):
        raw = raw[len("src/"):]
    if "/" in raw and not raw.startswith("L"):
        return f"L{raw};"
    if "." in raw and "/" not in raw:
        return f"L{raw.replace('.', '/')};"
    return raw


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
        json.dump(data, handle, ensure_ascii=True, indent=2)


def file_sha1(path: str) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def read_text_excerpt(path: str, limit: int = 16384) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read(limit)


def load_class_hits(class_key: str) -> Dict[str, Any]:
    normalized = normalize_class_key(class_key)
    if not normalized:
        return {}
    data = load_json_file(class_store_path(normalized))
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
    data["aliases"] = unique_keep_order(list(data.get("aliases", [])), limit=20)
    data["sources"] = list(data.get("sources", []))[-40:]
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
        "fragment": shorten_text(fragment, 1000),
        "tool": tool,
        "command": shorten_text(command, 300),
        "note": shorten_text(note, 200),
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
    data["aliases"] = unique_keep_order(aliases, limit=20)
    return save_class_hits(data)


def extract_snippet(file_path: str, max_lines: int = 40) -> Tuple[str, int]:
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception as exc:
        return f"读取失败: {exc}", 0

    start = 0
    for index, line in enumerate(lines):
        if CLASS_DECLARATION_PATTERN.search(line):
            start = index
            break
    end = min(len(lines), start + max_lines)
    snippet_lines = []
    for index in range(start, end):
        snippet_lines.append(f"{index + 1}: {lines[index].rstrip()}")
    return "\n".join(snippet_lines), start + 1


def convert_descriptor_to_java_name(class_key: str) -> str:
    normalized = normalize_class_key(class_key)
    if normalized.startswith("L") and normalized.endswith(";"):
        return normalized[1:-1].replace("/", ".")
    return normalized


def class_query_candidates(class_query: str) -> Tuple[str, List[str], List[str]]:
    normalized = normalize_class_key(class_query)
    java_name = convert_descriptor_to_java_name(normalized)
    body = normalized[1:-1] if normalized.startswith("L") and normalized.endswith(";") else normalized
    body = body.replace(".", "/")
    relative_candidates: List[str] = []
    if body:
        relative_candidates.extend([
            body + ".java",
            body + ".kt",
            os.path.join("sources", body + ".java"),
            os.path.join("sources", body + ".kt"),
        ])
    basename_candidates = []
    if body:
        basename = body.split("/")[-1]
        basename_candidates.extend([basename + ".java", basename + ".kt"])
    return normalized, unique_keep_order(relative_candidates, limit=8), unique_keep_order(basename_candidates, limit=4)


def find_class_files(root_dir: str, class_query: str) -> List[str]:
    normalized, relative_candidates, basename_candidates = class_query_candidates(class_query)
    matches: List[str] = []

    for candidate in relative_candidates:
        candidate_path = os.path.join(root_dir, candidate)
        if os.path.isfile(candidate_path):
            matches.append(os.path.abspath(candidate_path))

    if matches:
        return unique_keep_order(matches, limit=20)

    for current_root, _, files in os.walk(root_dir):
        for file_name in files:
            if file_name not in basename_candidates:
                continue
            matches.append(os.path.abspath(os.path.join(current_root, file_name)))

    if matches:
        return unique_keep_order(matches, limit=20)

    simple_name = ""
    if normalized.startswith("L") and normalized.endswith(";"):
        simple_name = normalized[1:-1].split("/")[-1]
    elif "." in class_query:
        simple_name = class_query.split(".")[-1]
    else:
        simple_name = os.path.basename(class_query)

    for current_root, _, files in os.walk(root_dir):
        for file_name in files:
            if not file_name.endswith((".java", ".kt")):
                continue
            abs_path = os.path.abspath(os.path.join(current_root, file_name))
            if simple_name and simple_name in file_name:
                matches.append(abs_path)
                continue
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read(4096)
            except Exception:
                continue
            if simple_name and re.search(rf"\b(class|interface|enum)\s+{re.escape(simple_name)}\b", content):
                matches.append(abs_path)

    return unique_keep_order(matches, limit=20)


def relative_match_output_path(decompiled_dir: str, match_path: str) -> str:
    abs_root = os.path.abspath(decompiled_dir)
    abs_match = os.path.abspath(match_path)
    try:
        relative = os.path.relpath(abs_match, abs_root)
    except ValueError:
        relative = os.path.basename(abs_match)
    if relative.startswith(".."):
        relative = os.path.basename(abs_match)
    return relative


def copy_match_to_output(match_path: str, decompiled_dir: str, output_dir: str) -> str:
    relative = relative_match_output_path(decompiled_dir, match_path)
    target_path = os.path.join(output_dir, relative)
    ensure_dir(os.path.dirname(target_path))
    shutil.copyfile(match_path, target_path)
    return os.path.abspath(target_path)


def prepare_output_dir_for_export(output_dir: str) -> Tuple[str, str]:
    abs_output = os.path.abspath(output_dir)
    if os.path.exists(abs_output) and not os.path.isdir(abs_output):
        return "", f"JADX 输出路径不是目录：{abs_output}"

    cached_output_dir = ""
    if os.path.isdir(abs_output):
        try:
            has_entries = any(os.scandir(abs_output))
        except OSError:
            has_entries = False
        if has_entries:
            cache_root = ensure_dir(os.path.join(SCRIPT_DIR, "runtime", "jadx_export_cache"))
            stamp = time.strftime("%Y%m%d_%H%M%S")
            digest = hashlib.sha1(abs_output.encode("utf-8", "ignore")).hexdigest()[:8]
            cache_name = f"{safe_slug(os.path.basename(abs_output) or 'jadx_out')}_{stamp}_{digest}"
            cached_output_dir = os.path.join(cache_root, cache_name)
            try:
                shutil.copytree(abs_output, cached_output_dir)
            except Exception as exc:
                return "", f"缓存旧的 JADX 输出失败：{exc}"
        try:
            shutil.rmtree(abs_output, ignore_errors=True)
        except Exception as exc:
            return cached_output_dir, f"清理旧的 JADX 输出失败：{exc}"

    ensure_dir(abs_output)
    return cached_output_dir, ""


def collect_archive_source_files(input_path: str, sync_package: bool = False) -> Tuple[List[str], str]:
    abs_input = os.path.abspath(input_path)
    if os.path.isfile(abs_input):
        return [abs_input], os.path.dirname(abs_input) or abs_input

    if not os.path.isdir(abs_input):
        return [], abs_input

    walk_root = abs_input
    sources_dir = os.path.join(abs_input, "sources")
    if sync_package and os.path.isdir(sources_dir):
        walk_root = sources_dir

    files: List[str] = []
    for current_root, _, file_names in os.walk(walk_root):
        for file_name in sorted(file_names):
            if file_name == ARCHIVE_MANIFEST_NAME:
                continue
            abs_path = os.path.abspath(os.path.join(current_root, file_name))
            if os.path.isfile(abs_path):
                files.append(abs_path)
    return files, abs_input


def build_archive_signature(source_root: str, source_files: List[str]) -> str:
    digest = hashlib.sha1()
    normalized_root = os.path.abspath(source_root or "")
    digest.update(normalized_root.encode("utf-8", "ignore"))
    for source_path in sorted(source_files):
        abs_path = os.path.abspath(source_path)
        try:
            relative = os.path.relpath(abs_path, normalized_root)
        except ValueError:
            relative = os.path.basename(abs_path)
        digest.update(relative.replace("\\", "/").encode("utf-8", "ignore"))
        digest.update(file_sha1(abs_path).encode("ascii", "ignore"))
    return digest.hexdigest()


def load_archive_manifest(output_dir: str) -> Dict[str, Any]:
    manifest_path = os.path.join(os.path.abspath(output_dir), ARCHIVE_MANIFEST_NAME)
    return load_json_file(manifest_path)


def save_archive_manifest(output_dir: str, data: Dict[str, Any]) -> str:
    manifest_path = os.path.join(os.path.abspath(output_dir), ARCHIVE_MANIFEST_NAME)
    save_json_file(manifest_path, data)
    return manifest_path


def infer_class_key_from_java_source(file_path: str) -> str:
    lower_name = str(file_path or "").lower()
    if not lower_name.endswith((".java", ".kt")):
        return ""
    try:
        excerpt = read_text_excerpt(file_path)
    except Exception:
        return ""
    package_match = PACKAGE_DECLARATION_PATTERN.search(excerpt)
    simple_name = os.path.splitext(os.path.basename(file_path))[0]
    if package_match:
        package_name = str(package_match.group(1) or "").strip()
        if package_name:
            return normalize_class_key(f"L{package_name.replace('.', '/')}/{simple_name};")
    return ""


def refresh_archive_index(index_path: str,
                          input_path: str,
                          output_dir: str,
                          copied_paths: List[str],
                          cached_output_dir: str,
                          source_signature: str,
                          reused_cached_output: bool) -> str:
    abs_index = os.path.abspath(index_path)
    entry = load_json_file(abs_index)
    artifact_paths = [os.path.abspath(path) for path in copied_paths]
    archive_payload = {
        "input": os.path.abspath(input_path),
        "output": os.path.abspath(output_dir),
        "artifact_paths": artifact_paths[:64],
        "count": len(artifact_paths),
        "cached_output_dir": os.path.abspath(cached_output_dir) if cached_output_dir else "",
        "source_signature": source_signature,
        "reused_cached_output": bool(reused_cached_output),
        "updated_at": time.time(),
    }
    history = entry.get("archive_events", [])
    if not isinstance(history, list):
        history = []
    history = [
        item for item in history
        if not (
            isinstance(item, dict)
            and str(item.get("source_signature", "") or "") == source_signature
            and os.path.abspath(str(item.get("output", "") or "")) == os.path.abspath(output_dir)
        )
    ]
    history.insert(0, archive_payload)
    entry["archive_events"] = history[:20]
    entry["analysis_pkg"] = {
        "ready": bool(artifact_paths),
        "output": os.path.abspath(output_dir),
        "artifact_paths": artifact_paths[:64],
        "count": len(artifact_paths),
        "source_signature": source_signature,
        "updated_at": time.time(),
    }
    entry["updated_at"] = time.time()
    save_json_file(abs_index, entry)
    return abs_index


def ensure_decompiled_dir(input_path: str, output_dir: Optional[str], jadx_path: str) -> Tuple[Optional[str], Optional[str], str]:
    abs_input = os.path.abspath(input_path)
    if os.path.isdir(abs_input):
        return abs_input, None, ""

    if not os.path.exists(abs_input):
        return None, None, f"输入不存在：{abs_input}"

    target_output = output_dir
    temp_dir = None
    if not target_output:
        temp_dir = tempfile.mkdtemp(prefix="jadx_toolchain_")
        target_output = os.path.join(temp_dir, "out")

    error = validate_jadx_cli(jadx_path)
    if error:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return None, None, error

    command = build_jadx_command(jadx_path, abs_input, os.path.abspath(target_output))
    ok, output = run_command(command, env=build_jadx_env())
    if not ok:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return None, None, output
    return os.path.abspath(target_output), temp_dir, ""


def cmd_archive_sources(args: argparse.Namespace) -> int:
    input_path = os.path.abspath(args.input)
    output_dir = os.path.abspath(args.output)
    if not os.path.exists(input_path):
        print(json.dumps({"ok": False, "error": f"input_not_found: {input_path}"}, ensure_ascii=True, indent=2))
        return 1

    source_files, source_root = collect_archive_source_files(input_path, sync_package=bool(args.sync_package))
    if not source_files:
        print(json.dumps({"ok": False, "error": f"archive_source_empty: {input_path}"}, ensure_ascii=True, indent=2))
        return 1

    source_signature = build_archive_signature(source_root, source_files)
    cached_output_dir = ""
    copied_paths: List[str] = []
    hit_store_paths: List[str] = []
    reused_cached_output = False
    manifest_path = os.path.join(output_dir, ARCHIVE_MANIFEST_NAME)
    previous_manifest = load_archive_manifest(output_dir) if os.path.isdir(output_dir) else {}

    if (
        previous_manifest
        and os.path.isdir(output_dir)
        and str(previous_manifest.get("source_signature", "") or "") == source_signature
        and int(previous_manifest.get("count", 0) or 0) == len(source_files)
    ):
        expected_rel_paths = [
            str(path or "")
            for path in previous_manifest.get("relative_paths", [])
            if str(path or "")
        ]
        copied_paths = [os.path.abspath(os.path.join(output_dir, relative_path)) for relative_path in expected_rel_paths]
        if copied_paths and all(os.path.exists(path) for path in copied_paths):
            reused_cached_output = True

    if not reused_cached_output:
        cached_output_dir, cache_error = prepare_output_dir_for_export(output_dir)
        if cache_error:
            print(json.dumps({
                "ok": False,
                "error": cache_error,
                "output": output_dir,
                "cached_output_dir": cached_output_dir,
            }, ensure_ascii=True, indent=2))
            return 1

        relative_paths: List[str] = []
        for source_path in source_files:
            try:
                relative_path = os.path.relpath(os.path.abspath(source_path), os.path.abspath(source_root))
            except ValueError:
                relative_path = os.path.basename(source_path)
            if relative_path.startswith(".."):
                relative_path = os.path.basename(source_path)
            target_path = os.path.abspath(os.path.join(output_dir, relative_path))
            ensure_dir(os.path.dirname(target_path))
            shutil.copyfile(source_path, target_path)
            copied_paths.append(target_path)
            relative_paths.append(relative_path.replace("\\", "/"))
            if args.record:
                class_key = infer_class_key_from_java_source(target_path)
                if class_key:
                    hit_store_path = record_class_hit(
                        class_key=class_key,
                        source_path=target_path,
                        fragment="",
                        tool="jadx_toolchain.archive_sources",
                        command=f"archive-sources --input {input_path} --output {output_dir}",
                        note="archive_sources",
                    )
                    if hit_store_path:
                        hit_store_paths.append(hit_store_path)

        manifest_path = save_archive_manifest(output_dir, {
            "input": input_path,
            "source_root": os.path.abspath(source_root),
            "source_signature": source_signature,
            "count": len(copied_paths),
            "relative_paths": relative_paths,
            "updated_at": time.time(),
        })
    elif args.record:
        for copied_path in copied_paths:
            class_key = infer_class_key_from_java_source(copied_path)
            if class_key:
                hit_store_path = record_class_hit(
                    class_key=class_key,
                    source_path=copied_path,
                    fragment="",
                    tool="jadx_toolchain.archive_sources",
                    command=f"archive-sources --input {input_path} --output {output_dir}",
                    note="archive_sources_reuse",
                )
                if hit_store_path:
                    hit_store_paths.append(hit_store_path)

    refreshed_index = ""
    if args.refresh_index:
        refreshed_index = refresh_archive_index(
            args.refresh_index,
            input_path=input_path,
            output_dir=output_dir,
            copied_paths=copied_paths,
            cached_output_dir=cached_output_dir,
            source_signature=source_signature,
            reused_cached_output=reused_cached_output,
        )

    payload = {
        "ok": True,
        "input": input_path,
        "source_root": os.path.abspath(source_root),
        "output": output_dir,
        "manifest_path": manifest_path,
        "cached_output_dir": cached_output_dir,
        "reused_cached_output": reused_cached_output,
        "source_signature": source_signature,
        "count": len(copied_paths),
        "copied_paths": copied_paths,
        "hit_store_paths": unique_keep_order(hit_store_paths, limit=64),
        "refresh_index": refreshed_index,
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if copied_paths or reused_cached_output else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    jadx_path = args.jadx or discover_jadx_path()
    error = validate_jadx_cli(jadx_path)
    jadx_env = build_jadx_env()
    payload: Dict[str, Any] = {
        "jadx_path": jadx_path,
        "class_hit_store": CLASS_HIT_STORE_DIR,
        "jadx_config_dir": jadx_env.get("JADX_CONFIG_DIR", ""),
        "ok": error is None,
    }
    if error is None:
        if jadx_path.lower().endswith((".bat", ".cmd", ".exe")):
            version_command = [jadx_path, "--version"]
        else:
            version_command = [JAVA_PATH, "-jar", jadx_path, "--version"]
        ok, version = run_command(version_command, env=jadx_env)
        payload["version"] = version if ok else ""
        if not ok:
            payload["ok"] = False
            payload["error"] = version
    else:
        payload["error"] = error

    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload.get("ok") else 1


def cmd_decompile(args: argparse.Namespace) -> int:
    jadx_path = args.jadx or discover_jadx_path()
    error = validate_jadx_cli(jadx_path)
    if error:
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=True, indent=2))
        return 1

    output_dir = os.path.abspath(args.output)
    ensure_dir(output_dir)
    command = build_jadx_command(jadx_path, os.path.abspath(args.input), output_dir)
    ok, output = run_command(command, env=build_jadx_env())
    payload = {
        "ok": ok,
        "tool": "jadx_toolchain",
        "jadx_path": jadx_path,
        "input": os.path.abspath(args.input),
        "output": output_dir,
        "command": " ".join(command),
        "message": output,
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if ok else 1


def cmd_find_class(args: argparse.Namespace) -> int:
    jadx_path = args.jadx or discover_jadx_path()
    decompiled_dir, temp_dir, error = ensure_decompiled_dir(args.input, args.output, jadx_path)
    if error:
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=True, indent=2))
        return 1

    try:
        matches = find_class_files(decompiled_dir or "", args.class_query)
        normalized = normalize_class_key(args.class_query)
        result_matches: List[Dict[str, Any]] = []

        for match in matches[: args.limit]:
            snippet, start_line = extract_snippet(match, max_lines=args.max_lines)
            record_path = ""
            if args.record:
                record_path = record_class_hit(
                    class_key=normalized or args.class_query,
                    source_path=match,
                    fragment=snippet,
                    tool="jadx_toolchain.find_class",
                    command=f"find-class --input {args.input} --class {args.class_query}",
                    note="find_class",
                )
            result_matches.append({
                "path": match,
                "class_key": normalized or args.class_query,
                "line_start": start_line,
                "snippet": snippet if args.with_snippet else "",
                "hit_store": record_path,
            })

        payload = {
            "ok": bool(result_matches),
            "input": os.path.abspath(args.input),
            "decompiled_dir": decompiled_dir,
            "class_query": args.class_query,
            "normalized_class": normalized,
            "hit": bool(result_matches),
            "count": len(result_matches),
            "matches": result_matches,
        }
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0 if result_matches else 1
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def cmd_export_classes(args: argparse.Namespace) -> int:
    jadx_path = args.jadx or discover_jadx_path()
    decompiled_dir, temp_dir, error = ensure_decompiled_dir(args.input, None, jadx_path)
    if error:
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=True, indent=2))
        return 1

    output_dir = os.path.abspath(args.output)
    cached_output_dir, cache_error = prepare_output_dir_for_export(output_dir)
    if cache_error:
        print(json.dumps({"ok": False, "error": cache_error, "output": output_dir}, ensure_ascii=True, indent=2))
        return 1
    class_queries = unique_keep_order(
        [str(value or "").strip() for value in args.class_query if str(value or "").strip()],
        limit=24,
    )

    try:
        class_results: List[Dict[str, Any]] = []
        exported_paths: List[str] = []

        for class_query in class_queries:
            normalized = normalize_class_key(class_query)
            matches = find_class_files(decompiled_dir or "", class_query)
            result_matches: List[Dict[str, Any]] = []

            for match in matches[: args.limit_per_class]:
                snippet, start_line = extract_snippet(match, max_lines=args.max_lines)
                export_path = copy_match_to_output(match, decompiled_dir or "", output_dir)
                record_path = ""
                if args.record:
                    record_path = record_class_hit(
                        class_key=normalized or class_query,
                        source_path=export_path,
                        fragment=snippet,
                        tool="jadx_toolchain.export_classes",
                        command=f"export-classes --input {args.input} --class {class_query}",
                        note="export_classes",
                    )
                result_matches.append({
                    "path": match,
                    "export_path": export_path,
                    "class_key": normalized or class_query,
                    "line_start": start_line,
                    "snippet": snippet if args.with_snippet else "",
                    "hit_store": record_path,
                })
                exported_paths.append(export_path)

            class_results.append({
                "class_query": class_query,
                "normalized_class": normalized,
                "count": len(result_matches),
                "matches": result_matches,
            })

        payload = {
            "ok": any(item.get("count", 0) for item in class_results),
            "input": os.path.abspath(args.input),
            "decompiled_dir": decompiled_dir,
            "output": output_dir,
            "cached_output_dir": cached_output_dir,
            "class_queries": class_queries,
            "count": len(exported_paths),
            "exported_paths": exported_paths,
            "classes": class_results,
        }
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0 if payload.get("ok") else 1
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def cmd_record_hit(args: argparse.Namespace) -> int:
    fragment = args.text or ""
    if args.file:
        with open(args.file, "r", encoding="utf-8", errors="replace") as handle:
            fragment = handle.read()

    path = record_class_hit(
        class_key=args.class_query,
        source_path=args.source or "",
        fragment=fragment,
        tool=args.tool,
        command=args.command or "",
        alias=args.alias or "",
        note=args.note or "",
    )
    payload = {
        "ok": bool(path),
        "class_key": normalize_class_key(args.class_query),
        "hit_store": path,
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if path else 1


def cmd_show_hit(args: argparse.Namespace) -> int:
    data = load_class_hits(args.class_query)
    payload = data or {"class_key": normalize_class_key(args.class_query), "hit_count": 0, "sources": []}
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if payload.get("sources") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JADX 工具链执行脚本")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    doctor = subparsers.add_parser("doctor", help="检查 JADX 可执行状态")
    doctor.add_argument("--jadx", help="指定 jadx.bat / jadx.exe / jadx-cli.jar")
    doctor.set_defaults(func=cmd_doctor)

    decompile = subparsers.add_parser("decompile", help="执行 JADX 反编译")
    decompile.add_argument("-i", "--input", required=True, help="dex/apk/jar 输入路径")
    decompile.add_argument("-o", "--output", required=True, help="输出目录")
    decompile.add_argument("--jadx", help="指定 jadx.bat / jadx.exe / jadx-cli.jar")
    decompile.set_defaults(func=cmd_decompile)

    find_class = subparsers.add_parser("find-class", help="检索 JADX 输出中的类并可记录命中")
    find_class.add_argument("-i", "--input", required=True, help="dex/apk/jar 或已反编译目录")
    find_class.add_argument("-c", "--class-query", required=True, help="类描述符、Java类名或相对路径")
    find_class.add_argument("-o", "--output", help="如果输入不是目录，则反编译输出到此目录")
    find_class.add_argument("--jadx", help="指定 jadx.bat / jadx.exe / jadx-cli.jar")
    find_class.add_argument("--record", action="store_true", help="命中后写入 class_hit_store")
    find_class.add_argument("--with-snippet", action="store_true", help="输出命中片段")
    find_class.add_argument("--max-lines", type=int, default=40, help="片段最大行数")
    find_class.add_argument("--limit", type=int, default=5, help="最多返回多少个命中")
    find_class.set_defaults(func=cmd_find_class)

    record = subparsers.add_parser("record-hit", help="手动写入类命中")
    record.add_argument("-c", "--class-query", required=True, help="类描述符或类名")
    record.add_argument("-s", "--source", help="命中来源路径")
    record.add_argument("-t", "--text", help="命中片段文本")
    record.add_argument("-f", "--file", help="从文件读取命中片段")
    record.add_argument("--tool", default="jadx_toolchain.manual", help="来源工具名")
    record.add_argument("--command", help="记录触发命令")
    record.add_argument("--alias", help="附加别名")
    record.add_argument("--note", help="附加备注")
    record.set_defaults(func=cmd_record_hit)

    show = subparsers.add_parser("show-hit", help="查看类命中档案")
    show.add_argument("-c", "--class-query", required=True, help="类描述符或类名")
    show.set_defaults(func=cmd_show_hit)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JADX toolchain helper")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    doctor = subparsers.add_parser("doctor", help="check whether JADX is runnable")
    doctor.add_argument("--jadx", help="path to jadx.bat / jadx.exe / jadx-cli.jar")
    doctor.set_defaults(func=cmd_doctor)

    decompile = subparsers.add_parser("decompile", help="run full JADX decompile")
    decompile.add_argument("-i", "--input", required=True, help="input dex/apk/jar path")
    decompile.add_argument("-o", "--output", required=True, help="output directory")
    decompile.add_argument("--jadx", help="path to jadx.bat / jadx.exe / jadx-cli.jar")
    decompile.set_defaults(func=cmd_decompile)

    find_class = subparsers.add_parser("find-class", help="find matching classes inside JADX output")
    find_class.add_argument("-i", "--input", required=True, help="input dex/apk/jar or existing JADX output directory")
    find_class.add_argument("-c", "--class-query", required=True, help="class descriptor, Java class name, or relative path")
    find_class.add_argument("-o", "--output", help="temporary/output directory when input is not already decompiled")
    find_class.add_argument("--jadx", help="path to jadx.bat / jadx.exe / jadx-cli.jar")
    find_class.add_argument("--record", action="store_true", help="record hits into class_hit_store")
    find_class.add_argument("--with-snippet", action="store_true", help="include matched source snippet")
    find_class.add_argument("--max-lines", type=int, default=40, help="maximum snippet lines")
    find_class.add_argument("--limit", type=int, default=5, help="maximum match count")
    find_class.set_defaults(func=cmd_find_class)

    export_classes = subparsers.add_parser("export-classes", help="export only matched classes into the target directory")
    export_classes.add_argument("-i", "--input", required=True, help="input dex/apk/jar or existing JADX output directory")
    export_classes.add_argument("-o", "--output", required=True, help="output directory; existing content is cached then replaced")
    export_classes.add_argument("-c", "--class-query", action="append", required=True, help="repeatable class descriptor or Java class name")
    export_classes.add_argument("--jadx", help="path to jadx.bat / jadx.exe / jadx-cli.jar")
    export_classes.add_argument("--record", action="store_true", help="record hits into class_hit_store")
    export_classes.add_argument("--with-snippet", action="store_true", help="include matched source snippet")
    export_classes.add_argument("--max-lines", type=int, default=80, help="maximum snippet lines")
    export_classes.add_argument("--limit-per-class", type=int, default=2, help="maximum exported matches per class")
    export_classes.set_defaults(func=cmd_export_classes)

    archive_sources = subparsers.add_parser("archive-sources", help="copy the selected named JADX sources into analysis_pkg with cache reuse")
    archive_sources.add_argument("-i", "--input", required=True, help="named_export directory or copied source root")
    archive_sources.add_argument("-o", "--output", required=True, help="analysis_pkg output directory")
    archive_sources.add_argument("--sync-package", "--mirror-package", dest="sync_package", action="store_true", help="prefer the input sources subtree and preserve the package directory layout")
    archive_sources.add_argument("--refresh-index", "--update-index", dest="refresh_index", help="update the call-chain index file after archiving")
    archive_sources.add_argument("--record", action="store_true", help="record copied Java classes into class_hit_store when possible")
    archive_sources.set_defaults(func=cmd_archive_sources)

    record = subparsers.add_parser("record-hit", help="manually write a class hit record")
    record.add_argument("-c", "--class-query", required=True, help="class descriptor or Java class name")
    record.add_argument("-s", "--source", help="source path of the hit")
    record.add_argument("-t", "--text", help="matched text fragment")
    record.add_argument("-f", "--file", help="read matched text fragment from file")
    record.add_argument("--tool", default="jadx_toolchain.manual", help="source tool name")
    record.add_argument("--command", help="trigger command")
    record.add_argument("--alias", help="extra alias name")
    record.add_argument("--note", help="extra note")
    record.set_defaults(func=cmd_record_hit)

    show = subparsers.add_parser("show-hit", help="show stored class hit records")
    show.add_argument("-c", "--class-query", required=True, help="class descriptor or Java class name")
    show.set_defaults(func=cmd_show_hit)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
