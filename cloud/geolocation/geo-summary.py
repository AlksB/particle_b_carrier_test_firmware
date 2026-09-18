#!/usr/bin/env python3
"""Summarize an events.tsv exported from the Particle Console (see
export-console-events.js): how often the geolocation chain fired versus how
often the device connected and changed towers.

Usage: geo-summary.py events.tsv
"""
import collections
import json
import sys

rows = [l.rstrip('\n').split('\t') for l in open(sys.argv[1], encoding='utf-8')]
rows = [r for r in rows if len(r) >= 4]
rows.reverse()  # console lists newest first
print(f'rows: {len(rows)}   span: {rows[0][3]} -> {rows[-1][3]}')

cnt = collections.Counter(r[0] for r in rows)
for k, v in cnt.most_common():
    print(f'{v:5}  {k}')

print('\n--- geo chain ---')
for r in rows:
    if r[0] in ('geo_lookup', 'hook-response/geo_lookup', 'hook-error/geo_lookup', 'geo_fix'):
        print(r[3], '|', r[0], '|', r[1][:110])

print('\n--- vitals with tower (time, rat, cid, rsrp) ---')
for r in rows:
    if r[0] == 'spark/device/diagnostics/update' and 'cell_global_identity' in r[1]:
        try:
            d = json.loads(r[1])
            c = d['device']['network']['cellular']
            print(r[3], c['radio_access_technology'], c['cell_global_identity']['cell_id'],
                  d['device']['network']['signal']['strengthv'])
        except (KeyError, ValueError):
            print(r[3], 'parse fail')
