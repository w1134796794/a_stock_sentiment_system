"""
定时任务调度器 - 每日收盘后自动执行分析
"""
import schedule
import time
from datetime import datetime
from pathlib import Path
import sys
import subprocess

def job():
    """定时执行的分析任务"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] 开始执行日度分析任务...")
    
    try:
        result = subprocess.run(
            [sys.executable, "main.py"], 
            capture_output=True, 
            text=True,
            encoding='utf-8'
        )
        print(result.stdout)
        if result.stderr:
            print("错误:", result.stderr)
    except Exception as e:
        print(f"任务执行失败: {e}")

def main():
    schedule.every().day.at("15:40").do(job)
    print("调度器已启动，每日15:40自动执行...")
    print("按 Ctrl+C 停止")
    
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()
