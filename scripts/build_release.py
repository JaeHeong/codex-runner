#!/usr/bin/python3
"""Build Codex Runner release artifacts for the shared package archive."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.search(r"__version__ = '([^']+)'", (ROOT / 'src/codex_runner/__init__.py').read_text())[1]
HOST = 'https://apt.jaehyeong.com'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def build_package(output):
    staging = ROOT / 'build' / ('codex-runner-' + VERSION)
    if staging.exists():
        shutil.rmtree(staging)
    lib = staging / 'usr/lib/codex-runner/codex_runner'
    lib.parent.mkdir(parents=True)
    shutil.copytree(ROOT / 'src/codex_runner', lib, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for source, target in [('packaging/codex-runner', 'usr/bin/codex-runner'),
                           ('packaging/codex-runner.service', 'usr/lib/systemd/user/codex-runner.service'),
                           ('README.md', 'usr/share/doc/codex-runner/README.md'),
                           ('LICENSE', 'usr/share/doc/codex-runner/copyright')]:
        p = staging / target; p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / source, p)
    (staging / 'usr/bin/codex-runner').chmod(0o755)
    (staging / 'DEBIAN').mkdir()
    control = f'''Package: codex-runner
Version: {VERSION}
Section: utils
Priority: optional
Architecture: all
Maintainer: Codex Runner maintainers <packages@jaehyeong.com>
Depends: python3 (>= 3.10)
Homepage: {HOST}/
Description: Resume local Codex sessions after subscription quota resets
 Per-user persistent queue with quota-aware waits, completion detection,
 pause controls, private logs and a systemd user service.
 Codex CLI and a ChatGPT login must be configured separately by each user.
'''
    (staging / 'DEBIAN/control').write_text(control)
    # Package ownership is normalized without building or running a root daemon.
    for p in staging.rglob('*'):
        if p.is_dir():
            p.chmod(0o755)
        elif p != staging / 'usr/bin/codex-runner':
            p.chmod(0o644)
    deb = output / f'codex-runner_{VERSION}_all.deb'
    run('dpkg-deb', '--root-owner-group', '-Zxz', '--build', str(staging), str(deb))
    return deb



def main():
    parser = argparse.ArgumentParser(description='Build Codex Runner artifacts; publication is handled by tools/package-archive')
    parser.parse_args()
    output = ROOT / 'dist'; output.mkdir(parents=True, exist_ok=True)
    deb = build_package(output)
    archive = output / f'codex-runner-{VERSION}.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for name in ['src', 'packaging', 'scripts', 'tests', 'README.md', 'LICENSE', 'pyproject.toml']:
            tar.add(ROOT / name, arcname='codex-runner-' + VERSION + '/' + name,
                    filter=lambda x: None if '__pycache__' in x.name or x.name.endswith('.pyc') else x)
    print('Built:', deb, archive)
    print('Add these artifacts with tools/package-archive/archive.py, then publish the shared archive.')


if __name__ == '__main__':
    main()
