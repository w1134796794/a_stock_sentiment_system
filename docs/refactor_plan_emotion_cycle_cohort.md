# 分阶段改造设计：情绪周期 · 大/小/中军分群 · 循环相位模型

> 目标读者：本项目维护者。本文是**实施蓝图**，不是已落地状态。每一阶段都可独立交付、独立回滚。
> 三条主线：①把"全市场一锅烩"的情绪判定拆为**大票/小票/中军**分群；②把静态四标签（冰点/退潮/震荡/高潮）换成**有方向的循环相位 + 动量**；③修掉评分悬空、口径错位等设计债，并收敛到单一真源配置。
> 面向人群：**超短/打板选手**——输出要直接回答"周期走到哪了、谁在领涨、能不能上车/该不该跑"。

---

## 0. 背景与现状（改造前）

主引擎 `core/analysis/emotion_cycle_engine.py`，相位层 `core/analysis/emotion_phase.py`，流水线入口 `core/pipeline/layer3_stock_selection.py::_analyze_emotion_cycle`。

实测 5+1 个阶段：`高潮期 / 上升期 / 震荡期 / 退潮期 / 冰点期`，外加下游残留但引擎从不产出的 `回暖期`。

### 四个结构性硬伤

1. **`震荡期` 是兜底垃圾桶**：`_determine_cycle`（engine.py:487）里，只有 boom/freeze/decline 需要"最高分且 ≥5"，rise 需"最高分"，其余一律落到 `震荡期`（rise/shake 同分、极端阶段不达 5 分都掉进去）。"震荡"因此不是真实情绪相位，而是"分类器不确定"。

2. **权重悬空**：`scoring_weights` 在 `config/emotion_cycle_config.yaml` 配了、`_load_scoring_weights()` 也加载了，但 `_calculate_cycle_scores()` 用的是**硬编码整数加分**，权重从未读取。调参无效，10 个维度等权乱加。

3. **核按钮口径错**：`nuclear_button_count` 实际取 `len(limit_down_df)`（跌停池行数），又和 `limit_down_count` 重复入模 → 同一信号双重计票，且与业务语义"核按钮"不符。

4. **无任何分群**：涨停/跌停/炸板/连板/溢价一律全市场聚合（`get_limit_up_pool/get_limit_down_pool`）。A 股超短情绪经常结构性分裂（小票题材高潮 + 权重大票冰点；主线退潮 + 中军补涨），全市场平均把分裂信号抹平，判定自然失真。

### 附带债（一并修）

- 已采集未入模的结构因子：`first_board_ratio / one_word_ratio / tail_board_ratio / avg_seal_ratio`（engine.py 590–640 只写进 `metrics`，未进 `detect_cycle`）。
- 相位 gap 阈值 8/15、`win_rate`/`avg_profit` 阈值硬编码，未外置。
- ML 融合（`emotion_cycle_integrated.py`，rule 0.6 / ml 0.4）结果只写 `integrated_analysis`，权威 `cycle_name` 仍取规则引擎。
- `continuous_rate` 是"连板占比"（存量），不是"晋级率"（昨 N 板今晋 N+1），缺真正的领先指标。

### 设计原则

1. **渐进迁移**：绝不大爆炸重写。新模型先与旧引擎并行旁路，权威 `cycle_name` 最后切换。
2. **向后兼容**：下游大量硬编码中文周期名（`risk/circuit_breaker.py::freeze_cycles`、`config/factors/profiles.yaml` 四套 profile、`pattern_params::multi_factor_emotion_fit`、`layer4` 仓位、`recap/*` 配色）。改造保留 `legacy_cycle_name` 新→旧映射，下游不动，逐步迁移。
3. **结果一致性回归**：每阶段用黄金快照（`scripts/regression_pattern_snapshot.py`）做 A/B，差异需可解释。
4. **配置单一真源**：阈值/权重/分群口径全部落 YAML，代码只读。
5. **可解释**：保留每个 cohort 的子分明细、相位判据、分歧说明，便于复盘归因。

---

## 1. 目标模型：二维结构

### 维度 A —— 周期相位（循环 + 动量）

用**有序循环相位**替换零散标签，每相位绑定明确短线动作：

| 新相位 | 核心触发特征 | 短线动作 |
|---|---|---|
| **冰点** | 涨停地量、一致性跌停/核按钮、连板高度坍塌 | 空仓 / 只做低位反包试错 |
| **修复（启动）** | 首板·低位板成功率回升、昨涨停今转红、亏钱效应收敛 | 低吸 + 打首板，轻仓试温 |
| **发酵（主升）** | 晋级率抬升、高度阶梯健康、主线明确 | 打板接力、卡主线中军 |
| **高潮（分歧）** | 高度见顶/加速、涨停峰值但炸板与分歧同步放大 | 减龙头、低位补涨、不追高 |
| **退潮** | 高标 A 杀/核按钮、打板红利转负、晋级率断崖 | 空仓或反核，等冰点反包 |

- **去掉"震荡期"作为相位**；"无主线/纯轮动"改为独立辅助轴 `trunk_clarity`（主线明确度 0–1，低值时提示"轮动市、降打板权重"）。
- 额外输出 `momentum`（升温/见顶/降温），由"晋级率 + 打板溢价"的环比一阶导决定——这是短线最值钱的信号。

### 维度 B —— 大票 / 小票 / 中军分群

每个 cohort 独立算子情绪分，再合成全局 + 暴露分歧。

**分群口径（默认，可配；全部可用现有涨停池字段实现，无需新数据源）**——涨停池 normalize 后已有 `流通市值/总市值/连板数`：

| Cohort | 默认口径 | 短线含义 |
|---|---|---|
| **小票** | 流通市值 < 50 亿 | 游资题材主战场，最敏感、领先 |
| **中军** | 流通市值 50–200 亿 且连板≥2 | 板块厚度/资金承接力，**退潮中段预警源** |
| **大票** | 流通市值 > 200 亿 / 指数权重 / 赛道龙头 | 机构抱团情绪，量价+宽度驱动 |

每群各算：涨停数、晋级率、炸板率、昨涨停今溢价、最高板、核按钮。

**合成规则**：
- 全局相位以**小票 cohort 为锚**（超短情绪以小票为主），用 `cohort 分歧度` 修正：
  - 小票高潮 + 大票冰点 → "纯题材市，退潮快、控容错"。
  - 主线退潮 + 中军补涨 → "退潮中段，注意高低切后的二波/反包"。

---

## 2. 输入因子重构（短线口径）

把"结果型"降权，"领先型"升权：

| 优先级 | 因子 | 现状 | 改造 |
|---|---|---|---|
| 领先 | **连板晋级率**（昨 N 板今晋 N+1 占比） | 缺，仅 `continuous_rate`（存量占比） | 新增，作发酵/退潮主信号 |
| 领先 | **赚钱/亏钱效应**（昨涨停今开盘+收盘溢价分布） | 仅 `prev_limit_up_premium` 单值 | 拆为收红占比 + 溢价中位数，**分 cohort** |
| 同步 | 炸板率 | 已涨停池内 `open_times>0` | 统一口径（建议"触板/(涨停+触板)"），至少文档明确 |
| 同步 | 涨停/跌停/核按钮 | 核按钮=跌停数（错+重复） | 核按钮独立定义（跌停无量一字 / 炸板核），与跌停数解耦 |
| 结果 | 最高板高度 | 权重过高 | 降权，仅作"高度顶"参考 |

接入已采集未入模的结构因子（首板比、一字板比、尾盘板比、封单比）作为质量修正项。

---

## 3. 输出 Schema（新）+ 兼容层

```jsonc
{
  "phase": "发酵",              // 新相位
  "momentum": "升温",           // 升温/见顶/降温
  "trunk_clarity": 0.72,        // 主线明确度 0-1（替代"震荡"语义）
  "cohorts": {
    "small": {"phase":"高潮","score":78,"promote_rate":0.55,"premium":3.1},
    "mid":   {"phase":"发酵","score":61},
    "large": {"phase":"冰点","score":22}
  },
  "divergence": "小票高潮·大票冰点 → 纯题材市，控容错",
  "legacy_cycle_name": "上升期" // 映射回旧 5 名，保证下游不炸
}
```

**新→旧映射（初版建议）**：冰点→冰点期；修复→上升期（或新增"回暖期"接线）；发酵→上升期；高潮→高潮期；退潮→退潮期；`trunk_clarity` 低且无明确相位 → 震荡期。映射表落 YAML，便于灰度调整。

---

## 4. 阶段总览与依赖

```
Phase 0  护栏：黄金快照基线 + 新指标口径评审            （无行为变更）
   │
Phase 1  分群子指标 + 真·晋级率 + 赚钱效应，写入 metrics   （纯展示，零 diff 判定）
   │
Phase 2  新相位/动量/主线明确度引擎，与旧引擎并行旁路 + legacy 映射
   │
Phase 3  修债：权重接线 / 核按钮·炸板率口径 / 阈值外置 / 单一真源配置
   │
Phase 4  切换权威 cycle_name 到新引擎；下游 profile/风控/仲裁逐模块迁移
```

| Phase | 内容 | 风险 | 改变现有判定 | 回归 |
|---|---|---|---|---|
| P0 | 基线快照 + 口径评审 | 无 | 否 | — |
| P1 | cohort 分群指标 + 晋级率 + 赚钱效应（仅 `metrics`） | 零 | 否 | 快照新增字段 |
> **P1 落地状态（已实现）**：`emotion_cycle_engine.py` 新增 `_calculate_cohort_metrics`（按 `float_mv` 分 小/中军/大）、`_calculate_promotion_rate`（昨日 T-1 vs 今日 T 的真·晋级率），写入 `metrics.cohorts` / `metrics.promotion`；`layer3` 透传 T-1 涨停池；`desktop/status.py` + `web/templates/overview.html` 增加分群与晋级率展示面板。赚钱效应沿用现有 `win_rate/avg_profit`（T+1 溢价），分 cohort 的赚钱效应留待后续（需逐股取价）。**判定逻辑零变更**，数据在下次收盘分析后出现在快照与概览页。
| P2 | 新引擎并行输出 + `legacy_cycle_name` 映射 | 低 | 否（旧 `cycle_name` 不变） | A/B 旁路对比 |
> **P2 落地状态（已实现）**：新增 `core/analysis/emotion_phase_model.py`（纯函数 `compute_phase_model`，五相位循环评分 + `momentum` 粗略代理 + `trunk_clarity` 主线明确度 + `legacy_cycle_name` 新→旧映射）。引擎在 `analyze_market_data` 末尾以**旁路**方式产出 `phase_model`（权威 `cycle_name` 不变）；`snapshot/writer.py` 落盘 `market.phase_model`；`desktop/status.py` + `overview.html` 在情绪周期卡内对照展示"循环相位(实验) + 动量 + 主线明确度 + 与旧判定分歧"。单测 `tests/test_emotion_phase_model.py`（6 例）。实测 20260612：新模型=退潮/降温/legacy 退潮期，与旧引擎一致。`momentum` 真·环比、阈值外置、并入权威源留待 P3/P4。
| P3 | 权重接线 + 口径修正 + 阈值/权重外置 | 中 | 是（旧引擎判定会变） | 快照回归，差异需可解释 |
> **P3 落地状态（已实现，含范围调整）**：
> - **范围决策**：原 P3 含"重写旧引擎悬空权重 + 核按钮口径"，但 P4 将把权威判定切到新相位模型、旧引擎评分终将退役，且新模型并不依赖那几个有问题的入参（用 `limit_down_ratio`/`win_rate`，不碰 `nuclear_button_count`）。故**旧引擎内部重写推迟到 P4 一并决策**，避免白做。**P3 实际未改变旧引擎权威判定 → 零回归风险。**
> - **阈值/分群口径外置单一真源**：`emotion_cycle_config.yaml` 新增 `phase_model`（`cohort` 市值分档(亿) + `thresholds` 相位阈值 + `momentum` 环比参数）；`emotion_phase_model.py` 与引擎 `_load_cohort_cutoffs` 运行时读 YAML、失败回退默认。
> - **真·环比动量**：`compute_phase_model(metrics, prev_metrics)` 用昨日快照 metrics 算晋级率/赚钱效应/高度一阶导 → 升温/见顶/降温；`layer3` 经 `SnapshotReader.load(prev_trade_date)` 注入昨日 metrics。
> - 单测扩到 9 例（新增 环比升温/降温、配置加载）；lint 干净。
| P4 | 权威源切换 + 下游灰度迁移（profile/freeze_cycles/仲裁） | 中高 | 是 | 分模块回归 |
> **P4 落地状态（已实现为开关门控，默认关闭=零行为变更）**：
> - 新增 YAML 开关 `phase_model.authoritative`（默认 `false`）。引擎 `_is_phase_model_authoritative()` 读取；为 `true` 时在 `analyze_market_data` 末尾把权威 `cycle_name` 切到新模型 `legacy_cycle_name`，并以统一中文周期名重取 `strategy`——**下游 profile/熔断(`freeze_cycles`)/仲裁/仓位经由 cycle_name 自动跟随，无需逐个改下游**。旧引擎判定保留在 `cycle_name_rule_engine` 作对照。
> - `snapshot/writer.py` 落盘 `cycle_name_rule_engine` / `authoritative_source`；`overview.html` 在切换后显示「新模型」徽标 + 旧引擎对照名。
> - 单测扩到 11 例（开关 off 保持旧引擎 / 开关 on 切到新模型）。实测默认配置：`authoritative_source=rule_engine`、`cycle_name=退潮期` 不变。
> - **启用方式**：把 `phase_model.authoritative` 改为 `true` 并重跑收盘分析；可先观察几日 overview 的"新相位 vs 旧周期"分歧后再切。回滚=改回 `false`。
> - 待办：`回暖期` 是否启用以区分"修复 vs 发酵"、新模型自有的 phase/transition 展示是否替换旧 `emotion_phase`，留作 P4+ 微调。

---

## 5. 待决策口径（需维护者拍板）

1. **分群口径**：纯流通市值分档（推荐，最易落地）／市值 + 中军叠加主线板块卡位（复用 `sector_position.CORE_LEADER`，更贴超短但复杂）／按连板高度身位分群。
2. **相位集**：五相位循环（冰点→修复→发酵→高潮→退潮，推荐，去震荡）／保留震荡为"无主线轮动"独立相位／更细（再拆"退潮反包/二波"）。
3. **新→旧映射**：是否启用残留的 `回暖期` 接线，还是只用现有 5 名。
4. **权威源**：P4 是否把 ML 融合（rule/ml 0.6/0.4）也并入新引擎的权威判定。

---

## 6. 改动面清单（便于评估工作量）

- 新增：`core/analysis/emotion_cohort.py`（分群指标）、新相位引擎或扩展 `emotion_cycle_engine.py`。
- 修改：`emotion_phase.py`（动量/主线明确度/gap 外置）、`layer3_stock_selection.py`（接线）、`config/emotion_cycle_config.yaml` + `config/factors/emotion_cycle.yaml`（口径/权重/分群/映射单一真源）、`snapshot/writer.py` + `desktop/status.py` + `web/templates/overview.html`（展示新结构）。
- 兼容不动（P4 前）：`risk/circuit_breaker.py`、`config/factors/profiles.yaml`、`pattern_params::multi_factor_emotion_fit`、`recap/*`、`signal_arbitrator` 情绪路由/闸门。
