"""
微信公众号发布模块

功能：
1. 获取和缓存access_token
2. 上传图文消息素材
3. 发布图文消息到公众号
4. 生成适合公众号的报告格式

使用流程：
1. 在config.yaml中配置AppID和AppSecret
2. 调用WechatPublisher.publish_report()发布报告
"""

from .wechat_publisher import WechatPublisher

__all__ = ['WechatPublisher']
