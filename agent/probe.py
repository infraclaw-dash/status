#!/usr/bin/env python3
"""Read-only host probe. Streamed over SSH to `sudo python3 - ROLE` and prints one JSON object.

Only local sources are queried (Docker, Core RPC through dash-cli, Tenderdash RPC,
DAPI gRPC through the local gateway, Insight, the faucet). Nothing is written.
"""
import calendar
import base64
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.parse

START = time.time()
ROLE = sys.argv[1] if len(sys.argv) > 1 else ''
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None

# Credentials from local containers must never follow a redirect off-host.
LOCAL_AUTH_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
errors = []


def run(args, timeout=20, stdin=None):
    p = subprocess.run(args, capture_output=True, timeout=timeout, input=stdin)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).decode(errors='replace').strip().splitlines()[-1:][0][:200] if (p.stderr or p.stdout) else 'exit %d' % p.returncode)
    return p.stdout


def parse(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors='replace').strip()


def http_json(url, timeout=6):
    with OPENER.open(url, timeout=timeout) as r:
        return json.loads(r.read(4 * 1024 * 1024))


def attempt(label, fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # every source is optional; report which failed
        errors.append('%s: %s' % (label, str(e)[:200]))
        return None


def system():
    mem = {}
    for line in open('/proc/meminfo'):
        k, v = line.split(':', 1)
        mem[k] = int(v.split()[0]) * 1024
    disks, seen = [], set()
    for mount in ['/', '/var/lib/docker', '/dash', '/home', '/data']:
        if not os.path.isdir(mount):
            continue
        st = os.stat(mount)
        if st.st_dev in seen:
            continue
        seen.add(st.st_dev)
        v = os.statvfs(mount)
        disks.append(dict(mount=mount, size=v.f_blocks * v.f_frsize, used=(v.f_blocks - v.f_bfree) * v.f_frsize,
                          avail=v.f_bavail * v.f_frsize))
    release = {}
    try:
        for line in open('/etc/os-release'):
            if '=' in line:
                k, v = line.rstrip().split('=', 1)
                release[k] = v.strip('"')
    except OSError:
        pass
    return dict(load=[float(x) for x in open('/proc/loadavg').read().split()[:3]], cpus=os.cpu_count(),
                memTotal=mem.get('MemTotal'), memAvailable=mem.get('MemAvailable'),
                swapTotal=mem.get('SwapTotal'), swapFree=mem.get('SwapFree'),
                uptime=float(open('/proc/uptime').read().split()[0]), disks=disks,
                kernel=os.uname().release, os=release.get('PRETTY_NAME'), arch=os.uname().machine)


def repo(image):
    name = image.split('@')[0]
    if ':' in name.rsplit('/', 1)[-1]:
        name = name.rsplit(':', 1)[0]
    return re.sub(r'^((index\.)?docker\.io/)?(library/)?', '', name)


def is_running(c):
    state = c.get('State') or {}
    return (state.get('Running') is True and state.get('Status') in (None, 'running')
            and not any(state.get(k) for k in ('Restarting', 'Paused', 'Dead')))


def explorer_component(c, component):
    config = c.get('Config') or {}
    labels = config.get('Labels') or {}
    # Forks and image-ID pins have no upstream repository name. The managed
    # services' exact Compose identity is stable across those deployments.
    services = ('explorer-api',) if component == 'api' else ('explorer-indexer', 'explorer-migrate')
    return (repo(config.get('Image', '')) in (
                'ghcr.io/pshenmic/platform-explorer-' + component,
                'ghcr.io/infraclaw-dash/platform-explorer-' + component)
            or (labels.get('com.docker.compose.project') == 'devnet-services'
                and labels.get('com.docker.compose.service') in services))


def find_explorer(raw, component, running=True):
    return next((c for _, c in sorted(raw.items()) if explorer_component(c, component)
                 and (not running or is_running(c))), None)


def explorer_migration(c):
    config = c.get('Config') or {}
    service = (config.get('Labels') or {}).get('com.docker.compose.service')
    return (explorer_component(c, 'indexer')
            and (service == 'explorer-migrate' or (config.get('Cmd') or [])[-1:] == ['migrate']))


def docker():
    if not shutil.which('docker'):
        return None, {}
    ids = run(['docker', 'ps', '-aq', '--no-trunc']).split()
    info = json.loads(run(['docker', 'inspect', *[i.decode() for i in ids]])) if ids else []
    images = {}
    unique = sorted({c['Image'] for c in info})
    if unique:
        for img in json.loads(run(['docker', 'image', 'inspect', *unique])):
            images[img['Id']] = img
    out, raw = [], {}
    for c in info:
        name = c['Name'].lstrip('/')
        img = images.get(c['Image'], {})
        ref = c['Config']['Image']
        digest = next((d for d in img.get('RepoDigests', []) if repo(d) == repo(ref)), None)
        st = c['State']
        ports = []
        for port, binds in (c['HostConfig'].get('PortBindings') or {}).items():
            for b in binds or []:
                ports.append('%s:%s->%s' % (b.get('HostIp') or '0.0.0.0', b.get('HostPort'), port))
        out.append(dict(id=c['Id'], name=name, image=ref, repo=repo(ref), digest=digest.split('@')[1] if digest else None,
                        imageCreated=img.get('Created'), state=st['Status'], running=is_running(c),
                        restarting=bool(st.get('Restarting') or st['Status'] == 'restarting'),
                        service=(c['Config'].get('Labels') or {}).get('com.docker.compose.service'),
                        restartPolicy=(c['HostConfig'].get('RestartPolicy') or {}).get('Name'), oneShot=explorer_migration(c),
                        health=(st.get('Health') or {}).get('Status'), startedAt=st.get('StartedAt'),
                        finishedAt=st.get('FinishedAt'), exitCode=st.get('ExitCode'), restarts=c.get('RestartCount', 0),
                        ports=sorted(ports), network=c['HostConfig'].get('NetworkMode')))
        raw[name] = c
    out.sort(key=lambda c: c['name'])
    return out, raw


def find(raw, repos, running=True):
    for name, c in sorted(raw.items()):
        if (c.get('Config', {}).get('Labels') or {}).get('com.docker.compose.service') == 'miner': continue
        if repo(c['Config']['Image']) in repos and (not running or is_running(c)):
            return c
    return None


def host_port(c, port):
    for b in (c['HostConfig'].get('PortBindings') or {}).get('%d/tcp' % port) or []:
        if b.get('HostIp', '') in ('', '0.0.0.0', '127.0.0.1'):
            return '127.0.0.1', int(b['HostPort'])
    if c['HostConfig'].get('NetworkMode') == 'host':
        return '127.0.0.1', port
    for n in (c['NetworkSettings'].get('Networks') or {}).values():
        if n.get('IPAddress'):
            return n['IPAddress'], port
    raise RuntimeError('no reachable port %d' % port)


def core_cli(raw):
    c = find(raw, ['dashpay/dashd'])
    if c:
        args = ['docker', 'exec', c['Id'], 'dash-cli']
        confs = [m['Destination'] for m in c['Mounts'] if m['Destination'].endswith('dash.conf')]
        cmd = (c['Config'].get('Entrypoint') or []) + (c['Config'].get('Cmd') or [])
        for a in cmd:
            if a.startswith(('-conf=', '-datadir=')):
                args.append(a)
        if confs and not any(a.startswith('-conf=') for a in args):
            args.append('-conf=' + confs[0])
        return args, c['Name'].lstrip('/')
    if shutil.which('dash-cli') or os.path.exists('/usr/local/bin/dash-cli'):
        return ['sudo', '-u', 'ubuntu', '-H', shutil.which('dash-cli') or '/usr/local/bin/dash-cli'], 'native'
    return None, None


def core(raw):
    cli, where = core_cli(raw)
    if not cli:
        return None
    call = lambda *a: parse(run(cli + [str(x) for x in a], timeout=20))
    t = time.monotonic()
    bc = call('getblockchaininfo')
    rpc_ms = round((time.monotonic() - t) * 1000)
    net = attempt('core getnetworkinfo', call, 'getnetworkinfo') or {}
    out = dict(rpcLatencyMs=rpc_ms, source=where, chain=bc.get('chain'), blocks=bc.get('blocks'), headers=bc.get('headers'),
               bestBlockHash=bc.get('bestblockhash'), blockTime=bc.get('time'), medianTime=bc.get('mediantime'),
               ibd=bc.get('initialblockdownload'), progress=bc.get('verificationprogress'), sizeOnDisk=bc.get('size_on_disk'),
               pruned=bc.get('pruned'), difficulty=bc.get('difficulty'),
               version=net.get('version'), subversion=net.get('subversion'), protocol=net.get('protocolversion'),
               connections=net.get('connections'), connectionsIn=net.get('connections_in'), connectionsOut=net.get('connections_out'))
    sync = attempt('core mnsync', call, 'mnsync', 'status')
    if isinstance(sync, dict):
        out['synced'] = bool(sync.get('IsSynced'))
    cl = attempt('core chainlock', call, 'getbestchainlock')
    if isinstance(cl, dict):
        out['chainLockHeight'] = cl.get('height')
        if cl.get('blockhash'):
            header = attempt('ChainLock header', call, 'getblockheader', cl['blockhash']) or {}
            out['chainLockTime'] = header.get('time')
    peers = attempt('core peers', call, 'getpeerinfo')
    if isinstance(peers, list):
        groups = set()
        networks = {}
        for peer in peers:
            if peer.get('inbound'):
                continue
            kind = peer.get('network', 'unknown')
            networks[kind] = networks.get(kind, 0) + 1
            address = peer.get('addr', '').rsplit(':', 1)[0].strip('[]')
            try:
                ip = ipaddress.ip_address(address)
                groups.add(str(ipaddress.ip_network(str(ip) + ('/16' if ip.version == 4 else '/32'), strict=False)))
            except ValueError:
                if kind in ('onion', 'i2p'): groups.add(address)
        out['peerDiversity'] = dict(outbound=sum(networks.values()), groups=len(groups), networks=networks)
    if ROLE in ('seed', 'wallet'):
        qs = attempt('quorum list', call, 'quorum', 'list')
        if isinstance(qs, dict):
            out['quorums'] = {kind: len(hashes) for kind, hashes in qs.items() if isinstance(hashes, list)}
    if ROLE in ('validator', 'masternode'):
        dkg = attempt('DKG status', call, 'quorum', 'dkgstatus', '0')
        if isinstance(dkg, dict):
            out['dkg'] = [dict(type=v.get('llmqType'), phase=v.get('status', {}).get('phase'), height=v.get('status', {}).get('quorumHeight'), aborted=v.get('status', {}).get('aborted'),
                               receivedContributions=v.get('status', {}).get('receivedContributions'), receivedComplaints=v.get('status', {}).get('receivedComplaints'),
                               receivedJustifications=v.get('status', {}).get('receivedJustifications'), receivedPrematureCommitments=v.get('status', {}).get('receivedPrematureCommitments'))
                          for v in dkg.get('session', []) if isinstance(v, dict)]
    mp = attempt('core mempool', call, 'getmempoolinfo')
    if isinstance(mp, dict):
        out['mempool'] = mp.get('size')
    if ROLE in ('validator', 'masternode'):
        mn = attempt('core masternode status', call, 'masternode', 'status')
        if isinstance(mn, dict):
            st = mn.get('dmnState') or {}
            out['masternode'] = dict(state=mn.get('state'), status=mn.get('status'), proTxHash=mn.get('proTxHash'),
                                     service=mn.get('service'), type=mn.get('type'), posePenalty=st.get('PoSePenalty'),
                                     poseBanHeight=st.get('PoSeBanHeight'), lastPaidHeight=st.get('lastPaidHeight'),
                                     registeredHeight=st.get('registeredHeight'))
    wallets = attempt('core listwallets', call, 'listwallets') if ROLE in ('wallet', 'mixer', 'miner') else None
    if isinstance(wallets, list) and wallets:
        out['wallets'] = []
        out['payouts'] = []
        for w in wallets[:20]:
            b = attempt('core wallet ' + w, call, '-rpcwallet=' + w, 'getbalances')
            if isinstance(b, dict):
                mine = b.get('mine') or {}
                out['wallets'].append(dict(name=w, trusted=mine.get('trusted'), pending=mine.get('untrusted_pending'),
                                           immature=mine.get('immature'), coinjoin=mine.get('coinjoin')))
            if ROLE == 'wallet' and 'faucet' in w.lower():
                txs = attempt('faucet recent transactions', call, '-rpcwallet=' + w, 'listtransactions', '*', '20')
                if isinstance(txs, list):
                    sent = {v['txid']: v for v in txs if v.get('category') == 'send' and v.get('txid')}
                    for tx in list(sent.values())[-3:]:
                        detail = attempt('payout confirmation', call, '-rpcwallet=' + w, 'gettransaction', tx['txid']) or {}
                        out['payouts'].append(dict(time=tx.get('time'), confirmations=detail.get('confirmations'),
                                                   instantlock=detail.get('instantlock'), chainlock=detail.get('chainlock'),
                                                   abandoned=detail.get('abandoned', tx.get('abandoned', False))))
    if ROLE == 'mixer':
        cj = attempt('coinjoin info', call, 'getcoinjoininfo')
        if isinstance(cj, dict): out['coinjoin'] = {k: cj.get(k) for k in ('enabled', 'running', 'queue_size', 'sessions') if isinstance(cj.get(k), (bool, int))}
    if ROLE == 'miner':
        mi = attempt('core mining', call, 'getmininginfo')
        if isinstance(mi, dict):
            out['mining'] = dict(hashps=mi.get('networkhashps'), difficulty=mi.get('difficulty'))
    return out


def tenderdash(raw):
    c = find(raw, ['dashpay/tenderdash'])
    if not c:
        return None
    # RPC is often bound only inside the container: query it from its network namespace.
    pid = str(c['State']['Pid'])
    bound = (c['HostConfig'].get('PortBindings') or {})
    ports = [p for p in (36657, 26657) if '%d/tcp' % p in bound] or [26657, 36657]

    def fetch(path, port):
        raw_ = run(['nsenter', '-t', pid, '-n', 'curl', '-s', '--fail', '-m', '6', 'http://127.0.0.1:%d/%s' % (port, path)], timeout=10)
        v = json.loads(raw_)
        if v.get('error'):
            raise RuntimeError(str(v['error'])[:120])
        return v.get('result', v)
    port = None
    for candidate in ports:
        try:
            st = fetch('status', candidate); port = candidate; break
        except Exception:
            continue
    if port is None and ROLE == 'validator':
        raise RuntimeError('rpc unreachable on %s' % ports)
    if port is None:
        # Seed-mode nodes expose no RPC: count established P2P sessions and read
        # the chain ID from the metrics endpoint instead.
        est = run(['nsenter', '-t', pid, '-n', 'ss', '-Htn', 'state', 'established'], timeout=10).decode().splitlines()
        p2p = [l for l in est if ':36656 ' in l or ':26656 ' in l]
        out = dict(source='p2p', peers=len(p2p))
        for mport in (36660, 26660):
            try:
                text = run(['nsenter', '-t', pid, '-n', 'curl', '-s', '--fail', '-m', '6', 'http://127.0.0.1:%d/metrics' % mport], timeout=10).decode()
                m = re.search(r'chain_id="([^"]+)"', text)
                if m:
                    out['network'] = m.group(1)
                break
            except Exception:
                continue
        return out
        raise RuntimeError('rpc unreachable on %s' % ports)
    get = lambda p: fetch(p, port)
    ni, si, vi = st.get('node_info') or {}, st.get('sync_info') or {}, st.get('validator_info') or {}
    out = dict(network=ni.get('network'), version=ni.get('version'), nodeId=ni.get('id'),
               protocolApp=int((ni.get('protocol_version') or {}).get('app') or 0) or None,
               height=int(si.get('latest_block_height') or 0), blockTime=si.get('latest_block_time'),
               catchingUp=si.get('catching_up'), proTxHash=vi.get('pro_tx_hash'),
               votingPower=int(vi['voting_power']) if vi.get('voting_power') is not None else None)
    net = attempt('tenderdash net_info', get, 'net_info')
    if isinstance(net, dict):
        out['peers'] = int(net.get('n_peers') or 0)
    if ROLE == 'validator':
        vals = attempt('tenderdash validators', get, 'validators?per_page=100')
        if isinstance(vals, dict):
            out['validatorSetSize'] = int(vals.get('total') or len(vals.get('validators') or []))
            out['inValidatorSet'] = any((v.get('pro_tx_hash') or '').lower() == (out['proTxHash'] or '').lower()
                                        for v in vals.get('validators') or [])
    if ROLE == 'validator':
        consensus = attempt('consensus state', get, 'consensus_state') or {}
        rs = consensus.get('round_state', {})
        hrs = str(rs.get('height/round/step', '')).split('/')
        out['round'] = int(hrs[1]) if len(hrs) > 1 and hrs[1].isdigit() else None
        block = attempt('latest commit', get, 'block') or {}
        block = block.get('block', {})
        header = block.get('header', {})
        out['proposer'] = header.get('proposer_pro_tx_hash') or header.get('proposer_address')
        out['commitRound'] = (block.get('last_commit') or {}).get('round')
        # Tenderdash has a threshold block signature, not individual Tendermint signatures.
        out['thresholdSigned'] = bool((block.get('last_commit') or {}).get('threshold_block_signature'))
    return out


def protobuf(raw):
    fields, i = {}, 0

    def varint():
        nonlocal i
        shift = value = 0
        while True:
            if i >= len(raw) or shift > 63: raise ValueError('truncated protobuf')
            b = raw[i]; i += 1
            value |= (b & 0x7f) << shift
            if b < 0x80:
                return value
            shift += 7
    while i < len(raw):
        key = varint(); number, wire = key >> 3, key & 7
        if wire == 0:
            value = varint()
        elif wire == 2:
            n = varint(); value = raw[i:i + n]; i += n
        elif wire == 1:
            value = raw[i:i + 8]; i += 8
        elif wire == 5:
            value = raw[i:i + 4]; i += 4
        else:
            raise ValueError('protobuf wire type')
        if i > len(raw): raise ValueError('truncated protobuf')
        fields[number] = value
    return fields


def dapi(raw, address):
    gw = find(raw, ['dashpay/envoy'])
    if not gw:
        return None
    ports = (gw['HostConfig'].get('PortBindings') or {})
    port = next((int(b['HostPort']) for k, v in ports.items() if k == '10000/tcp' for b in v or []), None)
    if not port and gw['HostConfig'].get('NetworkMode') == 'host':
        port = 1443  # dash-network-go gateway: host networking, TLS on 1443
    if not port:
        port = next((int(b['HostPort']) for k, v in ports.items() for b in v or [] if b.get('HostPort') == '443'), 443)
    cert = [m['Source'] for m in gw['Mounts'] if m['Destination'].endswith('/bundle.crt')]
    def query(method, payload):
        with tempfile.TemporaryDirectory(prefix='status-probe-') as d:
            headers = os.path.join(d, 'h')
            args = ['curl', '--silent', '--show-error', '--fail', '--noproxy', '*', '--max-time', '10', '--http2',
                    '-D', headers, '-H', 'content-type: application/grpc', '-H', 'te: trailers', '--data-binary', '@-']
            args += (['--cacert', cert[0], '--connect-to', '%s:%d:127.0.0.1:%d' % (address, port, port),
                      'https://%s:%d/org.dash.platform.dapi.v0.Platform/%s' % (address, port, method)]
                     if cert and address else ['-k', 'https://127.0.0.1:%d/org.dash.platform.dapi.v0.Platform/%s' % (port, method)])
            t = time.time()
            body = run(args, timeout=15, stdin=b'\x00' + len(payload).to_bytes(4, 'big') + payload)
            latency = round((time.time() - t) * 1000)
            if 'grpc-status: 0' not in open(headers).read().lower():
                raise RuntimeError('grpc-status not ok')
        return body, latency
    body, latency = query('getStatus', b'\x0a\x00')
    if len(body) < 5 or body[0] != 0 or int.from_bytes(body[1:5], 'big') > len(body)-5:
        raise RuntimeError('bad grpc frame')
    v0 = protobuf(protobuf(body[5:5 + int.from_bytes(body[1:5], 'big')])[1])
    version = protobuf(v0.get(1, b''))
    software = protobuf(version.get(1, b''))
    protocol = protobuf(version.get(2, b''))
    chain = protobuf(v0.get(3, b''))
    network = protobuf(v0.get(4, b''))
    txt = lambda b: b.decode(errors='replace') if isinstance(b, bytes) else None
    tdp = protobuf(protocol.get(1, b'')) if protocol.get(1) else {}
    drp = protobuf(protocol.get(2, b'')) if protocol.get(2) else {}
    drive_query = None
    try:
        # Latest epoch: count=1, descending, non-proof response. This hits Drive.
        response, elapsed = query('getEpochsInfo', b'\x0a\x02\x10\x01')
        if len(response) < 5 or response[0] != 0 or len(response) - 5 < int.from_bytes(response[1:5], 'big'):
            raise ValueError('truncated epoch response')
        v = protobuf(protobuf(response[5:5 + int.from_bytes(response[1:5], 'big')])[1])
        epoch = protobuf(protobuf(v[1])[1])
        drive_query = dict(ok=True, method='getEpochsInfo', latencyMs=elapsed, epoch=epoch.get(1, 0),
                           firstBlockHeight=epoch.get(2), protocol=epoch.get(6))
    except Exception as exc:
        drive_query = dict(ok=False, method='getEpochsInfo', error=type(exc).__name__)
    return dict(query=drive_query, ok=True, latencyMs=latency, dapiVersion=txt(software.get(1)), driveVersion=txt(software.get(2)),
                tenderdashVersion=txt(software.get(3)), height=chain.get(4), catchingUp=bool(chain.get(1, 0)),
                chainId=txt(network.get(1)), peers=network.get(2),
                driveProtocol=drp.get(2) or drp.get(1), tenderdashP2P=tdp.get(1),
                tls=attempt('gateway tls', gateway_tls, address, port) if address else None)


def gateway_tls(address, port):
    """What a client connecting to the public IP sees: is the gateway's
    certificate publicly trusted for that IP, who issued it, when it expires."""
    def fetch(ctx):
        with socket.create_connection(('127.0.0.1', port), timeout=5) as raw:
            with ctx.wrap_socket(raw, server_hostname=address) as conn:
                return conn.getpeercert(binary_form=True)
    expired = False
    try:
        der, trusted = fetch(ssl.create_default_context()), True
    except ssl.SSLCertVerificationError as e:
        # An expired publicly issued certificate fails verification too.
        expired = e.verify_code == 10  # X509_V_ERR_CERT_HAS_EXPIRED
        der, trusted = fetch(ssl._create_unverified_context()), False
    text = subprocess.run(['openssl', 'x509', '-inform', 'DER', '-noout', '-issuer', '-enddate'], input=der, capture_output=True, timeout=10).stdout.decode()
    line = next((l for l in text.splitlines() if l.startswith('issuer=')), '')
    field = lambda k: (re.search(r'(?:^|[,/=\s])' + k + r'\s*=\s*([^,/\n]+)', line[7:]) or [None, None])[1]
    end = re.search(r'notAfter=(.+)', text)
    expires = calendar.timegm(time.strptime(end.group(1).strip(), '%b %d %H:%M:%S %Y %Z')) if end else None
    return dict(trusted=trusted, expired=expired, issuer=' '.join(x.strip() for x in [field('O'), field('CN')] if x) or None,
                expiresAt=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(expires)) if expires else None)


def insight(raw):
    c = find(raw, ['dashpay/insight'])
    if not c:
        return None
    host, port = host_port(c, 3001)
    base = 'http://%s:%d/insight-api/' % (host, port)
    info = http_json(base + 'status?q=getInfo').get('info', {})
    sync = attempt('insight sync', http_json, base + 'sync') or {}
    functional = dict(ok=False)
    try:
        t = time.monotonic()
        tip = http_json(base + 'block-index/' + str(info['blocks']))['blockHash']
        block = http_json(base + 'block/' + tip)
        tx = http_json(base + 'tx/' + block['tx'][0])
        addresses = [a for v in tx.get('vout', []) for a in v.get('scriptPubKey', {}).get('addresses', [])]
        # Legacy Insight omits decoded addresses on devnets. Decode this public
        # output through the same host's Core, not a hardcoded network prefix.
        if not addresses:
            cli, _ = core_cli(raw)
            for v in tx.get('vout', [])[:3] if cli else []:
                script = v.get('scriptPubKey', {}).get('hex', '')
                if not re.fullmatch(r'[0-9a-fA-F]{2,10000}', script): continue
                decoded = parse(run(cli + ['decodescript', script], timeout=4))
                if not isinstance(decoded, dict): continue
                addresses = decoded.get('addresses') or ([decoded['address']] if decoded.get('address') else [])
                if addresses: break
        if not addresses:
            return dict(query=dict(ok=None, reason='recent transaction has no decodable address', blockAndTransaction=bool(tx.get('txid')) and block.get('height') == info['blocks']),
                        blocks=info.get('blocks'), syncStatus=sync.get('status'), syncHeight=sync.get('height'))
        address = http_json(base + 'addr/' + addresses[0] + '?noTxList=1')
        functional = dict(ok=block.get('height') == info['blocks'] and bool(tx.get('txid')) and bool(address.get('addrStr')),
                          latencyMs=round((time.monotonic() - t) * 1000), height=block.get('height'))
    except Exception as exc:
        functional['error'] = type(exc).__name__
    return dict(query=functional, blocks=info.get('blocks'), version=info.get('version'), network=info.get('network'),
                syncStatus=sync.get('status'), syncPercentage=sync.get('syncPercentage'),
                syncHeight=sync.get('height') or sync.get('blockChainHeight'), error=sync.get('error'))


def http_check(raw, repos, port):
    c = find(raw, repos)
    if not c:
        return None
    host, p = host_port(c, port)
    t = time.time()
    try:
        with OPENER.open('http://%s:%d/' % (host, p), timeout=8) as r:
            code = r.status
            body = r.read(65536).decode(errors='replace')
    except urllib.error.HTTPError as e:
        code, body = e.code, ''
    title = re.search(r'<title>(.*?)</title>', body, re.S | re.I)
    return dict(status=code, latencyMs=round((time.time() - t) * 1000), title=title.group(1).strip()[:80] if title else None)


def get_json(url, timeout=8, opener=OPENER):
    t = time.time()
    try:
        with opener.open(url, timeout=timeout) as r:
            return r.status, json.loads(r.read(2 * 1024 * 1024) or b'null'), round((time.time() - t) * 1000)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read(1024 * 1024) or b'null')
        except ValueError:
            body = None
        return e.code, body, round((time.time() - t) * 1000)


def quorum_server(raw):
    c = find(raw, ['dashpay/quorum-list-server'])
    if not c:
        return None
    port = 8080 if c['HostConfig'].get('NetworkMode') == 'host' else host_port(c, 8080)[1]
    code, _, ms = get_json('http://127.0.0.1:%d/health' % port)
    qcode, q, _ = get_json('http://127.0.0.1:%d/quorums' % port)
    data = q.get('data') if isinstance(q, dict) else None
    return dict(status=code, latencyMs=ms, quorums=len(data) if isinstance(data, list) else None, quorumsStatus=qcode)


def explorer(raw):
    c = find_explorer(raw, 'api')
    if not c:
        return None
    code, v, ms = get_json('http://127.0.0.1:3005/status', 10)
    v = v if isinstance(v, dict) else {}
    # The migration uses the same image as the long-running indexer. It must
    # never stand in for it, even while migrating, nor mask a crash/restart loop.
    candidates = {name: c for name, c in raw.items() if not explorer_migration(c)}
    idx = find_explorer(candidates, 'indexer') or find_explorer(candidates, 'indexer', running=False)
    state = (idx or {}).get('State') or {}
    api = v.get('api') if isinstance(v.get('api'), dict) else {}
    chain = v.get('tenderdash') if isinstance(v.get('tenderdash'), dict) else {}
    height = lambda obj: (obj.get('block') or {}).get('height') if isinstance(obj.get('block'), dict) else None
    return dict(status=code, latencyMs=ms, apiVersion=api.get('version'), indexedHeight=height(api),
                chainHeight=height(chain), network=v.get('network'),
                identities=v.get('identitiesCount'), transactions=v.get('transactionsCount'), indexerRunning=bool(idx and is_running(idx)),
                indexerId=(idx or {}).get('Id'), indexerState=state.get('Status'),
                indexerRestarting=bool(state.get('Restarting') or state.get('Status') == 'restarting'),
                indexerHealth=(state.get('Health') or {}).get('Status'), indexerExitCode=state.get('ExitCode'),
                indexerRestarts=(idx or {}).get('RestartCount'))


def new_faucet(raw):
    c = next((c for n, c in sorted(raw.items()) if c['State']['Running'] and (repo(c['Config']['Image']) in ('dashpay/dash-faucet', 'devnet-faucet'))), None)
    if not c:
        return None
    code, v, ms = get_json('http://127.0.0.1:8000/api/status', 10)
    v = v if isinstance(v, dict) else {}
    return dict(status=code, latencyMs=ms, state=v.get('status'), balance=v.get('balance'), blockHeight=v.get('block_height') or v.get('blockHeight'),
                utxos=v.get('available_utxos') or v.get('availableUtxos'), kind='dash-faucet')



def legacy_faucet(raw):
    c = find(raw, ['dashpay/multifaucet'])
    if not c:
        return None
    out = http_check(raw, ['dashpay/multifaucet'], 80)
    out['kind'] = 'multifaucet'
    php = """<?php
error_reporting(0);
try {
require '/var/www/html/config/db.conf.php';
$db = mysqli_init(); $db->options(MYSQLI_OPT_CONNECT_TIMEOUT, 4);
$db->real_connect(DB_HOST, DB_USER, DB_PASS, DB_NAME);
// HotWallet writes an attempt before synchronous send; a missing txid is not
// a queued request. Preserve that history separately from the actual queue.
$q = $db->query("SELECT COUNT(*) total, (SELECT COUNT(*) FROM faucet_pending_payments) queued, (SELECT MIN(UNIX_TIMESTAMP(created_date)) FROM faucet_pending_payments) oldestQueuedAt, MAX(IF(txid IS NOT NULL AND txid!='', UNIX_TIMESTAMP(lastupdate),NULL)) lastBroadcastAt, COALESCE(SUM(txid IS NULL OR txid=''),0) payoutsWithoutTxidCount, MAX(IF(txid IS NULL OR txid='', UNIX_TIMESTAMP(timestamp),NULL)) lastIncompleteAttemptAt FROM faucet_payouts");
$v = $q->fetch_assoc();
$r = $db->query("SELECT txid FROM faucet_payouts WHERE txid IS NOT NULL AND txid!='' ORDER BY id DESC LIMIT 3");
$v['recent'] = array(); while ($row = $r->fetch_assoc()) $v['recent'][] = $row['txid'];
echo json_encode($v);
} catch (Throwable $e) { echo '{"error":"database unavailable"}'; exit(1); }
"""
    db = attempt('faucet queue', lambda: json.loads(run(['docker','exec','-i',c['Id'],'php'], timeout=10, stdin=php.encode())))
    if not db or 'error' in db:
        out['queue'] = dict(ok=False)
        return out
    fields = ('total','queued','oldestQueuedAt','lastBroadcastAt','payoutsWithoutTxidCount','lastIncompleteAttemptAt')
    try:
        if not isinstance(db, dict) or any(isinstance(db.get(k), bool) for k in fields):
            raise ValueError('malformed queue')
        queue = {k: int(db[k]) if db[k] is not None else None for k in fields}
        if any(queue[k] is None or queue[k] < 0 for k in ('total','queued','payoutsWithoutTxidCount')):
            raise ValueError('malformed queue counts')
        if queue['payoutsWithoutTxidCount'] > queue['total'] or any(v is not None and v < 0 for v in queue.values()):
            raise ValueError('malformed queue history')
        if queue['queued'] > 0 and queue['oldestQueuedAt'] is None:
            raise ValueError('missing pending payment timestamp')
    except (KeyError, TypeError, ValueError):
        out['queue'] = dict(ok=False)
        return out
    out['queue'] = dict(ok=True, **queue)
    cli, _ = core_cli(raw)
    out['payouts'] = []
    if cli:
        for txid in db.get('recent', []):
            if not re.fullmatch('[0-9a-fA-F]{64}', txid): continue
            tx = attempt('faucet broadcast confirmation', lambda: json.loads(run(cli + ['getrawtransaction',txid,'true'],timeout=6)))
            if isinstance(tx, dict):
                out['payouts'].append(dict(confirmations=tx.get('confirmations',0), instantlock=tx.get('instantlock'), chainlock=tx.get('chainlock'), time=tx.get('time')))
    return out


def role_services(raw):
    checks = []
    specs = [('grafana', ['grafana/grafana'], 3000, '/api/health'),
             ('prometheus', ['prom/prometheus'], 9090, '/api/v1/targets'),
             ('elasticsearch', ['docker.elastic.co/elasticsearch/elasticsearch'], 9200, '/_cluster/health'),
             ('kibana', ['docker.elastic.co/kibana/kibana'], 5601, '/api/status')]
    for name, repos, port, path in specs:
        c = find(raw, repos)
        if not c: continue
        try:
            host, bound = host_port(c, port)
            if name == 'prometheus':
                cmd = c['Config'].get('Cmd') or []
                def flag_value(flag):
                    for i, arg in enumerate(cmd):
                        if arg.startswith(flag + '='): return arg.split('=', 1)[1]
                        if arg == flag and i + 1 < len(cmd): return cmd[i + 1]
                    return None
                prefix = flag_value('--web.route-prefix')
                if prefix is None: prefix = urllib.parse.urlparse(flag_value('--web.external-url') or '').path
                path = '/' + (prefix or '').strip('/') + path if (prefix or '').strip('/') else path
            # Inspect the local container endpoint; never send a local service
            # credential through a public reverse proxy or redirect.
            local_ip = next((n.get('IPAddress') for n in (c.get('NetworkSettings', {}).get('Networks') or {}).values() if n.get('IPAddress')), None)
            if local_ip and ipaddress.ip_address(local_ip).is_private: host, bound = local_ip, port
            request = 'http://%s:%d%s' % (host,bound,path)
            opener = OPENER
            if name == 'elasticsearch':
                password = next((v.split('=',1)[1] for v in c['Config'].get('Env',[]) if v.startswith('ELASTIC_PASSWORD=')), None)
                if password:
                    credential = base64.b64encode(('elastic:' + password).encode()).decode()
                    request = urllib.request.Request(request, headers={'Authorization':'Basic ' + credential})
                    opener = LOCAL_AUTH_OPENER
            code, data, ms = get_json(request,4,opener)
            ok = code == 200
            facts = {}
            if code in (401,403):
                checks.append(dict(service=name,ok=None,status=code,reason='authentication required'))
                continue
            if not isinstance(data,dict):
                checks.append(dict(service=name,ok=False,status=code,reason='invalid health response'))
                continue
            if name == 'prometheus':
                targets = (data.get('data') or {}).get('activeTargets')
                ok = ok and data.get('status') == 'success' and isinstance(targets,list) and len(targets)>0
                facts = dict(targets=len(targets or []), down=sum(t.get('health')!='up' for t in targets or []))
            if name == 'grafana': ok = ok and data.get('database') == 'ok'
            if name == 'elasticsearch':
                facts = dict(cluster=data.get('status'), unassigned=data.get('unassigned_shards'))
                ok = ok and data.get('status') in ('green','yellow')
            if name == 'kibana':
                overall=(data.get('status') or {}).get('overall') or {}
                ok = ok and (overall.get('level') in ('available','degraded') or overall.get('state') in ('green','yellow'))
            checks.append(dict(service=name,ok=bool(ok),status=code,latencyMs=ms,**facts))
        except Exception as exc:
            checks.append(dict(service=name,ok=False,error=type(exc).__name__))
    if ROLE == 'miner':
        # Service status only; never start the miner or generate a block.
        mining = [c for c in raw.values() if (c.get('Config', {}).get('Labels') or {}).get('com.docker.compose.service') == 'miner']
        active = ('active' if any(c['State']['Running'] and not c['State'].get('Restarting') for c in mining) else 'inactive') if mining else subprocess.run(['systemctl','is-active','dashd-generate-miner.service'],capture_output=True,timeout=4).stdout.decode().strip()
        checks.append(dict(service='miner',ok=active=='active',state=active or 'unavailable'))
    return checks


def main():
    result = dict(role=ROLE, system=attempt('system', system))
    containers, raw = attempt('docker', docker) or (None, {})
    result['containers'] = containers
    address = sys.argv[2] if len(sys.argv) > 2 else ''
    result['core'] = attempt('core', core, raw)
    result['tenderdash'] = attempt('tenderdash', tenderdash, raw)
    if ROLE == 'validator':
        try:
            result['dapi'] = dapi(raw, address)
        except Exception as e:
            result['dapi'] = dict(ok=False, error=str(e)[:200])
    result['insight'] = attempt('insight', insight, raw)
    result['faucet'] = attempt('faucet', new_faucet, raw) or attempt('faucet', legacy_faucet, raw)
    result['quorumServer'] = attempt('quorum server', quorum_server, raw)
    result['explorer'] = attempt('explorer', explorer, raw)
    result['services'] = attempt('role services', role_services, raw) or []
    result['errors'] = errors
    result['probeMs'] = round((time.time() - START) * 1000)
    print(json.dumps(result, separators=(',', ':')))


if __name__ == "__main__":
    main()
