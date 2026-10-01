"""Isolated staging, offline preflight, explicit online auth-check and startup.

Dry-run never opens credentials, imports runtime modules, or starts networking.
Use the existing venv; this tool never installs packages or changes services.
"""
import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import urlsplit


PREP = Path(__file__).resolve().parent
FILES = frozenset((
    'main.py', 'display_state.py', 'medication_schedule.py', 'display_server.py',
    'radio_presets.py', 'voice_ai.py', 'audio_playback.py',
    'interaction_manager.py', 'conversation_store.py',
    'firebase_commands.py', 'firebase_inactivity.py', 'inactivity_care.py',
    'firebase_medication.py', 'medication_care.py', 'wake_word.py',
    'persistent_memory.py', 'memory_storage.py', 'memory_dispatch.py',
    'memory_reset.py', 'memory_runtime.py', 'display/index.html', 'display/app.js',
    'display/styles.css', 'display/memory_game.js', 'firebase_auth.py',
))
TOP_KEYS = frozenset(('schema_version', 'test_project_id', 'test_database_url',
                     'production_project_id', 'production_database_url',
                     'database_project_confirmed', 'elder_id', 'device_id', 'pi', 'guardian'))
PI_KEYS = frozenset(('code_dir', 'working_directory', 'original_code_dir',
                    'original_state_directory', 'python', 'openai_key_file',
                    'firebase_credential_file', 'firebase_uid'))
PI_OPTIONAL_KEYS = frozenset(('playback_device',))
DEFAULT_PLAYBACK_DEVICE = 'plughw:CARD=USB,DEV=0'
GUARDIAN_KEYS = frozenset(('VITE_FIREBASE_API_KEY', 'VITE_FIREBASE_AUTH_DOMAIN',
                          'VITE_FIREBASE_DATABASE_URL', 'VITE_FIREBASE_PROJECT_ID'))
AUTH_REQUIRED = 'ONLINE_AUTH_REQUIRED: offline validation does not authenticate or authorize a runtime'


class Refused(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def text(value, label):
    require(isinstance(value, str) and value.strip() == value and value
            and '<' not in value and '>' not in value
            and not any(ord(c) < 32 for c in value), label + ': required explicit value')
    return value


def database_url(value):
    value = text(value, 'database URL')
    try:
        url = urlsplit(value)
        host = url.hostname or ''
        port = url.port
    except ValueError:
        raise Refused('database URL: invalid syntax') from None
    require(url.scheme == 'https' and not url.username and not url.password
            and port is None and url.path in ('', '/') and not url.query and not url.fragment,
            'database URL: HTTPS root without auth/query/fragment/port required')
    require(re.fullmatch(r'[a-z0-9-]+\.firebaseio\.com', host) is not None
            or re.fullmatch(r'[a-z0-9-]+\.[a-z0-9-]+\.firebasedatabase\.app', host) is not None,
            'database URL: exact Firebase RTDB hostname required')
    require(value.lower() == value, 'database URL: lowercase canonical form required')
    return 'https://' + host


def clean_environment(environ=None):
    env = os.environ if environ is None else environ
    exact = {'ELDER_ID', 'DEVICE_ID', 'LOCATION_NAME', 'REMINDER_MODE',
             'GOOGLE_APPLICATION_CREDENTIALS', 'CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE',
             'LD_PRELOAD', 'LD_LIBRARY_PATH', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
             'http_proxy', 'https_proxy', 'all_proxy', 'NODE_OPTIONS'}
    prefixes = ('FIREBASE_', 'VITE_', 'OPENAI_', 'AI_', 'DISPLAY_', 'MEDICATION_', 'PYTHON')
    require(not any(k in exact or k.startswith(prefixes) for k in env),
            'INHERITED_CONFIGURATION: use a clean environment; values were not inspected')


def profile_data(data):
    require(isinstance(data, dict) and set(data) == TOP_KEYS, 'profile: unexpected/missing fields')
    require(type(data['schema_version']) is int and data['schema_version'] == 2, 'profile version')
    for key in ('test_project_id', 'production_project_id'):
        value = text(data[key], key)
        require(re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', value) is not None, key + ': invalid')
    require(data['test_project_id'] != data['production_project_id'], 'TEST_EQUALS_PRODUCTION_PROJECT')
    target, production = database_url(data['test_database_url']), database_url(data['production_database_url'])
    require(target != production, 'TEST_EQUALS_PRODUCTION_DATABASE')
    require(data['database_project_confirmed'] is True,
            'PROJECT_DATABASE_OWNERSHIP: operator must confirm exact instance in test project')
    require(data['elder_id'] == 'elder_001' and data['device_id'] == 'device_001',
            'scope must match current Guardian source constants')
    require(isinstance(data['pi'], dict) and PI_KEYS <= set(data['pi'])
            and set(data['pi']) <= PI_KEYS | PI_OPTIONAL_KEYS,
            'pi: unexpected/missing fields')
    for key in PI_KEYS:
        text(data['pi'][key], 'pi.' + key)
    playback_device = text(data['pi'].get('playback_device', DEFAULT_PLAYBACK_DEVICE),
                           'pi.playback_device')
    require(len(playback_device) <= 128 and
            re.fullmatch(r'[A-Za-z0-9_.:,+/=-]+', playback_device) is not None,
            'pi.playback_device: invalid ALSA name')
    require(len(data['pi']['firebase_uid']) <= 128
            and re.search(r'[.#$\[\]/]', data['pi']['firebase_uid']) is None,
            'pi.firebase_uid: explicit device account UID required')
    guardian = data['guardian']
    require(isinstance(guardian, dict) and set(guardian) == GUARDIAN_KEYS, 'guardian: only four public VITE fields allowed')
    for key in GUARDIAN_KEYS:
        text(guardian[key], key)
    require(guardian['VITE_FIREBASE_PROJECT_ID'] == data['test_project_id'], 'GUARDIAN_PROJECT_MISMATCH')
    require(database_url(guardian['VITE_FIREBASE_DATABASE_URL']) == target, 'GUARDIAN_DATABASE_MISMATCH')
    require(guardian['VITE_FIREBASE_AUTH_DOMAIN'] == data['test_project_id'] + '.firebaseapp.com',
            'GUARDIAN_AUTH_DOMAIN_MISMATCH')
    require(re.fullmatch(r'AIza[0-9A-Za-z_-]{35}', guardian['VITE_FIREBASE_API_KEY']) is not None,
            'guardian API key must be a public Firebase web identifier, never a credential/token')
    return data


def load_profile(path):
    path = Path(path)
    require(path.is_absolute() and path.name.endswith('.json') and not path.is_symlink(),
            'profile: explicit absolute public JSON path required')
    require(not any(word in path.name.lower().replace('-', '').replace('_', '')
                    for word in ('credential', 'serviceaccount', 'privatekey')),
            'profile: select the public test profile, never a credential file')
    require(path.is_file() and path.stat().st_size <= 16384, 'profile: missing or oversized')
    # Only this explicitly selected public configuration is read. Credential paths are never opened.
    try:
        return profile_data(json.loads(path.read_text(encoding='utf-8-sig')))
    except (UnicodeError, json.JSONDecodeError):
        raise Refused('profile: invalid JSON') from None


def absolute(path):
    path = Path(path)
    require(path.is_absolute(), 'absolute paths required')
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink paths are refused')
    return path.resolve()


def overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def manifest(directory=PREP):
    hashes = {}
    for filename, count in (('isolated-runtime.sha256', len(FILES)),):
        path = directory / filename
        require(path.is_file() and not path.is_symlink(), 'manifest missing')
        lines = path.read_text(encoding='utf-8-sig').splitlines()
        require(len(lines) == count, 'manifest row count mismatch')
        for line in lines:
            parts = line.split('  ', 1)
            require(len(parts) == 2, 'manifest format')
            digest, name = parts
            require(name in FILES and name not in hashes and re.fullmatch('[0-9a-f]{64}', digest),
                    'manifest allowlist/digest mismatch')
            hashes[name] = digest
    require(set(hashes) == FILES, 'manifest incomplete')
    return hashes


def verify_code(code, hashes, exact=True):
    code = absolute(code)
    require(code.is_dir(), 'code directory missing')
    if exact:
        seen = set()
        for parent, dirs, names in os.walk(code, followlinks=False):
            for name in dirs + names:
                require(not (Path(parent) / name).is_symlink(), 'code contains symlink')
            seen.update((Path(parent) / name).relative_to(code).as_posix() for name in names)
        require(seen == FILES, 'CODE_EXTRA_OR_MISSING_FILES: stage only the allowlisted files')
    for name, digest in hashes.items():
        path = code / name
        require(path.is_file() and not path.is_symlink(), 'runtime file missing')
        body = path.read_bytes()
        require(hashlib.sha256(body).hexdigest() == digest, 'RUNTIME_HASH_MISMATCH: ' + name)
        if name.endswith('.py'):
            compile(body, name, 'exec')
    return code


def credential_present(path, code, state):
    path = absolute(path)
    require(not overlap(path, code) and not overlap(path, state), 'credential path must be separate from code/state')
    if not path.exists():
        return False
    require(path.is_file(), 'credential path must be a regular file')
    if os.name == 'posix':
        require(path.stat().st_uid == os.getuid() and path.stat().st_mode & 0o077 == 0,
                'credential file must be owned by user and private')
    return True


def dependencies():
    issues = []
    for module, package in (('requests', 'requests'), ('dotenv', 'python-dotenv'), ('gtts', 'gtts'),
                            ('openai', 'openai'), ('webrtcvad', 'webrtcvad-wheels')):
        try:
            version = importlib.metadata.version(package)
            present = importlib.util.find_spec(module) is not None
        except (ImportError, importlib.metadata.PackageNotFoundError):
            version, present = '', False
        if not present:
            issues.append('DEPENDENCY_MISSING: ' + package)
        elif package == 'openai':
            parts = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', version)
            if not parts or not (tuple(map(int, parts.groups())) >= (2, 40, 0)
                                  and int(parts[1]) < 3):
                issues.append('DEPENDENCY_VERSION: openai requires >=2.40.0,<3')
        elif package == 'webrtcvad-wheels' and version != '2.0.14':
            issues.append('DEPENDENCY_VERSION: webrtcvad-wheels requires 2.0.14')
    for program in ('arecord', 'mpg123'):
        if shutil.which(program) is None:
            issues.append('EXECUTABLE_MISSING: ' + program)
    return issues


def inspect_profile(data, hashes):
    cfg = data['pi']
    code, state, original_code, original_state = (absolute(cfg[k]) for k in
        ('code_dir', 'working_directory', 'original_code_dir', 'original_state_directory'))
    require(original_code.is_dir() and original_state.is_dir(), 'original code/state paths must be confirmed existing directories')
    require(not overlap(code, state) and all(not overlap(new, old) for new in (code, state)
                                           for old in (original_code, original_state)), 'ORIGINAL_OR_TEST_PATH_OVERLAP')
    require(state.is_dir() and not any(state.iterdir()), 'WORKING_DIRECTORY_NOT_EMPTY')
    require(os.access(state, os.W_OK | os.X_OK), 'state directory not writable')
    verify_code(code, hashes)
    # A standard Linux venv has a symlinked bin/python. Keep its lexical path:
    # resolving it would lose the venv identity; data/code paths remain symlink-free.
    executable = Path(cfg['python'])
    require(executable.is_absolute() and executable.is_file(), 'configured venv Python missing')
    require(os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(os.path.abspath(cfg['python'])),
            'WRONG_PYTHON: invoke the explicitly configured existing venv Python')
    require(not any((state / n).exists() for n in ('.env', '.env.local')), 'DOTENV_FILE_PRESENT')
    target = database_url(data['test_database_url'])
    memory_url = target + '/memories/' + data['elder_id'] + '.json'
    scope = hashlib.sha256(memory_url.encode('utf-8')).hexdigest()
    summary = {
        'code_path': str(code), 'working_directory': str(state),
        'database_hostname': urlsplit(target).hostname, 'project_id': data['test_project_id'],
        'elder_id': data['elder_id'], 'device_id': data['device_id'],
        'audio_path': str(state / 'audio'), 'outbox_path': str(state / 'conversation_outbox'),
        'memory_path': str(state / 'memory_outbox' / scope[:24]),
        'reset_epoch_path': str(state / 'memory_outbox' / scope[:24] / 'reset-epoch.state'),
        'os_lock_path': str(Path(tempfile.gettempdir()) / 'doran-memory-runtime' / (scope + '.lock')),
        'openai_credential_file_present': credential_present(cfg['openai_key_file'], code, state),
        'firebase_credential_file_present': credential_present(cfg['firebase_credential_file'], code, state),
        'guardian_backend_matches': True, 'original_state_overlap': False,
        'manifest_matches': True, 'runtime_files': len(FILES), 'state_is_empty': True,
        'credentials_contents_read': False, 'online_auth_verified': False,
    }
    return summary


def prepare(repo, destination, write=False):
    repo, destination = absolute(repo), absolute(destination)
    require(not overlap(repo, destination), 'payload destination must be outside repository')
    require(not destination.exists(), 'payload destination must be new; nothing will be overwritten')
    hashes = manifest()
    source = verify_code(repo / 'raspberry-pi-voice-hub', hashes, exact=False)
    tool_names = ('isolated_pi.py', 'isolated_http_smoke.py', 'isolated-runtime.sha256')
    require(all((PREP / name).is_file() and not (PREP / name).is_symlink() for name in tool_names),
            'required preparation tool missing')
    print(json.dumps({'code_path': str(destination / 'code'), 'manifest_matches': True,
                      'runtime_files': len(FILES), 'dry_run': not write}, indent=2))
    if not write:
        return
    destination.mkdir(mode=0o700, parents=False)
    code = destination / 'code'
    code.mkdir()
    for name in sorted(FILES):
        target = code / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    for name in tool_names:
        shutil.copyfile(PREP / name, destination / name)
    verify_code(code, hashes)
    # No .env, credentials, radio preset, existing outbox/state or venv is copied.


def load_openai_key(filename, code, state, forbidden_roots=()):
    """Explicit startup only; never called by dry-run or auth-check."""
    require(credential_present(filename, code, state), 'OPENAI_CREDENTIAL_NOT_SUPPLIED')
    path = absolute(filename)
    require(all(not overlap(path, absolute(root)) for root in forbidden_roots),
            'OPENAI_CREDENTIAL_MUST_BE_OUTSIDE_ORIGINAL_REPOSITORY')
    require(path.stat().st_size <= 4096, 'OPENAI_CREDENTIAL_INVALID')
    try:
        value = path.read_text(encoding='ascii').strip()
    except (OSError, UnicodeError):
        raise Refused('OPENAI_CREDENTIAL_UNREADABLE') from None
    require(value and all(32 < ord(c) < 127 for c in value), 'OPENAI_CREDENTIAL_INVALID')
    return value


def online(data, *, start=False):
    """Authenticate the named test device before any application worker starts."""
    cfg = data['pi']
    code, state = absolute(cfg['code_dir']), absolute(cfg['working_directory'])
    verify_code(code, manifest())
    require(not any(state.iterdir()), 'WORKING_DIRECTORY_NOT_EMPTY')
    require(credential_present(cfg['firebase_credential_file'], code, state),
            'FIREBASE_CREDENTIAL_NOT_SUPPLIED')
    # Code has already been allowlisted, byte-verified and compiled. No ambient
    # PYTHONPATH/config is used; this happens only in explicit online commands.
    require('firebase_auth' not in sys.modules and 'main' not in sys.modules,
            'RUNTIME_ALREADY_IMPORTED')
    sys.path.insert(0, str(code))
    import firebase_auth
    credentials = firebase_auth.load_device_credentials(
        cfg['firebase_credential_file'], project_id=data['test_project_id'],
        database_url=database_url(data['test_database_url']), elder_id=data['elder_id'],
        device_id=data['device_id'], expected_uid=cfg['firebase_uid'],
        expected_api_key=data['guardian']['VITE_FIREBASE_API_KEY'],
        forbidden_roots=(code, state, absolute(cfg['original_code_dir']).parent,
                         absolute(cfg['original_state_directory'])))
    key = load_openai_key(cfg['openai_key_file'], code, state,
                         (absolute(cfg['original_code_dir']).parent,
                          absolute(cfg['original_state_directory']))) if start else None
    provider = firebase_auth.FirebaseAuthProvider(credentials)
    try:
        provider.authenticate()
        transport = firebase_auth.AuthenticatedFirebaseHTTP(provider)
        # These are authorized bounded reads, not write-permission proof.
        base = database_url(data['test_database_url'])
        elder, device = data['elder_id'], data['device_id']
        probes = (
            (f'memories/{elder}/_control', {}),
            (f'memoryResetOperations/{elder}', {'orderBy': '"deviceStatus"',
                                               'equalTo': json.dumps(device + '|requested'), 'limitToFirst': 1}),
            (f'commands/{elder}/{device}', {'orderBy': '"$key"', 'limitToLast': 1}),
            (f'medicationSchedules/{elder}/{device}', {}),
        )
        for path, params in probes:
            response = transport.get(base + '/' + path + '.json', params=params, timeout=5)
            try:
                require(response.status_code == 200, 'ONLINE_SCOPE_READ_DENIED')
            finally:
                response.close()
        print('ONLINE_AUTH_CHECK_PASS: device identity, access binding and scoped reads verified; no writes or OpenAI calls.')
        if not start:
            return 0
        require(not any(state.iterdir()), 'WORKING_DIRECTORY_NOT_EMPTY')
        os.chdir(state)
        # Public runtime settings only. The OpenAI key is passed in memory to
        # the factory, never as an argument or inherited child environment value.
        os.environ.update({
            'FIREBASE_RUNTIME_MODE': 'isolated_authenticated',
            'FIREBASE_DATABASE_URL': base, 'ELDER_ID': elder, 'DEVICE_ID': device,
            'REMINDER_MODE': 'hub', 'AI_CONVERSATION_ENABLED': 'true',
            'AI_CONVERSATION_TRIGGER': 'firebase', 'DISPLAY_ENABLED': 'true',
            'DISPLAY_HOST': '127.0.0.1', 'DISPLAY_PORT': '8765',
            'AI_PLAYBACK_DEVICE': cfg.get('playback_device', DEFAULT_PLAYBACK_DEVICE),
        })
        import main as runtime
        runtime.configure_firebase_transport(transport, mode='isolated_authenticated')
        runtime.configure_openai_key(key)
        provider.start()
        print('ISOLATED_RUNTIME_START: scoped Firebase authentication verified; actual voice/Memory outcomes still require observation.')
        runtime.main()
        return 0
    finally:
        provider.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    pack = sub.add_parser('prepare')
    pack.add_argument('--repo', required=True)
    pack.add_argument('--out', required=True)
    group = pack.add_mutually_exclusive_group()
    group.add_argument('--write', action='store_true')
    group.add_argument('--dry-run', action='store_true')
    for name in ('preflight', 'auth-check', 'start'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--profile', required=True)
        cmd.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            # File-only allowlisted copy: no runtime/configuration is evaluated.
            prepare(args.repo, args.out, args.write)
            return 0
        clean_environment()
        data = load_profile(args.profile)
        summary = inspect_profile(data, manifest())
        summary['checks_requiring_action'] = dependencies()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if args.command == 'preflight' or args.dry_run:
            print('OFFLINE_PREFLIGHT_ONLY: credentials, networking and runtime workers were not used. ' + AUTH_REQUIRED)
            return 2 if summary['checks_requiring_action'] else 0
        require(not summary['checks_requiring_action'], 'RUNTIME_DEPENDENCIES_UNAVAILABLE')
        return online(data, start=args.command == 'start')
    except (Refused, OSError, SyntaxError) as exc:
        message = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        print('REFUSED: ' + message, file=sys.stderr)
        return 2
    except Exception:
        # Auth/network/runtime errors may contain credentials in chained
        # exceptions. Fixed diagnostics only; never emit a traceback or repr.
        print('REFUSED: ONLINE_AUTH_OR_RUNTIME_FAILURE; no anonymous fallback', file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
