"""复盘短视频生成包。

- P0 storyboard：每日结构化快照 → 9:16 短视频 storyboard JSON；
- P3 compose：storyboard → 自包含 HyperFrames 分镜 HTML；
- P4 captions/tts/pipeline：字幕、配音与一键出片流水线。
"""
from recap.storyboard import build_storyboard, build_and_save, load_recap  # noqa: F401
from recap.compose import build_composition_html, build_and_save_composition  # noqa: F401
