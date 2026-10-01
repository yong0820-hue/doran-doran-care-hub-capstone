"""Offline device authentication and real client injection; all HTTP is fake."""
import base64
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'raspberry-pi-voice-hub'))
from firebase_auth import (AuthenticatedFirebaseHTTP, DeviceCredentials, FirebaseAuthError,
    FirebaseAuthProvider, FirebaseEmulatorConfig, FirebaseTransportError,
    _RequestsHTTP, load_device_credentials)

PROJECT = 'doran-offline-test'
DATABASE = 'https://doran-offline-test-default-rtdb.firebaseio.com'
API_KEY = 'AIza' + '0' * 35
PASSWORD = 'PRIVATE_FAKE_PASSWORD_NEVER_ECHO'
REFRESH = 'PRIVATE_FAKE_REFRESH_NEVER_ECHO'


class Clock:
    def __init__(self):
        self.now = 1700000000.0
    def __call__(self):
        return self.now


def credential(**changes):
    result = DeviceCredentials(PROJECT, DATABASE, API_KEY, 'device@offline.invalid',
                               PASSWORD, 'device_uid', 'elder_001', 'device_001')
    return replace(result, **changes)


def token(clock, *, project=PROJECT, uid='device_uid', algorithm='RS256', **changes):
    claims = {'aud': project, 'iss': 'https://securetoken.google.com/' + project,
              'sub': uid, 'iat': clock(), 'exp': clock() + 600}
    claims.update(changes)
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return encode({'alg': algorithm}) + '.' + encode(claims) + '.' + ('' if algorithm == 'none' else 'fake_signature')


class Response:
    def __init__(self, body=None, status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = headers or {'ETag': 'etag-1'}
        self.closed = False
    @property
    def text(self):
        return json.dumps(self.body)
    def json(self):
        return self.body
    def iter_content(self, chunk_size=1024):
        raw = self.text.encode()
        for i in range(0, len(raw), chunk_size):
            yield raw[i:i + chunk_size]
    def close(self):
        self.closed = True


class FakeHTTP:
    def __init__(self, clock, credentials=None, emulator=False):
        self.clock, self.credentials = clock, credentials or credential()
        self.emulator = emulator
        self.calls = []
        self.failure = None
        self.binding = {'role': 'device', 'elderId': 'elder_001', 'deviceId': 'device_001', 'enabled': True}
        self.scope = {'deviceId': 'device_001', 'projectId': self.credentials.project_id}
        self.account = {'localId': self.credentials.uid, 'email': self.credentials.email}
        self.sign_in_override = None
        self.refresh_override = None
        self.refresh_entered = threading.Event()
        self.refresh_release = None
        self.db_response = Response(None)
        self.protocol = None

    def make_token(self):
        return token(self.clock, project=self.credentials.project_id,
                     uid=self.credentials.uid, algorithm='none' if self.emulator else 'RS256')

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.failure:
            raise self.failure
        if 'accounts:signInWithPassword' in url:
            return self.sign_in_override or Response({'idToken': self.make_token(), 'refreshToken': REFRESH,
                                                      'localId': self.credentials.uid, 'expiresIn': '600'})
        if url.endswith('/token'):
            self.refresh_entered.set()
            if self.refresh_release is not None:
                if not self.refresh_release.wait(3):
                    raise TimeoutError('fake refresh blocked')
            return self.refresh_override or Response({'id_token': self.make_token(), 'refresh_token': REFRESH + '_rotated',
                                                      'user_id': self.credentials.uid, 'expires_in': '600'})
        if 'accounts:lookup' in url:
            return Response({'users': [self.account]})
        if '/accessBindings/' in url:
            return Response(self.binding)
        if '/accessScopes/' in url:
            return Response(self.scope)
        if self.protocol is not None:
            response = getattr(self.protocol, method.lower())(url, **kwargs)
            # ProtocolFirebase's minimal write double omits headers; actual
            # requests responses always provide a header mapping.
            if not isinstance(response.headers, dict):
                response.headers = {}
            return response
        return self.db_response

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)
    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'device.json'
        self.data = {'schema_version': 1, **asdict(credential())}
        self.kwargs = dict(project_id=PROJECT, database_url=DATABASE, elder_id='elder_001',
                           device_id='device_001', expected_uid='device_uid', expected_api_key=API_KEY)

    def load(self):
        self.path.write_text(json.dumps(self.data), encoding='utf-8')
        self.path.chmod(0o600)
        return load_device_credentials(self.path, **self.kwargs)

    def test_valid_external_private_fixture_has_redacted_repr(self):
        loaded = self.load()
        self.assertEqual(loaded, credential())
        self.assertNotIn(PASSWORD, repr(loaded))
        self.assertNotIn(API_KEY, repr(loaded))
        self.assertNotIn(loaded.email, repr(loaded))

    def test_exact_scope_and_web_key_are_checked(self):
        for field, value in [('project_id', 'other-project'), ('database_url', 'https://other.firebaseio.com'),
                             ('uid', 'other_uid'), ('elder_id', 'other_elder'), ('device_id', 'other_device'),
                             ('api_key', 'AIza' + '1' * 35)]:
            with self.subTest(field=field):
                original = self.data[field]
                self.data[field] = value
                with self.assertRaises(FirebaseAuthError): self.load()
                self.data[field] = original

    def test_admin_or_extra_secret_schema_is_rejected_without_echo(self):
        self.data['private_key'] = PASSWORD
        with self.assertRaises(FirebaseAuthError) as caught: self.load()
        self.assertNotIn(PASSWORD, str(caught.exception))

    def test_forbidden_repository_root_is_checked_before_read(self):
        self.load()
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not read')):
            with self.assertRaises(FirebaseAuthError):
                load_device_credentials(self.path, forbidden_roots=[self.temp.name], **self.kwargs)

    @unittest.skipIf(os.name == 'nt', 'POSIX file permission enforcement requires a POSIX filesystem')
    def test_nonprivate_posix_file_is_rejected(self):
        self.load()
        self.path.chmod(0o644)
        with self.assertRaises(FirebaseAuthError):
            load_device_credentials(self.path, **self.kwargs)


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.http = FakeHTTP(self.clock)
        self.provider = FirebaseAuthProvider(credential(), http=self.http, clock=self.clock, monotonic=self.clock)
        self.addCleanup(self.provider.stop)

    def login(self):
        self.provider.authenticate()
        return AuthenticatedFirebaseHTTP(self.provider)

    def test_login_checks_account_binding_scope_and_starts_no_worker(self):
        self.login()
        self.assertIsNone(self.provider._thread)
        self.assertEqual(len(self.http.calls), 4)
        self.assertIn('accounts:signInWithPassword', self.http.calls[0][1])
        self.assertIn('accounts:lookup', self.http.calls[1][1])
        self.assertIn('/accessBindings/device_uid.json', self.http.calls[2][1])
        self.assertIn('/accessScopes/elder_001.json', self.http.calls[3][1])
        self.assertTrue(all(call[2]['allow_redirects'] is False for call in self.http.calls))
        self.provider.authenticate()
        self.assertEqual(len(self.http.calls), 4)

    def test_wrong_password_and_disabled_account_fail_closed(self):
        for code in ('INVALID_LOGIN_CREDENTIALS', 'USER_DISABLED'):
            with self.subTest(code=code):
                http = FakeHTTP(self.clock)
                http.sign_in_override = Response({'error': {'message': code}}, 400)
                provider = FirebaseAuthProvider(credential(), http=http, clock=self.clock, monotonic=self.clock)
                with self.assertRaises(FirebaseAuthError): provider.authenticate()
                with self.assertRaises(FirebaseAuthError): provider.get_id_token()
                with self.assertRaises(FirebaseAuthError): provider.authenticate()
                self.assertEqual(len(http.calls), 1)

    def test_token_project_issuer_subject_and_expiry_must_match(self):
        for changes in ({'aud': 'other-project'}, {'iss': 'https://wrong.invalid'},
                        {'sub': 'other_uid'}, {'exp': self.clock() - 1}):
            with self.subTest(changes=changes):
                self.http.sign_in_override = Response({'idToken': token(self.clock, **changes),
                    'refreshToken': REFRESH, 'localId': 'device_uid', 'expiresIn': '600'})
                with self.assertRaises(FirebaseAuthError): self.provider.authenticate()
                with self.assertRaises(FirebaseAuthError): self.provider.get_id_token()

    def test_wrong_lookup_uid_and_device_binding_are_rejected(self):
        cases = [('account', {'localId': 'other', 'email': credential().email}),
                 ('binding', {'role': 'guardian', 'elderId': 'elder_001', 'deviceId': 'device_001', 'enabled': True}),
                 ('binding', {'role': 'device', 'elderId': 'other', 'deviceId': 'device_001', 'enabled': True}),
                 ('binding', {'role': 'device', 'elderId': 'elder_001', 'deviceId': 'device_001', 'enabled': False}),
                 ('scope', {'deviceId': 'other_device'}), ('scope', None)]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                http = FakeHTTP(self.clock)
                setattr(http, name, value)
                provider = FirebaseAuthProvider(credential(), http=http, clock=self.clock, monotonic=self.clock)
                with self.assertRaises(FirebaseAuthError): provider.authenticate()
                with self.assertRaises(FirebaseAuthError): provider.get_id_token()

    def test_expired_snapshot_fails_immediately_without_login_or_io(self):
        transport = self.login()
        before = len(self.http.calls)
        self.clock.now += 601
        with self.assertRaises(FirebaseAuthError): transport.get(DATABASE + '/events/elder_001.json')
        self.assertEqual(len(self.http.calls), before)

    def test_trusted_scope_project_must_match_authenticated_project(self):
        for project in (None, 'other-firebase-project'):
            with self.subTest(project=project):
                self.http.scope = {'deviceId': 'device_001', 'projectId': project}
                with self.assertRaises(FirebaseAuthError): self.provider.authenticate()
                with self.assertRaises(FirebaseAuthError): self.provider.get_id_token()
        self.assertFalse(any('/memories/' in url for _, url, _ in self.http.calls))

    def test_refresh_rotates_only_in_memory_and_rechecks_binding(self):
        self.login()
        original = self.provider.get_id_token()
        self.clock.now += 481
        self.assertTrue(self.provider.refresh_if_due())
        self.assertNotEqual(self.provider.get_id_token(), original)
        self.assertEqual(self.provider._refresh_token, REFRESH + '_rotated')
        self.assertEqual(sum(url.endswith('/token') for _, url, _ in self.http.calls), 1)
        self.assertEqual(sum('accounts:signInWithPassword' in url for _, url, _ in self.http.calls), 1)

    def test_concurrent_refresh_is_singleflight_and_token_reads_do_not_wait(self):
        self.login()
        original = self.provider.get_id_token()
        self.clock.now += 481
        self.http.refresh_release = threading.Event()
        worker = threading.Thread(target=self.provider.refresh_if_due)
        worker.start()
        try:
            self.assertTrue(self.http.refresh_entered.wait(1))
            started = time.monotonic()
            self.assertEqual(self.provider.get_id_token(), original)
            self.assertFalse(self.provider.refresh_if_due())
            self.assertLess(time.monotonic() - started, .2)
        finally:
            self.http.refresh_release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(sum(url.endswith('/token') for _, url, _ in self.http.calls), 1)

    def test_failed_refresh_has_no_anonymous_or_password_fallback(self):
        transport = self.login()
        self.clock.now += 481
        self.http.refresh_override = Response({'error': {'message': 'USER_DISABLED'}}, 400)
        self.assertFalse(self.provider.refresh_if_due())
        before = len(self.http.calls)
        for _ in range(3):
            with self.assertRaises(FirebaseAuthError): transport.get(DATABASE + '/events/elder_001.json')
            self.assertFalse(self.provider.refresh_if_due())
            with self.assertRaises(FirebaseAuthError): self.provider.authenticate()
        self.assertEqual(len(self.http.calls), before)

    def test_transient_refresh_failure_is_bounded_and_never_anonymous(self):
        transport = self.login()
        self.clock.now += 481
        self.http.failure = OSError(PASSWORD + REFRESH)
        self.assertFalse(self.provider.refresh_if_due())
        self.http.failure = None
        before = len(self.http.calls)
        for _ in range(5): self.assertFalse(self.provider.refresh_if_due())
        with self.assertRaises(FirebaseAuthError): transport.get(DATABASE + '/events/elder_001.json')
        with self.assertRaises(FirebaseAuthError): self.provider.authenticate()
        self.assertEqual(len(self.http.calls), before)
        self.clock.now += 31
        self.assertTrue(self.provider.refresh_if_due())

    def test_start_and_stop_own_one_background_worker(self):
        self.provider.start()
        thread = self.provider._thread
        self.provider.start()
        self.assertIs(thread, self.provider._thread)
        self.assertTrue(thread.is_alive())
        self.assertTrue(self.provider.stop())
        self.assertFalse(thread.is_alive())
        with self.assertRaises(FirebaseAuthError): self.provider.get_id_token()

    def test_redirects_and_raw_exceptions_do_not_escape_auth_layer(self):
        self.http.sign_in_override = Response({'secret': PASSWORD}, 302)
        with self.assertRaises(FirebaseAuthError) as caught: self.provider.authenticate()
        self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertEqual(len(self.http.calls), 1)
        self.http.sign_in_override = None
        self.http.failure = OSError(PASSWORD + REFRESH + API_KEY)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            with self.assertRaises(FirebaseAuthError) as caught: self.provider.authenticate()
        for secret in (PASSWORD, REFRESH, API_KEY):
            self.assertNotIn(secret, str(caught.exception) + output.getvalue() + repr(self.provider))

    def test_explicit_demo_emulator_never_falls_back_to_cloud(self):
        cred = credential(project_id='demo-doran-memory', database_url='http://127.0.0.1:9000')
        http = FakeHTTP(self.clock, cred, emulator=True)
        provider = FirebaseAuthProvider(cred, http=http, emulator=FirebaseEmulatorConfig(), clock=self.clock, monotonic=self.clock)
        provider.authenticate()
        AuthenticatedFirebaseHTTP(provider).get(cred.database_url + '/events/elder_001.json')
        self.assertTrue(all(url.startswith(('http://127.0.0.1:9099/', 'http://127.0.0.1:9000/')) for _, url, _ in http.calls))
        self.assertEqual(http.calls[-1][2]['params']['ns'], 'demo-doran-memory-default-rtdb')
        with self.assertRaises(FirebaseAuthError):
            FirebaseAuthProvider(credential(), emulator=FirebaseEmulatorConfig())

    def test_transport_preserves_query_etag_body_timeout_and_412_without_retry(self):
        transport = self.login()
        self.http.db_response = Response({'current': True}, 412)
        payload = {'operationId': 'same-operation', 'status': 'processing'}
        before = len(self.http.calls)
        response = transport.put(DATABASE + '/memoryResetOperations/elder_001/same-operation.json',
            json=payload, headers={'if-match': 'old-etag'}, timeout=(.25, .5), params={'orderBy': '"status"'})
        self.assertEqual(response.status_code, 412)
        self.assertEqual(response.json(), {'current': True})
        self.assertEqual(response.headers['ETag'], 'etag-1')
        self.assertEqual(len(self.http.calls), before + 1)
        kwargs = self.http.calls[-1][2]
        self.assertIs(kwargs['json'], payload)
        self.assertEqual(kwargs['headers']['if-match'], 'old-etag')
        self.assertEqual(kwargs['timeout'], (.25, .5))
        self.assertEqual(kwargs['params']['orderBy'], '"status"')
        self.assertEqual(kwargs['params']['auth'], self.provider.get_id_token())
        self.assertFalse(kwargs['allow_redirects'])

    def test_401_permission_denial_does_not_trigger_login_refresh_or_write_retry(self):
        transport = self.login()
        self.http.db_response = Response({'error': PASSWORD + REFRESH}, 401)
        before = len(self.http.calls)
        response = transport.put(DATABASE + '/memoryResetOperations/elder_001/same-operation.json', json={'status': 'requested'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {'error': 'authentication_or_permission_denied'})
        self.assertNotIn(PASSWORD, response.text)
        self.assertEqual(len(self.http.calls), before + 1)

    def test_cross_host_query_header_and_redirect_token_leaks_are_refused(self):
        transport = self.login()
        before = len(self.http.calls)
        targets = ['https://other.firebaseio.com/.json', 'http://' + DATABASE.split('://')[1] + '/.json',
                   DATABASE + ':443/.json', DATABASE + '/.json?auth=unexpected', DATABASE + '/.json#fragment']
        for url in targets:
            with self.subTest(url=url), self.assertRaises(FirebaseTransportError): transport.get(url)
        for extra in ({'params': {'auth': 'unexpected'}}, {'headers': {'Authorization': 'unexpected'}},
                      {'allow_redirects': True}, {'verify': False}, {'proxies': {}},
                      {'timeout': None}, {'timeout': float('inf')}):
            with self.subTest(extra=extra), self.assertRaises(FirebaseTransportError): transport.get(DATABASE + '/.json', **extra)
        self.assertEqual(len(self.http.calls), before)
        self.http.db_response = Response(None, 302, {'Location': 'https://wrong.invalid/?auth=' + PASSWORD})
        with self.assertRaises(FirebaseTransportError) as caught: transport.get(DATABASE + '/.json')
        self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertEqual(len(self.http.calls), before + 1)

    def test_ack_loss_keeps_unknown_and_does_not_reissue_write(self):
        transport = self.login()
        self.http.failure = OSError('https://x.invalid/?auth=' + self.provider.get_id_token())
        before = len(self.http.calls)
        with self.assertRaises(FirebaseTransportError) as caught:
            transport.put(DATABASE + '/memoryResetOperations/elder_001/stable-id.json', json={'operationId': 'stable-id'})
        self.assertIn('unknown', str(caught.exception))
        self.assertNotIn(self.provider.get_id_token(), str(caught.exception))
        self.assertEqual(len(self.http.calls), before + 1)

    def test_standard_http_debug_logs_redact_auth_query(self):
        _RequestsHTTP()
        with self.assertLogs('urllib3.connectionpool', level=logging.DEBUG) as logs:
            logging.getLogger('urllib3.connectionpool').debug('GET /data.json?auth=%s&x=1&key=%s', REFRESH, API_KEY)
        self.assertNotIn(REFRESH, '\n'.join(logs.output))
        self.assertNotIn(API_KEY, '\n'.join(logs.output))

    def test_real_memory_protocol_uses_authenticated_bounded_stream_and_cas(self):
        from memory_protocol_fake import ProtocolFirebase
        from persistent_memory import INITIAL_GENERATION, MemoryStore
        transport = self.login()
        self.http.protocol = ProtocolFirebase()
        store = MemoryStore(DATABASE, 'elder_001', http=transport)
        row = dict(type='preference', subject='yoga', content='yoga preference',
                   lastMentionedAt=1700000000000, importance=3)
        self.assertTrue(store.save([row], 'stable-session', INITIAL_GENERATION))
        store.read()
        calls = [entry for entry in self.http.calls if '/memories/' in entry[1]]
        self.assertTrue(calls)
        self.assertTrue(all(entry[2]['params']['auth'] for entry in calls))
        self.assertTrue(any(entry[2].get('stream') for entry in calls))
        self.assertTrue(any(entry[2].get('headers', {}).get('if-match') for entry in calls))
        self.assertFalse(any(method in ('GET', 'PUT') and url == store.url for method, url, _ in calls))


class RuntimeInjectionTests(unittest.TestCase):
    def setUp(self):
        from test_main_regression import load_main
        with patch.dict(os.environ, {'FIREBASE_RUNTIME_MODE': 'isolated_authenticated'}):
            self.main = load_main()
        self.clock = Clock()
        self.http = FakeHTTP(self.clock)
        self.provider = FirebaseAuthProvider(credential(), http=self.http, clock=self.clock, monotonic=self.clock)
        self.provider.authenticate()
        self.addCleanup(self.provider.stop)
        self.transport = AuthenticatedFirebaseHTTP(self.provider)
        self.main.FIREBASE_DATABASE_URL = DATABASE
        self.main.ELDER_ID, self.main.DEVICE_ID = 'elder_001', 'device_001'

    def configure(self):
        self.main.configure_firebase_transport(self.transport)
        self.main.configure_openai_key('fake-openai-key-for-offline-test')

    def test_isolated_import_skips_dotenv_and_unconfigured_main_refuses_start(self):
        self.main.load_dotenv.assert_not_called()
        with self.assertRaises(RuntimeError): self.main.main()
        self.assertEqual(len(self.http.calls), 4)

    def test_main_direct_requests_keep_atomic_root_patch_and_authenticate(self):
        self.configure()
        self.http.db_response = Response({'runId': 'stable-run'})
        with redirect_stdout(io.StringIO()):
            self.assertTrue(self.main.firebase_patch_and_verify('event-id', {'createdAt': 1, 'runId': 'stable-run'}))
            self.main.fetch_events()
        calls = self.http.calls[4:]
        self.assertEqual([x[0] for x in calls], ['PATCH', 'GET', 'GET'])
        self.assertEqual(calls[0][1], DATABASE + '/.json')
        self.assertTrue(all(x[2]['params']['auth'] for x in calls))

    def test_main_passes_transport_to_schedule_and_all_voice_care_factories(self):
        self.configure()
        with patch.object(self.main, 'MedicationScheduleProvider') as schedule:
            self.main.start_medication_schedule_provider()
            self.assertIs(schedule.call_args.kwargs['http'], self.transport)
        voice = types.ModuleType('voice_ai')
        voice.create_firebase_controller = Mock()
        voice.create_inactivity_controller = Mock()
        voice.create_medication_care = Mock()
        active = Mock()
        with patch.dict(sys.modules, {'voice_ai': voice}), patch.dict(os.environ, {
                'AI_CONVERSATION_ENABLED': 'true', 'AI_CONVERSATION_TRIGGER': 'firebase',
                'MEDICATION_VOICE_CONFIRMATION_ENABLED': 'true'}), redirect_stdout(io.StringIO()):
            self.main.start_ai_conversation()
            self.main.start_inactivity_care(active)
            self.main.start_medication_care(active)
        for factory in (voice.create_firebase_controller, voice.create_inactivity_controller, voice.create_medication_care):
            self.assertIs(factory.call_args.kwargs['http'], self.transport)
        with patch('firebase_medication.FirebaseMedicationClient') as client, patch('medication_care.MedicationAnnouncementCare'):
            self.main.start_medication_announcement_care()
            self.assertIs(client.call_args.kwargs['http'], self.transport)

    def test_real_voice_factories_share_transport_for_commands_memory_outbox_and_care(self):
        import voice_ai
        fake_openai = types.ModuleType('openai')
        fake_openai.OpenAI = Mock()
        service = Mock()
        with patch.dict(sys.modules, {'openai': fake_openai}), \
                patch('persistent_memory.PersistentMemoryService', return_value=service) as memory, \
                patch.dict(os.environ, {'FIREBASE_RUNTIME_MODE': 'isolated_authenticated', 'AI_WAKE_WORD_ENABLED': 'false'}):
            controller = voice_ai.create_firebase_controller(DATABASE, 'elder_001', 'device_001',
                'test', threading.Lock(), Path('/fake/audio'), http=self.transport, api_key='fake-key')
            self.assertIs(controller.command_client.http, self.transport)
            self.assertIs(controller.conversation.store.http, self.transport)
            self.assertIs(memory.call_args.args[0].http, self.transport)
            with patch('inactivity_care.InactivityCareController') as inactive:
                voice_ai.create_inactivity_controller(DATABASE, 'elder_001', 'device_001', controller.conversation, http=self.transport)
                self.assertIs(inactive.call_args.args[0].http, self.transport)
            with patch('medication_care.MedicationCare') as medication:
                voice_ai.create_medication_care(DATABASE, 'elder_001', 'device_001', controller.conversation, Mock(), http=self.transport)
                self.assertIs(medication.call_args.args[0].http, self.transport)
        self.assertEqual(len(self.http.calls), 4)  # no runtime worker or external SDK call

    def test_isolated_factories_reject_anonymous_default_adapters(self):
        import voice_ai
        with patch.dict(os.environ, {'FIREBASE_RUNTIME_MODE': 'isolated_authenticated'}):
            with self.assertRaises(ValueError):
                voice_ai.create_firebase_controller(DATABASE, 'elder_001', 'device_001', 'test', threading.Lock(), Path('/fake/audio'))
            with self.assertRaises(ValueError):
                voice_ai.create_inactivity_controller(DATABASE, 'elder_001', 'device_001', Mock())
            with self.assertRaises(ValueError):
                voice_ai.create_medication_care(DATABASE, 'elder_001', 'device_001', Mock(), Mock())
        self.assertEqual(len(self.http.calls), 4)


if __name__ == '__main__':
    unittest.main()
