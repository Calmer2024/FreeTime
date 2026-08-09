"""
FreeTime 桌面版启动器 - 供 PyInstaller 打包使用

PyInstaller onedir 打包后的目录结构:
  freetime-backend/
  ├── freetime-backend.exe
  ├── .port
  └── _internal/
      ├── app/           ← Python 字节码（app 包）
      ├── portal/
      ├── static/
      ├── apps/
      └── ... (Python 运行时)
"""
import os
import socket
import sys
from pathlib import Path

# ========== 路径设置（必须在任何 app 导入之前）==========
if getattr(sys, "frozen", False):
    _exe_dir = os.path.dirname(sys.executable)
    _internal_dir = sys._MEIPASS
    # 工作目录设为 _internal（portal/, static/, .env 都在这里）
    os.chdir(_internal_dir)
    for p in [_internal_dir, _exe_dir]:
        if p not in sys.path:
            sys.path.insert(0, p)

    from app.storage import migrate_legacy_data

    _data_dir = Path(os.environ.get("FREETIME_DATA_DIR") or _exe_dir)
    migrate_legacy_data(
        _data_dir,
        [Path(_exe_dir), Path(_internal_dir)],
    )

# ========== 导入 app（PyInstaller 静态分析会追踪到这里）==========
from app.main import app as fastapi_app  # noqa: E402


def find_free_port(start=8000, end=9000):
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def main():
    port = find_free_port()

    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        # .port 文件写到 exe 同级目录（Electron 从 process.resourcesPath 读取）
        port_file = os.path.join(exe_dir, ".port")
        with open(port_file, "w") as f:
            f.write(str(port))
        print(f"[FreeTime] 启动服务，端口: {port}", flush=True)
        print(f"[FreeTime] 工作目录: {os.getcwd()}", flush=True)
    else:
        print(f"[FreeTime-Dev] 启动服务，端口: {port}", flush=True)
        print(f"[FreeTime-Dev] 工作目录: {os.getcwd()}", flush=True)

    import uvicorn
    uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
