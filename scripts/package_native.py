"""Package the native build and bundled dependency license notices."""
import argparse
from importlib import metadata
from pathlib import Path
import shutil
import sys
import tarfile
import zipfile
from codex_runner import __version__

parser = argparse.ArgumentParser(); parser.add_argument('--platform', required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
name = f'codex-runner-{__version__}-{args.platform}'
staging = root / 'native-dist' / name
staging.mkdir(parents=True, exist_ok=True)
exe = 'codex-runner.exe' if sys.platform == 'win32' else 'codex-runner'
shutil.copy2(root / 'dist' / exe, staging / exe)
if sys.platform == 'win32':
    shutil.copy2(root / 'dist/codex-runner-background.exe', staging / 'codex-runner-background.exe')
for filename in ['README.md', 'LICENSE']:
    shutil.copy2(root / filename, staging / filename)
notices = staging / 'licenses'; notices.mkdir(exist_ok=True)
python_license = Path(sys.base_prefix) / ('LICENSE.txt' if sys.platform == 'win32' else 'LICENSE')
if not python_license.is_file():
    candidates = list(Path(sys.base_prefix).glob('lib/python*/LICENSE.txt'))
    python_license = next(iter(candidates), python_license)
if python_license.is_file():
    shutil.copy2(python_license, notices / 'Python-LICENSE.txt')
else:
    import builtins
    builtins.license._Printer__setup()
    (notices / 'Python-LICENSE.txt').write_text('\n'.join(builtins.license._Printer__lines), encoding='utf-8')
for package in ['pyinstaller', 'windows-curses', 'discord.py', 'aiohttp', 'aiohappyeyeballs', 'aiosignal', 'attrs', 'frozenlist', 'multidict', 'propcache', 'yarl', 'idna', 'typing_extensions']:
    try:
        distribution = metadata.distribution(package)
    except metadata.PackageNotFoundError:
        continue
    target = notices / package; target.mkdir(exist_ok=True)
    for file in distribution.files or []:
        if any(part.lower().startswith(('license', 'copying', 'notice')) for part in file.parts):
            source = distribution.locate_file(file)
            if source.is_file(): shutil.copy2(source, target / source.name)
(staging / 'THIRD_PARTY_NOTICES.txt').write_text(
    'This distribution includes CPython (PSF License). Windows builds include windows-curses '
    '(BSD license) and PDCurses (public domain). The executable bootloader is PyInstaller '
    '(GPL with bootloader exception permitting distribution of this MIT application). '
    'Discord support includes discord.py (MIT) and aiohttp with its dependencies; see their bundled license files. '
    'See licenses/. Codex itself is installed separately and is not bundled.\n', encoding='utf-8')
if sys.platform == 'win32':
    with zipfile.ZipFile(Path(str(staging) + '.zip'), 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in staging.rglob('*'):
            if path.is_file(): archive.write(path, path.relative_to(staging))
    shutil.copy2(staging / exe, staging.parent / f'codex-runner-{__version__}-{args.platform}.exe')
else:
    with tarfile.open(str(staging) + '.tar.gz', 'w:gz') as archive:
        archive.add(staging, arcname=name)
shutil.rmtree(staging)
