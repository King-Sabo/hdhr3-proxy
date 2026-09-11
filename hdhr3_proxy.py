#!/usr/bin/env python3
"""
hdhr3_proxy.py -- expose a legacy HDHomeRun (HDHR3-EU / HDHR3-US / HDHR-US) to
Plex, Emby, Jellyfin or Channels as if it were a modern HTTP-capable tuner.

The HDHR3 has no HTTP interface: SiliconDust confirmed the hardware cannot do
it. It only pushes MPEG-TS over UDP to a target address set via the control
protocol on port 65001. Plex only speaks HTTP. This bridges the two:

  Plex  --HTTP GET /auto/v11-->  proxy  --set channel/program/target-->  HDHR3
  Plex  <--chunked MPEG-TS-----  proxy  <----------UDP TS packets-------  HDHR3

Endpoints served (the subset Plex actually uses):
  /discover.json  /lineup_status.json  /lineup.json  /lineup.post
  /device.xml     /auto/v<GuideNumber>  /epg.xml

Lineup comes from the muxes.json written by `hdhr3_epg.py scan`.

  ./hdhr3_epg.py  scan -d 1220EEF8 --channelmap eu-cable -o muxes.json
  ./hdhr3_proxy.py -d 1220EEF8 -m muxes.json --channelmap eu-cable \
                  --epg guide.xml --port 5004

Then in Plex: Live TV & DVR -> "Don't see your device?" -> http://<host>:5004

Requires: python3 (stdlib only) + hdhomerun_config in PATH.
"""

import argparse
import json
import os
import re
import shutil
import socket
import struct
import datetime as dt
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from xml.sax.saxutils import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    import hdhr3_epg as epg
except ImportError:
    sys.exit('hdhr3_epg.py must sit next to hdhr3_proxy.py')

TS_PACKET = 188
UDP_DGRAM = 1500
STREAM_TIMEOUT = 8.0          # seconds without UDP data before giving up
LOCK_TIMEOUT = 12.0

# ------------------------------------------------------------ hdhomerun_config

BINARY_NAMES = ('hdhomerun_config.exe', 'hdhomerun_config')

_SEARCH_DIRS = [
    os.environ.get('ProgramFiles', r'C:\Program Files'),
    os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
    os.environ.get('ProgramW6432', r'C:\Program Files'),
    r'C:\Program Files', r'C:\Program Files (x86)',
    '/usr/bin', '/usr/local/bin', '/opt/bin', '/opt/local/bin',
    '/usr/pkg/bin', os.path.expanduser('~/bin'),
]
_SUBDIRS = [('Silicondust', 'HDHomeRun'), ('SiliconDust', 'HDHomeRun'),
            ('HDHomeRun',), ()]


def find_binary(explicit=None):
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        found = shutil.which(explicit)
        if found:
            return found
        raise SystemExit('hdhomerun_config not usable at: %s' % explicit)
    for name in BINARY_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for base in _SEARCH_DIRS:
        if not base:
            continue
        for sub in _SUBDIRS:
            for name in BINARY_NAMES:
                cand = os.path.join(base, *sub, name)
                if os.path.isfile(cand):
                    return cand
    raise SystemExit('hdhomerun_config not found; pass --binary <full path>')


class Device:
    """Thin control wrapper. Each command is its own short-lived connection;
    tuner state (channel/program/target) persists on the device afterwards."""

    def __init__(self, dev_id, binary, channelmap=None, modulation='auto'):
        self.id = dev_id
        self.bin = binary
        self.channelmap = channelmap
        self.modulation = modulation
        self._cm_warned = False
        self._lock = threading.Lock()

    def _run(self, *args, timeout=20):
        cmd = [self.bin, self.id] + list(args)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = ((p.stdout or '') + (p.stderr or '')).strip()
        if p.returncode != 0 or out.startswith('ERROR'):
            raise RuntimeError('%s -> %s' % (' '.join(args), out or 'failed'))
        return out

    def get(self, item):
        with self._lock:
            return self._run('get', item)

    def set(self, item, value):
        with self._lock:
            return self._run('set', item, value)

    def ip(self):
        out = subprocess.run([self.bin, 'discover'], capture_output=True,
                             text=True, timeout=15).stdout
        for line in out.splitlines():
            m = re.search(r'device\s+(\S+)\s+found at\s+(\S+)', line, re.I)
            if m and (self.id.upper() in ('FFFFFFFF', m.group(1).upper())):
                return m.group(2)
        raise SystemExit('device %s not found on the network' % self.id)

    def tuner_count(self):
        for n in range(8):
            try:
                self.get('/tuner%d/status' % n)
            except RuntimeError as e:
                if n < 2:
                    sys.stderr.write(
                        'warning: /tuner%d/status failed (%s)\n'
                        '  reporting %d tuner(s). An HDHR3 DUAL has 2 -- if '
                        'this is wrong the device may be wedged;\n'
                        '  power-cycle it, or force it with --tuners 2.\n'
                        % (n, str(e).split('-> ')[-1], max(n, 1)))
                return max(n, 1)
        return 8

    # -- tuning ----------------------------------------------------------
    def set_channelmap(self, n):
        """Best effort: tuning uses an explicit frequency, so a device that
        rejects this variable can still be tuned."""
        if not self.channelmap:
            return
        try:
            self.set('/tuner%d/channelmap' % n, self.channelmap)
        except RuntimeError as e:
            if not self._cm_warned:
                self._cm_warned = True
                sys.stderr.write('warning: channelmap %s rejected (%s); '
                                 'tuning by frequency\n'
                                 % (self.channelmap, str(e).split('-> ')[-1]))

    def tune(self, n, freq, program, target):
        self.set_channelmap(n)
        self.set('/tuner%d/channel' % n, '%s:%d' % (self.modulation, freq))
        deadline = time.time() + LOCK_TIMEOUT
        status = ''
        while time.time() < deadline:
            status = self.get('/tuner%d/status' % n)
            m = re.search(r'lock=(\S+)', status)
            if m and m.group(1) != 'none':
                break
            time.sleep(0.4)
        else:
            raise RuntimeError('no lock on %d Hz (%s)' % (freq, status))
        # program filter makes the device emit a valid single-program TS with
        # generated PAT/PMT -- Plex will not accept a full multiplex.
        self.set('/tuner%d/program' % n, str(program))
        self.set('/tuner%d/target' % n, target)
        return status

    def release(self, n):
        for item, val in (('target', 'none'), ('channel', 'none')):
            try:
                self.set('/tuner%d/%s' % (n, item), val)
            except Exception:
                pass


class TunerPool:
    def __init__(self, count):
        self.free = list(range(count))
        self.cv = threading.Condition()
        self.count = count

    def acquire(self, timeout=5.0, min_free=0):
        """min_free: leave this many tuners spare (EPG must not block Plex)."""
        with self.cv:
            deadline = time.time() + timeout
            while len(self.free) <= min_free:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self.cv.wait(remaining)
            return self.free.pop(0)

    def release(self, n):
        with self.cv:
            if n not in self.free:
                self.free.append(n)
                self.free.sort()
            self.cv.notify()

    def in_use(self):
        with self.cv:
            return self.count - len(self.free)


# ------------------------------------------------------------------- lineup

_BAD_NAME = re.compile(r'^(\((encrypted|no data|control|internal)\)'
                       r'|[\W_]+)$', re.I)


def _expand_numbers(spec):
    """'1-49,60,83' -> {'1','2',...,'49','60','83'}"""
    out = set()
    for part in re.split(r'[,\s]+', spec.strip()):
        if not part:
            continue
        m = re.match(r'^(\d+)-(\d+)$', part)
        if m:
            out.update(str(n) for n in range(int(m.group(1)),
                                             int(m.group(2)) + 1))
        else:
            out.add(part)
    return out

# EN 300 468 table 87 service_type. Anything not listed is kept (unknown types
# are more often odd TV variants than junk).
RADIO_TYPES = {0x02, 0x07, 0x0A}                       # radio sound services
DATA_TYPES = {0x03, 0x06, 0x08, 0x0B, 0x0C, 0x0D,      # teletext, mosaic, data,
              0x0E, 0x0F, 0x10}                        # CI, RCS, MHP


def _compile(patterns):
    if not patterns:
        return None
    return re.compile('|'.join('(?:%s)' % p for p in patterns), re.I)


def load_lineup(path, services_path=None, allow_radio=False,
                allow_encrypted=False, exclude=None, include=None,
                numbers=None, no_shopping=False, only_visible=False,
                shop_threshold=0.5):
    """muxes.json (+ services.json when available) -> ordered channel list.

    The scan only flags encryption, and only when the tuner noticed. Once a
    guide grab has run we have the SDT, which authoritatively gives
    service_type (radio vs TV) and free_CA_mode (encrypted), keyed by
    (tsid, service_id) -- and in DVB the PAT program_number IS the service_id.
    """
    with open(path, encoding='utf-8') as f:
        muxes = json.load(f)

    svc = {}
    if services_path and os.path.exists(services_path):
        try:
            with open(services_path, encoding='utf-8') as f:
                for k, v in json.load(f).items():
                    if k.startswith('_'):
                        continue
                    onid, tsid, sid = (int(x) for x in k.split('.'))
                    svc[(tsid, sid)] = v
        except Exception as e:
            sys.stderr.write('services.json unreadable (%s), ignoring\n' % e)

    ex = _compile(exclude)
    inc = _compile(include)
    keep_nums = _expand_numbers(numbers) if numbers else None

    out, seen = [], set()
    dropped = {'encrypted': 0, 'radio': 0, 'data': 0, 'unnamed': 0, 'dup': 0,
               'not-a-service': 0, 'excluded': 0, 'shopping': 0, 'hidden': 0}
    for mux in muxes:
        tsid = mux.get('tsid')
        for p in mux.get('programs', []):
            # PAT program_number 0 points at the NIT, not a service: the
            # HDHomeRun scan reports it, but it is never a channel
            if not p.get('program'):
                dropped['not-a-service'] += 1
                continue
            name = epg.clean_name(p.get('name') or '')
            info = svc.get((tsid, p['program'])) if tsid is not None else None

            if info:
                stype = info.get('type')
                if info.get('ca') and not allow_encrypted:
                    dropped['encrypted'] += 1
                    continue
                if stype in RADIO_TYPES and not allow_radio:
                    dropped['radio'] += 1
                    continue
                if stype in DATA_TYPES:
                    dropped['data'] += 1
                    continue
                # EIT content nibble 0xA5 is "advertisement / shopping"; a
                # service whose schedule is mostly that is a shopping channel
                if (no_shopping and info.get('n', 0) >= 5
                        and info.get('shop', 0) >= shop_threshold):
                    dropped['shopping'] += 1
                    continue
                # NIT visible_service_flag: the network itself says this
                # service should not appear in a channel list
                if only_visible and info.get('vis', 1) == 0:
                    dropped['hidden'] += 1
                    continue
                name = info.get('name') or name       # SDT name beats scan name

            if not name or _BAD_NAME.match(name):
                dropped['unnamed'] += 1
                continue
            if '(encrypted)' in name.lower() and not allow_encrypted:
                dropped['encrypted'] += 1
                continue
            # the scan appends these to real names too, e.g. "BEM Service
            # (control)" -- they are carousel/service-information carriers
            low = name.lower()
            if any(t in low for t in ('(control)', '(no data)', '(internal)')):
                dropped['not-a-service'] += 1
                continue

            num = str(p.get('vchannel') or p.get('program'))
            if keep_nums is not None and num not in keep_nums:
                dropped['excluded'] += 1
                continue
            if inc is not None and not inc.search(name):
                dropped['excluded'] += 1
                continue
            if ex is not None and ex.search(name):
                dropped['excluded'] += 1
                continue
            if num in seen:
                dropped['dup'] += 1
                continue
            seen.add(num)
            out.append({'number': num, 'name': name, 'freq': mux['freq'],
                        'program': p['program'], 'tsid': tsid})
    out.sort(key=lambda c: (len(c['number']), c['number']))
    load_lineup.dropped = dropped
    load_lineup.have_sdt = bool(svc)
    return out


# ---------------------------------------------------------------- UDP relay

def local_ip_for(peer):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer, 65001))
        return s.getsockname()[0]
    finally:
        s.close()


def strip_rtp(pkt):
    """Legacy firmware sends raw TS; some send RTP. Detect and strip."""
    if pkt[:1] == b'\x47':
        return pkt
    if len(pkt) > 12 and pkt[12:13] == b'\x47' and (pkt[0] >> 6) == 2:
        return pkt[12:]
    return pkt


# ------------------------------------------------------------------- server

DEVICE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <URLBase>%(base)s</URLBase>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>%(name)s</friendlyName>
    <manufacturer>%(manufacturer)s</manufacturer>
    <manufacturerURL>%(manufacturer_url)s</manufacturerURL>
    <modelName>%(model)s</modelName>
    <modelNumber>%(model)s</modelNumber>
    <serialNumber></serialNumber>
    <UDN>uuid:%(uuid)s</UDN>
  </device>
</root>
"""


class Proxy:
    def __init__(self, args):
        self.args = args
        self.binary = find_binary(args.binary)
        self.device = Device(args.device, self.binary, args.channelmap,
                             args.modulation)
        self.device_ip = args.device_ip or self.device.ip()
        self.local_ip = args.advertise or local_ip_for(self.device_ip)
        self.ensure_muxes()
        base = os.path.dirname(os.path.abspath(args.muxes))
        self.services_path = os.path.join(base, 'services.json')
        self.cache_path = os.path.join(base, 'epg-cache.json')
        self.lineup = []
        self.by_number = {}
        self.reload_lineup()
        count = args.tuners or self.device.tuner_count()
        self.pool = TunerPool(count)
        if args.device_id:
            self.device_id = args.device_id.upper()
            did = int(self.device_id, 16)
            if not valid_device_id(did):
                print('warning: device id %s fails the HDHomeRun checksum; '
                      'clients may ignore it' % self.device_id)
            if legacy_device_id(did):
                print('warning: device id %s is in a legacy range; Plex will '
                      'treat the proxy as a legacy tuner' % self.device_id)
        else:
            # Derived, never reused: the real id's prefix marks it legacy.
            try:
                seed = int(args.device, 16)
            except ValueError:
                seed = 0x1234
            self.device_id = '%08X' % make_device_id(seed)
        self.base = 'http://%s:%d' % (self.local_ip, args.port)
        print('device %s at %s, %d tuner(s)' % (args.device, self.device_ip, count))
        print('advertising %s with %d channels' % (self.base, len(self.lineup)))

    def ensure_muxes(self):
        """First run (or --rescan): scan the cable/antenna band ourselves."""
        a = self.args
        if os.path.exists(a.muxes) and not a.rescan:
            return
        print('no %s yet -- running a channel scan.' % a.muxes)
        print('This takes 5-20 minutes and uses tuner 0. One time only.')
        hd = epg.HDHR(a.device, 0, self.binary)
        muxes = hd.scan(a.channelmap)
        if not muxes:
            raise SystemExit('scan found no multiplexes -- check the cable/antenna '
                             'and that --channelmap matches your signal source')
        with open(a.muxes, 'w', encoding='utf-8') as f:
            json.dump(muxes, f, indent=2, ensure_ascii=False)
        print('scan done: %d multiplexes -> %s' % (len(muxes), a.muxes))

    def reload_lineup(self):
        a = self.args
        lineup = load_lineup(a.muxes, self.services_path, a.allow_radio,
                             a.allow_encrypted, a.exclude, a.include,
                             a.channels, a.no_shopping, a.only_visible,
                             a.shopping_threshold)
        self.lineup = lineup
        self.by_number = {c['number']: c for c in lineup}
        d = load_lineup.dropped
        note = ', '.join('%d %s' % (v, k) for k, v in d.items() if v)
        print('lineup: %d channels%s%s'
              % (len(lineup), ' (dropped %s)' % note if note else '',
                 '' if load_lineup.have_sdt else ' [scan data only until the '
                 'first guide grab]'))
        return lineup

    def id_map(self):
        """Identity -> GuideNumber, so XMLTV channel ids match lineup.json.
        Keyed on (tsid, service_id) which is exact, with the slugged name as a
        fallback for anything the scan recorded without a tsid."""
        m = {}
        for c in self.lineup:
            if c.get('tsid') is not None:
                m[(c['tsid'], c['program'])] = c['number']
            m[epg._slug(c['name'])] = c['number']
        return m

    def discover(self):
        return {
            'FriendlyName': self.args.name,
            'Manufacturer': self.args.manufacturer,
            'ManufacturerURL': self.args.manufacturer_url,
            'ModelNumber': self.args.model,
            'FirmwareName': self.args.firmware_name,
            'FirmwareVersion': self.args.firmware_version,
            'DeviceID': self.device_id,
            'DeviceAuth': 'hdhrproxy',
            'TunerCount': self.pool.count,
            'BaseURL': self.base,
            'LineupURL': self.base + '/lineup.json',
        }

    def lineup_json(self):
        return [{'GuideNumber': c['number'], 'GuideName': c['name'],
                 'HD': 1, 'URL': '%s/auto/v%s' % (self.base, c['number'])}
                for c in self.lineup]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    proxy = None

    def log_message(self, fmt, *a):
        if not self.server.quiet:
            sys.stderr.write('%s %s\n' % (self.address_string(), fmt % a))

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, body, ctype='text/plain', code=200):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = lambda self: self.do_GET()

    def do_GET(self):
        p = self.proxy
        u = urlparse(self.path)
        path = u.path.rstrip('/') or '/'

        if path in ('/', '/discover.json'):
            return self._json(p.discover())

        if path == '/lineup_status.json':
            return self._json({'ScanInProgress': 0, 'ScanPossible': 0,
                               'Source': p.args.source,
                               'SourceList': [p.args.source]})

        if path == '/lineup.json':
            return self._json(p.lineup_json())

        if path == '/lineup.post':
            return self._json({'ScanInProgress': 0})

        if path == '/device.xml':
            return self._text(DEVICE_XML % {
                'base': p.base, 'name': p.args.name, 'model': p.args.model,
                'manufacturer': escape(p.args.manufacturer),
                'manufacturer_url': escape(p.args.manufacturer_url),
                'uuid': '%s-0000-0000-0000-%012x' % (p.device_id, 0)},
                'application/xml')

        if path == '/epg.xml':
            if p.args.epg and os.path.isfile(p.args.epg):
                with open(p.args.epg, 'rb') as f:
                    return self._text(f.read(), 'application/xml')
            return self._text('guide not built yet, try again later', code=503)

        m = re.match(r'^/auto/v(.+)$', path)
        if m:
            q = parse_qs(u.query)
            if 'transcode' in q:
                sys.stderr.write(
                    'note: Plex asked for transcode=%s. This proxy cannot '
                    'transcode;\n  serving the original stream. Set the DVR '
                    'quality to "Original" in Plex,\n  or check --model.\n'
                    % q['transcode'][0])
            return self.stream(m.group(1))

        self._text('not found', code=404)

    # -- streaming --------------------------------------------------------
    def stream(self, number):
        p = self.proxy
        ch = p.by_number.get(number)
        if not ch:
            return self._text('unknown channel %s' % number, code=404)

        tuner = p.pool.acquire()
        if tuner is None:
            return self._text('all %d tuners busy' % p.pool.count, code=503)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        sock.bind(('', 0))
        sock.settimeout(1.0)
        port = sock.getsockname()[1]
        target = '%s:%d' % (p.local_ip, port)

        try:
            sys.stderr.write('tuner%d -> ch %s "%s" %d Hz prog %d -> %s\n'
                             % (tuner, number, ch['name'], ch['freq'],
                                ch['program'], target))
            p.device.tune(tuner, ch['freq'], ch['program'], target)
        except Exception as e:
            sock.close()
            p.device.release(tuner)
            p.pool.release(tuner)
            return self._text('tune failed: %s' % e, code=502)

        self.send_response(200)
        self.send_header('Content-Type', 'video/mp2t')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()

        buf = bytearray()
        t0 = time.time()
        last = t0
        report = t0
        sent = 0
        first = None
        try:
            while True:
                try:
                    data = sock.recv(UDP_DGRAM * 2)
                    last = time.time()
                    if first is None:
                        first = last
                        sys.stderr.write('tuner%d: first packet after %.1fs\n'
                                         % (tuner, first - t0))
                except socket.timeout:
                    if time.time() - last > STREAM_TIMEOUT:
                        sys.stderr.write(
                            'tuner%d: no UDP data for %.0fs, dropping. Check '
                            'the firewall on inbound UDP\n'
                            % (tuner, STREAM_TIMEOUT))
                        break
                    continue
                buf += strip_rtp(data)
                if len(buf) >= 16 * 1024:
                    self.wfile.write(bytes(buf))
                    sent += len(buf)
                    del buf[:]
                now = time.time()
                if now - report >= 10.0:
                    sys.stderr.write('tuner%d: %.1f MiB, %.1f Mbit/s\n'
                                     % (tuner, sent / 1048576.0,
                                        sent * 8.0 / (now - t0) / 1e6))
                    report = now
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except OSError:
            pass
        finally:
            sock.close()
            p.device.release(tuner)
            p.pool.release(tuner)
            sys.stderr.write('tuner%d released after %.1f MiB in %.0fs%s\n'
                             % (tuner, sent / 1048576.0, time.time() - t0,
                                '' if sent else
                                ' -- nothing was relayed to the client'))
        self.close_connection = True


# ------------------------------------------------------- background EPG grab

CACHE_VERSION = 1


def save_cache(proxy, g):
    """Persist everything parsed off the air, so the XML can be regenerated
    without touching the tuner again."""
    data = {
        'version': CACHE_VERSION,
        'saved': int(time.time()),
        'services': {'%d.%d.%d' % k: v for k, v in g.services.items()},
        'lcn': {'%d.%d.%d' % k: v for k, v in g.lcn.items()},
        'visible': {'%d.%d.%d' % k: v for k, v in g.visible.items()},
        'shop': {'%d.%d.%d' % k: v for k, v in g.shop.items()},
        'events': [dict(e, start=int(e['start'].timestamp()),
                        stop=int(e['stop'].timestamp()))
                   for e in g.events.values()],
    }
    tmp = proxy.cache_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, proxy.cache_path)
    return len(data['events'])


def save_services(proxy, g, partial=False):
    """service_type / free_CA_mode / visibility per service -- this is what
    lets the lineup drop radio, encrypted and shopping channels."""
    svc = {}
    for k, v in g.services.items():
        shop = g.shop.get(k, [0, 0])
        svc['%d.%d.%d' % k] = {
            'name': v['name'], 'type': v.get('type'), 'ca': v.get('ca', 0),
            'vis': g.visible.get(k, 1),
            'shop': round(shop[0] / shop[1], 3) if shop[1] else 0.0,
            'n': shop[1],
        }
    if not svc:
        return 0
    if partial:
        svc['_partial'] = True
    tmp = proxy.services_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(svc, f, indent=1, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, proxy.services_path)
    return len(svc)


def load_cache(proxy):
    """Rebuild a Guide from the cache. Returns None if unusable."""
    try:
        with open(proxy.cache_path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get('version') != CACHE_VERSION:
        return None
    g = epg.Guide()

    def key(s):
        return tuple(int(x) for x in s.split('.'))

    g.services = {key(k): v for k, v in data.get('services', {}).items()}
    g.lcn = {key(k): v for k, v in data.get('lcn', {}).items()}
    g.visible = {key(k): v for k, v in data.get('visible', {}).items()}
    g.shop = {key(k): v for k, v in data.get('shop', {}).items()}
    utc = dt.timezone.utc
    for e in data.get('events', []):
        e = dict(e, start=dt.datetime.fromtimestamp(e['start'], utc),
                 stop=dt.datetime.fromtimestamp(e['stop'], utc))
        g.events[(e['onid'], e['tsid'], e['sid'], e['eid'])] = e
    return g


def _write_guide(proxy, g, force=True):
    """Atomically (re)write the XMLTV file, the cache and services.json.

    All three are O(events), so on slow storage this is not free: throttled by
    --write-interval so the incremental safety net does not cost more than the
    tuner time it protects."""
    a = proxy.args
    now = time.time()
    if not force:
        last = getattr(_write_guide, 'last', 0.0)
        cost = getattr(_write_guide, 'cost', 0.0)
        interval = max(a.write_interval, cost * 8)
        if now - last < interval:
            return getattr(_write_guide, 'counts', (0, 0))
    tmp = a.epg + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        only = None if a.epg_all_services else {c['number'] for c in proxy.lineup}
        ch, pr = epg.write_xmltv(g, f, id_style='lcn', days=a.epg_days,
                                 default_lang=a.lang, id_map=proxy.id_map(),
                                 only_ids=only)
    os.replace(tmp, a.epg)
    # Keep the cache and services.json in step with the guide: written only at
    # the end, an interrupted grab loses every hour of tuner time it spent.
    try:
        save_cache(proxy, g)
        save_services(proxy, g)
    except OSError as e:
        sys.stderr.write('epg: could not write cache: %s\n' % e)
    used = getattr(epg.write_xmltv, 'ids_used', set())
    want = {c['number'] for c in proxy.lineup}
    _write_guide.missing = sorted(want - used, key=lambda n: (len(n), n))
    _write_guide.matched = len(want & used)
    _write_guide.cost = time.time() - now
    _write_guide.last = time.time()
    _write_guide.counts = (ch, pr)
    if _write_guide.cost > 2.0 and not getattr(_write_guide, 'warned', False):
        _write_guide.warned = True
        print('epg: writing the guide takes %.1fs on this storage; doing it '
              'every %ds instead of every mux'
              % (_write_guide.cost, max(a.write_interval,
                                        _write_guide.cost * 8)))
    return ch, pr


def grab_epg(proxy, tuner, stop):
    a = proxy.args
    hd = epg.HDHR(a.device, tuner, proxy.binary)
    g = epg.Guide()
    with open(a.muxes, encoding='utf-8') as f:
        muxes = json.load(f)

    # Order by how many services each mux carries. EIT is broadcast per mux,
    # so the busiest ones pay off first -- and with --epg-muxes N this picks
    # the N best rather than whichever happened to be scanned first.
    # Only multiplexes that actually carry a lineup channel are worth a dwell:
    # filtering the lineup without filtering the muxes spends tuner time on
    # services that will never reach the guide.
    wanted = {(c.get('tsid'), c['program']) for c in proxy.lineup}

    def weight(m):
        tsid = m.get('tsid')
        return sum(1 for p in m.get('programs', [])
                   if (p.get('name') or '').strip()
                   and (not wanted or (tsid, p.get('program')) in wanted))

    skipped = [m for m in muxes if weight(m) == 0]
    muxes = [m for m in muxes if weight(m) > 0]
    if skipped:
        print('epg: skipping %d mux(es) carrying no lineup channel (~%d min '
              'saved)' % (len(skipped), (len(skipped) * a.epg_dwell + 59) // 60))
    muxes.sort(key=weight, reverse=True)
    if a.epg_muxes:
        muxes = muxes[:a.epg_muxes]

    if not muxes:
        sys.stderr.write('epg: no mux in %s carries any named service\n'
                         % a.muxes)
        return False
    print('epg: %d mux(es) x %ds, roughly %d min; %s is rewritten after each one'
          % (len(muxes), a.epg_dwell,
             (len(muxes) * a.epg_dwell + 59) // 60, a.epg))

    tmpdir = tempfile.mkdtemp(prefix='hdhr3_epg.')
    try:
        for i, mux in enumerate(muxes, 1):
            if stop.is_set():
                break
            ts = os.path.join(tmpdir, 'm%d.ts' % mux['freq'])
            nsvc = sum(1 for p in mux.get('programs', [])
                       if (p.get('name') or '').strip())
            before = dict(g.stats)
            try:
                ok, info = hd.capture(mux['freq'], a.epg_dwell, ts,
                                      a.modulation, a.channelmap, a.epg_pids)
            except RuntimeError as e:
                print('epg: mux %d/%d %d Hz -- tune failed: %s'
                      % (i, len(muxes), mux['freq'], e))
                continue
            if not ok:
                print('epg: mux %d/%d %d Hz (%d services) -- %s'
                      % (i, len(muxes), mux['freq'], nsvc, info))
                continue
            epg.parse_file(ts, g, quiet=True)
            os.unlink(ts)
            secs = ' '.join('%s=%d' % (k, g.stats[k] - before[k])
                            for k in ('eit', 'sdt', 'nit'))
            # how much of THIS mux did we actually capture? A full EIT schedule
            # cycle on a busy mux can take minutes -- if this ratio is low the
            # dwell is too short, not the network's fault.
            on_mux = {(mux.get('tsid'), p['program'])
                      for p in mux.get('programs', [])
                      if (p.get('name') or '').strip()}
            with_ev = {(e['tsid'], e['sid']) for e in g.events.values()}
            hit = len(on_mux & with_ev)
            cover = ' [%d/%d services on this mux]' % (hit, len(on_mux))
            # write as we go: the guide is usable long before the run finishes,
            # and a restart or crash does not throw the work away
            if g.events:
                ch, pr = _write_guide(proxy, g, force=(i == len(muxes)))
                print('epg: mux %d/%d %d Hz -- %s, sections %s%s, '
                      '%d channels, %d programmes total'
                      % (i, len(muxes), mux['freq'], info, secs, cover, ch, pr))
                if len(on_mux) and hit < len(on_mux) * 0.6:
                    print('     only %d%% of this mux captured -- EIT cycles '
                          'slowly on busy muxes; try a longer --epg-dwell'
                          % (100 * hit // max(len(on_mux), 1)))
            else:
                print('epg: mux %d/%d %d Hz (%d services) -- %s, sections %s, '
                      'still no events'
                      % (i, len(muxes), mux['freq'], nsvc, info, secs))
                if g.stats['eit'] == 0 and i == 1:
                    print('     no EIT sections at all -- see "Guide is empty" '
                          'in the README; try --epg-pids none')
            if a.epg_stop_when and len(g.events) >= a.epg_stop_when:
                print('epg: event target reached, stopping early')
                break
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if not g.events:
        sys.stderr.write('epg: no events decoded -- try a longer --epg-dwell\n')
        return False

    # services.json is already current (written with each guide); reload the
    # lineup so the metadata filters apply, then re-render with the final ids
    if g.services:
        proxy.reload_lineup()
        _write_guide(proxy, g)
    missing = getattr(_write_guide, 'missing', [])
    print('epg: %d of %d lineup channels have guide data'
          % (getattr(_write_guide, 'matched', 0), len(proxy.lineup)))
    if missing:
        print('epg: no guide data for channel(s) %s%s'
              % (', '.join(missing[:20]),
                 ' ...' if len(missing) > 20 else ''))
    if os.path.exists(proxy.cache_path):
        print('epg: cached %d events -> %s (%.1f MB; delete guide.xml and '
              'restart to regenerate the XML without retuning)'
              % (len(g.events), proxy.cache_path,
                 os.path.getsize(proxy.cache_path) / 1048576.0))
    print('epg: done -> %s' % a.epg)
    return True


def rebuild_from_cache(proxy):
    """Regenerate guide.xml from the cache. No tuner involved."""
    g = load_cache(proxy)
    if g is None or not g.events:
        return False
    # services.json drives the lineup filters, so refresh it from the cache too
    if save_services(proxy, g):
        proxy.reload_lineup()
    ch, pr = _write_guide(proxy, g)
    age = (time.time() - os.path.getmtime(proxy.cache_path)) / 3600.0
    print('epg: rebuilt from cache (%.1fh old): %d channels, %d programmes -> %s'
          % (age, ch, pr, proxy.args.epg))
    return True


def bootstrap_services(proxy, stop):
    """Short capture of NIT/SDT only, so the lineup is filtered before anything
    is served. SDT-other describes the whole network, so one multiplex and a
    few seconds is enough -- unlike EIT, these tables cycle in about two
    seconds."""
    a = proxy.args
    if os.path.exists(proxy.services_path) and not a.rescan:
        try:
            with open(proxy.services_path, encoding='utf-8') as f:
                if not json.load(f).get('_partial'):
                    return False
        except (OSError, ValueError):
            pass
    try:
        with open(a.muxes, encoding='utf-8') as f:
            muxes = json.load(f)
    except (OSError, ValueError):
        return False
    muxes = [m for m in muxes
             if any((p.get('name') or '').strip() for p in m.get('programs', []))]
    if not muxes:
        return False
    muxes.sort(key=lambda m: len(m.get('programs', [])), reverse=True)

    n = proxy.pool.acquire(timeout=30)
    if n is None:
        return False
    print('bootstrap: reading service info (%ds) so the lineup is filtered '
          'before Plex sees it' % a.bootstrap_dwell)
    g = epg.Guide()
    hd = epg.HDHR(a.device, n, proxy.binary)
    tmpdir = tempfile.mkdtemp(prefix='hdhr3_sdt.')
    try:
        for mux in muxes[:a.bootstrap_muxes]:
            if stop.is_set():
                break
            ts = os.path.join(tmpdir, 'b%d.ts' % mux['freq'])
            try:
                ok, info = hd.capture(mux['freq'], a.bootstrap_dwell, ts,
                                      a.modulation, a.channelmap, '0x0010-0x0011')
            except RuntimeError as e:
                sys.stderr.write('bootstrap: %d Hz failed: %s\n' % (mux['freq'], e))
                continue
            before = len(g.services)
            if ok and os.path.exists(ts):
                epg.parse_file(ts, g, quiet=True)
                os.unlink(ts)
            described = {(t, sid) for _, t, sid in g.services}
            covered = sum(1 for c in proxy.lineup
                          if (c.get('tsid'), c['program']) in described)
            print('bootstrap: %d Hz -> %d services known, %d of %d lineup '
                  'channels described'
                  % (mux['freq'], len(g.services), covered, len(proxy.lineup)))
            if covered >= len(proxy.lineup):
                break          # nothing left to learn
            if before and len(g.services) == before:
                break          # this mux told us nothing new
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        proxy.device.release(n)
        proxy.pool.release(n)

    if not g.services:
        sys.stderr.write('bootstrap: no SDT captured; the lineup stays '
                         'unfiltered until the first EPG grab\n')
        return False
    save_services(proxy, g, partial=True)
    described = {(t, sid) for _, t, sid in g.services}
    missing = [c['name'] for c in proxy.lineup
               if (c.get('tsid'), c['program']) not in described]
    print('bootstrap: %d services described (refined by the first full grab)'
          % len(g.services))
    if missing:
        print('bootstrap: no service info yet for %d channel(s): %s%s'
              % (len(missing), ', '.join(missing[:8]),
                 ' ...' if len(missing) > 8 else ''))
        print('           they stay in the lineup unfiltered; raise '
              '--bootstrap-muxes to describe more')
    proxy.reload_lineup()
    return True


def epg_worker(proxy, stop):
    a = proxy.args
    spare = 1 if proxy.pool.count > 1 else 0
    while not stop.is_set():
        try:
            age = time.time() - os.path.getmtime(a.epg)
        except OSError:
            age = 1e12
        if age < a.epg_interval * 3600:
            stop.wait(min(900, a.epg_interval * 3600 - age + 5))
            continue
        n = proxy.pool.acquire(timeout=60, min_free=spare)
        if n is None:
            stop.wait(600)       # tuners busy with Plex, try again later
            continue
        print('epg: refreshing on tuner%d' % n)
        try:
            grab_epg(proxy, n, stop)
        except Exception as e:
            sys.stderr.write('epg: refresh failed: %s\n' % e)
        finally:
            proxy.device.release(n)
            proxy.pool.release(n)
        stop.wait(60)


# ------------------------------------------------------------------- doctor

def check(proxy):
    a = proxy.args
    print('hdhomerun_config : %s' % proxy.binary)
    print('device           : %s at %s' % (a.device, proxy.device_ip))
    for item in ('/sys/hwmodel', '/sys/version', '/sys/features'):
        try:
            print('%-17s: %s' % (item, proxy.device.get(item).replace(chr(10), ' | ')))
        except Exception as e:
            print('%-17s: %s' % (item, e))
    print('tuners           : %d' % proxy.pool.count)
    d = load_lineup.dropped
    print('lineup           : %d channels from %s' % (len(proxy.lineup), a.muxes))
    print('filtered out     : %s'
          % (', '.join('%d %s' % (v, k) for k, v in d.items() if v) or 'nothing'))
    print('sdt data         : %s'
          % ('yes' if load_lineup.have_sdt else
             'not yet -- radio/encrypted filtering is approximate until the '
             'first guide grab'))
    for c in proxy.lineup[:8]:
        print('  %-6s %-28s %d Hz prog %d'
              % (c['number'], c['name'][:28], c['freq'], c['program']))
    if len(proxy.lineup) > 8:
        print('  ... and %d more' % (len(proxy.lineup) - 8))
    print('advertise        : %s' % proxy.base)
    did = int(proxy.device_id, 16)
    print('proxy device id  : %s (checksum %s, %s)'
          % (proxy.device_id, 'ok' if valid_device_id(did) else 'BAD',
             'legacy!' if legacy_device_id(did) else 'modern'))
    print('guide file       : %s (%s)'
          % (a.epg, 'present' if os.path.exists(a.epg) else 'will be built'))
    print('guide URL        : %s/epg.xml   <- give Plex this, not the path'
          % proxy.base)
    print('service data     : %s'
          % ('%s (present)' % proxy.services_path
             if os.path.exists(proxy.services_path)
             else 'written by the first EPG grab'))
    print('epg cache        : %s'
          % ('%s (%.1f MB)' % (proxy.cache_path,
                               os.path.getsize(proxy.cache_path) / 1048576.0)
             if os.path.exists(proxy.cache_path)
             else 'none yet -- the first grab creates it'))
    if not proxy.lineup:
        print('\nPROBLEM: empty lineup. Re-run with --rescan, and check that '
              '--channelmap matches\n         your source (eu-cable for cable, '
              'eu-bcast for antenna).')
        return 1
    print('\nLooks usable. Start without --check, then add %s to Plex.' % proxy.base)
    return 0


# ----------------------------------------------- optional UDP auto-discovery

DISCOVER_REQ, DISCOVER_RPY = 0x0002, 0x0003
TAG_DEVICE_TYPE, TAG_DEVICE_ID = 0x01, 0x02
TAG_TUNER_COUNT = 0x10
TAG_LINEUP_URL, TAG_BASE_URL, TAG_DEVICE_AUTH_STR = 0x27, 0x2A, 0x2B
DEVICE_TYPE_TUNER, WILDCARD = 0x00000001, 0xFFFFFFFF

# libhdhomerun rejects device ids whose checksum does not come out to zero,
# and flags whole id ranges as "legacy" purely from the top 12 bits -- 0x122 is
# HDHR3-EU, so announcing the real device's id would get the proxy classified
# as the very legacy device we are working around. Both rules below are lifted
# from hdhomerun_discover.c.
_ID_LUT = (0xA, 0x5, 0xF, 0x6, 0x7, 0xC, 0x1, 0xB,
           0x9, 0x2, 0x8, 0xD, 0x4, 0x3, 0xE, 0x0)


def valid_device_id(did):
    c = 0
    for shift, table in ((28, 1), (24, 0), (20, 1), (16, 0),
                         (12, 1), (8, 0), (4, 1), (0, 0)):
        n = (did >> shift) & 0x0F
        c ^= _ID_LUT[n] if table else n
    return c == 0


def legacy_device_id(did):
    prefix = did >> 20
    if prefix == 0x100:
        return did < 0x10040000
    if prefix == 0x120:
        return did < 0x12030000
    return prefix in (0x101, 0x102, 0x103, 0x111, 0x121, 0x122)


def make_device_id(seed, prefix=0x104):
    """Build a checksum-valid, non-legacy device id derived from `seed`."""
    base = (prefix << 20) | ((seed & 0xFFFF) << 4)
    c = 0
    for shift, table in ((28, 1), (24, 0), (20, 1), (16, 0), (12, 1), (8, 0),
                         (4, 1)):
        n = (base >> shift) & 0x0F
        c ^= _ID_LUT[n] if table else n
    return base | (c & 0x0F)


def _tlv(tag, value):
    return bytes([tag, len(value)]) + value


def _frame(ptype, payload):
    body = struct.pack('>HH', ptype, len(payload)) + payload
    return body + struct.pack('<I', zlib.crc32(body) & 0xFFFFFFFF)


def _parse_request(pkt):
    """Return (device_type, device_id) filters from a discover request."""
    if len(pkt) < 8 or struct.unpack('>H', pkt[:2])[0] != DISCOVER_REQ:
        return None
    plen = struct.unpack('>H', pkt[2:4])[0]
    body = pkt[4:4 + plen]
    if len(body) != plen:
        return None
    if struct.unpack('<I', pkt[4 + plen:8 + plen])[0] != (
            zlib.crc32(pkt[:4 + plen]) & 0xFFFFFFFF):
        return None
    dtype, did = WILDCARD, WILDCARD
    i = 0
    while i + 2 <= len(body):
        tag, ln = body[i], body[i + 1]
        i += 2
        if ln & 0x80:                      # var-length: 7 bits + next byte
            ln = (ln & 0x7F) | (body[i] << 7)
            i += 1
        val = body[i:i + ln]
        i += ln
        if tag == TAG_DEVICE_TYPE and len(val) == 4:
            dtype = struct.unpack('>I', val)[0]
        elif tag == TAG_DEVICE_ID and len(val) == 4:
            did = struct.unpack('>I', val)[0]
    return dtype, did


def build_reply(proxy):
    did = int(proxy.device_id, 16)
    return _frame(DISCOVER_RPY,
                  _tlv(TAG_DEVICE_TYPE, struct.pack('>I', DEVICE_TYPE_TUNER)) +
                  _tlv(TAG_DEVICE_ID, struct.pack('>I', did)) +
                  _tlv(TAG_TUNER_COUNT, bytes([proxy.pool.count])) +
                  _tlv(TAG_DEVICE_AUTH_STR, b'hdhrproxy') +
                  _tlv(TAG_BASE_URL, proxy.base.encode()) +
                  _tlv(TAG_LINEUP_URL, (proxy.base + '/lineup.json').encode()))


def discovery_server(proxy, stop):
    """Answer HDHomeRun UDP discovery on 65001 so clients auto-detect us."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except OSError:
        pass
    try:
        s.bind(('', 65001))
    except OSError as e:
        sys.stderr.write('discovery disabled -- port 65001 in use (%s).\n'
                         '  Stop the local HDHomeRun service, or add the proxy '
                         'manually in Plex.\n' % e)
        return
    s.settimeout(1.0)
    mine = int(proxy.device_id, 16)
    reply = build_reply(proxy)
    print('discovery: answering on UDP 65001 as device %s' % proxy.device_id)
    while not stop.is_set():
        try:
            pkt, addr = s.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        req = _parse_request(pkt)
        if not req:
            continue
        dtype, did = req
        if dtype not in (WILDCARD, DEVICE_TYPE_TUNER):
            continue
        if did not in (WILDCARD, mine):
            continue
        try:
            s.sendto(reply, addr)
        except OSError:
            pass
    s.close()


# --------------------------------------------------------------------- main

def require_runtime():
    """Fail loudly on interpreters that cannot drive hdhomerun_config properly."""
    if sys.version_info < (3, 8):
        raise SystemExit(
            'Python %d.%d.%d is too old for this tool (need 3.8+).\n'
            '  Running: %s\n'
            '  On Windows "python3" is often an old or non-native build while '
            '"python" is current -- try:  py -3 %s'
            % (sys.version_info[0], sys.version_info[1], sys.version_info[2],
               sys.executable, os.path.basename(sys.argv[0])))
    if sys.platform in ('cygwin', 'msys'):
        sys.stderr.write(
            'warning: running under %s Python. These builds rewrite arguments '
            'that look like\n  absolute POSIX paths, so device variables such '
            'as /tuner0/status can reach\n  hdhomerun_config.exe mangled '
            '("unknown getset variable"). Use native Windows\n  Python '
            '(py -3) if commands fail.\n' % sys.platform)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-d', '--device', default='FFFFFFFF', help='HDHomeRun device id')
    ap.add_argument('--device-ip', default=None, help='skip discovery, use this IP')
    ap.add_argument('-m', '--muxes', default='muxes.json')
    ap.add_argument('--data-dir', default=None,
                    help='where muxes.json and the guide live (default: cwd)')
    ap.add_argument('--epg', default='guide.xml',
                    help='XMLTV file, refreshed in the background, served at /epg.xml')
    ap.add_argument('--no-epg', action='store_true', help='do not grab a guide')
    ap.add_argument('--epg-interval', type=float, default=12.0, help='hours')
    ap.add_argument('--epg-dwell', type=int, default=90, help='seconds per mux')
    ap.add_argument('--epg-muxes', type=int, default=0,
                    help='only grab from the first N muxes (0 = all)')
    ap.add_argument('--epg-stop-when', type=int, default=0,
                    help='stop grabbing once N events collected')
    ap.add_argument('--epg-days', type=int, default=None)
    ap.add_argument('--write-interval', type=int, default=180, metavar='SEC',
                    help='minimum seconds between incremental guide/cache '
                         'writes during a grab')
    ap.add_argument('--epg-all-services', action='store_true',
                    help='keep guide entries for services that are not in the '
                         'lineup (radio, encrypted); they bloat the file and '
                         'no client can match them')
    ap.add_argument('--epg-pids', default='0x0010-0x0012',
                    help='hardware PID filter for guide capture; "none" '
                         'captures the whole multiplex (much bigger)')
    ap.add_argument('--lang', default='de')
    ap.add_argument('--no-shopping', action='store_true',
                    help='drop channels whose EIT marks most airings as '
                         'advertisement/shopping (content nibble 0xA5)')
    ap.add_argument('--shopping-threshold', type=float, default=0.5,
                    metavar='FRAC', help='share of airings that must be '
                                         'shopping to drop the channel')
    ap.add_argument('--only-visible', action='store_true',
                    help='drop services the NIT marks not visible')
    ap.add_argument('--exclude', action='append', metavar='REGEX',
                    help='drop channels whose name matches (repeatable, '
                         'case-insensitive)')
    ap.add_argument('--include', action='append', metavar='REGEX',
                    help='keep only channels whose name matches (repeatable)')
    ap.add_argument('--channels', metavar='LIST',
                    help='keep only these channel numbers, e.g. "1-49,60,83"')
    ap.add_argument('--allow-radio', action='store_true',
                    help='keep radio services in the lineup')
    ap.add_argument('--allow-encrypted', action='store_true',
                    help='keep encrypted services (they will not play)')
    ap.add_argument('--no-bootstrap', action='store_true',
                    help='skip the short SDT read at startup')
    ap.add_argument('--bootstrap-dwell', type=int, default=35,
                    help='seconds per mux for the SDT read')
    ap.add_argument('--bootstrap-muxes', type=int, default=3,
                    help='how many muxes the SDT read may try')
    ap.add_argument('--regrab', action='store_true',
                    help='ignore the EPG cache and capture from the air again')
    ap.add_argument('--rescan', action='store_true', help='redo the channel scan')
    ap.add_argument('--check', action='store_true',
                    help='print a diagnostic report and exit')
    ap.add_argument('-p', '--port', type=int, default=5004)
    ap.add_argument('--bind', default='0.0.0.0')
    ap.add_argument('--advertise', default=None,
                    help='IP to put in BaseURL (default: auto)')
    ap.add_argument('--channelmap', default=None, help='eu-cable | eu-bcast')
    ap.add_argument('--modulation', default='auto')
    ap.add_argument('--tuners', type=int, default=0, help='0 = probe device')
    ap.add_argument('--source', default='Cable', choices=['Cable', 'Antenna'])
    ap.add_argument('--name', default='HDHR3 Proxy')
    ap.add_argument('--model', default='HDHR4-2US',
                    help='HDHR4-2US (CONNECT, no transcoder) is the safe '
                         'choice; HDTC-2US makes Plex request ?transcode=')
    ap.add_argument('--manufacturer', default='King-Sabo',
                    help='reported in discover.json; if Plex ever stops '
                         'recognising the proxy, try --manufacturer Silicondust')
    ap.add_argument('--manufacturer-url',
                    default='https://github.com/King-Sabo/hdhr3-proxy')
    ap.add_argument('--firmware-name', default='hdhomerun4_dvbc')
    ap.add_argument('--firmware-version', default='20150826')
    ap.add_argument('--device-id', default=None)
    ap.add_argument('--no-discovery', action='store_true',
                    help='do not answer UDP discovery on 65001')
    ap.add_argument('--binary', default=None)
    ap.add_argument('-q', '--quiet', action='store_true')
    require_runtime()
    a = ap.parse_args()

    if a.data_dir:
        os.makedirs(a.data_dir, exist_ok=True)
        for attr in ('muxes', 'epg'):
            v = getattr(a, attr)
            if v and not os.path.isabs(v):
                setattr(a, attr, os.path.join(a.data_dir, v))

    proxy = Proxy(a)
    Handler.proxy = proxy
    if a.check:
        return check(proxy)

    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.daemon_threads = True
    srv.quiet = a.quiet

    stop = threading.Event()
    if not a.no_epg and not a.no_bootstrap:
        try:
            bootstrap_services(proxy, stop)
        except Exception as e:
            sys.stderr.write('bootstrap failed (%s); continuing\n' % e)
    if not a.no_epg:
        # A missing guide with a usable cache means "re-render", not "retune".
        if not os.path.exists(a.epg) and not a.regrab:
            try:
                rebuild_from_cache(proxy)
            except Exception as e:
                sys.stderr.write('epg: cache rebuild failed (%s)\n' % e)
        threading.Thread(target=epg_worker, args=(proxy, stop), daemon=True).start()
    if not a.no_discovery:
        threading.Thread(target=discovery_server, args=(proxy, stop),
                         daemon=True).start()

    print('')
    print('  add to Plex as : %s' % proxy.base)
    if not a.no_epg:
        print('  XMLTV URL      : %s/epg.xml' % proxy.base)
    print('  Ctrl-C to stop')
    print('')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for n in range(proxy.pool.count):
            proxy.device.release(n)
    return 0


if __name__ == '__main__':
    sys.exit(main())
