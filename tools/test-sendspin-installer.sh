#!/usr/bin/env bash
# Exercise the installer with mocked downloads, packages, and service commands.
set -euo pipefail
cd "$(dirname "$0")/.."
bash -n tools/install-sendspin-service.sh
python3 - <<'PY'
import json
import os
from pathlib import Path
import pty
import subprocess
import tempfile

source = Path('tools/install-sendspin-service.sh').read_text()
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    script = root / 'installer.sh'
    # Simulate a systemd host without changing the real host or production script.
    script.write_text(source.replace('/run/systemd/system', directory))
    mock = root / 'mock'
    mock.write_text('''#!/usr/bin/env python3
import hashlib, json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as log:
    log.write(json.dumps([name, *args]) + '\\n')
if name == 'sudo':
    os.execvp(args[0], args)
elif name == 'dpkg':
    if '--compare-versions' in args:
        os.execv('/usr/bin/dpkg', ['dpkg', *args])
    print(os.environ.get('TEST_ARCH', 'arm64'))
elif name == 'dpkg-query':
    version = os.environ.get('INSTALLED_VERSION')
    if not version: sys.exit(1)
    print('install ok installed ' + version)
elif name == 'apt' and os.environ.get('APT_FAIL'):
    sys.exit(100)
elif name == 'systemctl' and args[0] == 'is-active' and os.environ.get('SERVICE_FAIL'):
    sys.exit(1)
elif name == 'curl':
    if '--write-out' in args:
        print('https://github.com/dutchdronesquad/rh-race-voice/releases/tag/v1.2.3', end='')
    elif any('api.github.com' in arg for arg in args):
        releases = []
        for tag, prerelease, complete in [('v2.0.0-beta', True, True), ('v1.2.4', False, False), ('v1.2.3', False, True), ('v1.2.2', False, True)]:
            package = 'sendspin-service_' + tag[1:] + '_arm64.deb'
            assets = [{'name': package}, {'name': package + '.sha256'}] if complete else []
            releases.append(dict(tag_name=tag, prerelease=prerelease, draft=False, assets=assets))
        print(json.dumps(releases))
    else:
        if os.environ.get('DOWNLOAD_FAIL'): sys.exit(22)
        target = pathlib.Path(args[args.index('-o') + 1])
        if target.name == 'checksum':
            checksum = '0' * 64 if os.environ.get('BAD_CHECKSUM') else hashlib.sha256(b'package').hexdigest()
            target.write_text(checksum + '  dist/package.deb\\n')
        else:
            target.write_bytes(b'package')
''')
    mock.chmod(0o755)
    for name in ['curl', 'dpkg', 'dpkg-query', 'apt', 'systemctl', 'sudo']:
        (root / name).symlink_to(mock)

    def run(label, args, overrides=None, answer=None, expected=0, action=None):
        log = root / 'calls'
        log.write_text('')
        env = dict(os.environ, PATH=directory + ':' + os.environ['PATH'], CALL_LOG=str(log))
        env.update(overrides or {})
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(
                ['bash', str(script), *args], env=env,
                stdin=slave if answer is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if answer is not None:
                os.write(master, answer.encode())
            stdout, stderr = process.communicate(timeout=15)
        finally:
            os.close(master)
            os.close(slave)
        assert process.returncode == expected, (label, stdout, stderr)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        installs = [call for call in calls if call[0] == 'apt']
        assert bool(installs) == bool(action), (label, calls)
        if action:
            assert action + ' Sendspin service:' in stdout, (label, stdout)
            assert 'Dpkg::Options::=--force-confold' in installs[0], installs
            assert ('--allow-downgrades' in installs[0]) == (action == 'Downgrade'), installs
        print('PASS:', label)
        return stdout, stderr, calls

    run('latest fresh install', ['--latest', '--yes'], action='Install')
    run('pinned amd64 install', ['v1.2.2', '--yes'], {'TEST_ARCH': 'amd64'}, action='Install')
    run('upgrade keeps config', ['--latest', '--yes'], {'INSTALLED_VERSION': '1.2.2'}, action='Update')
    run('explicit downgrade', ['v1.2.2', '--yes'], {'INSTALLED_VERSION': '1.2.3'}, action='Downgrade')
    run('same version is unchanged', ['v1.2.3', '--yes'], {'INSTALLED_VERSION': '1.2.3'})
    stdout, stderr, calls = run('menu selects available stable version', [], answer='2\ny\n', action='Install')
    assert any('/download/v1.2.2/sendspin-service_1.2.2_arm64.deb' in arg for call in calls if call[0] == 'curl' for arg in call)
    assert 'v2.0.0-beta' not in stderr and 'v1.2.4' not in stderr
    run('menu cancel', [], answer='3\n')
    run('decline existing installation update', ['--latest'], {'INSTALLED_VERSION': '1.2.2'}, answer='n\n')
    run('unattended needs version', ['--yes'], expected=1)
    run('unattended needs confirmation', ['--latest'], expected=1)
    run('unsupported architecture', ['--latest', '--yes'], {'TEST_ARCH': 'armhf'}, expected=1)
    run('invalid checksum blocks apt', ['--latest', '--yes'], {'BAD_CHECKSUM': '1'}, expected=1)
    run('failed download blocks apt', ['--latest', '--yes'], {'DOWNLOAD_FAIL': '1'}, expected=22)
    run('invalid tag blocks apt', ['../invalid', '--yes'], expected=1)
    run('conflicting release options', ['--latest', 'v1.2.3'], expected=1)
    run('apt failure stops installation', ['--latest', '--yes'], {'APT_FAIL': '1'}, expected=100, action='Install')
    stdout, _, _ = run('inactive service fails', ['--latest', '--yes'], {'SERVICE_FAIL': '1'}, expected=1, action='Install')
    assert 'is installed and running' not in stdout
    run('help', ['--help'])
PY
