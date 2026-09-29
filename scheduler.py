"""
定时任务调度器 - 每日收盘后自动执行分析
"""
import time

def main():
    from core.automation.internal_scheduler import AUTOMATION_SCHEDULER

    AUTOMATION_SCHEDULER.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        AUTOMATION_SCHEDULER.stop()

if __name__ == "__main__":
    main()
