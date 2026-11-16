"""Capability confinement tests: fixtures only, no external network/service writes."""
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import commons
import hosted


class CapabilityScopeTests(unittest.TestCase):
    def test_supported_public_read_descriptors(self):
        for command in ('g8t-mcp health --json', 'g8t-mcp circuits --limit 3',
                        'g8t-mcp leaderboard --domain arithmetic --limit 10',
                        'g8t-mcp feed /g8t/reviews --limit 1',
                        'g8t-mcp bench --rounds 2 --paths 2',
                        'cloak-browse https://8c.is --selector body --max-chars 1000',
                        'gh pr list -R 8b-is/g8t --limit 5',
                        'gh pr view 4 --repo 8b-is/8c-chat',
                        'gh repo view 8b-is/CloakBrowser',
                        'gh search code capsela -R 8b-is/g8t',
                        'g8t-worktree status', '8c-self-deploy status'):
            with self.subTest(command=command):
                self.assertIsNotNone(commons.safe_host_command(command))

    def test_concrete_scope_escapes_rejected_even_when_autonomous(self):
        commands = (
            'g8t-mcp health --base http://127.0.0.1:8799',
            'g8t-mcp --base https://evil.invalid health',
            'g8t-mcp bench --rounds 1000000', 'g8t-mcp health --concurrency 100000',
            'g8t-mcp feed /g8t/stripe/invoices', 'g8t-mcp feed //evil.invalid/x',
            'g8t-mcp circuits --limit 3 --limit 4', 'g8t-mcp health --json --json',
            'gh pr list -R other/private', 'gh repo view https://evil.invalid/8b-is/g8t',
            'gh pr list -R 8b-is/g8t --repo other/private',
            'gh pr list -R 8b-is/g8t --hostname github.enterprise',
            'gh pr list -R 8b-is/g8t --web',
            'gh search code repo:other/private -R 8b-is/g8t',
            'gh pr create --head agent/anything --body-file /home/hue/secret',
            'agent-worktree /data/8b-is/g8t anything',
            'g8t-worktree apply artifacts/fix.patch --message test',
            'g8t-worktree test capsela::', '8c-self-deploy check',
            '8c-self-deploy patch artifacts/fix.patch', '8c-self-deploy deploy',
            'cloak-browse https://8c.is --screenshot /home/hue/overwrite.png',
            'cloak-browse https://8c.is --max-chars 999999',
            'cloak-browse https://evil.invalid', 'cloak-browse https://172.16.1.2',
            'cloak-browse https://[fd00::1]', 'cloak-browse https://u:p@8c.is',
            'cloak-browse https://8c.is:8799', 'cloak-browse https://github.com/other/private',
            'cloak-browse https://github.com/8b-is/g8t/../../other/private',
            'cloak-browse https://github.com/8b-is/g8t/%2e%2e/%2e%2e/other/private',
            '/tmp/g8t-mcp health', 'g8t-mcp health; id', 'gh pr list -R 8b-is/g8t | sh')
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(commons.safe_host_command(command, autonomous=True))

    def test_chains_share_budget_and_never_invoke_a_shell(self):
        command = 'g8t-mcp health && gh pr list -R 8b-is/g8t; 8c-self-deploy status'
        self.assertEqual(len(commons.safe_host_command_chain(command)), 3)
        for bad in ('g8t-mcp health; id', ';g8t-mcp health', 'g8t-mcp health;',
                    'g8t-mcp health &&&& g8t-mcp health', 'g8t-mcp health;' * 5):
            self.assertIsNone(commons.safe_host_command_chain(bad))
        clock, timeouts = [0], []
        def execute(args, directory, timeout, **kwargs):
            self.assertEqual(args[2], '--capability')
            self.assertNotIn('bash', args)
            timeouts.append(timeout)
            clock[0] += 6_000_000_000
            return {'exit_code':0,'output':'fixture','truncated':False,'timed_out':False}
        with tempfile.TemporaryDirectory() as root, patch.object(commons.time,'monotonic_ns',side_effect=lambda:clock[0]), patch.object(commons,'execute',side_effect=execute):
            result = commons.run(root,command)
        self.assertEqual(timeouts,[20,14,8])
        self.assertEqual(result['exit_code'],0)

    def test_bad_capability_never_falls_back_to_host_or_workspace_shell(self):
        with tempfile.TemporaryDirectory() as root, patch.object(commons,'execute') as execute:
            for command in ('g8t-mcp health --base http://127.0.0.1',
                            '8c-self-deploy deploy', 'g8t-worktree apply artifacts/x.patch'):
                with self.assertRaisesRegex(ValueError,'reviewed release broker'):
                    commons.run(root,command,autonomous=True)
            execute.assert_not_called()

    def test_dns_private_and_mixed_results_fail_before_connecting(self):
        for ip in ('127.0.0.1','10.0.0.1','172.16.0.1','192.168.1.1','169.254.169.254','::1','fd00::1'):
            addresses = [(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))]
            with patch.object(commons.socket,'getaddrinfo',return_value=addresses), patch.object(commons.socket,'create_connection') as connect:
                with self.assertRaisesRegex(ValueError,'nonpublic'):
                    commons.public_get('https://8c.is/',time.monotonic_ns()+10_000_000_000)
                connect.assert_not_called()

    def test_get_pins_tls_address_and_rejects_redirect_and_oversized_body(self):
        addresses=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
        for status, body, expected in ((200,b'fixture',None),(302,b'', 'redirects'),
                                       (200,b'x'*(commons.NETWORK_LIMIT+1),'1 MiB')):
            connection=Mock()
            response=connection.getresponse.return_value
            response.status=status
            remaining=[body]
            def read(size):
                part=remaining[0][:size]
                remaining[0]=remaining[0][size:]
                return part
            response.read.side_effect=read
            tls=Mock()
            with patch.object(commons.socket,'getaddrinfo',return_value=addresses), \
                 patch.object(commons.socket,'create_connection') as connect, \
                 patch.object(commons.ssl,'create_default_context',return_value=tls), \
                 patch.object(commons.http.client,'HTTPSConnection',return_value=connection):
                if expected:
                    with self.assertRaisesRegex(ValueError,expected):
                        commons.public_get('https://8c.is/',time.monotonic_ns()+10_000_000_000)
                else:
                    self.assertEqual(commons.public_get('https://8c.is/',time.monotonic_ns()+10_000_000_000),body)
                self.assertEqual(connect.call_args.args[0],('93.184.216.34',443))
                self.assertEqual(tls.wrap_socket.call_args.kwargs['server_hostname'],'8c.is')
                headers=connection.request.call_args.kwargs['headers']
                self.assertNotIn('Authorization',headers)
                self.assertNotIn('Cookie',headers)
                connection.close.assert_called_once()

    def test_supported_broker_results_use_only_fixed_public_routes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(commons,'public_get',return_value=b'[{"domain":"math"},{"domain":"text"}]') as get:
            result=commons.capability_result(commons.safe_host_command('g8t-mcp leaderboard --domain math --limit 1'),Path(directory),time.monotonic_ns()+20_000_000_000)
            self.assertEqual(result['data'],[{'domain':'math'}])
            self.assertEqual(get.call_args.args[0],'https://g8t.is/g8t/leaderboard')
            commons.capability_result(commons.safe_host_command('gh pr list -R 8b-is/g8t --limit 5'),Path(directory),time.monotonic_ns()+20_000_000_000)
            self.assertEqual(get.call_args.args[0],'https://api.github.com/repos/8b-is/g8t/pulls?per_page=5')

    def test_actual_offline_sandbox_and_smart_tree(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            root=Path(directory)
            (root/'reference').mkdir()
            (root/'reference'/'README.md').write_text('public committed fixture')
            result=commons.run(root,'test ! -e /data/8b-is && test ! -e /home/hue && test -z "$WORKER_KEY" && printf ok')
            self.assertEqual(result['exit_code'],0,result)
            self.assertEqual(result['output'],'ok')
            if Path('/usr/local/bin/st').exists():
                result=commons.run(root,'st --mode ai --depth 2 reference')
                self.assertEqual(result['exit_code'],0,result)
                self.assertIn('public committed fixture',result['output'])

    def test_existing_sol_grok_configs_are_replaced_not_merged(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            root=Path(directory)
            for name in ('sol','grok'):
                target=root/name/'.opencode'/'opencode.json'
                target.parent.mkdir(parents=True)
                target.write_text(json.dumps({'plugins':['unsafe'], 'mcp':{'servers':{'unsafe':{}}},
                    'agents':{'commons':{'permissions':[{'action':'mcp*','resource':'*','effect':'allow'}]}}}))
                agent={'id':name,'name':name.title(),'runtime':'opencode','model':'fixture'}
                hosted.setup_workspace(agent,root,None)
                config=json.loads(target.read_text())
                self.assertEqual(config['plugins'],[])
                self.assertEqual(config['mcp'],{'servers':{}})
                self.assertEqual(config['agents']['commons']['permissions'],config['agents']['commons-autonomous']['permissions'])
                allows=[p for p in config['agents']['commons']['permissions'] if p['effect']=='allow']
                self.assertFalse(any('mcp' in p['action'] or p['resource'].startswith('/data/') for p in allows))
                self.assertEqual([p['resource'] for p in allows if p['action']=='shell'],['/usr/local/libexec/8c-run *'])
                self.assertEqual(config['agents']['commons-funded']['permissions'],[{'action':'*','resource':'*','effect':'deny'}])

    def test_maintenance_status_sanitizes_receipts_without_executing_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'reference').mkdir()
            (root/'reference'/'8c-self-deploy.json').write_text(json.dumps({'head':'abc','ok':True,'secret':'not-public','output':'private command output'}))
            with patch.object(commons,'execute') as execute:
                result=commons.capability_result(['/usr/local/bin/8c-self-deploy','status'],root,1)
            execute.assert_not_called()
            self.assertEqual(result['receipt'],{'head':'abc','ok':True})
            self.assertIn('release broker',result['maintenance'])

    def test_installed_runner_rejects_unscoped_caller_directory(self):
        for directory in ('/home/hue', '/data/8b-is/g8t', '/data/.agent-workspaces/other/aye', '/tmp'):
            with self.assertRaisesRegex(ValueError, 'provisioned'):
                commons.validate_cli_workspace(Path(directory))
        with patch.object(Path, 'is_file', return_value=True):
            commons.validate_cli_workspace(Path('/data/.agent-workspaces/8c-commons-fixture/aye'))

    @unittest.skipUnless(Path(commons.CLOAK_BINARY).exists() and Path(commons.CLOAK_MODULES).exists(), 'CloakBrowser dependencies not installed')
    def test_actual_cloak_renderer_is_offline_and_scripts_do_not_execute(self):
        html = b'<html><head><title>Offline fixture</title></head><body><p>fixture visible</p><script>document.body.innerHTML="EXECUTED"</script><img src="https://127.0.0.1/private"></body></html>'
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory, patch.object(commons, 'public_get', return_value=html) as get:
            result = commons.capability_result(commons.safe_host_command('cloak-browse https://8c.is'), Path(directory), time.monotonic_ns()+18_000_000_000)
            self.assertEqual(result['mode'], 'offline-snapshot')
            self.assertIn('fixture visible', result['text'])
            self.assertNotIn('EXECUTED', result['text'])
            get.assert_called_once()

    def test_browser_uses_readonly_offline_renderer_not_raw_cli(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(commons,'public_get',return_value=b'<p>fixture</p>'), \
             patch.object(Path,'exists',return_value=True), patch.object(commons,'execute',return_value={'exit_code':0,'timed_out':False,'output':'{"text":"fixture"}'}) as execute:
            result=commons.capability_result(commons.safe_host_command('cloak-browse https://8c.is'),Path(directory),time.monotonic_ns()+20_000_000_000)
            self.assertEqual(result['text'],'fixture')
            args=execute.call_args.args[0]
            self.assertIn('--unshare-all',args)
            self.assertNotIn('--share-net',args)
            self.assertNotIn('/usr/local/bin/cloak-browse',args)
            self.assertNotIn('/home/hue',args)
            self.assertIn('javaScriptEnabled:false',commons.CLOAK_RENDER)
            self.assertIn("route=>route.abort()",commons.CLOAK_RENDER)


if __name__=='__main__':
    unittest.main()
