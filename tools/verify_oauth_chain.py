"""Local OAuth verification command using the same callback as the plugin."""
import argparse
import sys
from pathlib import Path
from aiohttp import web
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_oauth import create_authorization_app
import local_oauth
# Expose the module for transport fixtures in the callback tests.
aiohttp = local_oauth.aiohttp
create_probe = create_authorization_app


def main():
    parser = argparse.ArgumentParser(description='本机 OAuth + PKCE 真实链路验证，仅用于阶段 1')
    parser.add_argument('--config', type=Path, required=True, help='目标 ComfyUI 助手私密 config.json 的路径')
    parser.add_argument('--port', type=int, default=18188)
    parser.add_argument('--client-id', default='', help='显式使用已注册的公开 Client ID，不依赖验证期间其他配置变更')
    args = parser.parse_args()
    app = create_probe(args.config, args.port, args.client_id)
    print(f'请在 Civitai 应用注册回调：http://127.0.0.1:{args.port}/oauth/callback')
    print(f'在此电脑浏览器打开：http://127.0.0.1:{args.port}/start')
    print('请在官方页面自行确认授权；不打印账号凭据。验证会话有效期 15 分钟。')
    # Disable access logging: callback query contains a one-time authorization code.
    web.run_app(app, host='127.0.0.1', port=args.port, access_log=None)


if __name__ == '__main__':
    main()
