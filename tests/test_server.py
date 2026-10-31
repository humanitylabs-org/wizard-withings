"""Tests for the Withings connector against a fake Withings Public API built from the official docs
(https://developer.withings.com/api-reference, OpenAPI 2.0): requesttoken with client secret, key test, OAuth state,
refresh-token rotation, the {status, body, error} envelope, more/offset paging, 601 rate limits, the free plan's 277
refusal of advanced sleep fields (fallback to basic fields), lastupdate incremental sync with overlap, the
wizard.health-data/1 contract (no readiness, intensity-minute load, sleep, HRV), HTTP/MCP guards and the signed revoke
(Signature v2 - Getnonce + HMAC-SHA256) on disconnect.
Run: python3 -m unittest discover -s tests"""
import copy
import datetime as dt
import hashlib
import hmac
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
CID = '7573fd4a4c421ddd102dac406dc6e0e0e22f683c4a5e81ff0a5caf8b65abed67'      # from the docs' request sample
SECRET = 'd9286311451fc6ed71b372a075c58c5058be158f56a77865e43ab3783255424f'
CALLBACK = 'https://box.example.ts.net:8444/wizard-withings/callback'
TODAY = dt.datetime.now(dt.timezone.utc).date()
DAYS = 40
PAGE = 10
ADVANCED_SLEEP = {'sleep_score', 'rmssd_start_avg', 'rmssd_end_avg', 'rr_average', 'rr_min', 'rr_max',
                  'breathing_disturbances_intensity', 'snoring', 'snoringepisodecount'}

# ---- sample objects, copied from the examples in Withings' OpenAPI specification ----
DOC_ACTIVITY = {'date': '2020-06-24', 'timezone': 'Europe/Paris', 'deviceid': '892359876fd8805ac45bab078c4828692f0276b1',
                'hash_deviceid': '892359876fd8805ac45bab078c4828692f0276b1', 'brand': 18, 'modified': 1594257200, 'model': 'Pulse HR',
                'modelid': 59, 'steps': 6200, 'distance': 3000, 'elevation': 100, 'soft': 800, 'moderate': 1000, 'intense': 700,
                'active': 2300, 'calories': 1200, 'totalcalories': 3500, 'hr_average': 80, 'hr_min': 70, 'hr_max': 80,
                'hr_zone_0': 100, 'hr_zone_1': 200, 'hr_zone_2': 300, 'hr_zone_3': 400}
DOC_SLEEP = {'id': 12345, 'timezone': 'Europe/Paris', 'model': 16, 'model_id': 51, 'startdate': 1594245600, 'enddate': 1594257200,
             'date': '2020-07-09', 'created': 1594257200, 'modified': 1594257200,
             'hash_deviceid': '892359876fd8805ac45bab078c4828692f0276b1', 'completed': True,
             'data': {'total_timeinbed': 11600, 'total_sleep_time': 10200, 'lightsleepduration': 5400, 'deepsleepduration': 2400,
                      'remsleepduration': 2400, 'sleep_efficiency': 0.88, 'sleep_latency': 600, 'wakeup_latency': 300,
                      'wakeupduration': 800, 'wakeupcount': 2, 'waso': 500, 'nb_rem_episodes': 3, 'out_of_bed_count': 0,
                      'hr_average': 58, 'hr_min': 49, 'hr_max': 77, 'rr_average': 14, 'rr_min': 11, 'rr_max': 18,
                      'sleep_score': 74, 'rmssd_start_avg': 52.0, 'rmssd_end_avg': 61.0, 'snoring': 240, 'snoringepisodecount': 1}}
DOC_WORKOUT = {'id': 12345, 'category': 36, 'timezone': 'Europe/Paris', 'model': 59, 'attrib': 0, 'startdate': 1594245600,
               'enddate': 1594257200, 'date': '2020-07-09', 'modified': 1594257200, 'deviceid': '892359876fd8805ac45bab078c4828692f0276b1',
               'data': {'calories': 650, 'intensity': 0, 'hr_average': 131, 'hr_min': 92, 'hr_max': 168, 'hr_zone_0': 600,
                        'hr_zone_1': 1800, 'hr_zone_2': 6000, 'hr_zone_3': 1200, 'pause_duration': 600, 'steps': 9000,
                        'distance': 11000, 'elevation': 12}}
DOC_TOKEN = {'status': 0, 'body': {'userid': 363, 'access_token': 'a075f8c14fb8df40b08ebc8508533dc332a6910a',
                                   'refresh_token': 'f631236f02b991810feb774765b6ae8e6c6839ca', 'expires_in': 10800,
                                   'scope': 'user.info,user.metrics', 'csrf_token': 'PACnnxwHTaBQOzF7bQqwFUUotIuvtzSM', 'token_type': 'Bearer'}}


def ts(d, h=0, m=0):
    return int(dt.datetime(d.year, d.month, d.day, h, m, tzinfo=dt.timezone.utc).timestamp())


def make_data():
    now = int(time.time())
    sleeps, acts, works = [], [], []
    for i in range(DAYS):
        d = TODAY - dt.timedelta(days=i)
        prev = d - dt.timedelta(days=1)
        modified = now - i * 86400
        sleeps.append(dict(DOC_SLEEP, id=5000 + i, model=16, model_id=93, date=d.isoformat(), startdate=ts(prev, 16), enddate=ts(d, 0, 30),
                           created=modified, modified=modified,
                           data=dict(DOC_SLEEP['data'], total_timeinbed=30600, total_sleep_time=26100, lightsleepduration=14400,
                                     deepsleepduration=5400, remsleepduration=6300, sleep_efficiency=0.85, wakeupduration=2700,
                                     hr_min=48 + i % 5, sleep_score=60 + i % 30, rmssd_start_avg=40.0 + i % 9, rmssd_end_avg=50.0)))
        acts.append(dict(DOC_ACTIVITY, date=d.isoformat(), modified=modified, brand=1, is_tracker=True, steps=7000 + 10 * i,
                         moderate=1800, intense=600 + 60 * (i % 5), totalcalories=2300 + i, hash_deviceid='watch-1'))
        if i % 7 == 0:   # a phone also counted steps that day: the watch's row wins
            acts.append(dict(DOC_ACTIVITY, date=d.isoformat(), modified=modified, brand=18, is_tracker=False, steps=4000, hash_deviceid='phone-1'))
        if i % 3 == 0:
            works.append(dict(DOC_WORKOUT, id=9000 + i, category=2, date=d.isoformat(), startdate=ts(d, 11), enddate=ts(d, 11, 50), modified=modified))
    return {'sleep': sleeps, 'activity': acts, 'workout': works}


class Fake:
    def __init__(self):
        self.data = make_data()
        self.access, self.refresh, self.old_refresh = None, None, set()
        self.calls = []
        self.force_auth = 0
        self.force_601 = 0
        self.plan = 'free'
        self.nonces = set()
        self.revoked = []


FAKE = Fake()


def sig(*values):
    return hmac.new(SECRET.encode(), ','.join(str(v) for v in values).encode(), hashlib.sha256).hexdigest()


class FakeWithings(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body):
        b = json.dumps(body).encode()
        self.send_response(200)                           # Withings answers HTTP 200 and puts the outcome in "status"
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def issue(self):
        FAKE.access = 'acc' + os.urandom(18).hex()
        if FAKE.refresh:
            FAKE.old_refresh.add(FAKE.refresh)
        FAKE.refresh = 'ref' + os.urandom(18).hex()
        return {'status': 0, 'body': dict(DOC_TOKEN['body'], access_token=FAKE.access, refresh_token=FAKE.refresh,
                                          scope='user.info,user.metrics,user.activity')}

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        f = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        path = urlsplit(self.path).path
        FAKE.calls.append((path, f.get('action'), f))
        if path == '/v2/signature' and f.get('action') == 'getnonce':
            if f.get('client_id') != CID or f.get('signature') != sig('getnonce', CID, f.get('timestamp')):
                return self.reply({'status': 503, 'body': {}, 'error': 'Invalid Params: invalid signature'})
            nonce = os.urandom(20).hex()
            FAKE.nonces.add(nonce)
            return self.reply({'status': 0, 'body': {'nonce': nonce}})
        if path == '/v2/oauth2' and f.get('action') == 'revoke':
            ok = f.get('nonce') in FAKE.nonces and f.get('signature') == sig('revoke', CID, f.get('nonce'))
            FAKE.nonces.discard(f.get('nonce'))
            if ok:
                FAKE.revoked.append(f.get('userid'))
                FAKE.access = FAKE.refresh = None
            return self.reply({'status': 0 if ok else 503, 'body': {}})
        if path == '/v2/oauth2' and f.get('action') == 'requesttoken':
            if f.get('client_id') != CID or f.get('client_secret') != SECRET:
                return self.reply({'status': 503, 'body': {}, 'error': 'Invalid Params: invalid client id/secret'})   # the real answer
            if f.get('grant_type') == 'authorization_code':
                if f.get('code') == 'good-code' and f.get('redirect_uri') in (CALLBACK, 'https://example.com/wizard-withings/callback'):
                    return self.reply(self.issue())
                return self.reply({'status': 503, 'body': {}, 'error': 'Invalid Params: invalid code'})
            if f.get('grant_type') == 'refresh_token':
                if f.get('refresh_token') == FAKE.refresh:
                    return self.reply(self.issue())
                return self.reply({'status': 503, 'body': {}, 'error': 'Invalid Params: invalid refresh_token'})
            return self.reply({'status': 503, 'body': {}, 'error': 'Invalid Params'})
        # data services
        if self.headers.get('Authorization') != 'Bearer ' + str(FAKE.access) or FAKE.force_auth:
            FAKE.force_auth = max(0, FAKE.force_auth - 1)
            return self.reply({'status': 401, 'body': {}, 'error': 'XRequestID: Not provided invalid_token: The access token provided is invalid'})
        if FAKE.force_601:
            FAKE.force_601 -= 1
            return self.reply({'status': 601, 'body': {}, 'error': 'Too many request'})
        kind, key = {('/v2/sleep', 'getsummary'): ('sleep', 'series'), ('/v2/measure', 'getactivity'): ('activity', 'activities'),
                     ('/v2/measure', 'getworkouts'): ('workout', 'series')}.get((path, f.get('action')), (None, None))
        if not kind:
            return self.reply({'status': 2554, 'body': {}, 'error': 'Not implemented'})
        fields = set((f.get('data_fields') or '').split(',')) - {''}
        if kind == 'sleep' and FAKE.plan == 'free' and fields & ADVANCED_SLEEP:
            return self.reply({'status': 277, 'body': {}, 'error': 'Data type not available in your plan'})
        if 'lastupdate' in f and ('startdateymd' in f or 'enddateymd' in f):
            return self.reply({'status': 503, 'body': {}, 'error': 'Invalid Params'})
        since = int(f.get('lastupdate') or 0)
        rows = [r for r in FAKE.data[kind] if r['modified'] >= since]
        rows = sorted(rows, key=lambda r: (r['date'], str(r.get('id', r.get('hash_deviceid')))))
        out = []
        for r in rows:
            r = copy.deepcopy(r)
            if 'data' in r:
                r['data'] = {k: v for k, v in r['data'].items() if k in fields}
            else:
                for k in list(r):
                    if k in DOC_ACTIVITY and k not in fields and k not in ('date', 'timezone', 'deviceid', 'hash_deviceid', 'brand', 'modified', 'model', 'modelid'):
                        r.pop(k)
            out.append(r)
        off = int(f.get('offset') or 0)
        page = out[off:off + PAGE]
        more = off + PAGE < len(out)
        return self.reply({'status': 0, 'body': {key: page, 'more': more, 'offset': off + PAGE if more else 0}})


def http(port, method, path, body=None, headers=None):
    h = {'Host': f'127.0.0.1:{port}'}
    if body is not None:
        h['Content-Type'] = 'application/json'
    h.update(headers or {})
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class WithingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeWithings)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        fp = cls.fake.server_address[1]
        cls.tmp = tempfile.mkdtemp()
        os.environ.update({'DATA_DIR': cls.tmp + '/private', 'WITHINGS_API_BASE': f'http://127.0.0.1:{fp}',
                           'WITHINGS_AUTH_URL': f'http://127.0.0.1:{fp}/oauth2_user/authorize2',
                           'WITHINGS_REQUEST_GAP': '0', 'WITHINGS_BACKOFF': '0.01', 'PORT': '18997', 'WIZARD_TRUSTED_HOSTS': 'box.example.ts.net'})
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = importlib.reload(server)
        cls.app = ThreadingHTTPServer(('127.0.0.1', 0), cls.S.Handler)
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.port = cls.app.server_address[1]
        cls.S.TRUSTED_HOSTS.add(f'127.0.0.1:{cls.port}')

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        cls.app.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def files_text(self):
        return ''.join(p.read_bytes().decode('latin-1') for p in Path(self.tmp).rglob('*') if p.is_file())

    def wait_sync(self, timeout=30):
        t = time.time()
        while time.time() - t < timeout:
            if not self.S.SYNC_LOCK.locked() and self.S._state().get('last_sync'):
                time.sleep(0.05)
                if not self.S.SYNC_LOCK.locked():
                    return
            time.sleep(0.05)
        self.fail('sync did not finish')

    def state_of(self, start):
        return parse_qs(urlsplit(start['authorize_url']).query)['state'][0]

    def test_flow(self):
        S = self.S
        self.assertEqual(S.status_info()['status'], 'not_configured')
        info = S.setup_info()
        self.assertEqual((info['scopes'], info['scope_separator'], info['allow_localhost']), (['user.info', 'user.metrics', 'user.activity'], ',', False))
        self.assertIsNone(info['default_redirect'])

        # ---- step 4: Withings refuses localhost/IP/plain http callbacks, so the app does too
        for bad in ('http://localhost:3000/callback', 'https://localhost/callback', 'https://100.1.2.3:8444/wizard-withings/callback',
                    'http://box.example.ts.net/wizard-withings/callback', 'https://[::1]:8444/wizard-withings/callback'):
            code, _ = http(self.port, 'POST', '/api/keys', {'client_id': CID, 'client_secret': SECRET, 'redirect_uri': bad})
            self.assertEqual(code, 400, bad)
        http(self.port, 'POST', '/api/keys', {'client_id': CID, 'client_secret': 'wrong-secret-value-123', 'redirect_uri': CALLBACK})
        code, body = http(self.port, 'POST', '/api/keys/test', {})
        self.assertEqual(json.loads(body)['result'], 'invalid', body)
        code, body = http(self.port, 'POST', '/api/keys', {'client_id': CID, 'client_secret': SECRET, 'redirect_uri': CALLBACK})
        self.assertEqual(code, 200)
        self.assertNotIn(SECRET.encode(), body)
        self.assertNotIn(CID.encode(), body)
        code, body = http(self.port, 'POST', '/api/keys/test', {})
        self.assertEqual(json.loads(body)['result'], 'valid', body)
        tok = [c for c in FAKE.calls if c[1] == 'requesttoken'][-1][2]
        self.assertEqual((tok['client_secret'], tok['grant_type']), (SECRET, 'authorization_code'))
        self.assertEqual(oct(os.stat(S.DATA / 'client.json').st_mode & 0o777), '0o600')

        # ---- step 5: OAuth
        start = S.login_start()
        q = {k: v[0] for k, v in parse_qs(urlsplit(start['authorize_url']).query).items()}
        self.assertEqual((q['response_type'], q['scope'], q['redirect_uri'], q['client_id']), ('code', 'user.info,user.metrics,user.activity', CALLBACK, CID))
        self.assertFalse(start['paste_back'])
        with self.assertRaises(S.Problem):
            S.login_finish(CALLBACK + '?code=good-code&state=nope')
        with self.assertRaises(S.Problem) as e:
            S.login_finish(f"{CALLBACK}?error=access_denied&state={q['state']}")
        self.assertIn('not to allow', e.exception.message)
        with self.assertRaises(S.Problem) as e:
            S.login_finish(f'{CALLBACK}?code=expired-code&state={self.state_of(S.login_start())}')
        self.assertIn('30 seconds', e.exception.message)
        # the callback URL answers a plain probe (Withings may check it) without a code
        self.assertEqual(http(self.port, 'GET', '/wizard-withings/callback')[0], 200)

        # ---- connect through the app's own callback route; the free plan refuses the advanced sleep fields (277);
        #      one 601 and one stale-token answer on the way
        FAKE.calls.clear()
        FAKE.force_601 = 1
        state = self.state_of(S.login_start())
        code, body = http(self.port, 'GET', f'/wizard-withings/callback?code=good-code&state={state}')
        self.assertEqual(code, 200, body)
        self.assertIn(b'Withings connected', body)
        self.wait_sync()
        st = S.status_info()
        self.assertEqual(st['status'], 'connected', st)
        self.assertEqual(st['source'], {'id': 'wizard-withings', 'provider': 'withings', 'name': 'Withings', 'app_path': '/wizard-withings/'})
        self.assertIsNone(st['scales']['readiness'])
        self.assertEqual(st['scales']['load']['scale_max'], 60)
        self.assertEqual(st['counts'], {'days': DAYS, 'sleeps': DAYS, 'workouts': len(FAKE.data['workout'])}, st)
        self.assertEqual(st['setup']['plan_limited'], ['sleep'])
        self.assertIn('sleep score', st['setup']['plan_note'])
        sl_calls = [c[2] for c in FAKE.calls if c[1] == 'getsummary']
        self.assertTrue(set(sl_calls[0]['data_fields'].split(',')) & ADVANCED_SLEEP)
        self.assertFalse(set(sl_calls[-1]['data_fields'].split(',')) & ADVANCED_SLEEP)
        self.assertTrue(any(int(c.get('offset') or 0) > 0 for c in sl_calls), 'paged with more/offset')
        self.assertTrue(all('lastupdate' in c and 'startdateymd' not in c for c in sl_calls))
        self.assertAlmostEqual((time.time() - int(sl_calls[0]['lastupdate'])) / 86400, S.HISTORY_DAYS, delta=0.1)
        d0 = TODAY.isoformat()
        d = S.contract_days({'from': d0, 'to': d0})['days'][0]
        check_day(self, d)
        self.assertIsNone(d['readiness'])
        self.assertIsNone(d['sleep']['score'])           # held back by the plan, never invented
        self.assertIsNone(d['hrv_ms'])
        self.assertEqual(d['sleep']['asleep_h'], 7.25)

        # ---- the plan now allows it (re-checked weekly; force it here)
        FAKE.plan = 'advanced'
        S._update_state(plan_limited={'sleep': time.time() - S.PLAN_RECHECK - 1})
        S.sync(full=True)
        self.assertEqual(S.status_info()['setup']['plan_limited'], [])
        d = S.contract_days({'from': d0, 'to': d0})['days'][0]
        check_day(self, d)
        src = FAKE.data['sleep'][0]['data']
        self.assertEqual(d['sleep']['score'], src['sleep_score'])
        self.assertEqual(d['hrv_ms'], round((src['rmssd_start_avg'] + src['rmssd_end_avg']) / 2, 1))
        self.assertEqual(d['respiratory_rate'], 14)
        self.assertEqual(d['resting_hr_bpm'], src['hr_min'])
        sl = d['sleep']
        self.assertEqual((sl['asleep_h'], sl['in_bed_h'], sl['light_h'], sl['deep_h'], sl['rem_h'], sl['awake_h']), (7.25, 8.5, 4.0, 1.5, 1.75, 0.75))
        self.assertEqual((sl['efficiency_pct'], sl['need_h'], sl['start'], sl['end']),
                         (85, None, (TODAY - dt.timedelta(days=1)).isoformat() + 'T16:00:00.000Z', d0 + 'T00:30:00.000Z'))
        act = FAKE.data['activity'][0]
        self.assertEqual(d['load'], {'label': 'Intensity minutes', 'max': None, 'scale_max': 60, 'unit': 'min', 'value': 30 + 2 * 10})
        self.assertEqual((d['steps'], d['calories_kcal']), (act['steps'], act['totalcalories']))   # the watch's row, not the phone's
        w = d['workouts'][0]
        self.assertEqual((w['sport'], w['minutes'], w['avg_hr_bpm'], w['calories_kcal'], w['distance_m']), ('Run', 40, 131, 650, 11000))
        self.assertIsNone(w['load'])
        self.assertEqual(w['extras']['withings']['hr_zones_min'], {'light': 10.0, 'moderate': 30.0, 'intense': 100.0, 'maximal': 20.0})
        self.assertEqual(d['extras']['withings']['intense_min'], 10.0)

        # ---- paging through the contract, cursor, changed_since
        seen, page, cursors = [], None, set()
        while True:
            r = S.contract_days(dict({'limit': 15}, **({'page': page} if page else {})))
            seen += [x['date'] for x in r['days']]
            cursors.add(r['cursor'])
            page = r['next_page']
            if not page:
                break
        self.assertEqual((len(seen), seen == sorted(set(seen)), len(cursors)), (DAYS, True, 1))
        cursor = cursors.pop()
        S.sync(full=True)
        self.assertEqual(S.contract_days({'changed_since': cursor})['days'], [], 'an unchanged re-sync must not re-emit days')
        FAKE.data['sleep'][3]['data']['sleep_score'] = 33
        FAKE.data['sleep'][3]['modified'] = int(time.time())
        S.sync()
        ch = S.contract_days({'changed_since': cursor})
        self.assertEqual([x['date'] for x in ch['days']], [(TODAY - dt.timedelta(days=3)).isoformat()])
        self.assertEqual(ch['days'][0]['sleep']['score'], 33)
        with self.assertRaises(S.Problem):
            S.contract_days({'changed_since': 'p1.4'})

        # incremental: lastupdate = last sync minus the 48 h overlap
        FAKE.calls.clear()
        S.sync()
        lu = [int(c[2]['lastupdate']) for c in FAKE.calls if c[1] in ('getsummary', 'getactivity', 'getworkouts')]
        self.assertTrue(lu and all(abs((time.time() - x) / 3600 - S.OVERLAP_HOURS) < 0.2 for x in lu), lu)

        # ---- refresh rotation: an expired access token is renewed, the new refresh token saved, the old one dead
        t = S._tokens()
        old = t['refresh_token']
        t['expires_at'] = time.time() - 5
        S._save_tokens(t)
        S.api_call('/v2/measure', {'action': 'getactivity', 'lastupdate': int(time.time())})
        t2 = S._tokens()
        self.assertNotEqual(t2['refresh_token'], old)
        self.assertEqual((t2['refresh_token'], t2['access_token'], t2['userid']), (FAKE.refresh, FAKE.access, '363'))
        self.assertIn(old, FAKE.old_refresh)
        # a 401 from a data service refreshes once and retries
        FAKE.force_auth = 1
        S.api_call('/v2/measure', {'action': 'getactivity', 'lastupdate': int(time.time())})
        self.assertNotEqual(S._tokens()['refresh_token'], t2['refresh_token'])

        # ---- HTTP + MCP
        code, body = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual([t['name'] for t in json.loads(body)['result']['tools']], ['health_data_status', 'health_data_sync', 'health_data_days'])
        for name, args in [('health_data_status', {}), ('health_data_days', {'from': d0}), ('health_data_days', {'changed_since': ''})]:
            code, body = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
            res = json.loads(body)['result']
            self.assertFalse(res['isError'], (name, res))
            for secret in (SECRET, FAKE.access, FAKE.refresh, CID):
                self.assertNotIn(secret, body.decode())
        self.assertEqual(http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'}, {'X-Forwarded-For': '1.2.3.4'})[0], 403)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'evil.example:18997'})[0], 403)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'box.example.ts.net:8796'})[0], 200)
        for path in ('/api/status', '/api/setup', '/api/days', '/wizard-withings/api/status', '/healthz', '/', '/icon.svg'):
            code, body = http(self.port, 'GET', path)
            self.assertEqual(code, 200, path)
            for secret in (SECRET, FAKE.access, FAKE.refresh):
                self.assertNotIn(secret, body.decode('latin-1'), path)
        for hdr in ('X-Forwarded-For', 'Tailscale-User-Login', 'Forwarded', 'X-Real-IP', 'CF-Connecting-IP'):
            self.assertEqual(http(self.port, 'GET', '/api/days', headers={hdr: 'x'})[0], 403, hdr)

        # ---- refresh refused -> needs sign-in, tokens removed, data kept
        while S.SYNC_LOCK.locked():
            time.sleep(0.05)
        FAKE.refresh = 'rotated-elsewhere'
        t = S._tokens(); t['expires_at'] = 0; S._save_tokens(t)
        with self.assertRaises(S.Problem):
            S.api_call('/v2/sleep', {'action': 'getsummary', 'lastupdate': 0})
        st = S.status_info()
        self.assertEqual(st['status'], 'needs_login')
        self.assertFalse((S.DATA / 'tokens.json').exists())
        self.assertEqual(st['counts']['days'], DAYS)
        ds = st['dataset']

        # ---- paste-back with the public placeholder callback, then disconnect: signed revoke, wipe
        http(self.port, 'POST', '/api/keys', {'client_id': CID, 'client_secret': SECRET, 'redirect_uri': S.FALLBACK_REDIRECT})
        start = S.login_start()
        self.assertTrue(start['paste_back'])
        code, body = http(self.port, 'POST', '/api/oauth/finish', {'url': f'{S.FALLBACK_REDIRECT}?code=good-code&state={self.state_of(start)}'})
        self.assertEqual(code, 200, body)
        self.wait_sync()
        while S.SYNC_LOCK.locked():
            time.sleep(0.05)
        access, refresh = FAKE.access, FAKE.refresh
        code, body = http(self.port, 'POST', '/api/disconnect', {'forget_keys': True})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['status'], 'not_configured')
        self.assertEqual(FAKE.revoked, ['363'])
        nonce_call = [c[2] for c in FAKE.calls if c[1] == 'getnonce'][-1]
        self.assertEqual(nonce_call['signature'], sig('getnonce', CID, nonce_call['timestamp']))
        blob = self.files_text()
        for secret in (SECRET, CID, access, refresh):
            self.assertNotIn(secret, blob)
        st = S.status_info()
        self.assertEqual(st['counts'], {'days': 0, 'sleeps': 0, 'workouts': 0})
        self.assertNotEqual(st['dataset'], ds)


class MappingTest(unittest.TestCase):
    """The documented intensity-minute load and the sample objects from Withings' OpenAPI specification."""
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = server

    def test_intensity_minutes(self):
        S = self.S
        self.assertEqual(S.intensity_minutes(1000, 700), 40)          # 16.7 + 2 x 11.7
        self.assertEqual(S.intensity_minutes(1800, 0), 30)
        self.assertEqual(S.intensity_minutes(None, 600), 20)
        self.assertEqual(S.intensity_minutes(0, 0), 0)
        self.assertIsNone(S.intensity_minutes(None, None))

    def test_doc_samples(self):
        S = self.S
        d = S._n_day('2020-07-09', {'sleep': [copy.deepcopy(DOC_SLEEP)], 'activity': [copy.deepcopy(DOC_ACTIVITY)],
                                     'workout': [copy.deepcopy(DOC_WORKOUT)]})
        check_day(self, d)
        self.assertIsNone(d['readiness'])
        sl = d['sleep']
        self.assertEqual((sl['start'], sl['end']), ('2020-07-08T22:00:00.000Z', '2020-07-09T01:13:20.000Z'))
        self.assertEqual((sl['asleep_h'], sl['in_bed_h'], sl['awake_h'], sl['score'], sl['efficiency_pct'], sl['respiratory_rate']),
                         (2.83, 3.22, 0.22, 74, 88, 14))
        self.assertEqual((d['hrv_ms'], d['resting_hr_bpm']), (56.5, 49))
        self.assertEqual(d['load']['value'], 40)
        self.assertEqual((d['steps'], d['calories_kcal'], d['avg_hr_bpm'], d['max_hr_bpm']), (6200, 3500, 80, 80))
        w = d['workouts'][0]
        self.assertEqual((w['sport'], w['minutes'], w['start'], w['end']), ('Other', 183, '2020-07-08T22:00:00.000Z', '2020-07-09T01:13:20.000Z'))
        self.assertEqual(d['extras']['withings']['moderate_min'], 16.7)
        # an external-source night: total asleep only, no stages
        ext = S._n_sleep({'startdate': 1594245600, 'enddate': 1594270800, 'data': {'asleepduration': 21600}})
        self.assertEqual((ext['asleep_h'], ext['in_bed_h'], ext['deep_h']), (6.0, 7.0, None))
        # sleep efficiency as a ratio becomes a percent
        self.assertEqual(S._n_sleep({'data': {'sleep_efficiency': 0.91}})['efficiency_pct'], 91)


DAY_KEYS = {'date', 'updated_at', 'readiness', 'hrv_ms', 'resting_hr_bpm', 'spo2_pct', 'skin_temp_c', 'skin_temp_delta_c', 'respiratory_rate',
            'sleep', 'load', 'calories_kcal', 'energy_kj', 'avg_hr_bpm', 'max_hr_bpm', 'steps', 'workouts', 'extras'}
SLEEP_KEYS = {'start', 'end', 'asleep_h', 'in_bed_h', 'awake_h', 'light_h', 'deep_h', 'rem_h', 'score', 'efficiency_pct', 'respiratory_rate', 'need_h', 'naps'}
WORKOUT_KEYS = {'id', 'sport', 'start', 'end', 'minutes', 'load', 'avg_hr_bpm', 'max_hr_bpm', 'calories_kcal', 'energy_kj', 'distance_m', 'zones_min', 'extras'}


def check_day(t, d):
    """The day record shape promised by docs/health-data-contract.md."""
    t.assertTrue(DAY_KEYS <= set(d), DAY_KEYS - set(d))
    t.assertRegex(d['date'], r'^\d{4}-\d{2}-\d{2}$')
    t.assertIsNone(d['readiness'])                       # Withings has no readiness score; never invent one
    if d['sleep']:
        t.assertTrue(SLEEP_KEYS <= set(d['sleep']), SLEEP_KEYS - set(d['sleep']))
    if d['load']:
        t.assertEqual(set(d['load']), {'value', 'max', 'label', 'scale_max', 'unit'})
        t.assertIsNone(d['load']['max'])                 # open-ended scale
    for w in d['workouts']:
        t.assertTrue(WORKOUT_KEYS <= set(w), WORKOUT_KEYS - set(w))
    t.assertEqual(set(d['extras']), {'withings'})


class IconTest(unittest.TestCase):
    """Own icon: full-colour store square (icon.png from wizard.md) and a silhouette at web/icon.svg the shell tints.
    A generic watch; never Withings' own logo or wordmark."""
    root = ROOT

    def test_icons(self):
        self.assertIn('\nicon: icon.png\n', (self.root / 'wizard.md').read_text())
        png = (self.root / 'icon.png').read_bytes()
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual((int.from_bytes(png[16:20], 'big'), int.from_bytes(png[20:24], 'big')), (1024, 1024))
        mask = (self.root / 'web' / 'icon.svg').read_text()
        self.assertIn('viewBox="0 0 512 512"', mask)
        self.assertNotRegex(mask, r'<text|<image|href=|<script')
        self.assertNotIn('withings', (mask + (self.root / 'icon.svg').read_text()).lower())


if __name__ == '__main__':
    unittest.main()
