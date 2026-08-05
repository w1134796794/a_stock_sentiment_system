# 微信小程序 API 契约

本文档对应 Phase 0-1，接口基址为 `/api/v1/mobile`。这些接口同时可供未来小程序和其他只读客户端使用。

## 1. 当前边界

- 当前阶段复用 Web 的数据库用户、订阅有效期、会话和角色权限。
- 微信 `wx.login`、OpenID 绑定和移动 Token 属于 Phase 2，本阶段不实现。
- 所有接口只读取 `WEB_DATA_DIR` 产物、DuckDB 和服务端实时缓存。
- 请求不会调用 Tushare、adata、easyquotation、pqquotation 或其他行情源。
- 取数、因子、选股、回测、配置和用户管理 API 仍为管理员专属。

## 2. 统一响应

成功：

```json
{
  "ok": true,
  "data": {},
  "meta": {
    "trade_date": "20260727",
    "generated_at": "2026-07-27T18:35:20",
    "source_status": "ready",
    "is_realtime": false,
    "cache_age_seconds": null,
    "request_id": "f64f..."
  },
  "error": null
}
```

失败：

```json
{
  "ok": false,
  "data": null,
  "meta": {
    "trade_date": "",
    "generated_at": "2026-07-30T09:00:00",
    "source_status": "error",
    "is_realtime": false,
    "cache_age_seconds": null,
    "request_id": "fb83..."
  },
  "error": {
    "code": "NOT_AUTHENTICATED",
    "message": "请先登录",
    "details": null
  }
}
```

固定错误码：

| HTTP | 错误码 | 含义 |
| --- | --- | --- |
| 401 | `NOT_AUTHENTICATED` | 未登录 |
| 401 | `SESSION_REVOKED` / `SESSION_EXPIRED` | 会话被踢下线或过期 |
| 403 | `SUBSCRIPTION_EXPIRED` | 服务订阅已到期 |
| 403 | `PERMISSION_DENIED` | 当前角色无对应功能权限 |
| 404 | `CANDIDATE_NOT_FOUND` / `STOCK_NOT_FOUND` | 对象不存在 |
| 422 | `VALIDATION_ERROR` | 参数格式或范围错误 |
| 429 | `RATE_LIMITED` | 请求频率过高 |
| 500 | `SERVER_ERROR` | 未处理的服务端异常 |

移动认证补充错误码：

| HTTP | 错误码 | 含义 |
| --- | --- | --- |
| 400 | `WECHAT_NOT_CONFIGURED` | 服务端未配置微信 AppID/AppSecret |
| 400 | `WECHAT_CODE_REJECTED` | `js_code` 无效或已使用 |
| 401 | `ACCESS_TOKEN_EXPIRED` | Access Token 已过期，应尝试刷新 |
| 401 | `REFRESH_TOKEN_INVALID` / `REFRESH_TOKEN_EXPIRED` | 需要重新微信登录 |
| 401 | `MOBILE_SESSION_REVOKED` | 会话被后台或新设备踢下线 |
| 403 | `USER_DISABLED` | 账号已禁用 |
| 409 | `SESSION_LIMIT_REACHED` | 在线会话达到上限且策略为拒绝新设备 |
| 409 | `WECHAT_ALREADY_BOUND` / `ACCOUNT_ALREADY_BOUND` | 微信或账号已存在其他绑定 |

## 3. 接口清单

| 方法 | 路径 | 权限映射 | 数据来源 |
| --- | --- | --- | --- |
| GET | `/bootstrap` | 概览 | 用户会话、本地交易日、市场快照 |
| GET | `/dashboard?date=` | 概览 | 决策池、市场因子 |
| GET | `/candidates?date=&group=&limit=&offset=` | 候选股 | 决策池 |
| GET | `/candidates/{code}?date=` | 候选股 | 决策池 |
| GET | `/realtime?candidate_date=&market_date=&limit=` | 实时行情 | 服务端共享实时缓存 |
| GET | `/leaders?date=&lookback=&limit=` | 龙头池 | 龙头池本地产物 |
| GET | `/limitup?date=` | 涨停数据 | 涨停/跌停 Silver 表 |
| GET | `/lhb?date=` | 龙虎榜 | 龙虎榜本地产物 |
| GET | `/stocks/{code}?date=` | 候选股 | 个股因子、日线、决策池 |
| GET | `/stocks/{code}/daily?date=&limit=` | 候选股 | 日线 Silver 表 |

### Phase 2 认证接口

认证接口完整前缀仍为 `/api/v1/mobile`。

| 方法 | 路径 | 是否需要 Access Token | 说明 |
| --- | --- | --- | --- |
| POST | `/auth/wechat` | 否 | 使用 `wx.login` 的 `js_code` 登录 |
| POST | `/auth/bind` | 否 | 首次使用现有账号密码绑定微信 |
| POST | `/auth/refresh` | 否 | 轮换 Access/Refresh Token |
| POST | `/auth/logout` | 是 | 撤销当前移动会话 |
| GET | `/auth/profile` | 是 | 返回当前用户、订阅和设备会话摘要 |

`POST /auth/wechat`：

```json
{
  "js_code": "wx.login返回的临时凭证",
  "device_id": "客户端生成并持久化的设备ID",
  "device_name": "iPhone",
  "platform": "wechat_miniprogram"
}
```

未绑定响应的 `data`：

```json
{
  "status": "binding_required",
  "binding_ticket": "一次性绑定凭证",
  "binding_ticket_expires_in": 600
}
```

`POST /auth/bind`：

```json
{
  "binding_ticket": "上一步返回的凭证",
  "username": "现有系统账号",
  "password": "现有系统密码",
  "device_id": "与登录请求一致",
  "device_name": "iPhone",
  "platform": "wechat_miniprogram"
}
```

登录或绑定成功的 `data`：

```json
{
  "status": "authenticated",
  "user": {
    "id": 2,
    "username": "viewer01",
    "display_name": "用户",
    "role": "viewer",
    "expire_at": "2026-08-30",
    "max_sessions": 1
  },
  "access_token": "仅本次响应返回",
  "refresh_token": "仅本次响应返回",
  "token_type": "Bearer",
  "expires_in": 900,
  "refresh_expires_in": 2592000
}
```

刷新请求：

```json
{
  "refresh_token": "当前Refresh Token"
}
```

刷新成功后客户端必须原子替换本地 Access/Refresh Token，不得继续使用旧令牌。

日期格式统一为 `YYYYMMDD`。未传日期时读取最新可用交易日，不以系统自然日冒充交易日。

## 4. 关键 DTO

### 候选摘要

```text
code, name, action_group
hit_strategies, strategy_consensus, strategy_total
mainline, related_themes, sector_strength
entry_mode, conclusion, confirmation, invalidation
position, position_cap_pct
confidence_grade
expected_return_pct, expected_excess_return_pct
```

### 市场摘要

```text
regime, regime_label, emotion_phase
market_score, limit_up_count, limit_down_count
broken_rate, amount_yuan, position_scale, risk_flags
```

### 实时结果

实时接口只返回缓存快照。`source_status` 可能为：

- `cached`：已有缓存。
- `cache_empty`：尚未生成实时快照。
- `cache_unavailable`：当前进程未接入共享缓存。

`meta.cache_age_seconds` 表示缓存年龄。客户端不得因缓存为空而自行请求第三方行情源。

## 5. OpenAPI

开发环境登录管理员账号后访问：

```text
http://127.0.0.1:8000/api/docs
```

筛选 `mobile` 标签即可查看 Phase 0-1 的完整请求参数和响应 DTO。
