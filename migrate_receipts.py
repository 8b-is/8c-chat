#!/usr/bin/env python3
"""Offline, additive legacy Bridge receipt migration (stdlib; POSIX flock).

Stop writers and supply a frozen journal and its exact memory-source sidecar:
  python3 migrate_receipts.py --journal FROZEN --source MEMORY_SOURCE --root BRIDGE
Review the private JSON manifest; repeat with --apply to publish anchors. Optional
--manifest PATH safely creates a mode-0600 copy (never overwrites). No inference,
network, tools, receipt replay, or repair is performed. Originals are never edited.

The bridge and journal locks are nonblocking and held throughout verification and
publication. Locks are advisory: operators must exclude non-cooperating writers
and use trusted, offline directories. Individual outputs are atomic, not the whole
batch; the version marker is published last. An interrupted batch can be rerun with
identical inputs. A conflicting output/marker requires manual review, not deletion.
"""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

MAX_LINE = 131_072                 # Includes the required newline.
MAX_TOTAL = 128 * 1024 * 1024       # Journal + source + original receipt/shared bytes.
MAX_ROWS = 1_000_000
MAX_FILES = 100_000
SOURCE_BYTES = 48
TYPES = frozenset(('message', 'job', 'result', 'tub', 'profile', 'import_complete'))
KEY_RE = re.compile(r'([0-9a-f]{64})(-work|-attempt)?\.json\Z')
ANCHOR_RE = re.compile(r'[0-9a-f]{64}\.json\Z')
AGENT_RE = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
METADATA_RE = re.compile(r'(?:active-[A-Za-z0-9_-]+|[A-Za-z0-9_-]+-(?:funded-)?pause)\.json\Z')


class MigrationError(ValueError):
    """Deliberately content-free diagnostic safe to display to an operator."""


def require(condition, reason):
    if not condition:
        raise MigrationError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def legacy_key(agent, job_id, lease):
    return sha(json.dumps([agent, job_id, lease], separators=(',', ':')).encode())


def anchor_key(agent, job_id):
    return sha(json.dumps([agent, job_id], separators=(',', ':')).encode())


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode()


def _not_integer(_):
    raise MigrationError('Non-integer JSON number rejected')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate JSON member rejected')
        result[key] = value
    return result


def decode(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_float=_not_integer, parse_constant=_not_integer)
    except (UnicodeError, ValueError, RecursionError):
        raise MigrationError('Invalid or unsupported JSON') from None


def checked_path(path):
    """Reject symlink components, rather than silently resolving them."""
    path = Path(os.path.abspath(path))
    for part in reversed((path, *path.parents)):
        require(not part.is_symlink(), 'Symlink path rejected')
    return path


def directory(path):
    path = checked_path(path)
    require(path.is_dir(), 'Required directory unavailable')
    return path


@contextmanager
def regular(path, *, create=False):
    path = checked_path(path)
    flags = (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), 'Non-regular file rejected')
        with os.fdopen(fd, 'r+b' if create else 'rb', closefd=False) as stream:
            yield stream
    finally:
        os.close(fd)


def lock(stream):
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise MigrationError('Writer lock held; migration refused') from None


class Budget:
    def __init__(self):
        self.bytes = 0

    def add(self, size):
        self.bytes += size
        require(self.bytes <= MAX_TOTAL, 'Total input byte bound exceeded')


def read_bytes(path, budget=None, bound=MAX_TOTAL):
    if budget is not None:
        bound = min(bound, MAX_TOTAL - budget.bytes)
    with regular(path) as stream:
        require(os.fstat(stream.fileno()).st_size <= bound, 'Input file byte bound exceeded')
        raw = stream.read(bound + 1)
    require(len(raw) <= bound, 'Input file byte bound exceeded')
    if budget is not None:
        budget.add(len(raw))
    return raw


def source_bytes(path, budget=None):
    raw = read_bytes(path, budget, SOURCE_BYTES)
    require(len(raw) == SOURCE_BYTES and re.fullmatch(b'[0-9a-fA-F]{48}', raw) is not None,
            'Memory source must be exactly 48 ASCII hex bytes')
    return raw


def _entries(path):
    # Bound enumeration itself, not only the eventual JSON payloads.
    result = []
    with os.scandir(directory(path)) as entries:
        for entry in entries:
            require(len(result) < MAX_FILES, 'Directory entry bound exceeded')
            require(not entry.is_symlink(), 'Symlink entry rejected')
            result.append(Path(entry.path))
    return sorted(result)


def inventory(root, budget):
    """Hash every original, but parse only generation/work/attempt/shared files."""
    originals, generations = {}, {}

    def remember(path):
        require(len(originals) < MAX_FILES, 'Original file count bound exceeded')
        raw = read_bytes(path, budget)
        originals[str(path.relative_to(root))] = sha(raw)
        return raw

    for path in _entries(root / 'receipts'):
        if path.name == 'anchors':
            directory(path)
            continue
        if path.name == 'anchors-version.json':
            continue
        match = KEY_RE.fullmatch(path.name)
        if match:
            key, suffix = match.groups()
            kind = {None: 'generation', '-work': 'work', '-attempt': 'attempt'}[suffix]
            value = decode(remember(path))
            require(isinstance(value, dict), 'Receipt must be a JSON object')
            generations.setdefault(key, {})[kind] = value
        else:
            require(METADATA_RE.fullmatch(path.name) is not None, 'Unknown receipt filename')
            remember(path)
    shared = root / 'shared'
    if shared.exists():
        for agent in _entries(shared):
            require(AGENT_RE.fullmatch(agent.name) is not None, 'Invalid shared agent directory')
            for path in _entries(agent):
                require(ANCHOR_RE.fullmatch(path.name) is not None, 'Unknown shared filename')
                key = path.stem
                value = decode(remember(path))
                require(isinstance(value, dict), 'Shared receipt must be a JSON object')
                entry = generations.setdefault(key, {})
                require('shared' not in entry, 'Receipt shared by multiple agents')
                entry['shared'] = value
                entry['shared_agent'] = agent.name
    return originals, generations


def route(job):
    require(isinstance(job, dict), 'Invalid job snapshot')
    agent, job_id = job.get('agent'), job.get('id')
    require(isinstance(agent, str) and AGENT_RE.fullmatch(agent) is not None,
            'Invalid job agent identity')
    require(type(job_id) is int and 0 < job_id <= 9_223_372_036_854_775_807,
            'Invalid job integer identity')
    fields = tuple(job.get(field) for field in ('room', 'model', 'runtime'))
    require(all(isinstance(v, str) and 0 < len(v) <= 1024 for v in fields),
            'Invalid job route')
    return (agent, job_id, *fields)


def generation_content(entry):
    generation = entry.get('generation')
    require(isinstance(generation, dict) and isinstance(generation.get('text'), str),
            'Missing or invalid final generation')
    work = generation.get('work', {})
    require(isinstance(work, dict), 'Invalid generation work')
    artifacts = work.get('artifacts', [])
    require(isinstance(artifacts, list), 'Invalid generation artifacts')
    return generation['text'], artifacts


def validate_identity(entry, identity):
    agent, job_id, room, model, runtime = identity
    if 'generation' in entry:
        generation_content(entry)
    if 'attempt' in entry:
        attempt = entry['attempt']
        require(attempt.get('agent') == agent and type(attempt.get('job')) is int
                and attempt['job'] == job_id, 'Attempt identity conflict')
    if 'shared' in entry:
        shared = entry['shared']
        require(entry['shared_agent'] == agent and shared.get('agent') == agent
                and type(shared.get('job_id')) is int and shared['job_id'] == job_id,
                'Shared identity conflict')
        require(shared.get('room') == room and shared.get('model') == model
                and shared.get('runtime', runtime) == runtime, 'Shared route conflict')
        text, artifacts = generation_content(entry)
        require(shared.get('text') == text and encode(shared.get('artifacts')) == encode(artifacts),
                'Shared generation content conflict')


def scan_journal(stream, entries, budget):
    digest = hashlib.sha256()
    matched, latest, agents = {}, {}, {}
    rows, offset = 0, 0
    require(os.fstat(stream.fileno()).st_size <= MAX_TOTAL, 'Journal byte bound exceeded')
    while True:
        raw = stream.readline(MAX_LINE + 1)
        if not raw:
            break
        rows += 1
        require(rows <= MAX_ROWS, 'Journal row bound exceeded')
        require(len(raw) <= MAX_LINE and raw.endswith(b'\n'),
                'Incomplete or oversized journal record')
        budget.add(len(raw))
        digest.update(raw)
        record = decode(raw)
        require(isinstance(record, dict) and isinstance(record.get('type'), str)
                and record['type'] in TYPES, 'Unknown journal record type')
        kind = record['type']
        if kind == 'result':
            require('message' in record and (record['message'] is None
                    or isinstance(record['message'], dict)), 'Invalid result message envelope')
        position = {'line': rows, 'byte': offset}
        offset += len(raw)
        job = record.get('job')
        if job is None:
            continue
        identity = route(job)
        lease = job.get('lease', '')
        require(isinstance(lease, str), 'Invalid lease representation')
        # Hash ALL nonempty leases, including failed/replaced leases. Never use a
        # latest-job-only replay, which would erase earlier receipt provenance.
        key = legacy_key(identity[0], identity[1], lease) if lease else None
        if key in entries:
            require(agents.setdefault(identity[1], identity[0]) == identity[0],
                    'Receipt-bearing job agent changed')
            if key not in matched:
                validate_identity(entries[key], identity)
                matched[key] = {'route': identity, 'first': position, 'last': position,
                                'accepted_result': None}
            info = matched[key]
            require(info['route'] == identity, 'Receipt-bearing route changed')
            info['last'] = position
            latest[identity] = position
            if kind == 'result' and job.get('status') == 'complete':
                entry = entries[key]
                # A completed journal record without a final receipt cannot prove
                # replayable output. Shared records require both proofs below.
                if 'generation' in entry:
                    text, _ = generation_content(entry)
                    message = record['message']
                    require(message is None or message.get('text') == text,
                            'Successful journal result text conflict')
                    info['accepted_result'] = position
        if identity in latest:
            latest[identity] = position
    require(len(matched) == len(entries), 'Unknown or unmapped legacy receipt key')
    return matched, latest, digest.hexdigest(), rows, offset


def classify(entries, matched, latest):
    groups = {}
    for key, info in matched.items():
        groups.setdefault(info['route'][:2], []).append(key)
    anchors, manifests = {}, []
    for (agent, job_id), keys in sorted(groups.items()):
        keys.sort(key=lambda key: matched[key]['first']['line'])
        identity = matched[keys[0]]['route']
        require(all(matched[key]['route'] == identity for key in keys),
                'Route changed across receipt-bearing snapshots')
        accepted = []
        for key in keys:
            if 'shared' in entries[key]:
                require(matched[key]['accepted_result'] is not None,
                        'Shared receipt lacks successful journal result')
                accepted.append(key)
        chosen, state, reason = keys[0], 'blocked', 'multiple_unaccepted_generations'
        if len(accepted) > 1:
            contents = {encode(generation_content(entries[key])) for key in accepted}
            require(len(contents) == 1, 'Conflicting accepted results; manual review required')
            reason = 'multiple_accepted_generations_require_review'
        elif len(accepted) == 1:
            chosen, state, reason = accepted[0], 'accepted', None
        elif len(keys) == 1:
            if 'generation' in entries[chosen]:
                state, reason = 'active', None
            elif 'attempt' in entries[chosen]:
                reason = 'unfinished_attempt_uncertain_execution'
            else:
                reason = 'unfinished_work_action_arguments_unavailable'
        anchor = {'version': 1, 'agent': agent, 'job_id': job_id,
                  'room': identity[2], 'model': identity[3], 'runtime': identity[4],
                  'key': chosen, 'first_key': keys[0], 'legacy_keys': keys, 'state': state}
        if reason is not None:
            anchor['reason'] = reason
        stable = anchor_key(agent, job_id)
        anchors[stable + '.json'] = anchor
        snapshot = (matched[chosen]['accepted_result'] if state == 'accepted' else latest[identity])
        manifests.append({'anchor': stable, 'agent': agent, 'job_id': job_id,
                          'key': chosen, 'first_key': keys[0], 'state': state,
                          'reason': reason, 'route_snapshot': snapshot,
                          'generations': [{'key': key, 'first': matched[key]['first'],
                                           'last': matched[key]['last'],
                                           'accepted_result': matched[key]['accepted_result'],
                                           'files': sorted(k for k in entries[key] if k != 'shared_agent')}
                                          for key in keys]})
    return anchors, manifests


def fsync_directory(path):
    fd = os.open(directory(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def existing_identical(path, value, budget=None):
    if not path.exists():
        require(not path.is_symlink(), 'Symlink output rejected')
        return False
    require(encode(decode(read_bytes(path, budget))) == encode(value),
            'Conflicting existing migration output')
    return True


def publish(path, value):
    """Under bridge.lock, never replace a preexisting output, even if identical."""
    if existing_identical(path, value):
        return
    fd, name = tempfile.mkstemp(prefix='.anchor-', suffix='.tmp', dir=directory(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        if not existing_identical(path, value):
            os.replace(temporary, path)
            fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def preflight(root, anchors, marker):
    planned, existing = Budget(), Budget()
    for value in (marker, *anchors.values()):
        planned.add(len(encode(value)))
    marker_path = root / 'receipts' / 'anchors-version.json'
    if marker_path.exists():
        old = decode(read_bytes(marker_path, existing))
        # A new-format root is not evidence of legacy migration. Never upgrade it
        # silently, even if currently empty; an operator must review its history.
        require(isinstance(old, dict) and old.get('legacy_migration') is True,
                'Existing marker is not a legacy migration marker')
    existing_identical(marker_path, marker)
    target = root / 'receipts' / 'anchors'
    if target.exists():
        for path in _entries(target):
            require(path.name in anchors, 'Unknown existing anchor output')
        for name, anchor in anchors.items():
            existing_identical(target / name, anchor, existing)
    else:
        require(not target.is_symlink(), 'Symlink output directory rejected')


def verify_inputs(root, originals, journal_path, journal_hash, source_path, source_hash):
    budget = Budget()
    current, _ = inventory(root, budget)
    require(current == originals, 'Original receipt/shared files changed')
    require(sha(read_bytes(journal_path, budget)) == journal_hash, 'Frozen journal changed')
    require(sha(source_bytes(source_path, budget)) == source_hash, 'Memory source changed')


def create_manifest(path, manifest):
    path = checked_path(path)
    directory(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(encode(manifest))
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)


def migrate(journal, source, root, *, apply=False, manifest=None):
    """Return only hashes, identifiers, classifications and physical positions.

    A dry run may create bridge.lock, but does not create anchors or a marker.
    Every reject precedes publication when detectable in preflight. Unexpected
    mid-apply I/O failure may leave anchors, but never an early success marker.
    """
    root = directory(root)
    journal, source = checked_path(journal), checked_path(source)
    directory(root / 'receipts')
    if manifest is not None:
        manifest = checked_path(manifest)
        require(not manifest.exists(), 'Manifest already exists')
        # Do not let a report pollute the inventory or alias a planned output.
        require(not manifest.is_relative_to(root) and manifest not in (journal, source),
                'Manifest must be outside bridge root and input files')
        directory(manifest.parent)
    with regular(root / 'bridge.lock', create=True) as bridge_lock, regular(journal) as stream:
        lock(bridge_lock)
        lock(stream)
        budget = Budget()
        source_hash = sha(source_bytes(source, budget))
        originals, entries = inventory(root, budget)
        matched, latest, journal_hash, rows, size = scan_journal(stream, entries, budget)
        anchors, classifications = classify(entries, matched, latest)
        marker = {'version': 1, 'source_sha256': source_hash, 'journal_sha256': journal_hash,
                  'legacy_migration': True}
        preflight(root, anchors, marker)
        verify_inputs(root, originals, journal, journal_hash, source, source_hash)
        if apply:
            target = root / 'receipts' / 'anchors'
            if not target.exists():
                target.mkdir(mode=0o700)
                fsync_directory(target.parent)
            for name, anchor in sorted(anchors.items()):
                publish(target / name, anchor)
            preflight(root, anchors, marker)
            verify_inputs(root, originals, journal, journal_hash, source, source_hash)
            # The only completion signal; all anchors and originals checked first.
            publish(root / 'receipts' / 'anchors-version.json', marker)
            verify_inputs(root, originals, journal, journal_hash, source, source_hash)
        result = {'version': 1, 'applied': bool(apply), 'source_sha256': source_hash,
                  'journal_sha256': journal_hash, 'journal_rows': rows, 'journal_bytes': size,
                  'original_sha256': originals, 'anchors': classifications,
                  'output_sha256': {
                      name: sha(read_bytes(root / 'receipts' / 'anchors' / name))
                      if (root / 'receipts' / 'anchors' / name).exists() else sha(encode(value))
                      for name, value in anchors.items()}}
        if manifest is not None:
            create_manifest(manifest, result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', required=True, help='Frozen chat JSONL')
    parser.add_argument('--source', '--memory-source', required=True, help='Exact memory-source sidecar')
    parser.add_argument('--root', '--bridge-root', required=True, help='Offline Bridge root')
    parser.add_argument('--apply', action='store_true', help='Publish anchors; default is dry-run')
    parser.add_argument('--manifest', help='New private manifest file outside Bridge root')
    args = parser.parse_args(argv)
    try:
        result = migrate(args.journal, args.source, args.root, apply=args.apply, manifest=args.manifest)
    except (MigrationError, OSError, RecursionError):
        # OSError paths and JSON exception excerpts may contain private material.
        print('Migration refused: validation, lock, input/output conflict, or I/O failure; originals retained.',
              file=sys.stderr)
        return 2
    print(encode(result).decode('ascii'), end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
