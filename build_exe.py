"""
Boot Runner 可执行文件打包脚本
使用 PyInstaller 将 auto_boot.py 编译为单文件无控制台 Windows EXE。
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


# 确保在任何语言区域的终端下均能安全输出 UTF-8 文本，防止在 Windows CI (cp1252) 下抛出 UnicodeEncodeError
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def build() -> int:
    project_dir = Path(__file__).resolve().parent
    entry_script = project_dir / "auto_boot.py"
    dist_dir = project_dir / "dist"
    build_dir = project_dir / "build"

    print(f"[*] Starting Boot Runner build process...")
    print(f"[*] Project root: {project_dir}")
    print(f"[*] Entry script: {entry_script}")

    # 检查 PyInstaller 是否可用
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[!] Error: PyInstaller is not installed in the current environment.")
        print("[!] Please run: pip install pyinstaller")
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

    print(f"[*] Executing build command: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(project_dir))

    if result.returncode != 0:
        print(f"[!] Build failed with exit code: {result.returncode}")
        return result.returncode

    target_exe = dist_dir / "BootRunner.exe"
    if target_exe.is_file():
        size_mb = target_exe.stat().st_size / (1024 * 1024)
        print(f"[+] Build succeeded!")
        print(f"[+] Output executable: {target_exe} ({size_mb:.2f} MB)")
        return 0
    else:
        print("[!] Build command finished but expected output file was not found.")
        return 1


if __name__ == "__main__":
    raise SystemExit(build())
