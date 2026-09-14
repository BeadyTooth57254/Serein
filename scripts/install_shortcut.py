"""Install a cwd-based se launcher without binding it to one instance."""
import os
from pathlib import Path
import shlex
import shutil
import sys

MARKER='Serein directory launcher v1'
POSIX=r'''#!/bin/sh
# Serein directory launcher v1
if [ ! -f ./release-files.json ] || [ ! -f ./scripts/manage.py ] || [ ! -f ./scripts/one_click.sh ]; then
    printf '%s\n' 'Please enter a Serein directory before running se.' >&2
    exit 2
fi
exec bash ./scripts/one_click.sh "$@"
'''
WINDOWS='''@echo off
rem Serein directory launcher v1
if not exist "release-files.json" goto wrong_directory
if not exist "scripts\\manage.py" goto wrong_directory
if not exist "scripts\\one_click.ps1" goto wrong_directory
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\\scripts\\one_click.ps1" %*
exit /b %ERRORLEVEL%
:wrong_directory
echo Please enter a Serein directory before running se. 1>&2
exit /b 2
'''


def write_launcher(directory,windows=False):
    target=directory/('se.cmd' if windows else 'se')
    existing=shutil.which('se')
    if existing and Path(existing).resolve()!=target.resolve():
        raise ValueError('已有其他 se 命令，未覆盖：'+existing)
    if target.is_symlink() or target.exists() and MARKER not in target.read_text(errors='replace'):
        raise ValueError('已有其他 se 文件，未覆盖：'+str(target))
    content=(WINDOWS.replace('\n','\r\n') if windows else POSIX).encode()
    if target.exists() and target.read_bytes()==content:return target,False
    directory.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(target.name+'.serein-tmp')
    temporary.write_bytes(content);temporary.chmod(0o755);temporary.replace(target)
    return target,True


def windows_path(directory):
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,'Environment',0,winreg.KEY_READ|winreg.KEY_WRITE) as key:
        try:value,kind=winreg.QueryValueEx(key,'Path')
        except FileNotFoundError:value,kind='',winreg.REG_EXPAND_SZ
        entries=[os.path.normcase(os.path.expandvars(p).rstrip('\\/')) for p in value.split(';') if p]
        if os.path.normcase(str(directory).rstrip('\\/')) in entries:return False
        winreg.SetValueEx(key,'Path',0,kind,value.rstrip(';')+(';' if value else '')+str(directory))
    # Notify new desktop terminals; the parent shell still needs to be reopened.
    import ctypes
    from ctypes import wintypes
    send=ctypes.windll.user32.SendMessageTimeoutW
    send.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPCWSTR,wintypes.UINT,wintypes.UINT,ctypes.POINTER(ctypes.c_size_t)]
    result=ctypes.c_size_t()
    send(0xffff,0x001a,0,'Environment',2,1000,ctypes.byref(result))
    return True


def shell_path(directory,home):
    if str(directory) in os.environ.get('PATH','').split(os.pathsep):return False
    line='export PATH='+shlex.quote(str(directory))+':"$PATH"'
    for name in ('.profile','.bashrc','.zshrc'):
        path=home/name;text=path.read_text() if path.exists() else ''
        if line not in text.splitlines():
            with path.open('a') as file:file.write('\n# Serein se command\n'+line+'\n')
    return True


def install():
    if os.name=='nt':
        base=os.environ.get('LOCALAPPDATA')
        if not base:raise ValueError('未找到 LOCALAPPDATA，未注册快捷命令。')
        directory=Path(base)/'Serein'/'bin'
        target,changed=write_launcher(directory,windows=True)
        changed_path=windows_path(directory)
    else:
        prefix=os.environ.get('PREFIX','')
        directory=Path(prefix)/'bin' if 'com.termux' in prefix else Path('/usr/local/bin') if os.geteuid()==0 else Path.home()/'.local/bin'
        target,changed=write_launcher(directory)
        changed_path=shell_path(directory,Path.home())
    if changed or changed_path:
        print('已注册 se：进入任一 Serein 发行目录后输入 se 即可打开管理菜单。')
        if changed_path:print('PATH 已保存；若当前终端找不到 se，请重新打开终端。')
    return target


if __name__=='__main__':
    try:install()
    except (OSError,ValueError) as error:
        print('se 快捷命令未注册：'+str(error)+'；仍可使用原启动脚本。',file=sys.stderr)
