"""Download pinned model resources from official sherpa-onnx releases."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / 'models'
NAME = 'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
RELEASE = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/'


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): value.update(chunk)
    return value.hexdigest()


def download(url, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.part')
    try:
        with urllib.request.urlopen(url, timeout=60) as source, temporary.open('wb') as output:
            shutil.copyfileobj(source, output, 1024 * 1024)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    expected = json.loads((ROOT / 'tools/model-checksums.json').read_text(encoding='utf-8'))
    MODELS.mkdir(exist_ok=True)
    need_asr = any(not (MODELS/name).is_file() or digest(MODELS/name) != sha
                   for name, sha in expected.items() if name.startswith(NAME + '/'))
    if need_asr:
        archive = MODELS / '.downloads' / (NAME + '.tar.bz2')
        if not archive.exists(): download(RELEASE + archive.name, archive)
        with tarfile.open(archive, 'r:bz2') as source:
            for member in source:
                parts = PurePosixPath(member.name).parts
                if not parts or parts[0] != NAME or '..' in parts or not member.isfile(): continue
                allowed = (len(parts) == 2 and parts[1] in {'model.int8.onnx','tokens.txt','README.md','LICENSE'})
                allowed = allowed or (len(parts) == 3 and parts[1] == 'test_wavs' and parts[2].endswith('.wav'))
                if not allowed or member.size > 300 * 1024 * 1024: continue
                relative = '/'.join(parts)
                target = (MODELS / relative).resolve()
                if not target.is_relative_to(MODELS.resolve()): raise RuntimeError('Unsafe archive path')
                if target.exists():
                    if relative in expected and digest(target) != expected[relative]:
                        raise RuntimeError('Existing model differs; preserve and inspect it: ' + relative)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + '.part')
                try:
                    with source.extractfile(member) as data, temporary.open('wb') as output:
                        shutil.copyfileobj(data, output, 1024 * 1024)
                    if relative in expected and digest(temporary) != expected[relative]:
                        raise RuntimeError('Checksum mismatch: ' + relative)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
    vad = MODELS / 'vad/silero_vad.onnx'
    if not vad.exists(): download(RELEASE + 'silero_vad.onnx', vad)
    for relative, sha in expected.items():
        path = MODELS / relative
        if not path.is_file() or digest(path) != sha: raise RuntimeError('Verification failed: ' + relative)
        print('Verified:', relative)


if __name__ == '__main__': main()
