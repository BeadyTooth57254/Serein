import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import types
import zipfile

import pytest


def updater():
    path = Path(__file__).parents[1] / 'scripts/upstream_update.py'
    spec = importlib.util.spec_from_file_location('updater', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package(module, *, extra=None, tag='v0.1.0-rc66'):
    files = {name: b'# synthetic source\n' for name in module.REQUIRED}
    files['release-version.json'] = json.dumps({'version': tag}).encode()
    files.update(extra or {})
    files['release-files.json'] = json.dumps(sorted(files)).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in files.items():
            archive.writestr('serein-public/' + name, data)
    data = buffer.getvalue()
    name = f'serein-public-{tag[1:]}.zip'
    sha = hashlib.sha256(data).hexdigest()
    prefix = f'https://github.com/Yinglianchun/Serein/releases/download/{tag}/'
    release = {'tag': tag, 'asset': {'name': name, 'browser_download_url': prefix + name,
                                    'digest': 'sha256:' + sha},
               'checksum': {'name': name + '.sha256', 'browser_download_url': prefix + name + '.sha256'}}
    return release, data, f'{sha}  {name}\n'.encode()


def network(monkeypatch, module, release, data, checksum):
    def fetch(url, target=None, **kwargs):
        content = data if url == release['asset']['browser_download_url'] else checksum
        if target:
            Path(target).write_bytes(content)
        else:
            return content
    monkeypatch.setattr(module, 'fetch', fetch)


@pytest.mark.parametrize('bad', ['../outside', '/absolute', 'C:/escape', 'scripts\\escape',
    'deploy/.env', 'deploy/.env.', 'deploy/runtime/serein.db', '.git/config',
    'deploy/config.toml', 'scripts/CON', 'scripts/x/../../escape', 'scripts/nul.txt'])
def test_rejects_private_and_platform_escape_paths(bad):
    with pytest.raises(ValueError):
        updater().safe_name(bad)


def test_checksum_failure_does_not_extract(monkeypatch, tmp_path):
    module = updater()
    release, data, checksum = package(module)
    network(monkeypatch, module, release, data + b'changed', checksum)
    with pytest.raises(ValueError, match='校验失败'):
        module.prepare(release, tmp_path)
    assert not (tmp_path / 'source').exists()


@pytest.mark.parametrize('kind', ['extra', 'duplicate', 'symlink', 'case_collision', 'private'])
def test_rejects_unsafe_archive_even_with_valid_checksum(monkeypatch, tmp_path, kind):
    module = updater()
    release, data, _ = package(module, extra={'README.md': b'synthetic'})
    buffer = io.BytesIO(data)
    with zipfile.ZipFile(buffer, 'a') as archive:
        if kind == 'extra':
            archive.writestr('serein-public/not-in-manifest', b'x')
        elif kind == 'duplicate':
            with pytest.warns(UserWarning):
                archive.writestr('serein-public/README.md', b'duplicate')
        else:
            files = {i.filename: archive.read(i) for i in archive.infolist()}
            name = {'symlink': 'link', 'case_collision': 'readme.md', 'private': 'deploy/.env'}[kind]
            files['serein-public/' + name] = b'x'
            names = json.loads(files['serein-public/release-files.json']) + [name]
            files['serein-public/release-files.json'] = json.dumps(names).encode()
    if kind in ('symlink', 'case_collision', 'private'):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for name, content in files.items():
                info = zipfile.ZipInfo(name)
                if kind == 'symlink' and name.endswith('/link'):
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, content)
    data = buffer.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    release['asset']['digest'] = 'sha256:' + sha
    network(monkeypatch, module, release, data, f'{sha}  {release["asset"]["name"]}'.encode())
    with pytest.raises(ValueError):
        module.prepare(release, tmp_path)


def installation(root):
    files = {'release-files.json': b'', 'README.md': b'old source', 'obsolete.py': b'old module'}
    files['release-files.json'] = json.dumps(sorted(files)).encode()
    for name, data in files.items():
        (root / name).write_bytes(data)
    for name in ('deploy/config.toml', 'deploy/.env', 'deploy/runtime/serein.db',
                 'deploy/secrets/api-token', 'deploy/installation.json', 'my-notes.txt'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'keep me')
    return files


def test_apply_preserves_state_and_removes_only_obsolete_manifest_files(monkeypatch, tmp_path):
    module = updater()
    root = tmp_path / 'installed'; root.mkdir()
    installation(root)
    stage = tmp_path / 'stage'; stage.mkdir()
    release, data, checksum = package(module, extra={'README.md': b'new source'})
    network(monkeypatch, module, release, data, checksum)
    staged, names, _ = module.prepare(release, stage)
    all_names, obsolete = module.plan_files(root, names)
    backup = module.backup_sources(root, all_names)
    module.apply_files(root, staged, names, obsolete, backup)
    assert (root / 'README.md').read_bytes() == b'new source'
    assert not (root / 'obsolete.py').exists()
    assert (root / 'deploy/runtime/serein.db').read_bytes() == b'keep me'
    assert (root / 'deploy/config.toml').read_bytes() == b'keep me'
    assert (root / 'deploy/.env').read_bytes() == b'keep me'
    assert (root / 'deploy/secrets/api-token').read_bytes() == b'keep me'
    assert (root / 'my-notes.txt').read_bytes() == b'keep me'
    with zipfile.ZipFile(backup) as archive:
        assert archive.read('README.md') == b'old source'
        assert all(not name.startswith('deploy/') for name in archive.namelist())


def test_failed_file_replacement_restores_all_old_sources(monkeypatch, tmp_path):
    module = updater(); root = tmp_path / 'installed'; root.mkdir()
    files = installation(root)
    stage = tmp_path / 'stage'; stage.mkdir()
    release, data, checksum = package(module, extra={'README.md': b'new source'})
    network(monkeypatch, module, release, data, checksum)
    staged, names, _ = module.prepare(release, stage)
    all_names, obsolete = module.plan_files(root, names)
    backup = module.backup_sources(root, all_names)
    real = module.replace_file
    calls = 0
    def fail_once(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('disk write failed')
        return real(*args)
    monkeypatch.setattr(module, 'replace_file', fail_once)
    with pytest.raises(OSError):
        module.apply_files(root, staged, names, obsolete, backup)
    assert all((root / name).read_bytes() == data for name, data in files.items())
    assert not (root / 'release-version.json').exists()


@pytest.mark.parametrize('failure', ['', 'download', 'backup', 'build', 'start'])
def test_complete_update_order_and_failure_boundaries(monkeypatch, tmp_path, failure):
    module = updater(); installation(tmp_path)
    release, data, checksum = package(module, extra={'README.md': b'new source'})
    network(monkeypatch, module, release, data, checksum)
    monkeypatch.setattr(module, 'latest_release', lambda: release)
    calls = []
    def step(name):
        calls.append(name)
        if name == failure:
            raise OSError(name + ' failed')
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content)
    manager = types.SimpleNamespace(ROOT=tmp_path, choose=lambda *a, **kw: '1', ensure_tools=lambda: None,
        service_action=lambda action: step('stop'), backup_runtime=lambda: step('backup'), private_file=write)
    fresh = types.SimpleNamespace(ensure_tools=lambda: None, build_runtime=lambda **kw: step('build'),
        service_action=lambda action: step('start' if action == 'up' else 'stop-new'), check_memory=lambda: step('health'))
    monkeypatch.setattr(module, 'load_manager', lambda root: fresh)
    monkeypatch.setattr(module, 'backup_runtime', lambda *a: step('backup'))
    if failure == 'download':
        monkeypatch.setattr(module, 'prepare', lambda *a: step('download'))
    if failure:
        with pytest.raises(OSError):
            module.run_update(manager)
        assert 'health' not in calls
        if failure == 'download':
            assert calls == ['download'] and (tmp_path / 'README.md').read_bytes() == b'old source'
        elif failure == 'backup':
            assert calls == ['stop', 'backup'] and (tmp_path / 'README.md').read_bytes() == b'old source'
        else:
            assert calls[:3] == ['stop', 'backup', 'build']
            if failure == 'start':
                assert calls[-1] == 'stop-new'
    else:
        assert module.run_update(manager)
        assert calls == ['stop', 'backup', 'build', 'start', 'health']
        state = json.loads((tmp_path / 'deploy/update-state.json').read_text())
        assert state['phase'] == 'complete'
        calls.clear()
        assert not module.run_update(manager) and calls == []
    assert (tmp_path / 'deploy/runtime/serein.db').read_bytes() == b'keep me'


def test_local_file_collision_stops_before_overwrite(tmp_path):
    module = updater(); installation(tmp_path)
    (tmp_path / 'new.py').write_bytes(b'user file')
    with pytest.raises(ValueError, match='重名'):
        module.plan_files(tmp_path, ['release-files.json', 'new.py'])
    assert (tmp_path / 'new.py').read_bytes() == b'user file'


def test_version_comparison_handles_multi_digit_release_candidates():
    module = updater()
    assert module.version('v0.1.0-rc9') < module.version('0.1.0-rc65') < module.version('v0.1.0')


def test_latest_rejects_foreign_asset_url(monkeypatch):
    module = updater(); release, _, _ = package(module)
    metadata = {'tag_name': release['tag'], 'assets': [release['asset'], release['checksum']]}
    release['asset']['browser_download_url'] = 'https://example.org/foreign.zip'
    monkeypatch.setattr(module, 'fetch', lambda *a: json.dumps(metadata).encode())
    with pytest.raises(ValueError, match='上游仓库'):
        module.latest_release()


def test_real_runtime_backup_while_installer_lock_is_held(tmp_path):
    import tarfile
    from serein.file_lock import exclusive_lock
    module = updater()
    deploy = tmp_path / 'deploy'; (deploy / 'runtime').mkdir(parents=True)
    (deploy / 'runtime/serein.db').write_bytes(b'synthetic-db')
    with exclusive_lock(deploy / 'runtime/installer.lock'):
        backup = module.backup_runtime(Path(__file__).parents[1], deploy)
    with tarfile.open(backup) as archive:
        assert archive.extractfile('runtime/serein.db').read() == b'synthetic-db'
        assert 'runtime/installer.lock' not in archive.getnames()


@pytest.mark.parametrize('selection', ['r', '0'])
def test_cancel_and_skip_data_backup(monkeypatch, tmp_path, selection):
    module = updater(); installation(tmp_path)
    release, data, checksum = package(module)
    network(monkeypatch, module, release, data, checksum)
    monkeypatch.setattr(module, 'latest_release', lambda: release)
    calls = []
    manager = types.SimpleNamespace(ROOT=tmp_path, choose=lambda *a, **kw: selection,
        ensure_tools=lambda: None, service_action=lambda action: calls.append(action),
        private_file=lambda path, text: path.write_text(text))
    fresh = types.SimpleNamespace(ensure_tools=lambda: None, build_runtime=lambda **kw: calls.append('build'),
        service_action=lambda action: calls.append(action), check_memory=lambda: None)
    monkeypatch.setattr(module, 'load_manager', lambda root: fresh)
    monkeypatch.setattr(module, 'backup_runtime', lambda *a: pytest.fail('Data backup must be skipped'))
    assert module.run_update(manager) == (selection == '0')
    assert calls == ([] if selection == 'r' else ['stop', 'build', 'up'])


def test_interrupted_same_version_can_retry_and_hand_edits_are_preserved(monkeypatch, tmp_path):
    module = updater(); installation(tmp_path)
    release, data, checksum = package(module)
    network(monkeypatch, module, release, data, checksum)
    monkeypatch.setattr(module, 'latest_release', lambda: release)
    (tmp_path / 'release-version.json').write_text(json.dumps({'version': release['tag']}))
    # A same-version update must not be mistaken for success after a crash.
    (tmp_path / 'deploy/update-state.json').write_text(json.dumps({'version': release['tag'], 'phase': 'building'}))
    calls = []
    manager = types.SimpleNamespace(ROOT=tmp_path, choose=lambda *a, **kw: calls.append('offered-retry') or 'r')
    assert not module.run_update(manager)
    assert calls == ['offered-retry']
    hashes = module.source_hashes(tmp_path)
    (tmp_path / 'deploy/update-state.json').write_text(json.dumps({'source_hashes': hashes}))
    (tmp_path / 'README.md').write_text('user edited source')
    with pytest.raises(ValueError, match='被修改'):
        module.plan_files(tmp_path, ['release-files.json', 'README.md'])
    assert (tmp_path / 'README.md').read_text() == 'user edited source'


def test_real_release_manifest_and_archive_are_accepted(monkeypatch, tmp_path):
    import sys
    module = updater(); root = Path(__file__).parents[1]
    archive = tmp_path / 'review.zip'
    subprocess.run([sys.executable, str(root / 'scripts/release.py'), str(archive)], check=True)
    tag = 'v' + json.loads((root / 'release-version.json').read_text())['version']
    name = f'serein-public-{tag[1:]}.zip'
    data = archive.read_bytes(); sha = hashlib.sha256(data).hexdigest()
    release = {'tag': tag, 'asset': {'name': name, 'browser_download_url': 'archive', 'digest': 'sha256:' + sha},
               'checksum': {'browser_download_url': 'checksum'}}
    network(monkeypatch, module, release, data, f'{sha}  {name}\n'.encode())
    staged, names, digest = module.prepare(release, tmp_path)
    assert digest == sha
    assert 'web/src/data/memory.js' in names
    assert (staged / 'scripts/upstream_update.py').read_bytes() == (root / 'scripts/upstream_update.py').read_bytes()


def test_power_loss_during_replacement_can_resume_without_accepting_other_edits(monkeypatch, tmp_path):
    module = updater(); root = tmp_path / 'installed'; root.mkdir(); installation(root)
    stage = tmp_path / 'stage'; stage.mkdir()
    release, data, checksum = package(module, extra={'README.md': b'new source'})
    network(monkeypatch, module, release, data, checksum)
    staged, names, _ = module.prepare(release, stage)
    before = module.source_hashes(root)
    (root / 'deploy/update-state.json').write_text(json.dumps({'phase': 'applying', 'source_hashes': before}))
    # Simulate an OS kill after some files, including the manifest, were replaced.
    for name in ('release-files.json', 'release-version.json', 'README.md'):
        (root / name).write_bytes((staged / name).read_bytes())
    all_names, obsolete = module.plan_files(root, names, staged)
    assert 'obsolete.py' in obsolete and 'release-version.json' in all_names
    (root / 'README.md').write_bytes(b'a separate user edit')
    with pytest.raises(ValueError, match='额外修改'):
        module.plan_files(root, names, staged)


def test_bootstrap_uses_existing_instance_and_holds_its_installer_lock(monkeypatch, tmp_path):
    from serein.file_lock import exclusive_lock
    module = updater()
    manager = types.SimpleNamespace(ROOT=tmp_path)
    monkeypatch.setattr(module, 'load_manager', lambda root: manager)
    monkeypatch.setattr(module, 'load_source', lambda path: types.SimpleNamespace(exclusive_lock=exclusive_lock))
    def update(active):
        assert active is manager
        with pytest.raises(RuntimeError, match='已有任务'):
            with exclusive_lock(tmp_path / 'deploy/runtime/installer.lock'):
                pass
    monkeypatch.setattr(module, 'run_update', update)
    assert module.main(['--root', str(tmp_path)]) == 0
    with exclusive_lock(tmp_path / 'deploy/runtime/installer.lock'):
        pass


def test_bootstrap_does_not_update_if_another_installer_is_running(monkeypatch, tmp_path):
    from serein.file_lock import exclusive_lock
    module = updater()
    monkeypatch.setattr(module, 'load_manager', lambda root: types.SimpleNamespace(ROOT=root))
    monkeypatch.setattr(module, 'load_source', lambda path: types.SimpleNamespace(exclusive_lock=exclusive_lock))
    monkeypatch.setattr(module, 'run_update', lambda manager: pytest.fail('Cannot run two installers'))
    with exclusive_lock(tmp_path / 'deploy/runtime/installer.lock'):
        assert module.main(['--root', str(tmp_path)]) == 1
