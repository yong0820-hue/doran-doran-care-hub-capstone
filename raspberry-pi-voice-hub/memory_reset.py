"""Single-device v1 reset coordinator.

The device owns destructive reset. Its dispatch barrier is never bypassed by a
TTL: completion is published only while exclusive ownership still protects the
new generation and the synchronous local invalidation callback. All workers in
one voice process must share this barrier; multi-device elders need an all-device
acknowledgement protocol before this contract can be extended to them.
"""
from copy import deepcopy
import hashlib
import json
import threading
import time
from urllib.parse import quote

from memory_storage import _valid_time


class MemoryResetWorker:
    QUERY_LIMIT = 32
    BARRIER_TIMEOUT = 0.25
    POLL_INTERVAL = 1.0
    CAS_ATTEMPTS = 3
    ARCHIVE_RETENTION_MS = 30 * 86400000

    def __init__(self, store, barrier, on_reset, device_id='device_001'):
        self.store, self.barrier, self.on_reset = store, barrier, on_reset
        self.device_id = device_id
        self.url = (store.base_url + '/memoryResetOperations/'
                    + quote(store.elder_id, safe='') + '.json')
        self._stopped = threading.Event()
        self._processing = threading.Lock()
        self._thread = None
        self._retry_ids = set()
        self._legacy_cursor = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped.clear()
        self._thread = threading.Thread(target=self._run, name='memory-reset', daemon=True)
        self._thread.start()

    def stop(self, timeout=1.0):
        self._stopped.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(max(0.0, timeout))

    def _run(self):
        while not self._stopped.is_set():
            self.process_pending()
            self._stopped.wait(self.POLL_INTERVAL)

    def _operation_url(self, identity):
        if not self.store.valid_record_key(identity):
            raise ValueError('Invalid reset operation ID')
        return self.url[:-5] + '/' + identity + '.json'

    def _read_operation(self, identity):
        response = self.store.http.get(self._operation_url(identity),
                                       headers={'X-Firebase-ETag': 'true', 'Cache-Control': 'no-cache'},
                                       timeout=self.store.REQUEST_TIMEOUT)
        if response.status_code != 200:
            raise RuntimeError('Reset operation unavailable')
        operation = response.json()
        etag = response.headers.get('ETag') or response.headers.get('etag')
        if not isinstance(operation, dict) or not isinstance(etag, str) or not etag:
            raise ValueError('Invalid reset operation')
        return operation, etag

    def _put_operation(self, identity, operation, etag):
        response = self.store.http.put(self._operation_url(identity), json=operation,
                                       headers={'if-match': etag}, timeout=self.store.REQUEST_TIMEOUT)
        if response.status_code == 412:
            return False
        if not 200 <= response.status_code < 300:
            raise RuntimeError('Reset operation write uncertain')
        return True

    def _query_pending(self):
        found = {}
        for status in ('processing', 'requested'):
            response = self.store.http.get(self.url, params={
                'orderBy': json.dumps('deviceStatus'),
                'equalTo': json.dumps(self.device_id + '|' + status),
                'limitToFirst': self.QUERY_LIMIT,
            }, timeout=self.store.REQUEST_TIMEOUT)
            if response.status_code != 200:
                raise RuntimeError('Reset queue unavailable')
            rows = response.json()
            if rows is None:
                continue
            if not isinstance(rows, dict) or len(rows) > self.QUERY_LIMIT:
                raise ValueError('Reset query exceeded protocol bounds')
            found.update({key: row for key, row in rows.items()
                          if self.store.valid_record_key(key) and isinstance(row, dict)
                          and row.get('deviceId') == self.device_id})
        # Upgrade compatibility: walk old unindexed operations by key, instead
        # of repeatedly filtering the same first 32 old-device rows. New requests
        # always use the device/status index and need no history scan to progress.
        params = {'orderBy': json.dumps('$key'), 'limitToFirst': self.QUERY_LIMIT + 1}
        if self._legacy_cursor is not None:
            params['startAt'] = json.dumps(self._legacy_cursor)
        response = self.store.http.get(self.url, params=params, timeout=self.store.REQUEST_TIMEOUT)
        if response.status_code != 200:
            raise RuntimeError('Legacy reset queue unavailable')
        rows = response.json() or {}
        if not isinstance(rows, dict) or len(rows) > self.QUERY_LIMIT + 1:
            raise ValueError('Legacy reset query exceeded protocol bounds')
        keys = sorted(rows)
        self._legacy_cursor = keys[-1] if len(keys) == self.QUERY_LIMIT + 1 else None
        found.update({key: row for key, row in rows.items()
                      if self.store.valid_record_key(key) and isinstance(row, dict)
                      and 'deviceStatus' not in row and row.get('deviceId') == self.device_id
                      and row.get('status') in ('requested', 'processing')})
        return found

    def process_pending(self):
        """One bounded poll, also usable synchronously by deterministic tests.

        Read/permission failures are uncertainty, never definitive reset failure.
        A remembered ambiguous completion is re-read by ID even if the server
        applied it and it has therefore disappeared from the pending query.
        """
        if not self._processing.acquire(blocking=False):
            return 0
        try:
            found = self._query_pending()
            control, _, _, _ = self.store.control_read()
            pending_id = (control.get('pendingReset') or {}).get('operationId')
            coordinator = control.get('resetCoordinator') or {}
            leader_id = coordinator.get('activeOperationId') if coordinator.get('state') != 'completed' else None
            extras = set(self._retry_ids)
            if self.store.valid_record_key(pending_id):
                extras.add(pending_id)
            if self.store.valid_record_key(leader_id):
                extras.add(leader_id)
            for identity in extras:
                if identity not in found:
                    found[identity], _ = self._read_operation(identity)
            ordered = sorted(found, key=lambda identity: (
                identity != (pending_id or leader_id), found[identity].get('status') != 'processing',
                found[identity].get('requestedAt') if _valid_time(found[identity].get('requestedAt')) else 0,
                identity))
            completed = 0
            for identity in ordered[:self.QUERY_LIMIT * 2]:
                if self._stopped.is_set():
                    break
                completed += int(self._process(identity))
            return completed
        except Exception:
            return 0
        finally:
            self._processing.release()

    @staticmethod
    def _generation(control):
        return control.get('generation', 'initial')

    def _receipt_control(self, control, identity, generation, coalesced=False, leader=None):
        receipts = dict(control.get('resetOperations') or {})
        receipts[identity] = {'status': 'completed', 'generation': generation,
                              'completedAt': int(time.time() * 1000), 'coalesced': coalesced}
        if leader is not None:
            receipts[identity]['coalescedTo'] = leader
        coordinator = control.get('resetCoordinator') or {}
        if not coalesced and coordinator.get('activeOperationId') == identity:
            receipts[identity].update(epoch=coordinator['epoch'], baseGeneration=coordinator['baseGeneration'])
        # Keep the just-written receipt even if a previous device had a skewed
        # clock. The external operation is its permanent idempotency record.
        others = sorted((key for key in receipts if key != identity),
                        key=lambda key: (receipts[key].get('completedAt', 0), key), reverse=True)[:31]
        return {**control, 'resetOperations': {key: receipts[key] for key in (*others, identity)}}

    def _coalesced_result(self, control, identity, operation):
        """Bind immutable client intent before deciding to claim a new epoch.

        A snapshot taken before a claim carries its base generation; a snapshot
        during a claim/commit carries the leader ID. Neither can become a new
        destructive reset just because its operation PUT arrives late.
        """
        coordinator = control.get('resetCoordinator') or {}
        intent = operation.get('intent')
        if intent is not None:
            bound = intent.get('operationId')
            if bound and bound != identity:
                leader, _ = self._read_operation(bound)
                if leader.get('deviceId') != self.device_id or leader.get('status') != 'completed':
                    raise RuntimeError('Reset leader is not completed')
                generation = leader.get('resultingGeneration', leader.get('generation'))
                if not self._valid_generation(generation):
                    raise ValueError('Invalid reset leader result')
                return generation, bound
        if coordinator and coordinator.get('activeOperationId') != identity:
            if coordinator.get('state') != 'completed':
                raise RuntimeError('Reset leader must finish before its followers')
            if intent is not None:
                covered = intent['generation'] != self._generation(control)
            else:
                # Old clients have no causal token. Treat timestamp ties as the
                # same intent conservatively; upgraded clients distinguish them.
                covered = operation['requestedAt'] <= coordinator.get('completedAt', 0)
            if covered:
                if intent is not None:
                    return self._preclaim_result(control, intent)
                return coordinator['resultingGeneration'], coordinator['activeOperationId']
        if intent is not None and intent['generation'] != self._generation(control):
            raise RuntimeError('Stale reset intent has no authoritative leader')
        if (control.get('resetCoveredIds') or {}).get(identity) is True:
            return self._generation(control), None  # v4 already-committed cohort
        return None

    def _preclaim_result(self, control, intent):
        # A preclaim snapshot belongs to the immediately following epoch, even
        # if several later intentional resets finish before its PUT arrives.
        epoch = intent['epoch'] + 1
        coordinator = control.get('resetCoordinator') or {}
        if (coordinator.get('epoch') == epoch
                and coordinator.get('baseGeneration') == intent['generation']):
            return coordinator['resultingGeneration'], coordinator['activeOperationId']
        for identity, receipt in (control.get('resetOperations') or {}).items():
            if (receipt.get('epoch') == epoch and receipt.get('status') == 'completed'
                    and not receipt.get('coalesced') and receipt.get('baseGeneration') == intent['generation']):
                return receipt['generation'], identity
        # Internal receipts are bounded. The permanent leader operation retains
        # a device/epoch index; this lookup never scans old operation history.
        response = self.store.http.get(self.url, params={
            'orderBy': json.dumps('deviceEpoch'), 'equalTo': json.dumps(self.device_id + '|' + str(epoch)),
            'limitToFirst': 2,
        }, timeout=self.store.REQUEST_TIMEOUT)
        if response.status_code != 200:
            raise RuntimeError('Reset epoch result unavailable')
        rows = response.json()
        if not isinstance(rows, dict) or len(rows) != 1:
            raise RuntimeError('Reset epoch has no unique authoritative leader')
        identity, operation = next(iter(rows.items()))
        if (not self.store.valid_record_key(identity) or not isinstance(operation, dict)
                or operation.get('deviceId') != self.device_id or operation.get('status') != 'completed'
                or operation.get('resetEpoch') != epoch or operation.get('coalesced')
                or operation.get('baseGeneration') != intent['generation']
                or not self._valid_generation(operation.get('resultingGeneration'))):
            raise ValueError('Invalid reset epoch result')
        return operation['resultingGeneration'], identity

    @staticmethod
    def _valid_generation(value):
        return ((isinstance(value, str) and 0 < len(value) <= 80)
                or (isinstance(value, int) and not isinstance(value, bool)
                    and 0 <= value < 9007199254740991))

    def _valid_intent(self, intent):
        return intent is None or (isinstance(intent, dict) and intent.get('version') == 1
            and self._valid_generation(intent.get('generation'))
            and isinstance(intent.get('epoch'), int) and not isinstance(intent['epoch'], bool)
            and 0 <= intent['epoch'] < 9007199254740991
            and (intent.get('operationId') is None or self.store.valid_record_key(intent['operationId'])))

    def _finish_coordinator(self, identity):
        # Keep the coordinator joinable until the permanent leader operation is
        # completed. ACK loss/restart resumes this CAS, never the destructive CAS.
        for _ in range(self.CAS_ATTEMPTS):
            control, etag, _, _ = self.store.control_read()
            coordinator = control.get('resetCoordinator') or {}
            if coordinator.get('activeOperationId') != identity or coordinator.get('state') == 'completed':
                return True
            if coordinator.get('state') != 'committed':
                return False
            completed = {**coordinator, 'state': 'completed', 'completedAt': {'.sv': 'timestamp'}}
            if self.store.control_put({**control, 'resetCoordinator': completed}, etag):
                return True
        return False

    def _process(self, identity):
        # Reject known malformed requests before entering reset mode. A corrupt
        # queue row must neither starve valid requests nor latch voice dispatch.
        try:
            operation, operation_etag = self._read_operation(identity)
            if operation.get('deviceId') != self.device_id:
                self._retry_ids.discard(identity)
                return False
            if (operation.get('status') not in ('completed', 'failed')
                    and (operation.get('status') not in ('requested', 'processing')
                         or not _valid_time(operation.get('requestedAt'))
                         or not self._valid_intent(operation.get('intent'))
                         or operation.get('requestedBy') != 'guardian_dashboard')):
                control, _, _, _ = self.store.control_read()
                if (control.get('pendingReset') or {}).get('operationId') != identity:
                    self._put_operation(identity, {**operation, 'status': 'failed',
                                                  'deviceStatus': self.device_id + '|failed',
                                                  'failureCode': 'invalid_request'}, operation_etag)
                    self._retry_ids.discard(identity)
                return False
        except Exception:
            return False
        self._retry_ids.add(identity)
        if not self.barrier.acquire_exclusive(self.BARRIER_TIMEOUT):
            return False
        acknowledged = False
        try:
            for _ in range(self.CAS_ATTEMPTS):
                operation, operation_etag = self._read_operation(identity)
                if operation.get('deviceId') != self.device_id:
                    return False
                control, etag, _, _ = self.store.control_read()
                if operation.get('status') == 'failed' and not control.get('pendingReset'):
                    self.on_reset(control)
                    acknowledged = True
                    return False
                if operation.get('status') == 'completed':
                    self.on_reset(control)
                    acknowledged = self._finish_coordinator(identity)
                    return acknowledged
                if (operation.get('status') not in ('requested', 'processing')
                        or not _valid_time(operation.get('requestedAt'))
                        or not self._valid_intent(operation.get('intent'))
                        or operation.get('requestedBy') != 'guardian_dashboard'):
                    return False
                pending = control.get('pendingReset')
                if pending and pending.get('operationId') != identity:
                    return False
                if operation['status'] == 'requested':
                    operation = {**operation, 'status': 'processing',
                                 'deviceStatus': self.device_id + '|processing',
                                 'baseGeneration': self._generation(control),
                                 'processingAt': {'.sv': 'timestamp'}}
                    if not self._put_operation(identity, operation, operation_etag):
                        continue
                receipt = (control.get('resetOperations') or {}).get(identity)
                if not receipt or receipt.get('status') != 'completed':
                    result = self._coalesced_result(control, identity, operation)
                    if result is not None and not pending:
                        generation, leader = result
                        updated = self._receipt_control(control, identity, generation, True, leader)
                        if not self.store.control_put(updated, etag):
                            continue
                    else:
                        coordinator = control.get('resetCoordinator') or {}
                        if not pending or coordinator.get('activeOperationId') != identity:
                            pending = {'operationId': identity, 'createdAt': int(time.time() * 1000)}
                            coordinator = {'activeOperationId': identity, 'deviceId': self.device_id,
                                           'epoch': coordinator.get('epoch', 0) + 1, 'state': 'processing',
                                           'baseGeneration': self._generation(control),
                                           'claimedAt': int(time.time() * 1000)}
                            if not self.store.control_put({**control, 'pendingReset': pending,
                                                           'resetCoordinator': coordinator}, etag):
                                continue
                            # A successful fence excludes all extraction CASes
                            # that began before the reset, without a root write.
                            control, etag, _, _ = self.store.control_read()
                        if not self._reset(control, etag, identity):
                            continue
                # Resolve server timestamps and any ambiguous reset CAS by an
                # authoritative read, then synchronously install local state.
                control, _, _, _ = self.store.control_read()
                receipt = (control.get('resetOperations') or {}).get(identity)
                if not receipt or receipt.get('status') != 'completed' or control.get('pendingReset'):
                    continue
                self.on_reset(control)
                operation, operation_etag = self._read_operation(identity)
                if operation.get('status') == 'completed':
                    acknowledged = self._finish_coordinator(identity)
                    return acknowledged
                completed = {**operation, 'status': 'completed',
                             'deviceStatus': self.device_id + '|completed',
                             'generation': receipt['generation'], 'resultingGeneration': receipt['generation'],
                             'completedAt': {'.sv': 'timestamp'}, 'coalesced': receipt.get('coalesced', False)}
                if receipt.get('coalescedTo'):
                    completed['coalescedTo'] = receipt['coalescedTo']
                if not receipt.get('coalesced') and 'epoch' in receipt:
                    completed.update(resetEpoch=receipt['epoch'], baseGeneration=receipt['baseGeneration'],
                                     deviceEpoch=self.device_id + '|' + str(receipt['epoch']))
                if self._put_operation(identity, completed, operation_etag):
                    acknowledged = self._finish_coordinator(identity)
                    return acknowledged
            return False
        except Exception:
            return False
        finally:
            if acknowledged:
                self._retry_ids.discard(identity)
            # An uncertain write may arrive later. Keep subsequent dispatches
            # memory-free until this operation has been reconciled under lock.
            self.barrier.release_exclusive(clear_pending=acknowledged)

    def _reset(self, control, etag, identity):
        if (control.get('pendingReset') or {}).get('operationId') != identity:
            return False
        active = dict(control.get('activeIds') or {})
        archive = dict(control.get('archiveIds') or {})
        cleanup = {}
        now = int(time.time() * 1000)
        for key in list(active):
            response = self.store.http.get(self.store.endpoint('active/' + key), timeout=self.store.REQUEST_TIMEOUT)
            if response.status_code != 200:
                raise RuntimeError('Reset memory body unavailable')
            raw = response.json()
            # A corrupt manifest cannot grant ownership over user/care/other
            # elders' data. Preserve both their body and active manifest entry.
            if not self.store._owned(raw):
                continue
            row = self.store.project_body(raw)
            if row is None:
                continue
            archive_key = 'r_' + hashlib.sha256((identity + '/' + key).encode()).hexdigest()[:40]
            archived = {**row, 'status': 'archived', 'archivedAt': now, 'archiveReason': 'guardian_reset'}
            projected = self.store.project_body(archived)
            if projected is None:
                # Valid active bodies can sit exactly at the transport cap;
                # remove optional metadata to leave room for archive markers.
                for field in ('sourceSessionId', 'topicKey', 'tags', 'evidenceKey',
                              'eventDate', 'eventDatePrecision', 'validUntil'):
                    archived.pop(field, None)
                    projected = self.store.project_body(archived)
                    if projected is not None:
                        break
            if projected is None:
                raise ValueError('Archive body cannot fit bounded schema')
            response = self.store.http.put(self.store.endpoint('archive/' + archive_key),
                                           json=projected, timeout=self.store.REQUEST_TIMEOUT)
            if not 200 <= response.status_code < 300:
                raise RuntimeError('Reset archive write uncertain')
            archive[archive_key] = now
            del active[key]
            cleanup['active/' + key] = None
        eligible = [(key, stamp) for key, stamp in archive.items() if stamp >= now - self.ARCHIVE_RETENTION_MS]
        kept = dict(sorted(eligible, key=lambda item: (item[1], item[0]), reverse=True)[:64])
        for key in archive.keys() - kept.keys():
            response = self.store.http.get(self.store.endpoint('archive/' + key), timeout=self.store.REQUEST_TIMEOUT)
            if response.status_code != 200:
                raise RuntimeError('Reset archive ownership unavailable')
            if self.store._owned(response.json()):
                cleanup['archive/' + key] = None
        previous = self._generation(control)
        generation = previous + 1 if isinstance(previous, int) and not isinstance(previous, bool) else 1
        updated = self._receipt_control(control, identity, generation)
        updated.update(schemaVersion=2, generation=generation, activeIds=active, archiveIds=kept,
                       leases={}, lastResetAt={'.sv': 'timestamp'}, resetCutoffAt={'.sv': 'timestamp'},
                       resetCoordinator={**control['resetCoordinator'], 'state': 'committed',
                                         'resultingGeneration': generation})
        updated.pop('resetCoveredIds', None)
        updated.pop('pendingReset', None)
        if not self.store.control_put(updated, etag):
            return False
        self.store.cleanup_paths(cleanup)
        return True
