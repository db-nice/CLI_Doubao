import subprocess
import threading
import time
import os
import signal
import json
from typing import Dict, Tuple, Optional

# -------------------------- 内部工具函数：进程终止 --------------------------
def _kill_process_tree(pid: int) -> bool:
    """内部函数：终止进程及其所有子进程（仅Windows）"""
    try:
        subprocess.call(['taskkill', '/F', '/T', '/PID', str(pid)], 
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception as e:
        print(f"[辅助信息] 终止进程失败（PID:{pid}）：{str(e)}")
        try:
            os.kill(pid, signal.SIGTERM)
        except:
            pass
        return False

# -------------------------- 核心分析函数（外部调用入口） --------------------------
_process_cache: Dict[int, dict] = {}
_cache_lock = threading.Lock()

def run_powershell_analysis(
    script_content: str,          # 【必传】待检查的PowerShell脚本字符串
    timeout: int = 10,            # 【可选】超时时间（秒），默认10秒
    encoding: str = "gbk",        # 【可选】输出编码，Windows默认gbk，PS7+可用utf-8
    severity_level: str = "Error,Warning"  # 【可选】检查级别：Error/Warning/Information，默认检查错误+警告
) -> Tuple[Optional[list], Optional[str], bool, int]:
    """
    外部调用核心函数：执行PowerShell脚本检查
    :param script_content: 待检查的PowerShell脚本字符串（必传）
    :param timeout: 超时时间（秒），默认10秒
    :param encoding: 输出编码，Windows默认gbk
    :param severity_level: 检查级别，默认"Error,Warning"（仅检查错误和警告）
    :return: (解析后的检查结果列表, 致命错误信息, 是否超时, 状态码status)
             status=0：无致命问题；status=1：有致命问题
    """
    # 初始化状态
    analysis_result = []
    fatal_error = None
    is_timeout = False
    status = 0

    # 1. 封装PowerShell命令（动态传入检查级别）
    ps_command = f"""
Import-Module PSScriptAnalyzer -Force -ErrorAction SilentlyContinue
if (-not (Get-Command Invoke-ScriptAnalyzer -ErrorAction SilentlyContinue)) {{
    Write-Error "PSScriptAnalyzer模块未安装，请以管理员身份运行PowerShell执行：Install-Module -Name PSScriptAnalyzer -Force -Scope CurrentUser"
    exit 1
}}
$scriptContent = @'
{script_content}
'@
# 执行检查，空结果返回[]
$analysisResult = Invoke-ScriptAnalyzer -ScriptDefinition $scriptContent -Severity {severity_level}
if ($analysisResult) {{ $analysisResult | ConvertTo-Json -Compress }} else {{ '[]' }}
    """.strip()

    # 2. 启动PowerShell进程
    try:
        process = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-Command", ps_command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
            encoding=encoding,
            errors="ignore"
        )
    except Exception as e:
        fatal_error = f"启动PowerShell失败：{str(e)}"
        status = 1
        return analysis_result, fatal_error, is_timeout, status

    # 3. 缓存进程信息
    with _cache_lock:
        _process_cache[process.pid] = {
            "process": process,
            "create_time": time.time(),
            "timeout": timeout
        }

    # 4. 执行并捕获结果
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        
        # 处理PowerShell执行错误
        if stderr.strip():
            fatal_error = f"PowerShell执行错误：{stderr.strip()}"
            status = 1
        
        # 解析检查结果
        if stdout.strip():
            try:
                analysis_result = json.loads(stdout.strip())
            except json.JSONDecodeError as e:
                fatal_error = f"解析检查结果失败：{str(e)} | 原始输出：{stdout}"
                status = 1
    except subprocess.TimeoutExpired:
        fatal_error = f"PowerShell执行超时（超时时间：{timeout}秒）"
        is_timeout = True
        status = 1
        _kill_process_tree(process.pid)
    finally:
        # 确保进程终止，清理缓存
        if process.poll() is None:
            _kill_process_tree(process.pid)
        with _cache_lock:
            if process.pid in _process_cache:
                del _process_cache[process.pid]

    return analysis_result, fatal_error, is_timeout, status

# -------------------------- 辅助函数：格式化检查报告 --------------------------
def format_analysis_report(analysis_result: list) -> str:
    """
    外部调用辅助函数：将检查结果格式化为易读的中文报告
    :param analysis_result: run_powershell_analysis返回的检查结果列表
    :return: 格式化后的报告字符串
    """
    if not analysis_result:
        return "✅ 脚本无语法错误/规范问题"
    
    report = "\n🔍 脚本检查结果（非致命问题）：\n"
    severity_map = {0: "错误", 1: "警告", 2: "信息"}
    for idx, issue in enumerate(analysis_result, 1):
        severity = severity_map.get(issue.get("Severity"), "未知")
        rule = issue.get("RuleName", "未知规则")
        message = issue.get("Message", "无描述")
        line = issue.get("Line", 0)
        column = issue.get("Column", 0)
        report += f"\n{idx}. [{severity}] 行{line}列{column} - {rule}\n   描述：{message}"
    return report

# -------------------------- 模块自测代码（仅在直接运行本文件时执行） --------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("📌 模块自测：直接运行ps_analyzer.py")
    print("=" * 60)

    # 自测脚本
    test_script = """
$num = 10
if ($num -eq 10) {
    Write-Host "数字是10"
}
$unusedVar = 123  # 未使用的变量
    """.strip()

    # 调用核心函数
    analysis_result, fatal_error, is_timeout, status = run_powershell_analysis(
        script_content=test_script,
        timeout=5,
        severity_level="Error,Warning"
    )

    # 输出结果
    print(f"\n📌 运行结束 | 状态码status：{status} | 是否超时：{is_timeout}")
    if fatal_error:
        print(f"\n❌ 致命错误日志：\n{fatal_error}")
    else:
        print(format_analysis_report(analysis_result))
    print("=" * 60)