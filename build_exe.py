"""
Boot Runner 可执行文件打包脚本
使用 PyInstaller 将 auto_boot.py 编译为单文件无控制台 Windows EXE。
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def build() -> int:
    project_dir = Path(__file__).resolve().parent
    entry_script = project_dir / "auto_boot.py"
    dist_dir = project_dir / "dist"
    build_dir = project_dir / "build"

    print(f"[*] 开始构建 Boot Runner 可执行文件...")
    print(f"[*] 项目根目录: {project_dir}")
    print(f"[*] 入口文件: {entry_script}")

    # 检查 PyInstaller 是否可用
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[!] 错误: 当前环境中未安装 PyInstaller。")
        print("[!] 请先运行: pip install pyinstaller")
        return 1

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconsole",          # 无黑框控制台
        "--onefile",            # 单文件打包
        "--name=BootRunner",    # 生成 BootRunner.exe
        "--clean",              # 清理临时缓存
        f"--distpath={dist_dir}",
        f"--workpath={build_dir}",
        str(entry_script),
    ]

    print(f"[*] 执行构建命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(project_dir))

    if result.returncode != 0:
        print(f"[!] 构建失败，退出码: {result.returncode}")
        return result.returncode

    target_exe = dist_dir / "BootRunner.exe"
    if target_exe.is_file():
        size_mb = target_exe.stat().st_size / (1024 * 1024)
        print(f"[+] 构建成功！")
        print(f"[+] 目标文件: {target_exe} ({size_mb:.2f} MB)")
        print(f"[+] 日志、配置及节假日缓存文件将在运行时默认保存在与 BootRunner.exe 同级的目录下。")
        return 0
    else:
        print("[!] 构建命令完成，但未找到预期的输出文件。")
        return 1


if __name__ == "__main__":
    raise SystemExit(build())
