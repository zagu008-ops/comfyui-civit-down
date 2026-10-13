# 真实链路验证记录

日期：2026-10-13。最新结论：**本地关键链路已通过；浏览器实际交互、手机 Wi-Fi 与跨电脑安装保留为后续验收。** 下方保留早期检查与修复过程。

| 检查项 | 结果 | 证据/限制 |
| --- | --- | --- |
| 自动化回归 | 通过 | `python -m unittest discover -s tests -p 'test_*.py'`，19 项通过，包含 scope 格式、PKCE 回调及存储域名边界 |
| ComfyUI 监听 | 通过 | TCP `127.0.0.1:8188` 与 `172.16.16.81:8188` |
| Client ID 配置 | 已修复 | 运行配置此前缺少 ID，恢复用户已提供的公开 ID |
| 官方 discovery | 通过 | issuer `https://auth.civitai.com`，包含设备授权端点与 grant type |
| 官方设备授权 | 失败 | `/api/auth/oauth/device` 对当前 Client ID 返回 `unauthorized_client` |
| 用户批准及 Token 保存 | 通过 | 改用本机 PKCE，用户在 Chrome 批准；只核查凭据存在状态 |
| 真实模型下载、SHA256 校验 | 通过 | 228471644 字节，独立哈希与官方一致 |
| ComfyUI 列表与酒馆代理读取 | 通过 | 原生列表与本地酒馆实际代理均包含相对路径；尚未作浏览器 UI 验收 |
| 手机当前网址与跨设备访问 | 待验证 | 现有 Origin 仍是历史酒馆地址，需要核实实际地址 |

## 当前请求

目标为官方授权 hub 的设备授权接口，表单参数：

```text
client_id=c62bd385-135e-49cb-a132-a91f8e2a8ba5
scope=5
```

助手对外返回 HTTP 400，并显示 `Civitai 未允许此应用启动设备授权：unauthorized_client`。这里的 Client ID 是公开标识，记录中不包含账号凭据。

官方 discovery 支持设备授权，仅证明服务提供该能力，不证明这个已注册应用可以使用它。需要核实应用类型、允许的 grant 与线上注册状态；不能把错误归因于 Token、模型文件或局域网下载目录。

## 官方实现核实

已读取官方仓库当前实现：

- [设备授权接口](https://github.com/civitai/civitai/blob/main/apps/auth/src/routes/api/auth/oauth/device/%2Bserver.ts) 仅当客户端 grants 含 `urn:ietf:params:oauth:grant-type:device_code` 时允许设备授权；否则准确返回 `unauthorized_client`。
- [数据库定义](https://github.com/civitai/civitai/blob/main/packages/civitai-db-schema/prisma/schema.full.prisma) 的普通应用 grants 默认仅为 `authorization_code` 与 `refresh_token`。
- [普通注册/更新 schema](https://github.com/civitai/civitai/blob/main/src/server/schema/oauth-client.schema.ts) 不接受 grants 字段；普通用户页面无法直接开启设备授权。
- [公开客户端 Token 交换实现](https://github.com/civitai/civitai/blob/main/apps/auth/src/lib/server/oauth/model.ts) 允许原生客户端不发送 Origin 的 PKCE 交换；如果发送 Origin 则必须匹配登记来源。

因此此前“注册一个 Public 应用即可设备授权”的假设不成立。不能继续要求用户找不存在的设备授权开关。应验证标准授权码 + PKCE；如要继续设备授权，需要由 Civitai 管理方授予该 grant。

## 已准备的本机验证工具

`tools/verify_oauth_chain.py` 只监听本机 `127.0.0.1`，使用强随机 state 与 S256 PKCE，验证 state、会话有效期、拒绝与重复回调，不记录回调查询日志。授权成功后才将凭据写到目标助手的私密配置，保留已有配对设置。

需在同一个 OAuth 应用追加实际测试回调：`http://127.0.0.1:18188/oauth/callback`。这是本机电脑浏览器的验证方式，不是手机登录方案；手机的 127.0.0.1 指向手机自己。手机与多目标电脑的正式回调策略应在阶段 1 完成后设计，不能假定这个工具解决了它们。

## 下一步

1. 用户在应用注册设置中追加上述本机回调，并保持应用类型为 Public、权限为 UserRead + ModelsRead。
2. 在 ComfyUI 这台电脑的浏览器打开验证工具的 `/start`，由用户亲自登录并批准。
3. 工具完成授权交换，随后继续真实模型下载、哈希和下游验证。
4. 核实当前手机酒馆网址，验证真实跨设备访问。
5. 阶段 1 全部通过后再进入正式设计；届时必须解决手机与多目标电脑的授权，不直接将本机工具作为正式版发布。

## 本机回调后续验证

用户已追加真实回调并返回本机。旧验证会话在 Token 交换/保存阶段失败，旧工具未保留异常类型，不能确定旧故障的具体原因；配置中未保存访问或刷新凭据。

已增加不含敏感数据的阶段及异常类型诊断，以及凭据保存成功与网络失败分类的模拟测试。重启后，从实际验证服务内部用无效测试码访问官方 Token 端点，确认接口可达并返回 HTTP 400。新的会话处于 pending，等待用户重新确认。真实授权链路仍未标记通过。

## scope 返回格式修复

新的失败记录准确定位到 token_validation / TypeError。官方 Token 实现直接返回 OAuth 库的 token.scope，其值可为数组；此前 int(scope) 不支持数组。现已共用 scope_bitmask 解析器，接受数值、十进制字符串和数组，并拒绝空值、布尔值及非数字格式。授权成功保存与刷新测试均改为数组响应并通过。验证服务已重启；等待用户再次批准，尚未将真实链路标记通过。

## 本地真实下载链路通过

用户随后在 Chrome 完成 PKCE 授权，目标配置已保存访问和刷新凭据。下载首次因官方跳转主机 `b2.civitai.com` 未列入允许列表而停止，现已精确添加该主机；账号认证头仍只发给 Civitai API 主域。

重试完成 228471644 字节下载。独立计算 SHA256 与官方 `f02ab96b677b22d62961ff51a4badb4da8f451dd934a1811c95ac910c55dd881` 一致。局域网 ComfyUI LoraLoader 列表发现 `恋上换装娃娃/sajuna-inui-s1-illustriousxl-lora-nochekaiser.safetensors`。本地酒馆实际 CSRF 保护的 `/api/sd/comfy/object-info` 代理返回 HTTP 200，包含该文件。重复提交返回 completed/reused=true/received=0（当前兼容接口的 completed 名称为 done），无遗留临时文件。

用户选择先用本地酒馆完成验证；手机 Wi-Fi 改列为发布前检查。已进入正式设计草案阶段，正式账号授权方式等待选择。Chrome 浏览器连接器不可用，浏览器交互尚未实测，未声称按钮端到端验收完成。

## 正式流程实现与自动化验收（2026-10-13）

用户已选择“每台 ComfyUI 首次本机浏览器授权，手机复用目标授权”。实现版本为 1.0.0-beta.1，包括本机配置/PKCE、SQLite 任务、取消重试、酒馆原生秘密存储代理、公开资料识别、作品查询、统一确认及下载后导入保存校验。

- Python 28 项测试通过：OAuth 状态/范围、刷新串行、文件哈希/覆盖/跳转/路径、原子预约/重启、取消清理、活动任务重试、重复任务进度、本机管理来源与格式限制。
- 酒馆 28 组测试通过（包含新角色/服装导入事务回归）。
- Node 代理测试通过：用户目标隔离、密钥不进连接记录/响应、操作白名单、不跟随携带凭据的跳转、待导入记录无凭据、落盘验证与预览来源限制。
- 尚未部署到运行环境；自动审批拒绝启用酒馆服务端插件和重启范围，需要用户明确确认该项操作。配置备份与部署脚本已准备，但未执行。
- 本记录不代表新界面实际交互或手机/另一台电脑安装已经通过。
