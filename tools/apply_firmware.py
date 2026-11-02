"""Apply the Mini3 application patch to a matching official source checkout.

No build or device flash is performed. Credential defaults are blanked locally.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def normalized(data):
    text = data.decode('utf-8-sig').replace('\r\n', '\n')
    return re.sub(r'(?m)^(CONFIG_CLOUD_(?:PRODUCT_ID|SECRET_ID)_DEFAULT)=.*$', r'\1=""', text).encode('utf-8')


def safe_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()): raise ValueError('Path escapes the SDK checkout')
    return path


def apply(sdk, check_only=False):
    sdk = sdk.resolve()
    manifest = json.loads((ROOT / 'firmware/baseline.json').read_text(encoding='utf-8'))
    head = subprocess.run(['git','rev-parse','HEAD'],cwd=sdk,capture_output=True,text=True,check=True).stdout.strip()
    if head != manifest['commit']:
        raise RuntimeError('Checkout does not match the documented firmware baseline')
    originals = {}; prepared = {}
    for entry in manifest['modified_files']:
        path = safe_path(sdk, entry['path'])
        originals[entry['path']] = path.read_bytes()
        data = normalized(originals[entry['path']])
        if hashlib.sha256(data).hexdigest() != entry['base_sha256']:
            raise RuntimeError('Target file differs from the expected baseline: ' + entry['path'])
        prepared[entry['path']] = data
    for entry in manifest['overlay_files']:
        if safe_path(sdk, entry['path']).exists():
            raise RuntimeError('Overlay destination already exists; preserve it: ' + entry['path'])
    patch = ROOT / 'firmware/patches/mini3-local-agent.patch'
    if check_only:
        with tempfile.TemporaryDirectory(prefix='mini3-patch-check-') as temporary:
            folder = Path(temporary).resolve()
            for name, data in prepared.items():
                path = safe_path(folder, name); path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(data)
            result = subprocess.run(['git','-c','core.autocrlf=false','apply','--check',str(patch)],cwd=folder,capture_output=True,text=True)
            if result.returncode: raise RuntimeError('Patch validation failed: ' + result.stderr)
        print('Baseline and patch validation passed; SDK checkout unchanged.')
        return
    created = []
    try:
        for name, data in prepared.items(): safe_path(sdk, name).write_bytes(data)
        subprocess.run(['git','-c','core.autocrlf=false','apply','--check',str(patch)],cwd=sdk,check=True)
        subprocess.run(['git','-c','core.autocrlf=false','apply',str(patch)],cwd=sdk,check=True)
        for entry in manifest['overlay_files']:
            target = safe_path(sdk, entry['path']); target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/'firmware/overlay'/entry['path'],target); created.append(target)
        for entry in manifest['modified_files']:
            result_data = safe_path(sdk,entry['path']).read_bytes().replace(b'\r\n', b'\n')
            if hashlib.sha256(result_data).hexdigest() != entry['result_sha256']:
                raise RuntimeError('Post-apply verification failed: '+entry['path'])
        for entry in manifest['overlay_files']:
            if hashlib.sha256(safe_path(sdk,entry['path']).read_bytes()).hexdigest() != entry['sha256']:
                raise RuntimeError('Overlay verification failed: '+entry['path'])
    except BaseException:
        for name,data in originals.items(): safe_path(sdk,name).write_bytes(data)
        for path in created: path.unlink(missing_ok=True)
        raise
    print('Mini3 patch and overlays applied and verified. No firmware has been flashed.')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk',type=Path,required=True)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    apply(args.sdk,args.check)
