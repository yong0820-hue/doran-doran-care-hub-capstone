"""Run only the demo-project Auth/RTDB emulators with isolated tool state.

Requires an externally installed firebase-tools 15.31.0 and its database JAR.
Never installs, signs in, deploys, imports real data, or reads user credentials.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PROJECT = 'demo-doran-memory-local'

# Applied to the CLI and its Node child. No request to a cloud API is permitted.
# Java runs only the local database emulator with no credential environment.
NODE_GUARD = r"""
const net = require('node:net');
const dns = require('node:dns');
const fs = require('node:fs');
const children = require('node:child_process');
const spawn = children.spawn;
children.spawn = function(command, args, options) {
  const child = spawn.call(this, command, args, options);
  if (Array.isArray(args) && args.includes(process.env.DORAN_EMULATOR_JAR)) {
    child.once('spawn', () => fs.appendFileSync(process.env.DORAN_EMULATOR_PIDS, String(child.pid) + '\n'));
  }
  return child;
};
const allowed = host => ['127.0.0.1','localhost','::1'].includes(host);
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function(...args) {
  let opts = args[0];
  if (Array.isArray(opts)) opts = opts[0];
  if (typeof opts === 'object' && opts !== null) {
    if (!opts.path && !allowed(opts.host || 'localhost')) throw new Error('EXTERNAL_NETWORK_BLOCKED');
  } else if (typeof opts === 'number') {
    if (!allowed(typeof args[1] === 'string' ? args[1] : 'localhost')) throw new Error('EXTERNAL_NETWORK_BLOCKED');
  } else if (typeof opts === 'string' && /^\d+$/.test(opts)) {
    if (!allowed(typeof args[1] === 'string' ? args[1] : 'localhost')) throw new Error('EXTERNAL_NETWORK_BLOCKED');
  }
  return connect.apply(this, args);
};
const lookup = dns.lookup;
dns.lookup = function(host, ...args) {
  if (!allowed(host)) {
    const callback = args[args.length - 1];
    const error = new Error('EXTERNAL_DNS_BLOCKED');
    if (typeof callback === 'function') return process.nextTick(callback, error);
    throw error;
  }
  return lookup.call(this, host, ...args);
};
"""


def require(condition, code):
    if not condition:
        raise ValueError(code)


def cleanup_owned_emulators(pid_file, jar):
    """Windows CLI SIGINT can leave its Java child behind; verify before stopping."""
    if os.name != 'nt' or not pid_file.exists():
        return
    for line in pid_file.read_text().splitlines():
        require(line.isdigit(), 'INVALID_CHILD_PID_RECORD')
        # PID originates from this run's spawn hook; also verify the exact JAR
        # command line before stopping it, to avoid touching another process.
        # On Windows the CLI may spawn a command wrapper; Java is its direct
        # child, so the wrapper PID alone can already have exited here.
        script = "$ErrorActionPreference='Stop'; $children = @(Get-CimInstance Win32_Process -Filter '(ProcessId=" + line + " OR ParentProcessId=" + line + ")'); "
        script += "foreach ($child in $children) { if ($child.Name -eq 'java.exe' -and $child.CommandLine.Contains('" + str(jar).replace("'", "''") + "')) { "
        script += "& taskkill.exe /PID $child.ProcessId /F; if ($LASTEXITCODE -ne 0) { exit 3 } } }; exit 0"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script],
                                capture_output=True, text=True)
        require(result.returncode == 0, 'OWNED_EMULATOR_CLEANUP_FAILED')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools-dir', required=True)
    parser.add_argument('--python', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    tools, python, out = (Path(p).absolute() for p in (args.tools_dir, args.python, args.out))
    require(not out.exists() and out.parent.is_dir(), 'OUTPUT_MUST_BE_NEW')
    require(not out.is_relative_to(HERE.parents[1]), 'OUTPUT_MUST_BE_OUTSIDE_REPOSITORY')
    cli_root = tools / 'node_modules/firebase-tools'
    require(json.loads((cli_root / 'package.json').read_text())['version'] == '15.31.0', 'CLI_VERSION_MISMATCH')
    info = json.loads((cli_root / 'lib/emulator/downloadableEmulatorInfo.json').read_text())['database']
    jar = tools / 'emulators' / info['downloadPathRelativeToCacheDir']
    require(hashlib.sha256(jar.read_bytes()).hexdigest() == info['expectedChecksumSHA256'], 'EMULATOR_HASH_MISMATCH')
    require(python.is_file(), 'PYTHON_UNAVAILABLE')
    node = shutil.which('node')
    require(node is not None and shutil.which('java') is not None, 'NODE_OR_JAVA_UNAVAILABLE')
    config = json.loads((HERE / 'firebase.emulator.json').read_text())
    require(config['emulators']['auth'] == {'host': '127.0.0.1', 'port': 9099}
            and config['emulators']['database'] == {'host': '127.0.0.1', 'port': 9000}, 'EMULATOR_CONFIG_MISMATCH')
    # emulators:exec accepts one shell command. Restrict its path alphabet, then
    # quote the three path arguments; none contains a credential or token.
    for value in (str(node), str(python), str(HERE)):
        require(not any(c in value for c in '"\r\n&|<>^%`$!'), 'UNSAFE_COMMAND_PATH')
    command = f'"{node}" "{HERE / "firebase_emulator_tests.mjs"}" --python "{python}"'
    print(json.dumps({'project': PROJECT, 'auth': '127.0.0.1:9099', 'database': '127.0.0.1:9000',
                      'cli_version': '15.31.0', 'database_version': info['version'],
                      'real_cloud_allowed': False, 'dry_run': args.dry_run, 'output': str(out)}))
    if args.dry_run:
        return 0
    for port in (9099, 9000, 4400, 4500):
        # Never reuse another developer's running emulator or its local data.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)
            require(probe.connect_ex(('127.0.0.1', port)) != 0, 'EMULATOR_PORT_ALREADY_IN_USE')
    out.mkdir(mode=0o700)
    private_home = out / 'empty-home'
    private_home.mkdir()
    guard = out / 'loopback-only.cjs'
    guard.write_text(NODE_GUARD, encoding='utf-8')
    keep = {'PATH','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','TEMP','TMP','NUMBER_OF_PROCESSORS'}
    env = {k: os.environ[k] for k in os.environ if k.upper() in keep}
    env.update(HOME=str(private_home), USERPROFILE=str(private_home), APPDATA=str(private_home),
               LOCALAPPDATA=str(private_home), XDG_CONFIG_HOME=str(private_home), CI='true',
               FIREBASE_EMULATORS_PATH=str(tools / 'emulators'),
               DORAN_EMULATOR_JAR=str(jar), DORAN_EMULATOR_PIDS=str(out / 'owned-java-pids.txt'),
               NODE_OPTIONS='--require "' + guard.as_posix() + '"')
    cli = cli_root / 'lib/bin/firebase.js'
    argv = [node, str(cli), 'emulators:exec', '--only', 'auth,database', '--project', PROJECT,
            '--config', str(HERE / 'firebase.emulator.json'), '--non-interactive', command]
    try:
        with (out / 'emulator-checks.log').open('x', encoding='utf-8') as log:
            result = subprocess.run(argv, cwd=out, env=env, stdout=log, stderr=subprocess.STDOUT)
    finally:
        cleanup_owned_emulators(out / 'owned-java-pids.txt', jar)
    print('EMULATOR_CHECKS_EXIT:', result.returncode)
    print('Log:', out / 'emulator-checks.log')
    return result.returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError):
        print('REFUSED: local emulator prerequisites or configuration invalid', file=sys.stderr)
        raise SystemExit(2)
