"""
auth.py - 登录与会话持久化
首次运行时打开浏览器让用户手动登录，之后复用 Playwright storageState。
"""

import os
from playwright.sync_api import Browser, BrowserContext


STATE_FILE = "auth_state.json"
BASE_URL = "https://i.chaoxing.com/base"


def has_saved_session(state_file: str = STATE_FILE) -> bool:
    """检查是否有已保存的登录状态文件."""
    return os.path.exists(state_file)


def create_context(browser: Browser, state_file: str = STATE_FILE) -> BrowserContext:
    """
    创建浏览器上下文。
    如果有已保存的 session 则复用，否则创建新的（需手动登录）。
    """
    if has_saved_session(state_file):
        print(f"[AUTH] ♻ 复用已保存的登录状态 ({state_file})")
        return browser.new_context(storage_state=state_file)
    else:
        print("[AUTH] 🆕 首次运行，需要手动登录")
        return browser.new_context()


def do_login(context: BrowserContext, state_file: str = STATE_FILE):
    """
    导航到超星首页，检测是否需要登录。
    如需登录则等待用户手动完成，并保存状态。
    """
    page = context.new_page()
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)

    # 检测是否被重定向到登录页
    current_url = page.url
    if "passport" in current_url or "login" in current_url.lower():
        print("[AUTH] 🔐 请在浏览器中手动登录（扫码或账号密码均可）")
        print("[AUTH] ⏳ 等待登录完成...")

        # 等待重定向回主页，最长等待 5 分钟
        try:
            page.wait_for_url("**/base**", timeout=300_000)
        except Exception:
            # 可能已经登录但 URL 没变
            page.wait_for_timeout(3000)

        # 保存登录状态
        context.storage_state(path=state_file)
        print(f"[AUTH] ✅ 登录状态已保存到 {state_file}")
    else:
        print("[AUTH] ✅ 已登录，无需重复登录")

    page.close()
