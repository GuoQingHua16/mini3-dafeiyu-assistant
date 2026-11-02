"""Check tracked release files for accidental local/private artifacts."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'API key': rb'\bsk-[A-Za-z0-9]{16,}',
    'GitHub token': rb'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})',
    'cloud secret default': rb'CONFIG_CLOUD_SECRET_ID_DEFAULT="[A-Za-z0-9_./+=:-]{12,}"',
    'personal Windows path': rb'[A-Za-z]:[\\/]Users[\\/]',
}


def main():
    output = subprocess.run(['git','ls-files','-z'],cwd=ROOT,capture_output=True,check=True).stdout
    names = [name.decode('utf-8') for name in output.split(b'\0') if name]
    errors = []
    for name in names:
        path = ROOT / name
        if any(part in {'runtime','backups','.venv','models','mini3-firmware'} for part in path.relative_to(ROOT).parts):
            errors.append((name,'private/dependency directory'))
        if path.suffix.lower() in {'.mp4','.mov','.onnx','.bin','.elf','.lpk','.log'}:
            errors.append((name,'excluded artifact type'))
        data = path.read_bytes()
        if len(data) > 25 * 1024 * 1024: errors.append((name,'oversized source file'))
        if path.suffix.lower() not in {'.png','.gif','.webp'}:
            for label, pattern in PATTERNS.items():
                if re.search(pattern,data): errors.append((name,label))
    if errors:
        for name, reason in errors: print(name + ': ' + reason)
        raise SystemExit(1)
    print('Release check passed:',len(names),'tracked files; no excluded artifacts or detected credential patterns.')


if __name__ == '__main__': main()
