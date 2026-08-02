"""
player.py - 视频播放器控制模块
状态机驱动的播放器：LOADING → PLAYING → COMPLETED/ERROR，处理弹窗打断。
"""

import time
from enum import Enum, auto
from typing import Optional
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout


class State(Enum):
    LOADING = auto()
    PLAYING = auto()
    POPUP = auto()
    COMPLETED = auto()
    ERROR = auto()


class PlayerController:
    """视频播放器状态机控制器。"""

    def __init__(self, page: Page, config: dict):
        self.page = page
        self.speed = config.get("speed", 2.0)
        self.poll_interval = config.get("poll_interval_seconds", 3)
        self.max_retries = config.get("max_retries", 3)
        self.load_timeout = config.get("load_timeout_seconds", 60)

        self.state = State.LOADING
        self.retry_count = 0
        self.last_progress = 0.0
        self.stuck_count = 0

    def watch(self) -> bool:
        """
        主循环：驱动状态机直到视频完成或失败。
        返回 True 表示完成，False 表示失败。
        """
        start_time = time.time()
        print(f"[PLAYER] ▶ 开始播放 (倍速: {self.speed}x)")

        self.state = State.LOADING

        while self.state not in (State.COMPLETED, State.ERROR):
            if self.state == State.LOADING:
                self._handle_loading()
            elif self.state == State.PLAYING:
                self._handle_playing()
            elif self.state == State.POPUP:
                self._handle_popup()

        elapsed = time.time() - start_time
        if self.state == State.COMPLETED:
            mins, secs = divmod(int(elapsed), 60)
            print(f"[PLAYER] ✅ 播放完成 ({mins}m{secs}s)")
            return True
        else:
            print(f"[PLAYER] ❌ 播放失败（已重试 {self.retry_count} 次）")
            return False

    # ------------------------------------------------------------------
    # 状态处理
    # ------------------------------------------------------------------

    def _handle_loading(self):
        """LOADING: 等待视频元素加载，找到播放器。"""
        print("[PLAYER] ⏳ 等待视频加载...")

        try:
            # 等待视频元素出现在页面或 iframe 中
            video = self._find_video_element()
            if video:
                self._set_speed(video)
                self.state = State.PLAYING
                self.stuck_count = 0
                return
        except Exception:
            pass

        # 检查加载是否超时
        if self.retry_count >= self.max_retries:
            self.state = State.ERROR
            return

        self.retry_count += 1
        print(f"[PLAYER] ⚠ 加载超时，重试 ({self.retry_count}/{self.max_retries})")
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_timeout(3000)

    def _handle_playing(self):
        """PLAYING: 监控播放进度，检测弹窗，检测完成。"""
        self.page.wait_for_timeout(self.poll_interval * 1000)

        # 1. 先检测弹窗
        if self._detect_popup():
            self._dismiss_popup()
            self.state = State.POPUP
            return

        # 2. 检测视频完成
        if self._is_video_completed():
            self.state = State.COMPLETED
            return

        # 3. 检测进度（防卡死）
        progress = self._get_progress()
        if progress is not None:
            if abs(progress - self.last_progress) < 0.001:
                self.stuck_count += 1
                if self.stuck_count >= 5:  # 连续 5 次不动 ≈ 15 秒
                    print("[PLAYER] ⚠ 视频卡住，尝试刷新...")
                    self.page.reload(wait_until="domcontentloaded")
                    self.page.wait_for_timeout(3000)
                    self.state = State.LOADING
                    self.stuck_count = 0
                    return
            else:
                self.stuck_count = 0
                self.last_progress = progress

        # 4. 确保还在播放，如果是暂停状态就点一下播放
        self._ensure_playing()

    def _handle_popup(self):
        """POPUP: 弹窗已关闭，恢复播放。"""
        self.page.wait_for_timeout(1000)
        # 弹窗关闭后可能需要重新设置倍速
        try:
            video = self._find_video_element()
            if video:
                self._set_speed(video)
        except Exception:
            pass
        self.state = State.PLAYING

    # ------------------------------------------------------------------
    # 视频元素查找
    # ------------------------------------------------------------------

    def _find_video_element(self):
        """
        查找视频元素。先查主页面，再查 iframe。
        返回 video 元素的 JS handle，或 None。
        """
        # 在主页面中查找
        try:
            video = self.page.locator("video").first
            if video.count() > 0:
                return video
        except Exception:
            pass

        # 在所有 iframe 中查找
        for frame in self.page.frames:
            try:
                video = frame.locator("video").first
                if video.count() > 0:
                    return video
            except Exception:
                continue

        return None

    # ------------------------------------------------------------------
    # 倍速设置
    # ------------------------------------------------------------------

    def _set_speed(self, video) -> None:
        """设置视频播放速度为最大值。多重策略兜底。"""

        # 策略 1：通过 JS 直接设置 playbackRate
        try:
            video.evaluate(f"el => el.playbackRate = {self.speed}")
            # 验证
            rate = video.evaluate("el => el.playbackRate")
            print(f"[PLAYER] ⚡ 倍速已设为 {rate}x (JS API)")
            return
        except Exception as e:
            print(f"[PLAYER] JS 调速失败: {e}")

        # 策略 2：点击速度按钮
        try:
            # 在 iframe 和主页中查找速度按钮
            for frame in [self.page] + self.page.frames:
                speed_btn = frame.locator(
                    "[class*='speed'], [class*='rate'], "
                    "button[data-rate], .speed-button, "
                    ".playback-rate, .vjs-playback-rate"
                ).first
                if speed_btn.count() > 0:
                    speed_btn.click()
                    frame.wait_for_timeout(500)
                    # 选择最大倍速
                    max_rate = frame.locator(
                        "[class*='speed'] [class*='item']:last-child, "
                        ".speed-option:last-child, "
                        "[data-rate='2.0'], [data-value='2.0']"
                    ).first
                    if max_rate.count() > 0:
                        max_rate.click()
                        print(f"[PLAYER] ⚡ 通过 UI 设置倍速")
                        return
        except Exception as e:
            print(f"[PLAYER] UI 调速失败: {e}")

    # ------------------------------------------------------------------
    # 弹窗检测与处理
    # ------------------------------------------------------------------

    def _detect_popup(self) -> bool:
        """检测是否有需要处理的弹窗。"""
        popup_keywords = [
            "继续学习", "确定", "我知道了", "好的", "知道了",
            "确认", "继续", "关闭",
        ]

        try:
            for frame in [self.page] + self.page.frames:
                for keyword in popup_keywords:
                    btn = frame.locator(f"button:has-text('{keyword}'), "
                                        f"a:has-text('{keyword}'), "
                                        f"[class*='dialog'] :has-text('{keyword}')").first
                    if btn.count() > 0 and btn.is_visible():
                        return True
        except Exception:
            pass

        return False

    def _dismiss_popup(self) -> None:
        """关闭/确认弹窗。"""
        popup_keywords = [
            "继续学习", "确定", "我知道了", "好的", "知道了",
            "确认", "继续", "关闭",
        ]

        for frame in [self.page] + self.page.frames:
            for keyword in popup_keywords:
                try:
                    btn = frame.locator(
                        f"button:has-text('{keyword}'), "
                        f"a:has-text('{keyword}'), "
                        f"[class*='dialog'] :has-text('{keyword}')"
                    ).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click()
                        print(f"[PLAYER] 🔔 已关闭弹窗: '{keyword}'")
                        return
                except Exception:
                    continue

        # 如果找不到按钮，尝试按 ESC
        self.page.keyboard.press("Escape")

    # ------------------------------------------------------------------
    # 进度与完成检测
    # ------------------------------------------------------------------

    def _get_progress(self) -> Optional[float]:
        """获取当前视频播放进度 (0.0 ~ 1.0)。"""
        try:
            for frame in [self.page] + self.page.frames:
                video = frame.locator("video").first
                if video.count() > 0:
                    current = video.evaluate("el => el.currentTime")
                    duration = video.evaluate("el => el.duration")
                    if duration and duration > 0:
                        return current / duration
        except Exception:
            pass
        return None

    def _is_video_completed(self) -> bool:
        """检测视频是否已播放完毕。"""
        # 方法 1：检查进度
        progress = self._get_progress()
        if progress is not None and progress >= 0.99:
            return True

        # 方法 2：检查页面上的"已完成"标记
        completion_keywords = [
            "已完成", "已观看完毕", "学习完成", "已完成学习",
            "观看完毕", "任务完成",
        ]
        try:
            for frame in [self.page] + self.page.frames:
                page_text = frame.locator("body").inner_text() or ""
                for kw in completion_keywords:
                    if kw in page_text:
                        return True
        except Exception:
            pass

        # 方法 3：检查视频 ended 属性
        try:
            for frame in [self.page] + self.page.frames:
                video = frame.locator("video").first
                if video.count() > 0:
                    ended = video.evaluate("el => el.ended")
                    if ended:
                        return True
        except Exception:
            pass

        return False

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _ensure_playing(self) -> None:
        """确保视频处于播放状态（不是暂停）。"""
        try:
            for frame in [self.page] + self.page.frames:
                video = frame.locator("video").first
                if video.count() > 0:
                    paused = video.evaluate("el => el.paused")
                    if paused:
                        # 尝试点击播放按钮
                        play_btn = frame.locator(
                            ".vjs-big-play-button, [class*='play'], "
                            "button[title*='Play'], [aria-label*='Play']"
                        ).first
                        if play_btn.count() > 0:
                            play_btn.click()
                        else:
                            video.evaluate("el => el.play()")
                        print("[PLAYER] ▶ 已恢复播放")
        except Exception:
            pass
