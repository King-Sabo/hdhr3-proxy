#!/usr/bin/env python3
"""
hdhr3_epg.py -- build an XMLTV guide from the OTA/cable DVB EIT using an
HDHomeRun (tested target: HDHR3-EU, DVB-T "eu-bcast" / DVB-C "eu-cable").

The HDHR3 has no guide service, so we do it the honest way:
  set channel -> wait lock -> hardware PID filter 0x0010/0x0011/0x0012
  -> capture N seconds of TS -> parse NIT/SDT/EIT -> emit XMLTV.

EIT "schedule other" (table_id 0x60-0x6F) is parsed too, so on most EU
cable/DVB-T networks a single mux already yields the whole network's guide.

Requires: python3 (stdlib only) + hdhomerun_config in PATH.

Usage:
  ./hdhr3_epg.py scan   --device FFFFFFFF --channelmap eu-cable -o muxes.json
  ./hdhr3_epg.py grab   --device FFFFFFFF --muxes muxes.json --dwell 60 -o guide.xml
  ./hdhr3_epg.py grab   --device FFFFFFFF --freq 474000000 --dwell 120 -o guide.xml
  ./hdhr3_epg.py parse  capture.ts -o guide.xml
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
from xml.sax.saxutils import escape as _escape, quoteattr as _quoteattr

EIT_PID = 0x0012
SDT_PID = 0x0011
NIT_PID = 0x0010
PID_FILTER = "0x0010-0x0012"

# ---------------------------------------------------------------- CRC-32/MPEG2

_CRC_TAB = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8):
        _c = ((_c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if _c & 0x80000000 else (_c << 1) & 0xFFFFFFFF
    _CRC_TAB.append(_c)


def crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TAB[((crc >> 24) ^ b) & 0xFF]
    return crc


# ------------------------------------------------------------ DVB text decoding

_ISO6937_SINGLE = {
    0xA1: '\u00a1', 0xA2: '\u00a2', 0xA3: '\u00a3', 0xA4: '$', 0xA5: '\u00a5',
    0xA7: '\u00a7', 0xA8: '\u00a4', 0xA9: '\u2018', 0xAA: '\u201c', 0xAB: '\u00ab',
    0xAC: '\u2190', 0xAD: '\u2191', 0xAE: '\u2192', 0xAF: '\u2193',
    0xB0: '\u00b0', 0xB1: '\u00b1', 0xB2: '\u00b2', 0xB3: '\u00b3', 0xB4: '\u00d7',
    0xB5: '\u00b5', 0xB6: '\u00b6', 0xB7: '\u00b7', 0xB8: '\u00f7', 0xB9: '\u2019',
    0xBA: '\u201d', 0xBB: '\u00bb', 0xBC: '\u00bc', 0xBD: '\u00bd', 0xBE: '\u00be',
    0xBF: '\u00bf',
    0xD0: '\u2015', 0xD1: '\u00b9', 0xD2: '\u00ae', 0xD3: '\u00a9', 0xD4: '\u2122',
    0xD5: '\u266a', 0xD6: '\u00ac', 0xD7: '\u00a6', 0xDC: '\u215b', 0xDD: '\u215c',
    0xDE: '\u215d', 0xDF: '\u215e',
    0xE0: '\u03a9', 0xE1: '\u00c6', 0xE2: '\u0110', 0xE3: '\u00aa', 0xE4: '\u0126',
    0xE6: '\u0132', 0xE7: '\u013f', 0xE8: '\u0141', 0xE9: '\u00d8', 0xEA: '\u0152',
    0xEB: '\u00ba', 0xEC: '\u00de', 0xED: '\u0166', 0xEE: '\u014a', 0xEF: '\u0149',
    0xF0: '\u0138', 0xF1: '\u00e6', 0xF2: '\u0111', 0xF3: '\u00f0', 0xF4: '\u0127',
    0xF5: '\u0131', 0xF6: '\u0133', 0xF7: '\u0140', 0xF8: '\u0142', 0xF9: '\u00f8',
    0xFA: '\u0153', 0xFB: '\u00df', 0xFC: '\u00fe', 0xFD: '\u0167', 0xFE: '\u014b',
    0xFF: '\u00ad',
}
_ISO6937_DIACRITIC = {
    0xC1: '\u0300', 0xC2: '\u0301', 0xC3: '\u0302', 0xC4: '\u0303', 0xC5: '\u0304',
    0xC6: '\u0306', 0xC7: '\u0307', 0xC8: '\u0308', 0xCA: '\u030a', 0xCB: '\u0327',
    0xCD: '\u030b', 0xCE: '\u0328', 0xCF: '\u030c',
}


def _iso6937(data: bytes) -> str:
    out = []
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        i += 1
        if b < 0x80:
            out.append(chr(b))
        elif b in _ISO6937_DIACRITIC:
            if i < n:
                base = chr(data[i])
                i += 1
                out.append(unicodedata.normalize('NFC', base + _ISO6937_DIACRITIC[b]))
        elif b in _ISO6937_SINGLE:
            out.append(_ISO6937_SINGLE[b])
        # else: undefined codepoint -> drop
    return ''.join(out)


_CHARSET = {
    0x01: 'iso8859-5', 0x02: 'iso8859-6', 0x03: 'iso8859-7', 0x04: 'iso8859-8',
    0x05: 'iso8859-9', 0x06: 'iso8859-10', 0x07: 'iso8859-11', 0x09: 'iso8859-13',
    0x0A: 'iso8859-14', 0x0B: 'iso8859-15', 0x11: 'utf-16-be', 0x12: 'euc-kr',
    0x13: 'gb2312', 0x14: 'big5', 0x15: 'utf-8',
}


# XML 1.0 permits only tab, LF, CR and >= 0x20 (plus the usual upper ranges).
# DVB text can legitimately carry other control bytes; emitting them produces a
# file that parsers reject at that byte, silently discarding everything after.
_XML_ILLEGAL = re.compile(
    '[^\u0009\u000A\u000D\u0020-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]')


def clean_name(s: str) -> str:
    """Scrub a service name that did not come through dvb_text -- the
    HDHomeRun scan reports names with DVB control bytes still in them
    (0x86/0x87 are emphasis on/off), which would otherwise reach lineup.json
    and the guide."""
    if not s:
        return ''
    out = ''.join(c for c in s
                  if not (ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F))
    return ' '.join(out.split())


def xml_safe(s: str) -> str:
    return _XML_ILLEGAL.sub('', s).replace('\ufffd', '')


def dvb_text(data: bytes) -> str:
    """Decode an EN 300 468 annex A string."""
    if not data:
        return ''
    enc = None
    first = data[0]
    if first == 0x10 and len(data) >= 3:
        n = (data[1] << 8) | data[2]
        enc = 'iso8859-%d' % n if 1 <= n <= 15 and n != 12 else None
        data = data[3:]
    elif first == 0x1F:
        # encoding_type_id (UK Freeview Huffman etc.) -- not supported
        return ''
    elif first in _CHARSET:
        enc = _CHARSET[first]
        data = data[1:]
    elif first < 0x20:
        data = data[1:]  # reserved selector, fall back to ISO 6937

    if enc == 'utf-16-be':
        try:
            s = data.decode('utf-16-be', 'strict')
        except (UnicodeDecodeError, LookupError):
            s = data.decode('utf-16-be', 'ignore')
    elif enc:
        try:
            s = data.decode(enc, 'strict')
        except LookupError:
            s = _iso6937(data)
        except UnicodeDecodeError:
            # the selector lied about the charset: ISO 6937 is the DVB default
            # and decodes anything, which beats peppering titles with U+FFFD
            s = _iso6937(data)
    else:
        s = _iso6937(data)

    # control codes: 0x8A = line break, 0x80-0x9F otherwise stripped
    out = []
    for ch in s:
        o = ord(ch)
        if o == 0x8A:
            out.append('\n')
        elif 0x80 <= o <= 0x9F or o in (0x00, 0x0D):
            continue
        else:
            out.append(ch)
    return xml_safe(''.join(out)).strip()


# --------------------------------------------------------------- TS / sections

class SectionAssembler:
    """Reassembles PSI/SI sections from TS payloads of one PID."""

    def __init__(self, sink):
        self.sink = sink
        self.buf = bytearray()
        self.want = 0

    def _drain(self):
        while True:
            if not self.want:
                if len(self.buf) < 3:
                    return
                if self.buf[0] == 0xFF:
                    self.buf.clear()
                    return
                self.want = 3 + (((self.buf[1] & 0x0F) << 8) | self.buf[2])
            if len(self.buf) < self.want:
                return
            sect = bytes(self.buf[:self.want])
            del self.buf[:self.want]
            self.want = 0
            if len(sect) >= 12 and crc32_mpeg(sect) == 0:
                self.sink(sect)
            if not self.buf or self.buf[0] == 0xFF:
                self.buf.clear()
                return

    def feed(self, pusi: bool, payload: bytes):
        if pusi:
            if not payload:
                return
            ptr = payload[0]
            body = payload[1:]
            if self.buf and ptr:
                self.buf += body[:ptr]
                self._drain()
            self.buf.clear()
            self.want = 0
            self.buf += body[ptr:]
        else:
            if not self.buf:
                return
            self.buf += payload
        self._drain()


def demux(stream, handlers, progress=None):
    """Feed a byte stream of 188-byte TS packets into {pid: SectionAssembler}."""
    buf = b''
    total = 0
    while True:
        chunk = stream.read(1 << 20)
        if not chunk:
            break
        buf += chunk
        total += len(chunk)
        if progress:
            progress(total)
        # resync
        start = 0
        end = len(buf) - 188
        while start <= end:
            if buf[start] != 0x47:
                start += 1
                continue
            pkt = buf[start:start + 188]
            start += 188
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            h = handlers.get(pid)
            if h is None:
                continue
            if pkt[3] & 0xC0:  # scrambled
                continue
            afc = (pkt[3] >> 4) & 0x03
            off = 4
            if afc in (2, 3):
                off += 1 + pkt[4]
                if afc == 2 or off >= 188:
                    continue
            h.feed(bool(pkt[1] & 0x40), pkt[off:])
        buf = buf[start:]


# ------------------------------------------------------------------- SI parsing

def iter_descriptors(buf: bytes):
    i = 0
    n = len(buf)
    while i + 2 <= n:
        tag = buf[i]
        ln = buf[i + 1]
        if i + 2 + ln > n:
            return
        yield tag, buf[i + 2:i + 2 + ln]
        i += 2 + ln


def escape(s):
    return _escape(xml_safe(s))


def quoteattr(s):
    return _quoteattr(xml_safe(s))


_MJD_EPOCH = dt.datetime(1858, 11, 17, tzinfo=dt.timezone.utc)


def _bcd(b: int) -> int:
    return (b >> 4) * 10 + (b & 0x0F)


def dvb_time(b: bytes):
    mjd = (b[0] << 8) | b[1]
    if mjd == 0xFFFF:
        return None
    try:
        return _MJD_EPOCH + dt.timedelta(days=mjd, hours=_bcd(b[2]),
                                         minutes=_bcd(b[3]), seconds=_bcd(b[4]))
    except ValueError:
        return None


def dvb_duration(b: bytes) -> int:
    return _bcd(b[0]) * 3600 + _bcd(b[1]) * 60 + _bcd(b[2])


CONTENT_L1 = {
    0x1: 'Movie / Drama', 0x2: 'News / Current affairs', 0x3: 'Show / Game show',
    0x4: 'Sports', 0x5: "Children's / Youth", 0x6: 'Music / Ballet / Dance',
    0x7: 'Arts / Culture', 0x8: 'Social / Political / Economics',
    0x9: 'Education / Science / Factual', 0xA: 'Leisure / Hobbies',
}


class Guide:
    def __init__(self, lang_pref=None):
        self.events = {}      # (onid,tsid,sid,event_id) -> dict
        self.services = {}    # (onid,tsid,sid) -> {'name','provider','type'}
        self.lcn = {}         # (onid,tsid,sid) -> int
        self.visible = {}     # (onid,tsid,sid) -> NIT visible_service_flag
        self.shop = {}        # (onid,tsid,sid) -> [shopping events, total]
        self.lang_pref = lang_pref
        self.stats = {'eit': 0, 'sdt': 0, 'nit': 0}

    # -- SDT ---------------------------------------------------------------
    def sdt(self, s: bytes):
        if s[0] not in (0x42, 0x46):
            return
        tsid = (s[3] << 8) | s[4]
        onid = (s[8] << 8) | s[9]
        i = 11
        end = 3 + (((s[1] & 0x0F) << 8) | s[2]) - 4
        while i + 5 <= end:
            sid = (s[i] << 8) | s[i + 1]
            ca = (s[i + 3] >> 4) & 0x01
            dlen = ((s[i + 3] & 0x0F) << 8) | s[i + 4]
            desc = s[i + 5:i + 5 + dlen]
            i += 5 + dlen
            for tag, d in iter_descriptors(desc):
                if tag != 0x48 or len(d) < 3:
                    continue
                stype = d[0]
                pl = d[1]
                provider = dvb_text(d[2:2 + pl])
                if 2 + pl < len(d):
                    nl = d[2 + pl]
                    name = dvb_text(d[3 + pl:3 + pl + nl])
                else:
                    name = ''
                if name:
                    self.services[(onid, tsid, sid)] = {
                        'name': name, 'provider': provider, 'type': stype,
                        'ca': ca}
        self.stats['sdt'] += 1

    # -- NIT (logical channel numbers) -------------------------------------
    def nit(self, s: bytes):
        if s[0] not in (0x40, 0x41):
            return
        ndl = ((s[8] & 0x0F) << 8) | s[9]
        i = 10 + ndl
        if i + 2 > len(s):
            return
        tsl = ((s[i] & 0x0F) << 8) | s[i + 1]
        i += 2
        end = min(i + tsl, len(s) - 4)
        while i + 6 <= end:
            tsid = (s[i] << 8) | s[i + 1]
            onid = (s[i + 2] << 8) | s[i + 3]
            dlen = ((s[i + 4] & 0x0F) << 8) | s[i + 5]
            desc = s[i + 6:i + 6 + dlen]
            i += 6 + dlen
            for tag, d in iter_descriptors(desc):
                if tag not in (0x83, 0x87):  # LCN / HD simulcast LCN
                    continue
                off = 0
                if tag == 0x87:
                    off = 0
                for j in range(off, len(d) - 3, 4):
                    sid = (d[j] << 8) | d[j + 1]
                    # visible_service_flag(1) reserved(5) LCN(10)
                    visible = (d[j + 2] >> 7) & 0x01
                    num = ((d[j + 2] & 0x03) << 8) | d[j + 3]
                    key = (onid, tsid, sid)
                    if num and key not in self.lcn:
                        self.lcn[key] = num
                    if key not in self.visible:
                        self.visible[key] = visible
        self.stats['nit'] += 1

    # -- EIT ---------------------------------------------------------------
    def eit(self, s: bytes):
        tid = s[0]
        if not (tid in (0x4E, 0x4F) or 0x50 <= tid <= 0x6F):
            return
        sid = (s[3] << 8) | s[4]
        version = (s[5] >> 1) & 0x1F
        tsid = (s[8] << 8) | s[9]
        onid = (s[10] << 8) | s[11]
        i = 14
        end = 3 + (((s[1] & 0x0F) << 8) | s[2]) - 4
        while i + 12 <= end:
            eid = (s[i] << 8) | s[i + 1]
            start = dvb_time(s[i + 2:i + 7])
            dur = dvb_duration(s[i + 7:i + 10])
            dlen = ((s[i + 10] & 0x0F) << 8) | s[i + 11]
            desc = s[i + 12:i + 12 + dlen]
            i += 12 + dlen
            if start is None or dur <= 0:
                continue
            key = (onid, tsid, sid, eid)
            old = self.events.get(key)
            if old is not None and old['version'] == version:
                continue
            ev = self._event(desc)
            ev.update(onid=onid, tsid=tsid, sid=sid, eid=eid, version=version,
                      start=start, stop=start + dt.timedelta(seconds=dur))
            self.events[key] = ev
            tally = self.shop.setdefault((onid, tsid, sid), [0, 0])
            tally[0] += 1 if ev.get('shopping') else 0
            tally[1] += 1
        self.stats['eit'] += 1

    @staticmethod
    def _event(desc: bytes) -> dict:
        ev = {'title': '', 'sub_title': '', 'desc': '', 'lang': '',
              'categories': [], 'rating': None, 'items': [], 'video': None,
              'shopping': False}
        ext = {}
        for tag, d in iter_descriptors(desc):
            if tag == 0x4D and len(d) >= 5:                 # short_event
                lang = d[0:3].decode('ascii', 'ignore').strip()
                nl = d[3]
                name = dvb_text(d[4:4 + nl])
                if 4 + nl < len(d):
                    tl = d[4 + nl]
                    text = dvb_text(d[5 + nl:5 + nl + tl])
                else:
                    text = ''
                if not ev['title']:
                    ev['title'], ev['sub_title'], ev['lang'] = name, text, lang
            elif tag == 0x4E and len(d) >= 5:               # extended_event
                num = d[0] >> 4
                lang = d[1:4].decode('ascii', 'ignore').strip()
                ilen = d[4]
                j = 5
                stop = 5 + ilen
                items = []
                while j + 2 <= stop and j + 2 <= len(d):
                    dl = d[j]
                    k = dvb_text(d[j + 1:j + 1 + dl])
                    j += 1 + dl
                    if j >= len(d):
                        break
                    vl = d[j]
                    v = dvb_text(d[j + 1:j + 1 + vl])
                    j += 1 + vl
                    if k or v:
                        items.append((k, v))
                text = ''
                if stop < len(d):
                    tl = d[stop]
                    text = dvb_text(d[stop + 1:stop + 1 + tl])
                ext.setdefault(lang, {})[num] = text
                ev['items'].extend(items)
            elif tag == 0x54:                               # content
                for j in range(0, len(d) - 1, 2):
                    n1, n2 = d[j] >> 4, d[j] & 0x0F
                    if (n1, n2) == (0xA, 0x5):   # advertisement / shopping
                        ev['shopping'] = True
                        name = 'Shopping'
                    else:
                        name = CONTENT_L1.get(n1)
                    if name and name not in ev['categories']:
                        ev['categories'].append(name)
            elif tag == 0x55:                               # parental_rating
                for j in range(0, len(d) - 3, 4):
                    r = d[j + 3]
                    if 0x01 <= r <= 0x0F:
                        ev['rating'] = r + 3
                        break
            elif tag == 0x50 and len(d) >= 4:               # component
                if (d[0] & 0x0F) == 0x01 and d[1] in (0x09, 0x0A, 0x0B,
                                                      0x0D, 0x0E, 0x0F, 0x10):
                    ev['video'] = 'HDTV'
        if ext:
            lang = ev['lang'] if ev['lang'] in ext else next(iter(ext))
            ev['desc'] = ''.join(ext[lang][n] for n in sorted(ext[lang]))
        if not ev['desc']:
            ev['desc'] = ev['sub_title']
            ev['sub_title'] = ''
        _episode_numbers(ev)
        return ev


# --------------------------------------------------------- episode numbering

# German and English forms broadcasters use in extended_event items and in
# free text: "Folge 12", "Staffel 3", "Episode 4", "S3 F12", "(5/13)".
_SEASON_KEY = re.compile(r'^(staffel|season|serie)', re.I)
_EPISODE_KEY = re.compile(r'^(folge|episode|epis|nummer|nr)', re.I)
_INT = re.compile(r'(\d{1,4})')
_SXXEYY = re.compile(r'\bS(?:taffel)?\s*(\d{1,3})\s*[,/ ]?\s*'
                     r'(?:F(?:olge)?|E(?:pisode)?)\s*(\d{1,4})\b', re.I)
_FOLGE = re.compile(r'\b(?:Folge|Episode|Teil)\s*(\d{1,4})\b', re.I)
_OF = re.compile(r'\((\d{1,4})\s*/\s*(\d{1,4})\)')


def _episode_numbers(ev):
    """Fill ev['season'] / ev['episode'] / ev['total'] from whatever the
    broadcaster provided. DVB has no dedicated field, so it lives in the
    extended_event items or in the text."""
    season = episode = total = None
    for k, v in ev.get('items', []):
        if season is None and _SEASON_KEY.match(k.strip()):
            m = _INT.search(v)
            if m:
                season = int(m.group(1))
        if episode is None and _EPISODE_KEY.match(k.strip()):
            m = _INT.search(v)
            if m:
                episode = int(m.group(1))

    blob = ' '.join(x for x in (ev.get('sub_title', ''), ev.get('desc', ''))
                    if x)[:400]
    if season is None or episode is None:
        m = _SXXEYY.search(blob)
        if m:
            season = season if season is not None else int(m.group(1))
            episode = episode if episode is not None else int(m.group(2))
    if episode is None:
        m = _FOLGE.search(blob)
        if m:
            episode = int(m.group(1))
    if episode is None:
        m = _OF.search(blob)
        if m:
            episode, total = int(m.group(1)), int(m.group(2))

    ev['season'] = season
    ev['episode'] = episode
    ev['total'] = total


def xmltv_ns(season, episode, total=None):
    """XMLTV's zero-based 'season.episode/total.part' form."""
    if episode is None and season is None:
        return None
    a = '' if season is None else str(season - 1) if season > 0 else '0'
    if episode is None:
        b = ''
    elif total:
        b = '%d/%d' % (max(episode - 1, 0), total)
    else:
        b = str(max(episode - 1, 0))
    return '%s.%s.' % (a, b)


# ------------------------------------------------------------------- XMLTV out

def _slug(s: str) -> str:
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r'[^A-Za-z0-9]+', '-', s).strip('-').lower()
    return s or 'unknown'


def channel_id(g: Guide, key, style: str, id_map=None) -> str:
    onid, tsid, sid = key
    if id_map:
        # exact first: (tsid, service_id) is the same identity the lineup uses,
        # since a DVB PAT program_number IS the service_id. Name matching is
        # only a fallback -- SDT and scan names disagree more often than not.
        forced = id_map.get((tsid, sid))
        if not forced:
            svc = g.services.get(key)
            if svc:
                forced = id_map.get(_slug(svc['name']))
        if forced:
            return forced
    if style == 'name':
        svc = g.services.get(key)
        if svc:
            return _slug(svc['name']) + '.dvb'
    elif style == 'lcn':
        n = g.lcn.get(key)
        if n:
            return '%d.dvb' % n
    return '%d.%d.%d.dvb' % (sid, tsid, onid)


def _t(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime('%Y%m%d%H%M%S +0000')


def write_xmltv(g: Guide, out, id_style='dvb', only_named=False,
                days=None, default_lang='de', id_map=None, only_ids=None):
    now = dt.datetime.now(dt.timezone.utc)
    horizon = now + dt.timedelta(days=days) if days else None

    keys = {(e['onid'], e['tsid'], e['sid']) for e in g.events.values()}
    if only_named:
        keys = {k for k in keys if k in g.services}
    ids = {k: channel_id(g, k, id_style, id_map) for k in keys}
    if only_ids is not None:
        # drop services that are not in the consumer's lineup: they can never
        # be matched, and they are most of the file
        keys = {k for k in keys if ids[k] in only_ids}
        ids = {k: v for k, v in ids.items() if k in keys}
    write_xmltv.ids_used = set(ids.values())
    write_xmltv.duplicate_ids = []

    w = out.write
    w('<?xml version="1.0" encoding="UTF-8"?>\n')
    w('<!DOCTYPE tv SYSTEM "xmltv.dtd">\n')
    w('<tv generator-info-name="hdhr3_epg.py" source-info-name="DVB EIT">\n')

    def _num(cid):
        # natural order: "2" before "10" before "100". String order puts channel
        # 2 near the end of the file, which matters if an importer stops early.
        return (0, int(cid)) if cid.isdigit() else (1, 0)

    def sort_key(k):
        return (_num(ids[k]), g.services.get(k, {}).get('name', ''), k)

    emitted = set()
    dupes = []
    for k in sorted(keys, key=sort_key):
        cid = ids[k]
        if cid in emitted:
            # two services resolving to one GuideNumber: emit the channel once,
            # duplicate <channel> ids are invalid XMLTV and confuse importers
            dupes.append(cid)
            continue
        emitted.add(cid)
        svc = g.services.get(k)
        w('  <channel id=%s>\n' % quoteattr(cid))
        if svc:
            w('    <display-name>%s</display-name>\n' % escape(svc['name']))
        # The numeric display-name and <lcn> must agree with the channel id:
        # importers that map on a number rather than the id will otherwise bind
        # to a channel the consumer's lineup does not have. Only fall back to
        # the NIT logical channel number when the id is not itself numeric.
        num = cid if cid.isdigit() else None
        if num is None:
            n = g.lcn.get(k)
            num = str(n) if n else None
        if num:
            w('    <display-name>%s</display-name>\n' % num)
            w('    <lcn>%s</lcn>\n' % num)
        if not svc and not num:
            w('    <display-name>%s</display-name>\n' % escape(cid))
        w('  </channel>\n')

    evs = [e for e in g.events.values()
           if (e['onid'], e['tsid'], e['sid']) in keys]
    evs.sort(key=lambda e: (_num(ids[(e['onid'], e['tsid'], e['sid'])]),
                            ids[(e['onid'], e['tsid'], e['sid'])],
                            e['start'], e['stop']))
    # One channel id can be fed by more than one service key (the same service
    # seen under a second original_network_id via EIT other). Keeping both sets
    # produces a schedule that overlaps itself, which importers reject outright
    # -- so emit one non-overlapping run of airings per channel.
    clean = []
    prev_id = None
    prev_stop = None
    overlaps = 0
    for e in evs:
        cid = ids[(e['onid'], e['tsid'], e['sid'])]
        if cid != prev_id:
            prev_id, prev_stop = cid, None
        if prev_stop is not None and e['start'] < prev_stop:
            overlaps += 1
            continue
        prev_stop = e['stop']
        clean.append(e)
    evs = clean
    write_xmltv.overlaps_dropped = overlaps
    written = 0
    for e in evs:
        if horizon and e['start'] > horizon:
            continue
        if not e['title']:
            continue
        cid = ids[(e['onid'], e['tsid'], e['sid'])]
        lang = e['lang'] or default_lang
        w('  <programme start="%s" stop="%s" channel=%s>\n'
          % (_t(e['start']), _t(e['stop']), quoteattr(cid)))
        w('    <title lang="%s">%s</title>\n' % (lang, escape(e['title'])))
        if e['sub_title']:
            w('    <sub-title lang="%s">%s</sub-title>\n' % (lang, escape(e['sub_title'])))
        if e['desc']:
            w('    <desc lang="%s">%s</desc>\n' % (lang, escape(e['desc'])))
        for c in e['categories']:
            w('    <category lang="en">%s</category>\n' % escape(c))
        if e['items']:
            w('    <credits>\n')
            for k_, v in e['items']:
                kl = k_.lower()
                if 'regie' in kl or 'director' in kl:
                    w('      <director>%s</director>\n' % escape(v))
                elif 'darsteller' in kl or 'actor' in kl or 'cast' in kl:
                    for a in re.split(r'\s*[,;]\s*', v):
                        if a:
                            w('      <actor>%s</actor>\n' % escape(a))
            w('    </credits>\n')
        if e['video']:
            w('    <video><quality>HDTV</quality></video>\n')
        if e['rating']:
            w('    <rating system="DVB"><value>%d</value></rating>\n' % e['rating'])
        ns = xmltv_ns(e.get('season'), e.get('episode'), e.get('total'))
        if ns:
            w('    <episode-num system="xmltv_ns">%s</episode-num>\n' % ns)
            if e.get('episode') is not None:
                w('    <episode-num system="onscreen">%s</episode-num>\n'
                  % escape(('S%d ' % e['season'] if e.get('season') else '')
                           + 'E%d' % e['episode']))
        # a stable per-airing key, so Plex can hold onto an airing across
        # refreshes instead of re-matching it every time
        w('    <episode-num system="dd_progid">EP%08d.%04d</episode-num>\n'
          % ((e['onid'] << 16 | e['tsid']) & 0xFFFFFFF, e['eid'] & 0xFFFF))
        w('    <episode-num system="dvb">%d.%d.%d.%d</episode-num>\n'
          % (e['onid'], e['tsid'], e['sid'], e['eid']))
        w('  </programme>\n')
        written += 1
    w('</tv>\n')
    write_xmltv.duplicate_ids = sorted(set(dupes))
    if overlaps:
        sys.stderr.write('note: dropped %d overlapping airing(s) so every '
                         'channel has a clean schedule\n' % overlaps)
    if dupes:
        sys.stderr.write('warning: %d channel id(s) claimed by more than one '
                         'service: %s\n'
                         % (len(set(dupes)), ', '.join(sorted(set(dupes))[:10])))
    return len(emitted), written


# ------------------------------------------------------------------ HDHomeRun

BINARY_NAMES = ('hdhomerun_config.exe', 'hdhomerun_config')

_SEARCH_DIRS = [
    os.environ.get('ProgramFiles', r'C:\Program Files'),
    os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
    os.environ.get('ProgramW6432', r'C:\Program Files'),
    r'C:\Program Files', r'C:\Program Files (x86)',
    '/usr/bin', '/usr/local/bin', '/opt/bin', '/opt/local/bin',
    '/usr/pkg/bin', os.path.expanduser('~/bin'),
]

_SUBDIRS = [
    ('Silicondust', 'HDHomeRun'), ('SiliconDust', 'HDHomeRun'),
    ('HDHomeRun',), (),
]


def find_binary(explicit=None):
    """Locate hdhomerun_config on Windows or POSIX. Raises SystemExit if absent."""
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK | os.R_OK):
            return explicit
        found = shutil.which(explicit)
        if found:
            return found
        raise SystemExit('hdhomerun_config not usable at: %s' % explicit)

    for name in BINARY_NAMES:
        found = shutil.which(name)
        if found:
            return found

    seen = set()
    for base in _SEARCH_DIRS:
        if not base or base in seen:
            continue
        seen.add(base)
        for sub in _SUBDIRS:
            for name in BINARY_NAMES:
                cand = os.path.join(base, *sub, name)
                if os.path.isfile(cand):
                    return cand

    # Linux distro packages sometimes version the name (hdhomerun_config-20221010)
    for base in ('/usr/bin', '/usr/local/bin'):
        try:
            for entry in sorted(os.listdir(base), reverse=True):
                if entry.startswith('hdhomerun_config'):
                    cand = os.path.join(base, entry)
                    if os.access(cand, os.X_OK):
                        return cand
        except OSError:
            pass

    raise SystemExit(
        'hdhomerun_config not found. Install the HDHomeRun software or libhdhomerun,\n'
        'or pass --binary <full path> (e.g. --binary '
        r'"C:\Program Files\Silicondust\HDHomeRun\hdhomerun_config.exe").')


def _stop(proc, grace=5.0):
    """Ask hdhomerun_config to stop saving so it flushes and closes the file."""
    if proc.poll() is not None:
        return
    try:
        if os.name == 'nt':
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
    except (OSError, ValueError):
        pass
    try:
        proc.wait(grace)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.terminate()
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class HDHR:
    def __init__(self, device='FFFFFFFF', tuner=0, binary='hdhomerun_config'):
        self.dev, self.tuner, self.bin = device, tuner, binary

    def _run(self, *args, timeout=30):
        cmd = [self.bin, self.dev] + list(args)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or '') + (p.stderr or '')
        if p.returncode != 0 or out.startswith('ERROR'):
            raise RuntimeError('%s -> %s' % (' '.join(cmd), out.strip()))
        return out.strip()

    def get(self, item):
        return self._run('get', item)

    def set(self, item, value):
        return self._run('set', item, value)

    def t(self, item):
        return '/tuner%d/%s' % (self.tuner, item)

    def set_channelmap(self, channelmap):
        """Best effort. We always tune by explicit frequency (auto:<Hz>), so
        channelmap only matters for `scan` -- never fail a tune over it."""
        if not channelmap:
            return True
        try:
            self.set(self.t('channelmap'), channelmap)
            return True
        except RuntimeError as e:
            if not getattr(HDHR, '_cm_warned', False):
                HDHR._cm_warned = True
                sys.stderr.write(
                    'warning: could not set channelmap %s (%s)\n'
                    '  tuning by frequency anyway. If tunes also fail, the '
                    'device is probably wedged -- power-cycle it.\n'
                    % (channelmap, str(e).split('-> ')[-1]))
            return False

    def wait_lock(self, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.get(self.t('status'))
            m = re.search(r'lock=(\S+)', st)
            if m and m.group(1) not in ('none', '(ntsc)'):
                return st
            time.sleep(0.5)
        return None

    def scan(self, channelmap=None):
        self.set_channelmap(channelmap)
        cmd = [self.bin, self.dev, 'scan', '/tuner%d' % self.tuner]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
        muxes, cur = [], None
        for line in p.stdout:
            line = line.strip()
            print(line, file=sys.stderr)
            m = re.match(r'^SCANNING:\s+(\d+)\s*\(([^)]*)\)', line)
            if m:
                if cur and cur.get('lock'):
                    muxes.append(cur)
                cur = {'freq': int(m.group(1)), 'channel': m.group(2).split(',')[0],
                       'lock': None, 'tsid': None, 'programs': []}
                continue
            if cur is None:
                continue
            m = re.match(r'^LOCK:\s+(\S+)', line)
            if m and m.group(1) != 'none':
                cur['lock'] = m.group(1)
            m = re.match(r'^TSID:\s+(\S+)', line)
            if m:
                cur['tsid'] = int(m.group(1), 0)
            m = re.match(r'^PROGRAM\s+(\d+):\s*(\S+)\s*(.*)$', line)
            if m:
                cur['programs'].append({'program': int(m.group(1)),
                                        'vchannel': m.group(2),
                                        'name': clean_name(m.group(3))})
        p.wait()
        if cur and cur.get('lock'):
            muxes.append(cur)
        return muxes

    def capture(self, freq, seconds, path, modulation='auto', channelmap=None,
                pids=PID_FILTER):
        self.set_channelmap(channelmap)
        self.set(self.t('channel'), '%s:%d' % (modulation, freq))
        st = self.wait_lock()
        if not st:
            self.set(self.t('channel'), 'none')
            return False, 'no lock'
        # filter is reset to pass-all by "set channel", so apply it after tuning
        if pids and pids != 'none':
            try:
                self.set(self.t('filter'), pids)
            except RuntimeError as e:
                sys.stderr.write('  filter %r rejected (%s); capturing '
                                 'unfiltered\n' % (pids, e))
        cmd = [self.bin, self.dev, 'save', '/tuner%d' % self.tuner, path]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        # keep save's output: it is the only place "resource locked" and
        # friends show up, and silently discarding it hides real failures
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, creationflags=flags)
        try:
            time.sleep(seconds)
        finally:
            _stop(p)
            try:
                self.set(self.t('channel'), 'none')
            except RuntimeError:
                pass
        try:
            out = (p.stdout.read() or b'').decode('utf-8', 'replace')
        except Exception:
            out = ''
        p.stdout.close() if p.stdout else None
        size = os.path.getsize(path) if os.path.exists(path) else 0
        msg = re.sub(r'\.{2,}', '', out).strip().strip('.').strip()
        msg = ' '.join(msg.split())
        if size == 0:
            return False, 'captured 0 bytes%s' % (' -- %s' % msg if msg else '')
        return True, '%d bytes%s' % (size, ' -- %s' % msg if msg else '')


# ----------------------------------------------------------------- entry points

def parse_file(path, g: Guide, quiet=False):
    handlers = {
        EIT_PID: SectionAssembler(g.eit),
        SDT_PID: SectionAssembler(g.sdt),
        NIT_PID: SectionAssembler(g.nit),
    }
    size = os.path.getsize(path) if os.path.exists(path) else 0
    with open(path, 'rb') as f:
        def prog(n):
            if not quiet and size:
                sys.stderr.write('\r  parsing %5.1f%%' % (100.0 * n / size))
        demux(f, handlers, prog)
    if not quiet and size:
        sys.stderr.write('\r  parsed %.1f MiB   \n' % (size / 1048576.0))


def cmd_scan(a):
    hd = HDHR(a.device, a.tuner, a.binary)
    muxes = hd.scan(a.channelmap)
    with open(a.output, 'w') as f:
        json.dump(muxes, f, indent=2, ensure_ascii=False)
    print('%d locked multiplexes -> %s' % (len(muxes), a.output), file=sys.stderr)
    return 0


def cmd_grab(a):
    hd = HDHR(a.device, a.tuner, a.binary)
    if a.freq:
        muxes = [{'freq': f, 'channel': str(f)} for f in a.freq]
    else:
        with open(a.muxes) as f:
            muxes = json.load(f)
    if a.max_muxes:
        muxes = muxes[:a.max_muxes]

    g = Guide()
    tmpdir = tempfile.mkdtemp(prefix='hdhr3_epg.')
    try:
        for i, mux in enumerate(muxes, 1):
            freq = mux['freq']
            print('[%d/%d] %s (%d Hz), %ds' % (i, len(muxes), mux.get('channel', ''),
                                               freq, a.dwell), file=sys.stderr)
            ts = os.path.join(tmpdir, 'mux%d.ts' % freq)
            try:
                ok, info = hd.capture(freq, a.dwell, ts, a.modulation, a.channelmap)
            except RuntimeError as e:
                print('  tuner error: %s' % e, file=sys.stderr)
                continue
            if not ok:
                print('  %s' % info, file=sys.stderr)
                continue
            if os.path.exists(ts):
                parse_file(ts, g, a.quiet)
                if not a.keep:
                    os.unlink(ts)
            print('  events=%d services=%d' % (len(g.events), len(g.services)),
                  file=sys.stderr)
            if a.stop_when and len(g.events) >= a.stop_when:
                print('  event target reached, stopping', file=sys.stderr)
                break
    finally:
        if not a.keep:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print('captures kept in %s' % tmpdir, file=sys.stderr)
    return emit(g, a)


def cmd_parse(a):
    g = Guide()
    for p in a.files:
        print('parsing %s' % p, file=sys.stderr)
        parse_file(p, g, a.quiet)
    return emit(g, a)


def emit(g: Guide, a):
    if not g.events:
        print('no EIT events decoded', file=sys.stderr)
        return 1
    out = open(a.output, 'w', encoding='utf-8') if a.output != '-' else sys.stdout
    try:
        ch, pr = write_xmltv(g, out, a.id_style, a.only_named, a.days, a.lang)
    finally:
        if out is not sys.stdout:
            out.close()
    print('sections: EIT=%d SDT=%d NIT=%d' % (g.stats['eit'], g.stats['sdt'],
                                              g.stats['nit']), file=sys.stderr)
    print('%d channels, %d programmes -> %s' % (ch, pr, a.output), file=sys.stderr)
    return 0


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
    ap.add_argument('--binary', default=None,
                    help='path to hdhomerun_config (auto-detected if omitted)')
    ap.add_argument('-q', '--quiet', action='store_true')
    sub = ap.add_subparsers(dest='cmd', required=True)

    def out_opts(p):
        p.add_argument('-o', '--output', default='guide.xml')
        p.add_argument('--id-style', choices=['dvb', 'name', 'lcn'], default='dvb',
                       help='XMLTV channel id scheme (default sid.tsid.onid.dvb)')
        p.add_argument('--only-named', action='store_true',
                       help='drop services with no SDT name')
        p.add_argument('--days', type=int, default=None)
        p.add_argument('--lang', default='de', help='fallback language code')

    s = sub.add_parser('scan', help='channel scan -> muxes.json')
    s.add_argument('-d', '--device', default='FFFFFFFF')
    s.add_argument('-t', '--tuner', type=int, default=0)
    s.add_argument('--channelmap', default=None, help='eu-bcast | eu-cable')
    s.add_argument('-o', '--output', default='muxes.json')
    s.set_defaults(func=cmd_scan)

    g = sub.add_parser('grab', help='tune muxes, capture EIT, write XMLTV')
    g.add_argument('-d', '--device', default='FFFFFFFF')
    g.add_argument('-t', '--tuner', type=int, default=0)
    g.add_argument('--channelmap', default=None)
    g.add_argument('--modulation', default='auto')
    g.add_argument('-m', '--muxes', default='muxes.json')
    g.add_argument('-f', '--freq', type=int, action='append',
                   help='tune this frequency in Hz (repeatable; skips muxes.json)')
    g.add_argument('--dwell', type=int, default=60, help='seconds per mux')
    g.add_argument('--max-muxes', type=int, default=0)
    g.add_argument('--stop-when', type=int, default=0,
                   help='stop after N events collected (EIT other often suffices)')
    g.add_argument('--keep', action='store_true', help='keep captured .ts files')
    out_opts(g)
    g.set_defaults(func=cmd_grab)

    p = sub.add_parser('parse', help='parse existing TS capture(s)')
    p.add_argument('files', nargs='+')
    out_opts(p)
    p.set_defaults(func=cmd_parse)

    require_runtime()
    a = ap.parse_args()
    if a.cmd != 'parse':
        a.binary = find_binary(a.binary)
        if not a.quiet:
            print('using %s' % a.binary, file=sys.stderr)
    try:
        return a.func(a)
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
