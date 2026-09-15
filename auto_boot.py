from __future__ import annotations

import argparse
import base64
import ctypes
import datetime as dt
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def get_app_dir() -> Path:
    """获取程序所在目录（兼容源码运行与 PyInstaller 打包为 exe 的场景）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_NAME = "BootRunner"
APP_DIR = get_app_dir()
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "boot-runner.log"
HOLIDAY_CACHE_DIR = APP_DIR / "holidays"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TASK_NAME = "BootRunner Startup"
UI_RUN_VALUE = f"{APP_NAME} UI"
HOLIDAY_DATA_URLS = (
    "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json",
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json",
)
DEFAULT_CONFIG: dict[str, Any] = {
    "programs": [],
    "start_time": "00:00",
    "cutoff_time": "18:30",
    "startup_delay": 10,
    "check_workday": True,
    "run_when_offline": True,
}
SERVICE_START_TIMEOUT = 15.0
SERVICE_POLL_INTERVAL = 0.5


def load_config(path: Path = CONFIG_FILE) -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                config.update(saved)
        except (OSError, json.JSONDecodeError):
            pass
    return validate_config(config)


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    raw_programs = config.get("programs", [])
    if not isinstance(raw_programs, list):
        raw_programs = []
    programs: list[dict[str, str]] = []
    for item in raw_programs:
        if isinstance(item, str):
            path = item.strip()
            service_name = ""
        elif isinstance(item, dict):
            raw_path = item.get("path", "")
            raw_service_name = item.get("service_name", "")
            path = raw_path.strip() if isinstance(raw_path, str) else ""
            service_name = (
                raw_service_name.strip() if isinstance(raw_service_name, str) else ""
            )
        else:
            continue
        if path:
            programs.append({"path": path, "service_name": service_name})

    def valid_time(key: str) -> str:
        value = str(config.get(key, DEFAULT_CONFIG[key]))
        try:
            dt.datetime.strptime(value, "%H:%M")
            return value
        except ValueError:
            return DEFAULT_CONFIG[key]

    start_time = valid_time("start_time")
    cutoff = valid_time("cutoff_time")

    try:
        delay = min(600, max(0, int(config.get("startup_delay", 10))))
    except (TypeError, ValueError):
        delay = 10

    return {
        "programs": programs,
        "start_time": start_time,
        "cutoff_time": cutoff,
        "startup_delay": delay,
        "check_workday": bool(config.get("check_workday", True)),
        "run_when_offline": bool(config.get("run_when_offline", True)),
    }


def save_config(config: dict[str, Any], path: Path = CONFIG_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.with_suffix(".tmp")
    target.write_text(
        json.dumps(validate_config(config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    target.replace(path)


def get_logger(path: Path = LOG_FILE) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"{APP_NAME}:{path}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
    return logger


def fetch_json(url: str, timeout: float = 5) -> Any:
    request = Request(url, headers={"User-Agent": f"{APP_NAME}/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_timor_day_type(date: dt.date) -> tuple[int, str]:
    url = f"https://timor.tech/api/holiday/info/{date.isoformat()}"
    payload = fetch_json(url)
    day = payload["type"]
    return int(day["type"]), str(day["name"])


def day_type_from_year_data(date: dt.date, payload: Any) -> tuple[int, str]:
    if not isinstance(payload, dict) or payload.get("year") != date.year:
        raise ValueError("年度节假日数据年份无效")
    days = payload.get("days")
    if not isinstance(days, list):
        raise ValueError("年度节假日数据格式无效")
    for item in days:
        if isinstance(item, dict) and item.get("date") == date.isoformat():
            name = str(item.get("name") or "调休")
            return (2, name) if item.get("isOffDay") is True else (3, f"{name}调休")
    if date.weekday() < 5:
        return 0, "工作日"
    return 1, "周六" if date.weekday() == 5 else "周日"


def read_holiday_cache(date: dt.date, cache_dir: Path) -> Any | None:
    path = cache_dir / f"{date.year}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        day_type_from_year_data(date, payload)
        return payload
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def write_holiday_cache(year: int, payload: Any, cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{year}.json"
    target = path.with_suffix(".tmp")
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    target.replace(path)


def fetch_day_type(
    date: dt.date, cache_dir: Path = HOLIDAY_CACHE_DIR
) -> tuple[int, str]:
    # 优先使用本地有效缓存，避免开机断网/弱网时无谓等待 5 秒超时
    cached = read_holiday_cache(date, cache_dir)
    if cached is not None:
        return day_type_from_year_data(date, cached)

    errors: list[str] = []
    try:
        result = fetch_timor_day_type(date)
        for template in HOLIDAY_DATA_URLS:
            try:
                payload = fetch_json(template.format(year=date.year))
                day_type_from_year_data(date, payload)
                write_holiday_cache(date.year, payload, cache_dir)
                break
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError):
                continue
        return result
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as error:
        errors.append(f"timor.tech: {error}")

    for template in HOLIDAY_DATA_URLS:
        url = template.format(year=date.year)
        try:
            payload = fetch_json(url)
            result = day_type_from_year_data(date, payload)
            write_holiday_cache(date.year, payload, cache_dir)
            return result
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f"{url}: {error}")
    raise RuntimeError("所有节假日数据源均不可用: " + "; ".join(errors))


def should_run(
    config: dict[str, Any],
    now: dt.datetime | None = None,
    day_type_provider: Callable[[dt.date], tuple[int, str]] = fetch_day_type,
    log: logging.Logger | None = None,
    check_window: bool = True,
) -> bool:
    config = validate_config(config)
    now = now or dt.datetime.now()
    logger = log or get_logger()
    logger.info("开始检查，当前时间 %s", now.strftime("%Y-%m-%d %H:%M:%S"))

    if check_window:
        start = dt.datetime.strptime(config["start_time"], "%H:%M").time()
        cutoff = dt.datetime.strptime(config["cutoff_time"], "%H:%M").time()
        current = now.time()
        in_window = start <= current <= cutoff if start <= cutoff else current >= start or current <= cutoff
        if not in_window:
            logger.info(
                "当前时间不在启动窗口 %s-%s，不启动",
                config["start_time"],
                config["cutoff_time"],
            )
            return False
    else:
        logger.info("已跳过时间窗口检查（登录托盘模式）")

    if not config["check_workday"]:
        logger.info("未启用工作日检查，允许启动")
        return True

    try:
        day_type, day_name = day_type_provider(now.date())
        logger.info("今日类型：%s (type=%s)", day_name, day_type)
        if day_type in (0, 3):
            logger.info("今天需要上班，允许启动")
            return True
        logger.info("今天是休息日，不启动")
        return False
    except Exception as error:
        logger.warning("工作日查询失败：%s", error)
        if now.weekday() >= 5:
            weekday = "周六" if now.weekday() == 5 else "周日"
            logger.info("接口不可用，本地日历显示今天是%s，不启动", weekday)
            return False
        if config["run_when_offline"]:
            logger.info("接口不可用，但本地日历为周一至周五；已启用断网兜底，允许启动")
            return True
        logger.info("未启用断网兜底，不启动")
        return False


def is_process_running(executable_name: str) -> bool:
    """检查指定可执行文件名是否已在系统中运行"""
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FI", f"IMAGENAME eq {executable_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
        )
        return executable_name.casefold() in result.stdout.casefold()
    except OSError:
        return False


def launch_programs(
    programs: list[str | dict[str, str]], log: logging.Logger | None = None
) -> int:
    logger = log or get_logger()
    launched = 0
    for item in programs:
        if isinstance(item, dict):
            value = item.get("path", "")
            service_name = item.get("service_name", "").strip()
        else:
            value = item
            service_name = ""
        path = Path(value).expanduser()
        if not path.is_file():
            logger.error("找不到程序：%s", path)
            continue
        noninteractive = is_noninteractive_session()
        if noninteractive:
            if not service_name:
                logger.error("SYSTEM 模式下未配置 Windows 服务名，已跳过：%s", path)
                continue
            if start_windows_service(service_name, logger):
                launched += 1
                logger.info(
                    "SYSTEM 模式下不启动桌面程序 %s，已启动显式配置的 Windows 服务：%s",
                    path,
                    service_name,
                )
            continue
        if is_process_running(path.name):
            logger.info("程序已在运行中，跳过重复启动：%s", path)
            launched += 1
            continue
        try:
            subprocess.Popen([str(path)], cwd=str(path.parent))
            logger.info("已启动：%s", path)
            launched += 1
        except OSError as error:
            logger.error("启动失败：%s；%s", path, error)
    return launched


def is_noninteractive_session() -> bool:
    if os.name != "nt":
        return False
    session_id = ctypes.c_ulong()
    if ctypes.windll.kernel32.ProcessIdToSessionId(
        os.getpid(), ctypes.byref(session_id)
    ):
        return session_id.value == 0
    return os.environ.get("USERNAME", "").upper() == "SYSTEM"


def service_is_running(output: str) -> bool:
    return bool(re.search(r"\bSTATE\s*:\s*4\s+RUNNING\b", output, re.IGNORECASE))


def wait_for_windows_service_running(
    name: str,
    log: logging.Logger,
    timeout: float = SERVICE_START_TIMEOUT,
    poll_interval: float = SERVICE_POLL_INTERVAL,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    last_output = ""
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            result = subprocess.run(
                ["sc.exe", "query", name],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            log.error("Windows 服务状态查询超时：%s", name)
            return False
        last_output = f"{result.stdout}\n{result.stderr}"
        if result.returncode == 0 and service_is_running(last_output):
            return True
        if time.monotonic() >= deadline:
            log.error("Windows 服务未进入 RUNNING 状态：%s；%s", name, last_output.strip())
            return False
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


def start_windows_service(
    name: str,
    log: logging.Logger,
    timeout: float = SERVICE_START_TIMEOUT,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        result = subprocess.run(
            ["sc.exe", "start", name],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=max(0.0, deadline - time.monotonic()),
        )
    except subprocess.TimeoutExpired:
        log.error("Windows 服务启动请求超时：%s", name)
        return False
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode and "1056" not in output:
        log.error("Windows 服务启动请求失败：%s；%s", name, output.strip())
        return False
    if wait_for_windows_service_running(
        name, log, timeout=max(0.0, deadline - time.monotonic())
    ):
        log.info("Windows 服务已确认进入 RUNNING 状态：%s", name)
        return True
    return False


def run_once(
    config: dict[str, Any] | None = None,
    wait: bool = True,
    check_window: bool = True,
    config_path: Path = CONFIG_FILE,
    log_path: Path = LOG_FILE,
) -> int:
    config = validate_config(config or load_config(config_path))
    logger = get_logger(log_path)
    if not config["programs"]:
        logger.warning("没有配置要启动的程序")
        return 0
    if wait and config["startup_delay"]:
        logger.info("等待 %s 秒后检查", config["startup_delay"])
        time.sleep(config["startup_delay"])
    holiday_cache = config_path.parent / "holidays"
    provider = lambda date: fetch_day_type(date, holiday_cache)
    if not should_run(config, day_type_provider=provider, log=logger, check_window=check_window):
        return 0
    return launch_programs(config["programs"], logger)


def autostart_command(
    config_path: Path = CONFIG_FILE, log_path: Path = LOG_FILE
) -> str:
    executable = Path(sys.executable)
    args = [str(executable)]
    if not getattr(sys, "frozen", False):
        args.append(str(Path(__file__).resolve()))
    args.extend(["--run", "--config", str(config_path), "--log", str(log_path)])
    return subprocess.list2cmdline(args)


def user_ui_command(
    config_path: Path = CONFIG_FILE, log_path: Path = LOG_FILE
) -> str:
    executable = Path(sys.executable)
    if not getattr(sys, "frozen", False) and executable.name.casefold() == "python.exe":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.is_file():
            executable = pythonw
    args = [str(executable)]
    if not getattr(sys, "frozen", False):
        args.append(str(Path(__file__).resolve()))
    args.extend(["--run-ui", "--config", str(config_path), "--log", str(log_path)])
    return subprocess.list2cmdline(args)


def set_user_ui_autostart(enabled: bool) -> None:
    if os.name != "nt":
        raise OSError("用户登录自启仅支持 Windows")
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        try:
            winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass
        if enabled:
            winreg.SetValueEx(key, UI_RUN_VALUE, 0, winreg.REG_SZ, user_ui_command())
        else:
            try:
                winreg.DeleteValue(key, UI_RUN_VALUE)
            except FileNotFoundError:
                pass


def user_ui_autostart_needs_sync(desired: bool) -> bool | None:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            try:
                value = winreg.QueryValueEx(key, UI_RUN_VALUE)[0]
            except FileNotFoundError:
                value = None
            try:
                winreg.QueryValueEx(key, APP_NAME)
                legacy_exists = True
            except FileNotFoundError:
                legacy_exists = False
            if legacy_exists:
                return True
            if value is None:
                return desired
            if str(value).strip() != user_ui_command().strip():
                return True
            return not desired
    except FileNotFoundError:
        return desired
    except OSError:
        return None


def system_autostart_status() -> bool | None:
    """返回任务状态；None 表示查询失败且不能确认任务是否存在。"""
    if os.name != "nt":
        return False
    result = subprocess.run(
        ["schtasks.exe", "/Query", "/TN", TASK_NAME],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}".casefold()
    denied_keywords = (
        "access is denied",
        "拒绝访问",
        "存取被拒",
        "アクセスが拒否",
        "accès refusé",
        "zugriff verweigert",
        "0x80070005",
    )
    if result.returncode == 0:
        return True
    if any(kw in output for kw in denied_keywords):
        return None
    not_found_keywords = (
        "cannot find",
        "找不到",
        "不存在",
        "not exist",
        "指定されたファイルが見つかりません",
    )
    if any(kw in output for kw in not_found_keywords):
        return False
    return None


def is_system_autostart_enabled() -> bool:
    return system_autostart_status() is True


def should_update_system_autostart(
    current: bool | None, desired: bool, user_changed: bool
) -> bool:
    """未知状态下仅在用户明确操作开关时才允许修改自启配置。"""
    if current is None:
        return user_changed
    return current != desired


def autostart_update_plan(
    system_status: bool | None,
    user_ui_needs_sync: bool | None,
    desired: bool,
    user_changed: bool,
) -> tuple[bool, bool]:
    """决定是否分别更新 SYSTEM 任务和当前用户的登录项。"""
    if system_status is None and not user_changed:
        return False, False
    update_system = should_update_system_autostart(
        system_status, desired, user_changed
    )
    update_user_ui = user_changed if user_ui_needs_sync is None else user_ui_needs_sync
    return update_system, update_user_ui


def set_system_autostart(
    enabled: bool, config_path: Path = CONFIG_FILE, log_path: Path = LOG_FILE
) -> None:
    if os.name != "nt":
        raise OSError("开机自启仅支持 Windows")
    if enabled:
        command = [
            "schtasks.exe",
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            autostart_command(config_path, log_path),
            "/SC",
            "ONSTART",
            "/RU",
            "SYSTEM",
            "/RL",
            "HIGHEST",
            "/F",
        ]
    else:
        command = ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"]
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    status = system_autostart_status()
    if result.returncode:
        if not enabled and status is False:
            return
        detail = result.stderr.strip() or result.stdout.strip() or f"错误码 {result.returncode}"
        raise OSError(f"计划任务操作失败：{detail}")
    if enabled and status is not True:
        raise OSError("计划任务创建后验证失败，无法确认 BootRunner Startup 已存在。")
    if not enabled and status is not False:
        raise OSError("计划任务删除后验证失败，无法确认 BootRunner Startup 已删除。")


def run_elevated_autostart_helper(enabled: bool) -> None:
    executable = str(Path(sys.executable))
    arguments: list[str] = []
    if not getattr(sys, "frozen", False):
        arguments.append(str(Path(__file__).resolve()))
    arguments.extend(
        [
            "--set-system-autostart",
            "enable" if enabled else "disable",
            "--config",
            str(CONFIG_FILE),
            "--log",
            str(LOG_FILE),
        ]
    )

    def ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    argument_line = subprocess.list2cmdline(arguments)
    script = (
        f"$p=Start-Process -FilePath {ps_quote(executable)} "
        f"-ArgumentList {ps_quote(argument_line)} -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    if result.returncode:
        raise OSError("未能修改系统启动任务。请确认 UAC 管理员授权。")


def read_log_tail(path: Path = LOG_FILE, limit: int = 200_000) -> str:
    if not path.exists():
        return "暂无日志。"
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - limit))
        data = stream.read()
    text = data.decode("utf-8", errors="replace")
    if size > limit and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


class BootRunnerApp:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title("Boot Runner")
        self.root.geometry("820x620")
        self.root.minsize(700, 520)
        self.config = load_config()
        self.service_names = {
            program["path"]: program["service_name"]
            for program in self.config["programs"]
        }
        self.selected_program_path: str | None = None
        self.autostart_status = system_autostart_status()
        self.autostart_changed = False
        self.messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self.last_log_state: tuple[int, int] | None = None

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Hint.TLabel", foreground="#5f6b76")

        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Boot Runner", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="按时间和工作日规则，在用户登录前启动指定软件。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(2, 12))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        self.settings_tab = ttk.Frame(notebook, padding=14)
        self.log_tab = ttk.Frame(notebook, padding=10)
        notebook.add(self.settings_tab, text="启动设置")
        notebook.add(self.log_tab, text="运行日志")
        self._build_settings()
        self._build_log_view()

        self.status = tk.StringVar(value=f"配置文件：{CONFIG_FILE}")
        ttk.Label(outer, textvariable=self.status, style="Hint.TLabel").pack(
            fill="x", pady=(10, 0)
        )
        self.root.after(300, self._poll)

    def _build_settings(self) -> None:
        from tkinter import ttk

        tab = self.settings_tab
        ttk.Label(tab, text="要启动的软件").grid(row=0, column=0, sticky="w")
        list_frame = ttk.Frame(tab)
        list_frame.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(5, 12))
        self.program_list = self.tk.Listbox(
            list_frame, height=8, selectmode="extended", font=("Microsoft YaHei UI", 9)
        )
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.program_list.yview)
        self.program_list.configure(yscrollcommand=scrollbar.set)
        self.program_list.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.program_list.bind("<<ListboxSelect>>", self._on_program_selected)
        for program in self.config["programs"]:
            self.program_list.insert("end", program["path"])

        service_frame = ttk.Frame(tab)
        service_frame.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        ttk.Label(service_frame, text="选中程序的 Windows 服务名").pack(side="left")
        self.service_name = self.tk.StringVar()
        ttk.Entry(service_frame, textvariable=self.service_name, width=34).pack(
            side="left", padx=(10, 0)
        )
        ttk.Label(
            service_frame,
            text="（登录前启动服务时必填；可留空表示只在登录后启动 GUI）",
            style="Hint.TLabel",
        ).pack(side="left", padx=(8, 0))

        ttk.Button(tab, text="添加软件...", command=self._add_program).grid(
            row=3, column=0, sticky="w"
        )
        ttk.Button(tab, text="移除选中", command=self._remove_program).grid(
            row=3, column=1, sticky="w", padx=(8, 0)
        )

        options = ttk.LabelFrame(tab, text="执行规则", padding=12)
        options.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(18, 12))
        ttk.Label(options, text="开始时间").grid(row=0, column=0, sticky="w")
        start_hour, start_minute = self.config["start_time"].split(":")
        self.start_hour = self.tk.StringVar(value=start_hour)
        self.start_minute = self.tk.StringVar(value=start_minute)
        ttk.Spinbox(options, from_=0, to=23, width=4, textvariable=self.start_hour, format="%02.0f").grid(
            row=0, column=1, padx=(10, 2)
        )
        ttk.Label(options, text=":").grid(row=0, column=2)
        ttk.Spinbox(options, from_=0, to=59, width=4, textvariable=self.start_minute, format="%02.0f").grid(
            row=0, column=3, padx=(2, 20)
        )
        ttk.Label(options, text="开机后等待（秒）").grid(row=0, column=4, sticky="w")
        self.delay = self.tk.StringVar(value=str(self.config["startup_delay"]))
        ttk.Spinbox(options, from_=0, to=600, width=7, textvariable=self.delay).grid(
            row=0, column=5, padx=(10, 0)
        )

        ttk.Label(options, text="结束时间").grid(row=1, column=0, sticky="w", pady=(8, 0))
        end_hour, end_minute = self.config["cutoff_time"].split(":")
        self.end_hour = self.tk.StringVar(value=end_hour)
        self.end_minute = self.tk.StringVar(value=end_minute)
        ttk.Spinbox(options, from_=0, to=23, width=4, textvariable=self.end_hour, format="%02.0f").grid(
            row=1, column=1, padx=(10, 2), pady=(8, 0)
        )
        ttk.Label(options, text=":").grid(row=1, column=2, pady=(8, 0))
        ttk.Spinbox(options, from_=0, to=59, width=4, textvariable=self.end_minute, format="%02.0f").grid(
            row=1, column=3, padx=(2, 20), pady=(8, 0)
        )

        self.check_workday = self.tk.BooleanVar(value=self.config["check_workday"])
        self.run_offline = self.tk.BooleanVar(value=self.config["run_when_offline"])
        self.autostart = self.tk.BooleanVar(value=self.autostart_status is True)
        ttk.Checkbutton(options, text="仅工作日和调休工作日启动", variable=self.check_workday).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(12, 0)
        )
        ttk.Checkbutton(options, text="接口不可用时，普通周一至周五仍启动", variable=self.run_offline).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        ttk.Checkbutton(
            options,
            text="登录前启动服务，登录后显示软件托盘图标",
            variable=self.autostart,
            command=self._mark_autostart_changed,
        ).grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        ttk.Label(
            options,
            text="注意：登录前只启动你为程序明确填写的 Windows 服务，不再根据路径猜测；桌面 GUI 会在用户登录后启动。",
            style="Hint.TLabel",
            wraplength=620,
        ).grid(row=5, column=0, columnspan=6, sticky="w", pady=(8, 0))

        actions = ttk.Frame(tab)
        actions.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="保存设置", command=self._save).pack(side="left")
        ttk.Button(actions, text="立即测试（不等待）", command=self._run_now).pack(
            side="left", padx=(8, 0)
        )
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

    def _build_log_view(self) -> None:
        from tkinter import ttk

        toolbar = ttk.Frame(self.log_tab)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Button(toolbar, text="刷新", command=self._refresh_log).pack(side="left")
        ttk.Button(toolbar, text="打开日志目录", command=self._open_log_dir).pack(
            side="left", padx=(8, 0)
        )
        self.log_text = self.tk.Text(
            self.log_tab,
            wrap="none",
            state="disabled",
            bg="#101820",
            fg="#d7e1e8",
            insertbackground="white",
            font=("Consolas", 9),
        )
        yscroll = ttk.Scrollbar(self.log_tab, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=yscroll.set)
        yscroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self._refresh_log()

    def _add_program(self) -> None:
        from tkinter import filedialog

        paths = filedialog.askopenfilenames(
            title="选择要启动的软件",
            filetypes=[("可执行文件", "*.exe *.bat *.cmd *.com"), ("所有文件", "*.*")],
        )
        existing = set(self.program_list.get(0, "end"))
        for path in paths:
            norm_path = os.path.normpath(path)
            if norm_path not in existing:
                self.program_list.insert("end", norm_path)
                self.service_names[norm_path] = ""
                existing.add(norm_path)

    def _remove_program(self) -> None:
        self._store_selected_service_name()
        for index in reversed(self.program_list.curselection()):
            self.service_names.pop(self.program_list.get(index), None)
            self.program_list.delete(index)

    def _store_selected_service_name(self, _event: Any = None) -> None:
        path = self.selected_program_path
        if path is not None:
            self.service_names[path] = self.service_name.get().strip()

    def _on_program_selected(self, _event: Any = None) -> None:
        self._store_selected_service_name()
        selection = self.program_list.curselection()
        if selection:
            self.selected_program_path = self.program_list.get(selection[0])
            self.service_name.set(self.service_names.get(self.selected_program_path, ""))

    def _mark_autostart_changed(self) -> None:
        self.autostart_changed = True

    def _collect_config(self) -> dict[str, Any]:
        self._store_selected_service_name()
        try:
            start_hour = int(self.start_hour.get())
            start_minute = int(self.start_minute.get())
            end_hour = int(self.end_hour.get())
            end_minute = int(self.end_minute.get())
            delay = int(self.delay.get())
        except ValueError as error:
            raise ValueError("时间和等待秒数必须是数字") from error
        times_valid = (
            0 <= start_hour <= 23
            and 0 <= start_minute <= 59
            and 0 <= end_hour <= 23
            and 0 <= end_minute <= 59
        )
        if not times_valid or not 0 <= delay <= 600:
            raise ValueError("时间或等待秒数超出有效范围")
        return {
            "programs": [
                {
                    "path": path,
                    "service_name": self.service_names.get(path, "").strip(),
                }
                for path in self.program_list.get(0, "end")
            ],
            "start_time": f"{start_hour:02d}:{start_minute:02d}",
            "cutoff_time": f"{end_hour:02d}:{end_minute:02d}",
            "startup_delay": delay,
            "check_workday": self.check_workday.get(),
            "run_when_offline": self.run_offline.get(),
        }

    def _save(self, quiet: bool = False) -> bool:
        from tkinter import messagebox

        try:
            self.config = self._collect_config()
            save_config(self.config)
            desired_autostart = self.autostart.get()
            current_autostart = system_autostart_status()
            update_system, update_user_ui = autostart_update_plan(
                current_autostart,
                user_ui_autostart_needs_sync(desired_autostart),
                desired_autostart,
                self.autostart_changed,
            )
            if update_system:
                run_elevated_autostart_helper(desired_autostart)
            if update_user_ui:
                set_user_ui_autostart(desired_autostart)
            if update_system or update_user_ui:
                self.autostart_changed = False
        except (OSError, ValueError) as error:
            self.autostart_changed = False
            self.autostart.set(system_autostart_status() is True)
            messagebox.showerror("保存失败", str(error), parent=self.root)
            return False
        self.status.set("设置已保存")
        if not quiet:
            messagebox.showinfo("Boot Runner", "设置已保存。", parent=self.root)
        return True

    def _run_now(self) -> None:
        if not self._save(quiet=True):
            return
        self.status.set("正在执行检查...")
        threading.Thread(target=self._run_worker, daemon=True).start()

    def _run_worker(self) -> None:
        try:
            count = run_once(self.config, wait=False)
            self.messages.put(("ok", f"执行完成，已启动 {count} 个软件"))
        except Exception as error:
            get_logger().exception("执行发生未处理错误")
            self.messages.put(("error", str(error)))

    def _refresh_log(self) -> None:
        content = read_log_tail()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", content)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        try:
            stat = LOG_FILE.stat()
            self.last_log_state = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            self.last_log_state = None

    def _open_log_dir(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(APP_DIR)

    def _poll(self) -> None:
        from tkinter import messagebox

        try:
            stat = LOG_FILE.stat()
            state = (stat.st_mtime_ns, stat.st_size)
            if state != self.last_log_state:
                self._refresh_log()
        except FileNotFoundError:
            pass
        try:
            kind, text = self.messages.get_nowait()
            self.status.set(text)
            if kind == "error":
                messagebox.showerror("执行失败", text, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(750, self._poll)

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="按时间和工作日规则启动软件")
    parser.add_argument("--run", action="store_true", help="无界面执行一次")
    parser.add_argument("--run-ui", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help=argparse.SUPPRESS)
    parser.add_argument("--log", type=Path, default=LOG_FILE, help=argparse.SUPPRESS)
    parser.add_argument(
        "--set-system-autostart",
        choices=("enable", "disable"),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.set_system_autostart:
        set_system_autostart(
            args.set_system_autostart == "enable", args.config, args.log
        )
        return 0
    if args.run:
        run_once(config_path=args.config, log_path=args.log)
        return 0
    if args.run_ui:
        run_once(wait=False, check_window=True, config_path=args.config, log_path=args.log)
        return 0
    BootRunnerApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
