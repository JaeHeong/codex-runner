"""Generate Homebrew and WinGet metadata from exact native release bytes."""
import argparse
import hashlib
from pathlib import Path
from codex_runner import __version__

parser = argparse.ArgumentParser(); parser.add_argument('--artifacts', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True); args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
base = f'https://packages.jaehyeong.com/projects/codex-runner/{__version__}/'

def asset(platform, suffix):
    name = f'codex-runner-{__version__}-{platform}.{suffix}'
    candidates = list(args.artifacts.rglob(name))
    if len(candidates) != 1:
        raise SystemExit('Expected exactly one artifact: ' + name)
    return name, hashlib.sha256(candidates[0].read_bytes()).hexdigest()

arm, arm_sha = asset('macos-arm64', 'tar.gz')
intel, intel_sha = asset('macos-x64', 'tar.gz')
windows, win_sha = asset('windows-x64', 'zip')
formula = f'''class CodexRunner < Formula
  desc "Resume local Codex sessions after subscription quota resets"
  homepage "https://github.com/JaeHeong/codex-runner"
  version "{__version__}"
  license "MIT"
  depends_on macos: :sonoma

  on_arm do
    url "{base}{arm}"
    sha256 "{arm_sha}"
  end
  on_intel do
    depends_on macos: :sequoia
    url "{base}{intel}"
    sha256 "{intel_sha}"
  end

  def install
    bin.install "codex-runner"
    pkgshare.install "licenses", "THIRD_PARTY_NOTICES.txt", "LICENSE"
  end

  def caveats
    <<~EOS
      Install Codex CLI and log in with your ChatGPT account separately.
      To start the per-user background service: codex-runner service install
      Open the session menu with: codex-runner
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{{bin}}/codex-runner --version")
  end
end
'''
(args.output/'codex-runner.rb').write_text(formula, encoding='utf-8')
identity = f'PackageIdentifier: JaeHeong.CodexRunner\nPackageVersion: {__version__}\n'
(args.output/'JaeHeong.CodexRunner.yaml').write_text(identity + 'DefaultLocale: en-US\nManifestType: version\nManifestVersion: 1.9.0\n')
(args.output/'JaeHeong.CodexRunner.locale.en-US.yaml').write_text(identity + '''PackageLocale: en-US
Publisher: JaeHeong
PublisherUrl: https://github.com/JaeHeong
PackageName: Codex Runner
PackageUrl: https://github.com/JaeHeong/codex-runner
License: MIT
LicenseUrl: https://github.com/JaeHeong/codex-runner/blob/main/LICENSE
ShortDescription: Resume local Codex sessions after subscription quota resets
Moniker: codex-runner
Tags:
- codex
- cli
- productivity
ManifestType: defaultLocale
ManifestVersion: 1.9.0
''')
(args.output/'JaeHeong.CodexRunner.installer.yaml').write_text(identity + f'''InstallerType: zip
NestedInstallerType: portable
NestedInstallerFiles:
- RelativeFilePath: codex-runner.exe
  PortableCommandAlias: codex-runner
Scope: user
UpgradeBehavior: uninstallPrevious
Installers:
- Architecture: x64
  InstallerUrl: {base}{windows}
  InstallerSha256: {win_sha.upper()}
ManifestType: installer
ManifestVersion: 1.9.0
''')
print('Generated package manager metadata in', args.output)
