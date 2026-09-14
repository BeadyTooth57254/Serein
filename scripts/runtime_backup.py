"""Stopped-service backup; restore is explicit and never overwrites live data."""
from pathlib import Path
import tarfile
import re
from datetime import datetime,timezone
from uuid import uuid4


def cleanup_plan(deploy, keep=3):
    if type(keep) is not int or keep<1:raise ValueError('至少保留最近 1 份备份')
    folder=Path(deploy).resolve()/'backups'
    if folder.is_symlink() or folder.resolve()!=folder:raise ValueError('备份目录不能是符号链接或目录联接')
    rows=[]
    if folder.is_dir():
        for path in folder.iterdir():
            if not re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{8}\.tar\.gz',path.name):continue
            if path.is_symlink() or not path.is_file() or path.resolve().parent!=folder:continue
            info=path.stat()
            rows.append({'name':path.name,'size':info.st_size,'mtime_ns':info.st_mtime_ns,
                         'inode':info.st_ino,'device':info.st_dev})
    rows.sort(key=lambda row:(row['mtime_ns'],row['name']),reverse=True)
    return {'folder':str(folder),'keep':keep,'files':rows,'remove':rows[keep:]}


def cleanup(deploy, plan):
    # Recheck the complete preview before deleting anything. No recursive delete,
    # no symlink following, and no access outside this instance's backup folder.
    current=cleanup_plan(deploy,plan['keep'])
    if current!=plan:raise ValueError('备份清单已变化，请重新预览后确认')
    folder=Path(current['folder'])
    for row in current['remove']:
        path=folder/row['name']
        if path.is_symlink() or path.resolve().parent!=folder:raise ValueError('备份路径已变化，停止清理')
        info=path.stat()
        if (info.st_size,info.st_mtime_ns,info.st_ino,info.st_dev)!=(row['size'],row['mtime_ns'],row['inode'],row['device']):
            raise ValueError('备份文件已变化，停止清理')
        path.unlink()
    return {'removed':len(current['remove']),'bytes':sum(row['size'] for row in current['remove']),
            'retained':len(current['files'])-len(current['remove'])}


def snapshot(deploy):
    deploy=Path(deploy).resolve()
    if not (deploy/'runtime'/'serein.db').is_file():return None
    folder=deploy/'backups';folder.mkdir(exist_ok=True)
    target=folder/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]+'.tar.gz')
    pending=target.with_suffix('.pending')
    try:
        with tarfile.open(pending,'w:gz',dereference=False) as archive:
            for name in ('runtime','secrets','config.toml','.env','installation.json'):
                path=deploy/name
                if not path.exists():continue
                # Backups may contain credentials; keep the archive private.
                # The running installer holds this lock on Windows; it is
                # process coordination, not persistent instance state.
                archive.add(path,arcname=name,recursive=True,
                            filter=lambda item: None if item.name == 'runtime/installer.lock' else item)
        pending.chmod(0o600);pending.replace(target)
    except BaseException:
        pending.unlink(missing_ok=True);raise
    return target
