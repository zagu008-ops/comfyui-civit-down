# ComfyUI Civit Down

运行在 **ComfyUI 电脑**上的下载插件，供配对的酒馆调用。支持读取 Civitai 模型资料、网页账号授权、后台下载 LoRA、SHA256 校验和下载状态查询。当前版本为 `0.1.0` 链路测试版。

它是后台扩展，不新增工作流节点。安装后由 ComfyUI 自动注册 `/cosji-civit/` 接口。无需额外 pip 安装：使用 ComfyUI 已有的 `aiohttp`。

## 安装

在目标电脑的 ComfyUI 目录执行：

```bash
git clone https://github.com/zagu008-ops/comfyui-civit-down.git custom_nodes/comfyui-civit-down
```

使用该 ComfyUI 的 Python 运行初始化脚本。将下面示例中的酒馆网址和 LoRA 路径替换为实际配置：

```bash
python custom_nodes/comfyui-civit-down/setup_config.py --origin http://192.168.1.10:8000 --lora-root /absolute/path/to/models/loras
```

Windows 路径示例：

```powershell
python custom_nodes/comfyui-civit-down/setup_config.py --origin http://192.168.1.10:8000 --lora-root "D:\ComfyUI\models\loras"
```

可以重复传入 `--origin` 配对多个酒馆来源。LoRA 根目录必须是目标 ComfyUI 已配置的目录，包括 `extra_model_paths.yaml` 中的目录；助手在每次请求时核验。初始化会生成随机配对密钥，现有 Civitai 账号授权会保留。不要直接使用示例配置里的占位密钥。

重启 ComfyUI，并配置它监听自己的局域网 IP。例如：

```bash
python main.py --listen 127.0.0.1,192.168.1.20 --enable-cors-header http://192.168.1.10:8000
```

ComfyUI 桌面版可在启动参数中设置相同参数。IP 改变时更新监听参数及酒馆保存的目标地址。允许多个来源的助手配置不会自动修改 ComfyUI 的 CORS 设置；浏览器调用还需匹配 ComfyUI 的 CORS 配置。

**升级或迁移：同一个 ComfyUI 中只能安装一份本插件。** 如果之前安装过 `cosji-civit-download-helper`，停止 ComfyUI，把其中的 `config.json` 移到本目录，并将旧目录移出 `custom_nodes`，再启动，避免重复注册接口。

## 酒馆使用

需要支持这些接口的酒馆插件版本。在“角色 / 服装”的 Civit 链接旁点击“下载 LoRA 到目标 ComfyUI”：

1. 在本机查看 `config.json` 的 `pairing_key`，填写到下载窗口；仅在页面内存暂存，刷新后重新填写。
2. 点击“通过 Civitai 网页授权”，在官方网页登录并确认，凭据自动保存到目标电脑。
3. 读取模型资料，确认模型版本、基础模型、文件大小和保存子文件夹。
4. 提交下载，等待目标电脑完成校验。返回的相对路径可用于 ComfyUI 的 LoRA 选择。

默认子目录为 `Civit测试`；也可填写 `恋上换装娃娃` 等作品名。此版本不自动识别角色/作品、不创建服装、不自动判定基础模型兼容性。

## 账号授权

仓库内置公开的本插件 OAuth Client ID：`c62bd385-135e-49cb-a132-a91f8e2a8ba5`。示例配置和初始化脚本提供此值。用户仍须亲自批准账号授权；Client ID 不包含作者或任何用户的账号授权。

设备授权使用 `https://auth.civitai.com`，无需公网回调地址。请求权限为 `UserRead + ModelsRead`，用于身份识别和模型下载；UserRead 是 Civitai 的基本必选权限。参考 [官方 OAuth 文档](https://github.com/civitai/civitai/blob/main/docs/auth/oauth-developer-docs.md)。如果应用尚未获准使用设备授权，接口会返回明确错误，管理员需在 Civitai 完成应用设置；当前尚未完成真实用户批准后的下载验证。

授权完成后，服务端保存访问凭据和刷新凭据，过期时自动刷新。也支持 `/token` 接口手动配置个人 Token，作为网页授权不可用时的备用。自行注册的应用可用 `--client-id` 覆盖默认 ID；必须支持 Public 设备授权及上述权限。

## 数据放在哪里

| 数据 | 保存位置 |
| --- | --- |
| OAuth Client ID | GitHub 示例配置，可公开 |
| 配对密钥、Civitai Token、刷新凭据 | ComfyUI 电脑的本地 `config.json` |
| LoRA 文件 | ComfyUI 已配置的 LoRA 根目录/作品子目录 |
| 链接、角色、服装、Trigger Words、预览关联 | 酒馆的预设配置，由酒馆插件管理 |
| 当前下载进度与任务状态 | ComfyUI 服务内存；重启后清空 |

`config.json` 已加入 `.gitignore`。它包含凭据，不要提交、分享或加入普通预设导出。初始化在支持 POSIX 权限的平台设置为当前用户读写；Windows 上应额外通过 NTFS 权限限制读取。复制到新电脑时建议重新生成配对密钥、重新授权；不要把原电脑的私密配置当成安装模板。

当前局域网 HTTP 会在网络上传输配对凭据，跨公网使用需要 HTTPS。不要将这些接口未经保护地暴露到公网。

## 接口

所有接口要求 `Authorization: Bearer <pairing_key>`；浏览器的 Origin 还必须在本机配置的来源列表中。所有 JSON 响应均不会返回 Token 原文。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/cosji-civit/inspect` | `{url}`，读取模型及可下载版本 |
| POST | `/cosji-civit/download` | `{url, versionId, fileId, folder}`，提交任务 |
| GET | `/cosji-civit/jobs/{id}` | 查询下载任务 |
| POST | `/cosji-civit/oauth/start` | 开始网页设备授权 |
| GET | `/cosji-civit/oauth/status/{id}` | 查询授权状态 |
| POST | `/cosji-civit/token` | `{token}`，配置备用 Token |

## 限制与验证

- 只支持有 SHA256 的主 `.safetensors` LoRA 文件，单文件最多 2 GiB，每次一个下载任务。
- 只允许单层子文件夹，不接受绝对路径、路径穿越或指向外部的符号链接。
- 相同文件复用；同名不同内容以哈希后缀区分，不覆盖已有文件。
- 临时文件校验通过后才发布为模型文件；失败清理临时文件。
- Civitai 认证头只发送给 Civitai 域名，不发送给下载存储域名。
- 手机关闭页面后下载继续；ComfyUI 重启会丢失任务状态。目前无断点续传、任务取消和自动重试。
- 下载跳转限于 Civitai、R2 与指定 Civitai 存储域名，遇到其他域名会停止。

测试命令（使用 ComfyUI 环境的 Python）：

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

测试覆盖下载与复用、哈希失败清理、不覆盖冲突文件、目录逃逸、认证和来源限制，以及 OAuth 等待、拒绝、限流和凭据刷新。文件传输及 OAuth 成功路径使用测试响应；不代表已验证真实账号授权和实际模型下载。
