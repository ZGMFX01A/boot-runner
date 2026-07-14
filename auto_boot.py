from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


APP_NAME = "BootRunner"
APP_DIR = Path(os.getenv("APPDATA", Path.home())) / APP_NAME
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "boot-runner.log"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TASK_NAME = "BootRunner Startup"
DEFAULT_CONFIG: dict[str, Any] = {
    "programs": [],
    "start_time": "00:00",
    "cutoff_time": "18:30",
    "startup_delay": 10,
    "check_workday": True,
    "run_when_offline": True,
}


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
    programs = config.get("programs", [])
    if not isinstance(programs, list):
        programs = []
    programs = [str(item) for item in programs if isinstance(item, str) and item.strip()]

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


def fetch_day_type(date: dt.date, retries: int = 3, retry_delay: float = 2) -> tuple[int, str]:
    url = f"https://timor.tech/api/holiday/info/{date.isoformat()}"
    request = Request(url, headers={"User-Agent": f"{APP_NAME}/1.0"})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            day = payload["type"]
            return int(day["type"]), str(day["name"])
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as error:
            last_error = error
            if attempt + 1 < retries:
                time.sleep(retry_delay)
    raise RuntimeError(f"节假日服务不可用: {last_error}")


def should_run(
    config: dict[str, Any],
    now: dt.datetime | None = None,
    day_type_provider: Callable[[dt.date], tuple[int, str]] = fetch_day_type,
    log: logging.Logger | None = None,
) -> bool:
    config = validate_config(config)
    now = now or dt.datetime.now()
    start = dt.datetime.strptime(config["start_time"], "%H:%M").time()
    cutoff = dt.datetime.strptime(config["cutoff_time"], "%H:%M").time()
    logger = log or get_logger()
    logger.info("开始检查，当前时间 %s", now.strftime("%Y-%m-%d %H:%M:%S"))

    current = now.time()
    in_window = start <= current <= cutoff if start <= cutoff else current >= start or current <= cutoff
    if not in_window:
        logger.info(
            "当前时间不在启动窗口 %s-%s，不启动",
            config["start_time"],
            config["cutoff_time"],
        )
        return False
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
        if config["run_when_offline"]:
            logger.info("已启用断网兜底，允许启动")
            return True
        logger.info("未启用断网兜底，不启动")
        return False


def launch_programs(programs: list[str], log: logging.Logger | None = None) -> int:
    logger = log or get_logger()
    launched = 0
    for value in programs:
        path = Path(value).expanduser()
        if not path.is_file():
            logger.error("找不到程序：%s", path)
            continue
        try:
            subprocess.Popen([str(path)], cwd=str(path.parent))
            logger.info("已启动：%s", path)
            launched += 1
        except OSError as error:
            logger.error("启动失败：%s；%s", path, error)
    return launched


def run_once(
    config: dict[str, Any] | None = None,
    wait: bool = True,
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
    if not should_run(config, log=logger):
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


def remove_legacy_autostart() -> None:
    if os.name != "nt":
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        try:
            winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass


def is_system_autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    result = subprocess.run(
        ["schtasks.exe", "/Query", "/TN", TASK_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


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
    if result.returncode and not (not enabled and not is_system_autostart_enabled()):
        detail = result.stderr.strip() or result.stdout.strip() or f"错误码 {result.returncode}"
        raise OSError(f"计划任务操作失败：{detail}")
    remove_legacy_autostart()


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
    return data.decode("utf-8", errors="replace")


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
        for program in self.config["programs"]:
            self.program_list.insert("end", program)

        ttk.Button(tab, text="添加软件...", command=self._add_program).grid(
            row=2, column=0, sticky="w"
        )
        ttk.Button(tab, text="移除选中", command=self._remove_program).grid(
            row=2, column=1, sticky="w", padx=(8, 0)
        )

        options = ttk.LabelFrame(tab, text="执行规则", padding=12)
        options.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(18, 12))
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
        self.autostart = self.tk.BooleanVar(value=is_system_autostart_enabled())
        ttk.Checkbutton(options, text="仅工作日和调休工作日启动", variable=self.check_workday).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(12, 0)
        )
        ttk.Checkbutton(options, text="节假日服务不可用时仍启动", variable=self.run_offline).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        ttk.Checkbutton(options, text="系统启动时执行（登录前，需管理员授权）", variable=self.autostart).grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        ttk.Label(
            options,
            text="注意：普通桌面程序在登录前位于 Session 0，不会显示界面；远程软件需自身支持系统服务模式。",
            style="Hint.TLabel",
            wraplength=620,
        ).grid(row=5, column=0, columnspan=6, sticky="w", pady=(8, 0))

        actions = ttk.Frame(tab)
        actions.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(8, 0))
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
            if path not in existing:
                self.program_list.insert("end", path)
                existing.add(path)

    def _remove_program(self) -> None:
        for index in reversed(self.program_list.curselection()):
            self.program_list.delete(index)

    def _collect_config(self) -> dict[str, Any]:
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
            "programs": list(self.program_list.get(0, "end")),
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
            if desired_autostart != is_system_autostart_enabled():
                remove_legacy_autostart()
                run_elevated_autostart_helper(desired_autostart)
        except (OSError, ValueError) as error:
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
    BootRunnerApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
