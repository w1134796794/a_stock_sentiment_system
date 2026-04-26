"""
微信公众号发布器

实现微信公众号图文消息的自动发布
"""
import json
import time
import requests
import base64
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import loguru
from dataclasses import dataclass

logger = loguru.logger


@dataclass
class Article:
    """公众号图文消息"""
    title: str                    # 标题
    content: str                  # 图文消息具体内容（HTML格式）
    thumb_media_id: str = ""      # 封面图片素材ID
    author: str = ""             # 作者
    digest: str = ""             # 图文消息的摘要
    show_cover_pic: int = 1       # 是否显示封面，1为显示，0为不显示
    content_source_url: str = ""  # 原文链接
    need_open_comment: int = 0    # 是否打开评论，0不打开，1打开
    only_fans_can_comment: int = 0  # 是否粉丝才可评论，0所有人可评论，1粉丝才可评论


class WechatPublisher:
    """
    微信公众号发布器
    
    功能：
    1. 自动获取和缓存access_token
    2. 上传图片素材
    3. 上传图文消息
    4. 发布图文消息
    """
    
    # 微信API接口地址
    API_BASE = "https://api.weixin.qq.com/cgi-bin"
    
    def __init__(self, app_id: str, app_secret: str, token_cache_file: str = None):
        """
        初始化发布器
        
        Args:
            app_id: 微信公众号AppID
            app_secret: 微信公众号AppSecret
            token_cache_file: access_token缓存文件路径
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self.token_cache_file = token_cache_file or "data/wechat_token.json"
        
        self._access_token = None
        self._token_expires_time = None
        
        # 确保缓存目录存在
        Path(self.token_cache_file).parent.mkdir(parents=True, exist_ok=True)
        
        logger.info("[WechatPublisher] 初始化完成")
    
    def _get_access_token(self) -> str:
        """
        获取access_token（带缓存）
        
        access_token有效期为7200秒（2小时），需要缓存避免频繁请求
        
        Returns:
            access_token字符串
        """
        # 检查内存缓存
        if self._access_token and self._token_expires_time:
            if datetime.now() < self._token_expires_time:
                return self._access_token
        
        # 检查文件缓存
        try:
            cache_path = Path(self.token_cache_file)
            if cache_path.exists():
                with open(cache_path, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
                    expires_time = datetime.fromisoformat(cache['expires_time'])
                    if datetime.now() < expires_time:
                        self._access_token = cache['access_token']
                        self._token_expires_time = expires_time
                        logger.info("[WechatPublisher] 从文件缓存获取access_token")
                        return self._access_token
        except Exception as e:
            logger.warning(f"[WechatPublisher] 读取token缓存失败: {e}")
        
        # 重新获取access_token
        url = f"{self.API_BASE}/token"
        params = {
            "grant_type": "client_credential",
            "appid": self.app_id,
            "secret": self.app_secret
        }
        
        try:
            response = requests.get(url, params=params, timeout=10)
            result = response.json()
            
            if 'access_token' not in result:
                error_msg = result.get('errmsg', '未知错误')
                logger.error(f"[WechatPublisher] 获取access_token失败: {error_msg}")
                raise Exception(f"获取access_token失败: {error_msg}")
            
            self._access_token = result['access_token']
            expires_in = result.get('expires_in', 7200)
            # 提前5分钟过期，避免边界问题
            self._token_expires_time = datetime.now() + timedelta(seconds=expires_in - 300)
            
            # 保存到文件缓存
            cache = {
                'access_token': self._access_token,
                'expires_time': self._token_expires_time.isoformat()
            }
            with open(self.token_cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache, f)
            
            logger.info(f"[WechatPublisher] 成功获取access_token，有效期{expires_in}秒")
            return self._access_token
            
        except requests.RequestException as e:
            logger.error(f"[WechatPublisher] 请求access_token失败: {e}")
            raise
    
    def upload_image(self, image_path: str) -> str:
        """
        上传图片素材到公众号
        
        Args:
            image_path: 图片文件路径
            
        Returns:
            图片URL，可用于图文消息中
        """
        url = f"{self.API_BASE}/media/uploadimg"
        params = {"access_token": self._get_access_token()}
        
        try:
            with open(image_path, 'rb') as f:
                files = {'media': f}
                response = requests.post(url, params=params, files=files, timeout=30)
                result = response.json()
                
                if 'url' in result:
                    logger.info(f"[WechatPublisher] 图片上传成功: {result['url']}")
                    return result['url']
                else:
                    error_msg = result.get('errmsg', '未知错误')
                    logger.error(f"[WechatPublisher] 图片上传失败: {error_msg}")
                    raise Exception(f"图片上传失败: {error_msg}")
                    
        except Exception as e:
            logger.error(f"[WechatPublisher] 图片上传异常: {e}")
            raise
    
    def upload_news_image(self, image_path: str) -> str:
        """
        上传图文消息内的图片素材
        
        返回的URL可以在图文消息正文中使用
        
        Args:
            image_path: 图片文件路径
            
        Returns:
            图片URL
        """
        url = f"{self.API_BASE}/media/uploadimg"
        params = {"access_token": self._get_access_token()}
        
        try:
            with open(image_path, 'rb') as f:
                files = {'media': f}
                response = requests.post(url, params=params, files=files, timeout=30)
                result = response.json()
                
                if 'url' in result:
                    logger.info(f"[WechatPublisher] 图文图片上传成功")
                    return result['url']
                else:
                    error_msg = result.get('errmsg', '未知错误')
                    logger.error(f"[WechatPublisher] 图文图片上传失败: {error_msg}")
                    raise Exception(f"图文图片上传失败: {error_msg}")
                    
        except Exception as e:
            logger.error(f"[WechatPublisher] 图文图片上传异常: {e}")
            raise
    
    def upload_thumb_media(self, image_path: str) -> str:
        """
        上传缩略图（封面图片）
        
        返回media_id，用于图文消息的封面
        
        注意：新版接口使用 material/add_material，但type应该是image而不是thumb
        
        Args:
            image_path: 图片文件路径
            
        Returns:
            media_id
        """
        url = f"{self.API_BASE}/material/add_material"
        params = {
            "access_token": self._get_access_token(),
            "type": "image"  # 使用image而不是thumb
        }
        
        try:
            with open(image_path, 'rb') as f:
                files = {'media': f}
                response = requests.post(url, params=params, files=files, timeout=30)
                result = response.json()
                
                if 'media_id' in result:
                    logger.info(f"[WechatPublisher] 封面图片上传成功: {result['media_id']}")
                    return result['media_id']
                else:
                    error_msg = result.get('errmsg', '未知错误')
                    logger.error(f"[WechatPublisher] 封面图片上传失败: {error_msg}")
                    raise Exception(f"封面图片上传失败: {error_msg}")
                    
        except Exception as e:
            logger.error(f"[WechatPublisher] 封面图片上传异常: {e}")
            raise
    
    def add_draft(self, articles: List[Article]) -> str:
        """
        添加草稿（新版接口，替代已废弃的add_news）
        
        微信已废弃 add_news 接口，需要使用草稿箱接口
        
        Args:
            articles: 图文消息列表
            
        Returns:
            media_id，用于发布图文消息
        """
        url = f"{self.API_BASE}/draft/add"
        params = {"access_token": self._get_access_token()}
        
        # 构建请求数据
        articles_data = []
        for article in articles:
            articles_data.append({
                "title": article.title,
                "thumb_media_id": article.thumb_media_id,
                "author": article.author,
                "digest": article.digest,
                "show_cover_pic": article.show_cover_pic,
                "content": article.content,
                "content_source_url": article.content_source_url,
                "need_open_comment": article.need_open_comment,
                "only_fans_can_comment": article.only_fans_can_comment
            })
        
        data = {"articles": articles_data}
        
        # 打印完整请求信息用于调试
        logger.info(f"[WechatPublisher] 草稿请求URL: {url}")
        logger.info(f"[WechatPublisher] 草稿请求参数: {params}")
        # 只打印文章标题和thumb_media_id，避免日志过大
        debug_data = {
            "articles": [
                {
                    "title": a.get("title", ""),
                    "thumb_media_id": a.get("thumb_media_id", ""),
                    "author": a.get("author", ""),
                    "digest": a.get("digest", "")[:50] + "..." if len(a.get("digest", "")) > 50 else a.get("digest", "")
                } for a in articles_data
            ]
        }
        logger.info(f"[WechatPublisher] 草稿请求数据(摘要): {json.dumps(debug_data, ensure_ascii=False)}")
        
        try:
            response = requests.post(
                url, 
                params=params, 
                data=json.dumps(data, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                timeout=30
            )
            
            # 打印响应信息
            logger.info(f"[WechatPublisher] 草稿响应状态码: {response.status_code}")
            logger.info(f"[WechatPublisher] 草稿响应内容: {response.text}")
            
            result = response.json()
            
            if 'media_id' in result:
                logger.info(f"[WechatPublisher] 草稿添加成功: {result['media_id']}")
                return result['media_id']
            else:
                error_msg = result.get('errmsg', '未知错误')
                logger.error(f"[WechatPublisher] 草稿添加失败: {error_msg}")
                raise Exception(f"草稿添加失败: {error_msg}")
                
        except Exception as e:
            logger.error(f"[WechatPublisher] 草稿添加异常: {e}")
            raise
    
    def publish_news(self, media_id: str) -> bool:
        """
        发布图文消息（群发）- 使用新版发布接口
        
        注意：群发接口有频次限制，订阅号每天1次，服务号每月4次
        
        Args:
            media_id: 草稿media_id
            
        Returns:
            是否发布成功
        """
        # 新版接口：先发布为草稿，然后群发
        url = f"{self.API_BASE}/freepublish/submit"
        params = {"access_token": self._get_access_token()}
        
        data = {
            "media_id": media_id
        }
        
        try:
            response = requests.post(
                url,
                params=params,
                data=json.dumps(data, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                timeout=30
            )
            result = response.json()
            
            if result.get('errcode') == 0:
                publish_id = result.get('publish_id', 'unknown')
                logger.info(f"[WechatPublisher] 图文消息发布成功，发布ID: {publish_id}")
                return True
            else:
                error_msg = result.get('errmsg', '未知错误')
                logger.error(f"[WechatPublisher] 图文消息发布失败: {error_msg}")
                return False
                
        except Exception as e:
            logger.error(f"[WechatPublisher] 图文消息发布异常: {e}")
            return False
    
    def preview_news(self, media_id: str, wx_name: str) -> bool:
        """
        预览图文消息（发送给指定用户预览）- 使用新版接口
        
        用于正式发布前的测试
        
        Args:
            media_id: 草稿media_id
            wx_name: 接收预览的微信号
            
        Returns:
            是否发送成功
        """
        # 新版接口：使用草稿预览接口
        url = f"{self.API_BASE}/draft/preview"
        params = {"access_token": self._get_access_token()}
        
        data = {
            "touser": wx_name,
            "media_id": media_id
        }
        
        # 打印完整请求信息用于调试
        logger.info(f"[WechatPublisher] 预览请求URL: {url}")
        logger.info(f"[WechatPublisher] 预览请求参数: {params}")
        logger.info(f"[WechatPublisher] 预览请求数据: {json.dumps(data, ensure_ascii=False)}")
        
        try:
            response = requests.post(
                url,
                params=params,
                data=json.dumps(data, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                timeout=30
            )
            
            # 打印响应信息
            logger.info(f"[WechatPublisher] 预览响应状态码: {response.status_code}")
            logger.info(f"[WechatPublisher] 预览响应内容: {response.text}")
            
            result = response.json()
            
            if result.get('errcode') == 0:
                logger.info(f"[WechatPublisher] 预览消息已发送给: {wx_name}")
                return True
            else:
                error_msg = result.get('errmsg', '未知错误')
                logger.error(f"[WechatPublisher] 预览消息发送失败: {error_msg}")
                return False
                
        except Exception as e:
            logger.error(f"[WechatPublisher] 预览消息发送异常: {e}")
            return False
    
    def publish_report(self, title: str, content: str, 
                      thumb_path: str = None,
                      author: str = "A股情绪系统",
                      preview_wx: str = None) -> bool:
        """
        发布报告到公众号（简化接口）
        
        Args:
            title: 文章标题
            content: 文章内容（HTML格式）
            thumb_path: 封面图片路径
            author: 作者名
            preview_wx: 预览微信号（可选，用于测试）
            
        Returns:
            是否发布成功
        """
        try:
            # 1. 上传封面图片
            thumb_media_id = None
            if thumb_path and Path(thumb_path).exists():
                try:
                    thumb_media_id = self.upload_thumb_media(thumb_path)
                except Exception as e:
                    logger.warning(f"[WechatPublisher] 封面上传失败，将不使用封面: {e}")
            
            # 如果没有封面，创建默认封面
            if not thumb_media_id:
                try:
                    default_thumb = self._create_default_thumb()
                    if default_thumb and Path(default_thumb).exists():
                        thumb_media_id = self.upload_thumb_media(default_thumb)
                        logger.info(f"[WechatPublisher] 使用默认封面: {thumb_media_id}")
                except Exception as e:
                    logger.warning(f"[WechatPublisher] 默认封面创建失败: {e}")
            
            # 2. 创建图文消息
            article_kwargs = {
                "title": title,
                "content": content,
                "author": author,
                "digest": f"{datetime.now().strftime('%Y年%m月%d日')} A股市场情绪分析报告"
            }
            
            # 只有成功上传封面后才添加
            if thumb_media_id:
                article_kwargs["thumb_media_id"] = thumb_media_id
            
            article = Article(**article_kwargs)
            
            # 3. 添加草稿（新版接口）
            media_id = self.add_draft([article])
            
            # 4. 预览或发布
            if preview_wx is None:
                # 仅生成草稿，不发布也不预览
                logger.info(f"[WechatPublisher] 草稿已生成: {media_id}")
                return True
            elif preview_wx:
                return self.preview_news(media_id, preview_wx)
            else:
                return self.publish_news(media_id)
                
        except Exception as e:
            logger.error(f"[WechatPublisher] 发布报告失败: {e}")
            return False
    
    def _create_default_thumb(self) -> str:
        """
        创建默认封面图片
        
        使用PIL生成一个简单的封面图片
        
        Returns:
            图片文件路径
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
            
            # 创建图片 (900x500 是公众号封面推荐尺寸)
            width, height = 900, 500
            image = Image.new('RGB', (width, height), color='#1a1a2e')
            draw = ImageDraw.Draw(image)
            
            # 添加渐变背景
            for y in range(height):
                r = int(26 + (y / height) * 20)
                g = int(26 + (y / height) * 40)
                b = int(46 + (y / height) * 60)
                draw.line([(0, y), (width, y)], fill=(r, g, b))
            
            # 添加标题
            title = "A股情绪分析"
            subtitle = datetime.now().strftime('%Y-%m-%d')
            
            # 尝试使用系统字体
            try:
                font_large = ImageFont.truetype("simhei.ttf", 80)
                font_small = ImageFont.truetype("simhei.ttf", 40)
            except:
                try:
                    font_large = ImageFont.truetype("arial.ttf", 80)
                    font_small = ImageFont.truetype("arial.ttf", 40)
                except:
                    font_large = ImageFont.load_default()
                    font_small = ImageFont.load_default()
            
            # 计算文字位置（居中）
            bbox = draw.textbbox((0, 0), title, font=font_large)
            text_width = bbox[2] - bbox[0]
            x = (width - text_width) // 2
            y = height // 2 - 60
            
            # 绘制文字（带阴影）
            draw.text((x+3, y+3), title, fill='#000000', font=font_large)
            draw.text((x, y), title, fill='#ffffff', font=font_large)
            
            # 绘制日期
            bbox2 = draw.textbbox((0, 0), subtitle, font=font_small)
            text_width2 = bbox2[2] - bbox2[0]
            x2 = (width - text_width2) // 2
            y2 = height // 2 + 60
            
            draw.text((x2+2, y2+2), subtitle, fill='#000000', font=font_small)
            draw.text((x2, y2), subtitle, fill='#cccccc', font=font_small)
            
            # 保存图片
            thumb_path = "data/default_thumb.jpg"
            Path(thumb_path).parent.mkdir(parents=True, exist_ok=True)
            image.save(thumb_path, "JPEG", quality=90)
            
            logger.info(f"[WechatPublisher] 默认封面已创建: {thumb_path}")
            return thumb_path
            
        except ImportError:
            logger.warning("[WechatPublisher] 未安装PIL，无法创建默认封面")
            return None
        except Exception as e:
            logger.error(f"[WechatPublisher] 创建默认封面失败: {e}")
            return None


if __name__ == "__main__":
    # 测试代码
    import os
    
    # 从环境变量读取配置（实际使用时应从配置文件读取）
    app_id = os.getenv("WECHAT_APP_ID", "")
    app_secret = os.getenv("WECHAT_APP_SECRET", "")
    
    if not app_id or not app_secret:
        print("请设置环境变量 WECHAT_APP_ID 和 WECHAT_APP_SECRET")
        exit(1)
    
    publisher = WechatPublisher(app_id, app_secret)
    
    # 测试发布
    test_content = """
    <h1>A股情绪分析报告</h1>
    <p>这是测试内容</p>
    """
    
    result = publisher.publish_report(
        title=f"A股情绪分析 - {datetime.now().strftime('%m月%d日')}",
        content=test_content,
        preview_wx="your_wechat_id"  # 替换为你的微信号
    )
    
    print(f"发布结果: {'成功' if result else '失败'}")
