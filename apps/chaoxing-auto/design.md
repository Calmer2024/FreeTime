# 超星自动刷课脚本 - 设计文档

## 概述
基于 Python + Playwright 的超星学习通自动化刷课脚本。支持最大倍速播放、自动切换视频、弹窗处理、会话持久化。

## 架构
```
chaoxing-auto/
├── main.py            # 入口，编排流程
├── config.json         # 课程 URL/名称、倍速配置
├── auth.py             # 登录 + session 持久化
├── course.py           # 课程导航，任务列表
├── player.py           # 播放器状态机
└── requirements.txt    # playwright
```

## 播放器状态机
LOADING → PLAYING → COMPLETED (正常流程)
PLAYING → POPUP → PLAYING (弹窗打断)
任意状态 → ERROR → LOADING (重试)

## 弹窗处理
- 确认类弹窗（"确定"/"继续"/"我知道了"）→ 自动点击
- 验证码弹窗 → 截图保存 + 等待用户手动处理
- 暂停提示 → 自动恢复播放

## 配置
- course: url 或 name 指定课程
- playback: speed(默认2x), poll_interval(3s), max_retries(3)
- auth: state_file 持久化登录
