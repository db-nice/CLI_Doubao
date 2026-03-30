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
from typing import Dict, List, Optional, Tuple


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


def iter_tool_dir_candidates() -> List[str]:
    candidates: List[str] = []
    _append_unique_bootstrap_path(candidates, os.path.join(APP_DIR, "tools"))
    _append_unique_bootstrap_path(candidates, os.path.join(SCRIPT_DIR, "tools"))
    return candidates


TOOL_DIR_CANDIDATES = iter_tool_dir_candidates()
TOOLS_DIR = TOOL_DIR_CANDIDATES[0] if TOOL_DIR_CANDIDATES else os.path.join(APP_DIR, "tools")
CONFIG_PATH = os.path.join(APP_DIR, "tool_dispatcher_config.json")
WORKSPACE_ROOT = os.environ.get("SMALI_MASTER_ROOT", "")
JAVA_PATH = os.environ.get("JAVA_PATH", "java")


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


def find_first_existing(patterns: List[str]) -> str:
    for pattern in patterns:
        for path in sorted(glob(pattern, recursive=True)):
            if os.path.isfile(path):
                return path
    return ""


def load_runtime_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def normalize_root(path: str) -> str:
    if not path:
        return ""
    return _normalize_bootstrap_path(path)


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def iter_workspace_candidates() -> List[str]:
    config = load_runtime_config()
    raw_candidates: List[str] = []

    explicit_root = str(WORKSPACE_ROOT or "").strip()
    if explicit_root:
        raw_candidates.append(explicit_root)

    config_root = str(config.get("workspace_root", "") or "").strip()
    if config_root:
        raw_candidates.append(config_root)

    target_dex = str(config.get("target_dex", "") or "").strip()
    if target_dex:
        current = os.path.abspath(target_dex)
        current = current if os.path.isdir(current) else os.path.dirname(current)
        for _ in range(10):
            if not current:
                break
            raw_candidates.append(current)
            parent = os.path.dirname(current)
            if not parent or parent == current:
                break
            current = parent

    current = os.getcwd()
    while current:
        raw_candidates.append(current)
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        current = parent
    raw_candidates.append(APP_DIR)
    raw_candidates.append(SCRIPT_DIR)

    results: List[str] = []
    seen: set[str] = set()

    def add_candidate(path: str) -> None:
        normalized = normalize_root(path)
        lowered = normalized.lower()
        if not normalized or lowered in seen:
            return
        seen.add(lowered)
        results.append(normalized)

    for candidate in raw_candidates:
        normalized = normalize_root(candidate)
        if not normalized:
            continue
        child_root = os.path.join(normalized, "smali-master")
        if os.path.isdir(child_root):
            add_candidate(child_root)
        add_candidate(normalized)
    return results


def guess_workspace_root() -> str:
    explicit = str(WORKSPACE_ROOT or "").strip()
    if explicit:
        return explicit
    for candidate in iter_workspace_candidates():
        if os.path.isdir(os.path.join(candidate, "baksmali")) or os.path.isdir(os.path.join(candidate, "smali")):
            return candidate
    candidates = iter_workspace_candidates()
    return candidates[0] if candidates else SCRIPT_DIR


WORKSPACE_ROOT = guess_workspace_root()


DEFAULT_SMALI_JAR_PATH = os.environ.get("SMALI_JAR_PATH") or find_latest_file([
    *[os.path.join(root, "smali", "build", "libs", "smali-*-fat.jar") for root in iter_workspace_candidates()],
    os.path.join(SCRIPT_DIR, "smali.jar"),
])
DEFAULT_BAKSMALI_JAR_PATH = os.environ.get("BAKSMALI_JAR_PATH") or find_latest_file([
    *[os.path.join(root, "baksmali", "build", "libs", "baksmali-*-fat.jar") for root in iter_workspace_candidates()],
    os.path.join(SCRIPT_DIR, "baksmali.jar"),
])
DEFAULT_JADX_PATH = os.environ.get("JADX_PATH") or find_first_existing([
    *[
        pattern
        for tool_dir in TOOL_DIR_CANDIDATES
        for pattern in (
            os.path.join(tool_dir, "**", "jadx.bat"),
            os.path.join(tool_dir, "**", "jadx.exe"),
            os.path.join(tool_dir, "**", "jadx-cli*.jar"),
            os.path.join(tool_dir, "**", "jadx*.jar"),
        )
    ],
]) or os.path.join(TOOLS_DIR, "jadx.jar")
DEFAULT_D8_PATH = os.environ.get("D8_PATH", "")
DEFAULT_DX_PATH = os.environ.get("DX_PATH", "")
SMALI_METHOD_SOURCE_PATTERN = re.compile(r"^(L[^;]+;)->([^\(\s]+)(\([^)]*\).+)$")
SMALI_FIELD_SOURCE_PATTERN = re.compile(r"^(L[^;]+;)->([^:\s]+)(:.+)$")


def run_command(command: List[str], cwd: Optional[str] = None) -> Tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except Exception as exc:
        return False, str(exc)

    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    if result.returncode != 0:
        return False, error or output or f"命令执行失败: {' '.join(command)}"
    return True, output or error


def convert_class_name(input_str: str, target_format: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        if target_format == "java":
            if not input_str.startswith("L") or not input_str.endswith(";"):
                return None, "无效的Smali类名格式（需以L开头、;结尾，如 Lcom/example/Test;）"
            return input_str[1:-1].replace("/", "."), None
        if target_format == "smali":
            if "/" in input_str or input_str.startswith("L") or input_str.endswith(";"):
                return None, "无效的Java类名格式（需为 com.example.Test 形式）"
            return f"L{input_str.replace('.', '/')};", None
        return None, "目标格式仅支持 'smali' 或 'java'"
    except Exception as exc:
        return None, f"类名转换失败：{exc}"


def looks_like_smali_code(text: str) -> bool:
    return any(token in text for token in [".class", ".super", ".method", "invoke-", "const/"])


def looks_like_java_code(text: str) -> bool:
    return any(token in text for token in [" class ", " interface ", " enum ", "package ", "import "])


def looks_like_smali_descriptor(text: str) -> bool:
    return text.startswith("L") and text.endswith(";") and "/" in text


def extract_smali_descriptor(smali_code: str) -> Optional[str]:
    match = re.search(r"^\s*\.class\b[^\n]*\s(L[^;]+;)", smali_code, re.MULTILINE)
    return match.group(1) if match else None


def extract_java_file_spec(java_code: str) -> Tuple[str, str]:
    package_match = re.search(r"^\s*package\s+([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*)\s*;", java_code, re.MULTILINE)
    type_match = re.search(r"\b(public\s+)?(class|interface|enum)\s+([A-Za-z_]\w*)", java_code)
    package_name = package_match.group(1) if package_match else ""
    class_name = type_match.group(3) if type_match else "Temp"
    return package_name, class_name


def ensure_smali_source(input_str: str, temp_dir: str) -> Tuple[Optional[str], Optional[str]]:
    if os.path.exists(input_str):
        path = os.path.abspath(input_str)
        if os.path.isdir(path) or path.lower().endswith(".smali"):
            return path, None
        return None, f"输入路径不是smali目录或.smali文件：{path}"

    if not looks_like_smali_code(input_str):
        return None, "输入不是有效的Smali代码，无法转换为Java"

    descriptor = extract_smali_descriptor(input_str) or "LTemp;"
    relative_path = os.path.join(*descriptor[1:-1].split("/")) + ".smali"
    smali_file = os.path.join(temp_dir, relative_path)
    os.makedirs(os.path.dirname(smali_file), exist_ok=True)
    with open(smali_file, "w", encoding="utf-8") as handle:
        handle.write(input_str)
    return smali_file, None


def ensure_java_source(input_str: str, temp_dir: str) -> Tuple[Optional[str], Optional[str]]:
    if os.path.exists(input_str):
        path = os.path.abspath(input_str)
        if os.path.isdir(path) or path.lower().endswith(".java"):
            return path, None
        return None, f"输入路径不是Java目录或.java文件：{path}"

    if not looks_like_java_code(input_str):
        return None, "输入不是有效的Java代码，无法转换为Smali"

    package_name, class_name = extract_java_file_spec(input_str)
    relative_dir = package_name.replace(".", os.sep) if package_name else ""
    java_dir = os.path.join(temp_dir, relative_dir)
    os.makedirs(java_dir, exist_ok=True)
    java_file = os.path.join(java_dir, f"{class_name}.java")
    with open(java_file, "w", encoding="utf-8") as handle:
        handle.write(input_str)
    return java_file, None


def assemble_smali_to_dex(smali_source: str, dex_output: str, smali_jar_path: str) -> Optional[str]:
    if not smali_jar_path or not os.path.exists(smali_jar_path):
        return "未找到可用的smali fat jar，请先构建 smali-master 或设置 SMALI_JAR_PATH"

    ok, output = run_command([JAVA_PATH, "-jar", smali_jar_path, "assemble", "-o", dex_output, smali_source])
    if not ok:
        return f"smali assemble 失败：{output}"
    return None


def disassemble_to_smali(input_path: str, smali_output_dir: str, baksmali_jar_path: str) -> Optional[str]:
    if not baksmali_jar_path or not os.path.exists(baksmali_jar_path):
        return "未找到可用的baksmali fat jar，请先构建 smali-master 或设置 BAKSMALI_JAR_PATH"

    ok, output = run_command([JAVA_PATH, "-jar", baksmali_jar_path, "disassemble", "-o", smali_output_dir, input_path])
    if not ok:
        return f"baksmali disassemble 失败：{output}"
    return None


def validate_jadx_cli(jadx_path: str) -> Optional[str]:
    if not jadx_path:
        return "未配置 JADX 路径，请通过 --jadx 指定 jadx.bat / jadx.exe / jadx-cli.jar"
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
        return f"JADX 文件不是有效zip/jar：{jadx_path}"

    return (
        f"当前 JADX 资产不可直接执行：{jadx_path}。"
        "它不是标准的 jadx CLI 包，请改用真实的 jadx.bat、jadx.exe 或 jadx-cli.jar"
    )


def run_jadx(input_path: str, output_dir: str, jadx_path: str) -> Optional[str]:
    error = validate_jadx_cli(jadx_path)
    if error:
        return error

    lower = jadx_path.lower()
    if lower.endswith(".jar"):
        command = [JAVA_PATH, "-jar", jadx_path, "-d", output_dir, input_path]
    else:
        command = [jadx_path, "-d", output_dir, input_path]

    ok, output = run_command(command)
    if not ok:
        return f"JADX 执行失败：{output}"
    return None


def resolve_d8_or_dx(d8_path: str, dx_path: str) -> Tuple[str, str]:
    if d8_path:
        return "d8", d8_path
    if dx_path:
        return "dx", dx_path

    d8_candidate = shutil.which("d8") or shutil.which("d8.bat")
    if d8_candidate:
        return "d8", d8_candidate

    dx_candidate = shutil.which("dx") or shutil.which("dx.bat")
    if dx_candidate:
        return "dx", dx_candidate

    return "", ""


def build_dex_from_java(java_source: str, dex_output: str, d8_path: str, dx_path: str, temp_dir: str) -> Optional[str]:
    source_path, error = ensure_java_source(java_source, temp_dir)
    if error:
        return error

    class_dir = os.path.join(temp_dir, "classes")
    os.makedirs(class_dir, exist_ok=True)

    javac_inputs = [source_path]
    if os.path.isdir(source_path):
        javac_inputs = []
        for root, _, files in os.walk(source_path):
            for file_name in files:
                if file_name.endswith(".java"):
                    javac_inputs.append(os.path.join(root, file_name))
        if not javac_inputs:
            return f"Java目录中未找到.java文件：{source_path}"

    ok, output = run_command(["javac", "-encoding", "UTF-8", "-d", class_dir] + javac_inputs)
    if not ok:
        return f"javac 编译失败：{output}"

    tool_type, tool_path = resolve_d8_or_dx(d8_path, dx_path)
    if not tool_path:
        return "未找到 d8/dx，请通过 --d8 或 --dx 指定 Android build-tools 路径后再执行 Java -> Smali"

    if tool_type == "d8":
        d8_out_dir = os.path.join(temp_dir, "d8_out")
        os.makedirs(d8_out_dir, exist_ok=True)
        ok, output = run_command([tool_path, "--output", d8_out_dir, class_dir])
        if not ok:
            return f"d8 转 dex 失败：{output}"
        generated_dex = os.path.join(d8_out_dir, "classes.dex")
        if not os.path.exists(generated_dex):
            return "d8 执行完成但未生成 classes.dex"
        shutil.copyfile(generated_dex, dex_output)
        return None

    ok, output = run_command([tool_path, "--dex", "--output", dex_output, class_dir])
    if not ok:
        return f"dx 转 dex 失败：{output}"
    return None


def first_file_with_suffix(root_dir: str, suffix: str) -> Optional[str]:
    matches: List[str] = []
    for root, _, files in os.walk(root_dir):
        for file_name in files:
            if file_name.endswith(suffix):
                matches.append(os.path.join(root, file_name))
    if not matches:
        return None
    matches.sort()
    return matches[0]


def copy_output_tree(src_dir: str, output_path: str) -> str:
    output_path = os.path.abspath(output_path)
    if os.path.exists(output_path):
        if os.path.isdir(output_path):
            shutil.rmtree(output_path)
        else:
            os.remove(output_path)
    shutil.copytree(src_dir, output_path)
    return output_path


def descriptor_to_smali_relpath(descriptor: str) -> str:
    return os.path.join(*descriptor[1:-1].split("/")) + ".smali"


def has_smali_files(root_dir: str) -> bool:
    if not root_dir or not os.path.isdir(root_dir):
        return False
    return first_file_with_suffix(root_dir, ".smali") is not None


def normalize_symbol_name(raw_value: str) -> str:
    value = str(raw_value or "").strip().strip("`\"'")
    if not value:
        return ""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value):
        return ""
    return value


def backup_tree_snapshot(path: str, label: str = "cache") -> str:
    absolute_path = os.path.abspath(path)
    if not os.path.isdir(absolute_path):
        return ""
    parent = os.path.dirname(absolute_path)
    base_name = os.path.basename(absolute_path.rstrip("\\/"))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    digest = hashlib.sha1(absolute_path.encode("utf-8", "ignore")).hexdigest()[:8]
    backup_path = os.path.join(parent, f"{base_name}.{label}.{stamp}_{digest}")
    suffix = 1
    while os.path.exists(backup_path):
        backup_path = os.path.join(parent, f"{base_name}.{label}.{stamp}_{digest}_{suffix:02d}")
        suffix += 1
    shutil.copytree(absolute_path, backup_path)
    return backup_path


def build_target_class_descriptor(source_descriptor: str, candidate_name: str) -> str:
    descriptor_body = source_descriptor[1:-1]
    package_part, _, _ = descriptor_body.rpartition("/")
    target_body = f"{package_part}/{candidate_name}" if package_part else candidate_name
    return f"L{target_body};"


def parse_class_rename_specs(values: List[str]) -> Tuple[List[Dict[str, str]], Optional[str]]:
    specs: List[Dict[str, str]] = []
    seen_sources: set[str] = set()
    for raw_value in values:
        source_value, separator, candidate_value = str(raw_value or "").partition("=")
        source_descriptor = source_value.strip()
        candidate_name = normalize_symbol_name(candidate_value)
        if not separator or not looks_like_smali_descriptor(source_descriptor):
            return [], f"无效的类重命名参数: {raw_value}"
        if not candidate_name:
            return [], f"无效的类新名称: {raw_value}"
        source_descriptor = source_descriptor.strip()
        if source_descriptor in seen_sources:
            continue
        seen_sources.add(source_descriptor)
        specs.append({
            "source": source_descriptor,
            "candidate": candidate_name,
            "target": build_target_class_descriptor(source_descriptor, candidate_name),
        })
    return specs, None


def parse_method_rename_specs(values: List[str], class_map: Dict[str, str]) -> Tuple[List[Dict[str, str]], Optional[str]]:
    specs: List[Dict[str, str]] = []
    seen_sources: set[str] = set()
    for raw_value in values:
        source_value, separator, candidate_value = str(raw_value or "").partition("=")
        source_descriptor = source_value.strip()
        candidate_name = normalize_symbol_name(candidate_value)
        match = SMALI_METHOD_SOURCE_PATTERN.match(source_descriptor)
        if not separator or not match:
            return [], f"无效的方法重命名参数: {raw_value}"
        if not candidate_name:
            return [], f"无效的方法新名称: {raw_value}"
        if source_descriptor in seen_sources:
            continue
        seen_sources.add(source_descriptor)
        owner_descriptor, old_name, proto = match.groups()
        specs.append({
            "source": source_descriptor,
            "candidate": candidate_name,
            "owner": owner_descriptor,
            "resolved_owner": class_map.get(owner_descriptor, owner_descriptor),
            "old_name": old_name,
            "proto": proto,
        })
    return specs, None


def parse_field_rename_specs(values: List[str], class_map: Dict[str, str]) -> Tuple[List[Dict[str, str]], Optional[str]]:
    specs: List[Dict[str, str]] = []
    seen_sources: set[str] = set()
    for raw_value in values:
        source_value, separator, candidate_value = str(raw_value or "").partition("=")
        source_descriptor = source_value.strip()
        candidate_name = normalize_symbol_name(candidate_value)
        match = SMALI_FIELD_SOURCE_PATTERN.match(source_descriptor)
        if not separator or not match:
            return [], f"无效的字段重命名参数: {raw_value}"
        if not candidate_name:
            return [], f"无效的字段新名称: {raw_value}"
        if source_descriptor in seen_sources:
            continue
        seen_sources.add(source_descriptor)
        owner_descriptor, old_name, field_type = match.groups()
        specs.append({
            "source": source_descriptor,
            "candidate": candidate_name,
            "owner": owner_descriptor,
            "resolved_owner": class_map.get(owner_descriptor, owner_descriptor),
            "old_name": old_name,
            "field_type": field_type,
        })
    return specs, None


def validate_rename_plan(root_dir: str,
                         class_specs: List[Dict[str, str]],
                         method_specs: List[Dict[str, str]],
                         field_specs: List[Dict[str, str]]) -> Optional[str]:
    seen_class_targets: Dict[str, str] = {}
    for spec in class_specs:
        target_descriptor = str(spec.get("target", "") or "")
        source_descriptor = str(spec.get("source", "") or "")
        existing_source = seen_class_targets.get(target_descriptor, "")
        if existing_source and existing_source != source_descriptor:
            return f"不同类映射到了同一个新类名: {existing_source} / {source_descriptor} -> {target_descriptor}"
        seen_class_targets[target_descriptor] = source_descriptor
        source_path = os.path.join(root_dir, descriptor_to_smali_relpath(source_descriptor))
        target_path = os.path.join(root_dir, descriptor_to_smali_relpath(target_descriptor))
        if os.path.exists(target_path) and os.path.abspath(target_path) != os.path.abspath(source_path):
            return f"目标类文件已存在，无法覆盖: {target_path}"

    seen_method_targets: Dict[str, str] = {}
    for spec in method_specs:
        target_key = "|".join([
            str(spec.get("resolved_owner", "") or ""),
            str(spec.get("candidate", "") or ""),
            str(spec.get("proto", "") or ""),
        ])
        source_descriptor = str(spec.get("source", "") or "")
        existing_source = seen_method_targets.get(target_key, "")
        if existing_source and existing_source != source_descriptor:
            return f"不同方法映射到了同一个新方法名: {existing_source} / {source_descriptor}"
        seen_method_targets[target_key] = source_descriptor

    seen_field_targets: Dict[str, str] = {}
    for spec in field_specs:
        target_key = "|".join([
            str(spec.get("resolved_owner", "") or ""),
            str(spec.get("candidate", "") or ""),
            str(spec.get("field_type", "") or ""),
        ])
        source_descriptor = str(spec.get("source", "") or "")
        existing_source = seen_field_targets.get(target_key, "")
        if existing_source and existing_source != source_descriptor:
            return f"不同字段映射到了同一个新字段名: {existing_source} / {source_descriptor}"
        seen_field_targets[target_key] = source_descriptor
    return None


def prepare_smali_workspace(input_path: str,
                            output_dir: str,
                            baksmali_jar_path: str) -> Tuple[Optional[str], bool, bool, Optional[str]]:
    absolute_input = os.path.abspath(input_path)
    absolute_output = os.path.abspath(output_dir)
    os.makedirs(absolute_output, exist_ok=True)
    reused_existing_output = has_smali_files(absolute_output)
    decoded_from_input = False
    if reused_existing_output:
        return absolute_output, decoded_from_input, reused_existing_output, None

    if os.path.isdir(absolute_input):
        if not has_smali_files(absolute_input):
            return None, False, False, f"输入目录中未找到 .smali 文件: {absolute_input}"
        if os.path.abspath(absolute_input) != absolute_output:
            if os.path.exists(absolute_output):
                shutil.rmtree(absolute_output, ignore_errors=True)
            shutil.copytree(absolute_input, absolute_output)
        return absolute_output, decoded_from_input, False, None

    if not os.path.isfile(absolute_input) or not absolute_input.lower().endswith((".dex", ".apk", ".jar")):
        return None, False, False, f"输入必须是 dex/apk/jar 或已有的 smali 目录: {absolute_input}"

    if os.path.exists(absolute_output):
        shutil.rmtree(absolute_output, ignore_errors=True)
    os.makedirs(absolute_output, exist_ok=True)
    error = disassemble_to_smali(absolute_input, absolute_output, baksmali_jar_path)
    if error:
        return None, False, False, error
    decoded_from_input = True
    return absolute_output, decoded_from_input, False, None


def apply_rename_plan(root_dir: str,
                      class_specs: List[Dict[str, str]],
                      method_specs: List[Dict[str, str]],
                      field_specs: List[Dict[str, str]]) -> Tuple[List[str], List[Dict[str, str]]]:
    class_replacements = [
        (str(spec.get("source", "") or ""), str(spec.get("target", "") or ""))
        for spec in class_specs
        if spec.get("source") and spec.get("target")
    ]
    class_replacements.sort(key=lambda item: len(item[0]), reverse=True)
    rename_paths = {
        os.path.abspath(os.path.join(root_dir, descriptor_to_smali_relpath(str(spec.get("source", "") or "")))): os.path.abspath(
            os.path.join(root_dir, descriptor_to_smali_relpath(str(spec.get("target", "") or "")))
        )
        for spec in class_specs
        if spec.get("source") and spec.get("target")
    }

    entries: List[Dict[str, str]] = []
    for current_root, _, files in os.walk(root_dir):
        for file_name in files:
            if not file_name.lower().endswith(".smali"):
                continue
            old_path = os.path.abspath(os.path.join(current_root, file_name))
            with open(old_path, "r", encoding="utf-8") as handle:
                original_text = handle.read()
            updated_text = original_text
            for source_descriptor, target_descriptor in class_replacements:
                updated_text = updated_text.replace(source_descriptor, target_descriptor)
            entries.append({
                "old_path": old_path,
                "new_path": rename_paths.get(old_path, old_path),
                "original_text": original_text,
                "text": updated_text,
            })

    for entry in entries:
        updated_text = str(entry.get("text", "") or "")
        for spec in method_specs:
            owner_descriptor = str(spec.get("resolved_owner", "") or "")
            old_name = str(spec.get("old_name", "") or "")
            proto = str(spec.get("proto", "") or "")
            candidate_name = str(spec.get("candidate", "") or "")
            if owner_descriptor and old_name and proto and candidate_name:
                updated_text = updated_text.replace(
                    f"{owner_descriptor}->{old_name}{proto}",
                    f"{owner_descriptor}->{candidate_name}{proto}",
                )
                original_owner = str(spec.get("owner", "") or "")
                if original_owner and original_owner != owner_descriptor:
                    updated_text = updated_text.replace(
                        f"{original_owner}->{old_name}{proto}",
                        f"{owner_descriptor}->{candidate_name}{proto}",
                    )
        for spec in field_specs:
            owner_descriptor = str(spec.get("resolved_owner", "") or "")
            old_name = str(spec.get("old_name", "") or "")
            field_type = str(spec.get("field_type", "") or "")
            candidate_name = str(spec.get("candidate", "") or "")
            if owner_descriptor and old_name and field_type and candidate_name:
                updated_text = updated_text.replace(
                    f"{owner_descriptor}->{old_name}{field_type}",
                    f"{owner_descriptor}->{candidate_name}{field_type}",
                )
                original_owner = str(spec.get("owner", "") or "")
                if original_owner and original_owner != owner_descriptor:
                    updated_text = updated_text.replace(
                        f"{original_owner}->{old_name}{field_type}",
                        f"{owner_descriptor}->{candidate_name}{field_type}",
                    )
        entry["text"] = updated_text

    entry_by_new_path: Dict[str, Dict[str, str]] = {
        os.path.abspath(str(entry.get("new_path", "") or "")): entry
        for entry in entries
    }
    for spec in method_specs:
        owner_descriptor = str(spec.get("resolved_owner", "") or "")
        owner_path = os.path.abspath(os.path.join(root_dir, descriptor_to_smali_relpath(owner_descriptor)))
        entry = entry_by_new_path.get(owner_path)
        if not entry:
            continue
        pattern = re.compile(
            rf"(^\s*\.method\b[^\n]*\s){re.escape(str(spec.get('old_name', '') or ''))}(?={re.escape(str(spec.get('proto', '') or ''))})",
            re.MULTILINE,
        )
        entry["text"] = pattern.sub(rf"\1{str(spec.get('candidate', '') or '')}", str(entry.get("text", "") or ""))

    for spec in field_specs:
        owner_descriptor = str(spec.get("resolved_owner", "") or "")
        owner_path = os.path.abspath(os.path.join(root_dir, descriptor_to_smali_relpath(owner_descriptor)))
        entry = entry_by_new_path.get(owner_path)
        if not entry:
            continue
        pattern = re.compile(
            rf"(^\s*\.field\b[^\n]*\s){re.escape(str(spec.get('old_name', '') or ''))}(?={re.escape(str(spec.get('field_type', '') or ''))})",
            re.MULTILINE,
        )
        entry["text"] = pattern.sub(rf"\1{str(spec.get('candidate', '') or '')}", str(entry.get("text", "") or ""))

    changed_paths: List[str] = []
    renamed_paths: List[Dict[str, str]] = []
    for entry in entries:
        old_path = os.path.abspath(str(entry.get("old_path", "") or ""))
        new_path = os.path.abspath(str(entry.get("new_path", "") or old_path))
        original_text = str(entry.get("original_text", "") or "")
        updated_text = str(entry.get("text", "") or "")
        ensure_dir(os.path.dirname(new_path))
        with open(new_path, "w", encoding="utf-8") as handle:
            handle.write(updated_text)
        if old_path != new_path and os.path.exists(old_path):
            os.remove(old_path)
            renamed_paths.append({
                "from": old_path,
                "to": new_path,
            })
        if old_path != new_path or updated_text != original_text:
            changed_paths.append(new_path)

    return sorted(set(changed_paths)), renamed_paths


def cmd_apply_renames(args: argparse.Namespace) -> int:
    output_dir = os.path.abspath(args.output)
    working_dir, decoded_from_input, reused_existing_output, error = prepare_smali_workspace(
        input_path=args.input,
        output_dir=output_dir,
        baksmali_jar_path=args.baksmali_jar,
    )
    if error or not working_dir:
        print(json.dumps({"ok": False, "error": error or "prepare_smali_workspace_failed"}, ensure_ascii=True, indent=2))
        return 1

    class_specs, error = parse_class_rename_specs(list(args.class_rename or []))
    if error:
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=True, indent=2))
        return 1
    class_map = {
        str(spec.get("source", "") or ""): str(spec.get("target", "") or "")
        for spec in class_specs
        if spec.get("source") and spec.get("target")
    }
    method_specs, error = parse_method_rename_specs(list(args.method_rename or []), class_map)
    if error:
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=True, indent=2))
        return 1
    field_specs, error = parse_field_rename_specs(list(args.field_rename or []), class_map)
    if error:
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=True, indent=2))
        return 1

    if not class_specs and not method_specs and not field_specs:
        print(json.dumps({"ok": False, "error": "未提供任何重命名计划"}, ensure_ascii=True, indent=2))
        return 1

    validation_error = validate_rename_plan(working_dir, class_specs, method_specs, field_specs)
    if validation_error:
        print(json.dumps({"ok": False, "error": validation_error}, ensure_ascii=True, indent=2))
        return 1

    backup_dir = backup_tree_snapshot(working_dir, label="rename_cache")
    changed_paths, renamed_paths = apply_rename_plan(
        root_dir=working_dir,
        class_specs=class_specs,
        method_specs=method_specs,
        field_specs=field_specs,
    )

    rebuilt_dex = ""
    if args.rebuilt_dex:
        rebuilt_dex = os.path.abspath(args.rebuilt_dex)
        ensure_dir(os.path.dirname(rebuilt_dex))
        error = assemble_smali_to_dex(working_dir, rebuilt_dex, args.smali_jar)
        if error:
            print(json.dumps({
                "ok": False,
                "error": error,
                "output": working_dir,
                "backup_dir": backup_dir,
            }, ensure_ascii=True, indent=2))
            return 1

    payload = {
        "ok": True,
        "input": os.path.abspath(args.input),
        "output": working_dir,
        "backup_dir": backup_dir,
        "decoded_from_input": decoded_from_input,
        "reused_existing_output": reused_existing_output,
        "rebuilt_dex": rebuilt_dex,
        "class_renames": class_specs,
        "method_renames": method_specs,
        "field_renames": field_specs,
        "changed_count": len(changed_paths),
        "changed_paths": changed_paths[:128],
        "renamed_paths": renamed_paths[:64],
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def convert_code(
    input_str: str,
    target_format: str,
    output_path: Optional[str] = None,
    jadx_path: str = DEFAULT_JADX_PATH,
    smali_jar_path: str = DEFAULT_SMALI_JAR_PATH,
    baksmali_jar_path: str = DEFAULT_BAKSMALI_JAR_PATH,
    d8_path: str = DEFAULT_D8_PATH,
    dx_path: str = DEFAULT_DX_PATH,
) -> Tuple[Optional[str], Optional[str]]:
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            if target_format == "java":
                input_path = input_str if os.path.exists(input_str) else ""
                if input_path:
                    abs_input_path = os.path.abspath(input_path)
                    lower = abs_input_path.lower()
                    if lower.endswith(".java"):
                        with open(abs_input_path, "r", encoding="utf-8") as handle:
                            return handle.read(), None
                    if os.path.isdir(abs_input_path) or lower.endswith(".smali"):
                        dex_input = os.path.join(temp_dir, "classes.dex")
                        error = assemble_smali_to_dex(abs_input_path, dex_input, smali_jar_path)
                        if error:
                            return None, error
                        input_for_jadx = dex_input
                    elif lower.endswith((".dex", ".apk", ".jar")):
                        input_for_jadx = abs_input_path
                    else:
                        return None, f"无法识别的输入路径类型：{abs_input_path}"
                else:
                    smali_source, error = ensure_smali_source(input_str, temp_dir)
                    if error:
                        return None, error
                    dex_input = os.path.join(temp_dir, "classes.dex")
                    error = assemble_smali_to_dex(smali_source, dex_input, smali_jar_path)
                    if error:
                        return None, error
                    input_for_jadx = dex_input

                jadx_output_dir = os.path.join(temp_dir, "jadx_out")
                error = run_jadx(input_for_jadx, jadx_output_dir, jadx_path)
                if error:
                    return None, error

                if output_path:
                    copied_dir = copy_output_tree(jadx_output_dir, output_path)
                    return f"Java源码目录已输出：{copied_dir}", None

                java_file = first_file_with_suffix(jadx_output_dir, ".java")
                if not java_file:
                    return None, "JADX 执行完成但未生成 .java 文件"
                with open(java_file, "r", encoding="utf-8") as handle:
                    return handle.read(), None

            if target_format == "smali":
                input_path = input_str if os.path.exists(input_str) else ""
                if input_path:
                    abs_input_path = os.path.abspath(input_path)
                    lower = abs_input_path.lower()
                    if os.path.isdir(abs_input_path):
                        has_java = first_file_with_suffix(abs_input_path, ".java") is not None
                        has_smali = first_file_with_suffix(abs_input_path, ".smali") is not None
                        if has_smali and not has_java:
                            if output_path:
                                copied_dir = copy_output_tree(abs_input_path, output_path)
                                return f"Smali目录已输出：{copied_dir}", None
                            smali_file = first_file_with_suffix(abs_input_path, ".smali")
                            if not smali_file:
                                return None, f"Smali目录中未找到.smali文件：{abs_input_path}"
                            with open(smali_file, "r", encoding="utf-8") as handle:
                                return handle.read(), None
                        if has_java:
                            dex_input = os.path.join(temp_dir, "classes.dex")
                            error = build_dex_from_java(abs_input_path, dex_input, d8_path, dx_path, temp_dir)
                            if error:
                                return None, error
                            input_for_baksmali = dex_input
                        else:
                            return None, f"目录中既没有.java也没有.smali文件：{abs_input_path}"
                    elif lower.endswith(".smali"):
                        with open(abs_input_path, "r", encoding="utf-8") as handle:
                            return handle.read(), None
                    elif lower.endswith((".dex", ".apk", ".jar")):
                        input_for_baksmali = abs_input_path
                    elif lower.endswith(".java"):
                        dex_input = os.path.join(temp_dir, "classes.dex")
                        error = build_dex_from_java(abs_input_path, dex_input, d8_path, dx_path, temp_dir)
                        if error:
                            return None, error
                        input_for_baksmali = dex_input
                    else:
                        return None, f"无法识别的输入路径类型：{abs_input_path}"
                else:
                    if looks_like_smali_code(input_str):
                        return input_str, None
                    if not looks_like_java_code(input_str):
                        return None, "输入既不是有效Java代码，也不是已存在的dex/java路径"
                    dex_input = os.path.join(temp_dir, "classes.dex")
                    error = build_dex_from_java(input_str, dex_input, d8_path, dx_path, temp_dir)
                    if error:
                        return None, error
                    input_for_baksmali = dex_input

                smali_output_dir = os.path.join(temp_dir, "smali_out")
                error = disassemble_to_smali(input_for_baksmali, smali_output_dir, baksmali_jar_path)
                if error:
                    return None, error

                if output_path:
                    copied_dir = copy_output_tree(smali_output_dir, output_path)
                    return f"Smali目录已输出：{copied_dir}", None

                smali_file = first_file_with_suffix(smali_output_dir, ".smali")
                if not smali_file:
                    return None, "baksmali 执行完成但未生成 .smali 文件"
                with open(smali_file, "r", encoding="utf-8") as handle:
                    return handle.read(), None

            return None, "目标格式仅支持 'smali' 或 'java'"
        except Exception as exc:
            return None, f"代码转换失败：{exc}"


def convert(
    input_str: str,
    target_format: str,
    mode: str = "auto",
    output_path: Optional[str] = None,
    jadx_path: str = DEFAULT_JADX_PATH,
    smali_jar_path: str = DEFAULT_SMALI_JAR_PATH,
    baksmali_jar_path: str = DEFAULT_BAKSMALI_JAR_PATH,
    d8_path: str = DEFAULT_D8_PATH,
    dx_path: str = DEFAULT_DX_PATH,
) -> Tuple[Optional[str], Optional[str]]:
    if mode == "auto":
        if looks_like_smali_descriptor(input_str) or (
            "." in input_str and not os.path.exists(input_str) and not looks_like_smali_code(input_str) and not looks_like_java_code(input_str)
        ):
            mode = "class"
        else:
            mode = "code"

    if mode == "class":
        return convert_class_name(input_str, target_format)
    if mode == "code":
        return convert_code(
            input_str=input_str,
            target_format=target_format,
            output_path=output_path,
            jadx_path=jadx_path,
            smali_jar_path=smali_jar_path,
            baksmali_jar_path=baksmali_jar_path,
            d8_path=d8_path,
            dx_path=dx_path,
        )
    return None, "模式仅支持 'auto' / 'class' / 'code'"


def run_legacy_cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Smali <-> Java 杞崲宸ュ叿")
    parser.add_argument("-i", "--input", required=True, help="杈撳叆瀛楃涓层€乨ex/java/smali鏂囦欢璺緞锛屾垨鐩綍璺緞")
    parser.add_argument("-to", "--target", required=True, choices=["smali", "java"], help="鐩爣鏍煎紡锛歴mali 鎴?java")
    parser.add_argument("-m", "--mode", default="auto", choices=["auto", "class", "code"], help="杞崲妯″紡锛歛uto/class/code")
    parser.add_argument("-o", "--output", help="杈撳嚭鏂囦欢澶硅矾寰勶紱鐢ㄤ簬浠ｇ爜杞崲鏃朵細杈撳嚭瀹屾暣鐩綍")
    parser.add_argument("--jadx", default=DEFAULT_JADX_PATH, help="jadx.bat / jadx.exe / jadx-cli.jar 璺緞")
    parser.add_argument("--smali-jar", default=DEFAULT_SMALI_JAR_PATH, help="smali fat jar 璺緞")
    parser.add_argument("--baksmali-jar", default=DEFAULT_BAKSMALI_JAR_PATH, help="baksmali fat jar 璺緞")
    parser.add_argument("--d8", default=DEFAULT_D8_PATH, help="d8 鎴?d8.bat 璺緞")
    parser.add_argument("--dx", default=DEFAULT_DX_PATH, help="dx 鎴?dx.bat 璺緞")
    args = parser.parse_args(argv)

    result, error = convert(
        input_str=args.input,
        target_format=args.target,
        mode=args.mode,
        output_path=args.output,
        jadx_path=args.jadx,
        smali_jar_path=args.smali_jar,
        baksmali_jar_path=args.baksmali_jar,
        d8_path=args.d8,
        dx_path=args.dx,
    )

    print("=" * 60)
    if error:
        print(f"杞崲澶辫触锛歕n{error}")
        print("=" * 60)
        return 1
    print(f"杞崲鎴愬姛锛歕n{result}")
    print("=" * 60)
    return 0


def run_apply_renames_cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Apply selected rename assignments on a smali workspace")
    parser.add_argument("-i", "--input", required=True, help="dex/apk/jar or existing smali directory")
    parser.add_argument("-o", "--output", required=True, help="working smali directory")
    parser.add_argument("--class-rename", action="append", default=[], help="class rename mapping: Lxx/yy;=NewName")
    parser.add_argument("--method-rename", action="append", default=[], help="method rename mapping: Lxx/yy;->a()V=NewName")
    parser.add_argument("--field-rename", action="append", default=[], help="field rename mapping: Lxx/yy;->a:I=NewName")
    parser.add_argument("--rebuilt-dex", help="optional rebuilt dex output after rename")
    parser.add_argument("--smali-jar", default=DEFAULT_SMALI_JAR_PATH, help="smali fat jar path")
    parser.add_argument("--baksmali-jar", default=DEFAULT_BAKSMALI_JAR_PATH, help="baksmali fat jar path")
    args = parser.parse_args(argv)
    return cmd_apply_renames(args)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "apply-renames":
        return run_apply_renames_cli(argv[1:])
    return run_legacy_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
    parser = argparse.ArgumentParser(description="Smali <-> Java 转换工具")
    parser.add_argument("-i", "--input", required=True, help="输入字符串、dex/java/smali文件路径，或目录路径")
    parser.add_argument("-to", "--target", required=True, choices=["smali", "java"], help="目标格式：smali 或 java")
    parser.add_argument("-m", "--mode", default="auto", choices=["auto", "class", "code"], help="转换模式：auto/class/code")
    parser.add_argument("-o", "--output", help="输出文件夹路径；用于代码转换时会输出完整目录")
    parser.add_argument("--jadx", default=DEFAULT_JADX_PATH, help="jadx.bat / jadx.exe / jadx-cli.jar 路径")
    parser.add_argument("--smali-jar", default=DEFAULT_SMALI_JAR_PATH, help="smali fat jar 路径")
    parser.add_argument("--baksmali-jar", default=DEFAULT_BAKSMALI_JAR_PATH, help="baksmali fat jar 路径")
    parser.add_argument("--d8", default=DEFAULT_D8_PATH, help="d8 或 d8.bat 路径")
    parser.add_argument("--dx", default=DEFAULT_DX_PATH, help="dx 或 dx.bat 路径")
    args = parser.parse_args()

    result, error = convert(
        input_str=args.input,
        target_format=args.target,
        mode=args.mode,
        output_path=args.output,
        jadx_path=args.jadx,
        smali_jar_path=args.smali_jar,
        baksmali_jar_path=args.baksmali_jar,
        d8_path=args.d8,
        dx_path=args.dx,
    )

    print("=" * 60)
    if error:
        print(f"转换失败：\n{error}")
    else:
        print(f"转换成功：\n{result}")
    print("=" * 60)
