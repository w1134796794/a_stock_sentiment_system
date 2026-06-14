"""
统一配置加载器

功能：
1. 加载YAML/JSON配置文件
2. 提供统一的配置访问接口
3. 支持配置热更新
4. 启动时 schema 校验（P3-4）
5. 统一访问入口 get_setting：YAML 优先，回退 settings.py（P3-4）
"""
import yaml
import json
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
import loguru

logger = loguru.logger


# 用作 "未设置" 的哨兵值（P3-4）
_SENTINEL = object()


class ConfigLoader:
    """统一配置加载器"""

    _instance = None
    _configs: Dict[str, Any] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, '_initialized'):
            self.config_dir = Path(__file__).parent
            self._initialized = True
            self._load_all_configs()
            self._validate_loaded()

    def _load_all_configs(self):
        """加载所有配置文件"""
        config_files = {
            'emotion_cycle': 'emotion_cycle_config.yaml',
            'sector_tracker': 'sector_tracker_config.yaml',
        }

        for name, filename in config_files.items():
            filepath = self.config_dir / filename
            if filepath.exists():
                self._configs[name] = self._load_yaml(filepath)
                logger.info(f"[ConfigLoader] 加载配置: {filename}")
            else:
                logger.warning(f"[ConfigLoader] 配置文件不存在: {filepath}")

        self._load_factor_configs()
        self._apply_overrides()

    def _apply_overrides(self):
        """套用 webdata/config_overrides.json 的 yaml 作用域覆盖（就地修改 _configs）。"""
        try:
            from config.overrides import apply_yaml_overrides
            apply_yaml_overrides(self._configs)
        except Exception as e:  # pragma: no cover
            logger.debug(f"[ConfigLoader] 套用YAML覆盖失败: {e}")

    def _load_factor_configs(self):
        """加载因子配置文件"""
        factors_dir = self.config_dir / "factors"
        if not factors_dir.exists():
            logger.debug("[ConfigLoader] factors目录不存在，跳过因子配置加载")
            return

        factor_config_files = {
            'factor_registry': 'factor_registry.yaml',
            'layer1_market_env': 'layer1_market_env.yaml',
            'emotion_cycle_factors': 'emotion_cycle.yaml',
            'layer2_sector': 'layer2_sector.yaml',
            'layer3_stock_select': 'layer3_stock_select.yaml',
            'layer4_trade_plan': 'layer4_trade_plan.yaml',
        }

        for name, filename in factor_config_files.items():
            filepath = factors_dir / filename
            if filepath.exists():
                self._configs[name] = self._load_yaml(filepath)
                logger.info(f"[ConfigLoader] 加载因子配置: factors/{filename}")
            else:
                logger.debug(f"[ConfigLoader] 因子配置文件不存在: factors/{filename}")

    def _load_yaml(self, filepath: Path) -> Dict:
        """加载YAML文件"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"[ConfigLoader] 加载YAML失败 {filepath}: {e}")
            return {}

    # =========================================================================
    # P3-4：配置 schema 校验
    # =========================================================================

    # 必备配置键 schema：(配置名, 必备路径列表)
    REQUIRED_KEYS: Dict[str, list] = {
        'emotion_cycle': [
            'phase_model.cohort',
            'phase_model.thresholds',
        ],
        'sector_tracker': [
            'analyze_sectors.top_n',
            'sector_params',
            'persistence.lookback_days',
        ],
    }

    def _validate_loaded(self) -> None:
        """启动时对必备配置做轻量 schema 校验，缺失键打 warning（不阻断启动）"""
        for cfg_name, key_paths in self.REQUIRED_KEYS.items():
            cfg = self._configs.get(cfg_name)
            if cfg is None:
                logger.warning(f"[ConfigLoader] 必备配置缺失: {cfg_name}.yaml")
                continue
            for path in key_paths:
                if not self._has_path(cfg, path):
                    logger.warning(
                        f"[ConfigLoader] {cfg_name} 缺少必备键: {path}"
                    )

    @staticmethod
    def _has_path(d: Dict, dotted_path: str) -> bool:
        """检查嵌套字典是否包含 dotted 路径，如 'analyze_sectors.top_n'"""
        cur: Any = d
        for part in dotted_path.split('.'):
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur[part]
        return True

    @staticmethod
    def _resolve_path(d: Dict, dotted_path: str, default: Any = None) -> Any:
        cur: Any = d
        for part in dotted_path.split('.'):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    # 全部 YAML 配置名 -> 相对 config 目录的文件路径（供注册表读取原始默认值）
    ALL_CONFIG_FILES: Dict[str, str] = {
        'emotion_cycle': 'emotion_cycle_config.yaml',
        'sector_tracker': 'sector_tracker_config.yaml',
        'factor_registry': 'factors/factor_registry.yaml',
        'layer1_market_env': 'factors/layer1_market_env.yaml',
        'emotion_cycle_factors': 'factors/emotion_cycle.yaml',
        'layer2_sector': 'factors/layer2_sector.yaml',
        'layer3_stock_select': 'factors/layer3_stock_select.yaml',
        'layer4_trade_plan': 'factors/layer4_trade_plan.yaml',
    }

    def get_config(self, name: str) -> Dict[str, Any]:
        """获取指定配置（已套用 Web 覆盖）"""
        return self._configs.get(name, {})

    def loaded_config_names(self) -> list:
        """返回当前已加载的配置名列表。"""
        return list(self._configs.keys())

    def pristine_config(self, name: str) -> Dict[str, Any]:
        """读取某配置的**原始文件**内容（不含 Web 覆盖），供默认值展示/重置参考。"""
        rel = self.ALL_CONFIG_FILES.get(name)
        if not rel:
            return {}
        fp = self.config_dir / rel
        return self._load_yaml(fp) if fp.exists() else {}

    # =========================================================================
    # P3-4：统一访问入口
    # =========================================================================

    def get_setting(self, dotted_path: str, default: Any = None) -> Any:
        """
        统一配置访问入口（YAML 优先，回退 settings.py）。

        路径格式：`<group>.<key>...`，例：
            - 'sector_tracker.persistence.lookback_days'  -> 走 sector_tracker.yaml
            - 'settings.LIMIT_UP_THRESHOLD'               -> 走 config.settings 模块属性

        Returns:
            找到的值；找不到时返回 default。
        """
        if not dotted_path:
            return default

        head, _, rest = dotted_path.partition('.')

        # 1) YAML 配置组
        if head in self._configs:
            value = self._resolve_path(self._configs[head], rest, _SENTINEL)
            if value is not _SENTINEL:
                return value

        # 2) 回退到 config.settings 模块属性
        if head == 'settings' or head == '':
            try:
                from config import settings  # 延迟 import 避免循环依赖
                attr_path = rest if head == 'settings' else dotted_path
                if attr_path:
                    parts = attr_path.split('.')
                    val: Any = getattr(settings, parts[0], _SENTINEL)
                    if val is _SENTINEL:
                        return default
                    for p in parts[1:]:
                        if isinstance(val, dict):
                            val = val.get(p, _SENTINEL)
                        else:
                            val = getattr(val, p, _SENTINEL)
                        if val is _SENTINEL:
                            return default
                    return val
            except Exception:
                pass

        return default

    def get_emotion_cycle_config(self) -> Dict[str, Any]:
        """获取情绪周期配置"""
        return self.get_config('emotion_cycle')

    def get_sector_tracker_config(self) -> Dict[str, Any]:
        """获取板块追踪器配置"""
        return self.get_config('sector_tracker')

    def reload_config(self, name: str = None):
        """重新加载配置"""
        if name:
            config_files = {
                'emotion_cycle': 'emotion_cycle_config.yaml',
                'sector_tracker': 'sector_tracker_config.yaml',
            }
            if name in config_files:
                filepath = self.config_dir / config_files[name]
                if filepath.exists():
                    self._configs[name] = self._load_yaml(filepath)
                    self._apply_overrides()
                    logger.info(f"[ConfigLoader] 重新加载配置: {name}")
        else:
            self._load_all_configs()
            logger.info("[ConfigLoader] 重新加载所有配置")


# 全局配置加载器实例
config_loader = ConfigLoader()


def get_setting(dotted_path: str, default: Any = None) -> Any:
    """
    便捷的统一配置访问函数。

    例：
        from config.config_loader import get_setting

        top_n = get_setting('sector_tracker.persistence.top_n', 10)
        token = get_setting('settings.TUSHARE_TOKEN')
    """
    return config_loader.get_setting(dotted_path, default)


def get_config_loader() -> ConfigLoader:
    """获取全局配置加载器实例"""
    return config_loader


# 便捷访问函数
def get_emotion_cycle_config() -> Dict[str, Any]:
    """获取情绪周期完整配置"""
    return config_loader.get_emotion_cycle_config()


def get_sector_tracker_config() -> Dict[str, Any]:
    """获取板块追踪器完整配置"""
    return config_loader.get_sector_tracker_config()


def get_sector_params() -> Dict[str, Any]:
    """获取板块差异化参数"""
    config = config_loader.get_sector_tracker_config()
    return config.get('sector_params', {})


def get_sector_analyze_config() -> Dict[str, Any]:
    """获取板块分析配置"""
    config = config_loader.get_sector_tracker_config()
    return config.get('analyze_sectors', {})


def get_persistence_config() -> Dict[str, Any]:
    """获取持续性分析配置"""
    config = config_loader.get_sector_tracker_config()
    return config.get('persistence', {})


def get_internal_structure_config() -> Dict[str, Any]:
    """获取内部结构分析配置"""
    config = config_loader.get_sector_tracker_config()
    return config.get('internal_structure', {})


def get_resonance_config() -> Dict[str, Any]:
    """获取共振分析配置"""
    config = config_loader.get_sector_tracker_config()
    return config.get('resonance', {})


def get_sector_relation_config() -> Dict[str, Any]:
    """获取板块关联配置"""
    config = config_loader.get_sector_tracker_config()
    return config.get('sector_relation', {})


# ============================================
# 因子配置便捷访问函数
# ============================================

def get_factor_registry_config() -> Dict[str, Any]:
    """获取因子注册表配置"""
    return config_loader.get_config('factor_registry')


def get_layer1_factor_config() -> Dict[str, Any]:
    """获取Layer1因子配置"""
    return config_loader.get_config('layer1_market_env')


def get_emotion_factor_config() -> Dict[str, Any]:
    """获取情绪周期因子配置"""
    return config_loader.get_config('emotion_cycle_factors')


def get_layer2_factor_config() -> Dict[str, Any]:
    """获取Layer2因子配置"""
    return config_loader.get_config('layer2_sector')


def get_layer3_factor_config() -> Dict[str, Any]:
    """获取Layer3因子配置"""
    return config_loader.get_config('layer3_stock_select')


def get_layer4_factor_config() -> Dict[str, Any]:
    """获取Layer4因子配置"""
    return config_loader.get_config('layer4_trade_plan')


def get_layer_factor_config(layer: str) -> Dict[str, Any]:
    """
    根据Layer名称获取对应的因子配置

    Args:
        layer: 'layer1' / 'emotion' / 'layer2' / 'layer3' / 'layer4'

    Returns:
        因子配置字典
    """
    mapping = {
        'layer1': 'layer1_market_env',
        'emotion': 'emotion_cycle_factors',
        'layer2': 'layer2_sector',
        'layer3': 'layer3_stock_select',
        'layer4': 'layer4_trade_plan',
    }
    config_name = mapping.get(layer, '')
    return config_loader.get_config(config_name) if config_name else {}
