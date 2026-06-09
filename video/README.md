# A股收盘复盘短视频（HyperFrames）

把每日结构化快照渲染成 9:16 竖屏复盘短视频的工程目录。整条链路：

```
快照 snapshot.json
   └─(P0) recap/storyboard.py      → webdata/recaps/<date>.json   分镜脚本（确定性，单一数据源）
        └─(P3) recap/compose.py    → video/build/<date>/composition.html  自包含 HyperFrames 分镜 HTML
             └─(P4) 字幕 + 配音     → captions.srt/.vtt + audio/*.mp3
                  └─ hyperframes render → video/build/<date>/recap_<date>.mp4
```

演出视图（无需出片即可大屏放映 / 截帧）：浏览器打开 `http://127.0.0.1:8000/show/<date>`。

## 环境要求

- Node.js >= 22
- FFmpeg（在 PATH 中）
- 渲染机可访问 `cdn.jsdelivr.net`（GSAP）与 `fonts.googleapis.com`（字体）；离线渲染见下方「离线」。

## 安装

```bash
cd video
npm install        # 安装 hyperframes CLI
```

## 一键出片（推荐）

在项目根目录执行流水线（自动：分镜 → 字幕 → 配音 → 合成 HTML），它会打印最终 render 命令：

```bash
python scripts/make_recap_video.py 20260605            # 指定交易日
python scripts/make_recap_video.py                     # 最新交易日
python scripts/make_recap_video.py 20260605 --render   # 末尾自动调用 hyperframes render
```

只想生成分镜 HTML 并拿到 render 命令（不配音、不做字幕）：

```bash
python scripts/render_recap.py 20260605
python scripts/render_recap.py 20260605 --render
```

## 手动渲染 / 校验

```bash
cd video
npx hyperframes lint   build/20260605/composition.html
npx hyperframes preview build/20260605/composition.html      # 浏览器热预览
npx hyperframes render build/20260605/composition.html --output build/20260605/recap_20260605.mp4
```

## 目录结构

```
video/
  package.json          # hyperframes CLI 依赖与脚本
  README.md
  build/                # 每日产物（git 忽略，可重建）
    <date>/
      composition.html  # 自包含分镜（recap/compose.py 生成）
      storyboard.json   # 当日分镜脚本副本（排查用）
      captions.srt      # 字幕（由时间轴确定性生成，无需 Whisper）
      captions.vtt
      audio/            # 逐幕配音（TTS，可选）
      recap_<date>.mp4  # 最终成片
```

## 离线渲染

分镜 HTML 默认引用 CDN 上的 GSAP 与字体。完全离线时：

- GSAP：把 `gsap.min.js` 放到本地，调用 `recap.compose.build_composition_html(story, gsap_cdn="./gsap.min.js")`；
- 字体：在渲染机预装 Noto Sans SC / JetBrains Mono，并按需改写 `<link>`。

## 设计要点

- 分镜 HTML 完全自包含、确定性：同一 storyboard 永远生成同一 HTML，HyperFrames 逐帧 seek 出片可复现。
- 每幕是一个 `class="clip"` 元素，自带 `data-start/data-duration/data-track-index`；GSAP 时间轴注册在 `window.__timelines["root"]`，总时长由 `tl.duration()` 决定。
- 涨跌配色遵循 A股惯例（红涨绿跌），主色由当日情绪周期决定。
