# eltdx 3.3.0升级

## 范围

本地从1.0.2升级为3.3.0，requirements.txt锁定准确版本，避免服务器继续保留旧版本或无界升级。该版本包含`helpers.board_quotes`和`helpers.board_member_quotes`，但本次仅升级SDK与现有封装，不自动切换板块确认来源、改变采样频率或降低买点门槛。

## 使用授权提醒

截至本次核验，上游[PyPI发布页](https://pypi.org/project/eltdx/3.3.0/)声明用途为个人学习与协议研究，并限制商业、付费、生产服务等用途。生产部署、VIP群消息或其他相关使用前，需要自行确认授权范围；不能把本地安装成功视为生产使用授权。当前仓库的自动部署会安装requirements，提交并部署此升级前也应完成该核对。

## 兼容修改

- 当日分时优先使用`client.minutes.today`，历史分时使用`client.minutes.history`；保留旧入口兼容分支。原分时点、估算额及成交量手数口径不变。
- 09:25撮合通过`helpers.auction_data`取得，并关闭不需要的过程序列与报价请求；保留旧版本helper分支。
- 新撮合记录为TradeTick，其成交额是`trade_amount_yuan`属性，dataclass转换不能保留该属性，因此在转换后显式读取。带有事件类型的新记录必须为09:25真实opening_match，不能用竞价虚拟快照替代。
- 批量快照、模块化K线与竞价过程接口保留。实际SDK模型测试覆盖手/元、一档盘口、开盘竞价额与协议原始时间字段。

## 离线检查

Windows使用`.venv/Scripts/python.exe -X utf8 scripts/check_eltdx_runtime.py`，Linux使用`.venv/bin/python scripts/check_eltdx_runtime.py`。

检查脚本使用SDK自带in_memory传输，验证Python/Rust导入、参数派发及所需API是否存在；不连接Redis或行情服务器、不发送通知，也不下载板块资料。输出`live_data_verified=false`，不能当作服务器能取得真实板块行情的证明。

## 部署边界

本次未升级远端服务器。确认授权与部署窗口后，使用原虚拟环境安装锁定依赖，再执行离线检查；成功后重启`a-stock-market.target`，并核对实际报价、分钟及竞价数据。不要仅凭服务active或接口存在就判断实时链路正常。

板块自身行情后续需要盘前准备通达信资料与同花顺映射，并验证成员重合、差异和失效规则。`board_quotes`按分类返回板块，不能假设调用一次只请求我们关注的46个板块；批量代码过滤、5至10秒方向通道、20至30秒宽度通道和分钟归档还需单独实现与验证。
