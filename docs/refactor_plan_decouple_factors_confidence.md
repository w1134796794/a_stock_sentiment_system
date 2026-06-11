# 分阶段改造设计：数据解耦 · 因子开关 · 置信度扣分制

> 目标读者：本项目维护者。本文是**实施蓝图**，不是已落地状态。每一阶段都可独立交付、独立回滚。
> 三条主线：①数据与业务解耦（DataPrep + 只读 Repository）；②因子可启用/禁用并接通主流程；③置信度统一为"满分扣分制"。

---

## 0. 背景与现状（改造前）

- **数据高度耦合**：`core/pattern/*`、`core/analysis/*`、`core/pipeline/layer*` 在分析过程中直接 `self.dm.xxx()` 取数。典型证据：
  - `core/pattern/pattern_recognition.py:1063` `_calculate_gap_ratio` 中途 `dm.get_stock_daily_price(code, date)`；
  - `scan_all_patterns` 扫描循环里反复 `get_limit_up_pool` / `get_stock_daily` / `get_auction_data`；
  - `core/pipeline/layer1/3/4/5` 各自 `get_index_daily` / `get_all_stocks_daily` / `get_limit_up_pool`。
- **snapshot/duckdb/sqlite 只在分析完成后写**，供 Web/问答/盘前用，**不参与当日分析的数据供给**。
- **因子开关半成品**：`config/factors/factor_registry.yaml` 每因子已有 `enabled` 字段，`FactorRegistry` 有 `enable_factor/disable_factor/get_enabled_factors`，`FactorComputer` 已 `register_compute_fn` 注册了 A1–F3，但 **`FactorComputer` 未被 pipeline 调用**；Layer1/Layer3 的因子是硬编码，改 YAML 不生效。
- **置信度无统一框架**：各策略各写各的，主流是 `base 0.6~0.7 + 阶梯加分 + min(…,0.95) 封顶`，外加 L2/L3 混合扣分。代表：`pattern_recognition._calculate_second_board_confidence`、`second_board_dragon._calculate_confidence`。

### 设计原则

1. **渐进迁移**：绝不大爆炸重写。每步小改、可回滚。
2. **结果一致性回归**：每阶段以"黄金快照"对比改造前后输出，差异需可解释。
3. **配置驱动**：行为变化尽量落到 YAML/`pattern_params`，代码只读配置。
4. **可解释**：保留打分/扣分明细、数据来源、启用因子清单，便于复盘归因。
5. **失败显式**：解耦后数据缺失要显式报错（严格模式），不允许业务层偷偷打 API。

---

## 阶段总览与依赖

```
Phase 0  护栏：黄金快照回归 + 数据需求盘点          （1~2 天，无行为变更）
   │
Phase 1  数据解耦：DataPrep + MarketDataset + Repository   （地基，收益最大）
   │
Phase 2  因子引擎接通 + 启用/禁用开关真正生效
   │
Phase 3  统一置信度扣分制（ConfidenceScorer + 规则表）
   │
Phase 4  收尾：清理死代码、文档、Web 配置面板联动
```

推进顺序理由：**数据解耦是另外两条的地基**（因子/置信度计算解耦后才能脱离 DataManager 单测与复现）；因子引擎接通后，扣分制可直接复用"因子→规则→开关"这套基础设施。

---

## Phase 0：护栏与数据需求盘点（无行为变更）

**目标**：建立"改造前后结果一致"的客观判据；把"系统到底取了哪些数据"固化成清单。

### 0.1 黄金快照回归工具

- 选 5~10 个有代表性的交易日（不同情绪周期）。
- 写脚本 `scripts/regression_snapshot.py`：对给定日期跑 `ReviewPipeline.execute`，把关键产物（`patterns`、`trade_plans`、`market.scores`、`risk_gate`）序列化为稳定 JSON（排序、定点小数），存为 `tests/golden/{date}.json`。
- 提供 `--check` 模式：重新跑并 diff，输出字段级差异。
- 这是后续每个阶段的**唯一验收依据**。

### 0.2 数据需求盘点（Data Manifest）

- 产出 `docs/data_manifest.md`：枚举每个数据获取点 → 方法、入参、时间窗、调用方 layer/strategy。
- 盘点方法：以现有 `self.dm.xxx()` 调用点为事实来源（探查已给出大部分）。
- 重点标注**回溯窗口**差异：模式识别需 15/20/60 天日线、竞价当日、资金流 5 天等。
- 顺带标注 3 个"被调用但 DataManager 未实现"的死路径（`get_chip_data`/`get_moneyflow_data`/`get_sector_daily`），改造时一并清理。

**验收**：黄金快照工具可用；data_manifest 覆盖所有 `dm.` 调用点。

---

## Phase 1：数据与业务解耦

**目标**：分析前一次性把当日所需数据备齐到"当日数据集"，业务层只读 Repository，不再中途打 API。

### 1.1 目标架构

```
┌─────────────┐   build(trade_date)   ┌──────────────────┐
│  DataPrep   │ ────────────────────▶ │  MarketDataset    │  （当日只读数据集）
│ (用 dm 批量) │                        │  - daily[code]    │
└─────────────┘                        │  - auction[code]  │
       ▲                               │  - moneyflow[code]│
       │ DataManager (仅此阶段触网)     │  - sector_map     │
                                       │  - zt_pool/...    │
                                       └────────┬─────────┘
                                                │ 注入
                                       ┌────────▼─────────┐
                                       │ StockRepository  │  只读门面，缺失即报错
                                       └────────┬─────────┘
                                                │ 依赖
                          ┌─────────────────────┼─────────────────────┐
                          ▼                     ▼                     ▼
                   pattern/* (策略)      analysis/* (大盘/情绪/板块)   factors/*
```

### 1.2 新增模块

- `core/data/market_dataset.py`
  - `@dataclass MarketDataset`：按 `trade_date` 持有预取帧。建议结构：
    - `daily: Dict[str, pd.DataFrame]`（code → 最近 N 日日线，N=最大窗口如 60）
    - `auction: Dict[str, dict]`、`tick: Dict[str, pd.DataFrame]`（按需）
    - `moneyflow: Dict[str, pd.DataFrame]`、`sector_map: Dict[str, str]`
    - `zt_pool / prev_zt_pool / limit_down / index_daily / all_daily`
    - `meta: {built_at, universe_size, windows}`
  - 支持 `to_parquet(dir)/from_parquet(dir)`：可落盘 `data/dataset/{date}/`，实现**离线可复现**与二次运行秒级加载。

- `core/data/data_prep.py`
  - `class DataPrep: def __init__(self, dm); def build(self, trade_date, prev_date, *, windows: dict, strict: bool) -> MarketDataset`
  - 流程：①确定 **universe**（今日涨停 ∪ 昨日/前日/近15日涨停 ∪ 龙头池 ∪ 候选）；②用**批量接口**拉数据（`get_stocks_daily_batch`、`get_moneyflow_summary`、`get_stock_sectors_batch`、`get_index_daily`、`get_all_stocks_daily`、竞价按需 `get_auction_data`）；③组装 `MarketDataset`。
  - 实时/盘中数据（竞价、分时）单独标注：盘后复盘可预取，盘前 `auction_confirm` 场景例外（见 1.5）。

- `core/data/repository.py`
  - `class StockRepository`：**只读门面**，方法名尽量与现 `DataManager` 对齐，便于平滑替换：
    - `get_daily(code, *, window=None, end=None) -> pd.DataFrame`
    - `get_daily_price(code, date) -> dict`
    - `get_auction(code, date) -> dict`
    - `get_moneyflow(code, ...) -> pd.DataFrame`
    - `sector_of(code) -> str`
    - `zt_pool / prev_zt_pool / index_daily(...)` 等
  - **严格模式**：数据集缺该 code/窗口 → `raise DataNotPrefetchedError`（而非偷偷 `dm`）。`strict=False` 时可回退 `dm` 并打 WARNING（迁移期用）。

### 1.3 接入点（最小侵入）

- `core/pipeline/review_pipeline.py`
  - `ReviewPipeline.__init__`：构造 `self.data_prep = DataPrep(self.dm)`。
  - `execute()` 在 `_fetch_base_data(ctx)` 之后、Layer1 之前，新增一步 `ctx.dataset = self.data_prep.build(...)`、`ctx.repo = StockRepository(ctx.dataset)`。
  - `SharedContext` 增加字段：`dataset: Optional[MarketDataset]`、`repo: Optional[StockRepository]`。
- 各 Layer/strategy 构造函数从 `(data_manager)` 改为同时可接收 `repo`；迁移期二者并存。

### 1.4 迁移顺序（逐模块替换 `self.dm.` → `self.repo.`）

1. `core/pattern/pattern_recognition.py`（耦合最重，先做、收益最大）——例如 `_calculate_gap_ratio` 改读 `repo.get_daily_price`。
2. `core/pattern/{weak_to_strong, first_board_breakout, dragon_second_wave}.py`
3. `core/analysis/*`（板块、情绪、资金流、筹码）
4. `core/factors/*`（与 Phase 2 协同）
5. `core/pipeline/layer*`（多数已有 ctx 雏形，收尾）

每替换一个模块：跑黄金快照回归，差异为 0 或可解释方可合并。

### 1.5 边界与风险

| 风险 | 对策 |
|------|------|
| 实时数据无法本地化（竞价/分时/盘中快照） | 数据集区分"盘后可复现"与"盘中实时"两类；`auction_confirm.py` 盘前场景保留直连 `dm` |
| universe 圈不全导致策略缺数据 | 严格模式先在测试日跑，把缺失 code 反馈进 universe 规则；迁移期 `strict=False` 兜底 |
| 首次预取耗时 | 全部走批量接口替代循环单股；落 parquet 后二次运行直接加载 |
| 窗口不一致（15/20/60） | 统一按**最大窗口**预取，Repository 按需切片 |

**验收**：模式识别层全部走 `repo`；关闭 `dm` 直连（strict=True）仍能跑通目标测试日；黄金快照一致。

---

## Phase 2：因子引擎接通 + 启用/禁用开关

**目标**：把已存在但未接线的 `FactorRegistry` + `FactorComputer` 接到主流程，使 YAML 的 `enabled` 与 `enabled_factors` **真正生效**；支持按情绪周期切换因子 profile。

### 2.1 现状可复用资产

- `FactorRegistry`（`core/factors/factor_registry.py`）：`get_enabled_factors(layer, sub_category)`、`get_factor_weight(...)`、`get_composite_weights(layer)`、`enable_factor/disable_factor`、`FactorDefinition.enabled`。
- `FactorComputer`（`core/factors/factor_computer.py`）：`register_compute_fn(factor_id, fn)`、`compute_layer(...)`、已注册 A1–F3 的 `_calc_*`。
- 缺口：Layer1 核心因子（如 `multi_index_trend`、`total_amount`）**无对应 `_calc_*`**；Layer3 的 D1–D5/E1–E4 在 `layer3_stock_selection.py` 内**硬编码**，不查 registry。

### 2.2 改造点

1. **Layer3 因子收敛到 registry**：把 `_compute_stock_tech_factors`/`_compute_moneyflow_factors` 里的 D/E 硬编码，改为：取 `registry.get_enabled_factors('layer3', sub)` → 调对应 `compute_fn` → 聚合时用 `get_factor_weight`。被禁用的因子自动跳过。
2. **补齐缺失 `_calc_*`**：为 Layer1 主因子在 `FactorComputer` 注册计算函数（或在 registry 标注"由 layer 内部计算"，避免"无计算函数静默跳过"误导）。
3. **权重重新归一化（关键）**：禁用部分因子后，剩余因子权重必须**重新归一化**到和为 1，否则综合分被系统性拉低。封装 `normalize_weights(enabled_ids, raw_weights)` 统一处理。
4. **情绪周期 profile（呼应"不同阶段不同因子"）**：在 `config/factors/` 增加 `profiles:`，每个情绪周期一套 `enabled_factors` 覆盖；`FactorRegistry` 增 `apply_profile(cycle_name)`，在 Layer1 判定周期后调用。
5. **运行留痕**：把"本次启用的因子清单 + profile"写入 `SharedContext` 与 snapshot.meta，便于复盘归因。

### 2.3 风险

- 改 Layer3 计算路径直接影响选股结果 → 必须黄金快照回归，且默认 profile 的 `enabled_factors` 要与当前硬编码因子集**完全一致**（先做到"行为不变"，再谈调优）。
- registry 是单例（`__new__` 缓存）→ profile 切换要注意进程内状态污染，回测多日循环时每日 `reload()` 或显式 `apply_profile`。

**验收**：删/禁一个因子，Layer3 输出按预期变化且权重重新归一化；默认 profile 下黄金快照与 Phase 1 末一致。

---

## Phase 3：统一置信度扣分制

**目标**：把"基础分+加分"翻转为"满分起扣"，统一到一个数据驱动的 `ConfidenceScorer`，各策略只提供规则集；保留扣分明细。

### 3.1 规则模型（声明式，落 YAML/`pattern_params`）

每条规则 = `因子 + 分段阈值表 + 启用开关`：

```yaml
# 例：二板定龙置信度规则（写入 config/confidence_rules.yaml 或 pattern_params）
second_board_dragon_confidence:
  ceiling: 95            # 天花板<100，承认不确定性（见 3.3 坑①）
  floor: 40              # 地板，防扣穿失去区分度
  rules:
    - factor: seal_ratio        # 封单强度
      enabled: true
      # 分段：阈值降序，命中第一个满足的区间取其 penalty
      bands: [[0.05, 0], [0.03, 3], [0.02, 5], [0.01, 10], [0.0, 15]]
    - factor: gap_ratio         # 次日高开
      enabled: true
      bands: [[0.05, 0], [0.03, 4], [0.02, 8], [0.0, 15]]
    - factor: first_board_score
      enabled: true
      bands: [[80, 0], [70, 5], [60, 10], [0, 20]]
```

> 分段表用**降序阈值**表达"≥某值扣多少"，天然连续无空洞（解决坑②）。`enabled:false` = 该因子不扣分（与 Phase 2 因子开关同源）。

### 3.2 新增模块

- `core/scoring/confidence_scorer.py`
  - `class ConfidenceScorer: def __init__(self, ruleset: dict)`
  - `def score(self, factors: dict) -> ConfidenceResult`
    - 从 `ceiling` 起，对每条 `enabled` 规则按 `bands` 命中扣分；`max(floor, ceiling - Σpenalty)`。
    - 返回 `ConfidenceResult{ value: float(0~1), raw: int, breakdown: List[{factor, value, penalty}] }`。
  - `breakdown` 即"扣分明细"，写入信号 `key_metrics` 与 snapshot，供调参/复盘（解决"看不到为什么不是满分"）。

### 3.3 必须堵的坑

1. **满分虚高**：纯扣分制下没踩任何线就 100 → 过度自信。对策：`ceiling < 100`（如 95），或对"未验证项"默认轻扣。
2. **区间空洞**：分段阈值必须连续覆盖到 0（如上 `[…, [0.0, 15]]`）。
3. **叠加塌陷**：多因子同时扣可能扣穿。对策：`floor` 地板 + 关键缺陷可设"硬过滤"（沿用现有"累计扣分≥阈值直接淘汰"）。
4. **跨策略可比性**：各策略扣分项数量不同，满分都是 95 但内涵不同。下游 `MultiFactorScorer` 把 `confidence` 当"模式质量"时口径要统一（建议都归一化到 0~1 再加权）。

### 3.4 迁移顺序

- 先在**已重构过的**二板定龙上落地（Phase 1 刚动过、有回归基线），用 `ConfidenceScorer` 替换 `_calculate_second_board_confidence` + L2/L3 penalty。
- 再推广到弱转强 / 首板突破 / 龙二波，逐个用规则集替换内联 if/elif。
- 旧函数保留一版做 A/B 对比，确认分布合理后再删。

**验收**：扣分明细可见；同一标的新旧置信度差异可解释；策略间 `confidence` 口径统一（0~1）。

---

## 横切关注点

### 配置与 Web 面板

- 新增配置：`config/confidence_rules.yaml`、`config/factors/profiles`。沿用现有 `webdata/config_overrides.json` 深合并机制（`config/overrides.py`），让 Web 可调。
- Web 配置页（`/config`）后续增加：因子开关勾选、profile 选择、扣分规则编辑（Phase 4）。

### 测试策略

- 单元测试：`DataPrep`/`Repository`（用 mock dataset，无需触网）、`ConfidenceScorer`（规则表 → 期望分）、`normalize_weights`。
- 集成测试：黄金快照回归（贯穿每个 Phase）。
- 解耦后策略可**脱离 DataManager** 单测——这是本次改造的重要副产品。

### 死代码清理（随手做）

- `get_chip_data`/`get_moneyflow_data`/`get_sector_daily` 等 `hasattr` 探测的不存在方法，迁移到 Repository 时统一指向真实方法（`get_cyq_perf`/`get_moneyflow`/`get_ths_daily`）。
- `core/pattern/second_board_dragon.py` 的 `SecondBoardDragonStrategy`（有完整定龙逻辑但生产未调用）：决定"接通"或"删除"，避免两套并存。

---

## 风险登记表（总）

| # | 风险 | 等级 | 缓解 |
|---|------|------|------|
| R1 | 大改导致选股结果漂移且无法判断对错 | 高 | Phase 0 黄金快照贯穿全程，差异必须可解释 |
| R2 | 实时数据无法本地化 | 中 | 数据集区分盘后/盘中两类，盘前确认保留直连 |
| R3 | 因子权重禁用后未归一化致分数塌陷 | 中 | `normalize_weights` 统一处理 + 单测 |
| R4 | 扣分制满分虚高/区间空洞 | 中 | ceiling<100、分段覆盖到 0、floor 地板 |
| R5 | FactorRegistry 单例状态污染（回测多日） | 中 | 每日 `apply_profile`/`reload` |
| R6 | 迁移期 dm 与 repo 双路径行为不一致 | 中 | strict 开关 + 单模块逐个切换回归 |

---

## 里程碑 / 交付物清单

- **Phase 0**：`scripts/regression_snapshot.py`、`tests/golden/*.json`、`docs/data_manifest.md`
- **Phase 1**：`core/data/{market_dataset,data_prep,repository}.py`、`SharedContext` 扩展、模式识别层切 repo
- **Phase 2**：Layer3 因子走 registry、Layer1 `_calc_*` 补齐、`normalize_weights`、情绪 profile
- **Phase 3**：`core/scoring/confidence_scorer.py`、`config/confidence_rules.yaml`、四策略迁移
- **Phase 4**：Web 配置面板联动、死代码清理、文档更新

> 每个 Phase 合并前的硬性门槛：**目标测试日黄金快照回归通过（差异=0 或书面可解释）**。