"""Path-aware RTDB double: records actual wire paths, bytes and CAS conflicts."""
from copy import deepcopy
from email.utils import formatdate
import json
import hashlib
import threading
import time
from unittest.mock import Mock
from urllib.parse import urlparse


class ProtocolFirebase:
    def __init__(self, root=None):
        self.root = deepcopy(root or {})
        self.operations = {}
        self.version = 0
        self.lock = threading.RLock()
        self.get_calls, self.put_calls, self.patch_calls = [], [], []
        self.bytes_read = self.bytes_written = 0
        self.bytes_streamed = 0
        self.before_put = None
        self.read_failure = self.write_failure = self.no_etag = False
        self.now = time.time()

    def parts(self, url):
        if '/memoryResetOperations/' in url:
            path = urlparse(url).path.split('/memoryResetOperations/', 1)[1].split('/', 1)
            return ['@operations'] + ([] if len(path) == 1 else path[1][:-5].split('/'))
        path = urlparse(url).path.split('/memories/', 1)[1].split('/', 1)
        return [] if len(path) == 1 else path[1][:-5].split('/')

    def value(self, parts):
        if parts and parts[0] == '@operations':
            node, parts = self.operations, parts[1:]
        else:
            node = self.root
        for part in parts:
            node = node.get(part) if isinstance(node, dict) else None
        return node

    def assign(self, parts, value):
        if parts and parts[0] == '@operations':
            node, parts = self.operations, parts[1:]
        else:
            node = self.root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if value is None:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = self.resolve_timestamps(value)

    def resolve_timestamps(self, value):
        if value == {'.sv': 'timestamp'}:
            return int(self.now * 1000)
        if isinstance(value, dict):
            return {key: self.resolve_timestamps(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve_timestamps(item) for item in value]
        return deepcopy(value)

    def get(self, url, **kwargs):
        with self.lock:
            self.get_calls.append((url, kwargs))
            if self.read_failure:
                raise ConnectionError('offline')
            value = deepcopy(self.value(self.parts(url)))
            params = kwargs.get('params', {})
            if params.get('orderBy') in ('"status"', '"deviceStatus"', '"deviceEpoch"') and isinstance(value, dict):
                status = json.loads(params['equalTo'])
                field = json.loads(params['orderBy'])
                value = dict(sorted((key, row) for key, row in value.items()
                                    if row.get(field) == status)[:int(params['limitToFirst'])])
            if params.get('orderBy') == '"$key"' and isinstance(value, dict):
                start = json.loads(params.get('startAt', '""'))
                value = dict(sorted((key, row) for key, row in value.items()
                                    if key >= start)[:int(params['limitToFirst'])])
            if kwargs.get('params', {}).get('shallow') == 'true' and isinstance(value, dict):
                value = dict.fromkeys(value, True)
            encoded = json.dumps(value).encode()
            self.bytes_read += len(encoded) if not kwargs.get('stream') else 0
            def chunks(chunk_size=1024):
                for offset in range(0, len(encoded), chunk_size):
                    chunk = encoded[offset:offset + chunk_size]
                    self.bytes_streamed += len(chunk)
                    self.bytes_read += len(chunk)
                    yield chunk
            return Mock(status_code=200, headers={**({} if self.no_etag else {'ETag': self.etag(self.parts(url))}),
                        'Date': formatdate(self.now, usegmt=True)}, json=lambda: value, iter_content=chunks)

    def etag(self, parts):
        return hashlib.sha256(json.dumps(self.value(parts), sort_keys=True).encode()).hexdigest()

    def put(self, url, *, json, headers=None, **kwargs):
        with self.lock:
            self.put_calls.append((url, deepcopy(json), headers or {}, kwargs))
            if self.before_put:
                hook, self.before_put = self.before_put, None
                hook(self)
            if self.write_failure:
                raise ConnectionError('offline')
            if headers and 'if-match' in headers and headers['if-match'] != self.etag(self.parts(url)):
                return Mock(status_code=412)
            self.assign(self.parts(url), json)
            self.bytes_written += len(__import__('json').dumps(json).encode())
            self.version += 1
            return Mock(status_code=200)

    def patch(self, url, *, json, **kwargs):
        with self.lock:
            self.patch_calls.append((url, deepcopy(json)))
            if self.write_failure:
                raise ConnectionError('offline')
            base = self.parts(url)
            for key, value in json.items():
                self.assign(base + key.split('/'), value)
            self.version += 1
            return Mock(status_code=200)

    def reset(self):
        with self.lock:
            control = self.root.setdefault('_control', {})
            control.update(generation=1, lastResetAt=int(self.now * 1000), activeIds={})
            self.version += 1
