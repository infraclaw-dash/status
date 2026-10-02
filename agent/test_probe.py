import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('probe', Path(__file__).with_name('probe.py'))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def container(image, cmd=None, env=None):
    return {'Config': {'Image': image, 'Cmd': cmd or [], 'Env': env or []},
            'State': {'Running': True}, 'HostConfig': {'PortBindings': {}, 'NetworkMode': 'bridge'},
            'NetworkSettings': {'Networks': {'local': {'IPAddress': '172.20.0.2'}}}}


class Probes(unittest.TestCase):
    def test_fork_and_image_id_deployments_preserve_runtime_checks(self):
        for prefix in ['ghcr.io/infraclaw-dash/platform-explorer-', 'sha256:']:
            api = container(prefix + 'api')
            idx = container(prefix + 'indexer')
            migration = container(prefix + 'indexer', ['/app/indexer', 'migrate'])
            for c, service in [(api, 'explorer-api'), (idx, 'explorer-indexer'), (migration, 'explorer-migrate')]:
                c['Config']['Labels'] = {'com.docker.compose.project': 'devnet-services',
                                         'com.docker.compose.service': service}
            migration['State'] = {'Status': 'exited', 'Running': False, 'ExitCode': 0}
            self.assertTrue(p.explorer_migration(migration))
            self.assertFalse(p.explorer_migration(idx))
            for state, running in [('running', True), ('restarting', False), ('exited', False)]:
                idx.update(Id='real-indexer', RestartCount=7,
                           State={'Status': state, 'Running': state != 'exited', 'ExitCode': 101})
                with patch.object(p, 'get_json', return_value=(200, {'api': {'block': {'height': 42}},
                                                                             'tenderdash': {'block': {'height': 43}}}, 1)):
                    check = p.explorer({'api': api, 'a-migration': migration, 'idx': idx})
                self.assertEqual(check['indexerId'], 'real-indexer')
                self.assertEqual(check['indexerRunning'], running)
                self.assertEqual(check['indexerRestarts'], 7)
                self.assertEqual(check['indexedHeight'], 42)
                self.assertEqual(check['chainHeight'], 43)

    def test_unrelated_compose_service_is_not_explorer(self):
        c = container('sha256:' + 'a' * 64, ['migrate'])
        for labels in [{}, {'com.docker.compose.project': 'other', 'com.docker.compose.service': 'explorer-migrate'},
                       {'com.docker.compose.project': 'devnet-services', 'com.docker.compose.service': 'unrelated'}]:
            c['Config']['Labels'] = labels
            self.assertFalse(p.explorer_migration(c))
            self.assertFalse(p.explorer_component(c, 'api'))
            self.assertFalse(p.explorer_component(c, 'indexer'))

    def test_legacy_faucet_history_is_not_a_pending_payment_queue(self):
        # Actual incident aggregates: 10,373 attempts, 2,184 without txid,
        # but zero rows in faucet_pending_payments. Do not discard history.
        c = container('dashpay/multifaucet:latest'); c['Id'] = 'faucet'
        db = dict(total='10373', queued='0', oldestQueuedAt=None,
                  lastBroadcastAt='1764594772', payoutsWithoutTxidCount='2184',
                  lastIncompleteAttemptAt='1770628068', recent=[])
        def run(args, timeout, stdin):
            self.assertEqual(args, ['docker', 'exec', '-i', 'faucet', 'php'])
            sql = stdin.decode()
            self.assertIn('COUNT(*) FROM faucet_pending_payments', sql)
            self.assertIn('MIN(UNIX_TIMESTAMP(created_date)) FROM faucet_pending_payments', sql)
            self.assertIn('payoutsWithoutTxidCount', sql)
            return json.dumps(db).encode()
        with patch.object(p, 'run', run), patch.object(p, 'http_check', return_value={}), patch.object(p, 'core_cli', return_value=(None, None)):
            out = p.legacy_faucet({'faucet': c})['queue']
        self.assertEqual(out, dict(ok=True, total=10373, queued=0, oldestQueuedAt=None,
                                   lastBroadcastAt=1764594772, payoutsWithoutTxidCount=2184,
                                   lastIncompleteAttemptAt=1770628068))

    def test_legacy_faucet_real_pending_rows_remain_visible(self):
        c = container('dashpay/multifaucet:latest'); c['Id'] = 'faucet'
        db = dict(total='10373', queued='2', oldestQueuedAt='1790940000',
                  lastBroadcastAt='1764594772', payoutsWithoutTxidCount='2184',
                  lastIncompleteAttemptAt='1770628068', recent=[])
        with patch.object(p, 'run', return_value=json.dumps(db).encode()), patch.object(p, 'http_check', return_value={}), patch.object(p, 'core_cli', return_value=(None, None)):
            out = p.legacy_faucet({'faucet': c})['queue']
        self.assertTrue(out['ok']); self.assertEqual(out['queued'], 2)
        self.assertEqual(out['oldestQueuedAt'], 1790940000)

    def test_legacy_faucet_missing_queue_source_is_unknown_not_green(self):
        c = container('dashpay/multifaucet:latest'); c['Id'] = 'faucet'
        with patch.object(p, 'run', return_value=b'{"error":"database unavailable"}'), patch.object(p, 'http_check', return_value={}):
            self.assertEqual(p.legacy_faucet({'faucet': c})['queue'], dict(ok=False))

    def test_legacy_faucet_empty_history_and_malformed_results(self):
        c = container('dashpay/multifaucet:latest'); c['Id'] = 'faucet'
        empty = dict(total='0', queued='0', oldestQueuedAt=None, lastBroadcastAt=None,
                     payoutsWithoutTxidCount='0', lastIncompleteAttemptAt=None, recent=[])
        with patch.object(p, 'run', return_value=json.dumps(empty).encode()), patch.object(p, 'http_check', return_value={}), patch.object(p, 'core_cli', return_value=(None, None)):
            self.assertEqual(p.legacy_faucet({'faucet': c})['queue']['queued'], 0)
        for changed in [dict(queued=None), dict(queued='2'), dict(queued=True), dict(total='-1'), dict(payoutsWithoutTxidCount='1')]:
            with self.subTest(changed=changed), patch.object(p, 'run', return_value=json.dumps({**empty, **changed}).encode()), patch.object(p, 'http_check', return_value={}):
                self.assertEqual(p.legacy_faucet({'faucet': c})['queue'], dict(ok=False))

    def test_docker_preserves_successful_migration_evidence(self):
        c = container('ghcr.io/pshenmic/platform-explorer-indexer:2.5.3', ['/app/indexer', 'migrate'])
        c.update(Id='migration', Name='/services-explorer-migrate-1', Image='image-id')
        c['Config']['Labels'] = {'com.docker.compose.service': 'explorer-migrate'}
        c['HostConfig']['RestartPolicy'] = {'Name': 'no'}
        c['State'] = {'Status': 'exited', 'Running': False, 'ExitCode': 0}
        def run(args):
            if args[1:3] == ['ps', '-aq']: return b'migration'
            if args[1] == 'inspect': return json.dumps([c]).encode()
            if args[1:3] == ['image', 'inspect']: return b'[{"Id":"image-id"}]'
            self.fail(args)
        with patch.object(p.shutil, 'which', return_value='/usr/bin/docker'), patch.object(p, 'run', run):
            out, _ = p.docker()
        self.assertTrue(out[0]['oneShot'])
        self.assertEqual(out[0]['restartPolicy'], 'no')
        self.assertEqual(out[0]['exitCode'], 0)
        self.assertFalse(out[0]['running'])

    def test_explorer_never_mistakes_migration_for_indexer(self):
        for migration in [container('ghcr.io/pshenmic/platform-explorer-indexer:2.5.3', ['/app/indexer', 'migrate']),
                          container('ghcr.io/pshenmic/platform-explorer-indexer:2.5.3')]:
            migration['Config']['Labels'] = {'com.docker.compose.service': 'explorer-migrate'}
            raw = {'api': container('ghcr.io/pshenmic/platform-explorer-api:2.5.3'), 'migration': migration}
            with patch.object(p, 'get_json', return_value=(200, {'api': {'block': {'height': 324}}, 'tenderdash': {'block': {'height': 11008}}}, 1)):
                check = p.explorer(raw)
            self.assertFalse(check['indexerRunning'])
            self.assertIsNone(check['indexerId'])
            self.assertEqual(check['indexedHeight'], 324)

    def test_explorer_runtime_state_and_health_are_not_existence(self):
        for state, expected in [({'Status': 'running', 'Running': True}, True),
                                ({'Status': 'restarting', 'Running': True, 'Restarting': True, 'ExitCode': 101}, False),
                                ({'Status': 'restarting', 'Running': True}, False),
                                ({'Status': 'exited', 'Running': False, 'ExitCode': 101}, False),
                                ({'Status': 'running', 'Running': True, 'Paused': True}, False),
                                ({'Status': 'running', 'Running': True, 'Health': {'Status': 'unhealthy'}}, True)]:
            with self.subTest(state=state):
                idx = container('ghcr.io/pshenmic/platform-explorer-indexer:2.5.3')
                idx.update(Id='indexer', RestartCount=7, State=state)
                raw = {'api': container('ghcr.io/pshenmic/platform-explorer-api:2.5.3'), 'idx': idx}
                with patch.object(p, 'get_json', return_value=(200, {}, 1)):
                    check = p.explorer(raw)
                self.assertEqual(check['indexerRunning'], expected)
                self.assertEqual(check['indexerState'], state['Status'])
                self.assertEqual(check['indexerHealth'], (state.get('Health') or {}).get('Status'))
                self.assertEqual(check['indexerRestarts'], 7)
                self.assertEqual(check['indexerRestarting'], state['Status'] == 'restarting')

    def test_explorer_selects_live_indexer_over_stopped_same_image(self):
        old = container('ghcr.io/pshenmic/platform-explorer-indexer:2.5.3')
        old['State'] = {'Status': 'exited', 'Running': False}
        live = container('ghcr.io/pshenmic/platform-explorer-indexer:2.5.3')
        live['Id'] = 'live-indexer'
        raw = {'api': container('ghcr.io/pshenmic/platform-explorer-api:2.5.3'), 'a-old': old, 'z-live': live}
        with patch.object(p, 'get_json', return_value=(200, {'api': 'bad', 'tenderdash': {'block': 'bad'}}, 1)):
            check = p.explorer(raw)
        self.assertTrue(check['indexerRunning'])
        self.assertEqual(check['indexerId'], 'live-indexer')
        self.assertIsNone(check['indexedHeight'])
        self.assertIsNone(check['chainHeight'])

    def test_malformed_protobuf_is_bounded(self):
        for data in [b'\x80'*100,b'\x0a\x08a',b'\x09a',b'\x0da']:
            with self.assertRaises(ValueError): p.protobuf(data)

    def test_zero_epoch_is_valid(self):
        self.assertEqual(p.protobuf(b'\x08\x00\x10\x01'), {1: 0, 2: 1})

    def test_prometheus_route_prefix_and_target_failure(self):
        for flags in [['--web.external-url=https://example.org/prometheus'],
                      ['--web.external-url=https://example.org/other', '--web.route-prefix', '/prometheus']]:
            def get(url, timeout, opener):
                self.assertEqual(url, 'http://172.20.0.2:9090/prometheus/api/v1/targets')
                return 200, {'status': 'success', 'data': {'activeTargets': [{'health': 'down'}]}}, 1
            with patch.object(p, 'get_json', get):
                check = p.role_services({'prom': container('prom/prometheus:latest', flags)})[0]
            self.assertTrue(check['ok'])
            self.assertEqual(check['down'], 1)

    def test_elasticsearch_auth_is_local_nonredirecting_and_red_is_failed(self):
        def get(req, timeout, opener):
            self.assertEqual(req.full_url, 'http://172.20.0.2:9200/_cluster/health')
            self.assertEqual(req.get_header('Authorization'), 'Basic ZWxhc3RpYzpEdW1teVNlY3JldA==')
            self.assertIs(opener, p.LOCAL_AUTH_OPENER)
            return 200, {'status': 'red', 'unassigned_shards': 35}, 1
        with patch.object(p, 'get_json', get):
            check = p.role_services({'es': container('docker.elastic.co/elasticsearch/elasticsearch:8', env=['ELASTIC_PASSWORD=DummySecret'])})[0]
        self.assertFalse(check['ok'])
        self.assertNotIn('DummySecret', json.dumps(check))
        self.assertIsNone(p.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://external.invalid'))

    def test_insight_devnet_decodes_public_output_locally(self):
        def get(url):
            path = url.split('/insight-api/')[1]
            return {'status?q=getInfo': {'info': {'blocks': 42}}, 'sync': {'status': 'finished'},
                    'block-index/42': {'blockHash': 'block'}, 'block/block': {'height': 42, 'tx': ['tx']},
                    'tx/tx': {'txid': 'tx', 'vout': [{'scriptPubKey': {'hex': '76a914' + '00' * 20 + '88ac'}}]},
                    'addr/devnet-address?noTxList=1': {'addrStr': 'devnet-address'}}[path]
        def run(args, timeout):
            self.assertEqual(args[:2], ['dash-cli', 'decodescript'])
            return b'{"address":"devnet-address"}'
        with patch.object(p, 'http_json', get), patch.object(p, 'core_cli', return_value=(['dash-cli'], 'native')), patch.object(p, 'run', run):
            check = p.insight({'insight': container('dashpay/insight:latest')})
        self.assertTrue(check['query']['ok'])
        self.assertNotIn('devnet-address', json.dumps(check))


if __name__ == '__main__':
    unittest.main()
