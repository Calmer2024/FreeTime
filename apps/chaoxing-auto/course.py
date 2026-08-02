"""
course.py - 课程导航模块
根据 URL 或名称定位课程，获取未完成的任务列表。
"""

from playwright.sync_api import Page

BASE_URL = "https://i.chaoxing.com/base"


def find_course(page: Page, course_url: str = "", course_name: str = "") -> str:
    """
    导航到目标课程页面。
    优先使用 URL，其次按名称搜索。
    返回课程页面的 URL。
    """
    if course_url:
        print(f"[COURSE] 🔗 通过 URL 进入课程: {course_url}")
        page.goto(course_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3000)
        return page.url

    if course_name:
        print(f"[COURSE] 🔍 通过名称搜索课程: {course_name}")

        # 确保在课程列表页
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3000)

        # 尝试多种选择器定位课程链接
        selectors = [
            f".course-item a:has-text('{course_name}')",
            f".courselist a:has-text('{course_name}')",
            f"a[title*='{course_name}']",
            f"a:has-text('{course_name}')",
        ]

        for sel in selectors:
            link = page.locator(sel).first
            if link.count() > 0:
                link.click()
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(3000)
                print(f"[COURSE] ✅ 已进入课程: {course_name}")
                return page.url

        raise Exception(f"❌ 未找到课程: {course_name}")

    raise Exception("❌ 请提供 course.url 或 course.name")


def get_tasks(page: Page) -> list[dict]:
    """
    从课程页面获取所有未完成的视频任务。
    返回任务列表，每个任务包含 index, title。

    超星课程页面通常有一个左侧章节导航栏，
    每个章节下有若干任务点（视频/文档/测验等）。
    """
    tasks = []
    page.wait_for_timeout(2000)

    # ============================================================
    # 策略 1：查找章节列表（新版界面）
    # ============================================================
    # 常见的章节容器
    chapter_selectors = [
        ".catalogList li",           # 新版章节列表
        ".catalog_sbar li",          # 侧边栏章节
        ".chapter-list li",          # 章节列表
        "[class*='chapter'] li",     # 含 chapter 类的 li
        ".catalog_level li",         # 目录层级
    ]

    task_items = []
    for sel in chapter_selectors:
        items = page.locator(sel).all()
        if len(items) > 2:  # 至少有几个才认为是有效列表
            task_items = items
            print(f"[COURSE] 使用选择器 '{sel}' 找到 {len(items)} 个条目")
            break

    # ============================================================
    # 策略 2：在 iframe 中查找（某些课程把导航放在 iframe 里）
    # ============================================================
    if not task_items:
        print("[COURSE] 主页面未找到章节列表，尝试在 iframe 中查找...")
        frames = page.frames
        for frame in frames:
            for sel in chapter_selectors:
                items = frame.locator(sel).all()
                if len(items) > 2:
                    task_items = items
                    print(f"[COURSE] 在 iframe 中使用 '{sel}' 找到 {len(items)} 个条目")
                    break
            if task_items:
                break

    # ============================================================
    # 策略 3：查找页面中所有可点击的任务点链接
    # ============================================================
    if not task_items:
        print("[COURSE] 使用通用策略：查找所有可能的任务链接...")
        # 查找所有看起来像视频任务的链接
        possible_links = page.locator(
            "a[href*='chapter'], a[href*='knowledge'], "
            "a[href*='video'], a[href*='study'], "
            "[class*='task'] a, [class*='item'] a"
        ).all()
        if possible_links:
            task_items = possible_links
            print(f"[COURSE] 通用策略找到 {len(possible_links)} 个链接")

    # ============================================================
    # 解析任务信息
    # ============================================================
    for i, item in enumerate(task_items):
        try:
            # 获取标题
            title = ""
            title_selectors = [
                ".catalog_title", ".catalog_name", ".chapter_name",
                ".title", "a", "span", ".name", ".task-name",
            ]
            for ts in title_selectors:
                el = item.locator(ts).first
                if el.count() > 0:
                    title = el.inner_text().strip()
                    if title:
                        break

            if not title:
                title = item.inner_text().strip()

            if not title or len(title) < 2:
                continue

            # 检查是否已完成
            item_class = (item.get_attribute("class") or "").lower()
            item_html = item.inner_html() or ""
            is_completed = any(kw in item_class + item_html for kw in [
                "completed", "finished", "done", "green", "success",
                "icon-ok", "icon-finish", "icon-done", "pass",
                "已通过", "已完成",
            ])

            if is_completed:
                continue

            # 优先识别视频任务，但也保留其他类型（可能需要刷）
            icon_html = item.inner_html() or ""
            is_video = any(kw in icon_html.lower() for kw in [
                "video", "mp4", "播放", "icon-play", "icon-video",
                "flv", "视频",
            ])

            # 不过滤非视频任务——有些任务虽然图标不是视频但实际是视频
            tasks.append({
                "index": i,
                "title": title[:80],  # 截断过长的标题
                "element_index": i,   # 保存索引以便后续定位
                "is_video": is_video,
            })

        except Exception as e:
            # 单个任务解析失败不影响整体
            continue

    print(f"[COURSE] 📋 共找到 {len(tasks)} 个未完成任务")
    for t in tasks:
        icon = "🎬" if t["is_video"] else "📄"
        print(f"  {icon} [{t['index']}] {t['title']}")

    return tasks


def click_task(page: Page, task_index: int):
    """
    点击指定索引的任务，进入视频/内容页面。
    任务索引对应 get_tasks 返回列表中的 element_index。
    """
    # 重新获取任务元素并点击
    # 由于页面可能已刷新，需要重新查找
    selectors = [
        ".catalogList li",
        ".catalog_sbar li",
        ".chapter-list li",
        "[class*='chapter'] li",
        ".catalog_level li",
        "a[href*='chapter'], a[href*='knowledge']",
    ]

    for sel in selectors:
        items = page.locator(sel).all()
        if len(items) > task_index:
            try:
                item = items[task_index]
                # 尝试点击内部的链接
                link = item.locator("a").first
                if link.count() > 0:
                    link.click()
                else:
                    item.click()

                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(3000)
                return True
            except Exception as e:
                print(f"[COURSE] ⚠ 点击任务 [{task_index}] 失败: {e}")
                continue

    # Fallback: 直接点元素
    try:
        items = page.locator(selectors[0]).all()
        if len(items) > task_index:
            items[task_index].click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(3000)
            return True
    except Exception:
        pass

    return False
