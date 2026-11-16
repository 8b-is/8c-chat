#!/usr/bin/env python3
"""8c shared workbench: bounded files, immutable artifacts, isolated commands."""
import hashlib
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import unquote, urlencode, urlsplit
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
# Public GET endpoints only: no billing, account, administrative or arbitrary paths.
G8T_PATHS = ('/health', '/g8t/observatory', '/g8t/leaderboard', '/g8t/circuits',
             '/g8t/kompress', '/g8t/updates', '/g8t/bulletins', '/g8t/announcements',
             '/g8t/reviews', '/g8t/hottub')
G8T_MCP_COMMANDS = ('health', 'endpoints', 'circuits', 'leaderboard', 'feed', 'digest', 'bench')
GH_READ_COMMANDS = {('repo', 'view'), ('pr', 'list'), ('pr', 'view'), ('pr', 'diff'),
                    ('issue', 'list'), ('issue', 'view'), ('run', 'list'), ('run', 'view'),
                    ('search', 'repos'), ('search', 'issues'), ('search', 'prs'),
                    ('search', 'code'), ('search', 'commits')}
PUBLIC_ORIGINS = ('8c.is', '8b.is', 'g8t.is', 'github.com')
NETWORK_LIMIT = 1_048_576
# These are runtime dependencies, never general home-directory mounts.
CLOAK_MODULES = '/home/hue/.local/share/8c-browser-tools/node_modules'
CLOAK_BINARY = '/home/hue/.cloakbrowser/chromium-146.0.7680.177.5/chrome'


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


def public_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValueError('Invalid public URL')
    url = urlsplit(value)
    if (url.scheme != 'https' or url.username or url.password or url.port not in (None, 443)
            or url.hostname not in PUBLIC_ORIGINS or url.fragment):
        raise ValueError('Use an approved public HTTPS origin')
    decoded_path = unquote(url.path)
    if '\\' in decoded_path or '%' in decoded_path or any(p in ('.', '..') for p in decoded_path.split('/')):
        raise ValueError('Ambiguous URL paths are not allowed')
    if url.hostname == 'github.com':
        parts = decoded_path.strip('/').split('/')
        if len(parts) < 2 or parts[0] != '8b-is' or parts[1] not in FAMILY_REPOS:
            raise ValueError('GitHub browsing is scoped to public family repositories')
    return value


def options(args, permitted):
    flags, operands = {}, []
    idx = 0
    while idx < len(args):
        item = args[idx]
        if item.startswith('-'):
            if item not in permitted or item in flags:
                raise ValueError('Unknown or repeated capability option')
            kind = permitted[item]
            if kind is None:
                flags[item] = True
            else:
                idx += 1
                if idx >= len(args):
                    raise ValueError('Missing option value')
                value = args[idx]
                if kind == 'limit' and (not value.isdecimal() or not 1 <= int(value) <= 100):
                    raise ValueError('Limit must be 1–100')
                if kind == 'small' and (not value.isdecimal() or not 1 <= int(value) <= 3):
                    raise ValueError('Fanout must be 1–3')
                if len(value) > 256 or any(ord(c) < 32 for c in value):
                    raise ValueError('Invalid capability value')
                flags[item] = value
        else:
            operands.append(item)
        idx += 1
    return flags, operands


def safe_host_command(command, autonomous=False):
    """Validated descriptors for the broker, never unrestricted host argv.

    Autonomous room participation does not grant publishing/deployment authority.
    """
    if not isinstance(command, str) or len(command) > 2000 or any(c in command for c in ';&|><`\n\r$'):
        return None
    try:
        argv = shlex.split(command)
        if len(argv) < 2:
            return None
        base = Path(argv[0]).name
        fixed = {'g8t-mcp':'/usr/local/bin/g8t-mcp', 'cloak-browse':'/usr/local/bin/cloak-browse',
                 'gh':'/usr/bin/gh', 'g8t-worktree':'/usr/local/bin/g8t-worktree', '8c-self-deploy':'/usr/local/bin/8c-self-deploy'}
        if base not in fixed or argv[0] not in (base, fixed[base]):
            return None
        sub = argv[1]
        if base == 'g8t-mcp':
            if sub not in G8T_MCP_COMMANDS:
                return None
            permitted = {'--json':None}
            if sub in ('circuits','leaderboard','feed'):
                permitted['--limit'] = 'limit'
            if sub == 'leaderboard':
                permitted['--domain'] = 'text'
            if sub == 'bench':
                permitted.update({'--rounds':'small','--paths':'small'})
            _, operands = options(argv[2:], permitted)
            if sub == 'feed':
                if len(operands) != 1 or operands[0] not in G8T_PATHS:
                    return None
            elif operands:
                return None
        elif base == 'cloak-browse':
            if sub == 'info':
                if len(argv) != 2:
                    return None
            else:
                public_url(sub)
                flags, operands = options(argv[2:], {'--selector':'text','--max-chars':'text','--wait-ms':'text'})
                if operands:
                    return None
                for name, low, high in (('--max-chars',256,16000),('--wait-ms',0,1000)):
                    if name in flags and (not flags[name].isdecimal() or not low <= int(flags[name]) <= high):
                        return None
        elif base == 'gh':
            if len(argv) < 3 or tuple(argv[1:3]) not in GH_READ_COMMANDS:
                return None
            flags, operands = options(argv[3:], {'-R':'text','--repo':'text','--limit':'limit'})
            if '-R' in flags and '--repo' in flags:
                return None
            repo = flags.get('-R', flags.get('--repo'))
            if (sub,argv[2]) == ('repo','view') and repo is None and len(operands) == 1:
                repo, operands = operands[0], []
            if repo not in {'8b-is/' + r for r in FAMILY_REPOS}:
                return None
            if sub == 'search':
                if len(operands) != 1 or not re.fullmatch(r'[A-Za-z0-9 _.-]{1,100}',operands[0]):
                    return None
            elif argv[2] in ('view','diff') and sub != 'repo':
                if len(operands) != 1 or not re.fullmatch(r'[1-9][0-9]{0,15}',operands[0]):
                    return None
            elif operands:
                return None
        elif sub != 'status' or len(argv) != 2:
            # Installed mutation helpers execute patched tests with owner HOME,
            # sudo and SSH. A pathname allowlist cannot safely authorize them.
            return None
        return [fixed[base], *argv[1:]]
    except (ValueError, IndexError):
        return None


def safe_host_command_chain(command, autonomous=False):
    single = safe_host_command(command, autonomous)
    if single is not None:
        return [single]
    if not isinstance(command,str) or any(c in command for c in '|><`\n\r$'):
        return None
    parts = re.split(r'&&|;',command)
    if not 2 <= len(parts) <= 4 or any(not p.strip() for p in parts):
        return None
    chain = [safe_host_command(p.strip(),autonomous) for p in parts]
    return chain if all(chain) else None


def public_get(url, deadline_ns):
    """Credential-free GET: pin public DNS address, verify TLS, refuse redirects.

    The complete call, including DNS, runs under the broker subprocess deadline.
    """
    target = urlsplit(url)
    if (target.scheme != 'https' or target.hostname not in (*PUBLIC_ORIGINS,'api.github.com')
            or target.username or target.password or target.port not in (None,443)):
        raise ValueError('Unapproved network target')
    def remaining():
        value = (deadline_ns-time.monotonic_ns())//1_000_000_000
        if value <= 0:
            raise ValueError('Capability deadline expired')
        return min(10,value)
    remaining()
    addresses = socket.getaddrinfo(target.hostname,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Public DNS resolved to a nonpublic address')
    connection = http.client.HTTPSConnection(target.hostname,timeout=remaining())
    try:
        raw = socket.create_connection(addresses[0][4][:2],timeout=remaining())
        try:
            connection.sock = ssl.create_default_context().wrap_socket(raw,server_hostname=target.hostname)
        except BaseException:
            raw.close()
            raise
        path = (target.path or '/') + ('?' + target.query if target.query else '')
        connection.request('GET',path,headers={'User-Agent':'8c-public-read/1','Accept':'application/json, text/html;q=0.9'})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('Public GET refused; redirects/authentication are not followed: ' + str(response.status))
        chunks, size = [], 0
        while True:
            timeout = remaining()
            if connection.sock:
                connection.sock.settimeout(timeout)
            chunk = response.read(min(65536,NETWORK_LIMIT+1-size))
            size += len(chunk)
            if size > NETWORK_LIMIT:
                raise ValueError('Public response exceeds 1 MiB')
            if not chunk:
                break
            chunks.append(chunk)
        return b''.join(chunks)
    finally:
        connection.close()


def github_url(argv):
    flags, operands = options(argv[3:],{'-R':'text','--repo':'text','--limit':'limit'})
    repo = flags.get('-R',flags.get('--repo'))
    if repo is None:
        repo, operands = operands[0], []
    area, operation = argv[1:3]
    limit = flags.get('--limit','20')
    path = '/repos/' + repo
    if area == 'search':
        category = {'prs':'issues','repos':'repositories'}.get(operation,operation)
        query = 'repo:' + repo + (' type:pr' if operation == 'prs' else '') + ' "' + operands[0] + '"'
        return 'https://api.github.com/search/' + category + '?' + urlencode({'q':query,'per_page':limit})
    path += {'pr':'/pulls','issue':'/issues','run':'/actions/runs'}.get(area,'')
    if operands:
        path += '/' + operands[0]
    if operation == 'diff':
        path += '/files'
    return 'https://api.github.com' + path + '?' + urlencode({'per_page':limit})


def sandbox_args(directory, readonly=False):
    args = ['/usr/bin/bwrap','--unshare-all','--die-with-parent','--new-session',
            '--ro-bind','/usr','/usr','--symlink','usr/bin','/bin',
            '--symlink','usr/lib','/lib','--symlink','usr/lib','/lib64',
            '--proc','/proc','--dev','/dev','--tmpfs','/tmp','--dir','/home',
            '--ro-bind' if readonly else '--bind',str(directory),'/workspace']
    for name in ('.git','.opencode','.agents','AGENTS.md','reference','shared'):
        entry = directory/name
        if entry.is_symlink():
            raise ValueError('Protected workspace paths must not be symlinks')
        if entry.exists() and not readonly:
            args += ['--ro-bind',str(entry),'/workspace/'+name]
    return args


CLOAK_RENDER = r'''const fs = require('node:fs');
(async () => {
 const input = JSON.parse(fs.readFileSync(0,'utf8'));
 process.env.CLOAKBROWSER_AUTO_UPDATE='false';
 process.env.CLOAKBROWSER_SUPPRESS_FONT_WARNING='1';
 process.env.CLOAKBROWSER_BINARY_PATH=input.binary;
 const cb=require(require.resolve('cloakbrowser',{paths:[input.modules]}));
 if(input.info){console.log(JSON.stringify({mode:'offline-snapshot',binary:cb.binaryInfo()}));return;}
 const browser=await cb.launch({headless:true,humanize:false,args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--renderer-process-limit=1']});
 try {
  const context=await browser.newContext({javaScriptEnabled:false,serviceWorkers:'block'});
  await context.route('**/*',route=>route.abort());
  const page=await context.newPage();
  await page.goto('data:text/html;charset=utf-8,'+encodeURIComponent(input.html),{waitUntil:'commit',timeout:8000});
  const text=await page.locator(input.selector||'body').innerText({timeout:2000});
  console.log(JSON.stringify({mode:'offline-snapshot',url:input.url,title:await page.title(),text:text.slice(0,input.maxChars),truncated:text.length>input.maxChars,
   note:'Approved-origin HTML rendered offline. Scripts/subrequests disabled; not live JavaScript verification.'}));
 } finally {await browser.close();}
})().catch(e=>{console.error(String(e.message));process.exitCode=1;});'''


def execute(args,directory,timeout,payload=None,browser=False):
    def limits():
        resource.setrlimit(resource.RLIMIT_CPU,(15,15))
        file_size = 67_108_864 if browser else 4_194_304
        resource.setrlimit(resource.RLIMIT_FSIZE,(file_size,file_size))
        file_descriptors = 1024 if browser else 128
        resource.setrlimit(resource.RLIMIT_NOFILE,(file_descriptors,file_descriptors))
        # V8 requires a large virtual cage; browser RSS needs a service cgroup
        # limit. Normal workspace commands retain the existing 2 GiB AS ceiling.
        address_space = 2_199_023_255_552 if browser else 2_147_483_648
        resource.setrlimit(resource.RLIMIT_AS,(address_space,address_space))
    with tempfile.TemporaryFile(dir='/tmp/opencode') as output:
        process=subprocess.Popen(args,cwd=str(directory),env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/tmp','LANG':'C.UTF-8'},
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,
            start_new_session=True,preexec_fn=limits)
        timed_out=False
        try:
            process.communicate(payload,timeout=timeout)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(process.pid,signal.SIGKILL)
            process.wait()
            timed_out=True
        output.seek(0)
        text=output.read(FILE_LIMIT+1)
    return {'exit_code':process.returncode,'output':text[:FILE_LIMIT].decode(errors='replace'),
            'truncated':len(text)>FILE_LIMIT,'timed_out':timed_out}


def capability_result(argv,directory,deadline_ns):
    base,sub=Path(argv[0]).name,argv[1]
    if base=='gh':
        return {'scope':'public-family-repositories','data':json.loads(public_get(github_url(argv),deadline_ns),parse_float=str)}
    if base=='g8t-mcp':
        flags,operands=options(argv[2:],{'--json':None,'--limit':'limit','--domain':'text','--rounds':'small','--paths':'small'})
        if sub=='endpoints':
            return {'paths':list(G8T_PATHS),'scope':'public-read-allowlist'}
        paths=list(G8T_PATHS[:3]) if sub in ('digest','bench') else [operands[0] if sub=='feed' else {'health':'/health','circuits':'/g8t/circuits','leaderboard':'/g8t/leaderboard'}[sub]]
        rounds=int(flags.get('--rounds','1')) if sub=='bench' else 1
        paths=paths[:int(flags.get('--paths','3'))]
        rows=[]
        for _ in range(rounds):
            for path in paths:
                began=time.monotonic_ns()
                raw=public_get('https://g8t.is'+path,deadline_ns)
                data=json.loads(raw,parse_float=str)
                if sub in ('bench','digest'):
                    rows.append({'path':path,'bytes':len(raw),'elapsed_ms':(time.monotonic_ns()-began)//1_000_000})
                else:
                    if isinstance(data,list):
                        if '--domain' in flags:
                            data=[row for row in data if isinstance(row,dict) and flags['--domain'].casefold() in str(row.get('domain','')).casefold()]
                        data=data[:int(flags.get('--limit','10'))]
                    return {'path':path,'data':data}
        return {'observations':rows,'note':'Bounded sequential GET measurements, not capsule benchmarks.'}
    if base in ('g8t-worktree','8c-self-deploy'):
        path=file_at(directory,'reference/'+base+'.json')
        result={'tool':base,'mode':'snapshot','maintenance':'Live mutation/test/deploy needs a separate reviewed, sandboxed operator release broker; proposals remain supported in artifacts/.'}
        if path.exists() and path.stat().st_size<=FILE_LIMIT:
            saved=json.loads(path.read_text())
            result['receipt']={k:saved[k] for k in ('ok','head','updated_at','deployed_at','test_summary') if k in saved and isinstance(saved[k],(str,int,bool))}
        return result
    if base=='cloak-browse':
        flags,_=options(argv[2:],{'--selector':'text','--max-chars':'text','--wait-ms':'text'})
        payload={'info':sub=='info','binary':CLOAK_BINARY,'modules':CLOAK_MODULES}
        if sub!='info':
            payload.update(url=public_url(sub),html=public_get(sub,deadline_ns).decode('utf-8',errors='replace'),selector=flags.get('--selector'),maxChars=int(flags.get('--max-chars','8000')))
        mounts=(Path(CLOAK_MODULES),Path(CLOAK_BINARY).parent)
        if not all(p.exists() for p in mounts):
            raise ValueError('Scoped CloakBrowser dependencies unavailable; no host fallback')
        args=sandbox_args(directory,readonly=True)
        for path in mounts:
            args+=['--ro-bind',str(path),str(path)]
        if Path('/etc/fonts').is_dir():
            args+=['--ro-bind','/etc/fonts','/etc/fonts']
        args+=['--chdir','/workspace','--clearenv','--setenv','HOME','/tmp','--setenv','PATH','/usr/bin:/bin','/usr/bin/node','-e',CLOAK_RENDER]
        result=execute(args,directory,max(1,(deadline_ns-time.monotonic_ns())//1_000_000_000),json.dumps(payload).encode(),browser=True)
        if result['exit_code'] or result['timed_out']:
            raise ValueError('Scoped CloakBrowser failed: '+result['output'][-400:])
        try:
            # Native/font diagnostics may precede the one JSON result emitted
            # by our fixed script. They are not renderer result metadata.
            return json.loads(result['output'].splitlines()[-1])
        except (ValueError, IndexError):
            raise ValueError('Scoped renderer did not return bounded JSON: ' + result['output'][:400]) from None
    raise ValueError('Unknown public capability')


def run(directory,command,deadline_ns=None,autonomous=False):
    directory=Path(directory).resolve(strict=True)
    if not isinstance(command,str) or len(command)>2000:
        raise ValueError('Command too long or invalid')
    if deadline_ns is not None and type(deadline_ns) is not int:
        raise ValueError('Invalid command deadline')
    deadline=min(deadline_ns or 2**63-1,time.monotonic_ns()+20_000_000_000)
    def remaining():
        value=(deadline-time.monotonic_ns())//1_000_000_000
        if value<=0:
            raise ValueError('Command deadline expired before execution')
        return value
    chain=safe_host_command_chain(command,autonomous)
    if chain:
        output,code,timed_out,truncated='',0,False,False
        for argv in chain:
            remaining()
            result=execute([sys.executable,str(Path(__file__).resolve()),'--capability',json.dumps(argv),str(deadline)],directory,remaining(),browser=Path(argv[0]).name=='cloak-browse')
            output+=result['output']+'\n'
            code,timed_out=result['exit_code'],result['timed_out']
            truncated=truncated or result['truncated'] or len(output.encode())>FILE_LIMIT
            if code or timed_out:
                break
        return {'exit_code':code,'output':output.encode()[:FILE_LIMIT].decode(errors='replace'),'timed_out':timed_out,'truncated':truncated}
    if re.search(r'(?<![\w-])(?:g8t-mcp|cloak-browse|gh|g8t-worktree|8c-self-deploy|agent-worktree)(?![\w-])',command):
        raise ValueError('Unsupported scoped capability; live maintenance requires a separately reviewed release broker')
    args=sandbox_args(directory)
    args+=['--chdir','/workspace','--clearenv','--setenv','PATH','/usr/local/bin:/usr/bin:/bin','--setenv','HOME','/tmp','--setenv','LANG','C.UTF-8','--setenv','ST_NO_DAEMON','1','/usr/bin/bash','--noprofile','--norc','-c',command]
    return execute(args,directory,remaining())


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


def validate_cli_workspace(directory):
    # A hosted shell tool may expose a caller-selected cwd. The installed runner
    # must never turn that into a writable bind of an arbitrary home/repository.
    if (not re.fullmatch(r'/data/\.agent-workspaces/8c-commons-[A-Za-z0-9_-]+', str(directory.parent))
            or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', directory.name)
            or not (directory / 'AGENTS.md').is_file()):
        raise ValueError('Installed runner requires a provisioned participant workspace')


if __name__ == '__main__':
    if str(Path(__file__).resolve()) in ('/usr/local/libexec/8c-run', '/usr/local/share/8c-chat/commons.py'):
        validate_cli_workspace(Path.cwd().resolve())
    if len(sys.argv) == 4 and sys.argv[1] == '--capability':
        argv = json.loads(sys.argv[2])
        if (not isinstance(argv,list) or not all(isinstance(a,str) for a in argv)
                or safe_host_command(shlex.join(argv)) != argv):
            raise ValueError('Invalid scoped capability descriptor')
        deadline = min(int(sys.argv[3]),time.monotonic_ns()+20_000_000_000)
        print(json.dumps(capability_result(argv,Path.cwd(),deadline),ensure_ascii=False))
    else:
        print(json.dumps(run(Path.cwd(),' '.join(sys.argv[1:]))))
