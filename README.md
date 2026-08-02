# FreeTime - 节省时间的应用工具箱

FreeTime 是一个帮助用户节省时间的应用工具箱，包含多个实用工具应用。

## 应用列表

### 1. 流媒体内容提取器

一键提取抖音、B站、YouTube、快手、微博、小红书、视频号等热门平台的内容。

**功能特性：**
- 支持主流流媒体平台内容提取
- 自动解析视频、图文、直播等多种形式
- 智能提取关键信息并结构化展示
- 一键复制提取内容
- 支持多种提取模式（智能/全模态）

**使用方法：**
```bash
cd apps/media-extractor
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

访问 http://localhost:8000

### 2. 超星刷课助手

自动完成超星学习通课程视频播放，支持倍速播放、自动跳过弹窗、断点续播。

**功能特性：**
- 自动登录并保持会话
- 智能识别未完成课程
- 支持自定义播放倍速
- 自动处理弹窗和验证
- 断点续播功能

**使用方法：**
```bash
cd apps/chaoxing-auto
pip install -r requirements.txt

# 方式1: 命令行运行
python main.py

# 方式2: Web UI
python app.py
```

访问 http://localhost:8001 使用 Web UI

## 项目结构

```
FreeTime/
├── README.md                    # 本文件
├── apps/
│   ├── media-extractor/         # 流媒体内容提取器
│   │   ├── app/
│   │   │   ├── main.py          # FastAPI 入口
│   │   │   ├── models.py        # 数据模型
│   │   │   ├── pipeline.py      # 提取管线
│   │   │   └── ...
│   │   ├── static/
│   │   │   ├── index.html
│   │   │   ├── app.js
│   │   │   └── app.css
│   │   └── requirements.txt
│   │
│   └── chaoxing-auto/           # 超星刷课助手
│       ├── main.py              # 主程序
│       ├── app.py               # Web UI
│       ├── config.json          # 配置文件
│       ├── templates/
│       └── requirements.txt
│
├── portal/                      # FreeTime 主门户
│   ├── index.html
│   └── style.css
│
└── docs/
    └── ...
```

## 技术栈

- **流媒体内容提取器**: Python, FastAPI, yt-dlp, Playwright
- **超星刷课助手**: Python, Playwright, Flask
- **主门户**: HTML, CSS, JavaScript

## 快速开始

1. 克隆仓库
```bash
git clone https://github.com/Calmer2024/FreeTime.git
cd FreeTime
```

2. 启动主门户（可选，使用任何静态文件服务器）
```bash
cd portal
python -m http.server 8080
```

3. 启动具体应用（见各应用目录的 README）

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！
