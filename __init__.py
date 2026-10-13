"""Paired Civitai LoRA downloads. Configure config.json locally, never commit it."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urljoin

import aiohttp
from aiohttp import web
import folder_paths
from server import PromptServer

NODE_CLASS_MAPPINGS = {}
CONFIG = Path(__file__).with_name('config.json')
JOBS = {}
TASKS = set()
AUTH = {}
HOSTS = {'civitai.com', 'civitai.red'}
MAX_BYTES = 2 * 1024**3
AUTH_URL = 'https://auth.civitai.com/api/auth/oauth/'


def save_config(cfg):
    temp = CONFIG.with_suffix('.json.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump(cfg, out, ensure_ascii=False, indent=2)
    os.replace(temp, CONFIG)


async def oauth_post(session, endpoint, data):
    async with session.post(AUTH_URL + endpoint, data=data, allow_redirects=False) as response:
        result = await response.json()
        if response.status == 429:
            return {'error': 'rate_limited'}
        if response.status != 200 and not result.get('error'):
            return {'error': 'oauth_http_' + str(response.status)}
        return result


def store_oauth(result):
    if not result.get('access_token') or not result.get('refresh_token'):
        raise ValueError('网页授权未返回有效凭据')
    scopes = int(result.get('scope', '0'))
    if not scopes & 4:
        raise ValueError('授权不包含模型下载权限')
    cfg = config()
    cfg.update(civitai_token=result['access_token'], oauth_refresh_token=result['refresh_token'],
               oauth_expires_at=time.time() + int(result.get('expires_in', 3600)))
    save_config(cfg)
    return cfg


async def fresh_config():
    cfg = config()
    if cfg.get('oauth_refresh_token') and cfg.get('oauth_expires_at', 0) < time.time() + 60:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=True) as session:
            result = await oauth_post(session, 'token', {'grant_type': 'refresh_token',
                                     'refresh_token': cfg['oauth_refresh_token'], 'client_id': cfg.get('oauth_client_id', '')})
        if result.get('error'):
            raise ValueError('Civitai 授权已过期或失效，请重新点击网页授权')
        return store_oauth(result)
    return cfg


async def poll_auth(auth, device):
    interval = max(5, int(device.get('interval', 5)))
    deadline = time.monotonic() + min(int(device.get('expires_in', 900)), 900)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=True) as session:
            while time.monotonic() < deadline:
                await asyncio.sleep(interval)
                result = await oauth_post(session, 'device-token', {'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                                         'device_code': device['device_code'], 'client_id': auth['clientId']})
                error = result.get('error')
                if error == 'authorization_pending':
                    continue
                if error in {'rate_limited', 'slow_down'}:
                    interval += 5
                    continue
                if error:
                    auth.update(state='failed', error='Civitai 授权未完成：' + str(error))
                    return
                store_oauth(result)
                auth['state'] = 'done'
                return
        auth.update(state='failed', error='授权链接已过期，请重新点击网页授权')
    except Exception:
        auth.update(state='failed', error='Civitai 授权连接失败，请重试')


def config():
    if not CONFIG.exists():
        raise ValueError('下载助手尚未配置 config.json')
    return json.loads(CONFIG.read_text(encoding='utf-8'))


def authorize(request):
    cfg = config()
    key = cfg.get('pairing_key', '')
    if not isinstance(key, str) or len(key) < 32 or key.startswith('REPLACE_') or not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + key):
        raise web.HTTPUnauthorized(text='下载助手配对密钥无效')
    origin = request.headers.get('Origin')
    if origin and origin not in cfg.get('allowed_origins', []):
        raise web.HTTPForbidden(text='此酒馆来源未配对')
    root = Path(cfg['lora_root']).resolve()
    if root not in [Path(p).resolve() for p in folder_paths.get_folder_paths('loras')]:
        raise ValueError('保存目录不是 ComfyUI 已配置的 LoRA 根目录')
    return cfg, root


def parse_link(value):
    u = urlsplit(str(value))
    match = re.fullmatch(r'/models/(\d+)(?:/[^/]*)?/?', u.path)
    host = (u.hostname or '').removeprefix('www.')
    if u.scheme != 'https' or host not in HOSTS or u.username or u.password or u.port not in (None, 443) or not match:
        raise ValueError('需要 https 的 civitai.com/red 模型链接')
    return host, match[1]


def safe_part(value):
    if not isinstance(value, str) or not value or len(value) > 160 or value in {'.', '..'} or re.search(r'[\\/:\x00-\x1f]', value):
        raise ValueError('目录或文件名无效；只允许单层子目录')
    return value


async def metadata(session, host, model_id, token):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    async with session.get(f'https://{host}/api/v1/models/{model_id}', headers=headers, allow_redirects=False) as r:
        if r.status != 200:
            raise ValueError(f'Civit 模型资料读取失败（HTTP {r.status}）')
        return await r.json()


def model_files(model):
    if str(model.get('type', '')).upper() != 'LORA':
        raise ValueError('测试按钮仅支持 LoRA')
    result = []
    for version in model.get('modelVersions', []):
        for f in version.get('files', []):
            sha = f.get('hashes', {}).get('SHA256', '')
            if f.get('type') == 'Model' and f.get('primary') and f.get('name', '').lower().endswith('.safetensors') and re.fullmatch('[a-fA-F0-9]{64}', sha):
                result.append({'versionId': version['id'], 'fileId': f['id'], 'versionName': version['name'], 'baseModel': version.get('baseModel', ''), 'name': f['name'], 'sha256': sha.lower(), 'bytes': int(f.get('sizeKB', 0) * 1024)})
    if not result:
        raise ValueError('没有带 SHA256 的主 safetensors LoRA 文件')
    return result


def allowed_download(url):
    u = urlsplit(url)
    host = u.hostname or ''
    return u.scheme == 'https' and not u.username and not u.password and u.port in (None, 443) and (host in HOSTS or host.endswith('.r2.cloudflarestorage.com') or host.endswith('.civitaiusercontent.com'))


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


async def download(job, cfg, root, host, file):
    temp = None
    try:
        folder = root / safe_part(job['folder'])
        if folder.is_symlink() or not folder.resolve().is_relative_to(root):
            raise ValueError('目录超出 LoRA 根目录')
        folder.mkdir(parents=True, exist_ok=True)
        name = safe_part(file['name'])
        dest = folder / name
        if dest.is_symlink():
            raise ValueError('不允许写入符号链接')
        if dest.exists():
            if await asyncio.to_thread(file_hash, dest) == file['sha256']:
                job.update(state='done', path=dest.relative_to(root).as_posix(), reused=True)
                return
            dest = folder / (Path(name).stem + '-' + file['sha256'][:12] + '.safetensors')
            if dest.exists():
                if not dest.is_symlink() and await asyncio.to_thread(file_hash, dest) == file['sha256']:
                    job.update(state='done', path=dest.relative_to(root).as_posix(), reused=True)
                    return
                raise ValueError('目标文件已存在，未覆盖')
        if file['bytes'] <= 0 or file['bytes'] > MAX_BYTES:
            raise ValueError('测试下载限于 2 GiB 以内的文件')
        if shutil.disk_usage(folder).free < file['bytes'] + 128 * 1024**2:
            raise ValueError('目标电脑磁盘空间不足')
        temp = folder / ('.' + job['id'] + '.part')
        url = f'https://{host}/api/download/models/{file["versionId"]}?fileId={file["fileId"]}'
        token = cfg.get('civitai_token', '')
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=90)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            for _ in range(6):
                if not allowed_download(url):
                    raise ValueError('下载跳转到了未允许的域名')
                headers = {'Authorization': 'Bearer ' + token} if urlsplit(url).hostname in HOSTS and token else {}
                async with session.get(url, headers=headers, allow_redirects=False) as r:
                    if r.status in {301, 302, 303, 307, 308}:
                        url = urljoin(url, r.headers.get('Location', ''))
                        continue
                    if r.status in {401, 403}:
                        raise ValueError('Civit 拒绝下载，请在目标电脑配置有效的 Civitai Token（HTTP ' + str(r.status) + '）')
                    if r.status != 200:
                        raise ValueError(f'下载失败（HTTP {r.status}）')
                    job['state'] = 'downloading'
                    h = hashlib.sha256()
                    with temp.open('xb') as out:
                        async for block in r.content.iter_chunked(256 * 1024):
                            job['received'] += len(block)
                            if job['received'] > MAX_BYTES or job['received'] > file['bytes'] + 1024**2:
                                raise ValueError('下载文件超过预期大小')
                            out.write(block)
                            h.update(block)
                    if h.hexdigest() != file['sha256']:
                        raise ValueError('文件 SHA256 校验失败')
                    # Exclusive creation prevents replacing another task's file.
                    os.link(temp, dest)
                    temp.unlink()
                    folder_paths.filename_list_cache.pop('loras', None)
                    job.update(state='done', path=dest.relative_to(root).as_posix(), reused=False)
                    return
            raise ValueError('下载跳转次数过多')
    except asyncio.CancelledError:
        job.update(state='cancelled', error='下载已取消')
    except Exception as exc:
        # Never include signed URLs, request headers or tokens in errors.
        job.update(state='failed', error=str(exc) if isinstance(exc, (ValueError, FileExistsError)) else type(exc).__name__)
    finally:
        if temp and temp.exists():
            temp.unlink()


@PromptServer.instance.routes.post('/cosji-civit/inspect')
async def inspect(request):
    try:
        cfg, root = authorize(request)
        cfg = await fresh_config()
        body = await request.json()
        host, model_id = parse_link(body.get('url', ''))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=True) as session:
            model = await metadata(session, host, model_id, cfg.get('civitai_token', ''))
        return web.json_response({'name': model['name'], 'files': model_files(model), 'root': str(root), 'tokenConfigured': bool(cfg.get('civitai_token')), 'oauthConfigured': bool(cfg.get('oauth_client_id'))})
    except (ValueError, KeyError, TypeError) as e:
        return web.json_response({'error': str(e)}, status=400)
    except aiohttp.ClientError:
        return web.json_response({'error': '目标电脑无法连接 Civit 接口'}, status=502)


@PromptServer.instance.routes.post('/cosji-civit/download')
async def start(request):
    try:
        cfg, root = authorize(request)
        cfg = await fresh_config()
        if any(j['state'] in {'queued', 'downloading'} for j in JOBS.values()):
            raise ValueError('测试版每次只允许一个下载任务')
        body = await request.json()
        host, model_id = parse_link(body.get('url', ''))
        folder = safe_part(body.get('folder', 'Civit测试'))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=True) as session:
            model = await metadata(session, host, model_id, cfg.get('civitai_token', ''))
        file = next((f for f in model_files(model) if str(f['versionId']) == str(body.get('versionId')) and str(f['fileId']) == str(body.get('fileId'))), None)
        if file is None:
            raise ValueError('所选文件不属于此模型版本')
        if any(j['state'] in {'queued', 'downloading'} for j in JOBS.values()):
            raise ValueError('测试版每次只允许一个下载任务')
        job = {'id': uuid.uuid4().hex, 'state': 'queued', 'folder': folder, 'name': file['name'], 'total': file['bytes'], 'received': 0}
        if len(JOBS) >= 100:
            JOBS.pop(next(iter(JOBS)))
        JOBS[job['id']] = job
        task = asyncio.create_task(download(job, cfg, root, host, file))
        TASKS.add(task)
        task.add_done_callback(TASKS.discard)
        return web.json_response(job, status=202)
    except (ValueError, KeyError, TypeError) as e:
        return web.json_response({'error': str(e)}, status=400)
    except aiohttp.ClientError:
        return web.json_response({'error': '目标电脑无法连接 Civit 接口'}, status=502)


@PromptServer.instance.routes.get('/cosji-civit/jobs/{id}')
async def status(request):
    authorize(request)
    job = JOBS.get(request.match_info['id'])
    return web.json_response(job or {'error': '任务不存在；测试版任务记录在重启后清空'}, status=200 if job else 404)


@PromptServer.instance.routes.post('/cosji-civit/token')
async def save_token(request):
    cfg, _ = authorize(request)
    if request.content_length is None or request.content_length > 4096:
        return web.json_response({'error': 'Token 请求大小无效'}, status=400)
    try:
        body = await request.json()
        token = body.get('token', '')
        if not isinstance(token, str) or not token or len(token) > 2048 or any(c.isspace() for c in token):
            raise ValueError('Token 格式无效')
        cfg['civitai_token'] = token
        cfg.pop('oauth_refresh_token', None)
        cfg.pop('oauth_expires_at', None)
        save_config(cfg)
        return web.json_response({'tokenConfigured': True})
    except (ValueError, TypeError) as exc:
        return web.json_response({'error': str(exc)}, status=400)


@PromptServer.instance.routes.post('/cosji-civit/oauth/start')
async def start_oauth(request):
    try:
        cfg, _ = authorize(request)
        client_id = cfg.get('oauth_client_id', '')
        if not client_id:
            raise ValueError('网页授权尚未配置：需先注册此插件的 Civitai OAuth 应用，并在目标电脑配置 oauth_client_id')
        if any(a['state'] == 'pending' for a in AUTH.values()):
            raise ValueError('已有等待确认的授权，请先完成它或等待链接过期')
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=True) as session:
            result = await oauth_post(session, 'device', {'client_id': client_id, 'scope': '5'})
        if result.get('error'):
            raise ValueError('Civitai 未允许此应用启动设备授权：' + str(result['error']))
        url = result.get('verification_uri_complete', '')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'auth.civitai.com' or not result.get('device_code'):
            raise ValueError('Civitai 返回了无效的授权链接')
        if len(AUTH) >= 20:
            AUTH.pop(next(iter(AUTH)))
        auth = {'id': uuid.uuid4().hex, 'state': 'pending', 'clientId': client_id}
        AUTH[auth['id']] = auth
        task = asyncio.create_task(poll_auth(auth, result))
        TASKS.add(task)
        task.add_done_callback(TASKS.discard)
        return web.json_response({'id': auth['id'], 'state': 'pending', 'url': url, 'code': result.get('user_code', '')})
    except (ValueError, KeyError, TypeError) as e:
        return web.json_response({'error': str(e)}, status=400)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({'error': '目标电脑无法连接 Civitai 授权服务'}, status=502)


@PromptServer.instance.routes.get('/cosji-civit/oauth/status/{id}')
async def oauth_status(request):
    authorize(request)
    auth = AUTH.get(request.match_info['id'])
    if not auth:
        return web.json_response({'error': '授权任务已过期或服务已重启'}, status=404)
    return web.json_response({k: auth[k] for k in ('id', 'state', 'error') if k in auth})
