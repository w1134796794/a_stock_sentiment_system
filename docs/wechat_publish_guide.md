# 微信公众号自动发布指南

## 功能概述

系统自动将A股情绪分析报告发布到微信公众号，支持：
- **预览模式**：发送给指定微信号预览
- **正式发布**：群发给所有公众号粉丝
- **自动格式化**：报告自动转换为公众号HTML格式

## 前置条件

### 1. 申请微信公众号

1. 访问 [微信公众平台](https://mp.weixin.qq.com)
2. 注册订阅号或服务号
3. 完成认证（服务号需要企业认证）

### 2. 开启开发者模式

1. 登录公众号后台
2. 左侧菜单：开发 → 基本配置
3. 启用开发者模式
4. 记录以下信息：
   - **AppID**(应用ID)
   - **AppSecret**(应用密钥)

### 3. 添加IP白名单

1. 在基本配置页面，找到"IP白名单"
2. 添加你的服务器IP地址
3. 如果是本地测试，需要内网穿透（如ngrok）

## 配置步骤

### 方式一：环境变量（推荐）

```bash
# Windows PowerShell
$env:WECHAT_APP_ID="your_app_id_here"
$env:WECHAT_APP_SECRET="your_app_secret_here"

# Windows CMD
set WECHAT_APP_ID=your_app_id_here
set WECHAT_APP_SECRET=your_app_secret_here

# Linux/Mac
export WECHAT_APP_ID=your_app_id_here
export WECHAT_APP_SECRET=your_app_secret_here
```

### 方式二：直接修改配置文件

编辑 `config/settings.py`：

```python
WECHAT_CONFIG = {
    "enabled": True,  # 启用公众号发布
    "app_id": "your_app_id_here",  # 替换为你的AppID
    "app_secret": "your_app_secret_here",  # 替换为你的AppSecret
    "author": "A股情绪系统",  # 文章作者
    "preview_wx": "your_wechat_id",  # 预览微信号（可选）
    "auto_publish": False,  # 是否自动发布（建议先False测试）
}
```

## 使用方法

### 1. 预览模式（推荐先测试）

```bash
# 发送今天的报告预览
python scripts/publish_to_wechat.py --preview

# 发送指定日期的报告预览
python scripts/publish_to_wechat.py --date 20250421 --preview

# 指定预览微信号
python scripts/publish_to_wechat.py --preview --wx your_wechat_id
```

### 2. 正式发布

```bash
# 正式发布今天的报告
python scripts/publish_to_wechat.py --publish

# 正式发布指定日期的报告
python scripts/publish_to_wechat.py --date 20250421 --publish
```

### 3. 集成到主流程

修改 `main.py`，在报告生成后自动发布：

```python
from config.settings import WECHAT_CONFIG
from scripts.publish_to_wechat import publish_report

# 在报告生成后调用
if WECHAT_CONFIG.get('enabled'):
    publish_report(
        date=date,
        preview=not WECHAT_CONFIG.get('auto_publish'),
        preview_wx=WECHAT_CONFIG.get('preview_wx')
    )
```

## 发布内容格式

公众号文章包含以下板块：

### 1. 情绪概览
- 综合评分（0-100）
- 情绪级别（恐慌/悲观/中性/乐观/狂热）
- 简短描述

### 2. 热点概念排行
- 概念名称、排名
- 10日内进入前10/5/3的次数
- 是否主线特征
- 涨停数量
- 所处阶段

### 3. 龙头候选池
- 股票名称、代码
- 龙头类型（连板龙/趋势龙）
- 所属概念
- 10日涨幅
- 涨停次数

### 4. 龙头走弱池
- 走弱股票列表
- 走弱原因
- 操作建议

### 5. 免责声明
- 风险提示
- 版权信息

## 注意事项

### 发布频次限制
- **订阅号**：每天可群发1次
- **服务号**：每月可群发4次

### 图片限制
- 封面图片：建议900x500像素
- 正文图片：单张不超过2MB
- 图片格式：jpg、png

### 内容审核
- 避免使用敏感词汇
- 不要承诺收益
- 免责声明必须完整

### 常见问题

**Q: 提示"access_token获取失败"**
A: 检查AppID和AppSecret是否正确，IP是否在白名单中

**Q: 提示"预览微信号不能为空"**
A: 在配置中设置preview_wx，或使用--wx参数指定

**Q: 文章格式错乱**
A: 公众号只支持部分HTML标签，系统会自动过滤不支持的标签

**Q: 图片无法显示**
A: 需要先上传图片到公众号素材库，系统会自动处理

## 安全建议

1. **不要泄露AppSecret**：相当于公众号的密码
2. **使用环境变量**：避免将密钥写入代码
3. **定期更换密钥**：在公众号后台可重置AppSecret
4. **限制IP白名单**：只允许特定IP调用API

## 技术支持

微信官方文档：https://developers.weixin.qq.com/doc/offiaccount/Getting_Started/Overview.html

## 更新日志

- 2025-04-21: 初始版本，支持图文消息发布
