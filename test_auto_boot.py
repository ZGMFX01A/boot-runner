import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import auto_boot


class ConfigTests(unittest.TestCase):
    def test_config_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = {
                "programs": [
                    {
                        "path": r"C:\Tools\demo.exe",
                        "service_name": "DemoService",
                    }
                ],
                "start_time": "07:30",
                "cutoff_time": "09:05",
                "startup_delay": 3,
                "check_workday": False,
                "run_when_offline": False,
            }
            auto_boot.save_config(config, path)
            self.assertEqual(auto_boot.load_config(path), config)

    def test_legacy_program_paths_are_migrated_without_a_guessed_service(self):
        config = auto_boot.validate_config({"programs": [r"C:\Tools\demo.exe"]})
        self.assertEqual(
            config["programs"],
            [{"path": r"C:\Tools\demo.exe", "service_name": ""}],
        )

    def test_invalid_config_uses_safe_defaults(self):
        config = auto_boot.validate_config(
            {"start_time": "bad", "cutoff_time": "25:99", "startup_delay": -5}
        )
        self.assertEqual(config["start_time"], "00:00")
        self.assertEqual(config["cutoff_time"], "18:30")
        self.assertEqual(config["startup_delay"], 0)

    def test_system_task_command_uses_explicit_user_paths(self):
        command = auto_boot.autostart_command(
            Path(r"C:\Users\demo\config.json"), Path(r"C:\Users\demo\runner.log")
        )
        self.assertIn("--config C:\\Users\\demo\\config.json", command)
        self.assertIn("--log C:\\Users\\demo\\runner.log", command)

    def test_user_ui_command_runs_after_login_with_pythonw(self):
        command = auto_boot.user_ui_command(
            Path(r"C:\Users\demo\config.json"), Path(r"C:\Users\demo\runner.log")
        )
        self.assertIn("pythonw.exe", command.casefold())
        self.assertIn("--run-ui", command)

    @patch("auto_boot.subprocess.run")
    def test_system_task_runs_at_boot_as_system(self, run: Mock):
        run.side_effect = [
            Mock(returncode=0, stdout="", stderr=""),
            Mock(returncode=0, stdout="TaskName: BootRunner Startup", stderr=""),
        ]
        auto_boot.set_system_autostart(True)
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[command.index("/SC") + 1], "ONSTART")
        self.assertEqual(command[command.index("/RU") + 1], "SYSTEM")

    @patch("auto_boot.subprocess.run")
    def test_system_task_creation_requires_post_create_confirmation(self, run: Mock):
        run.side_effect = [
            Mock(returncode=0, stdout="", stderr=""),
            Mock(returncode=1, stdout="", stderr="Access is denied."),
        ]
        with self.assertRaisesRegex(OSError, "创建后验证失败"):
            auto_boot.set_system_autostart(True)

    def test_unknown_system_autostart_is_changed_only_after_user_action(self):
        self.assertFalse(auto_boot.should_update_system_autostart(None, False, False))
        self.assertTrue(auto_boot.should_update_system_autostart(None, True, True))
        self.assertTrue(auto_boot.should_update_system_autostart(False, True, False))

    def test_autostart_system_and_login_updates_are_independent(self):
        self.assertEqual(
            auto_boot.autostart_update_plan(True, True, True, False),
            (False, True),
        )
        self.assertEqual(
            auto_boot.autostart_update_plan(None, False, False, False),
            (False, False),
        )
        self.assertEqual(
            auto_boot.autostart_update_plan(None, True, False, True),
            (True, True),
        )
        self.assertEqual(
            auto_boot.autostart_update_plan(False, True, False, False),
            (False, True),
        )


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.config = auto_boot.DEFAULT_CONFIG | {"startup_delay": 0}
        self.logger = auto_boot.logging.getLogger("test-boot-runner")
        self.logger.addHandler(auto_boot.logging.NullHandler())

    def test_after_cutoff_never_runs(self):
        now = dt.datetime(2026, 7, 14, 18, 31)
        self.assertFalse(auto_boot.should_run(self.config, now, log=self.logger))

    def test_before_start_time_does_not_run(self):
        config = self.config | {"start_time": "08:30"}
        now = dt.datetime(2026, 7, 14, 8, 29)
        self.assertFalse(auto_boot.should_run(config, now, log=self.logger))

    def test_overnight_window_is_supported(self):
        config = self.config | {
            "start_time": "22:00",
            "cutoff_time": "06:00",
            "check_workday": False,
        }
        now = dt.datetime(2026, 7, 14, 23, 0)
        self.assertTrue(auto_boot.should_run(config, now, log=self.logger))

    def test_workday_runs_before_cutoff(self):
        now = dt.datetime(2026, 7, 14, 9, 0)
        provider = lambda _: (0, "工作日")
        self.assertTrue(auto_boot.should_run(self.config, now, provider, self.logger))

    def test_holiday_does_not_run(self):
        now = dt.datetime(2026, 7, 14, 9, 0)
        provider = lambda _: (2, "节日")
        self.assertFalse(auto_boot.should_run(self.config, now, provider, self.logger))

    def test_offline_policy_is_configurable(self):
        now = dt.datetime(2026, 7, 14, 9, 0)

        def unavailable(_):
            raise RuntimeError("offline")

        self.assertTrue(auto_boot.should_run(self.config, now, unavailable, self.logger))
        config = self.config | {"run_when_offline": False}
        self.assertFalse(auto_boot.should_run(config, now, unavailable, self.logger))

    def test_offline_sunday_never_runs(self):
        now = dt.datetime(2026, 7, 19, 9, 33)

        def unavailable(_):
            raise RuntimeError("offline")

        self.assertEqual(now.weekday(), 6)
        self.assertFalse(auto_boot.should_run(self.config, now, unavailable, self.logger))


class HolidaySourceTests(unittest.TestCase):
    def setUp(self):
        self.year_data = {
            "year": 2026,
            "days": [
                {"name": "春节", "date": "2026-02-17", "isOffDay": True},
                {"name": "春节", "date": "2026-02-14", "isOffDay": False},
            ],
        }

    def test_year_data_supports_holiday_and_weekend_makeup_day(self):
        self.assertEqual(
            auto_boot.day_type_from_year_data(dt.date(2026, 2, 17), self.year_data),
            (2, "春节"),
        )
        self.assertEqual(
            auto_boot.day_type_from_year_data(dt.date(2026, 2, 14), self.year_data),
            (3, "春节调休"),
        )

    @patch("auto_boot.fetch_json")
    @patch("auto_boot.fetch_timor_day_type", side_effect=OSError("primary offline"))
    def test_cdn_backup_is_cached(self, _primary: Mock, fetch_json: Mock):
        fetch_json.return_value = self.year_data
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            result = auto_boot.fetch_day_type(dt.date(2026, 2, 17), cache_dir)
            self.assertEqual(result, (2, "春节"))
            self.assertTrue((cache_dir / "2026.json").is_file())

    @patch("auto_boot.fetch_json")
    @patch("auto_boot.fetch_timor_day_type", return_value=(0, "工作日"))
    def test_primary_success_also_prepares_year_cache(self, _primary: Mock, fetch_json: Mock):
        fetch_json.return_value = self.year_data
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            result = auto_boot.fetch_day_type(dt.date(2026, 7, 20), cache_dir)
            self.assertEqual(result, (0, "工作日"))
            self.assertTrue((cache_dir / "2026.json").is_file())


    @patch("auto_boot.fetch_timor_day_type")
    def test_existing_cache_avoids_network_request(self, mock_timor: Mock):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            auto_boot.write_holiday_cache(2026, self.year_data, cache_dir)
            result = auto_boot.fetch_day_type(dt.date(2026, 2, 17), cache_dir)
            self.assertEqual(result, (2, "春节"))
            mock_timor.assert_not_called()


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.logger = auto_boot.logging.getLogger("test-boot-runner-launch")
        self.logger.addHandler(auto_boot.logging.NullHandler())

    def test_session_zero_is_noninteractive(self):
        def report_session_zero(_process_id, session_id):
            session_id._obj.value = 0
            return 1

        with patch.object(
            auto_boot.ctypes.windll.kernel32,
            "ProcessIdToSessionId",
            side_effect=report_session_zero,
        ):
            self.assertTrue(auto_boot.is_noninteractive_session())

    @patch("auto_boot.is_noninteractive_session", return_value=True)
    @patch("auto_boot.start_windows_service", return_value=True)
    @patch("auto_boot.Path.is_file", return_value=True)
    @patch("auto_boot.subprocess.Popen")
    def test_system_uses_explicit_service_instead_of_gui(
        self,
        popen: Mock,
        _is_file: Mock,
        start_service: Mock,
        _noninteractive: Mock,
    ):
        count = auto_boot.launch_programs(
            [
                {
                    "path": r"D:\uu\GameViewer\GameViewer.exe",
                    "service_name": "GameViewerService",
                }
            ],
            self.logger,
        )
        self.assertEqual(count, 1)
        start_service.assert_called_once_with("GameViewerService", self.logger)
        popen.assert_not_called()

    @patch("auto_boot.is_noninteractive_session", return_value=True)
    @patch("auto_boot.start_windows_service")
    @patch("auto_boot.Path.is_file", return_value=True)
    @patch("auto_boot.subprocess.Popen")
    def test_system_does_not_guess_service_from_program_path(
        self,
        popen: Mock,
        _is_file: Mock,
        start_service: Mock,
        _noninteractive: Mock,
    ):
        count = auto_boot.launch_programs([r"D:\uu\GameViewer\GameViewer.exe"], self.logger)
        self.assertEqual(count, 0)
        start_service.assert_not_called()
        popen.assert_not_called()

    @patch("auto_boot.is_noninteractive_session", return_value=False)
    @patch("auto_boot.is_process_running", return_value=True)
    @patch("auto_boot.Path.is_file", return_value=True)
    @patch("auto_boot.subprocess.Popen")
    def test_running_process_is_skipped(
        self,
        popen: Mock,
        _is_file: Mock,
        _is_running: Mock,
        _noninteractive: Mock,
    ):
        count = auto_boot.launch_programs(
            [r"D:\Tools\DemoApp.exe"], self.logger
        )
        self.assertEqual(count, 1)
        popen.assert_not_called()


class EnhancementTests(unittest.TestCase):
    def test_get_app_dir_frozen_vs_normal(self):
        import sys
        # 普通源码模式
        with patch.object(sys, "frozen", False, create=True):
            app_dir = auto_boot.get_app_dir()
            self.assertEqual(app_dir, Path(auto_boot.__file__).resolve().parent)

        # PyInstaller 冻结打包模式
        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "executable", r"C:\MyTools\BootRunner.exe"
        ):
            app_dir = auto_boot.get_app_dir()
            self.assertEqual(app_dir, Path(r"C:\MyTools"))

    @patch("auto_boot.subprocess.run")
    def test_is_system_autostart_enabled_multilingual(self, mock_run: Mock):
        # 权限不足不能被误判为“已开启”
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="錯誤: 存取被拒。")
        self.assertFalse(auto_boot.is_system_autostart_enabled())

        # 日文
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="エラー: アクセスが拒否されました。")
        self.assertFalse(auto_boot.is_system_autostart_enabled())

        # 错误码 0x80070005
        mock_run.return_value = Mock(returncode=1, stdout="0x80070005", stderr="")
        self.assertFalse(auto_boot.is_system_autostart_enabled())

        # 任务不存在
        mock_run.return_value = Mock(returncode=1, stdout="", stderr="ERROR: The system cannot find the file specified.")
        self.assertFalse(auto_boot.is_system_autostart_enabled())

    def test_user_ui_autostart_sync_checks_command_and_legacy_value(self):
        class MockKey:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        expected = '"C:\\Tools\\BootRunner.exe" --run-ui'

        def query_current(_key, name):
            if name == auto_boot.UI_RUN_VALUE:
                return expected, 1
            raise FileNotFoundError()

        with patch("winreg.OpenKey", return_value=MockKey()), \
             patch("winreg.QueryValueEx", side_effect=query_current), \
             patch("auto_boot.user_ui_command", return_value=expected):
            self.assertFalse(auto_boot.user_ui_autostart_needs_sync(True))

        def query_stale(_key, name):
            if name == auto_boot.UI_RUN_VALUE:
                return '"C:\\Old\\BootRunner.exe" --run-ui', 1
            raise FileNotFoundError()

        with patch("winreg.OpenKey", return_value=MockKey()), \
             patch("winreg.QueryValueEx", side_effect=query_stale), \
             patch("auto_boot.user_ui_command", return_value=expected):
            self.assertTrue(auto_boot.user_ui_autostart_needs_sync(True))

        def query_with_legacy(_key, name):
            if name == auto_boot.UI_RUN_VALUE:
                raise FileNotFoundError()
            return ("legacy command", 1)

        with patch("winreg.OpenKey", return_value=MockKey()), \
             patch("winreg.QueryValueEx", side_effect=query_with_legacy):
            self.assertTrue(auto_boot.user_ui_autostart_needs_sync(False))

    def test_read_log_tail_safe_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "test.log"
            content = "Line1: [2026-09-06 08:00:00] 开机启动测试\nLine2: [2026-09-06 08:00:05] 第二行中文日志\n"
            log_path.write_text(content, encoding="utf-8")
            # 当 limit 小于文件大小时，应安全丢弃第一行残断
            tail = auto_boot.read_log_tail(log_path, limit=len(content.encode("utf-8")) - 10)
            self.assertIn("Line2: [2026-09-06 08:00:05] 第二行中文日志", tail)
            self.assertNotIn("Line1:", tail)

    @patch("auto_boot.run_once")
    def test_run_ui_enforces_time_window(self, run_once: Mock):
        with patch.object(auto_boot.sys, "argv", ["auto_boot.py", "--run-ui"]):
            self.assertEqual(auto_boot.main(), 0)
        run_once.assert_called_once_with(
            wait=False,
            check_window=True,
            config_path=auto_boot.CONFIG_FILE,
            log_path=auto_boot.LOG_FILE,
        )

    @patch("auto_boot.subprocess.run")
    def test_service_start_requires_running_state(self, run: Mock):
        run.side_effect = [
            Mock(returncode=0, stdout="[SC] StartService SUCCESS", stderr=""),
            Mock(
                returncode=0,
                stdout="SERVICE_NAME: DemoService\n        STATE              : 4  RUNNING",
                stderr="",
            ),
        ]
        logger = auto_boot.logging.getLogger("test-service-state")
        logger.addHandler(auto_boot.logging.NullHandler())

        self.assertTrue(auto_boot.start_windows_service("DemoService", logger, timeout=0))
        self.assertEqual(run.call_args_list[1].args[0], ["sc.exe", "query", "DemoService"])
        self.assertEqual(run.call_args_list[1].kwargs["timeout"], 0.0)

    @patch("auto_boot.subprocess.run")
    def test_service_start_fails_when_service_is_not_running(self, run: Mock):
        run.side_effect = [
            Mock(returncode=0, stdout="[SC] StartService SUCCESS", stderr=""),
            Mock(
                returncode=0,
                stdout="SERVICE_NAME: DemoService\n        STATE              : 1  STOPPED",
                stderr="",
            ),
        ]
        logger = auto_boot.logging.getLogger("test-service-stopped")
        logger.addHandler(auto_boot.logging.NullHandler())

        self.assertFalse(auto_boot.start_windows_service("DemoService", logger, timeout=0))

    def test_first_program_selection_preserves_saved_service_name(self):
        path = r"D:\uu\GameViewer\GameViewer.exe"
        app = object.__new__(auto_boot.BootRunnerApp)
        app.selected_program_path = None
        app.service_names = {path: "GameViewerService"}
        app.service_name = Mock()
        app.program_list = Mock()
        app.program_list.curselection.return_value = (0,)
        app.program_list.get.return_value = path

        auto_boot.BootRunnerApp._on_program_selected(app)

        self.assertEqual(app.service_names[path], "GameViewerService")
        self.assertEqual(app.selected_program_path, path)
        app.service_name.set.assert_called_once_with("GameViewerService")


if __name__ == "__main__":
    unittest.main()
