"""Bounded memory manifest and generation-fenced immutable publication.

Only _control is CAS-written. Bodies are immutable, individually addressed, and
become visible exclusively through its bounded manifest. A delayed body PUT can
leave an orphan but cannot publish into a generation that has been reset.
"""
from copy import deepcopy
from email.utils import parsedate_to_datetime
import json
import time
import uuid

from firebase_auth import safe_error_details

MAX_RECORD_BYTES = 4096
MAX_CONTROL_BYTES = 65536
MAX_LEASES = 16
MAX_MAINTENANCE_IDS = 128
CANDIDATE_FIELDS = frozenset(('type', 'subject', 'content', 'lastMentionedAt', 'importance',
                              'tags', 'eventDate', 'eventDatePrecision', 'validUntil', 'evidenceKey'))
BODY_FIELDS = CANDIDATE_FIELDS | frozenset(('id', 'elderId', 'source', 'sourceSessionId', 'status',
                                         'createdAt', 'updatedAt', 'topicKey', 'archivedAt', 'archiveReason'))
CONTROL_PATH_TEMPLATE = '/memories/{elder}/_control.json'
BODY_PATH_TEMPLATE = '/memories/{elder}/active/{body}.json'


def _annotate_diagnostic(exc, *, stage, source_stage=None, method='GET',
                         path_template='unknown', status=None, conditional=False,
                         category=None, request_phase=None):
    """Attach only allowlisted, non-sensitive context and preserve ``exc``."""
    try:
        if getattr(exc, '_memory_diagnostic', None) is not None:
            return exc
        details = safe_error_details(exc, status=status, conditional=conditional)
        if category is not None and details['category'] == 'unknown':
            details['category'] = category
        if request_phase is not None and details['request_phase'] == 'unknown':
            details['request_phase'] = request_phase
        for key, value in details.items():
            setattr(exc, key, value)
        if details['request_phase'] == 'before_request' and stage != 'transport_pre_request':
            source_stage, stage = stage, 'transport_pre_request'
        exc._memory_diagnostic = {
            'stage': stage,
            'source_stage': source_stage,
            'method': method,
            'path_template': path_template,
        }
    except Exception:
        # Diagnostics must never replace the original exception or control flow.
        pass
    return exc


def _valid_time(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 253402267199000


def _json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))


class MemoryStorageProtocol:
    def endpoint(self, path):
        return self.url[:-5] + '/' + path + '.json'

    def control_read(self, *, timeout=None):
        started = time.monotonic()
        try:
            response = self.http.get(self.endpoint('_control'),
                                     headers={'X-Firebase-ETag': 'true', 'Cache-Control': 'no-cache'},
                                     timeout=self.REQUEST_TIMEOUT if timeout is None else timeout)
        except Exception as exc:
            _annotate_diagnostic(exc, stage='control_read', path_template=CONTROL_PATH_TEMPLATE)
            raise
        status = response.status_code
        if status != 200:
            error = RuntimeError('Memory control unavailable')
            _annotate_diagnostic(error, stage='control_read', path_template=CONTROL_PATH_TEMPLATE,
                                 status=status)
            raise error
        try:
            data = response.json()
        except Exception as exc:
            _annotate_diagnostic(exc, stage='response_validation', source_stage='control_read',
                                 path_template=CONTROL_PATH_TEMPLATE,
                                 status=status, category='response_invalid',
                                 request_phase='response')
            raise
        data = {} if data is None else data
        etag = response.headers.get('ETag') or response.headers.get('etag')
        if not isinstance(data, dict) or not isinstance(etag, str) or not etag:
            error = ValueError('Invalid memory control')
            _annotate_diagnostic(error, stage='response_validation', source_stage='control_read',
                                 path_template=CONTROL_PATH_TEMPLATE,
                                 status=status, category='response_invalid',
                                 request_phase='response')
            raise error
        try:
            self.validate_control(data)
            generation = self.generation({'_control': data}) if 'generation' in data else 'initial'
            if not self.observe_generation(generation):
                raise ValueError('Regressed memory generation')
        except Exception as exc:
            _annotate_diagnostic(exc, stage='response_validation', source_stage='control_read',
                                 path_template=CONTROL_PATH_TEMPLATE, status=status,
                                 category='response_invalid', request_phase='response')
            raise
        try:
            server_ms = int(parsedate_to_datetime(response.headers['Date']).timestamp() * 1000)
        except (KeyError, TypeError, ValueError):
            server_ms = None
        return data, etag, server_ms, started

    def validate_control(self, data):
        if not isinstance(data, dict) or _json_bytes(data) > MAX_CONTROL_BYTES:
            raise ValueError('Invalid memory control size')
        if 'generation' in data:
            generation = data['generation']
            if not ((isinstance(generation, str) and 0 < len(generation) <= 80)
                    or (isinstance(generation, int) and not isinstance(generation, bool)
                        and 0 <= generation < 9007199254740991)):
                raise ValueError('Invalid memory generation')
        for field, limit in (('activeIds', 128), ('archiveIds', 64), ('leases', MAX_LEASES),
                             ('resetOperations', 32), ('resetCoveredIds', 64)):
            entries = data.get(field)
            entries = {} if entries is None else entries
            if not isinstance(entries, dict) or len(entries) > limit:
                raise ValueError('Memory manifest exceeds protocol limit')
            if any(not self.valid_record_key(key) for key in entries):
                raise ValueError('Invalid manifest key')
        if any(value is not True for value in (data.get('activeIds') or {}).values()):
            raise ValueError('Invalid active manifest value')
        if any(value is not True for value in (data.get('resetCoveredIds') or {}).values()):
            raise ValueError('Invalid reset cohort value')
        if any(not _valid_time(value) for value in (data.get('archiveIds') or {}).values()):
            raise ValueError('Invalid archive manifest value')
        for lease in (data.get('leases') or {}).values():
            if not isinstance(lease, dict) or not _valid_time(lease.get('expiresAt')):
                raise ValueError('Invalid lease expiry')
        barrier = data.get('pendingReset')
        if barrier is not None and (not isinstance(barrier, dict)
                                   or not self.valid_record_key(barrier.get('operationId'))
                                   or not _valid_time(barrier.get('createdAt'))):
            raise ValueError('Invalid memory reset barrier')
        for receipt in (data.get('resetOperations') or {}).values():
            if not isinstance(receipt, dict) or receipt.get('status') not in (None, 'pending', 'completed', 'failed'):
                raise ValueError('Invalid memory reset receipt')
        coordinator = data.get('resetCoordinator')
        if coordinator is not None:
            if (not isinstance(coordinator, dict)
                    or not self.valid_record_key(coordinator.get('activeOperationId'))
                    or not self.valid_record_key(coordinator.get('deviceId'))
                    or coordinator.get('state') not in ('processing', 'committed', 'completed')
                    or not isinstance(coordinator.get('epoch'), int) or isinstance(coordinator['epoch'], bool)
                    or not 0 < coordinator['epoch'] < 9007199254740991
                    or not _valid_time(coordinator.get('claimedAt'))):
                raise ValueError('Invalid reset coordinator')
            for field in ('baseGeneration', 'resultingGeneration'):
                if field == 'resultingGeneration' and coordinator['state'] == 'processing':
                    continue
                value = coordinator.get(field)
                if not ((isinstance(value, str) and 0 < len(value) <= 80)
                        or (isinstance(value, int) and not isinstance(value, bool)
                            and 0 <= value < 9007199254740991)):
                    raise ValueError('Invalid reset coordinator generation')
            if coordinator['state'] == 'completed':
                stamp = coordinator.get('completedAt')
                if stamp != {'.sv': 'timestamp'} and not _valid_time(stamp):
                    raise ValueError('Invalid reset coordinator completion')

    @staticmethod
    def valid_record_key(key):
        return (isinstance(key, str) and 0 < len(key) <= 80
                and not any(c in key for c in '.#$[]/')
                and all(ord(c) >= 32 and ord(c) != 127 for c in key))

    def control_put(self, data, etag):
        self.validate_control(data)
        result = self.http.put(self.endpoint('_control'), json=data,
                               headers={'if-match': etag}, timeout=self.REQUEST_TIMEOUT)
        if result.status_code == 412:
            return False
        if not 200 <= result.status_code < 300:
            raise RuntimeError('Memory control write failed')
        return True

    def project_candidate(self, value):
        if (not isinstance(value, dict) or value.get('type') not in ('profile', 'episodic', 'preference')
                or not isinstance(value.get('subject'), str) or not value['subject'].strip() or len(value['subject']) > 60
                or not isinstance(value.get('content'), str) or not value['content'].strip() or len(value['content']) > 280
                or not _valid_time(value.get('lastMentionedAt'))):
            return None
        tags = value.get('tags', [])
        if (not isinstance(tags, list) or len(tags) > 6
                or any(not isinstance(tag, str) or not 1 <= len(tag) <= 30 for tag in tags)):
            return None
        for field in ('eventDate', 'eventDatePrecision', 'evidenceKey'):
            if field in value and (not isinstance(value[field], str) or len(value[field]) > 64):
                return None
        if 'validUntil' in value and not _valid_time(value['validUntil']):
            return None
        row = {key: deepcopy(value[key]) for key in CANDIDATE_FIELDS if key in value}
        try:
            return row if _json_bytes(row) <= MAX_RECORD_BYTES else None
        except (ValueError, TypeError):
            return None

    def project_body(self, value):
        if not self._owned(value) or self.project_candidate(value) is None:
            return None
        row = {key: deepcopy(value[key]) for key in BODY_FIELDS if key in value}
        try:
            return row if _json_bytes(row) <= MAX_RECORD_BYTES else None
        except (ValueError, TypeError):
            return None

    def active_read(self, control, owned_ids=None, *, timeout=None):
        records = {}
        try:
            identities = iter(control.get('activeIds') or {})
        except Exception as exc:
            _annotate_diagnostic(exc, stage='active_manifest_validation',
                                 source_stage='control_read',
                                 path_template=CONTROL_PATH_TEMPLATE,
                                 status=200, category='response_invalid',
                                 request_phase='response')
            raise
        while True:
            try:
                identity = next(identities)
            except StopIteration:
                break
            except Exception as exc:
                _annotate_diagnostic(exc, stage='active_manifest_validation',
                                     source_stage='control_read',
                                     path_template=CONTROL_PATH_TEMPLATE,
                                     status=200, category='response_invalid',
                                     request_phase='response')
                raise
            try:
                raw = self.read_bounded_body('active/' + identity, timeout=timeout)
                if owned_ids is not None and self._owned(raw):
                    owned_ids.add(identity)
                row = self.project_body(raw)
                if row is not None:
                    records[identity] = row
            except Exception as exc:
                _annotate_diagnostic(exc, stage='response_validation',
                                     source_stage='body_read',
                                     path_template=BODY_PATH_TEMPLATE,
                                     status=200, category='response_invalid',
                                     request_phase='response')
                raise
        return records

    def read_bounded_body(self, path, *, timeout=None):
        """Bound wire consumption before decoding even unknown legacy metadata.

        iter_content caps decompressed bytes too. At most one 1 KiB chunk past
        the 4 KiB schema limit is consumed, then the response is closed. This is
        separate from explicit maintenance of malformed/unreferenced bodies.
        """
        try:
            response = self.http.get(self.endpoint(path),
                                     timeout=self.REQUEST_TIMEOUT if timeout is None else timeout,
                                     stream=True)
        except Exception as exc:
            _annotate_diagnostic(exc, stage='body_read', path_template=BODY_PATH_TEMPLATE)
            raise
        status = None
        try:
            status = response.status_code
            if status != 200:
                error = RuntimeError('Memory record unavailable')
                _annotate_diagnostic(error, stage='body_read', path_template=BODY_PATH_TEMPLATE,
                                     status=status)
                raise error
            body = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=1024):
                    if not chunk:
                        continue
                    if len(body) + len(chunk) > MAX_RECORD_BYTES:
                        return None
                    body.extend(chunk)
            except Exception as exc:
                _annotate_diagnostic(exc, stage='body_read', path_template=BODY_PATH_TEMPLATE,
                                     status=status)
                raise
            try:
                return json.loads(body)
            except Exception as exc:
                _annotate_diagnostic(exc, stage='response_validation', source_stage='body_read',
                                     path_template=BODY_PATH_TEMPLATE,
                                     status=status, category='response_invalid',
                                     request_phase='response')
                raise
        finally:
            try:
                response.close()
            except Exception as exc:
                _annotate_diagnostic(exc, stage='response_validation', source_stage='body_read',
                                     path_template=BODY_PATH_TEMPLATE, status=status)
                raise

    def protocol_snapshot(self, *, timeout=None):
        control, etag, _, _ = self.control_read(timeout=timeout)
        rows = {} if control.get('pendingReset') else self.active_read(control, timeout=timeout)
        return {**rows, '_control': control}, etag

    def protocol_save(self, candidates, session_id, generation):
        if (generation is None or not self.valid_record_key(session_id)
                or not isinstance(candidates, (list, tuple)) or not candidates):
            return False
        candidates = [row for candidate in candidates[:8] if (row := self.project_candidate(candidate)) is not None]
        if not candidates:
            return False
        for _ in range(2):
            pending = []
            publication_rejected = False
            try:
                control, etag, _, _ = self.control_read()
                current = self.generation({'_control': control}) if 'generation' in control else 'initial'
                if current != generation or control.get('pendingReset'):
                    return False
                owned_ids = set()
                existing = self.active_read(control, owned_ids)
                merged = self._prune(self._merge(existing, candidates, session_id, generation))
                # Only confirmed conversation-owned bodies belong to this
                # writer. Preserve foreign and unreadable manifest entries;
                # bounded reads cannot prove ownership of an oversized body.
                active = {identity: True for identity in (control.get('activeIds') or {})
                          if identity not in owned_ids}
                available = 128 - len(active)
                if not available:
                    return False
                owned_active = [(identity, row) for identity, row in merged.items()
                                if row.get('status') == 'active']
                owned_active.sort(key=lambda item: (item[1]['lastMentionedAt'], item[0]), reverse=True)
                for identity, row in owned_active[:available]:
                    if existing.get(identity) == row:
                        active[identity] = True
                        continue
                    row = self.project_body(row)
                    if row is None:
                        continue
                    # Never overwrite a body a reader or concurrent writer owns.
                    identity = 'm_' + uuid.uuid4().hex
                    pending.append(identity)
                    result = self.http.put(self.endpoint('active/' + identity), json=row, timeout=self.REQUEST_TIMEOUT)
                    if not 200 <= result.status_code < 300:
                        raise RuntimeError('Memory body write failed')
                    active[identity] = True
                next_control = {**control, 'schemaVersion': 2, 'generation': generation, 'activeIds': active}
                # No parent PUT and no generation-only recheck: the CAS publishes
                # the exact manifest under the same reset publication barrier.
                if self.control_put(next_control, etag):
                    self.cleanup_paths({'active/' + key: None for key in owned_ids if key not in active})
                    return True
                publication_rejected = True
            except Exception:
                pass
            # A read-back cannot rule out a timed-out CAS arriving later. Only a
            # definite 412 permits eager deletion; ambiguous bodies are retained
            # until explicit maintenance first fences old publication attempts.
            try:
                latest, _, _, _ = self.control_read()
                live = latest.get('activeIds') or {}
                if pending and all(key in live for key in pending):
                    return True
                if publication_rejected:
                    self.cleanup_paths({'active/' + key: None for key in pending if key not in live})
            except Exception:
                pass
        return False

    def cleanup_paths(self, changes):
        if changes:
            try:
                # A multi-location PATCH has only named leaves; it never replaces
                # the elder bucket, foreign data, or care truth.
                response = self.http.patch(self.url, json=changes, timeout=self.REQUEST_TIMEOUT)
                return 200 <= response.status_code < 300
            except Exception:
                return False  # Explicit maintenance can retry detached bodies.
        return True

    def maintain_orphans(self, active_ids, archive_ids):
        """Explicit maintenance of <=128 known IDs from an offline inventory.

        Never enumerates the elder bucket or runs on the voice path. A fresh
        control CAS invalidates every older publication before deleting absent
        IDs. Protocol writers never reuse immutable body keys. A delayed body
        PUT may recreate an invisible orphan, which a later pass can remove.
        """
        if (not isinstance(active_ids, (list, tuple)) or not isinstance(archive_ids, (list, tuple))
                or len(active_ids) + len(archive_ids) > MAX_MAINTENANCE_IDS
                or any(not self.valid_record_key(identity) for identity in (*active_ids, *archive_ids))):
            raise ValueError('Bounded known body IDs required')
        for _ in range(2):
            try:
                control, etag, _, _ = self.control_read()
                if control.get('pendingReset'):
                    return False
                live = set(control.get('activeIds') or {}) | set(control.get('archiveIds') or {})
                changes = {f'{kind}/{identity}': None for kind, identities in (
                    ('active', active_ids), ('archive', archive_ids)) for identity in identities if identity not in live}
                if not changes:
                    return True
                if not self.control_put({**control, 'maintenanceRevision': uuid.uuid4().hex}, etag):
                    continue
                return self.cleanup_paths(changes)
            except Exception:
                # An ambiguous fence must not authorize any body deletion.
                return False
        return False

    def migrate_legacy(self, records, migration_id):
        """Explicit admin import of a bounded, preselected offline export.

        Never called by voice/startup and never discovers/downloads legacy rows.
        Foreign source=user/ai rows stay untouched; reset since session creation
        and missing trustworthy generation prevent promotion. Marker is CASed.
        """
        if not self.valid_record_key(migration_id) or not isinstance(records, dict) or len(records) > 128:
            raise ValueError('Bounded migration input required')
        if len(json.dumps(records, ensure_ascii=False).encode()) > 128 * MAX_RECORD_BYTES:
            raise ValueError('Migration input too large')
        control, etag, _, _ = self.control_read()
        if control.get('legacyMigration') == migration_id:
            return True
        # Only baseline generation imports are eligible; a reset is a durable
        # tombstone for ALL earlier memories, including unmigrated legacy rows.
        if control.get('generation', 'initial') != 'initial' or control.get('pendingReset'):
            return False
        candidates = [row for row in records.values() if self._owned(row) and row.get('status') == 'active'
                      and len(json.dumps(row, ensure_ascii=False).encode()) <= MAX_RECORD_BYTES]
        for offset in range(0, len(candidates), 8):
            if not self.protocol_save(candidates[offset:offset + 8], 'legacy_' + migration_id[:60], 'initial'):
                return False
        control, etag, _, _ = self.control_read()
        if control.get('generation', 'initial') != 'initial' or control.get('pendingReset'):
            return False
        return self.control_put({**control, 'legacyMigration': migration_id, 'schemaVersion': 2}, etag)
