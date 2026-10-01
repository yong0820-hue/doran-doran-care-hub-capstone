"""Actual Pi storage/reset/command/outbox protocols against localhost emulator.

Fixture ID token arrives on stdin, never argv/logs. No voice/SDK/main imports.
No mocks replace storage, reset, ETags or the dispatch barrier.
"""
import argparse
from io import BytesIO
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

PROJECT = 'demo-doran-memory-local'
NAMESPACE = PROJECT + '-default-rtdb'
BASE = 'http://127.0.0.1:9000'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('EMULATOR_REDIRECT_BLOCKED')


class Response:
    def __init__(self, response):
        self.status_code = response.code
        self.headers = dict(response.headers)
        self.body = response.read(1024 * 1024)
        response.close()

    def json(self):
        return json.loads(self.body)

    def iter_content(self, chunk_size=1024):
        source = BytesIO(self.body)
        while chunk := source.read(chunk_size):
            yield chunk

    def close(self):
        pass


class EmulatorHTTP:
    def __init__(self, token, lose_completion=False):
        self.token = token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.trace = []
        self.lose_completion = lose_completion
        self.lost = False

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.netloc != '127.0.0.1:9000'
                or parsed.query or parsed.fragment or not parsed.path.endswith('.json')):
            raise RuntimeError('EMULATOR_ENDPOINT_BLOCKED')
        params = {**kwargs.get('params', {}), 'ns': NAMESPACE, 'auth': self.token}
        headers = dict(kwargs.get('headers', {}))
        body = kwargs.get('json')
        raw = json.dumps(body).encode() if 'json' in kwargs else None
        if raw is not None:
            headers['Content-Type'] = 'application/json'
        timeout = kwargs.get('timeout', 1)
        timeout = max(timeout) if isinstance(timeout, tuple) else timeout
        request = Request(url + '?' + urlencode(params), raw, headers, method=method)
        try:
            response = Response(self.opener.open(request, timeout=timeout))
        except HTTPError as error:
            response = Response(error)
        entry = {'method': method, 'path': parsed.path, 'status': response.status_code}
        if ('/memoryResetOperations/' in parsed.path and isinstance(body, dict)
                and response.status_code >= 400):
            entry['operationStatus'] = body.get('status')
            entry['operationFields'] = sorted(body)
        self.trace.append(entry)
        if (self.lose_completion and not self.lost and method == 'PUT'
                and '/memoryResetOperations/' in parsed.path
                and isinstance(body, dict) and body.get('status') == 'completed'
                and response.status_code == 200):
            self.lost = True
            raise TimeoutError('LOCAL_FIXTURE_ACK_LOSS_AFTER_COMMIT')
        return response

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def put(self, url, **kwargs):
        return self.request('PUT', url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request('PATCH', url, **kwargs)


class LocalAuthHTTP:
    """Real stdlib REST transport for the production Auth provider test.

    No Firebase responses are fabricated. This avoids installing requests just
    for the local bundled Python while executing the actual provider logic.
    """
    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.netloc not in ('127.0.0.1:9099', '127.0.0.1:9000')
                or parsed.query or parsed.fragment or kwargs.get('allow_redirects') is not False):
            raise RuntimeError('AUTH_EMULATOR_ENDPOINT_BLOCKED')
        params = kwargs.get('params', {})
        if parsed.port == 9000 and params.get('ns') != NAMESPACE:
            raise RuntimeError('AUTH_EMULATOR_NAMESPACE_BLOCKED')
        headers = dict(kwargs.get('headers', {}))
        raw = None
        if 'json' in kwargs:
            raw = json.dumps(kwargs['json']).encode()
            headers['Content-Type'] = 'application/json'
        elif 'data' in kwargs:
            raw = urlencode(kwargs['data']).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        timeout = kwargs.get('timeout', 2)
        timeout = max(timeout) if isinstance(timeout, tuple) else timeout
        request = Request(url + '?' + urlencode(params), raw, headers, method=method)
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            return Response(opener.open(request, timeout=timeout))
        except HTTPError as error:
            return Response(error)

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)


def boundary(event, args):
    if event == 'socket.connect' and args[1] not in (('127.0.0.1', 9000), ('127.0.0.1', 9099)):
        raise RuntimeError('NETWORK_BLOCKED')
    if event == 'socket.getaddrinfo' and (args[0], args[1]) not in (('127.0.0.1', 9000), ('127.0.0.1', 9099)):
        raise RuntimeError('NETWORK_BLOCKED')
    if event.startswith('subprocess.') or event in ('os.system', 'os.exec', 'os.posix_spawn'):
        raise RuntimeError('PROCESS_BLOCKED')


def provider_check(payload):
    from firebase_auth import (FirebaseAuthProvider, FirebaseAuthError,
                               AuthenticatedFirebaseHTTP, FirebaseEmulatorConfig,
                               load_device_credentials)
    emulator = FirebaseEmulatorConfig()
    clock = [100.0]
    with tempfile.TemporaryDirectory(prefix='doran-emulator-fake-credentials-') as folder:
        path = Path(folder) / 'fake-device.json'
        path.write_text(json.dumps({
            'schema_version': 1, 'project_id': PROJECT, 'database_url': BASE,
            'api_key': 'AIza' + 'a' * 35, 'email': payload['email'],
            'password': payload['password'], 'uid': payload['uid'],
            'elder_id': payload['elder'], 'device_id': payload['device'],
        }), encoding='utf-8')
        path.chmod(0o600)
        credentials = load_device_credentials(path, project_id=PROJECT, database_url=BASE,
            elder_id=payload['elder'], device_id=payload['device'], expected_uid=payload['uid'],
            emulator=emulator)
    provider = FirebaseAuthProvider(credentials, http=LocalAuthHTTP(), emulator=emulator,
                                    monotonic=lambda: clock[0])
    real_http = provider.http
    counts = {'refresh': 0, 'login': 0, 'database': 0}
    count_lock = threading.Lock()
    class CountingActualHTTP:
        def post(self, url, **kwargs):
            with count_lock:
                if url.endswith('/token'):
                    counts['refresh'] += 1
                if url.endswith('/accounts:signInWithPassword'):
                    counts['login'] += 1
            return real_http.post(url, **kwargs)

        def get(self, url, **kwargs):
            with count_lock:
                if url.startswith(BASE):
                    counts['database'] += 1
            return real_http.get(url, **kwargs)

        def request(self, method, url, **kwargs):
            with count_lock:
                if url.startswith(BASE):
                    counts['database'] += 1
            return real_http.request(method, url, **kwargs)
    provider.http = CountingActualHTTP()
    try:
        if payload.get('expectedReject'):
            try:
                provider.authenticate()
            except FirebaseAuthError:
                before = dict(counts)
                try:
                    AuthenticatedFirebaseHTTP(provider).get(BASE + '/memories/' + payload['elder'] + '/_control.json')
                except FirebaseAuthError:
                    return {'action': 'auth', 'success': counts == before,
                            'rejected': True, 'anonymousFallback': False}
            raise RuntimeError('REJECTED_AUTH_BECAME_USABLE')
        provider.authenticate()
        transport = AuthenticatedFirebaseHTTP(provider)
        response = transport.get(BASE + '/memories/' + payload['elder'] + '/_control.json', timeout=2)
        if response.status_code != 200:
            raise RuntimeError('AUTH_TRANSPORT_DENIED')
        response.close()
        clock[0] = provider._deadline + 1
        try:
            provider.get_id_token()
        except FirebaseAuthError:
            pass
        else:
            raise RuntimeError('EXPIRED_TOKEN_USABLE')
        if not provider.refresh_if_due():
            raise RuntimeError('ACTUAL_REFRESH_FAILED')
        provider.get_id_token()
        clock[0] = provider._deadline + 1
        threads = [threading.Thread(target=provider.refresh_if_due) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError('REFRESH_THREAD_STILL_RUNNING')
        provider.get_id_token()
        if counts['login'] != 1 or counts['refresh'] != 2:
            raise RuntimeError('AUTH_SINGLEFLIGHT_COUNT')
        # A real emulator refresh with an invalid token fails closed; never
        # password-login or send an unauthenticated DB request on that failure.
        provider._refresh_token = 'local-fixture-invalid-refresh'
        clock[0] = provider._deadline + 1
        if provider.refresh_if_due():
            raise RuntimeError('BAD_REFRESH_ACCEPTED')
        before = dict(counts)
        try:
            transport.get(BASE + '/memories/' + payload['elder'] + '/_control.json')
        except FirebaseAuthError:
            pass
        else:
            raise RuntimeError('BAD_REFRESH_FELL_BACK')
        return {'action': 'auth', 'success': counts == before and counts['login'] == 1,
                'loginCount': counts['login'], 'successfulRefreshes': 2,
                'concurrentCalls': 6, 'anonymousFallback': False}
    finally:
        provider.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--code-dir', required=True)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    sys.addaudithook(boundary)
    payload = json.loads(sys.stdin.read(20000))
    if payload.get('project') != PROJECT:
        raise RuntimeError('PROJECT_MISMATCH')
    if payload.get('elder') != 'elder_test_a' or payload.get('device') != 'device_test_a':
        raise RuntimeError('FIXTURE_SCOPE_MISMATCH')
    sys.path.insert(0, str(Path(args.code_dir).resolve(strict=True)))
    if payload['action'] == 'auth':
        result = provider_check(payload)
        print(json.dumps(result))
        return 0 if result['success'] else 1
    from persistent_memory import MemoryStore
    from memory_dispatch import MemoryDispatchBarrier
    from memory_reset import MemoryResetWorker
    http = EmulatorHTTP(payload['idToken'], payload.get('loseCompletion', False))
    store = MemoryStore(BASE, payload['elder'], http=http)
    action = payload['action']
    result = {'action': action, 'success': False}
    if action == 'save':
        control, _, _, _ = store.control_read()
        result['success'] = store.save([{
            'type': 'preference', 'subject': '바둑', 'content': '바둑을 좋아해.',
            'importance': 3, 'tags': ['바둑'], 'evidenceKey': 'local-emulator-evidence',
            'lastMentionedAt': 100,
        }], 'local_fixture_session', store.generation({'_control': control}))
    elif action == 'reset':
        installed = []
        barrier = MemoryDispatchBarrier()
        def install(control):
            installed.append(control.get('generation'))
        worker = MemoryResetWorker(store, barrier, install, payload['device'])
        for _ in range(4):
            worker.process_pending()
        control, _, _, _ = store.control_read()
        result.update(success=bool(installed), generation=control.get('generation'),
                      callbacks=installed, lostCompletion=http.lost,
                      activeCount=len(control.get('activeIds') or {}))
    elif action == 'conversation':
        from conversation_store import ConversationStore
        with tempfile.TemporaryDirectory(prefix='doran-auth-emulator-') as outbox:
            conversations = ConversationStore(BASE, payload['elder'], payload['device'],
                                              'local_fixture', outbox, http=http)
            result['success'] = conversations.write_message(
                'auth_fixture_session', 100, 'auth_fixture_message', 'user',
                '비식별 연습 문장', 101, 1, 'recorded')
    elif action == 'command':
        from firebase_commands import FirebaseCommandClient
        client = FirebaseCommandClient(BASE, payload['elder'], payload['device'], http=http)
        invoked = []
        client.run_once(lambda: invoked.append('local-no-audio') or True)
        result.update(success=len(invoked) == 1, callbacks=len(invoked))
    else:
        raise RuntimeError('UNKNOWN_ACTION')
    result['trace'] = http.trace
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('{"success":false,"error":"LOCAL_PROTOCOL_FAILURE"}')
        raise SystemExit(1)
