# MiMo Trust 全链路信源核实 Demo

输入抖音、哔哩哔哩或 YouTube 的公开视频 URL、抖音图文链接、平台短链或手机完整分享文案，系统先提取完整原文并生成严格结构化主张，再自动完成检索规划、多源并发检索、证据语义初筛、逐主张判定和可追溯核验报告。

## 全链路流程

1. 解析公开内容并获取字幕、ASR、图文 OCR 或必要的画面观察；
2. 将内容转换成“内容主题、原子主张、隐性观点”标准协议；
3. 为全部主张生成检索计划和 claim 级证据契约；
4. 并发检索 Exa、OpenAlex、ArXiv、Wikipedia、Semantic Scholar，必要时使用 open-webSearch；
5. 对全部候选执行来源身份、关系、直接性和强度初筛；
6. 按证据门槛输出属实、部分属实、误导、虚假、待核实或缺乏证据等逐项结论；
7. 在网页展示综合判定、逐项依据、引用证据、检索计划与耗时；
8. 在 `data/trust/cases/<case_id>/runs/<run_id>/` 保留每一步 JSON、Markdown 报告和用量记录。

## 当前输出协议

完整字幕/ASR、逐图 OCR、画面观察和发布上下文会保存在 `full_source_text`、`transcript` 与 `keyframes`。面向阅读的 `cleaned_article` 会移除时间码、传输标签、重复 OCR 和截断碎片，再通过无损中文分句按发布内容、口播字幕与画面信息重组。服务端计算 `text_retention_percent`；低于 99% 时结果自动标记为 `partial`，不能把重组不完整的正文声明为完整。下游标准数据位于 `structured_data`：

```json
{
  "case_id": "watermelon-seed-rumor",
  "内容主题": "无籽西瓜、白籽西瓜食品安全谣言核验",
  "原子主张": [
    "使用激素喷洒雌花培育的西瓜，其种子会变白、萎缩并失去繁殖能力"
  ],
  "隐性观点": [
    "人工干预培育出的农作物不安全"
  ]
}
```

结构不是依赖提示词模拟：MiMo 请求使用 `response_format=json_schema` 和 `strict=true`，服务端再使用 Pydantic 以同一协议验证字段、类型、单条长度、中文完整句和去重。所有值得外部核验的事实性陈述统一进入 `原子主张`，不再设置容易产生重叠的 `新闻事实` 字段。两类数组不设固定数量上限；系统根据原文长度、视频时长和图文页数计算“自适应语义密度”软预算，引导模型先按核验事件聚类，再用最小充分命题覆盖关键核验路径。

## 自适应处理

1. 从手机分享文案抽取 URL，逐跳安全展开 `b23.tv` / `v.douyin.com`；
2. 区分视频和抖音 `awemeType=68` 图文轮播；
3. 优先使用完整平台字幕；无字幕时先对完整音频执行分段 ASR；
4. 字幕或 ASR 获得有效口播文本后立即短路，不下载视觉轨、不抽帧；
5. 只有无有效口播文本时，视频才提取场景变化/周期关键帧并执行 OCR；图文始终下载全部图片；
6. 合并发布上下文、完整语音、OCR 和画面信息；
7. 通过严格 JSON Schema 转换为下游标准数据；
8. 持久化完整原文、结构化数据、覆盖率和成本轨迹。

## 启动

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
# 至少填写 MIMO_API_KEY；推荐同时填写 EXA_API_KEY
uvicorn app.main:app --reload
```

打开 <http://127.0.0.1:8000>。接口文档位于 <http://127.0.0.1:8000/api/docs>。

开发环境通过独立、持久化的 Edge Profile 维护抖音会话，不读取个人浏览器 Cookie。Cookie 文件年龄只用于减少刷新次数；真正的健康检查是一次实际作品解析。遇到 Cookie/验证类错误时，各解析和下载阶段只强制刷新一次，并通过进程锁避免并发覆盖。

抖音视频优先监听抖音页面自身签名后的 `aweme/detail` 响应，直接选择包含完整 H.264 视频与 AAC 音频的低码率 MP4；yt-dlp 是兼容备用路径。`awemeType=68` 图文仍走页面作品数据解析并下载全部图片。两条路径均不访问私密、付费、登录限定或 DRM 内容。

若无头会话被抖音要求验证，先停止服务，在 `.env` 临时设置 `DOUYIN_BROWSER_HEADLESS=false`，启动并请求一次抖音作品，在弹出的专用 Edge 窗口中完成验证；随后恢复 `true`。该 Profile 只能用于低权限服务会话，不能指向个人浏览器目录。

生产环境建议将该浏览器适配器独立部署为低权限会话服务，保持稳定出口 IP，并监控真实作品探测成功率。若使用外部 Netscape Cookie 作为 yt-dlp 备用会话，应配置 `YTDLP_COOKIES_FILE`，并将导出浏览器当时的完整 UA 写入 `YTDLP_USER_AGENT`；一旦真实探测失败即轮换会话，而不是仅按固定 TTL 判断。

## 接口

- `POST /api/analyze`：默认执行内容提取与信源核实全链路；传 `verify=false` 可仅提取
- `POST /api/verify`：对已有标准结构化数据单独执行或重试信源核实
- `GET /api/videos`：列出持久化结果
- `DELETE /api/videos/{cache_key}`：删除单条
- `DELETE /api/videos`：清空全部

`mode=auto` 使用 L0–L3 成本阶梯自适应执行；`mode=visual` 强制执行 L3 全视频多模态补充。

下游核验模型可通过 `MIMO_PLANNING_MODEL`、`MIMO_TRIAGE_MODEL`、`MIMO_REPORT_MODEL` 和 `MIMO_REPORT_THINKING` 覆盖。未配置 Exa 时会尝试 `OPEN_WEBSEARCH_URL`，学术与百科检索轨仍可独立运行。

## 边界

- “隐性观点”是从输入措辞识别出的立场，不能作为已证实事实。
- 仅处理公开且用户有权访问的内容，不绕过 DRM、登录、付费、私密或地区限制。
- 视觉或结构化模型失败时保留完整原文，覆盖状态会标记为 `partial` 或 `needs_review`。
- 非当前协议版本的历史缓存会在启动时删除，避免旧结构继续流向下游。
