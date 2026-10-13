# ComfyUI Civit Down

在 **ComfyUI 电脑**上执行 Civitai 网页授权、LoRA 后台下载和 SHA256 校验。配合 Cosji 酒馆插件，将角色、作品、Trigger Words 与服装预设关联。当前为 `1.0.0-beta.1`，正式流程实现版；浏览器、手机和另一台电脑的安装验收须按 `DEVELOPMENT_PLAN.md` 完成后再发布稳定版。

本插件是 HTTP 后台扩展，不新增工作流节点。依赖 ComfyUI 已有的 `aiohttp`。

## 安装与首次授权

在目标电脑的 ComfyUI 目录执行：

```bash
git clone https://github.com/zagu008-ops/comfyui-civit-down.git custom_nodes/comfyui-civit-down
```

重启 ComfyUI，在 **这台电脑的本机浏览器**打开：

```text
http://127.0.0.1:8188/cosji-civit/local
```

页面自动生成随机配对密钥；选择 ComfyUI 已配置的 LoRA 根目录，保存配置，点击连接 Civitai。在官方页面登录并批准，凭据留在这台电脑。仓库内置公开 Client ID `c62bd385-135e-49cb-a132-a91f8e2a8ba5`，它不包含任何账号权限。

正式版采用授权码 + PKCE，回调监听随机本机端口、路径 `/oauth/callback`。自建 Public 应用需要登记 `http://127.0.0.1:18188/oauth/callback`；Civitai 的 loopback 匹配允许端口变化。请求权限为 UserRead + ModelsRead（scope 5）。普通注册应用不支持设备授权，不应把手机上的 localhost 当作 ComfyUI 电脑。

手机和其他酒馆随后复用该电脑已保存的授权。更换 ComfyUI 电脑，需要在新电脑安装、授权并配对；不用复制私密配置。授权失效时在本机页面重新连接。OAuth 凭据过期会串行刷新。

命令行初始化仍可使用：

```bash
python custom_nodes/comfyui-civit-down/setup_config.py --lora-root /absolute/path/to/models/loras --origin http://192.168.1.10:8000
```

Windows LoRA 路径可填写 `D:\ComfyUI\models\loras`。同一 ComfyUI 只能安装一份本插件；从旧 `cosji-civit-download-helper` 升级时保留 `config.json` 和 `.state`，将旧副本移出 `custom_nodes` 后再启动。

## 安装酒馆服务端接入

1. 将本仓库的 `sillytavern-server` 文件夹复制为 `SillyTavern/plugins/cosji-civit`。
2. 检查 `plugins` 目录中的其他插件。启用服务端插件会运行该目录中有效插件的代码。
3. 在酒馆 `config.yaml` 设置 `enableServerPlugins: true`，重启酒馆。
4. 更新配套 [Cosji 酒馆插件](https://github.com/zagu008-ops/1008-stchu)。
5. 在“角色 / 服装”填写 Civit 链接，点击“导入角色 / 服装并下载 LoRA”，首次填写目标电脑本机页面显示的配对密钥。

配对密钥存入酒馆服务器原生秘密存储，不进入普通角色/服装导出。浏览器后续只使用连接 ID。服务端代理使用酒馆原有登录与 CSRF，不能代理本机管理接口或任意外部网址。

局域网目标须让 ComfyUI 监听自己的局域网地址，例如：

```bash
python main.py --listen 127.0.0.1,192.168.1.20
```

酒馆服务器必须能访问目标 ComfyUI。下载接入走酒馆服务器代理，不要求手机浏览器直连下载接口；原有生图链路的配置保持按酒馆插件使用。HTTP 局域网配对不能替代公网 HTTPS 与访问控制。

## 使用流程

1. 读取链接指定版本，或选择符合当前工作流模型家族的版本。Pony、Illustrious 和普通 SDXL 分开判断；未知兼容性须人工核对。
2. 用酒馆当前配置的文本 LLM 识别公开标题、描述和 Trigger Words，自动查询作品名称候选。首版只支持同人单角色 LoRA。
3. 统一核对角色目标、作品中文名、每套服装的词组、版本、文件、大小、目录和权重。可纠正名称/词组，选择已有角色或可选预览图。
4. 确认后目标电脑下载。官方 SHA256 核验成功才创建正式角色与服装，保存后还会核对酒馆设置是否落盘。
5. 已有服装跳过，同内容模型文件复用；当前应用的服装不变。
6. 关闭页面后下载继续。重开导入窗口自动恢复未完成任务；失败可重试，任务运行时可取消。ComfyUI 重启将未完成任务标记为中断，允许重新下载。

识别只向配置的文本模型发送公开模型信息和公开作品候选，不发送聊天或角色卡。未配置文本 LLM、错误 JSON、编造触发词或不支持的模型类型会明确停止识别。

## 数据归属

| 数据 | 保存位置 |
| --- | --- |
| 公开 OAuth Client ID | 代码与配置示例 |
| 配对密钥、Civitai 访问/刷新凭据 | ComfyUI 本地 `config.json` |
| 下载任务、状态、文件元数据 | ComfyUI 本地 `.state/jobs.sqlite3` |
| 模型 | ComfyUI 已配置 LoRA 根目录 / 作品名称 / 原始文件名 |
| 配对凭据 | 酒馆服务器原生 `secrets.json`（按用户隔离） |
| 连接记录、待导入计划 | 酒馆用户数据目录 `cosji-civit-connections.json`、`cosji-civit-imports.json` |
| 角色、服装、触发词、LoRA 相对路径 | 酒馆插件设置 |

`config.json`、`.state`、临时模型文件均被 Git 忽略。不要分享私密配置。POSIX 下私密文件权限为 600；Windows 上通过 NTFS 权限限制访问。

## 接口与边界

下载接口前缀 `/cosji-civit/`，需 `Authorization: Bearer <pairing_key>`。带 Origin 的调用须匹配已配置来源，本机同源调用除外。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/capabilities` | 协议/版本、授权状态和目录 |
| POST | `/inspect` | `{url}`，公开模型与版本资料 |
| POST | `/download` | `{url, versionId, fileId, folder}`，预约持久任务 |
| GET | `/jobs`、`/jobs/{id}` | 查询持久状态 |
| POST | `/jobs/{id}/cancel`、`/jobs/{id}/retry` | 取消/重试 |
| GET | `/local`、`/local/config` | 仅本机配置页面及本机配对信息 |
| POST | `/local/config`、`/oauth/start`、`/token` | 仅本机配置/PKCE 授权/备用 Token |

- 主 safetensors 文件、官方 SHA256、最多 2 GiB、一次一个活动下载。
- 只接受单层作品目录，拒绝绝对路径、穿越、符号链接越界、Windows 保留名称。
- 发布模型文件时不覆盖；同名异内容增加哈希后缀。
- 只对 Civitai API 主域发送账号认证，不对 B2/R2 存储发送认证头。
- 限定官方存储跳转；429/5xx 与网络暂时故障最多尝试三次，取消等待临时文件清理。
- 不承诺断点续传。后台完成后酒馆界面必须再次打开才能保存服装；下载服务不会直接修改酒馆预设。

验证命令：

```bash
python -m unittest discover -s tests -p 'test_*.py'
node sillytavern-server/test.cjs
```

真实本地授权、218 MiB 模型下载、官方 SHA256、ComfyUI 列表和酒馆代理读取已验证，证据见 `CHAIN_VALIDATION.md`。安装验收与未完成项见 `DEVELOPMENT_PLAN.md`。OAuth 行为参照 [Civitai 官方源码和文档](https://github.com/civitai/civitai/tree/main/apps/auth)。
