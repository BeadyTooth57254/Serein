import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def shortcut():
    path=Path(__file__).parents[1]/'scripts/install_shortcut.py'
    spec=importlib.util.spec_from_file_location('shortcut',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_bash_entry_and_generated_launcher_use_lf():
    root=Path(__file__).parents[1]
    assert b'\r\n' not in (root/'scripts/one_click.sh').read_bytes()
    assert b'\r\n' not in shortcut().POSIX.encode()
    assert '*.sh text eol=lf' in (root/'.gitattributes').read_text()


def test_registration_is_idempotent_and_does_not_replace_other_commands(tmp_path,monkeypatch):
    module=shortcut();monkeypatch.setattr(module.shutil,'which',lambda name:None)
    target,changed=module.write_launcher(tmp_path)
    assert changed
    before=target.stat().st_mtime_ns
    assert module.write_launcher(tmp_path)==(target,False)
    assert target.stat().st_mtime_ns==before
    target.write_text('another application')
    with pytest.raises(ValueError,match='未覆盖'):module.write_launcher(tmp_path)
    assert target.read_text()=='another application'
    monkeypatch.setattr(module.shutil,'which',lambda name:str(tmp_path/'other/se'))
    with pytest.raises(ValueError,match='未覆盖'):module.write_launcher(tmp_path/'new')
    assert not (tmp_path/'new').exists()


def test_shell_path_preserves_profiles_and_only_adds_one_entry(tmp_path,monkeypatch):
    module=shortcut();monkeypatch.setenv('PATH','/usr/bin')
    original='alias existing="echo preserved"\n';(tmp_path/'.bashrc').write_text(original)
    directory=tmp_path/"with space's/bin"
    module.shell_path(directory,tmp_path);module.shell_path(directory,tmp_path)
    for name in ('.profile','.bashrc','.zshrc'):
        text=(tmp_path/name).read_text()
        assert text.count('# Serein se command')==1
    assert (tmp_path/'.bashrc').read_text().startswith(original)


@pytest.mark.skipif(os.name!='nt',reason='Windows command entry')
def test_windows_se_selects_current_folder_and_refuses_unrelated_folder(tmp_path,monkeypatch):
    module=shortcut();monkeypatch.setattr(module.shutil,'which',lambda name:None)
    target,_=module.write_launcher(tmp_path/'bin',windows=True)
    for name in ('first with space','second'):
        root=tmp_path/name;(root/'scripts').mkdir(parents=True)
        (root/'release-files.json').write_text('[]');(root/'scripts/manage.py').write_text('')
        (root/'scripts/one_click.ps1').write_text('Write-Output (Split-Path $PSScriptRoot -Parent)')
        result=subprocess.run([os.environ['COMSPEC'],'/d','/c',str(target)],cwd=root,capture_output=True,text=True)
        assert result.returncode==0 and str(root) in result.stdout
    result=subprocess.run([os.environ['COMSPEC'],'/d','/c',str(target)],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode==2 and 'Serein directory' in result.stderr


@pytest.mark.skipif(os.name=='nt' or not shutil.which('bash'),reason='POSIX shell entry')
def test_posix_se_selects_current_folder_and_refuses_unrelated_folder(tmp_path,monkeypatch):
    module=shortcut();monkeypatch.setattr(module.shutil,'which',lambda name:None)
    target,_=module.write_launcher(tmp_path/'bin')
    for name in ('first with space','second'):
        root=tmp_path/name;(root/'scripts').mkdir(parents=True)
        (root/'release-files.json').write_text('[]');(root/'scripts/manage.py').write_text('')
        (root/'scripts/one_click.sh').write_text('pwd\n')
        result=subprocess.run([str(target)],cwd=root,capture_output=True,text=True)
        assert result.returncode==0 and str(root) in result.stdout
    result=subprocess.run([str(target)],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode==2 and 'Serein directory' in result.stderr
