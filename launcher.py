# -*- coding: utf-8 -*-
"""
企业知识助手 —— 一键启动编排器

设计目标（参照「企业智行 · Corporate Journey Hub」launcher 模板）：
  - 纯 Python 源码（UTF-8），中文由 Python 处理，规避 bat 编码乱码
  - 步骤级失败即停，不再带病启动
  - 端口智能判断：已运行则直接开页面，避免「有时能起有时不能」
  - 后端 detached 后台运行（日志落盘 server.log）
  - 健康检查兜底，自动打开浏览器
  - 保留 stop / restart 子命令（与旧 start.bat 行为兼容）

用法：
  python launcher.py             # 一键启动（已运行则直接开页面）
  python launcher.py stop        # 停止服务
  python launcher.py restart     # 停止后重新启动
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
VENV_DIR = BASE_DIR / ".venv"
REQ_FILE = BASE_DIR / "requirements.txt"
LOG_FILE = BASE_DIR / "server.log"
ENV_FILE = BASE_DIR / ".env"

DEFAULT_PORT = 8008
HEALTH_TIMEOUT = 90  # 健康检查最长轮询时长（秒）
HEALTH_PATH = "/api/health"

IS_WIN = sys.platform == "win32"


def log(msg: str):
    print(msg, flush=True)


def _pause(msg: str):
    """交互终端才暂停等待 Enter；静默/管道/开机自启场景直接跳过。

    这样 `python launcher.py` 在任务计划、VBS 无窗口启动、`echo | python` 等
    非交互环境下不会卡在 input() 上（否则进程会永远挂着等待输入）。
    """
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input(msg)
    except (EOFError, OSError):
        pass


def step(title: str):
    log("")
    log("=" * 56)
    log(title)
    log("=" * 56)


# ---------------------------------------------------------------------------
# .env 解析（轻量：只取启动需要的 KEY=VALUE）
# ---------------------------------------------------------------------------
def read_env() -> dict:
    values: dict[str, str] = {}
    try:
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    return values


def get_port() -> int:
    env = read_env()
    try:
        return int(env.get("PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def is_port_alive(port: int, path: str = HEALTH_PATH, timeout: int = 3) -> bool:
    """检测服务端口是否可访问（HTTP 200）。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=timeout
        ) as r:
            return r.status == 200
    except Exception:
        return False


def run(cmd, cwd=None, check=True, capture=True):
    """统一执行命令；失败抛 RuntimeError 带出输出。"""
    log(f"  > {' '.join(str(c) for c in cmd)}")
    try:
        if capture:
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            if check and proc.returncode != 0:
                out = (proc.stdout or "") + (proc.stderr or "")
                raise RuntimeError(f"命令失败(退出码 {proc.returncode}):\n{out[-2000:]}")
            return proc
        proc = subprocess.run(cmd, cwd=cwd)
        if check and proc.returncode != 0:
            raise RuntimeError(f"命令失败(退出码 {proc.returncode})")
        return proc
    except FileNotFoundError as e:
        raise RuntimeError(f"找不到可执行文件: {cmd[0]} —— {e}")


# ---------------------------------------------------------------------------
# 1. 环境检查
# ---------------------------------------------------------------------------
def check_python() -> str:
    """返回用于创建/复用 venv 的基础解释器。"""
    if sys.version_info < (3, 10):
        raise RuntimeError(f"需要 Python >= 3.10，当前为 {sys.version.split()[0]}")
    log(f"[OK] Python {sys.version.split()[0]}")
    return sys.executable


# ---------------------------------------------------------------------------
# 2. 后端依赖（虚拟环境自动创建）
# ---------------------------------------------------------------------------
def venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if IS_WIN else "bin/python")


def ensure_deps(base_py: str):
    if venv_python().exists():
        log("[OK] 虚拟环境已存在，跳过安装")
        return
    step("2/创建虚拟环境并安装依赖")
    if not REQ_FILE.exists():
        raise RuntimeError(f"未找到依赖文件: {REQ_FILE}")
    run([base_py, "-m", "venv", str(VENV_DIR)])
    run([str(venv_python()), "-m", "pip", "install", "--upgrade", "pip", "-q"])
    run([str(venv_python()), "-m", "pip", "install", "-r", str(REQ_FILE)])
    log("[OK] 后端依赖安装完成")


# ---------------------------------------------------------------------------
# 3. 配置检查
# ---------------------------------------------------------------------------
def check_config() -> int:
    step("3/配置检查")
    env = read_env()
    port = get_port()
    if ENV_FILE.exists():
        has_llm_key = bool(env.get("LLM_API_KEY"))
        log(f"[OK] 已加载 .env（PORT={port}）")
        if has_llm_key:
            log("[OK] 检测到 LLM_API_KEY，将使用真实大模型")
        else:
            log("[提示] 未配置 LLM_API_KEY，将以演示模式运行（全链路离线可用）")
    else:
        log("[提示] 未找到 .env，使用默认配置（演示模式）")
    log(f"[OK] 服务端口: {port}（访问地址 http://127.0.0.1:{port}/）")
    return port


# ---------------------------------------------------------------------------
# 4. 端口预检
# ---------------------------------------------------------------------------
def stack_already_up(port: int) -> bool:
    return is_port_alive(port)


# ---------------------------------------------------------------------------
# 5. 启动后端（detached 后台运行，日志落盘）
# ---------------------------------------------------------------------------
def start_backend(port: int):
    step("4/启动服务（后台）")
    py = str(venv_python())
    logf = open(LOG_FILE, "ab")
    kwargs: dict = {"cwd": str(BASE_DIR), "stdout": logf, "stderr": logf, "stdin": subprocess.DEVNULL}
    if IS_WIN:
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([py, "-m", "serve.main"], **kwargs)
    log(f"  服务启动指令已发出，日志: {LOG_FILE.name}")


# ---------------------------------------------------------------------------
# 6. 健康检查
# ---------------------------------------------------------------------------
def wait_for_health(port: int) -> dict | None:
    step(f"5/健康检查（最长轮询 {HEALTH_TIMEOUT} 秒）")
    deadline = time.time() + HEALTH_TIMEOUT
    while time.time() < deadline:
        if is_port_alive(port, timeout=2):
            log("  [OK] 服务就绪")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=3
                ) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                return None
        time.sleep(2)
    log("  [警告] 超时未就绪，请查看日志最后 30 行：")
    _print_log_tail()
    return None


def _print_log_tail(n: int = 30):
    try:
        if LOG_FILE.exists():
            lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]
            log("  " + "-" * 52)
            for ln in lines:
                log("  " + ln)
            log("  " + "-" * 52)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 7. 打开浏览器
# ---------------------------------------------------------------------------
def open_browser(port: int):
    url = f"http://127.0.0.1:{port}/"
    try:
        webbrowser.open(url)
        log(f"[OK] 已尝试打开浏览器: {url}")
    except Exception as e:  # noqa: BLE001
        log(f"[提示] 无法自动打开浏览器，请手动访问 {url}（{e}）")


# ---------------------------------------------------------------------------
# stop：结束监听目标端口的进程（兼容旧 start.bat stop）
# ---------------------------------------------------------------------------
def stop_service(port: int):
    log(f"停止服务（端口 {port}）...")
    pids = _pids_on_port(port)
    if not pids:
        log("[INFO] 未发现运行中的服务，无需停止。")
        return
    for pid in pids:
        if IS_WIN:
            # 先尝试优雅关闭（不带 /F），给进程收尾机会
            subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True)
        else:
            subprocess.run(["kill", str(pid)], capture_output=True)
    time.sleep(2)
    # 强制清理仍在监听的残留
    for pid in _pids_on_port(port):
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True)
    if _pids_on_port(port):
        log("[警告] 仍有进程占用端口，请手动检查。")
    else:
        log("[OK] 服务已停止。")


def _pids_on_port(port: int) -> list[str]:
    """解析 netstat 输出，返回监听目标端口的 PID 列表（仅 Windows netstat）。

    netstat 输出为本地编码（GBK），统一按字节读取 + errors="ignore" 解码，
    只解析端口与 PID（纯 ASCII），避免控制台编码差异导致解析失败。
    """
    if not IS_WIN:
        return []
    try:
        out_b = subprocess.run(["netstat", "-ano"], capture_output=True).stdout or b""
    except Exception:  # noqa: BLE001
        return []
    out = out_b.decode("utf-8", errors="ignore")
    pids: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(f":{port}"):
            pid = parts[4]
            if pid not in pids and pid != "0":
                pids.append(pid)
    return pids


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    log("企业知识助手 —— 一键启动")
    base_py = ""
    try:
        step("1/检查环境")
        base_py = check_python()

        step("2/后端依赖")
        ensure_deps(base_py)

        port = check_config()

        if stack_already_up(port):
            log("")
            log("[OK] 检测到服务已在运行，直接打开页面。")
            open_browser(port)
            _pause("\n按 Enter 退出（服务继续在后台运行）。")
            return

        start_backend(port)

        health = wait_for_health(port)
        if health:
            log(f"  运行模式: {health.get('mode', 'unknown')} | 版本: {health.get('version', '-')}")

        step("6/打开页面")
        open_browser(port)

        _pause("\n按 Enter 退出（服务继续在后台运行；停止请运行: python launcher.py stop）。")
    except RuntimeError as e:
        log("")
        log("=" * 56)
        log("[启动失败]")
        log(str(e))
        log("=" * 56)
        _print_log_tail()
        _pause("\n按 Enter 关闭窗口。")
        sys.exit(1)
    except KeyboardInterrupt:
        log("\n已取消。")
        sys.exit(0)


def cli():
    port = get_port()
    action = sys.argv[1].strip().lower() if len(sys.argv) > 1 else "start"
    if action == "stop":
        stop_service(port)
    elif action == "restart":
        stop_service(port)
        time.sleep(1)
        main()
    else:
        main()


if __name__ == "__main__":
    cli()
