# Boot Runner

一个仅使用 Python 标准库的 Windows 开机启动工具。它会在 Windows 启动、用户尚未登录时以 `SYSTEM` 身份执行一次检查，符合时间和工作日规则时启动配置的软件，然后立即退出，不常驻后台。

## 使用

要求 Python 3.10 或更高版本，并包含 Tkinter（Windows 官方 Python 安装包默认包含）。

```powershell
python auto_boot.py
```

在界面中添加一个或多个软件，设置开始时间、结束时间和开机等待时间，勾选“登录前启动服务，登录后显示软件托盘图标”，然后保存。首次启用或关闭时 Windows 会显示 UAC 管理员授权提示；计划任务使用 `SYSTEM` 账户，开机时不要求输入用户密码。

日志保存在：

```text
%APPDATA%\BootRunner\boot-runner.log
```

日志单文件最大 2 MB，自动保留 3 个历史文件。配置保存在 `%APPDATA%\BootRunner\config.json`。

也可以手动执行一次无界面检查：

```powershell
python auto_boot.py --run
```

## 规则

- 当前时间不在开始时间和结束时间构成的启动窗口内时不启动；支持 `22:00-06:00` 这样的跨午夜窗口。
- 启用工作日检查时，普通工作日和调休工作日启动，周末和节假日不启动。
- 节假日服务不可用时，本地日历为周六、周日则不启动；周一至周五是否启动由“断网兜底”选项决定。
- 节假日判断依次使用 Timor 主接口、本地年度缓存、jsDelivr 和 GitHub Raw 年度数据；在线数据均不可用时才降级到本地星期。
- 登录前自启通过 Windows 计划任务 `BootRunner Startup` 实现，触发器为系统启动，运行账户为 `SYSTEM`。
- 用户登录后通过当前用户的 `Run` 项再次检查规则，并在交互会话中启动软件 GUI，因此 GameViewer 等软件可以正常显示自己的托盘图标。

## 登录前运行限制

Windows 会将 `SYSTEM` 启动的程序放在隔离的 Session 0。命令行程序、后台服务以及明确支持服务模式的远程软件可以正常工作，但普通桌面程序的窗口不会显示在之后登录的用户桌面。

远程控制软件能在登录前工作，通常是因为安装时注册了自己的 Windows 服务，而不只是启动了桌面版 EXE。Boot Runner 在 `SYSTEM` 模式下会查找所选程序同目录的配套服务：找到时启动服务并跳过 GUI，找不到时记录错误并跳过该程序，避免 WebView2 等桌面组件在 `systemprofile` 下报错。用户登录后，Boot Runner 会在真实用户会话中启动 GUI 以恢复托盘图标。应优先在远程软件自身设置中启用“无人值守访问”或“随系统启动服务”。

## 测试

```powershell
python -m unittest -v
```
