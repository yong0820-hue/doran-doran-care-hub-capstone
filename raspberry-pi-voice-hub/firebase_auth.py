"""Opt-in Firebase Auth REST transport; no import-time I/O or anonymous fallback.

Tokens remain in memory. authenticate() is startup/preflight-only; database
callers obtain a nonblocking token snapshot and never log in or refresh.
Firebase documents ID-token authentication as the RTDB ``auth`` query parameter.
"""
from dataclasses import dataclass, field
import base64
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from urllib.parse import quote, urlsplit


_DIAGNOSTIC_CATEGORIES = frozenset({
    'connect_timeout', 'read_timeout', 'connection_error', 'timeout_unknown',
    'http_error', 'precondition_failed', 'token_unavailable',
    'transport_rejected', 'response_invalid', 'unknown',
})
_REQUEST_PHASES = frozenset({'before_request', 'request', 'response', 'unknown'})
_DIAGNOSTIC_OPERATIONS = frozenset({
    'medication_apply_status_publish', 'memory_prefetch',
})
_DIAGNOSTIC_METHODS = frozenset({'GET', 'PATCH', 'PUT', 'DELETE', 'POST', 'unknown'})
_DIAGNOSTIC_PATHS = frozenset({
    '/.json', '/devices/{device}/medicationScheduleStatus',
    '/memories/{elder}/_control.json', '/memories/{elder}/active/{body}.json',
    'unknown',
})
_DIAGNOSTIC_STAGES = frozenset({
    'apply_status_publish', 'control_read', 'active_manifest_validation',
    'body_read', 'response_validation', 'cache_validation',
    'transport_pre_request', 'unknown',
})
_DIAGNOSTIC_FIELDS = frozenset({
    'method', 'path_template', 'target_template', 'stage', 'source_stage',
    'category', 'http_status', 'request_phase', 'elapsed_ms',
    'connect_timeout_ms', 'read_timeout_ms', 'cache_present', 'cache_age_ms',
    'retry_in_ms', 'snapshot_generation',
})
_DIAGNOSTIC_GROUP_FIELDS = (
    'method', 'path_template', 'target_template', 'stage', 'source_stage',
    'category', 'http_status', 'request_phase',
)


def _safe_http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


def _safe_enum(value, allowed):
    return value if type(value) is str and value in allowed else 'unknown'


def _safe_getattr(value, name, default=None):
    try:
        return getattr(value, name, default)
    except Exception:
        return default


class FirebaseAuthError(RuntimeError):
    """Only fixed, non-sensitive messages may be passed to this exception."""

    def __init__(self, message, *, category='unknown', status=None,
                 request_phase='unknown'):
        super().__init__(message)
        self.category = _safe_enum(category, _DIAGNOSTIC_CATEGORIES)
        self.http_status = _safe_http_status(status)
        self.request_phase = _safe_enum(request_phase, _REQUEST_PHASES)


class FirebaseTransportError(RuntimeError):
    def __init__(self, message, *, category='unknown', status=None,
                 request_phase='unknown'):
        super().__init__(message)
        self.category = _safe_enum(category, _DIAGNOSTIC_CATEGORIES)
        self.http_status = _safe_http_status(status)
        self.request_phase = _safe_enum(request_phase, _REQUEST_PHASES)


def _exception_types(module, *names):
    values = tuple(getattr(module, name, None) for name in names)
    return tuple(value for value in values if isinstance(value, type))


def _nested_exceptions(exc):
    """Yield exception objects only; never inspect or stringify their arguments."""
    pending = [exc] if isinstance(exc, BaseException) else []
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for nested in (_safe_getattr(current, '__cause__'),
                       _safe_getattr(current, '__context__'),
                       _safe_getattr(current, 'reason')):
            if isinstance(nested, BaseException):
                pending.append(nested)
        arguments = _safe_getattr(current, 'args', ())
        if isinstance(arguments, tuple):
            for nested in arguments:
                if isinstance(nested, BaseException):
                    pending.append(nested)


def safe_error_details(exc=None, *, status=None, conditional=False):
    """Return bounded diagnostics without rendering exception-controlled data."""
    safe_status = _safe_http_status(status)
    if safe_status is None:
        safe_status = _safe_http_status(_safe_getattr(exc, 'http_status'))
    if safe_status is None:
        response = _safe_getattr(exc, 'response')
        safe_status = _safe_http_status(_safe_getattr(response, 'status_code'))

    category = _safe_enum(_safe_getattr(exc, 'category'), _DIAGNOSTIC_CATEGORIES)
    phase = _safe_enum(_safe_getattr(exc, 'request_phase'), _REQUEST_PHASES)
    if category == 'precondition_failed' and not (
            safe_status == 412 and conditional is True):
        category = 'http_error' if safe_status is not None and safe_status >= 400 else 'unknown'
        phase = 'response' if category == 'http_error' else 'unknown'
    if category != 'unknown':
        return {'category': category, 'http_status': safe_status,
                'request_phase': phase}

    if safe_status == 412 and conditional is True:
        return {'category': 'precondition_failed', 'http_status': 412,
                'request_phase': 'response'}

    try:
        from requests import exceptions as requests_exceptions
    except ImportError:
        requests_exceptions = None
    try:
        from urllib3 import exceptions as urllib3_exceptions
    except ImportError:
        urllib3_exceptions = None

    if requests_exceptions is not None:
        connect_types = _exception_types(requests_exceptions, 'ConnectTimeout')
        read_types = _exception_types(requests_exceptions, 'ReadTimeout')
        connection_types = _exception_types(requests_exceptions, 'ConnectionError')
        timeout_types = _exception_types(requests_exceptions, 'Timeout')
        http_types = _exception_types(requests_exceptions, 'HTTPError')
        if connect_types and isinstance(exc, connect_types):
            category, phase = 'connect_timeout', 'request'
        elif read_types and isinstance(exc, read_types):
            category, phase = 'read_timeout', 'response'
        elif connection_types and isinstance(exc, connection_types):
            urllib3_read_types = (_exception_types(urllib3_exceptions, 'ReadTimeoutError')
                                  if urllib3_exceptions is not None else ())
            nested_read = any(
                (read_types and isinstance(item, read_types))
                or (urllib3_read_types and isinstance(item, urllib3_read_types))
                for item in _nested_exceptions(exc)
            )
            category, phase = (('read_timeout', 'response') if nested_read
                               else ('connection_error', 'request'))
        elif timeout_types and isinstance(exc, timeout_types):
            category, phase = 'timeout_unknown', 'unknown'
        elif http_types and isinstance(exc, http_types):
            category, phase = 'http_error', 'response'

    if category == 'unknown' and urllib3_exceptions is not None:
        read_types = _exception_types(urllib3_exceptions, 'ReadTimeoutError')
        if read_types and isinstance(exc, read_types):
            category, phase = 'read_timeout', 'response'

    if category == 'unknown' and isinstance(exc, TimeoutError):
        category, phase = 'timeout_unknown', 'unknown'

    if category == 'unknown' and safe_status is not None and safe_status >= 400:
        category, phase = 'http_error', 'response'
    return {'category': category, 'http_status': safe_status, 'request_phase': phase}


class DiagnosticReporter:
    """Rate-limited, allowlist-only runtime diagnostics; never starts workers."""

    def __init__(self, operation, *, clock=None, interval=60):
        if type(operation) is not str or operation not in _DIAGNOSTIC_OPERATIONS:
            raise ValueError('Unsupported diagnostic operation')
        if (not isinstance(interval, (int, float)) or isinstance(interval, bool)
                or not math.isfinite(interval) or interval <= 0):
            raise ValueError('Diagnostic interval must be positive and finite')
        self.operation = operation
        self._clock = time.monotonic if clock is None else clock
        self._interval = float(interval)
        self._lock = threading.Lock()
        self._episode = 0
        self._attempt = 0
        self._cause = None
        self._cause_fields = None
        self._suppressed = 0
        self._last_emitted_at = None

    @staticmethod
    def _bounded_number(value):
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or not 0 <= value <= 86400000):
            return None
        return value

    @classmethod
    def _sanitize_fields(cls, fields):
        clean = {}
        for name in _DIAGNOSTIC_FIELDS:
            value = fields.get(name)
            if name == 'method':
                clean[name] = _safe_enum(value, _DIAGNOSTIC_METHODS)
            elif name in ('path_template', 'target_template'):
                clean[name] = _safe_enum(value, _DIAGNOSTIC_PATHS)
            elif name in ('stage', 'source_stage'):
                clean[name] = _safe_enum(value, _DIAGNOSTIC_STAGES)
            elif name == 'category':
                clean[name] = _safe_enum(value, _DIAGNOSTIC_CATEGORIES)
            elif name == 'request_phase':
                clean[name] = _safe_enum(value, _REQUEST_PHASES)
            elif name == 'http_status':
                clean[name] = _safe_http_status(value)
            elif name == 'cache_present':
                clean[name] = value if type(value) is bool else None
            elif name == 'snapshot_generation':
                clean[name] = (value if type(value) is int
                               and 0 <= value <= 9223372036854775807 else None)
            else:
                clean[name] = cls._bounded_number(value)
        return clean

    def _now(self):
        try:
            value = self._clock()
            return (float(value) if isinstance(value, (int, float))
                    and not isinstance(value, bool) and math.isfinite(value) else None)
        except Exception:
            return None

    def _emit(self, event, fields, *, suppressed_count=0,
              previous_suppressed_count=None, previous_fields=None):
        try:
            # Sanitize a second time immediately before serialization.
            clean = self._sanitize_fields(fields)
            payload = {
                'operation': self.operation,
                'event': event if event in {'failure', 'failure_summary', 'recovered'} else 'failure',
                'episode': self._episode if type(self._episode) is int else 0,
                'attempt': self._attempt if type(self._attempt) is int else 0,
                'suppressed_count': (suppressed_count if type(suppressed_count) is int
                                     and suppressed_count >= 0 else 0),
                **clean,
            }
            if previous_suppressed_count is not None:
                payload['previous_cause_suppressed_count'] = (
                    previous_suppressed_count
                    if type(previous_suppressed_count) is int
                    and previous_suppressed_count >= 0 else 0
                )
            if previous_fields is not None:
                previous = self._sanitize_fields(previous_fields)
                for name in _DIAGNOSTIC_GROUP_FIELDS:
                    payload['previous_' + name] = previous[name]
            print('[RuntimeDiagnostic] ' + json.dumps(
                payload, sort_keys=True, separators=(',', ':'), allow_nan=False,
            ), flush=True)
        except Exception:
            pass

    def failure(self, **fields):
        try:
            clean = self._sanitize_fields(fields)
            cause = tuple(clean[name] for name in _DIAGNOSTIC_GROUP_FIELDS)
            now = self._now()
            with self._lock:
                if self._cause is None:
                    self._episode += 1
                    self._attempt = 1
                    self._cause, self._cause_fields = cause, clean
                    self._suppressed = 0
                    self._last_emitted_at = now
                    self._emit('failure', clean)
                elif cause != self._cause:
                    previous_suppressed = self._suppressed
                    self._attempt += 1
                    self._cause, self._cause_fields = cause, clean
                    self._suppressed = 0
                    self._last_emitted_at = now
                    self._emit('failure', clean,
                               previous_suppressed_count=previous_suppressed)
                else:
                    self._attempt += 1
                    self._cause_fields = clean
                    if (now is not None and self._last_emitted_at is not None
                            and now - self._last_emitted_at >= self._interval):
                        suppressed = self._suppressed
                        self._suppressed = 0
                        self._last_emitted_at = now
                        self._emit('failure_summary', clean,
                                   suppressed_count=suppressed)
                    else:
                        self._suppressed += 1
        except Exception:
            pass
        return None

    def success(self, **fields):
        try:
            clean = self._sanitize_fields(fields)
            with self._lock:
                if self._cause is None:
                    return None
                prior = dict(self._cause_fields)
                suppressed = self._suppressed
                self._emit('recovered', clean, suppressed_count=suppressed,
                           previous_fields=prior)
                self._cause = self._cause_fields = None
                self._suppressed = 0
                self._last_emitted_at = None
        except Exception:
            pass
        return None


@dataclass(frozen=True)
class FirebaseEmulatorConfig:
    auth_origin: str = 'http://127.0.0.1:9099'
    database_origin: str = 'http://127.0.0.1:9000'

    def __post_init__(self):
        if (self.auth_origin != 'http://127.0.0.1:9099'
                or self.database_origin != 'http://127.0.0.1:9000'):
            raise FirebaseAuthError('Emulator endpoints must be explicit loopback defaults')


def canonical_database_url(value, *, emulator=None):
    if not isinstance(value, str) or value != value.strip():
        raise FirebaseAuthError('Invalid Firebase database URL')
    try:
        parsed = urlsplit(value)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/') or '\\' in value):
            raise ValueError
        if emulator is not None:
            if value.rstrip('/') != emulator.database_origin:
                raise ValueError
            return emulator.database_origin
        if parsed.scheme != 'https' or parsed.port or ':' in parsed.netloc:
            raise ValueError
        host = parsed.hostname or ''
        if not (re.fullmatch(r'[a-z0-9][a-z0-9-]*\.firebaseio\.com', host)
                or re.fullmatch(r'[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*\.firebasedatabase\.app', host)):
            raise ValueError
        return 'https://' + host
    except (ValueError, TypeError):
        raise FirebaseAuthError('Invalid Firebase database URL') from None


def _key(value):
    return (isinstance(value, str) and 0 < len(value) <= 128
            and not any(c in value for c in '.#$[]/')
            and all(32 < ord(c) < 127 for c in value))


@dataclass(frozen=True)
class DeviceCredentials:
    project_id: str
    database_url: str
    api_key: str = field(repr=False)
    email: str = field(repr=False)
    password: str = field(repr=False)
    uid: str
    elder_id: str
    device_id: str


def load_device_credentials(filename, *, project_id, database_url, elder_id,
                            device_id, expected_uid, expected_api_key=None,
                            forbidden_roots=(), emulator=None):
    """Explicit online-only loader. Callers must not invoke it in dry-run.

    File must be external to code/repository roots and private on POSIX. Neither
    values nor the filename are included in failures. Service-account JSON is
    rejected by the exact schema. No token is read from or persisted to this file.
    """
    try:
        path = Path(filename)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError
        resolved = path.resolve(strict=True)
        roots = [Path(__file__).resolve().parent.parent, *map(Path, forbidden_roots)]
        if any(resolved.is_relative_to(root.resolve()) for root in roots):
            raise ValueError
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 16384
                or (os.name != 'nt' and (info.st_mode & 0o077 or info.st_uid != os.getuid()))):
            raise ValueError
        data = json.loads(path.read_text(encoding='utf-8'))
        fields = {'schema_version', 'project_id', 'database_url', 'api_key', 'email',
                  'password', 'uid', 'elder_id', 'device_id'}
        if not isinstance(data, dict) or set(data) != fields or type(data['schema_version']) is not int or data['schema_version'] != 1:
            raise ValueError
        if (not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', project_id)
                or (emulator is not None and not project_id.startswith('demo-'))):
            raise ValueError
        endpoint = canonical_database_url(database_url, emulator=emulator)
        if (data['project_id'] != project_id
                or canonical_database_url(data['database_url'], emulator=emulator) != endpoint
                or data['elder_id'] != elder_id or data['device_id'] != device_id
                or data['uid'] != expected_uid
                or not all(_key(v) for v in (elder_id, device_id, expected_uid))):
            raise ValueError
        if (not isinstance(data['api_key'], str)
                or not re.fullmatch(r'AIza[0-9A-Za-z_-]{35}', data['api_key'])
                or (expected_api_key is not None and data['api_key'] != expected_api_key)):
            raise ValueError
        if (not isinstance(data['email'], str) or not 3 <= len(data['email']) <= 254
                or data['email'] != data['email'].strip() or '@' not in data['email']
                or any(ord(c) < 33 for c in data['email'])
                or not isinstance(data['password'], str) or not 1 <= len(data['password']) <= 4096):
            raise ValueError
        return DeviceCredentials(project_id, endpoint, data['api_key'], data['email'],
                                 data['password'], expected_uid, elder_id, device_id)
    except Exception:
        raise FirebaseAuthError('Device credential file or configured scope is invalid') from None


class _RedactAuthQuery(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        redacted = re.sub(r'([?&](?:auth|access_token|key)=)[^&\s\"\']+', r'\1[redacted]', message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


class _RequestsHTTP:
    """Each worker owns its own Session; no ambient proxy/netrc/CA override."""
    def __init__(self):
        self._local = threading.local()
        for name in ('urllib3.connectionpool', 'urllib3.util.retry'):
            logger = logging.getLogger(name)
            if not any(isinstance(item, _RedactAuthQuery) for item in logger.filters):
                logger.addFilter(_RedactAuthQuery())

    def request(self, method, url, **kwargs):
        if not hasattr(self._local, 'session'):
            import requests
            self._local.session = requests.Session()
            self._local.session.trust_env = False
        return self._local.session.request(method, url, **kwargs)

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)


def _bounded_json(response):
    try:
        body = bytearray()
        for chunk in response.iter_content(chunk_size=1024):
            body.extend(chunk)
            if len(body) > 65536:
                raise ValueError
        return json.loads(body)
    except Exception as exc:
        details = safe_error_details(
            exc, status=getattr(response, 'status_code', None),
        )
        if details['category'] == 'unknown':
            details['category'], details['request_phase'] = 'response_invalid', 'response'
        raise FirebaseAuthError(
            'Firebase authentication response is invalid',
            category=details['category'], status=details['http_status'],
            request_phase=details['request_phase'],
        ) from None
    finally:
        response.close()


class FirebaseAuthProvider:
    AUTH_ORIGIN = 'https://identitytoolkit.googleapis.com'
    REFRESH_ORIGIN = 'https://securetoken.googleapis.com'
    TIMEOUT = (3, 7)
    TERMINAL_ERRORS = frozenset({'INVALID_LOGIN_CREDENTIALS', 'INVALID_PASSWORD',
        'EMAIL_NOT_FOUND', 'USER_DISABLED', 'USER_NOT_FOUND', 'INVALID_REFRESH_TOKEN',
        'TOKEN_EXPIRED', 'INVALID_ID_TOKEN', 'PROJECT_NUMBER_MISMATCH',
        'API_KEY_INVALID', 'OPERATION_NOT_ALLOWED'})

    def __init__(self, credentials, *, http=None, emulator=None,
                 clock=time.time, monotonic=time.monotonic):
        self.credentials = credentials
        self.emulator = emulator
        if emulator is not None and not credentials.project_id.startswith('demo-'):
            raise FirebaseAuthError('Emulator requires a demo project')
        if canonical_database_url(credentials.database_url, emulator=emulator) != credentials.database_url:
            raise FirebaseAuthError('Invalid configured backend')
        self.http = http if http is not None else _RequestsHTTP()
        self._clock, self._monotonic = clock, monotonic
        self._state = threading.Lock()
        self._singleflight = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._id_token = self._refresh_token = None
        self._expires_at = self._deadline = self._refresh_at = 0
        self._terminal = self._closed = False
        self.last_error = None

    def __repr__(self):
        return '<FirebaseAuthProvider tokens=redacted>'

    def _auth_url(self, route, refresh=False):
        if self.emulator is not None:
            service = 'securetoken.googleapis.com' if refresh else 'identitytoolkit.googleapis.com'
            return f'{self.emulator.auth_origin}/{service}/v1/{route}'
        return f'{self.REFRESH_ORIGIN if refresh else self.AUTH_ORIGIN}/v1/{route}'

    def _post(self, route, payload, refresh=False):
        with self._state:
            if self._closed:
                raise FirebaseAuthError('Firebase authentication provider is closed')
        try:
            response = self.http.post(self._auth_url(route, refresh),
                params={'key': self.credentials.api_key},
                **({'data': payload} if refresh else {'json': payload}),
                timeout=self.TIMEOUT, allow_redirects=False, stream=True)
            status = response.status_code
            if 300 <= status < 400:
                response.close()
                raise FirebaseAuthError(
                    'Authentication redirect refused', category='http_error',
                    status=status, request_phase='response',
                )
            data = _bounded_json(response)
            if status != 200:
                code = data.get('error', {}).get('message', '') if isinstance(data, dict) else ''
                code = code.split(' : ', 1)[0] if isinstance(code, str) else ''
                if code in self.TERMINAL_ERRORS:
                    with self._state:
                        self._terminal = True
                        self._id_token = self._refresh_token = None
                    raise FirebaseAuthError(
                        'Firebase account credentials were rejected',
                        category='http_error', status=status,
                        request_phase='response',
                    )
                raise FirebaseAuthError(
                    'Firebase authentication request failed', category='http_error',
                    status=status, request_phase='response',
                )
            if not isinstance(data, dict):
                raise FirebaseAuthError(
                    'Invalid authentication response', category='response_invalid',
                    status=status, request_phase='response',
                )
            return data
        except FirebaseAuthError:
            raise
        except Exception as exc:
            details = safe_error_details(exc)
            raise FirebaseAuthError(
                'Firebase authentication transport unavailable',
                category=details['category'], status=details['http_status'],
                request_phase=details['request_phase'],
            ) from None

    def _validate_token(self, token, uid, lifetime):
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 16384 or uid != self.credentials.uid:
                raise ValueError
            parts = token.split('.')
            if len(parts) != 3:
                raise ValueError
            decode = lambda part: json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
            header, claims = decode(parts[0]), decode(parts[1])
            algorithm = 'none' if self.emulator is not None else 'RS256'
            if header.get('alg') != algorithm or (self.emulator is None and not parts[2]):
                raise ValueError
            now = self._clock()
            exp, iat = claims['exp'], claims['iat']
            if (claims.get('aud') != self.credentials.project_id
                    or claims.get('iss') != 'https://securetoken.google.com/' + self.credentials.project_id
                    or claims.get('sub') != uid
                    or not all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in (exp, iat))
                    or exp <= now or iat > now + 60 or exp <= iat):
                raise ValueError
            seconds = float(lifetime)
            if not math.isfinite(seconds) or not 0 < seconds <= 7200:
                raise ValueError
            return min(float(exp), now + seconds)
        except Exception:
            raise FirebaseAuthError('Firebase token identity or lifetime does not match device') from None

    def _check_identity_and_binding(self, token):
        # Tokens originate only from the fixed HTTPS Auth endpoint (or explicit
        # demo emulator). Claim inspection is not a general JWT signature verifier.
        account = self._post('accounts:lookup', {'idToken': token})
        users = account.get('users')
        if (not isinstance(users, list) or len(users) != 1 or not isinstance(users[0], dict)
                or users[0].get('localId') != self.credentials.uid
                or users[0].get('disabled') is True
                or str(users[0].get('email', '')).casefold() != self.credentials.email.casefold()):
            raise FirebaseAuthError('Firebase account identity does not match device')
        params = {'auth': token}
        if self.emulator is not None:
            params['ns'] = self.credentials.project_id + '-default-rtdb'
        try:
            response = self.http.get(self.credentials.database_url + '/accessBindings/'
                + quote(self.credentials.uid, safe='') + '.json', params=params,
                timeout=self.TIMEOUT, allow_redirects=False, stream=True)
            if response.status_code != 200:
                status = response.status_code
                response.close()
                raise FirebaseAuthError(
                    'Device access binding is unavailable or denied',
                    category='http_error', status=status,
                    request_phase='response',
                )
            binding = _bounded_json(response)
        except FirebaseAuthError:
            raise
        except Exception as exc:
            details = safe_error_details(exc)
            raise FirebaseAuthError(
                'Device access binding transport unavailable',
                category=details['category'], status=details['http_status'],
                request_phase=details['request_phase'],
            ) from None
        if (not isinstance(binding, dict) or binding.get('enabled') is not True
                or binding.get('role') != 'device'
                or binding.get('elderId') != self.credentials.elder_id
                or binding.get('deviceId') != self.credentials.device_id):
            raise FirebaseAuthError('Device access binding does not authorize configured scope')
        try:
            response = self.http.get(self.credentials.database_url + '/accessScopes/'
                + quote(self.credentials.elder_id, safe='') + '.json', params=params,
                timeout=self.TIMEOUT, allow_redirects=False, stream=True)
            if response.status_code != 200:
                status = response.status_code
                response.close()
                raise FirebaseAuthError(
                    'Device access scope is unavailable or denied',
                    category='http_error', status=status,
                    request_phase='response',
                )
            scope = _bounded_json(response)
        except FirebaseAuthError:
            raise
        except Exception as exc:
            details = safe_error_details(exc)
            raise FirebaseAuthError(
                'Device access scope transport unavailable',
                category=details['category'], status=details['http_status'],
                request_phase=details['request_phase'],
            ) from None
        if (not isinstance(scope, dict) or scope.get('deviceId') != self.credentials.device_id
                or scope.get('projectId') != self.credentials.project_id):
            raise FirebaseAuthError('Device is not the designated owner of this elder scope')

    def _publish(self, token, refresh, expires_at):
        if not isinstance(refresh, str) or not 1 <= len(refresh) <= 16384:
            raise FirebaseAuthError('Firebase refresh credential is invalid')
        remaining = expires_at - self._clock()
        if remaining <= 0:
            raise FirebaseAuthError('Firebase authentication expired during initialization')
        with self._state:
            if self._closed:
                raise FirebaseAuthError('Firebase authentication provider is closed')
            self._id_token, self._refresh_token = token, refresh
            self._expires_at = expires_at
            self._deadline = self._monotonic() + remaining
            self._refresh_at = self._deadline - min(120, remaining / 4)
            self.last_error = None

    def authenticate(self):
        """Login and verify the trusted binding, without starting a worker."""
        with self._singleflight:
            with self._state:
                if self._closed or self._terminal:
                    raise FirebaseAuthError('Firebase authentication unavailable')
                if self._id_token is not None:
                    if self._monotonic() < self._deadline and self._clock() < self._expires_at:
                        return self
                    raise FirebaseAuthError('Existing Firebase session requires background refresh')
                if self._refresh_token is not None:
                    raise FirebaseAuthError('Existing Firebase session is unavailable; no password fallback')
            result = self._post('accounts:signInWithPassword', {
                'email': self.credentials.email, 'password': self.credentials.password,
                'returnSecureToken': True,
            })
            expires = self._validate_token(result.get('idToken'), result.get('localId'), result.get('expiresIn'))
            self._check_identity_and_binding(result['idToken'])
            self._publish(result['idToken'], result.get('refreshToken'), expires)
        return self

    def get_id_token(self):
        """No network or refresh lock: safe for an existing interaction section."""
        with self._state:
            if (self._closed or self._terminal or self._id_token is None
                    or self._monotonic() >= self._deadline or self._clock() >= self._expires_at):
                self._wake.set()
                raise FirebaseAuthError(
                    'No currently valid Firebase token is available',
                    category='token_unavailable', request_phase='before_request',
                )
            return self._id_token

    def refresh_if_due(self):
        """Single-flight background operation; never called by HTTP transport."""
        if not self._singleflight.acquire(blocking=False):
            return False
        try:
            with self._state:
                if self._closed or self._terminal or not self._refresh_token:
                    return False
                if self._monotonic() < self._refresh_at:
                    return False
                refresh = self._refresh_token
            try:
                result = self._post('token', {'grant_type': 'refresh_token', 'refresh_token': refresh}, refresh=True)
                expires = self._validate_token(result.get('id_token'), result.get('user_id'), result.get('expires_in'))
                self._check_identity_and_binding(result['id_token'])
                self._publish(result['id_token'], result.get('refresh_token'), expires)
                return True
            except FirebaseAuthError:
                with self._state:
                    # Fail closed after any failed refresh. Keep only the refresh
                    # credential for bounded background recovery, never password-login.
                    self._id_token = None
                    self.last_error = 'refresh_failed'
                    self._refresh_at = self._monotonic() + 30
                return False
        finally:
            self._singleflight.release()

    def start(self):
        self.authenticate()
        with self._state:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='firebase-token-refresh', daemon=True)
                self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            with self._state:
                delay = max(0.1, min(30, self._refresh_at - self._monotonic()))
                terminal = self._terminal
            if terminal:
                return
            self._wake.wait(delay)
            self._wake.clear()
            if not self._stop.is_set():
                self.refresh_if_due()

    def stop(self, timeout=11):
        with self._state:
            self._closed = True
            self._id_token = self._refresh_token = None
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(0, timeout))
        return self._thread is None or not self._thread.is_alive()


class _SafeResponse:
    def __init__(self, response, *, conditional=False):
        self._response = response
        self.status_code = response.status_code
        self._conditional = conditional is True
        self.headers = {name: response.headers[name] for name in (
            'ETag', 'Date', 'Content-Length', 'Content-Type', 'Cache-Control') if name in response.headers}

    @property
    def diagnostic_details(self):
        return safe_error_details(
            status=self.status_code, conditional=self._conditional,
        )

    @property
    def text(self):
        if self.status_code >= 400:
            return f'Firebase request failed (HTTP {self.status_code})'
        try:
            return self._response.text
        except Exception as exc:
            details = safe_error_details(exc, status=self.status_code,
                                         conditional=self._conditional)
            if details['category'] == 'unknown':
                details['category'], details['request_phase'] = 'response_invalid', 'response'
            raise FirebaseTransportError(
                'Firebase response unavailable', category=details['category'],
                status=details['http_status'], request_phase=details['request_phase'],
            ) from None

    def json(self):
        if self.status_code >= 400 and self.status_code != 412:
            return {'error': ('authentication_or_permission_denied'
                              if self.status_code in (401, 403) else 'firebase_request_failed')}
        try:
            return self._response.json()
        except Exception as exc:
            details = safe_error_details(exc, status=self.status_code,
                                         conditional=self._conditional)
            if details['category'] in {'unknown', 'http_error', 'precondition_failed'}:
                details['category'], details['request_phase'] = 'response_invalid', 'response'
            raise FirebaseTransportError(
                'Firebase response could not be decoded',
                category=details['category'], status=details['http_status'],
                request_phase=details['request_phase'],
            ) from None

    def iter_content(self, chunk_size=1024):
        if self.status_code >= 400 and self.status_code != 412:
            yield json.dumps(self.json()).encode('utf-8')
            return
        try:
            yield from self._response.iter_content(chunk_size=chunk_size)
        except Exception as exc:
            details = safe_error_details(exc, status=self.status_code,
                                         conditional=self._conditional)
            if details['category'] in {'unknown', 'http_error', 'precondition_failed'}:
                details['category'], details['request_phase'] = 'response_invalid', 'response'
            raise FirebaseTransportError(
                'Firebase response stream interrupted',
                category=details['category'], status=details['http_status'],
                request_phase=details['request_phase'],
            ) from None

    def close(self):
        try:
            self._response.close()
        except Exception:
            pass


class AuthenticatedFirebaseHTTP:
    """Drop-in request methods, preserving ETags/body/query/timeouts and no retries."""
    def __init__(self, provider):
        self.provider = provider
        self.database_url = provider.credentials.database_url
        self.elder_id = provider.credentials.elder_id
        self.device_id = provider.credentials.device_id

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        base = urlsplit(self.database_url)
        if (parsed.scheme != base.scheme or parsed.netloc != base.netloc or parsed.query
                or parsed.fragment or parsed.username or parsed.password or '\\' in url
                or not parsed.path.startswith('/') or not parsed.path.endswith('.json')):
            raise FirebaseTransportError(
                'Firebase request target does not match configured backend',
                category='transport_rejected', request_phase='before_request',
            )
        if method not in ('GET', 'PUT', 'PATCH', 'POST', 'DELETE'):
            raise FirebaseTransportError(
                'Unsupported Firebase method', category='transport_rejected',
                request_phase='before_request',
            )
        if any(k not in {'params', 'headers', 'json', 'data', 'timeout', 'stream'} for k in kwargs):
            raise FirebaseTransportError(
                'Unsupported Firebase transport override',
                category='transport_rejected', request_phase='before_request',
            )
        params = dict(kwargs.pop('params', None) or {})
        if any(str(k).lower() in {'auth', 'access_token', 'ns'} for k in params):
            raise FirebaseTransportError(
                'Firebase authentication query override refused',
                category='transport_rejected', request_phase='before_request',
            )
        headers = dict(kwargs.get('headers') or {})
        if any(str(k).lower() in {'authorization', 'host', 'cookie'} for k in headers):
            raise FirebaseTransportError(
                'Firebase authentication header override refused',
                category='transport_rejected', request_phase='before_request',
            )
        params['auth'] = self.provider.get_id_token()
        if self.provider.emulator is not None:
            params['ns'] = self.provider.credentials.project_id + '-default-rtdb'
        kwargs['params'], kwargs['headers'] = params, headers
        kwargs.setdefault('timeout', (3, 7))
        timeouts = kwargs['timeout'] if isinstance(kwargs['timeout'], tuple) else (kwargs['timeout'],)
        if (len(timeouts) not in (1, 2) or any(not isinstance(n, (int, float))
                or isinstance(n, bool) or not math.isfinite(n) or not 0 < n <= 30 for n in timeouts)):
            raise FirebaseTransportError(
                'Firebase request requires bounded timeouts',
                category='transport_rejected', request_phase='before_request',
            )
        try:
            response = self.provider.http.request(method, url, allow_redirects=False, **kwargs)
            if 300 <= response.status_code < 400:
                status = response.status_code
                response.close()
                raise FirebaseTransportError(
                    'Firebase redirect refused', category='http_error',
                    status=status, request_phase='response',
                )
            conditional = any(str(name).lower() == 'if-match' for name in headers)
            return _SafeResponse(response, conditional=conditional)
        except FirebaseTransportError:
            raise
        except Exception as exc:
            # Do not attach requests' response/request/URL containing auth query.
            details = safe_error_details(exc)
            raise FirebaseTransportError(
                'Firebase transport failed; write outcome may be unknown',
                category=details['category'], status=details['http_status'],
                request_phase=details['request_phase'],
            ) from None

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def put(self, url, **kwargs):
        return self.request('PUT', url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request('PATCH', url, **kwargs)

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request('DELETE', url, **kwargs)
