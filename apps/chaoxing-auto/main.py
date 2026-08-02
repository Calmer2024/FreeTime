"""
main.py - 超星学习通自动刷课脚本入口
自动以最大倍速播放视频，完成后自动切换到下一个，直到所有视频刷完。

用法：
  1. 编辑 config.json，填入课程 URL 或名称
  2. python main.py
  3. 首次运行会打开浏览器让你手动登录
  4. 后续运行自动复用登录状态
"""

import json
import os
import sys
from datetime import datetime

# 修复 Windows 控制台 emoji 编码问题
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from playwright.sync_api import sync_playwright

import auth
import course as course_mod
import player as player_mod


def now() -> str:
    """返回带时间戳的日志前缀."""
    return datetime.now().strftime("[%H:%M:%S]")


def load_config(path: str = "config.json") -> dict:
    """加载配置文件."""
    if not os.path.exists(path):
        print(f"❌ 配置文件 {path} 不存在")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)

    # 验证配置
    course_cfg = config.get("course", {})
    if not course_cfg.get("url") and not course_cfg.get("name"):
        print("❌ 请在 config.json 中填写 course.url 或 course.name")
        sys.exit(1)

    return config


def main():
    print("=" * 50)
    print("  超星学习通 - 自动刷课脚本")
    print("=" * 50)

    config = load_config()
    playback_cfg = config.get("playback", {})

    with sync_playwright() as p:
        # ---- 启动浏览器 ----
        print(f"{now()} 🚀 启动浏览器...")
        browser = p.chromium.launch(
            headless=False,  # 必须显示窗口，否则超星会检测
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )

        # ---- 登录 ----
        context = auth.create_context(browser, config["auth"]["state_file"])
        auth.do_login(context, config["auth"]["state_file"])

        # 创建主页面
        page = context.new_page()
        page.set_default_timeout(30_000)

        try:
            # ---- 进入课程 ----
            course_url = course_mod.find_course(
                page,
                config["course"].get("url", ""),
                config["course"].get("name", ""),
            )

            # ---- 获取任务列表 ----
            tasks = course_mod.get_tasks(page)

            if not tasks:
                print(f"{now()} 🎉 没有未完成的任务，所有视频已刷完！")
                return

            print(f"\n{now()} 📋 共 {len(tasks)} 个任务待完成\n")

            # ---- 逐个完成 ----
            completed = 0
            failed = 0
            skipped = 0

            for i, task in enumerate(tasks):
                print(f"{now()} ─────────────────────────────")
                print(f"{now()} ▶ [{i+1}/{len(tasks)}] {task['title']}")

                # 点击任务
                ok = course_mod.click_task(page, task["element_index"])
                if not ok:
                    print(f"{now()} ⚠ 无法点击任务，跳过")
                    skipped += 1
                    continue

                # 播放视频
                controller = player_mod.PlayerController(page, playback_cfg)
                success = controller.watch()

                if success:
                    completed += 1
                elif controller.retry_count >= controller.max_retries:
                    failed += 1
                else:
                    skipped += 1

                # 返回课程页面
                page.go_back()
                page.wait_for_timeout(2000)

            # ---- 总结 ----
            print(f"\n{'=' * 50}")
            print(f"{now()} 🎉 刷课完成！")
            print(f"  ✅ 完成: {completed}")
            print(f"  ❌ 失败: {failed}")
            print(f"  ⏭ 跳过: {skipped}")
            print(f"{'=' * 50}")

        except Exception as e:
            print(f"{now()} ❌ 运行出错: {e}")
            import traceback
            traceback.print_exc()

        finally:
            # 给用户几秒看看结果
            print(f"\n{now()} 浏览器将在 5 秒后关闭...")
            page.wait_for_timeout(5_000)
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
