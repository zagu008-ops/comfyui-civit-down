"""Machine-local download records; never store authentication material here."""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ACTIVE = ('queued', 'downloading', 'verifying')


class JobStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, data TEXT NOT NULL, snapshot TEXT NOT NULL, updated REAL NOT NULL)')
            rows = db.execute('SELECT id,data FROM jobs').fetchall()
            for job_id, raw in rows:
                job = json.loads(raw)
                if job['state'] in ACTIVE:
                    job.update(state='interrupted', error='ComfyUI 已重启，下载中断；可重试', updated=time.time())
                    db.execute('UPDATE jobs SET data=?,updated=? WHERE id=?', (json.dumps(job, ensure_ascii=False), job['updated'], job_id))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def snapshot(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT snapshot FROM jobs WHERE id=?', (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self):
        with self.connect() as db:
            rows = db.execute('SELECT data FROM jobs ORDER BY updated DESC LIMIT 100').fetchall()
        return [json.loads(row[0]) for row in rows]

    def save(self, job):
        job['updated'] = time.time()
        with self.connect() as db:
            db.execute('UPDATE jobs SET data=?,updated=? WHERE id=?', (json.dumps(job, ensure_ascii=False), job['updated'], job['id']))

    def reserve(self, snapshot):
        file = snapshot['file']
        identity = hashlib.sha256(json.dumps([snapshot['root'], snapshot['folder'], file['sha256']], ensure_ascii=False).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT data FROM jobs WHERE identity=?', (identity,)).fetchone()
            if existing:
                return json.loads(existing[0]), False
            for raw, in db.execute('SELECT data FROM jobs'):
                if json.loads(raw)['state'] in ACTIVE:
                    raise ValueError('目标电脑已有下载任务，请等待完成或取消')
            job = {'id': uuid.uuid4().hex, 'state': 'queued', 'folder': snapshot['folder'],
                   'name': file['name'], 'total': file['bytes'], 'received': 0,
                   'modelId': snapshot['modelId'], 'versionId': file['versionId'], 'fileId': file['fileId'],
                   'sha256': file['sha256'], 'created': time.time(), 'updated': time.time()}
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)', (job['id'], identity, json.dumps(job, ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False), job['updated']))
        return job, True

    def retry(self, job_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise ValueError('任务不存在')
            job = json.loads(row[0])
            if job['state'] not in ('failed', 'cancelled', 'interrupted'):
                raise ValueError('只能重试失败、取消或中断的任务')
            for raw, in db.execute('SELECT data FROM jobs'):
                if json.loads(raw)['state'] in ACTIVE:
                    raise ValueError('目标电脑已有下载任务')
            job.update(state='queued', received=0, updated=time.time())
            for field in ('error', 'path', 'reused'):
                job.pop(field, None)
            db.execute('UPDATE jobs SET data=?,updated=? WHERE id=?', (json.dumps(job, ensure_ascii=False), job['updated'], job_id))
        return job
