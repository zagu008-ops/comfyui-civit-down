import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode
import aiohttp
from aiohttp import web
OAUTH_OUTCOME = web.AppKey('civit_oauth_outcome', dict)
if __package__:
    from .civit_oauth_format import scope_bitmask
else:
    from civit_oauth_format import scope_bitmask


def create_authorization_app(config_path, port, explicit_client_id=''):
    cfg = json.loads(config_path.read_text(encoding='utf-8'))
    client_id = explicit_client_id or cfg.get('oauth_client_id', '')
    if not client_id:
        raise ValueError('配置中缺少 oauth_client_id')
    callback = f'http://127.0.0.1:{port}/oauth/callback'
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(32)
    outcome = {'state': 'pending'}
    expires = time.monotonic() + 900
    exchange_lock = asyncio.Lock()
    authorize_url = 'https://auth.civitai.com/api/auth/oauth/authorize?' + urlencode({
        'client_id': client_id, 'redirect_uri': callback, 'response_type': 'code',
        'scope': '5', 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})
    app = web.Application()

    async def home(request):
        return web.Response(text='请先在 Civitai 应用中注册回调地址 ' + callback + '，然后访问 /start 开始授权。', content_type='text/plain', charset='utf-8', headers={'Cache-Control': 'no-store'})

    async def start(request):
        if outcome['state'] != 'pending' or time.monotonic() >= expires:
            return web.Response(status=410, text='验证会话已结束，请重新启动验证工具')
        raise web.HTTPFound(authorize_url)

    async def status(request):
        # Only state is exposed; no code, PKCE verifier, account data or tokens.
        return web.json_response(outcome, headers={'Cache-Control': 'no-store'})

    async def finish(request):
        provided = request.query.get('state', '')
        if not hmac.compare_digest(provided, state) or time.monotonic() >= expires:
            return web.Response(status=400, text='授权 state 无效或会话已过期')
        async with exchange_lock:
            if outcome['state'] != 'pending':
                return web.Response(status=409, text='此验证会话已经处理，不重复交换凭据')
            if request.query.get('error'):
                outcome.update(state='failed', error='用户拒绝或平台未允许授权')
                return web.Response(status=400, text=outcome['error'])
            code = request.query.get('code', '')
            if not code:
                return web.Response(status=400, text='回调缺少授权码')
            outcome['state'] = 'exchanging'
            stage = 'token_exchange'
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40), trust_env=True) as session:
                    async with session.post('https://auth.civitai.com/api/auth/oauth/token', data={
                        'grant_type': 'authorization_code', 'code': code, 'redirect_uri': callback,
                        'client_id': client_id, 'code_verifier': verifier}, allow_redirects=False) as response:
                        if response.status != 200:
                            raise ValueError('授权交换失败（HTTP ' + str(response.status) + '）')
                        try:
                            token = await response.json()
                        except (aiohttp.ContentTypeError, json.JSONDecodeError):
                            raise ValueError('授权服务未返回 JSON 凭据，请重新开始授权') from None
                stage = 'token_validation'
                if not token.get('access_token') or not token.get('refresh_token') or not scope_bitmask(token.get('scope', '0')) & 4:
                    raise ValueError('授权未包含模型下载权限或有效刷新凭据')
                stage = 'credential_save'
                current = json.loads(config_path.read_text(encoding='utf-8'))
                if current.get('oauth_client_id') not in (None, '', client_id):
                    raise ValueError('配置中的 OAuth 应用已变化，未保存凭据')
                current.update(oauth_client_id=client_id, civitai_token=token['access_token'], oauth_refresh_token=token['refresh_token'],
                               oauth_expires_at=time.time() + int(token.get('expires_in', 3600)))
                temp = config_path.with_suffix('.json.tmp')
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(current, stream, ensure_ascii=False, indent=2)
                os.replace(temp, config_path)
                outcome.update(state='done')
                return web.Response(text='授权验证成功。凭据已保存在 ComfyUI 电脑，可关闭此页，继续验证真实模型下载。', content_type='text/plain', charset='utf-8', headers={'Cache-Control': 'no-store'})
            except Exception as exc:
                labels = {'token_exchange': '授权交换连接失败', 'token_validation': '授权返回值校验失败', 'credential_save': '本机凭据保存失败'}
                message = str(exc) if isinstance(exc, ValueError) else labels[stage] + '（' + type(exc).__name__ + '）'
                outcome.update(state='failed', stage=stage, error=message)
                return web.Response(status=400, text=outcome['error'])

    async def connectivity(request):
        # No real code/token is used; exercises the running process's network permissions.
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=True) as session:
                async with session.post('https://auth.civitai.com/api/auth/oauth/token', data={
                    'grant_type': 'authorization_code', 'client_id': client_id,
                    'code': 'invalid-connectivity-probe', 'code_verifier': 'x' * 64,
                    'redirect_uri': callback}, allow_redirects=False) as response:
                    return web.json_response({'reachable': True, 'http': response.status})
        except Exception as exc:
            return web.json_response({'reachable': False, 'exception': type(exc).__name__})

    app.router.add_get('/', home)
    app.router.add_get('/start', start)
    app.router.add_get('/status', status)
    app.router.add_get('/oauth/callback', finish)
    app.router.add_get('/connectivity', connectivity)
    app[OAUTH_OUTCOME] = outcome
    return app
