# 只读 HTTPS 环境接续说明（不含 Token）

目标：让 Codex 按需读取 https://astockreview.cn/api/v1/readonly 的市场数据，不使用 SSH/Tailscale。

## 当前进度
- 服务器项目：/opt/a_stock_sentiment_system；服务用户 deploy；WEB_DATA_DIR=/srv/a-stock/webdata。
- 只读接口已部署 main 提交 adfa1bd237fccc2fff5fcdb99c89cb67a231b34c，70 项相关测试通过。
- 用户已验证 HTTPS 正常、证书续期 dry-run 成功。
- 用户已验证未启用时接口返回 503 readonly_api_disabled；之后已进行 Token 配置操作，当前是否启用须实测，不能假设。
- 用户已验证 tailscale 命令无输出，tailscaled inactive；不要重新安装或配置 VPN。
- 当前编辑页反复提示“环境已更改”；此会话配置保存曾明确返回 stale_base。平台指出同一草稿重新读取和提交不能修复，需要从当前环境设置开启新的设置会话。
- 最新截图明确说明部分密钥/变量值已保存，舍弃草稿不会影响已保存值。READONLY_API_TOKEN 在界面中显示为掩码，不要要求用户把值贴到聊天里，不要因旧任务看不到凭据就要求重新生成。
- 旧运行实例仍为 spec_revision=1，自定义 HTTP 域名为空、无凭据就绪证据；这不代表用户刚保存的安全值不存在。

## 新设置会话应执行
1. 读取 cloud-environment-onboarding:setup 和 cloud-environment-runtime，以及新会话绑定的最新配置草稿；确认基于最新版本，不能复用旧 draft_id/base_version。
2. 检查现有 READONLY_API_TOKEN 的绑定、来源和目标，只读元数据，不输出值。优先复用已保存值，避免重复声明或更改 source 引起冲突。
3. 参考 proposed-config.json：把 astockreview.cn 合并到最新自定义域名列表，保留其他域名及包管理器预设。该 JSON 的 allowed_domains 是模板，不能在未知最新列表时直接替换。若凭据目标已正确，保持现有绑定；缺失时再声明。不要改安装脚本、仓库成员或其他无关设置。
4. 如果现有工具不支持移除旧 VPN / STOCK_SSH_*，在设置界面移除，不通过本地网络策略文件或绕过代理改变权限。
5. 保存新草稿，按产品流程发布并使用更新后的环境实例。发布前的旧实例看不到新值不能作为失败证据。
6. 当前代码 main 已含 scripts/query_readonly_api.py。验证 READONLY_API_TOKEN 变量是否存在时只检查名称/就绪状态，不打印其值；它可能是由代理替换的占位符，不能写到本地文件或当成原始凭据。
7. 从实际检出的项目路径运行查询客户端 health、dates 和一个有界行情查询。保留继承代理和 CA 信任，不关闭 TLS 验证，不跟随重定向。
8. 验证实际 HTTP 成功和响应 read_only=true 后，才称连接成功。记录 as_of、数据源时间、缺失和 stale 状态；不触发补数、策略执行或写入。

## 客户端示例

```bash
.venv/bin/python scripts/query_readonly_api.py health
.venv/bin/python scripts/query_readonly_api.py dates
.venv/bin/python scripts/query_readonly_api.py quotes --codes 000001,600000
```

READONLY_API_TOKEN 只能通过云环境安全凭据设置输入。不要将 Token、私钥、.env、数据库或完整环境变量输出放入附件/日志/Git。

若确需轮换 Token，服务器已有 scripts/configure_readonly_api.py，必须先确认原 Token 失效或用户明确要求；--rotate 使用新的 --token-file，原始值只在用户自己的终端复制到安全设置。

## 附件内容
- proposed-config.json：无秘密的网络/凭据声明模板，须与最新配置合并。
- query_readonly_api.py：无秘密的 HTTPS 客户端。
- server-deployment.md：已部署接口的完整使用说明。
- reference-install-script.sh / reference-start-skill.md：旧草稿原有说明，仅供保全和比较，不能覆盖较新的设置或视为新实例已验证。

