"""Offline migration fixtures contain invented text only; no live operations."""

import contextlib
import fcntl
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import migrate_receipts as migration


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'bridge'
        self.receipts = self.root / 'receipts'
        self.receipts.mkdir(parents=True)
        self.journal = self.base / 'frozen.jsonl'
        self.source = self.base / 'frozen.memory-source'
        self.source.write_bytes(b'0123456789abcdef' * 3)
        self.journal.write_bytes(b'')

    def job(self, lease='old-secret-lease', job_id=188, **fields):
        return {'agent': 'sample', 'id': job_id, 'room': 'hot-tub', 'model': 'sample-model',
                'runtime': 'sample-runtime', 'lease': lease, 'status': 'replying',
                'prompt': 'PRIVATE_PROMPT', 'system': 'PRIVATE_SYSTEM',
                'requester': 'PRIVATE_REQUESTER', 'query': 'PRIVATE_QUERY', **fields}

    def key(self, job):
        return migration.legacy_key(job['agent'], job['id'], job['lease'])

    def save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps(value, indent=2).encode() + b'\n')

    def records(self, *records):
        self.journal.write_bytes(b''.join(json.dumps(record).encode() + b'\n' for record in records))

    def snapshot(self, job):
        return {'type': 'job', 'job': job}

    def generation(self, job, text='PRIVATE_REPLY'):
        value = {'text': text, 'work': {'artifacts': [{'path': 'artifacts/sample.txt',
                                                     'sha256': '0' * 64, 'bytes': 12}]},
                 'session': 'PRIVATE_SESSION', 'output_tokens': 2}
        self.save(self.receipts / (self.key(job) + '.json'), value)
        return value

    def work(self, job):
        value = {'actions': [{'name': 'read', 'identity': '1' * 64}],
                 'context': 'PRIVATE_CONTEXT', 'before': {}}
        self.save(self.receipts / (self.key(job) + '-work.json'), value)

    def attempt(self, snapshot, **fields):
        self.save(self.receipts / (self.key(snapshot) + '-attempt.json'),
                  {'agent': snapshot['agent'], 'job': snapshot['id'], 'started_at': 123, **fields})

    def accept(self, job, generation, message=True):
        self.save(self.root / 'shared' / job['agent'] / (self.key(job) + '.json'),
                  {'agent': job['agent'], 'job_id': job['id'], 'room': job['room'],
                   'model': job['model'], 'text': generation['text'],
                   'artifacts': generation['work']['artifacts']})
        return {'type': 'result', 'job': {**job, 'status': 'complete', 'result_received': True},
                'message': {'text': generation['text']} if message else None}

    def migrate(self, **kwargs):
        return migration.migrate(self.journal, self.source, self.root, **kwargs)

    def anchor(self, job):
        return json.loads((self.receipts / 'anchors' /
                           (migration.anchor_key(job['agent'], job['id']) + '.json')).read_bytes())

    def reject(self, **kwargs):
        with self.assertRaises((migration.MigrationError, OSError)):
            self.migrate(**kwargs)
        self.assertFalse((self.receipts / 'anchors-version.json').exists())

    def test_normal_old_generation_dry_run_then_exact_replay_anchor(self):
        old = self.job()
        self.generation(old)
        self.attempt(old)
        self.records(self.snapshot(old), self.snapshot(self.job(lease='later-no-receipt')))
        result = self.migrate()
        self.assertEqual(result['anchors'][0]['state'], 'active')
        self.assertEqual(result['anchors'][0]['key'], self.key(old))
        self.assertEqual(result['anchors'][0]['route_snapshot']['line'], 2)
        self.assertFalse((self.receipts / 'anchors').exists())
        self.migrate(apply=True)
        anchor = self.anchor(old)
        self.assertEqual(anchor['key'], self.key(old))
        self.assertEqual(set(anchor), {'version', 'agent', 'job_id', 'room', 'model', 'runtime',
                                      'key', 'first_key', 'legacy_keys', 'state'})

    def test_accepted_later_key_keeps_first_failed_commands(self):
        early, later = self.job(), self.job(lease='later-secret-lease')
        self.work(early)
        # Simulate an old recorded failed command, not reconstructable arguments.
        self.save(self.receipts / (self.key(early) + '-work.json'),
                  {'actions': [{'name': 'shell', 'identity': '0' * 64,
                                'status': 'error', 'result': {'exit_code': 1}}]})
        gen = self.generation(later)
        accepted = self.accept(later, gen)
        self.records(self.snapshot(early), self.snapshot({**early, 'status': 'failed'}),
                     self.snapshot(later), accepted, self.snapshot(self.job(lease='third')))
        result = self.migrate(apply=True)
        anchor = self.anchor(early)
        self.assertEqual(anchor['state'], 'accepted')
        self.assertEqual(anchor['key'], self.key(later))
        self.assertEqual(anchor['first_key'], self.key(early))
        self.assertEqual(anchor['legacy_keys'], [self.key(early), self.key(later)])
        self.assertEqual(result['anchors'][0]['route_snapshot']['line'], 4)
        self.assertTrue((self.receipts / (self.key(early) + '-work.json')).exists())

    def test_unfinished_action_is_blocked(self):
        job = self.job(job_id=171)
        self.work(job)
        self.records(self.snapshot(job))
        self.migrate(apply=True)
        self.assertEqual(self.anchor(job)['state'], 'blocked')
        self.assertIn('arguments_unavailable', self.anchor(job)['reason'])

    def test_attempt_without_generation_is_blocked(self):
        job = self.job()
        self.attempt(job)
        self.records(self.snapshot(job))
        result = self.migrate()
        self.assertEqual(result['anchors'][0]['state'], 'blocked')
        self.assertIn('uncertain_execution', result['anchors'][0]['reason'])

    def test_multiple_unaccepted_generations_are_blocked(self):
        first, second = self.job(lease='z-first'), self.job(lease='a-second')
        self.generation(first)
        self.generation(second)
        self.records(self.snapshot(first), self.snapshot(second))
        self.migrate(apply=True)
        anchor = self.anchor(first)
        self.assertEqual(anchor['state'], 'blocked')
        self.assertEqual(anchor['key'], self.key(first))
        self.assertEqual(anchor['legacy_keys'], [self.key(first), self.key(second)])

    def test_first_key_is_first_receipt_bearing_not_first_lease(self):
        first, second = self.job(lease='no-file'), self.job(lease='has-file')
        self.generation(second)
        self.records(self.snapshot(first), self.snapshot(second))
        result = self.migrate()
        self.assertEqual(result['anchors'][0]['first_key'], self.key(second))
        self.assertEqual(result['anchors'][0]['generations'][0]['first'],
                         {'line': 2, 'byte': len(json.dumps(self.snapshot(first)).encode()) + 1})

    def test_unknown_receipt_key_rejected(self):
        self.generation(self.job())
        self.records(self.snapshot(self.job(lease='other')))
        self.reject(apply=True)

    def test_unknown_shared_key_rejected(self):
        job = self.job()
        gen = self.generation(job)
        self.accept(job, gen)
        self.reject(apply=True)

    def test_unknown_receipt_filename_rejected(self):
        self.save(self.receipts / 'surprise.json', {})
        self.reject()

    def test_metadata_is_preserved_not_treated_as_generations(self):
        for name in ('active-sample.json', 'sample-pause.json', 'sample-funded-pause.json'):
            self.save(self.receipts / name, {'text': 'PRIVATE_METADATA'})
        job = self.job()
        self.generation(job)
        self.records(self.snapshot(job))
        result = self.migrate(apply=True)
        self.assertEqual(len(result['anchors']), 1)
        self.assertEqual(len(result['original_sha256']), 4)

    def test_attempt_identity_conflict(self):
        job = self.job()
        self.attempt(job, agent='other')
        self.records(self.snapshot(job))
        self.reject(apply=True)

    def test_attempt_boolean_job_id_rejected(self):
        job = self.job(job_id=1)
        self.attempt(job, job=True)
        self.records(self.snapshot(job))
        self.reject()

    def test_shared_identity_conflict(self):
        job = self.job()
        gen = self.generation(job)
        event = self.accept(job, gen)
        path = self.root / 'shared' / job['agent'] / (self.key(job) + '.json')
        value = json.loads(path.read_bytes())
        value['job_id'] += 1
        self.save(path, value)
        self.records(event)
        self.reject(apply=True)

    def test_shared_text_and_artifacts_must_match(self):
        job = self.job()
        gen = self.generation(job)
        event = self.accept(job, gen)
        path = self.root / 'shared' / job['agent'] / (self.key(job) + '.json')
        original = json.loads(path.read_bytes())
        for field, value in (('text', 'different'), ('artifacts', [])):
            with self.subTest(field=field):
                self.save(path, {**original, field: value})
                self.records(event)
                self.reject()

    def test_route_conflict_same_lease(self):
        job = self.job()
        self.generation(job)
        for field in ('room', 'model', 'runtime'):
            with self.subTest(field=field):
                self.records(self.snapshot(job), self.snapshot({**job, field: 'changed'}))
                self.reject()

    def test_route_conflict_different_receipt_bearing_leases(self):
        first, second = self.job(), self.job(lease='next', room='changed')
        self.generation(first)
        self.generation(second)
        self.records(self.snapshot(first), self.snapshot(second))
        self.reject()

    def test_ignores_nonreceipt_route_change_for_selected_snapshot(self):
        job = self.job()
        self.generation(job)
        self.records(self.snapshot(job), self.snapshot(self.job(lease='other', room='different')))
        result = self.migrate()
        self.assertEqual(result['anchors'][0]['route_snapshot']['line'], 1)

    def test_truncated_invalid_and_blank_jsonl_rejected(self):
        for raw in (b'{"type":"tub"}', b'{"type":\n', b'\n', b'\xff\n', b'[]\n'):
            with self.subTest(raw=raw):
                self.journal.write_bytes(raw)
                self.reject(apply=True)

    def test_exact_line_bound_includes_newline(self):
        prefix, suffix = b'{"type":"profile","padding":"', b'"}\n'
        self.journal.write_bytes(prefix + b'x' * (migration.MAX_LINE - len(prefix) - len(suffix)) + suffix)
        self.assertEqual(self.migrate()['journal_bytes'], migration.MAX_LINE)
        self.journal.write_bytes(prefix + b'x' * (migration.MAX_LINE + 1 - len(prefix) - len(suffix)) + suffix)
        self.reject()

    def test_row_and_total_bounds(self):
        self.records({'type': 'tub'}, {'type': 'profile'})
        with patch.object(migration, 'MAX_ROWS', 1):
            self.reject()
        with patch.object(migration, 'MAX_TOTAL', 49):
            self.reject()

    def test_known_types_and_optional_nested_job_only(self):
        self.records(*({'type': kind, 'job': None, 'message': None}
                       for kind in sorted(migration.TYPES)))
        self.assertEqual(self.migrate()['journal_rows'], len(migration.TYPES))
        self.records({'type': 'unknown'})
        self.reject()
        job = self.job()
        self.generation(job)
        self.records({'type': 'job', **job})  # Not a nested job snapshot.
        self.reject()

    def test_result_received_failure_is_not_acceptance(self):
        job = self.job(status='failed', result_received=True)
        gen = self.generation(job)
        self.records(self.snapshot(job))
        self.assertEqual(self.migrate()['anchors'][0]['state'], 'active')
        self.accept(job, gen)
        self.reject()
        self.records({'type': 'result', 'job': job, 'message': {'text': gen['text']}})
        self.reject()

    def test_shared_without_success_result_is_rejected(self):
        job = self.job(status='complete')
        gen = self.generation(job)
        self.accept(job, gen)
        self.records(self.snapshot(job))
        self.reject()

    def test_success_result_message_must_match_unless_null(self):
        job = self.job()
        gen = self.generation(job)
        event = self.accept(job, gen)
        event['message']['text'] = 'different'
        self.records(event)
        self.reject()
        event['message'] = None
        self.records(event)
        self.assertEqual(self.migrate()['anchors'][0]['state'], 'accepted')

    def test_multiple_conflicting_accepted_results_rejected(self):
        first, second = self.job(), self.job(lease='later')
        a = self.accept(first, self.generation(first, 'one'))
        b = self.accept(second, self.generation(second, 'two'))
        self.records(a, b)
        self.reject(apply=True)

    def test_identical_multiple_accepted_generations_blocked_for_review(self):
        first, second = self.job(), self.job(lease='later')
        a = self.accept(first, self.generation(first))
        b = self.accept(second, self.generation(second))
        self.records(a, b)
        self.assertEqual(self.migrate()['anchors'][0]['state'], 'blocked')

    def test_preserves_all_original_bytes_and_idempotent_apply(self):
        first, second = self.job(), self.job(lease='later')
        self.work(first)
        self.attempt(first)
        event = self.accept(second, self.generation(second))
        self.records(self.snapshot(first), event)
        originals = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        original_journal, original_source = self.journal.read_bytes(), self.source.read_bytes()
        first_result = self.migrate(apply=True)
        outputs = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                   for path in self.receipts.rglob('*') if path.is_file()}
        second_result = self.migrate(apply=True)
        self.assertEqual(first_result, second_result)
        for path, raw in originals.items():
            self.assertEqual(path.read_bytes(), raw)
        for path, (raw, timestamp) in outputs.items():
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(path.stat().st_mtime_ns, timestamp)
        self.assertEqual(self.journal.read_bytes(), original_journal)
        self.assertEqual(self.source.read_bytes(), original_source)
        marker = json.loads((self.receipts / 'anchors-version.json').read_bytes())
        self.assertEqual(marker, {'version': 1, 'source_sha256': hashlib.sha256(original_source).hexdigest(),
                                 'journal_sha256': hashlib.sha256(original_journal).hexdigest(),
                                 'legacy_migration': True})

    def test_bridge_and_journal_lock_denial(self):
        for path in (self.root / 'bridge.lock', self.journal):
            with self.subTest(path=path.name), path.open('a+b') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.reject(apply=True)

    def test_wrong_source_or_journal_marker_conflict(self):
        job = self.job()
        self.generation(job)
        self.records(self.snapshot(job))
        self.migrate(apply=True)
        marker = (self.receipts / 'anchors-version.json').read_bytes()
        self.source.write_bytes(b'a' * 48)
        with self.assertRaises(migration.MigrationError):
            self.migrate(apply=True)
        self.source.write_bytes(b'0123456789abcdef' * 3)
        with self.journal.open('ab') as stream:
            stream.write(b'{"type":"tub"}\n')
        with self.assertRaises(migration.MigrationError):
            self.migrate(apply=True)
        self.assertEqual((self.receipts / 'anchors-version.json').read_bytes(), marker)

    def test_new_format_marker_not_silently_converted(self):
        self.save(self.receipts / 'anchors-version.json', {'version': 1, 'legacy_migration': False})
        original = (self.receipts / 'anchors-version.json').read_bytes()
        with self.assertRaises(migration.MigrationError):
            self.migrate(apply=True)
        self.assertEqual((self.receipts / 'anchors-version.json').read_bytes(), original)

    def test_source_file_exact_bound_and_hex_check(self):
        for raw in (b'a' * 47, b'a' * 49, b'a' * 48 + b'\n', b'a' * 4096, b'z' * 48):
            with self.subTest(size=len(raw)):
                self.source.write_bytes(raw)
                self.reject()
        self.source.write_bytes(b'ABCDEF0123456789' * 3)
        self.assertEqual(self.migrate()['source_sha256'], hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_preexisting_anchor_conflict_does_not_publish_marker(self):
        job = self.job()
        self.generation(job)
        self.records(self.snapshot(job))
        path = self.receipts / 'anchors' / (migration.anchor_key(job['agent'], job['id']) + '.json')
        self.save(path, {'version': 1, 'state': 'conflict'})
        original = path.read_bytes()
        self.reject(apply=True)
        self.assertEqual(path.read_bytes(), original)

    def test_marker_last_and_partial_apply_is_recoverable(self):
        job = self.job()
        self.generation(job)
        self.records(self.snapshot(job))
        real_publish = migration.publish
        order = []

        def fail_marker(path, value):
            order.append(path.name)
            if path.name == 'anchors-version.json':
                raise OSError('invented I/O failure')
            real_publish(path, value)

        with patch.object(migration, 'publish', side_effect=fail_marker):
            self.reject(apply=True)
        self.assertEqual(order[-1], 'anchors-version.json')
        self.assertEqual(self.anchor(job)['state'], 'active')
        self.migrate(apply=True)
        self.assertTrue((self.receipts / 'anchors-version.json').exists())

    def test_original_mutation_during_apply_prevents_marker(self):
        job = self.job()
        self.generation(job)
        self.records(self.snapshot(job))
        real_publish = migration.publish

        def mutate(path, value):
            real_publish(path, value)
            (self.receipts / (self.key(job) + '.json')).write_bytes(b'{}')

        with patch.object(migration, 'publish', side_effect=mutate):
            self.reject(apply=True)

    def test_duplicate_members_floats_nonfinite_rejected(self):
        for raw in (b'{"type":"tub","type":"profile"}\n',
                    b'{"type":"tub","number":1.5}\n',
                    b'{"type":"tub","number":NaN}\n'):
            self.journal.write_bytes(raw)
            self.reject()

    def test_symlinks_rejected(self):
        (self.receipts / ('0' * 64 + '.json')).symlink_to(self.source)
        self.reject(apply=True)

    def test_manifest_safe_creation_private_and_content_free(self):
        job = self.job()
        self.generation(job)
        self.records(self.snapshot(job), {'type': 'profile', 'profile': {'text': 'PRIVATE_PROFILE'}})
        target = self.base / 'private-manifest.json'
        result = self.migrate(manifest=target)
        self.assertEqual(json.loads(target.read_bytes()), result)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        public = target.read_text()
        for private in ('PRIVATE_', job['lease'], 'prompt', 'system', 'session', 'requester'):
            self.assertNotIn(private, public)
        original = target.read_bytes()
        with self.assertRaises(migration.MigrationError):
            self.migrate(manifest=target)
        self.assertEqual(target.read_bytes(), original)
        self.reject(manifest=self.receipts / 'new-report.json')

    def test_accepted_earlier_generation_wins_over_later_final_generation(self):
        first, later = self.job(), self.job(lease='later')
        accepted = self.accept(first, self.generation(first))
        self.generation(later, 'other-unaccepted-reply')
        self.records(accepted, self.snapshot(later))
        self.migrate(apply=True)
        self.assertEqual(self.anchor(first)['state'], 'accepted')
        self.assertEqual(self.anchor(first)['key'], self.key(first))

    def test_result_for_other_lease_does_not_accept_shared(self):
        job = self.job()
        accepted = self.accept(job, self.generation(job))
        accepted['job']['lease'] = 'other-lease'
        self.records(self.snapshot(job), accepted)
        self.reject()

    def test_boolean_marker_version_is_not_integer_version(self):
        self.migrate(apply=True)
        path = self.receipts / 'anchors-version.json'
        marker = json.loads(path.read_bytes())
        marker['version'] = True
        self.save(path, marker)
        with self.assertRaises(migration.MigrationError):
            self.migrate(apply=True)

    def test_existing_equivalent_output_preserved_and_manifest_hash_is_actual(self):
        job = self.job()
        self.generation(job)
        self.records(self.snapshot(job))
        self.migrate(apply=True)
        name = migration.anchor_key(job['agent'], job['id']) + '.json'
        path = self.receipts / 'anchors' / name
        self.save(path, self.anchor(job))  # Equivalent JSON, different original bytes.
        original = path.read_bytes()
        result = self.migrate(apply=True)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(result['output_sha256'][name], hashlib.sha256(original).hexdigest())

    def test_cli_default_dry_run_and_sanitized_error(self):
        args = ['--journal', str(self.journal), '--source', str(self.source), '--root', str(self.root)]
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(migration.main(args), 0)
        self.assertFalse(json.loads(output.getvalue())['applied'])
        self.assertFalse((self.receipts / 'anchors-version.json').exists())
        self.journal.write_bytes(b'PRIVATE_BROKEN_JSON\n')
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(migration.main(args), 2)
        self.assertNotIn('PRIVATE_', output.getvalue() + error.getvalue())
        self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
