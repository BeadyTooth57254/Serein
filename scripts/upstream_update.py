"""Fetch reviewed GitHub releases without depending on repository history."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import time
import types
import urllib.error
import urllib.request
from uuid import uuid4
import zipfile

REPOSITORY = 'Yinglianchun/Serein'
API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
MAX_ARCHIVE = 100 * 1024 * 1024
MAX_EXPANDED = 300 * 1024 * 1024
REQUIRED = {'release-files.json', 'release-version.json', 'scripts/manage.py',
            'scripts/upstream_update.py', 'scripts/runtime_backup.py', 'scripts/one_click.sh', 'scripts/one_click.ps1'}
DENIED = {'.git', '.env', '.local', '.runtime', '.venv', 'node_modules', '__pycache__',
          'runtime', 'secrets', 'backups', 'output', 'config.toml', 'installation.json'}


def version(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-rc(\d+))?', value or '')
    if not match:
        raise ValueError('上游版本号格式不支持')
    major, minor, patch, rc = match.groups()
    return int(major), int(minor), int(patch), 1 if rc is None else 0, int(rc or 0)


def fetch(url, target=None, *, limit=1024 * 1024):
    request = urllib.request.Request(url, headers={'User-Agent': 'Serein-Updater',
                                                  'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if not response.url.startswith('https://'):
                raise ValueError('上游下载必须使用 HTTPS')
            total = 0
            chunks = []
            started = time.monotonic()
            handle = Path(target).open('wb') if target else None
            try:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit or time.monotonic() - started > 300:
                        raise ValueError('上游下载过大或超过五分钟，请稍后重试')
                    if handle:
                        handle.write(chunk)
                    else:
                        chunks.append(chunk)
            finally:
                if handle:
                    handle.close()
        return b''.join(chunks) if target is None else total
    except urllib.error.HTTPError as exc:
        raise ValueError(f'无法读取 GitHub 发行包（HTTP {exc.code}）；请检查网络或稍后重试') from exc
    except urllib.error.URLError as exc:
        raise ValueError('无法连接 GitHub；当前服务和源码尚未更新') from exc


def latest_release():
    release = json.loads(fetch(API))
    tag = release.get('tag_name', '')
    version(tag)
    if release.get('draft') or release.get('prerelease'):
        raise ValueError('上游尚未提供正式发布的安装包')
    filename = f'serein-public-{tag.removeprefix("v")}.zip'
    assets = {item['name']: item for item in release.get('assets', [])}
    if filename not in assets or filename + '.sha256' not in assets:
        raise ValueError('最新发行缺少安装包或 SHA-256 校验文件，未开始更新')
    prefix = f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
    for name in (filename, filename + '.sha256'):
        if assets[name].get('browser_download_url') != prefix + name:
            raise ValueError('发行附件地址不属于配置的上游仓库')
    return {'tag': tag, 'asset': assets[filename], 'checksum': assets[filename + '.sha256']}


def safe_name(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('发行清单含无效路径')
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError('发行清单含越界路径')
    if any(p.endswith(('.', ' ')) or re.search(r'[\x00-\x1f<>"|?*]', p) or
           re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p) for p in path.parts):
        raise ValueError('发行清单含不兼容的文件名')
    if path.parts[0].lower() == 'data' or any(p.lower() in DENIED for p in path.parts) or path.suffix.lower() in ('.db', '.sqlite', '.sqlite3', '.log'):
        raise ValueError('发行清单包含实例数据或配置')
    if path.parts[0] == 'deploy' and name not in ('deploy/compose.yaml', 'deploy/Gateway.Dockerfile'):
        raise ValueError('发行清单包含未允许的部署文件')
    return name


def destination(root, name):
    safe_name(name)
    root = Path(root).resolve()
    target = root / name
    # Resolve each existing component: reject symlinks and Windows junctions.
    current = root
    for part in PurePosixPath(name).parts:
        current = current / part
        if current.is_symlink() or current.resolve() != current:
            raise ValueError('发行目标含符号链接或目录联接：' + name)
    if not target.resolve().is_relative_to(root):
        raise ValueError('发行目标超出当前目录')
    return target


def manifest(content):
    names = json.loads(content)
    if not isinstance(names, list) or not names or len(names) > 10000:
        raise ValueError('发行清单格式无效')
    names = [safe_name(name) for name in names]
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError('发行清单包含重复或大小写冲突路径')
    return names


def prepare(release, folder):
    folder = Path(folder)
    archive_path = folder / 'release.zip'
    fetch(release['asset']['browser_download_url'], archive_path, limit=MAX_ARCHIVE)
    checksum = fetch(release['checksum']['browser_download_url'], limit=4096).decode('ascii').strip().split()
    actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if len(checksum) != 2 or checksum[1].lstrip('*') != release['asset']['name'] or checksum[0].lower() != actual:
        raise ValueError('安装包 SHA-256 校验失败，当前服务和源码未改动')
    if release['asset'].get('digest') not in (None, 'sha256:' + actual):
        raise ValueError('安装包与 GitHub 附件摘要不一致')
    staged = folder / 'source'
    staged.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if sum(info.file_size for info in infos) > MAX_EXPANDED or len(infos) > 10000:
            raise ValueError('发行包展开后过大')
        names = manifest(archive.read('serein-public/release-files.json'))
        expected = {'serein-public/' + name for name in names}
        if len(infos) != len(expected) or {info.filename for info in infos} != expected or not REQUIRED.issubset(names):
            raise ValueError('安装包内容与发行清单不一致')
        metadata = json.loads(archive.read('serein-public/release-version.json'))
        if version(metadata.get('version', '')) != version(release['tag']):
            raise ValueError('安装包版本与发行标签不一致')
        for info in infos:
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('发行包不能包含符号链接')
            target = destination(staged, info.filename.removeprefix('serein-public/'))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
            target.chmod(0o755 if (info.external_attr >> 16) & 0o111 else 0o644)
    return staged, names, actual


def plan_files(root, names, staged=None):
    root = Path(root).resolve()
    old_names = manifest((root / 'release-files.json').read_text(encoding='utf-8'))
    state_path = root / 'deploy' / 'update-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    managed = state.get('source_hashes')
    interrupted = state.get('phase') == 'applying' and managed and staged is not None
    if interrupted:
        old_names = [safe_name(name) for name in managed]
    all_names = sorted(set(old_names) | set(names))
    for name in all_names:
        target = destination(root, name)
        if target.exists() and not target.is_file():
            raise ValueError('发行目标不是普通文件：' + name)
        if target.exists() and name not in old_names and not interrupted:
            raise ValueError('新版文件与本地自建文件重名，请先移开：' + name)
    if interrupted:
        # After a power loss, files may be either the previous or target release.
        # Accept only those two known versions; unrelated edits still stop here.
        for name in all_names:
            target = destination(root, name)
            actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
            incoming = hashlib.sha256((staged / name).read_bytes()).hexdigest() if name in names else None
            if actual not in (managed.get(name), incoming):
                raise ValueError('中断更新的源码包含额外修改，请先保存：' + name)
    elif managed:
        if managed != source_hashes(root):
            raise ValueError('发行源码在上次更新后被修改，请先保存；未覆盖本地源码')
    elif (root / '.git').exists():
        dirty = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=no'])
        if dirty.strip():
            raise ValueError('此 Git 工作区有未提交改动，请先保存；未覆盖本地源码')
    return all_names, sorted(set(old_names) - set(names))


def source_hashes(root):
    names = manifest((root / 'release-files.json').read_text(encoding='utf-8'))
    return {name: hashlib.sha256(destination(root, name).read_bytes()).hexdigest()
            if destination(root, name).is_file() else None for name in names}


def backup_sources(root, names):
    folder = Path(root) / 'deploy' / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    if folder.resolve() != folder or folder.is_symlink():
        raise ValueError('备份目录不能是符号链接或目录联接')
    path = folder / ('source-' + uuid4().hex + '.zip')
    with path.open('xb') as handle:
        os.chmod(path, 0o600)
        with zipfile.ZipFile(handle, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                target = destination(root, name)
                if target.is_file():
                    archive.write(target, name)
    return path


def replace_file(target, data, mode=0o644):
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_name(target.name + '.update-' + uuid4().hex)
    try:
        pending.write_bytes(data)
        pending.chmod(mode)
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)


def apply_files(root, staged, names, obsolete, backup):
    try:
        for name in names:
            source = staged / name
            replace_file(destination(root, name), source.read_bytes(), stat.S_IMODE(source.stat().st_mode))
        for name in obsolete:
            destination(root, name).unlink(missing_ok=True)
    except BaseException:
        with zipfile.ZipFile(backup) as archive:
            saved = set(archive.namelist())
            for name in set(names) | set(obsolete):
                target = destination(root, name)
                if name in saved:
                    info = archive.getinfo(name)
                    replace_file(target, archive.read(name), stat.S_IMODE(info.external_attr >> 16))
                else:
                    target.unlink(missing_ok=True)
        raise


def load_source(path):
    path = Path(path)
    module = types.ModuleType('updated_serein_' + path.stem)
    module.__file__ = str(path)
    # Read the new source, not a potentially cached pre-update .pyc file.
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module


def load_manager(root):
    return load_source(Path(root) / 'scripts' / 'manage.py')


def backup_runtime(staged, deploy):
    # Use the verified new backup helper, including lock-file handling, even
    # when the installed updater is from an older release.
    path = load_source(staged / 'scripts' / 'runtime_backup.py').snapshot(deploy)
    if path:
        print('数据备份已保存：' + str(path), flush=True)
    return path


def run_update(manager):
    root = Path(manager.ROOT).resolve()
    if not (root / 'deploy' / 'config.toml').is_file():
        raise ValueError('请先部署当前实例，再检查上游更新')
    release = latest_release()
    local_path = root / 'release-version.json'
    current = json.loads(local_path.read_text())['version'] if local_path.exists() else ''
    state_path = root / 'deploy' / 'update-state.json'
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    retry = previous.get('phase') in ('prepared', 'applying', 'building', 'starting', 'failed') and previous.get('version') == release['tag']
    print(f'当前版本：{current or "旧发行包（未记录版本）"}；上游最新：{release["tag"]}')
    if current and (version(current) > version(release['tag']) or version(current) == version(release['tag']) and not retry):
        print('当前已是此版本或更新版本，无须重复下载和构建。')
        return False
    choice = manager.choose('下载并更新上游版本', [('1', '先备份再更新（默认）'),
                            ('0', '跳过数据备份直接更新')], default='1', back=True)
    if choice == 'r':
        return False
    manager.ensure_tools()
    print('下载并校验新版安装包；这一阶段保持当前服务运行。', flush=True)
    with tempfile.TemporaryDirectory(prefix='serein-update-') as temporary:
        try:
            staged, names, sha256 = prepare(release, temporary)
        except (zipfile.BadZipFile, KeyError) as exc:
            raise ValueError('安装包结构不完整或已损坏；当前服务和源码未改动') from exc
        all_names, obsolete = plan_files(root, names, staged)
        backup = backup_sources(root, all_names)
        state = {'version': release['tag'], 'sha256': sha256, 'source_backup': str(backup)}
        def record(phase):
            state['phase'] = phase
            manager.private_file(state_path, json.dumps({**state, 'source_hashes': source_hashes(root)}))
        record('prepared')
        fresh = None
        try:
            manager.service_action('stop')
            if choice == '1':
                state['data_backup'] = str(backup_runtime(staged, root / 'deploy') or '')
            record('applying')
            apply_files(root, staged, names, obsolete, backup)
            fresh = load_manager(root)
            record('building')
            fresh.ensure_tools()
            fresh.build_runtime(backup=False)
            record('starting')
            fresh.service_action('up')
            fresh.check_memory()
            record('complete')
        except BaseException:
            if fresh and state.get('phase') == 'starting':
                try:
                    fresh.service_action('stop')
                except Exception:
                    print('服务启动失败，停止命令也未成功；请用菜单 4 检查当前服务状态。')
            record('failed')
            print('更新未完成。源码备份：' + str(backup) + '；数据备份（如选择）在 deploy/backups。')
            print('请查看错误后重试更新；构建失败不会自动启动半更新服务。', flush=True)
            raise
    print('上游更新完成：' + release['tag'] + '。重新输入 se 可打开新版管理菜单。')
    return True


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='在原安装目录内接入或运行 Serein 上游更新')
    parser.add_argument('--root', type=Path, required=True, help='已有实例的发行目录（包含 deploy 和 scripts）')
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        manager = load_manager(root)
        locks = load_source(root / 'src' / 'serein' / 'file_lock.py')
        with locks.exclusive_lock(root / 'deploy' / 'runtime' / 'installer.lock'):
            run_update(manager)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print('更新未完成：' + str(exc))
        return 1
    except KeyboardInterrupt:
        print('已取消更新。')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
