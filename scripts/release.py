"""Package the reviewed manifest only; never recurse through runtime directories."""
import argparse
import hashlib
import re
import json
from pathlib import Path
import zipfile

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('output',type=Path)
args=parser.parse_args()
files=json.loads((root/'release-files.json').read_text('utf-8'))
denied={'.git','.local','.runtime','.venv','node_modules','dist','__pycache__','output','.playwright-cli','build'}
private_deploy={'runtime','backups','secrets','venv','config.toml','.env','installation.json','connection-guide.txt'}
# Reviewed samples must not reintroduce private identities or verbatim drafts.
# Store fingerprints instead of repeating the removed personal text in a guard.
private_drafts = ['14c8e19dc0384d8e630c46f8e287ed7b5ea2af09e682315cb8f34f4bcd792ae7', '1c1696319db7417f77d3006f274386e95f2248afa1a8b6f5c25b609441209454', '813625ad8bf4d72518aff145bce86035dc8ee72bd3710ad6466ca06b83fd1a59']
private_names = ['04a69d8da302ef4efbe84fa115e688a02e70a772500ba0e22e673467da47da11', '570764f969e6d4d038c6fe40cb028b11779162b2bc71c97c8da5a38ed0dc8b2b', 'a8b2eca10c180ef011be6ffdb5873c29e8e22e56a37d92792142f48b497a3bfb']
sample_prefixes = ('examples/', 'src/serein/resources/', 'web/codex_agents/')
def inspect_text(name, content):
    if name.endswith('.json'):
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from strings(child)
            elif isinstance(value, list):
                for child in value:
                    yield from strings(child)
        content += '\n' + '\n'.join(strings(json.loads(content)))
    if re.search(r'\bsk-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', content):
        raise ValueError('Release content contains credential material: '+name)
    if name.startswith(sample_prefixes):
        words = re.findall(r'[A-Za-z]+', content.lower())
        for run in re.findall(r'[\u3400-\u9fff]+', content):
            words.extend(run[i:i+2] for i in range(len(run)-1))
        if any(hashlib.sha256(word.encode()).hexdigest() in private_names for word in words):
            raise ValueError('Example contains a private identity: '+name)
        if any(hashlib.sha256(line.strip().encode()).hexdigest() in private_drafts for line in content.splitlines()):
            raise ValueError('Example contains an unredacted private draft: '+name)
        if re.search(r'[A-Z]:[\\/]+Users[\\/]+[0-9]{4,}|/root/\.codex|scene_mig2_[a-f0-9]{16,}|window_[a-f0-9]{24}', content):
            raise ValueError('Example contains a private path or record ID: '+name)

for name in files:
    path=Path(name)
    if path.is_absolute() or '..' in path.parts or denied.intersection(path.parts):
        raise ValueError('Release manifest contains an excluded path')
    if path.parts[0]=='deploy' and len(path.parts)>1 and path.parts[1] in private_deploy:
        raise ValueError('Release manifest contains private installation state')
    target=(root/path).resolve()
    if not target.is_relative_to(root) or not target.is_file() or target.suffix in ('.db','.sqlite','.log'):
        raise ValueError('Release manifest must contain existing source files only')
    if target.suffix.lower() in {'.py','.md','.json','.toml','.yaml','.yml','.jsx','.js','.mjs','.sh','.ps1','.css','.html','.txt'}:
        inspect_text(name, target.read_text(encoding='utf-8'))
args.output.parent.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(args.output,'w',zipfile.ZIP_DEFLATED) as archive:
    for name in files:
        archive.write(root/name,'serein-public/'+name)
print(json.dumps({'status':'packaged','files':len(files),'output':str(args.output)}))
