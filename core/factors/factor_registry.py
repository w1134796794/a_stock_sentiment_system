"""
因子注册中心

功能：
1. 从 factor_registry.yaml 加载所有因子定义
2. 从各 Layer 配置文件加载因子启用状态和权重
3. 提供因子查询、过滤、权重获取等接口
4. 支持运行时动态调整因子配置
"""
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import loguru

logger = loguru.logger


class FactorCategory(Enum):
    MARKET_ENV = "market_env"
    EMOTION = "emotion"
    SECTOR = "sector"
    STOCK_TECH = "stock_tech"
    MONEYFLOW = "moneyflow"
    CROSS_CYCLE = "cross_cycle"


@dataclass
class FactorDefinition:
    """因子定义"""
    factor_id: str
    name: str
    category: FactorCategory
    sub_category: str
    description: str
    data_source: str
    output_type: str
    value_range: List[float] = field(default_factory=lambda: [0.0, 1.0])
    default_weight: float = 0.0
    enabled: bool = True
    params: Dict = field(default_factory=dict)


class FactorRegistry:
    """
    因子注册中心

    单例模式，全局唯一实例。
    从YAML配置文件加载所有因子定义和各Layer的启用/权重配置。
    """

    _instance = None

    def __new__(cls, config_dir: Path = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_dir: Path = None):
        if hasattr(self, '_initialized') and self._initialized:
            return

        if config_dir is None:
            config_dir = Path(__file__).parent.parent.parent / "config"

        self.config_dir = Path(config_dir)
        self.factors_dir = self.config_dir / "factors"
        self._factors: Dict[str, FactorDefinition] = {}
        self._layer_configs: Dict[str, Dict] = {}
        # Phase 2：情绪周期 profile（按周期切换因子启用集）
        self._profiles: Dict[str, Dict] = {}
        self._base_enabled: Dict[str, bool] = {}
        self._active_profile: Optional[str] = None
        self._initialized = True

        self._load_registry()
        self._load_layer_configs()
        self._load_profiles()
        self._snapshot_base_enabled()
        logger.info(f"[FactorRegistry] 初始化完成，加载 {len(self._factors)} 个因子定义")

    def _load_config_dict(self, config_name: str, fallback_path: Path) -> Dict:
        """
        读取某 YAML 配置：优先走 config_loader（已套用 webdata 的网页覆盖），
        失败/为空时回退直接读原始文件。

        这样网页对 factor_registry.yaml / layerX 配置的"因子开关/权重"覆盖，
        能即时流入本注册中心（无覆盖时与直接读文件逐字等价，行为不变）。
        """
        try:
            from config.config_loader import get_config_loader
            cfg = get_config_loader().get_config(config_name)
            if cfg:
                return cfg
        except Exception as e:  # pragma: no cover - 退回原始文件
            logger.debug(f"[FactorRegistry] 经 config_loader 读取 {config_name} 失败，回退原始文件: {e}")
        if fallback_path.exists():
            try:
                with open(fallback_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                logger.error(f"[FactorRegistry] 读取 {fallback_path} 失败: {e}")
        return {}

    def _load_registry(self):
        """加载因子注册表（经 config_loader，含网页覆盖）"""
        registry_path = self.factors_dir / "factor_registry.yaml"
        data = self._load_config_dict('factor_registry', registry_path)
        if not data:
            logger.warning(f"[FactorRegistry] 因子注册表为空或不存在: {registry_path}")
            return

        try:
            for factor_id, cfg in data.get('factors', {}).items():
                category_str = cfg.get('category', 'market_env')
                try:
                    category = FactorCategory(category_str)
                except ValueError:
                    logger.warning(f"[FactorRegistry] 未知因子类别: {category_str}，跳过 {factor_id}")
                    continue

                self._factors[factor_id] = FactorDefinition(
                    factor_id=factor_id,
                    name=cfg.get('name', factor_id),
                    category=category,
                    sub_category=cfg.get('sub_category', ''),
                    description=cfg.get('description', ''),
                    data_source=cfg.get('data_source', ''),
                    output_type=cfg.get('output_type', 'float'),
                    value_range=cfg.get('value_range', [0.0, 1.0]),
                    default_weight=cfg.get('default_weight', 0.0),
                    enabled=cfg.get('enabled', True),
                    params=cfg.get('params', {}),
                )

            logger.info(f"[FactorRegistry] 从注册表加载 {len(self._factors)} 个因子")
        except Exception as e:
            logger.error(f"[FactorRegistry] 加载因子注册表失败: {e}")

    def _load_layer_configs(self):
        """加载各Layer的因子启用+权重配置（经 config_loader，含网页覆盖）"""
        # layer 内部名 -> (config_loader 配置名, 原始文件名)
        layer_files = {
            'layer1': ('layer1_market_env', 'layer1_market_env.yaml'),
            'emotion': ('emotion_cycle_factors', 'emotion_cycle.yaml'),
            'layer2': ('layer2_sector', 'layer2_sector.yaml'),
            'layer3': ('layer3_stock_select', 'layer3_stock_select.yaml'),
            'layer4': ('layer4_trade_plan', 'layer4_trade_plan.yaml'),
        }

        for layer_name, (cfg_name, filename) in layer_files.items():
            cfg = self._load_config_dict(cfg_name, self.factors_dir / filename)
            if cfg:
                self._layer_configs[layer_name] = cfg
                logger.info(f"[FactorRegistry] 加载Layer配置: {filename}")
            else:
                logger.debug(f"[FactorRegistry] Layer配置为空/不存在: {filename}，使用默认值")

    def get_factor(self, factor_id: str) -> Optional[FactorDefinition]:
        """获取单个因子定义"""
        return self._factors.get(factor_id)

    def get_enabled_factors(self, layer: str, sub_category: str = None) -> List[str]:
        """
        获取某Layer某子类下所有启用的因子ID

        Args:
            layer: Layer名称 (layer1/emotion/layer2/layer3/layer4)
            sub_category: 子类名称 (trend/volume/width/continuity/core/structure等)

        Returns:
            启用的因子ID列表
        """
        layer_cfg = self._layer_configs.get(layer, {})
        if not layer_cfg:
            return self._get_default_enabled_factors(layer, sub_category)

        layer_key = self._get_layer_key(layer)
        sub_cfg = layer_cfg.get(layer_key, {}).get(sub_category, {}) if sub_category else layer_cfg.get(layer_key, {})
        enabled = sub_cfg.get('enabled_factors', [])

        if not enabled:
            return self._get_default_enabled_factors(layer, sub_category)

        return enabled

    def get_factor_weight(self, layer: str, sub_category: str, factor_id: str) -> float:
        """
        获取某因子在某Layer某子类下的权重

        Args:
            layer: Layer名称
            sub_category: 子类名称
            factor_id: 因子ID

        Returns:
            权重值 (0.0 ~ 1.0)
        """
        layer_cfg = self._layer_configs.get(layer, {})
        if not layer_cfg:
            factor = self._factors.get(factor_id)
            return factor.default_weight if factor else 0.0

        layer_key = self._get_layer_key(layer)
        weights = layer_cfg.get(layer_key, {}).get(sub_category, {}).get('factor_weights', {})
        weight = weights.get(factor_id)

        if weight is not None:
            return float(weight)

        factor = self._factors.get(factor_id)
        return factor.default_weight if factor else 0.0

    def get_composite_weights(self, layer: str) -> Dict[str, float]:
        """
        获取某Layer的子类综合权重

        Args:
            layer: Layer名称

        Returns:
            {sub_category: weight}
        """
        layer_cfg = self._layer_configs.get(layer, {})
        layer_key = self._get_layer_key(layer)
        return layer_cfg.get(layer_key, {}).get('composite_weights', {})

    def list_factors(self, category: FactorCategory = None,
                     sub_category: str = None,
                     enabled_only: bool = True) -> List[FactorDefinition]:
        """
        列出因子

        Args:
            category: 按类别过滤
            sub_category: 按子类过滤
            enabled_only: 是否只返回启用的因子

        Returns:
            因子定义列表
        """
        result = list(self._factors.values())
        if category:
            result = [f for f in result if f.category == category]
        if sub_category:
            result = [f for f in result if f.sub_category == sub_category]
        if enabled_only:
            result = [f for f in result if f.enabled]
        return result

    def get_all_categories(self) -> List[FactorCategory]:
        """获取所有因子类别"""
        return list(FactorCategory)

    def get_sub_categories(self, category: str) -> List[str]:
        """获取某类别下的所有子类"""
        sub_cats = set()
        for factor in self._factors.values():
            if factor.category.value == category:
                sub_cats.add(factor.sub_category)
        return sorted(sub_cats)

    def enable_factor(self, factor_id: str):
        """启用因子"""
        if factor_id in self._factors:
            self._factors[factor_id].enabled = True
            logger.info(f"[FactorRegistry] 启用因子: {factor_id}")

    def disable_factor(self, factor_id: str):
        """禁用因子"""
        if factor_id in self._factors:
            self._factors[factor_id].enabled = False
            logger.info(f"[FactorRegistry] 禁用因子: {factor_id}")

    def update_weight(self, layer: str, sub_category: str, factor_id: str, weight: float):
        """运行时更新因子权重"""
        layer_cfg = self._layer_configs.setdefault(layer, {})
        layer_key = self._get_layer_key(layer)
        sub_cfg = layer_cfg.setdefault(layer_key, {}).setdefault(sub_category, {})
        weights = sub_cfg.setdefault('factor_weights', {})
        weights[factor_id] = weight
        logger.info(f"[FactorRegistry] 更新权重: {layer}/{sub_category}/{factor_id} = {weight}")

    def reload(self):
        """重新加载所有配置"""
        self._factors.clear()
        self._layer_configs.clear()
        self._profiles.clear()
        self._active_profile = None
        self._load_registry()
        self._load_layer_configs()
        self._load_profiles()
        self._snapshot_base_enabled()
        logger.info("[FactorRegistry] 配置已重新加载")

    # ============================================================
    # Phase 2：情绪周期 profile
    # ============================================================

    def _load_profiles(self):
        """加载情绪周期 profile 配置（config/factors/profiles.yaml）。"""
        path = self.factors_dir / "profiles.yaml"
        if not path.exists():
            logger.debug("[FactorRegistry] profiles.yaml 不存在，profile 机制空载（行为不变）")
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
            self._profiles = data.get('profiles', {}) or {}
            logger.info(f"[FactorRegistry] 加载 {len(self._profiles)} 个情绪周期 profile")
        except Exception as e:
            logger.warning(f"[FactorRegistry] 加载 profiles 失败: {e}")

    def _snapshot_base_enabled(self):
        """快照各因子的基础启用态（来自注册表 YAML），供 profile 复位使用。"""
        self._base_enabled = {fid: f.enabled for fid, f in self._factors.items()}

    def apply_profile(self, cycle_name: str) -> str:
        """
        按情绪周期应用因子 profile。

        关键点（呼应 R5：单例多日回测状态污染）：每次调用**先复位到基础启用态**，
        再叠加该 profile 的禁用/启用覆盖，保证幂等、与调用历史无关。

        约定：profile 的 disabled_factors 为空（默认）时，等价于全启用 → 行为不变。
        未找到对应周期的 profile 时回退到 'default'。

        Returns:
            实际生效的 profile 名称。
        """
        # 1) 复位到基础启用态
        for fid, base in self._base_enabled.items():
            if fid in self._factors:
                self._factors[fid].enabled = base

        # 2) 选取 profile（缺失回退 default）
        profile = self._profiles.get(cycle_name)
        if profile is None:
            profile = self._profiles.get('default')
            self._active_profile = 'default' if profile is not None else cycle_name
        else:
            self._active_profile = cycle_name

        # 3) 应用覆盖
        if profile:
            for fid in (profile.get('disabled_factors') or []):
                if fid in self._factors:
                    self._factors[fid].enabled = False
            for fid in (profile.get('enabled_factors') or []):
                if fid in self._factors:
                    self._factors[fid].enabled = True

        logger.info(f"[FactorRegistry] 应用 profile: {self._active_profile} (周期={cycle_name})")
        return self._active_profile

    def get_active_profile(self) -> Optional[str]:
        """当前生效的 profile 名称（未调用 apply_profile 时为 None）。"""
        return self._active_profile

    def get_enabled_factor_ids(self) -> List[str]:
        """当前全局启用（FactorDefinition.enabled=True）的因子 ID 列表（稳定顺序）。"""
        return [fid for fid, f in self._factors.items() if f.enabled]

    def get_profiles(self) -> Dict[str, Dict]:
        """返回已加载的情绪周期 profile 定义（只读快照）。"""
        return dict(self._profiles)

    def _get_layer_key(self, layer: str) -> str:
        """获取Layer在YAML中的key名"""
        mapping = {
            'layer1': 'layer1_market_env',
            'emotion': 'emotion_cycle',
            'layer2': 'layer2_sector',
            'layer3': 'layer3_stock_select',
            'layer4': 'layer4_trade_plan',
        }
        return mapping.get(layer, layer)

    def _get_default_enabled_factors(self, layer: str, sub_category: str = None) -> List[str]:
        """当Layer配置文件不存在时，从注册表获取默认启用的因子"""
        category_map = {
            'layer1': FactorCategory.MARKET_ENV,
            'emotion': FactorCategory.EMOTION,
            'layer2': FactorCategory.SECTOR,
            'layer3': FactorCategory.STOCK_TECH,
            'layer4': FactorCategory.CROSS_CYCLE,
        }
        category = category_map.get(layer)
        if category is None:
            return []

        factors = self.list_factors(category=category, sub_category=sub_category, enabled_only=True)
        return [f.factor_id for f in factors]


def get_factor_registry(config_dir: Path = None) -> FactorRegistry:
    """获取全局因子注册中心实例"""
    return FactorRegistry(config_dir)