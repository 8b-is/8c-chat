#!/usr/bin/env python3
"""8c shared workbench: bounded files, immutable artifacts, isolated commands."""
import hashlib
import json
import os
from pathlib import Path
import resource
import re
import shlex
import subprocess
import sys
import tempfile
import time

FILE_LIMIT = 16_384
FAMILY_REPOS = ('g8t', '8c-chat', 'deepsiper-enthea', 'smart-tree', 'aurora', 'CloakBrowser')
FAMILY_DOCS = ('/data/AGENTS.md', '/data/8b-is/FAMILY_CONTEXT.md', '/data/8b-is/FAMILY_SEED.md')
G8T_MCP_COMMANDS = ('health', 'endpoints', 'circuits', 'leaderboard', 'feed', 'digest', 'bench')
GH_READ_COMMANDS = {
    ('repo', 'view'), ('repo', 'list'),
    ('pr', 'list'), ('pr', 'view'), ('pr', 'checks'), ('pr', 'diff'), ('pr', 'status'),
    ('issue', 'list'), ('issue', 'view'), ('issue', 'status'),
    ('run', 'list'), ('run', 'view'),
    ('search', 'repos'), ('search', 'issues'), ('search', 'prs'), ('search', 'code'), ('search', 'commits'),
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def exact_write_contract(query):
    """Recognize one explicit literal-write form; never guess ambiguous bytes.

    Unquoted text ends at a sentence delimiter. JSON strings preserve whitespace,
    punctuation and escapes exactly. This restricts actions; it never authorizes
    them. Callers must separately enforce current-request/no-action policy.
    """
    if not isinstance(query, str) or not re.search(r'\bcontaining\s+exactly\b', query, re.I):
        return None
    error = 'For an exact write, specify one relative file path and one literal; use a JSON string for punctuation or whitespace.'
    if len(re.findall(r'\bcontaining\s+exactly\b', query, re.I)) != 1:
        raise ValueError(error)
    match = re.search(r'\b(?:create|write)\s+((?:artifacts|src|tests)/[^\s\"\'`]+)\s+containing\s+exactly\s+', query, re.I)
    if not match:
        raise ValueError(error)
    path, rest = match[1], query[match.end():]
    if any(part.startswith('.') for part in Path(path).parts):
        raise ValueError(error)
    if rest.startswith('"'):
        try:
            text, end = json.JSONDecoder().raw_decode(rest)
        except ValueError:
            raise ValueError(error) from None
        if not isinstance(text, str):
            raise ValueError(error)
        suffix = rest[end:].strip()
        if suffix.startswith('.'):
            suffix = suffix[1:].strip()
    else:
        parts = re.split(r'\.(?:\s+|$)', rest, maxsplit=1)
        text, suffix = parts[0].strip(), parts[1].strip() if len(parts) > 1 else ''
        if not text or any(c in text for c in '\n\r\"\'`'):
            raise ValueError(error)
    if suffix and not re.fullmatch(r'(?:Briefly\s+)?(?:tell|report|describe)\b[^\r\n]*', suffix, re.I):
        raise ValueError(error)
    try:
        if len(text.encode('utf-8')) > FILE_LIMIT:
            raise ValueError(error)
    except UnicodeError:
        raise ValueError(error) from None
    return {'path': path, 'text': text}


def exact_write_matches(contract, action):
    return (isinstance(action, dict) and set(action) == {'tool', 'path', 'text'}
            and action['tool'] == 'write' and action['path'] == contract['path'] and action['text'] == contract['text'])


def file_at(root, name):
    path = Path(name)
    if path.is_absolute() or not path.parts or any(p.startswith('.') for p in path.parts):
        raise ValueError('Use a relative, non-hidden workspace path')
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in [target, *target.parents] if p != root.parent):
        raise ValueError('Workspace symlinks are not followed')
    return target


def safe_host_command(command, autonomous=False):
    """Validate single-command invocations for g8t-mcp, g8t-worktree, 8c-self-deploy, cloak-browse, and gh."""
    if not isinstance(command, str) or any(c in command for c in ';&|><`\n\r$'):
        return None
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not argv:
        return None
    base = Path(argv[0]).name
    if base == 'g8t-mcp' and len(argv) >= 2 and argv[1] in G8T_MCP_COMMANDS:
        return ['/usr/local/bin/g8t-mcp', *argv[1:]]
    if base == 'g8t-worktree' and len(argv) >= 2:
        sub = argv[1]
        if sub == 'status' and len(argv) == 2:
            return ['/usr/local/bin/g8t-worktree', 'status']
        if sub == 'test' and len(argv) in (2, 3):
            if len(argv) == 3 and not re.fullmatch(r'[A-Za-z0-9_:]+', argv[2]):
                return None
            return ['/usr/local/bin/g8t-worktree', *argv[1:]]
        if sub == 'apply' and len(argv) >= 3:
            patch_arg = argv[2]
            if patch_arg.startswith('/') or '..' in Path(patch_arg).parts:
                return None
            if len(argv) > 3 and argv[3] != '--message':
                return None
            return ['/usr/local/bin/g8t-worktree', *argv[1:]]
        return None
    if base == '8c-self-deploy' and len(argv) >= 2:
        sub = argv[1]
        if sub in ('status', 'check') and len(argv) == 2:
            return ['/usr/local/bin/8c-self-deploy', sub]
        if sub in ('patch', 'apply') and len(argv) >= 3:
            patch_arg = argv[2]
            if patch_arg.startswith('/') or '..' in Path(patch_arg).parts:
                return None
            rest = argv[3:]
            if not rest:
                return ['/usr/local/bin/8c-self-deploy', sub, patch_arg]
            if sub == 'patch' and rest[0] == '--deploy':
                rest = rest[1:]
            if not rest or (len(rest) >= 2 and rest[0] == '--message'):
                return ['/usr/local/bin/8c-self-deploy', *argv[1:]]
            return None
        if sub == 'deploy':
            if len(argv) == 2 or (len(argv) >= 4 and argv[2] == '--message'):
                return ['/usr/local/bin/8c-self-deploy', *argv[1:]]
        return None
    if base == 'cloak-browse' and len(argv) >= 2:
        if argv[1] == 'info' and len(argv) == 2:
            return ['/usr/local/bin/cloak-browse', 'info']
        if argv[1].startswith('https://'):
            idx = 2
            while idx < len(argv):
                if argv[idx] in ('--selector', '--wait-ms', '--max-chars') and idx + 1 < len(argv):
                    idx += 2
                else:
                    return None
            return ['/usr/local/bin/cloak-browse', *argv[1:]]
        return None
    if base == 'gh' and len(argv) >= 3:
        pair = (argv[1], argv[2])
        if pair in GH_READ_COMMANDS:
            if any(a.startswith('--hostname') for a in argv[3:]):
                return None
            return ['/usr/bin/gh', *argv[1:]]
        if autonomous and pair == ('pr', 'create'):
            if '--head' in argv[3:]:
                head_idx = argv.index('--head') + 1
                if head_idx < len(argv) and argv[head_idx].startswith('agent/'):
                    return ['/usr/bin/gh', *argv[1:]]
        return None
    if autonomous and base == 'agent-worktree' and len(argv) >= 2 and argv[1] in ('create', 'list', 'status', 'verify'):
        return ['/data/workspace-admin/bin/agent-worktree', *argv[1:]]
    return None


def safe_host_command_chain(command, autonomous=False):
    """Validate one or more safe host commands joined by && or ;."""
    single = safe_host_command(command, autonomous=autonomous)
    if single is not None:
        return [single]
    if not isinstance(command, str) or any(c in command for c in '|><`\n\r$'):
        return None
    parts = [p.strip() for p in re.split(r'(?:&&|;)', command) if p.strip()]
    if not parts or len(parts) > 4:
        return None
    chain = []
    for part in parts:
        argv = safe_host_command(part, autonomous=autonomous)
        if argv is None:
            return None
        chain.append(argv)
    return chain


def run(directory, command, deadline_ns=None, autonomous=False):
    """A command sees its workspace and system tools, no host home or network."""
    directory = Path(directory).resolve(strict=True)
    if len(command) > 2000:
        raise ValueError('Command too long')
    host_chain = safe_host_command_chain(command, autonomous=autonomous)
    timeout = 45 if (host_chain is not None and any(Path(a[0]).name in ('g8t-worktree', '8c-self-deploy') for a in host_chain)) else 20
    if deadline_ns is not None:
        if type(deadline_ns) is not int:
            raise ValueError('Invalid command deadline')
        timeout = min(timeout, (deadline_ns - time.monotonic_ns()) // 1_000_000_000)
        if timeout <= 0:
            raise ValueError('Command deadline expired before execution')
    if host_chain is not None and all(Path(a[0]).exists() for a in host_chain):
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': os.environ.get('HOME', '/home/hue'), 'LANG': 'C.UTF-8', 'PWD': str(directory)}
        combined = b''
        exit_code = 0
        timed_out = False
        for host_argv in host_chain:
            with tempfile.TemporaryFile(dir='/tmp/opencode') as output:
                process = subprocess.Popen(host_argv, cwd=str(directory), env=env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    timed_out = True
                output.seek(0)
                combined += output.read(FILE_LIMIT + 1)
                exit_code = process.returncode
                if exit_code != 0 or timed_out:
                    break
        return {'exit_code': exit_code, 'output': combined[:FILE_LIMIT].decode(errors='replace'),
                'truncated': len(combined) > FILE_LIMIT, 'timed_out': timed_out}
    args = ['/usr/bin/bwrap', '--unshare-all', '--die-with-parent', '--new-session',
            '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
            '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib', '/lib64',
            '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--dir', '/home',
            '--bind', str(directory), '/workspace']
    for name in ('.git', '.opencode', '.agents', 'AGENTS.md', 'reference', 'shared'):
        if (directory / name).exists():
            args += ['--ro-bind', str(directory / name), '/workspace/' + name]
    for doc in FAMILY_DOCS:
        if Path(doc).exists():
            args += ['--ro-bind', doc, doc]
    for repo in FAMILY_REPOS:
        repo_path = Path('/data/8b-is') / repo
        if repo_path.exists():
            args += ['--ro-bind', str(repo_path), str(repo_path)]
    for ro_extra in (
        '/data/.agent-workspaces/8c-chat-bb80c2d68133-agent-capabilities-20261101',
        '/data/.agent-workspaces/g8t-59dcc7978c6e-agent-forge-20261101',
    ):
        if Path(ro_extra).exists():
            args += ['--ro-bind', ro_extra, ro_extra]
    for state_ro in ('/data/.agent-state/8c-self-deploy.json', '/data/.agent-state/g8t-worktree.json', '/data/.agent-state/opensiper-maintenance.json'):
        if Path(state_ro).exists():
            args += ['--ro-bind', state_ro, state_ro]
    args += ['--chdir', '/workspace', '--clearenv', '--setenv', 'PATH', '/usr/local/bin:/usr/bin:/bin',
             '--setenv', 'HOME', '/tmp', '--setenv', 'LANG', 'C.UTF-8',
             '/usr/bin/bash', '--noprofile', '--norc', '-c', command]
    def limits():
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
        resource.setrlimit(resource.RLIMIT_FSIZE, (4_194_304, 4_194_304))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_AS, (2_147_483_648, 2_147_483_648))
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


def tool(directory, action, deadline_ns=None, autonomous=False):
    if deadline_ns is not None and (type(deadline_ns) is not int or time.monotonic_ns() >= deadline_ns):
        raise ValueError('Tool deadline expired before execution')
    root = Path(directory)
    name = action['tool']
    if name == 'list':
        return {'files': sorted(p.relative_to(root).as_posix() for p in root.glob('*') if not p.name.startswith('.'))[:128]}
    if name == 'run':
        return run(root, action['command'], deadline_ns, autonomous=autonomous)
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
    print(json.dumps(run(Path.cwd(), ' '.join(sys.argv[1:]), autonomous=os.environ.get('8C_AUTONOMOUS') == '1')))
