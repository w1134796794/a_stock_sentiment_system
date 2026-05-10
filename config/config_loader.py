"""
统一配置加载器

功能：
1. 加载YAML/JSON配置文件
2. 提供统一的配置访问接口
3. 支持配置热更新
4. 配置验证和默认值处理
"""
import yaml
import json
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
import loguru

logger = loguru.logger


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

    def _load_yaml(self, filepath: Path) -> Dict:
        """加载YAML文件"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"[ConfigLoader] 加载YAML失败 {filepath}: {e}")
            return {}

    def get_config(self, name: str) -> Dict[str, Any]:
        """获取指定配置"""
        return self._configs.get(name, {})

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
                    logger.info(f"[ConfigLoader] 重新加载配置: {name}")
        else:
            self._load_all_configs()
            logger.info("[ConfigLoader] 重新加载所有配置")


# 全局配置加载器实例
config_loader = ConfigLoader()


def get_config_loader() -> ConfigLoader:
    """获取全局配置加载器实例"""
    return config_loader


# 便捷访问函数
def get_emotion_cycle_config() -> Dict[str, Any]:
    """获取情绪周期完整配置"""
    return config_loader.get_emotion_cycle_config()


def get_emotion_thresholds() -> Dict[str, Any]:
    """获取情绪周期阈值配置"""
    config = config_loader.get_emotion_cycle_config()
    return config.get('cycle_thresholds', {})


def get_emotion_scoring_weights() -> Dict[str, float]:
    """获取情绪周期评分权重"""
    config = config_loader.get_emotion_cycle_config()
    return config.get('scoring_weights', {})


def get_emotion_cycle_rules() -> Dict[str, Any]:
    """获取情绪周期判定规则"""
    config = config_loader.get_emotion_cycle_config()
    return config.get('cycle_rules', {})


def get_emotion_strategies() -> Dict[str, Any]:
    """获取情绪周期策略配置"""
    config = config_loader.get_emotion_cycle_config()
    return config.get('cycle_strategies', {})


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


# 用于兼容旧代码的配置访问
class EmotionCycleConfig:
    """情绪周期配置兼容类"""

    def __init__(self):
        self._config = get_emotion_thresholds()

    @property
    def limit_up_high(self) -> int:
        return self._config.get('limit_up', {}).get('high', 100)

    @property
    def limit_up_mid_high(self) -> int:
        return self._config.get('limit_up', {}).get('mid_high', 80)

    @property
    def limit_up_mid_low(self) -> int:
        return self._config.get('limit_up', {}).get('mid_low', 50)

    @property
    def limit_up_low(self) -> int:
        return self._config.get('limit_up', {}).get('low', 30)

    @property
    def limit_up_freeze(self) -> int:
        return self._config.get('limit_up', {}).get('freeze', 20)

    @property
    def board_height_boom(self) -> int:
        return self._config.get('board_height', {}).get('boom', 7)

    @property
    def board_height_high(self) -> int:
        return self._config.get('board_height', {}).get('high', 6)

    @property
    def board_height_mid(self) -> int:
        return self._config.get('board_height', {}).get('mid', 4)

    @property
    def board_height_low(self) -> int:
        return self._config.get('board_height', {}).get('low', 3)

    @property
    def broken_rate_low(self) -> float:
        return self._config.get('broken_rate', {}).get('low', 15.0)

    @property
    def broken_rate_mid(self) -> float:
        return self._config.get('broken_rate', {}).get('mid', 25.0)

    @property
    def broken_rate_high(self) -> float:
        return self._config.get('broken_rate', {}).get('high', 40.0)

    @property
    def nuclear_button_low(self) -> int:
        return self._config.get('nuclear_button', {}).get('low', 3)

    @property
    def nuclear_button_high(self) -> int:
        return self._config.get('nuclear_button', {}).get('high', 10)

    @property
    def premium_high(self) -> float:
        return self._config.get('premium', {}).get('high', 3.0)

    @property
    def premium_mid(self) -> float:
        return self._config.get('premium', {}).get('mid', 1.0)

    @property
    def premium_low(self) -> float:
        return self._config.get('premium', {}).get('low', -1.0)

    @property
    def continuous_rate_high(self) -> float:
        return self._config.get('continuous_rate', {}).get('high', 30.0)

    @property
    def continuous_rate_mid(self) -> float:
        return self._config.get('continuous_rate', {}).get('mid', 20.0)

    @property
    def continuous_rate_low(self) -> float:
        return self._config.get('continuous_rate', {}).get('low', 10.0)

    @property
    def limit_down_ratio_low(self) -> float:
        return self._config.get('limit_down_ratio', {}).get('low', 0.1)

    @property
    def limit_down_ratio_mid(self) -> float:
        return self._config.get('limit_down_ratio', {}).get('mid', 0.3)

    @property
    def limit_down_ratio_high(self) -> float:
        return self._config.get('limit_down_ratio', {}).get('high', 0.5)


# 兼容旧的导入方式
def load_emotion_cycle_config() -> EmotionCycleConfig:
    """加载情绪周期配置（兼容旧代码）"""
    return EmotionCycleConfig()
