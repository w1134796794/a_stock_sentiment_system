"""
数据维护工具 - 缓存清理、磁盘检查
"""
import shutil
from pathlib import Path
from datetime import datetime, timedelta

def clean_old_cache(cache_dir: Path, keep_days: int = 7):
    """清理旧缓存"""
    cutoff = datetime.now() - timedelta(days=keep_days)
    for item in Path(cache_dir).iterdir():
        if item.is_dir():
            try:
                dir_date = datetime.strptime(item.name, "%Y%m%d")
                if dir_date < cutoff:
                    shutil.rmtree(item)
                    print(f"清理: {item.name}")
            except:
                continue

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config.settings import CACHE_DIR
    clean_old_cache(CACHE_DIR)
