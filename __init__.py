"""Paired Civitai LoRA downloads. Configure config.json locally, never commit it."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import secrets
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urljoin

import aiohttp
from aiohttp import web
import folder_paths
from server import PromptServer
from .civit_oauth_format import scope_bitmask
from .job_store import JobStore
from .local_oauth import create_authorization_app, OAUTH_OUTCOME

NODE_CLASS_MAPPINGS = {}
CONFIG = Path(__file__).with_name('config.json')
JOBS = {}
STORE = None
DOWNLOAD_TASKS = {}
REFRESH_LOCK = asyncio.Lock()
TASKS = set()
AUTH = {}
HOSTS = {'civitai.com', 'civitai.red'}
MAX_BYTES = 2 * 1024**3
AUTH_URL = 'https://auth.civitai.com/api/auth/oauth/'
VERSION = '1.0.0-beta.1'
DEFAULT_CLIENT_ID = 'c62bd385-135e-49cb-a132-a91f8e2a8ba5'


class RetryableDownloadError(ValueError):
    pass


def get_store():
    global STORE
    if STORE is None:
        STORE = JobStore(CONFIG.parent / '.state/jobs.sqlite3')
    return STORE


def record(job):
    if STORE is not None and job['id'] in JOBS:
        STORE.save(job)


async def download(job, cfg, root, host, file):
    try:
        for attempt in range(3):
            job.update(state='queued', received=0, attempt=attempt + 1)
            job.pop('error', None)
            job.pop('retryable', None)
            record(job)
            await download_once(job, cfg, root, host, file)
            if job['state'] != 'failed' or not job.get('retryable') or attempt == 2:
                break
            job['state'] = 'queued'
            record(job)
            await asyncio.sleep(2 ** attempt)
    except asyncio.CancelledError:
        job.update(state='cancelled', error='下载已取消')
    finally:
        record(job)


def launch_download(job, cfg, root, host, file):
    task = asyncio.create_task(download(job, cfg, root, host, file))
    DOWNLOAD_TASKS[job['id']] = task
    task.add_done_callback(lambda _: DOWNLOAD_TASKS.pop(job['id'], None))


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
    scopes = scope_bitmask(result.get('scope', '0'))
    if not scopes & 4:
        raise ValueError('授权不包含模型下载权限')
    cfg = config()
    cfg.update(civitai_token=result['access_token'], oauth_refresh_token=result['refresh_token'],
               oauth_expires_at=time.time() + int(result.get('expires_in', 3600)))
    save_config(cfg)
    return cfg


async def fresh_config():
    async with REFRESH_LOCK:
        cfg = config()
        if cfg.get('oauth_refresh_token') and cfg.get('oauth_expires_at', 0) < time.time() + 60:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=True) as session:
                result = await oauth_post(session, 'token', {'grant_type': 'refresh_token',
                                         'refresh_token': cfg['oauth_refresh_token'], 'client_id': cfg.get('oauth_client_id', '')})
            if result.get('error'):
                raise ValueError('Civitai 授权已过期或失效，请重新在目标电脑授权')
            current = config()
            if current.get('oauth_refresh_token') != cfg['oauth_refresh_token']:
                return current
            return store_oauth(result)
        return cfg




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
    local_origin = getattr(request, 'remote', None) in {'127.0.0.1', '::1'} and origin == f'{getattr(request, "scheme", "http")}://{getattr(request, "host", "")}'
    if origin and origin not in cfg.get('allowed_origins', []) and not local_origin:
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
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10))}
    if re.search(r'[<>"|?*]', value) or value.endswith(('.', ' ')) or value.split('.')[0].upper() in reserved:
        raise ValueError('名称不兼容 Windows 文件系统')
    return value


async def metadata(session, host, model_id, token):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    async with session.get(f'https://{host}/api/v1/models/{model_id}', headers=headers, allow_redirects=False) as r:
        if r.status != 200:
            raise ValueError(f'Civit 模型资料读取失败（HTTP {r.status}）')
        return await r.json()


def model_files(model):
    if str(model.get('type', '')).upper() != 'LORA':
        raise ValueError('仅支持 LoRA')
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
    return u.scheme == 'https' and not u.username and not u.password and u.port in (None, 443) and (host in HOSTS or host == 'b2.civitai.com' or host.endswith('.r2.cloudflarestorage.com') or host.endswith('.civitaiusercontent.com'))


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


async def download_once(job, cfg, root, host, file):
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
            raise ValueError('下载限于 2 GiB 以内的文件')
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
                    if r.status in {429, 500, 502, 503, 504}:
                        raise RetryableDownloadError(f'下载服务暂时不可用（HTTP {r.status}）')
                    if r.status != 200:
                        raise ValueError(f'下载失败（HTTP {r.status}）')
                    job['state'] = 'downloading'
                    record(job)
                    last_progress = time.monotonic()
                    h = hashlib.sha256()
                    with temp.open('xb') as out:
                        async for block in r.content.iter_chunked(256 * 1024):
                            job['received'] += len(block)
                            if time.monotonic() - last_progress >= 1:
                                record(job)
                                last_progress = time.monotonic()
                            if job['received'] > MAX_BYTES or job['received'] > file['bytes'] + 1024**2:
                                raise ValueError('下载文件超过预期大小')
                            out.write(block)
                            h.update(block)
                    job['state'] = 'verifying'
                    record(job)
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
        job.update(state='failed', error=str(exc) if isinstance(exc, (ValueError, FileExistsError)) else type(exc).__name__, retryable=isinstance(exc, (RetryableDownloadError, aiohttp.ClientError, asyncio.TimeoutError)))
    finally:
        if temp and temp.exists():
            temp.unlink()
        record(job)


async def request_object(request, limit=16384):
    if request.content_length is None or request.content_length > limit:
        raise ValueError('请求大小无效')
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError('请求必须是 JSON 对象')
    return body


@PromptServer.instance.routes.post('/cosji-civit/inspect')
async def inspect(request):
    try:
        cfg, root = authorize(request)
        cfg = await fresh_config()
        body = await request_object(request)
        host, model_id = parse_link(body.get('url', ''))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=True) as session:
            model = await metadata(session, host, model_id, cfg.get('civitai_token', ''))
        versions = [{'id': v['id'], 'name': v.get('name', ''), 'baseModel': v.get('baseModel', ''),
                     'trainedWords': v.get('trainedWords', []), 'description': str(v.get('description', ''))[:60000],
                     'images': [{'url': i.get('url'), 'width': i.get('width'), 'height': i.get('height')} for i in v.get('images', [])[:12]]}
                    for v in model.get('modelVersions', [])]
        return web.json_response({'modelId': model['id'], 'name': model['name'], 'type': model.get('type'),
                                  'description': str(model.get('description', ''))[:60000], 'tags': model.get('tags', []),
                                  'versions': versions, 'files': model_files(model), 'root': str(root),
                                  'tokenConfigured': bool(cfg.get('civitai_token')), 'oauthConfigured': bool(cfg.get('oauth_client_id'))})
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return web.json_response({'error': str(e)}, status=400)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({'error': '目标电脑无法连接 Civit 接口'}, status=502)


@PromptServer.instance.routes.post('/cosji-civit/download')
async def start(request):
    try:
        cfg, root = authorize(request)
        cfg = await fresh_config()
        body = await request_object(request)
        host, model_id = parse_link(body.get('url', ''))
        folder = safe_part(body.get('folder', 'Civit测试'))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=True) as session:
            model = await metadata(session, host, model_id, cfg.get('civitai_token', ''))
        file = next((f for f in model_files(model) if str(f['versionId']) == str(body.get('versionId')) and str(f['fileId']) == str(body.get('fileId'))), None)
        if file is None:
            raise ValueError('所选文件不属于此模型版本')
        snapshot = {'host': host, 'modelId': model_id, 'file': file, 'folder': folder, 'root': str(root)}
        job, created = get_store().reserve(snapshot)
        if not created and job['id'] in JOBS:
            job = JOBS[job['id']]
        JOBS[job['id']] = job
        if created:
            launch_download(job, cfg, root, host, file)
        elif job['state'] == 'done':
            path = root / job.get('path', '')
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root) or await asyncio.to_thread(file_hash, path) != file['sha256']:
                job.update(state='interrupted', error='已完成记录对应的文件缺失或已改变，请重试')
                record(job)
        return web.json_response(job, status=202 if created else 200)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return web.json_response({'error': str(e)}, status=400)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({'error': '目标电脑无法连接 Civit 接口'}, status=502)


@PromptServer.instance.routes.get('/cosji-civit/jobs/{id}')
async def status(request):
    authorize(request)
    job = JOBS.get(request.match_info['id']) or get_store().get(request.match_info['id'])
    return web.json_response(job or {'error': '任务不存在'}, status=200 if job else 404)


@PromptServer.instance.routes.post('/cosji-civit/token')
async def save_token(request):
    require_local(request)
    cfg, _ = authorize(request)
    if request.content_length is None or request.content_length > 4096:
        return web.json_response({'error': 'Token 请求大小无效'}, status=400)
    try:
        body = await request_object(request)
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


def require_local(request):
    host = urlsplit('http://' + request.host).hostname
    if request.remote not in {'127.0.0.1', '::1'} or host not in {'127.0.0.1', 'localhost', '::1'}:
        raise web.HTTPForbidden(text='请在 ComfyUI 电脑的本机浏览器操作')
    origin = request.headers.get('Origin')
    if origin and origin != f'{request.scheme}://{request.host}':
        raise web.HTTPForbidden(text='本机配置只允许同源操作')
    if request.headers.get('Sec-Fetch-Site') == 'cross-site':
        raise web.HTTPForbidden(text='拒绝跨站本机配置请求')


@PromptServer.instance.routes.get('/cosji-civit/capabilities')
async def capabilities(request):
    cfg, root = authorize(request)
    return web.json_response({'version': VERSION, 'protocol': 1, 'persistentJobs': True,
                              'localAuthorization': True, 'connected': bool(cfg.get('civitai_token')),
                              'root': str(root)})


@PromptServer.instance.routes.get('/cosji-civit/jobs')
async def jobs(request):
    authorize(request)
    return web.json_response({'jobs': get_store().list()})


@PromptServer.instance.routes.post('/cosji-civit/jobs/{id}/cancel')
async def cancel_job(request):
    authorize(request)
    job_id = request.match_info['id']
    job = JOBS.get(job_id) or get_store().get(job_id)
    if not job:
        return web.json_response({'error': '任务不存在'}, status=404)
    if job['state'] not in {'queued', 'downloading', 'verifying'}:
        return web.json_response({'error': '任务已经结束', 'job': job}, status=409)
    task = DOWNLOAD_TASKS.get(job_id)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    job.update(state='cancelled', error='下载已取消')
    get_store().save(job)
    return web.json_response(job)


@PromptServer.instance.routes.post('/cosji-civit/jobs/{id}/retry')
async def retry_job(request):
    try:
        cfg, root = authorize(request)
        cfg = await fresh_config()
        job_id = request.match_info['id']
        snapshot = get_store().snapshot(job_id)
        if not snapshot:
            raise ValueError('任务不存在')
        if str(root) != snapshot['root']:
            raise ValueError('LoRA 根目录已改变，请重新确认导入目标')
        folder = root / safe_part(snapshot['folder'])
        if folder.is_symlink() or not folder.resolve().is_relative_to(root):
            raise ValueError('原下载目录不再有效')
        task = DOWNLOAD_TASKS.get(job_id)
        if task and not task.done():
            raise ValueError('上次任务仍在退出，请稍后重试')
        previous = get_store().get(job_id)
        if previous['state'] not in {'failed', 'cancelled', 'interrupted'}:
            raise ValueError('只能重试失败、取消或中断的任务')
        partial = folder / ('.' + job_id + '.part')
        if partial.exists() or partial.is_symlink():
            partial.unlink()
        job = get_store().retry(job_id)
        JOBS[job_id] = job
        launch_download(job, cfg, root, snapshot['host'], snapshot['file'])
        return web.json_response(job, status=202)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return web.json_response({'error': str(exc)}, status=400)


@PromptServer.instance.routes.get('/cosji-civit/local')
async def local_page(request):
    require_local(request)
    return web.FileResponse(Path(__file__).with_name('web') / 'local.html', headers={'Cache-Control': 'no-store'})


@PromptServer.instance.routes.get('/cosji-civit/local/config')
async def local_config(request):
    require_local(request)
    roots = folder_paths.get_folder_paths('loras')
    if not roots:
        return web.json_response({'error': 'ComfyUI 未配置 LoRA 目录'}, status=400)
    if not CONFIG.exists():
        save_config({'pairing_key': secrets.token_urlsafe(32), 'allowed_origins': [], 'lora_root': str(Path(roots[0]).resolve()),
                     'oauth_client_id': DEFAULT_CLIENT_ID, 'civitai_token': ''})
    cfg = config()
    return web.json_response({'version': VERSION, 'roots': roots, 'root': cfg['lora_root'],
                              'allowedOrigins': cfg.get('allowed_origins', []), 'pairingKey': cfg['pairing_key'],
                              'connected': bool(cfg.get('civitai_token'))}, headers={'Cache-Control': 'no-store'})


@PromptServer.instance.routes.post('/cosji-civit/local/config')
async def update_local_config(request):
    require_local(request)
    try:
        if request.content_length is None or request.content_length > 8192:
            raise ValueError('配置请求大小无效')
        body = await request_object(request)
        root = Path(body['root']).resolve()
        if root not in [Path(p).resolve() for p in folder_paths.get_folder_paths('loras')]:
            raise ValueError('请选择 ComfyUI 已配置的 LoRA 根目录')
        if any(j['state'] in {'queued', 'downloading', 'verifying'} for j in get_store().list()):
            raise ValueError('下载执行中，不能更改目标配置')
        origins = body.get('allowedOrigins', [])
        if not isinstance(origins, list) or len(origins) > 16:
            raise ValueError('允许来源最多 16 项')
        for origin in origins:
            u = urlsplit(origin)
            if u.scheme not in {'http', 'https'} or not u.hostname or u.username or u.password or origin != u.scheme + '://' + u.netloc:
                raise ValueError('来源必须是协议、主机和端口，不包含路径')
        cfg = config()
        cfg.update(lora_root=str(root), allowed_origins=list(dict.fromkeys(origins)))
        cfg.setdefault('oauth_client_id', DEFAULT_CLIENT_ID)
        save_config(cfg)
        return web.json_response({'saved': True})
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return web.json_response({'error': str(exc)}, status=400)


@PromptServer.instance.routes.post('/cosji-civit/oauth/start')
async def start_oauth(request):
    require_local(request)
    try:
        cfg, _ = authorize(request)
        for old_id in list(AUTH):
            if time.monotonic() >= AUTH[old_id]['expires'] + 60:
                AUTH.pop(old_id, None)
        if len(AUTH) >= 20:
            raise ValueError('授权尝试过多，请稍后重试')
        for auth in AUTH.values():
            if auth['app'][OAUTH_OUTCOME]['state'] == 'pending' and time.monotonic() < auth['expires']:
                return web.json_response({'id': auth['id'], 'url': auth['url'], 'state': 'pending'})
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        sock.setblocking(False)
        port = sock.getsockname()[1]
        auth_app = create_authorization_app(CONFIG, port, cfg.get('oauth_client_id') or DEFAULT_CLIENT_ID)
        runner = web.AppRunner(auth_app, access_log=None)
        await runner.setup()
        await web.SockSite(runner, sock).start()
        auth_id = uuid.uuid4().hex
        AUTH[auth_id] = {'id': auth_id, 'url': f'http://127.0.0.1:{port}/start', 'app': auth_app, 'runner': runner, 'expires': time.monotonic() + 900}
        asyncio.get_running_loop().call_later(960, lambda: asyncio.create_task(runner.cleanup()))
        return web.json_response({'id': auth_id, 'url': AUTH[auth_id]['url'], 'state': 'pending'})
    except (ValueError, OSError) as exc:
        return web.json_response({'error': str(exc) if isinstance(exc, ValueError) else '无法启动本机回调服务'}, status=400)


@PromptServer.instance.routes.get('/cosji-civit/oauth/status/{id}')
async def oauth_status(request):
    authorize(request)
    auth = AUTH.get(request.match_info['id'])
    if not auth:
        return web.json_response({'error': '授权任务已过期或服务已重启'}, status=404)
    data = dict(auth['app'][OAUTH_OUTCOME], id=auth['id'])
    if data['state'] == 'pending' and time.monotonic() >= auth['expires']:
        data.update(state='failed', error='授权会话已过期')
    return web.json_response(data)
