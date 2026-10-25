# 云端盘中分析：只读 HTTPS 接口

接口使用现有 Web 服务，不增加采集进程，不依赖 SSH 或 Tailscale，不开放 Redis/数据库端口。部署代码后默认关闭，只有服务用户能读取的有效 Token 哈希配置存在时才启用。域名暂定 `https://astockreview.cn`；用户提供的原地址是 HTTP，生产 HTTPS 状态必须单独验证。

## 已实现的查询

所有接口都位于 `/api/v1/readonly`，仅支持 GET，必须通过 HTTPS 并携带 `Authorization: Bearer <token>`。网页登录 Cookie 不授权此接口；该 Token 也不能授权其他管理、手机或写入接口。

| 路径 | 参数 | 数据来源与限制 |
| --- | --- | --- |
| `/health` | 无 | 读取采集、归档、策略、分钟同步的共享心跳；明确 Redis/进程内存模式和时间戳状态 |
| `/dates` | 无 | 最近 30 个决策池文件日期，不查询或重建数据库 |
| `/quotes` | `codes=000001,600000` | 最多 20 个股票，从已有共享行情缓存读取；返回源时间、接收时间、陈旧标记和缺失代码 |
| `/signals` | `date=YYYYMMDD`、可选 `code/offset/limit` | 已发布的独立策略结果；过期的实时 confirmed 降级为 observe，不执行策略 |
| `/candidates` | `date=YYYYMMDD`、可选 `code/offset/limit` | 已生成决策池，缺失时读取同日期 screening 文件；包含已保存的候选和排除原因 |
| `/minutes/{code}` | `date=YYYYMMDD`、可选 `offset/limit` | 优先共享分钟缓存，其次本地 CSV/CSV.GZ；缺失不补数。每次最多 500 行 |
| `/logs/summary` | `date=YYYYMMDD`、`kind=preopen/postclose` | 固定日志类型末尾最多 64KiB 的错误/警告行计数；不提供原始日志文本 |

候选和信号分页每次最多 100 行，响应最多 2MiB；磁盘 JSON 单文件最多 16MiB，分钟 CSV 解压最多 2MiB、2000 行。超限或暂不可读返回明确错误，不扫描整个缓存。API 不接受文件路径、Redis key、SQL 或命令参数。

`as_of` 是服务响应时间，不等同于行情时间。行情的 `date/time/received_at`、信号的 `generated_at` 和分钟的 `last_bar_time` 用于判断数据新鲜度。缺失返回 `missing`，不会用空数据宣称行情正常；历史文件使用 `stored` 状态。CSV 数值保留原始字符串，共享缓存保持原数据类型。日志仅提供统计，分析具体异常仍需要人工提供已脱敏详情。

## 1. 移除服务器 Tailscale

使用现有阿里云免密登录执行，保留当前终端并新开登录窗口验证。此步骤会删除本次 Tailscale 登录状态，不修改 SSH/UFW 或其他服务。

```bash
(
set -e
systemctl stop stock-ts-rollback.timer 2>/dev/null || true
if command -v tailscale >/dev/null 2>&1; then
  timeout 20s tailscale logout || true
fi
systemctl disable --now tailscaled 2>/dev/null || true
apt-get purge -y tailscale tailscale-archive-keyring
rm -f /etc/apt/sources.list.d/tailscale.list
rm -f /usr/share/keyrings/tailscale-archive-keyring.gpg
rm -rf /var/lib/tailscale
systemctl daemon-reload
systemctl is-active ssh
ss -lntp | grep ':22'
)
```

完成后 `command -v tailscale` 不应再返回路径。系统 SSH 应仍为 active，22 端口监听，阿里云免密登录正常。不要执行 `iptables -F`、`ufw reset` 或广泛清理系统软件。

在 Codex 环境设置中移除不再使用的 Tailscale VPN 配置、TCP 目标和 `STOCK_SSH_HOST/STOCK_SSH_PORT` 参数。在 Tailscale 管理后台移除这台服务器的节点；若认证密钥仅为本次接入创建，可撤销该专用密钥。不要撤销其他设备正在使用的密钥。

## 2. 部署代码并验证 HTTPS

先通过正常代码审核及发布流程部署。新接口保持关闭，不需要修改现有业务账户。

服务器执行：

```bash
curl -I --connect-timeout 5 --max-time 15 https://astockreview.cn/
```

必须完成正常证书验证；不要使用 `-k`。若尚无 HTTPS，需要确认域名 DNS 指向服务器、80/443 按现有规则可达，再为现有 Nginx 站点签发证书。Ubuntu/Nginx 可使用 Certbot 的 Nginx 插件，但执行前应先确认实际反向代理类型及配置，避免覆盖现有站点；不默认安装另一套代理。

Nginx 终止 TLS 时，请求必须传到现有本机 Web 上游，并由可信代理覆盖 `X-Forwarded-Proto`：

```nginx
# 放在现有 astockreview.cn 的 HTTPS server 块中；上游端口以实际部署为准。
location ^~ /api/v1/readonly/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header Authorization $http_authorization;
    proxy_read_timeout 15s;
}
```

Uvicorn 只应信任本机反向代理传入的转发头，不要把 `forwarded-allow-ips` 配成任意来源。保持 Web 上游本机监听或现有网络限制，不直接对公网新增 8000 端口。先 `nginx -t`，通过后再 reload；非 Nginx 部署应使用对应代理配置。

无 Token 访问以下地址应返回 JSON 503（尚未启用）或 401（已启用）：

```bash
curl --connect-timeout 5 --max-time 15 https://astockreview.cn/api/v1/readonly/health
```

426 表示应用没有识别为 HTTPS，检查可信代理转发配置。接口收到的未认证 HTTP 不返回数据，也不自动转发 Token 到其他地址。

## 3. 生成独立、可撤销的 Token

以实际 Web 服务用户 `deploy` 执行，以保证应用能读取配置。下例使用已确认的服务器路径，Token 默认有效 30 天：

```bash
runuser -u deploy -- env WEB_DATA_DIR=/srv/a-stock/webdata \
  /opt/a_stock_sentiment_system/.venv/bin/python \
  /opt/a_stock_sentiment_system/scripts/configure_readonly_api.py \
  --token-file /tmp/astock-readonly-token.once --days 30
```

脚本将哈希配置原子写入 `/srv/a-stock/webdata/settings/readonly_api.json`，权限 0600。原始 Token 只写入指定的新文件，权限 0600，终端摘要和应用配置均不包含原始 Token。输出文件必须尚不存在。不要把原始 Token 放在命令参数、Git、聊天或普通环境变量截图中。

管理员可在自己的服务器终端打开该一次性文件，将内容直接复制到 Codex 的安全网络密钥设置，然后删除该文件。不要把包含 Token 的终端输出贴回聊天。

## 4. 配置云端 HTTPS 访问

在所用 Codex 云环境中：

- 启用所需的互联网访问，将显式目标限制为 `astockreview.cn`，保留原有包管理器设置。
- 在“网络密钥”安全设置中添加名为 `READONLY_API_TOKEN` 的凭据，值粘贴服务器生成的 Token，目标 HTTPS 域名限定为 `astockreview.cn`。代理凭据占位符仅在授权 HTTPS 请求中替换，不适用于本地原始密钥文件。
- 可选普通变量 `READONLY_API_BASE_URL=https://astockreview.cn`。不需要配置 VPN/TCP/SSH，也不要把 Token 当成普通非秘密变量。
- 保存并按界面要求发布配置，在实际使用的新环境中验证访问。草稿保存本身不等于运行时网络和凭据已生效。

本会话此前的环境草稿返回 `stale_base`，现有工具也不支持删除旧 VPN 项。因此这些界面操作必须在当前有效的环境设置中完成；不重复提交失效草稿，不声称已自动放行域名或注入凭据。

项目自带查询客户端保持继承代理、TLS 校验、禁止自动跟随重定向，也拒绝 HTTP 地址：

```bash
.venv/bin/python scripts/query_readonly_api.py health
.venv/bin/python scripts/query_readonly_api.py dates
.venv/bin/python scripts/query_readonly_api.py quotes --codes 000001,600000
.venv/bin/python scripts/query_readonly_api.py signals --date 20261026 --code 000001
.venv/bin/python scripts/query_readonly_api.py candidates --date 20261023 --limit 20
.venv/bin/python scripts/query_readonly_api.py minutes --date 20261023 --code 000001
```

部署验收必须验证：无 Token 被拒绝、正确 Token 能读取实际状态、POST 被拒绝、过期数据明确标识，并确认接口查询没有启动补数/采集/策略任务。公网尚不可达、证书无效、云端策略未生效或凭据缺失时，都不能宣称“已接通”。

## 限流与撤销

应用每个 worker 同时最多处理 2 个已认证查询，超出返回 429；按来源 IP、每个 Web worker 限制每分钟 60 次，并限制最多 4096 个来源的限流状态。多个 worker 的总限额会相应增加；如需全局限制，可在现有 Nginx 中增加共享限流区，不把应用内限流描述成集群全局配额。原始 HTTP access log 不应记录 Authorization 头。

更换 Token 使用 `--rotate` 和一个新的 `--token-file`；旧 Token 立即失效，不需要重启服务。撤销全部集成访问：

```bash
runuser -u deploy -- env WEB_DATA_DIR=/srv/a-stock/webdata \
  /opt/a_stock_sentiment_system/.venv/bin/python \
  /opt/a_stock_sentiment_system/scripts/configure_readonly_api.py --disable
```

业务缓存、交易记录和浏览器登录账户不受影响。后续新增查询应沿用固定白名单和有界读取，不添加任意 SQL、文件下载或 Shell 执行能力。


## 开发验证记录

新增接口通过 HTTPS 认证隔离、方法限制、过期/撤销、字段白名单、路径和大小限制、共享缓存只读、CSV/GZIP 读取、陈旧信号降级及现有认证回归检查。Token 管理脚本不把原始凭据写到配置或标准输出，查询客户端拒绝 HTTP 和重定向。

补充检查中，旧 `test_mobile_api_phase1.py` 的两个测试在本次修改前的代码目录同样失败：其 `_endpoint` 直接遍历 `router.routes` 并访问 `.path`，而当前 FastAPI 返回 `_IncludedRouter`。这是旧测试辅助函数的兼容问题，未通过改写旧测试掩盖；真实 HTTP 认证与权限回归独立验证。生产 HTTPS、代理配置和真实数据读取仍须服务器部署后验收。
