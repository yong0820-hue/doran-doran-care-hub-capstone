"""Authentication launch gates: synthetic files only, no cloud/runtime startup."""
import builtins
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
PREP = REPO / 'deployment-prep/persistent-memory-20260928-223050'
spec = importlib.util.spec_from_file_location('isolated_auth_launcher', PREP / 'isolated_pi.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class IsolatedAuthLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.code, self.state, self.original = (self.root / name for name in ('code', 'state', 'original'))
        for directory in (self.code, self.state, self.original):
            directory.mkdir()
        self.hashes = {}
        for name in launcher.FILES:
            source = REPO / 'raspberry-pi-voice-hub' / name
            destination = self.code / name
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, destination)
            self.hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
        self.profile = json.loads((PREP / 'isolated-test.example.json').read_text(encoding='utf-8'))
        self.profile.update(test_project_id='offline-fixture', production_project_id='offline-original',
            test_database_url='https://offline-fixture-default-rtdb.firebaseio.com',
            production_database_url='https://offline-original-default-rtdb.firebaseio.com',
            database_project_confirmed=True)
        self.profile['guardian'].update(VITE_FIREBASE_API_KEY='AIza' + '0' * 35,
            VITE_FIREBASE_AUTH_DOMAIN='offline-fixture.firebaseapp.com',
            VITE_FIREBASE_DATABASE_URL=self.profile['test_database_url'],
            VITE_FIREBASE_PROJECT_ID='offline-fixture')
        self.profile['pi'].update(code_dir=str(self.code), working_directory=str(self.state),
            original_code_dir=str(self.original), original_state_directory=str(self.original),
            python=sys.executable, firebase_uid='synthetic-device-uid',
            openai_key_file=str(self.root / 'fake-openai.key'),
            firebase_credential_file=str(self.root / 'fake-firebase.json'))
        self.public = self.root / 'test-profile.json'
        self.public.write_text(json.dumps(self.profile), encoding='utf-8')

    def invoke(self, command, *args, issues=()):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(launcher, 'manifest', return_value=self.hashes), \
                patch.object(launcher, 'dependencies', return_value=list(issues)), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = launcher.main([command, '--profile', str(self.public), *args])
        return code, output.getvalue()

    def test_dry_runs_never_open_credentials_or_enter_online_path(self):
        for command in ('preflight', 'auth-check', 'start'):
            with self.subTest(command=command), patch.object(launcher, 'online', side_effect=AssertionError('online called')):
                result, output = self.invoke(command, '--dry-run')
                self.assertEqual(result, 0)
                self.assertIn('OFFLINE_PREFLIGHT_ONLY', output)
                self.assertIn('ONLINE_AUTH_REQUIRED', output)
                self.assertIn('"online_auth_verified": false', output)
                self.assertNotIn('ONLINE_AUTH_CHECK_PASS', output)

    def test_missing_dependency_refuses_online_before_credentials(self):
        with patch.object(launcher, 'online') as online:
            result, output = self.invoke('start', issues=['DEPENDENCY_MISSING: fixture'])
        self.assertEqual(result, 2)
        online.assert_not_called()
        self.assertIn('RUNTIME_DEPENDENCIES_UNAVAILABLE', output)

    def test_online_commands_require_credentials_and_keep_no_fallback(self):
        for command in ('auth-check', 'start'):
            with self.subTest(command=command):
                result, output = self.invoke(command)
                self.assertEqual(result, 2)
                self.assertIn('FIREBASE_CREDENTIAL_NOT_SUPPLIED', output)
                self.assertNotIn('ONLINE_AUTH_CHECK_PASS', output)

    def test_auth_failure_is_redacted_without_runtime_fallback(self):
        with patch.object(launcher, 'online', side_effect=RuntimeError('PASSWORD_TOKEN_SECRET_SENTINEL')):
            result, output = self.invoke('start')
        self.assertEqual(result, 3)
        self.assertNotIn('PASSWORD_TOKEN_SECRET_SENTINEL', output)
        self.assertIn('no anonymous fallback', output)

    def invoke_fake_online_start(self, *, authentication_fails=False, denied_probe=None):
        """Exercise the real bootstrap while intercepting both runtime imports."""
        original_code = self.original / 'code'
        original_code.mkdir(exist_ok=True)
        self.profile['pi']['original_code_dir'] = str(original_code)
        self.public.write_text(json.dumps(self.profile), encoding='utf-8')
        secrets = ('SYNTHETIC_PASSWORD_SENTINEL', 'SYNTHETIC_OPENAI_SENTINEL',
                   'SYNTHETIC_ID_TOKEN_SENTINEL', 'SYNTHETIC_REFRESH_TOKEN_SENTINEL')
        credential_body = {
            'schema_version': 1, 'project_id': self.profile['test_project_id'],
            'database_url': self.profile['test_database_url'],
            'api_key': self.profile['guardian']['VITE_FIREBASE_API_KEY'],
            'email': 'synthetic-device@example.invalid', 'password': secrets[0],
            'uid': self.profile['pi']['firebase_uid'], 'elder_id': 'elder_001',
            'device_id': 'device_001',
        }
        credential_file = Path(self.profile['pi']['firebase_credential_file'])
        credential_file.write_text(json.dumps(credential_body), encoding='utf-8')
        credential_file.chmod(0o600)
        key_file = Path(self.profile['pi']['openai_key_file'])
        key_file.write_text(secrets[1] + '\n', encoding='ascii')
        key_file.chmod(0o600)
        events, probes, environments = [], [], []
        references = {}

        def record(event):
            events.append(event)
            environments.append(dict(os.environ))

        def load_credentials(filename, **kwargs):
            self.assertEqual(Path(filename), credential_file)
            self.assertEqual(kwargs, {
                'project_id': self.profile['test_project_id'],
                'database_url': self.profile['test_database_url'],
                'elder_id': 'elder_001', 'device_id': 'device_001',
                'expected_uid': self.profile['pi']['firebase_uid'],
                'expected_api_key': self.profile['guardian']['VITE_FIREBASE_API_KEY'],
                'forbidden_roots': (self.code, self.state, self.original, self.original),
            })
            record('load_credentials')
            return json.loads(credential_file.read_text(encoding='utf-8'))

        class FakeProvider:
            def __init__(provider, credentials):
                self.assertEqual(credentials, credential_body)
                references['provider'] = provider

            def authenticate(provider):
                record('authenticate')
                if authentication_fails:
                    raise RuntimeError(secrets[0] + secrets[2])
                provider.token, provider.refresh_token = secrets[2:]

            def start(provider):
                self.assertEqual(provider.token, secrets[2])
                record('refresh_start')

            def stop(provider):
                record('stop')
                provider.token = provider.refresh_token = None

        class FakeTransport:
            def __init__(transport, provider):
                self.assertIs(provider, references['provider'])
                references['transport'] = transport
                record('transport')

            def get(transport, url, **kwargs):
                path = url.removeprefix(self.profile['test_database_url'] + '/')
                probes.append((url, kwargs))
                record('get:' + path)
                return types.SimpleNamespace(
                    status_code=403 if path == denied_probe else 200,
                    close=lambda: record('close:' + path))

        def configure_transport(transport, *, mode):
            self.assertIs(transport, references['transport'])
            self.assertEqual(mode, 'isolated_authenticated')
            self.assertEqual(Path.cwd(), self.state)
            self.assertEqual(os.environ['FIREBASE_RUNTIME_MODE'], 'isolated_authenticated')
            self.assertEqual(os.environ['FIREBASE_DATABASE_URL'], self.profile['test_database_url'])
            self.assertEqual(os.environ['AI_PLAYBACK_DEVICE'],
                             self.profile['pi'].get('playback_device',
                                                    launcher.DEFAULT_PLAYBACK_DEVICE))
            record('configure_transport')

        def configure_key(key):
            self.assertEqual(key, secrets[1])
            record('configure_key')

        fake_auth = types.SimpleNamespace(load_device_credentials=load_credentials,
            FirebaseAuthProvider=FakeProvider, AuthenticatedFirebaseHTTP=FakeTransport)
        fake_main = types.SimpleNamespace(configure_firebase_transport=configure_transport,
            configure_openai_key=configure_key, main=lambda: record('main'))
        original_import = builtins.__import__

        def isolated_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == 'firebase_auth':
                return fake_auth
            if name == 'main':
                record('import_main')
                return fake_main
            return original_import(name, globals, locals, fromlist, level)

        previous_cwd = Path.cwd()
        try:
            with patch.dict(sys.modules), patch.object(sys, 'path', list(sys.path)), \
                    patch('builtins.__import__', side_effect=isolated_import):
                sys.modules.pop('firebase_auth', None)
                sys.modules.pop('main', None)
                result, output = self.invoke('start')
        finally:
            os.chdir(previous_cwd)
        for secret in (*secrets, credential_body['email'], credential_body['api_key']):
            self.assertNotIn(secret, output)
            self.assertFalse(any(secret in value for env in environments for value in env.values()))
        self.assertTrue(all('OPENAI_API_KEY' not in env for env in environments))
        self.assertIsNone(references['provider'].token)
        self.assertIsNone(references['provider'].refresh_token)
        self.assertEqual(list(self.state.iterdir()), [])
        return result, output, events, probes

    def test_online_start_authenticates_probes_injects_then_starts_and_stops(self):
        result, output, events, probes = self.invoke_fake_online_start()
        paths = ('memories/elder_001/_control.json', 'memoryResetOperations/elder_001.json',
                 'commands/elder_001/device_001.json', 'medicationSchedules/elder_001/device_001.json')
        expected_events = ['load_credentials', 'authenticate', 'transport']
        for path in paths:
            expected_events.extend(('get:' + path, 'close:' + path))
        expected_events.extend(('import_main', 'configure_transport', 'configure_key',
                                'refresh_start', 'main', 'stop'))
        self.assertEqual(events, expected_events)
        self.assertEqual(probes, [
            (self.profile['test_database_url'] + '/' + paths[0], {'params': {}, 'timeout': 5}),
            (self.profile['test_database_url'] + '/' + paths[1], {'params': {
                'orderBy': '"deviceStatus"', 'equalTo': '"device_001|requested"', 'limitToFirst': 1}, 'timeout': 5}),
            (self.profile['test_database_url'] + '/' + paths[2], {'params': {
                'orderBy': '"$key"', 'limitToLast': 1}, 'timeout': 5}),
            (self.profile['test_database_url'] + '/' + paths[3], {'params': {}, 'timeout': 5}),
        ])
        self.assertEqual(result, 0)
        self.assertIn('ONLINE_AUTH_CHECK_PASS', output)
        self.assertIn('ISOLATED_RUNTIME_START', output)

    def test_public_playback_setting_reaches_clean_isolated_runtime(self):
        self.profile['pi']['playback_device'] = 'plughw:CARD=Speaker,DEV=1'
        result, _, _, _ = self.invoke_fake_online_start()
        self.assertEqual(result, 0)
        self.profile['pi'].pop('playback_device')
        result, _, _, _ = self.invoke_fake_online_start()
        self.assertEqual(result, 0)

    def test_online_authentication_failure_stops_before_transport_or_runtime(self):
        result, output, events, probes = self.invoke_fake_online_start(authentication_fails=True)
        self.assertEqual(result, 3)
        self.assertEqual(events, ['load_credentials', 'authenticate', 'stop'])
        self.assertEqual(probes, [])
        self.assertIn('no anonymous fallback', output)
        self.assertNotIn('ONLINE_AUTH_CHECK_PASS', output)
        self.assertNotIn('ISOLATED_RUNTIME_START', output)

    def test_online_scope_denial_closes_response_and_stops_before_runtime(self):
        path = 'memories/elder_001/_control.json'
        result, output, events, probes = self.invoke_fake_online_start(denied_probe=path)
        self.assertEqual(result, 2)
        self.assertEqual(events, ['load_credentials', 'authenticate', 'transport',
                                  'get:' + path, 'close:' + path, 'stop'])
        self.assertEqual(len(probes), 1)
        self.assertIn('ONLINE_SCOPE_READ_DENIED', output)
        self.assertNotIn('ONLINE_AUTH_CHECK_PASS', output)
        self.assertNotIn('ISOLATED_RUNTIME_START', output)

    def test_scope_and_role_metadata_cannot_enable_runtime(self):
        for mutate in (
            lambda p: p['pi'].update(firebase_uid=''),
            lambda p: p['pi'].update(role='device'),
            lambda p: p['pi'].update(playback_device=''),
            lambda p: p.update(auth_ready=True),
            lambda p: p.update(test_database_url=p['production_database_url']),
            lambda p: p['guardian'].update(VITE_FIREBASE_PROJECT_ID='other-project'),
        ):
            candidate = copy.deepcopy(self.profile)
            mutate(candidate)
            with self.assertRaises(launcher.Refused):
                launcher.profile_data(candidate)

    def test_original_paths_or_nonempty_state_are_refused(self):
        candidate = copy.deepcopy(self.profile)
        candidate['pi']['working_directory'] = str(self.original)
        with self.assertRaises(launcher.Refused):
            launcher.inspect_profile(candidate, self.hashes)
        (self.state / '.env').write_text('SYNTHETIC_ONLY=never-loaded', encoding='utf-8')
        with self.assertRaises(launcher.Refused):
            launcher.inspect_profile(self.profile, self.hashes)

    def test_openai_loader_uses_private_external_fixture_without_environment(self):
        filename = Path(self.profile['pi']['openai_key_file'])
        filename.write_text('synthetic-openai-only\n', encoding='ascii')
        filename.chmod(0o600)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(launcher.load_openai_key(filename, self.code, self.state), 'synthetic-openai-only')
            self.assertNotIn('OPENAI_API_KEY', os.environ)
        filename.write_text('synthetic invalid key', encoding='ascii')
        with self.assertRaises(launcher.Refused):
            launcher.load_openai_key(filename, self.code, self.state)
        with self.assertRaises(launcher.Refused):
            launcher.load_openai_key(self.code / 'inside.key', self.code, self.state)


if __name__ == '__main__':
    unittest.main()
