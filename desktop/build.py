"""
FreeTime PyInstaller 构建脚本

使用方法:
    cd desktop
    python build.py

需要:
    - Python 3.10+
    - 已激活虚拟环境并安装依赖
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
EXTRACTOR_DIR = PROJECT_ROOT / "apps" / "media-extractor"
DESKTOP_DIR = Path(__file__).parent
RESOURCES_DIR = DESKTOP_DIR / "resources" / "freetime-backend"
DIST_DIR = DESKTOP_DIR / "release"
BUILD_DIR = DESKTOP_DIR / "build"


def validate_build_python(version_info=None, version=None, executable=None):
    """Fail before PyInstaller can mix an unsupported runtime into a release."""
    version_info = sys.version_info if version_info is None else version_info
    version = sys.version.split()[0] if version is None else version
    executable = sys.executable if executable is None else executable
    if version_info < (3, 10):
        raise RuntimeError(
            f"Python 3.10+ is required for desktop builds; got {version} "
            f"from {executable}"
        )


def clean():
    """清理旧的构建产物"""
    print("[1/5] 清理旧构建...")
    for d in [RESOURCES_DIR, DIST_DIR, BUILD_DIR]:
        if d.exists():
            shutil.rmtree(d)
    for f in [DESKTOP_DIR / "freetime-backend.spec"]:
        if f.exists():
            f.unlink()


def run_pyinstaller():
    """使用 PyInstaller 打包 Python 后端"""
    print("[2/5] PyInstaller 打包...")

    RESOURCES_DIR.mkdir(parents=True, exist_ok=True)

    # 收集所有需要的数据文件
    # portal 目录
    portal_src = PROJECT_ROOT / "portal"
    # static 目录
    static_src = PROJECT_ROOT / "static"
    # chaoxing-auto 目录
    chaoxing_src = PROJECT_ROOT / "apps" / "chaoxing-auto"
    # extractor 的 static 目录
    extractor_static = EXTRACTOR_DIR / "static"
    # 构建 --add-data 参数
    add_data = []

    def add_src_dst(src, dst_name):
        if src.exists():
            # Windows 用 ; 分隔符
            sep = ";" if sys.platform == "win32" else ":"
            add_data.append(f"--add-data={src}{sep}{dst_name}")

    add_src_dst(portal_src, "portal")
    add_src_dst(static_src, "static")
    add_src_dst(chaoxing_src, "apps/chaoxing-auto")
    add_src_dst(extractor_static, "apps/media-extractor/static")
    # 收集隐藏导入
    hidden_imports = [
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.asgi",
        "fastapi",
        "fastapi.responses",
        "fastapi.staticfiles",
        "starlette",
        "starlette.responses",
        "pydantic",
        "yt_dlp",
        "bs4",
        "httpx",
        "dotenv",
        "multipart",
        "playwright",
        "flask",
        "PIL",
        "PIL.Image",
        "lxml",
        "lxml.etree",
        "lxml._elementpath",
        "imageio_ffmpeg",
        # app 包及其子模块（通过 run.py 动态导入）
        "app",
        "app.main",
        "app.cache",
        "app.config",
        "app.models",
        "app.pipeline",
        "app.content",
        "app.security",
        "app.thumbnails",
        "app.storage",
    ]

    hidden_args = []
    for hi in hidden_imports:
        hidden_args.extend(["--hidden-import", hi])

    # 构建 PyInstaller 命令
    # 使用 run.py 作为入口点（它会设置正确的路径后导入 app.main）
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "freetime-backend",
        "--onedir",
        f"--distpath={RESOURCES_DIR}",
        f"--workpath={DESKTOP_DIR / 'build'}",
        f"--specpath={DESKTOP_DIR}",
        "--noconfirm",
        "--clean",
        *add_data,
        *hidden_args,
        str(DESKTOP_DIR / "run.py"),
    ]

    print(f"  命令: {' '.join(cmd[:5])}...")
    result = subprocess.run(cmd, cwd=str(EXTRACTOR_DIR))
    if result.returncode != 0:
        print("[ERROR] PyInstaller 构建失败!")
        sys.exit(1)

    print(f"  产出: {RESOURCES_DIR}")


def copy_static_files():
    """复制额外的静态文件到 resources"""
    print("[3/5] 复制静态文件...")

    backend_dir = RESOURCES_DIR / "freetime-backend"
    if not backend_dir.exists():
        print("[WARN] PyInstaller 产出目录不存在，跳过静态文件复制")
        return

    # 确保 portal 目录存在
    portal_dst = backend_dir / "portal"
    if not portal_dst.exists():
        shutil.copytree(PROJECT_ROOT / "portal", portal_dst)

    # 确保 static 目录存在
    static_dst = backend_dir / "static"
    if not static_dst.exists():
        shutil.copytree(PROJECT_ROOT / "static", static_dst)

    # 确保 chaoxing-auto 存在
    chaoxing_dst = backend_dir / "apps" / "chaoxing-auto"
    if not chaoxing_dst.exists():
        shutil.copytree(PROJECT_ROOT / "apps" / "chaoxing-auto", chaoxing_dst)

    # 确保 extractor static 存在
    extractor_static_dst = backend_dir / "apps" / "media-extractor" / "static"
    if not extractor_static_dst.exists():
        shutil.copytree(EXTRACTOR_DIR / "static", extractor_static_dst)


def install_electron_deps():
    """安装 Electron 依赖"""
    print("[4/5] 安装 Electron 依赖...")
    result = subprocess.run(
        ["npm", "install"],
        cwd=str(DESKTOP_DIR),
        shell=True,
    )
    if result.returncode != 0:
        print("[ERROR] npm install 失败!")
        sys.exit(1)


def build_electron():
    """构建 Electron 安装包"""
    print("[5/5] 构建 Electron 安装包...")
    result = subprocess.run(
        ["npm", "run", "build"],
        cwd=str(DESKTOP_DIR),
        shell=True,
    )
    if result.returncode != 0:
        print("[ERROR] Electron 构建失败!")
        sys.exit(1)

    installers = list(DIST_DIR.glob("FreeTime*.exe"))
    if installers:
        for installer in installers:
            print(f"  产物: {installer.name}")
        print(f"\n[SUCCESS] 最新发布目录: {DIST_DIR}")
    else:
        print(f"\n[WARN] 未找到安装包，请检查 {DIST_DIR} 目录")


def main():
    validate_build_python()
    print("=" * 50)
    print("  FreeTime 桌面版构建")
    print("=" * 50)

    clean()
    run_pyinstaller()
    copy_static_files()
    install_electron_deps()
    build_electron()


if __name__ == "__main__":
    main()
