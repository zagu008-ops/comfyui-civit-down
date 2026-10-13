"""Create machine-local configuration without printing credentials."""
import argparse
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description='配置 ComfyUI Civit 下载插件')
    parser.add_argument('--origin', required=True, action='append', help='完整酒馆来源，例如 http://192.168.1.10:8000；可重复指定')
    parser.add_argument('--lora-root', required=True, help='ComfyUI 已配置的 LoRA 根目录的绝对路径')
    parser.add_argument('--client-id', default='c62bd385-135e-49cb-a132-a91f8e2a8ba5')
    args = parser.parse_args()
    origins = []
    for value in args.origin:
        u = urlsplit(value)
        if u.scheme not in {'http', 'https'} or not u.hostname or u.username or u.password or u.path not in {'', '/'} or u.query or u.fragment:
            parser.error('--origin 应只包含协议、主机和端口')
        origins.append(f'{u.scheme}://{u.netloc}')
    root = Path(args.lora_root).expanduser()
    if not root.is_absolute() or not root.is_dir():
        parser.error('--lora-root 必须是存在的绝对目录')
    path = Path(__file__).with_name('config.json')
    cfg = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    cfg.update(allowed_origins=list(dict.fromkeys(origins)), lora_root=str(root.resolve()), oauth_client_id=args.client_id)
    if not isinstance(cfg.get('pairing_key'), str) or len(cfg['pairing_key']) < 32 or cfg['pairing_key'].startswith('REPLACE_'):
        cfg['pairing_key'] = secrets.token_urlsafe(32)
    cfg.setdefault('civitai_token', '')
    temp = path.with_suffix('.json.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(cfg, stream, ensure_ascii=False, indent=2)
    os.replace(temp, path)
    print(f'配置已保存：{path}')
    print('配对密钥保存在该文件的 pairing_key 中，请在本机查看。已有账号授权已保留。')
    print('Windows 请同时使用文件属性/NTFS 权限限制其他用户读取此文件。')


if __name__ == '__main__':
    main()
