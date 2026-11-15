#!/usr/bin/env python3
"""Read aggregate Gallery analytics; publish no credentials or visitor records."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent
SCHEMA = 'gallery-metrics-v1'
LIMIT = 10000


def count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid aggregate count')
    return round(value)


def clean_snapshot(data, work_ids, *, prune_popular=False):
    if data.get('schema') != SCHEMA or data.get('status') not in {'ready','empty','stale','unavailable'}:
        raise ValueError('Unsupported analytics snapshot')
    if data['status'] == 'unavailable':
        return {'schema':SCHEMA,'status':'unavailable'}
    stamp = data['generated_at']
    datetime.fromisoformat(stamp.replace('Z','+00:00'))
    windows = {}
    for days in ('7','30'):
        item = data['windows'][days]
        windows[days] = {'page_views':count(item['page_views']),'visits':count(item['visits'])}
    popular = []
    for item in data['popular'][:5]:
        if prune_popular and item['id'] not in work_ids:
            continue  # Retain aggregate history after a formerly public work is removed.
        if item['id'] not in work_ids or item['id'] in {p['id'] for p in popular}:
            raise ValueError('Unknown or duplicate public work')
        popular.append({'id':item['id'],'page_views':count(item['page_views'])})
    result = {'schema':SCHEMA,'status':data['status'],'generated_at':stamp,'windows':windows,'popular':popular}
    if 'history' in data:
        history = data['history']; scope = history['site_url']; parsed = urlsplit(scope)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not scope.endswith('/')):
            raise ValueError('Invalid analytics history scope')
        days = {}
        through = datetime.fromisoformat(stamp.replace('Z','+00:00')).date()
        for item in history['days']:
            day = date.fromisoformat(item['date'])
            if day.isoformat() != item['date'] or day > through or day in days:
                raise ValueError('Invalid or duplicate analytics history date')
            days[day] = {'date':day.isoformat(),'page_views':count(item['page_views']),
                         'visits':count(item['visits'])}
        if not days or max(days) != through:
            raise ValueError('Analytics history must include its snapshot day')
        first, last = min(days), max(days)
        result['history'] = {'site_url':scope,'days':[days[d] for d in sorted(days)]}
        result['cumulative'] = {'since':first.isoformat(),'through':stamp,
            'page_views':sum(d['page_views'] for d in days.values()),
            'visits':sum(d['visits'] for d in days.values()),
            'missing_days':(last-first).days+1-len(days)}
    return result


def merge_history(current, previous, catalog):
    """Replace queried days, never add overlapping rolling-window totals."""
    ids = set(catalog['works'])
    current = clean_snapshot(current, ids)
    if previous is None:
        return current
    previous = clean_snapshot(previous, ids, prune_popular=True)
    old = previous.get('history')
    if old is None:
        return current  # Legacy windows cannot reconstruct daily history.
    new = current['history']
    if old['site_url'] != new['site_url'] or new['site_url'] != catalog['site_url']:
        raise ValueError('Analytics history belongs to a different site')
    if (datetime.fromisoformat(previous['generated_at'].replace('Z','+00:00')) >
            datetime.fromisoformat(current['generated_at'].replace('Z','+00:00'))):
        raise ValueError('Analytics refresh predates the retained snapshot')
    days = {d['date']:d for d in old['days']}
    days.update({d['date']:d for d in new['days']})
    current['history'] = {'site_url':new['site_url'],'days':list(days.values())}
    return clean_snapshot(current, ids)


def aggregate(rows, catalog, now):
    if not isinstance(rows,list) or len(rows) >= LIMIT:
        raise ValueError('Missing or truncated analytics result')
    base = urlsplit(catalog['site_url']).path.rstrip('/')+'/'
    work_paths = {base+'works/'+slug+'/index.html':slug for slug in catalog['works']}
    allowed = {base,base+'index.html',base+'rights.html',base+'privacy.html',*work_paths}
    totals = {'7':{'page_views':0,'visits':0},'30':{'page_views':0,'visits':0}}
    daily = {(now.date()-timedelta(days=age)).isoformat():{'page_views':0,'visits':0}
             for age in range(30)}
    popular = {}
    for row in rows:
        dims = row['dimensions']; path = dims['requestPath']
        if path.endswith('/') and path != base:
            path += 'index.html'
        if path not in allowed:
            continue
        day = datetime.strptime(dims['date'],'%Y-%m-%d').date()
        age = (now.date()-day).days
        if not 0 <= age < 30:
            continue
        views,visits = count(row['count']),count(row['sum']['visits'])
        daily[day.isoformat()]['page_views'] += views
        daily[day.isoformat()]['visits'] += visits
        for window in ('7','30'):
            if age < int(window):
                totals[window]['page_views'] += views; totals[window]['visits'] += visits
        if path in work_paths:
            slug = work_paths[path]; popular[slug] = popular.get(slug,0)+views
    data = {'schema':SCHEMA,'status':'ready' if totals['30']['page_views'] else 'empty',
            'generated_at':now.isoformat().replace('+00:00','Z'),'windows':totals,
            'history':{'site_url':catalog['site_url'],'days':[{'date':day,**counts} for day,counts in daily.items()]},
            'popular':[{'id':slug,'page_views':views} for slug,views in
                       sorted(popular.items(),key=lambda x:(-x[1],x[0]))[:5]]}
    return clean_snapshot(data,set(catalog['works']))


def fetch(catalog, account, site, token, now):
    if not all(re.fullmatch('[a-fA-F0-9]{32}', v) for v in (account,site)) or not token:
        raise ValueError('Analytics account, site and read credential are required')
    start = (now-timedelta(days=29)).replace(hour=0,minute=0,second=0,microsecond=0)
    quoted = lambda value: json.dumps(value)
    query = ('{viewer{accounts(filter:{accountTag:'+quoted(account)+'}){'
             'rumPageloadEventsAdaptiveGroups(limit:'+str(LIMIT)+',filter:{siteTag:'+quoted(site)+
             ',datetime_geq:'+quoted(start.isoformat())+',datetime_leq:'+quoted(now.isoformat())+
             '}){count sum{visits} dimensions{date requestPath}}}}}')
    req = Request('https://api.cloudflare.com/client/v4/graphql',
                  data=json.dumps({'query':query}).encode(),
                  headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urlopen(req,timeout=25) as response:
        data = json.load(response)
    if data.get('errors'):
        raise ValueError('Cloudflare analytics query failed')
    accounts = data['data']['viewer']['accounts']
    if len(accounts) != 1:
        raise ValueError('Analytics account is unavailable')
    return aggregate(accounts[0]['rumPageloadEventsAdaptiveGroups'],catalog,now)


def unavailable(previous, work_ids):
    try:
        saved = clean_snapshot(previous,work_ids)
        if saved['status'] != 'unavailable':
            saved['status'] = 'stale'
        return saved
    except (ValueError,KeyError,TypeError,AttributeError):
        return {'schema':SCHEMA,'status':'unavailable'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=REPO/'temp/gallery-metrics.json')
    p.add_argument('--reuse-published',action='store_true',help='Carry forward published daily totals; fail if the prior snapshot cannot be read')
    args=p.parse_args()
    catalog=json.loads((REPO/'gallery/catalog.json').read_text())
    previous=None
    if args.reuse_published:
        try:
            url=urljoin(catalog['site_url'],'metrics.json'); parsed=urlsplit(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('Invalid public snapshot URL')
            with urlopen(url,timeout=15) as response:previous=json.load(response)
            previous=clean_snapshot(previous,set(catalog['works']),prune_popular=True)
            if previous.get('history',{}).get('site_url',catalog['site_url']) != catalog['site_url']:
                raise ValueError('Prior snapshot belongs to a different site')
        except Exception:
            print('Prior analytics snapshot unavailable or invalid; stopping to preserve published history.')
            return 1
    try:
        snapshot=fetch(catalog,os.environ.get('CLOUDFLARE_ACCOUNT_ID',''),os.environ.get('CLOUDFLARE_SITE_TAG',''),
                       os.environ.get('CLOUDFLARE_ANALYTICS_API_TOKEN',''),datetime.now(timezone.utc))
        snapshot=merge_history(snapshot,previous,catalog)
    except Exception:
        snapshot=unavailable(previous,set(catalog['works']))
        print('Analytics refresh unavailable; retained valid prior totals or marked unavailable.')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    temporary=args.output.with_name(args.output.name+'.tmp')
    temporary.write_text(json.dumps(snapshot,ensure_ascii=False,indent=2)+'\n')
    temporary.replace(args.output)
    print(json.dumps({'status':snapshot['status'],'output':str(args.output)}))
    return 0


if __name__=='__main__':raise SystemExit(main())
