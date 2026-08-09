"""
PyInstaller 分析入口 - 确保 app 模块被正确追踪
这个文件只用于 PyInstaller 的静态分析，实际运行用 run.py
"""
# 导入 app 包 - PyInstaller 会追踪这些导入
from app.main import app  # noqa: F401
from app.cache import ResultCache  # noqa: F401
from app.config import settings  # noqa: F401
from app.models import AnalyzeRequest, AnalyzeResponse  # noqa: F401
from app.pipeline import analyze  # noqa: F401
from app.content import analyze_article_url  # noqa: F401
from app.security import resolve_content_input  # noqa: F401
from app.thumbnails import thumbnail_store  # noqa: F401
