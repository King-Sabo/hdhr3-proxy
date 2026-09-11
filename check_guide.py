#!/usr/bin/env python3
"""
check_guide.py -- validate a generated guide.xml the way a consumer would read it.

    python check_guide.py guide.xml
    python check_guide.py guide.xml --lineup http://192.168.1.191:5004/lineup.json

Reports: XML well-formedness, channel and programme counts, ids that have no
programmes, programmes referencing an undeclared channel, overlapping or
duplicate airings, the time span covered, and how the ids line up against a
live lineup.json.
"""

import argparse
import collections
import datetime as dt
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET


def parse_ts(v):
    try:
        return dt.datetime.strptime(v.split()[0], '%Y%m%d%H%M%S')
    except (ValueError, IndexError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('path')
    ap.add_argument('--lineup', help='lineup.json URL or file to compare against')
    ap.add_argument('--show', type=int, default=12, help='max items to list')
    a = ap.parse_args()

    # 1. well-formedness -- a truncated or corrupt file fails here
    try:
        root = ET.parse(a.path).getroot()
    except ET.ParseError as e:
        print('FAIL: not well-formed XML: %s' % e)
        print('      a consumer would reject the whole file')
        return 2
    if root.tag != 'tv':
        print('FAIL: root element is <%s>, expected <tv>' % root.tag)
        return 2
    print('XML             : well-formed, root <tv>')

    chans = [c.get('id') for c in root.findall('channel')]
    progs = root.findall('programme')
    print('channels        : %d declared' % len(chans))
    print('programmes      : %d' % len(progs))

    dup_ch = [c for c, n in collections.Counter(chans).items() if n > 1]
    if dup_ch:
        print('DUPLICATE       : %d channel id(s) declared twice: %s'
              % (len(dup_ch), ', '.join(sorted(dup_ch)[:a.show])))

    by_chan = collections.defaultdict(list)
    undeclared = set()
    known = set(chans)
    for p in progs:
        cid = p.get('channel')
        if cid not in known:
            undeclared.add(cid)
        by_chan[cid].append((parse_ts(p.get('start', '')),
                             parse_ts(p.get('stop', '')),
                             (p.findtext('title') or '')))
    if undeclared:
        print('ORPHANED        : %d programme channel(s) never declared: %s'
              % (len(undeclared), ', '.join(sorted(undeclared)[:a.show])))

    empty = sorted(set(chans) - set(by_chan), key=lambda c: (len(c), c))
    print('with programmes : %d of %d channels' % (len(by_chan), len(chans)))
    if empty:
        print('  no programmes : %s%s' % (', '.join(empty[:a.show]),
                                          ' ...' if len(empty) > a.show else ''))

    # 2. per-channel schedule sanity
    overlaps = collections.Counter()
    bad_time = 0
    span_lo = span_hi = None
    for cid, items in by_chan.items():
        items.sort(key=lambda t: (t[0] or dt.datetime.min))
        prev_stop = None
        for start, stop, _ in items:
            if start is None or stop is None or stop <= start:
                bad_time += 1
                continue
            span_lo = start if span_lo is None or start < span_lo else span_lo
            span_hi = stop if span_hi is None or stop > span_hi else span_hi
            if prev_stop is not None and start < prev_stop:
                overlaps[cid] += 1
            prev_stop = stop
    if bad_time:
        print('BAD TIMES       : %d programme(s) with unparseable or reversed '
              'start/stop' % bad_time)
    if overlaps:
        print('OVERLAPS        : %d channel(s), %d airing(s) -- consumers may '
              'reject these channels' % (len(overlaps), sum(overlaps.values())))
        for cid, n in overlaps.most_common(a.show):
            print('  channel %-6s %d overlapping' % (cid, n))
    else:
        print('overlaps        : none')

    if span_lo:
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        print('covers          : %s .. %s UTC (%.1f days)'
              % (span_lo.strftime('%Y-%m-%d %H:%M'),
                 span_hi.strftime('%Y-%m-%d %H:%M'),
                 (span_hi - span_lo).total_seconds() / 86400.0))
        live = [c for c, items in by_chan.items()
                if any(s and e and s <= now < e for s, e, _ in items)]
        print('airing now      : %d channel(s) have a programme covering '
              'this moment' % len(live))
        if not live:
            print('  WARNING: nothing covers now -- a guide entirely in the '
                  'past or future looks empty in most UIs')

    # 3. where things sit in the file, and anything a stricter parser may
    #    dislike that expat happily accepted
    raw = open(a.path, 'rb').read()
    total = len(raw)
    print('file size       : %.1f MB' % (total / 1048576.0))
    marks = []
    for cid in chans:
        needle = ('channel="%s">' % cid).encode()
        i = raw.find(needle)
        if i >= 0:
            marks.append((100.0 * i / total, cid))
    marks.sort()
    print('first programme per channel, by position in file:')
    step = max(1, len(marks) // 10)
    for pct, cid in marks[::step]:
        print('  %5.1f%%  channel %s' % (pct, cid))

    text = raw.decode('utf-8', 'replace')
    odd = collections.Counter()
    first_at = {}
    for i, ch in enumerate(text):
        o = ord(ch)
        if o in (0x09, 0x0A, 0x0D) or 0x20 <= o <= 0x7E:
            continue
        cat = None
        if o < 0x20:
            cat = 'C0 control U+%04X' % o
        elif 0x80 <= o <= 0x9F:
            cat = 'C1 control U+%04X' % o
        elif o in (0x2028, 0x2029):
            cat = 'line/para separator U+%04X' % o
        elif 0xE000 <= o <= 0xF8FF:
            cat = 'private use U+%04X' % o
        elif o == 0xFFFD:
            cat = 'replacement char U+FFFD'
        elif o > 0xFFFF:
            cat = 'astral U+%04X' % o
        if cat:
            odd[cat] += 1
            first_at.setdefault(cat, 100.0 * i / len(text))
    if odd:
        print('unusual characters (expat accepts these, other parsers may not):')
        for cat, n in odd.most_common(10):
            print('  %-28s %6d  first at %.1f%%' % (cat, n, first_at[cat]))
    else:
        print('characters      : nothing unusual outside ASCII + accents')

    longest = max((len(p.findtext('desc') or '') for p in progs), default=0)
    longtitle = max((len(p.findtext('title') or '') for p in progs), default=0)
    print('longest desc    : %d chars (title %d)' % (longest, longtitle))
    lines = raw.split(b'\n')
    ml = max(len(l) for l in lines) if lines else 0
    print('longest line    : %d bytes' % ml)

    # 4. compare with the live lineup
    if a.lineup:
        try:
            if a.lineup.startswith('http'):
                raw = urllib.request.urlopen(a.lineup, timeout=10).read()
            else:
                raw = open(a.lineup, 'rb').read()
            lineup = {str(e['GuideNumber']) for e in json.loads(raw)}
        except Exception as e:
            print('lineup          : could not read (%s)' % e)
            return 0
        gset = set(chans)
        print('lineup          : %d channels' % len(lineup))
        missing = sorted(lineup - gset, key=lambda c: (len(c), c))
        extra = sorted(gset - lineup, key=lambda c: (len(c), c))
        if missing:
            print('  in lineup, not in guide : %s%s'
                  % (', '.join(missing[:a.show]),
                     ' ...' if len(missing) > a.show else ''))
        if extra:
            print('  in guide, not in lineup : %s%s  <-- unmatchable'
                  % (', '.join(extra[:a.show]),
                     ' ...' if len(extra) > a.show else ''))
        usable = len((lineup & gset) & set(by_chan))
        print('  usable                  : %d of %d lineup channels have '
              'guide data' % (usable, len(lineup)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
