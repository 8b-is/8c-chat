#!/usr/bin/env python3
"""8c shared workbench: bounded files, immutable artifacts, isolated commands."""
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

FILE_LIMIT = 16_384


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_at(root, name):
    path = Path(name)
    if path.is_absolute() or not path.parts or any(p.startswith('.') for p in path.parts):
        raise ValueError('Use a relative, non-hidden workspace path')
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in [target, *target.parents] if p != root.parent):
        raise ValueError('Workspace symlinks are not followed')
    return target


def run(directory, command, deadline_ns=None):
    """A command sees its workspace and system tools, no host home or network."""
    timeout = 20
    if deadline_ns is not None:
        if type(deadline_ns) is not int:
            raise ValueError('Invalid command deadline')
        timeout = min(timeout, (deadline_ns - time.monotonic_ns()) // 1_000_000_000)
        if timeout <= 0:
            raise ValueError('Command deadline expired before execution')
    directory = Path(directory).resolve(strict=True)
    if len(command) > 2000:
        raise ValueError('Command too long')
    args = ['/usr/bin/bwrap', '--unshare-all', '--die-with-parent', '--new-session',
            '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
            '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib', '/lib64',
            '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--dir', '/home',
            '--bind', str(directory), '/workspace']
    for name in ('.git', '.opencode', '.agents', 'AGENTS.md', 'reference', 'shared'):
        if (directory / name).exists():
            args += ['--ro-bind', str(directory / name), '/workspace/' + name]
    args += ['--chdir', '/workspace', '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
             '--setenv', 'HOME', '/tmp', '--setenv', 'LANG', 'C.UTF-8',
             '/usr/bin/bash', '--noprofile', '--norc', '-c', command]
    def limits():
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
        resource.setrlimit(resource.RLIMIT_FSIZE, (4_194_304, 4_194_304))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_AS, (2_147_483_648, 2_147_483_648))
    import tempfile
    with tempfile.TemporaryFile(dir='/tmp/opencode') as output:
        process = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT, preexec_fn=limits, start_new_session=True)
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            timed_out = True  # Partial effects are retained and must not be repeated.
        output.seek(0)
        text = output.read(FILE_LIMIT + 1)
    return {'exit_code': process.returncode, 'output': text[:FILE_LIMIT].decode(errors='replace'),
            'truncated': len(text) > FILE_LIMIT, 'timed_out': timed_out}


def artifacts(directory):
    result = {}
    root = Path(directory)
    if not (root / 'artifacts').exists():
        return result
    for scanned, path in enumerate((root / 'artifacts').rglob('*')):
        if scanned >= 1024:
            raise ValueError('Artifact directory scan exceeds bounds')
        if len(result) >= 64:
            raise ValueError('Keep at most 64 workbench artifacts')
        relative = path.relative_to(root).as_posix()
        path = file_at(root, relative)
        if path.is_file():
            if path.stat().st_size > FILE_LIMIT:
                raise ValueError('Artifacts must fit 16 KiB')
            raw = path.read_bytes()
            text = raw.decode('utf-8')
            if any(ord(c) < 32 and c not in '\n\t\r' for c in text):
                raise ValueError('Artifacts must be text')
            result[relative] = {'path': relative, 'sha256': sha(raw), 'bytes': len(raw), 'text': text}
    return result


def tool(directory, action, deadline_ns=None):
    if deadline_ns is not None and (type(deadline_ns) is not int or time.monotonic_ns() >= deadline_ns):
        raise ValueError('Tool deadline expired before execution')
    root = Path(directory)
    name = action['tool']
    if name == 'list':
        return {'files': sorted(p.relative_to(root).as_posix() for p in root.glob('*') if not p.name.startswith('.'))[:128]}
    if name == 'run':
        return run(root, action['command'], deadline_ns)
    path = file_at(root, action['path'])
    if name == 'read':
        if path.stat().st_size > 65_536:
            raise ValueError('Read a smaller file')
        return {'text': path.read_text()[:FILE_LIMIT]}
    if name == 'write':
        if Path(action['path']).parts[0] not in ('artifacts', 'src', 'tests'):
            raise ValueError('Write within artifacts/, src/, or tests/')
        raw = action['text'].encode()
        if len(raw) > FILE_LIMIT:
            raise ValueError('Write exceeds 16 KiB')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        observed = path.read_bytes()
        if observed != raw:
            raise ValueError('Written file readback did not match requested bytes')
        return {'path': action['path'], 'sha256': sha(observed)}
    raise ValueError('Unknown workbench tool')


if __name__ == '__main__':
    # Installed as an immutable command wrapper; the model supplies only the
    # command, not mounts, credentials, endpoint configuration or host paths.
    print(json.dumps(run(Path.cwd(), ' '.join(sys.argv[1:]))))
