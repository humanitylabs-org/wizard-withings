"""Withings: the Withings connector for a Wizard box (contract wizard.health-data/1).

Setup happens on this app's own screen. The owner creates a free "Public API integration" application on the
Withings Developer Dashboard, pastes its Client ID and Client Secret here (stored server-side, never sent back), then
signs in to Withings with OAuth 2.0. Withings refuses localhost and bare IP callback URLs, so the registered callback
is this app's own https address on the box (the app finishes the sign-in by itself, within Withings' 30-second code
lifetime). A public placeholder address with paste-back is the fallback.

Tokens follow the Withings docs: POST https://wbsapi.withings.net/v2/oauth2 with action=requesttoken and the client
secret (the documented "using client secret" variant); access tokens last 3 hours and every refresh returns a new
refresh token that is saved before use (the old one dies 8 hours later). Calls that the docs mark as signed (revoke
on disconnect) use a nonce from Signature v2 - Getnonce and an HMAC-SHA256 signature of the sorted parameter values.

The app keeps a local SQLite copy of sleep summaries (Sleep v2 - Getsummary), daily activity (Measure v2 -
Getactivity) and workouts (Measure v2 - Getworkouts): a backfill after sign-in, then every 20 minutes everything
modified since the last sync minus an overlap (lastupdate), paged with more/offset. Withings' free plan refuses a
whole request that names an advanced field (sleep score, overnight HRV, breathing rate) with status 277; the app then
asks again with the basic fields, remembers that for a week and says so on its screen.

Withings has no readiness score, so `readiness` stays null (Health shows "no score"). The day's load is intensity
minutes (moderate + 2 x intense), on its own scale (`scale_max` 60). Other apps on the box (the Health app) read the
copy through the box-only health-data contract: GET /api/status and GET /api/days (docs/health-data-contract.md).

Stdlib only. Secrets live in DATA_DIR (0600 files, 0700 folder) and are never returned by any API or tool.
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent if '__file__' in globals() else Path(os.environ.get('APP_ROOT', '.')).resolve()
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'wizard-withings'
NAME = 'Withings'
VERSION = '0.1.2'
PREFIX = '/' + APP_ID
PORT = os.environ.get('PORT', '8834')
AUTH_URL = os.environ.get('WITHINGS_AUTH_URL', 'https://account.withings.com/oauth2_user/authorize2')
API_BASE = os.environ.get('WITHINGS_API_BASE', 'https://wbsapi.withings.net').rstrip('/')
TOKEN_URL = API_BASE + '/v2/oauth2'
DASHBOARD_URL = 'https://developer.withings.com/dashboard/'
PLANS_URL = 'https://developer.withings.com/'
SIGNUP_URL = os.environ.get('WITHINGS_SIGNUP_URL', 'https://www.withings.com/account')
FALLBACK_REDIRECT = 'https://example.com/wizard-withings/callback'   # public placeholder; the owner pastes the address back
SUGGESTED_APP_NAME = 'Wizard Health'
SUGGESTED_DESCRIPTION = 'Personal use: my own Wizard box reads my own Withings data, read-only.'
SCOPES = ['user.info', 'user.metrics', 'user.activity']
SCOPE = ','.join(SCOPES)            # Withings scopes are comma-separated
SYNC_MINUTES = float(os.environ.get('WITHINGS_SYNC_MINUTES', '20'))   # Withings asks for no more than one poll per 10 min
STATE_TTL = float(os.environ.get('WITHINGS_STATE_TTL', str(2 * 3600)))
OVERLAP_HOURS = float(os.environ.get('WITHINGS_OVERLAP_HOURS', '48'))
HISTORY_DAYS = int(os.environ.get('WITHINGS_HISTORY_DAYS', '365'))
PLAN_RECHECK = float(os.environ.get('WITHINGS_PLAN_RECHECK', str(7 * 86400)))
REQUEST_GAP = float(os.environ.get('WITHINGS_REQUEST_GAP', '0.6'))   # 120 requests a minute per application
RETRIES = int(os.environ.get('WITHINGS_RETRIES', '4'))
BACKOFF = float(os.environ.get('WITHINGS_BACKOFF', '5'))
UA = f'{APP_ID}/{VERSION} (+Wizard OS)'
STATIC = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
FORWARDED = ('tailscale-user-login', 'x-forwarded-for', 'forwarded', 'x-real-ip', 'cf-connecting-ip')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
AUTH_FAILED = {100, 101, 102, 200, 401}     # Withings' "Authentication failed" family
PLAN_REFUSED = 277                          # a requested data type is outside the developer app's plan
RATE_LIMITED = 601
LOCK = threading.RLock()
TOKEN_LOCK = threading.Lock()               # one refresh at a time: Withings refresh tokens rotate
SYNC_LOCK = threading.Lock()
WAKE = threading.Event()

SLEEP_BASIC = ['total_timeinbed', 'total_sleep_time', 'asleepduration', 'lightsleepduration', 'remsleepduration', 'deepsleepduration',
               'sleep_efficiency', 'sleep_latency', 'wakeup_latency', 'wakeupduration', 'wakeupcount', 'waso', 'nb_rem_episodes',
               'out_of_bed_count', 'hr_average', 'hr_min', 'hr_max']
SLEEP_ADVANCED = ['sleep_score', 'rmssd_start_avg', 'rmssd_end_avg', 'rr_average', 'rr_min', 'rr_max',
                  'breathing_disturbances_intensity', 'snoring', 'snoringepisodecount']
ACTIVITY_BASIC = ['steps', 'distance', 'elevation', 'soft', 'moderate', 'intense', 'active', 'calories', 'totalcalories']
ACTIVITY_ADVANCED = ['hr_average', 'hr_min', 'hr_max', 'hr_zone_0', 'hr_zone_1', 'hr_zone_2', 'hr_zone_3']
WORKOUT_BASIC = ['calories', 'hr_average', 'hr_min', 'hr_max', 'steps', 'distance', 'elevation', 'intensity', 'pause_duration']
WORKOUT_ADVANCED = ['hr_zone_0', 'hr_zone_1', 'hr_zone_2', 'hr_zone_3', 'spo2_average', 'manual_distance', 'manual_calories']
# (kind, service path, action, list key, basic fields, advanced fields)
COLLECTIONS = [('sleep', '/v2/sleep', 'getsummary', 'series', SLEEP_BASIC, SLEEP_ADVANCED),
               ('activity', '/v2/measure', 'getactivity', 'activities', ACTIVITY_BASIC, ACTIVITY_ADVANCED),
               ('workout', '/v2/measure', 'getworkouts', 'series', WORKOUT_BASIC, WORKOUT_ADVANCED)]
CATEGORIES = {1: 'Walk', 2: 'Run', 3: 'Hiking', 4: 'Skating', 5: 'BMX', 6: 'Bicycling', 7: 'Swimming', 8: 'Surfing', 9: 'Kitesurfing',
              10: 'Windsurfing', 11: 'Bodyboard', 12: 'Tennis', 13: 'Table tennis', 14: 'Squash', 15: 'Badminton', 16: 'Lift weights',
              17: 'Fitness', 18: 'Elliptical', 19: 'Pilates', 20: 'Basketball', 21: 'Soccer', 22: 'Football', 23: 'Rugby', 24: 'Volleyball',
              25: 'Waterpolo', 26: 'Horse riding', 27: 'Golf', 28: 'Yoga', 29: 'Dancing', 30: 'Boxing', 31: 'Fencing', 32: 'Wrestling',
              33: 'Martial arts', 34: 'Skiing', 35: 'Snowboarding', 36: 'Other', 128: 'No activity', 187: 'Rowing', 188: 'Zumba',
              191: 'Baseball', 192: 'Handball', 193: 'Hockey', 194: 'Ice hockey', 195: 'Climbing', 196: 'Ice skating', 272: 'Multi-sport',
              306: 'Indoor walk', 307: 'Indoor running', 308: 'Indoor cycling', 455: 'Stand-up paddleboarding', 456: 'Padel',
              498: 'Trail running', 523: 'Mountain biking', 546: 'Crossfit', 547: 'Spin class', 550: 'HIIT', 551: 'Meditation',
              552: 'Stretching', 566: 'Nordic walking', 570: 'Jump rope', 595: 'Indoor rowing'}


class Problem(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class ProviderError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------------- private storage ----------------

def _private():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    return DATA


def _write_secret(name, text):
    p = _private() / name
    tmp = p.with_name('.' + name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _read(name):
    try:
        return json.loads((DATA / name).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _shred(name):
    p = DATA / name
    try:
        size = p.stat().st_size
        with open(p, 'r+b') as f:
            f.write(b'\0' * size)
            f.flush()
            os.fsync(f.fileno())
        p.unlink()
    except FileNotFoundError:
        pass


def _keys():
    return _read('client.json')


def _tokens():
    return _read('tokens.json')


def _save_tokens(t):
    with LOCK:
        if t:
            _write_secret('tokens.json', json.dumps(t))
        else:
            _shred('tokens.json')


def _state():
    return _read('state.json')


def _update_state(**kv):
    with LOCK:
        s = _state()
        for k, v in kv.items():
            if v is None:
                s.pop(k, None)
            else:
                s[k] = v
        _write_secret('state.json', json.dumps(s))
        return s


def secure_files():
    """Runtipi loosens app-data permissions; tighten them again on every start."""
    db().close()
    for p in DATA.iterdir():
        if p.is_file():
            os.chmod(p, 0o600)


def db():
    path = _private() / 'withings.sqlite3'
    fresh = not path.exists()
    c = sqlite3.connect(path, timeout=15)
    if fresh:
        os.chmod(path, 0o600)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, day TEXT, start TEXT, data TEXT NOT NULL,
        changed_seq INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (kind, id));
      CREATE INDEX IF NOT EXISTS records_day ON records(kind, day);
      CREATE INDEX IF NOT EXISTS records_changed ON records(changed_seq);
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, data TEXT NOT NULL);
    ''')
    return c


def _next_seq(c):
    n = int(json.loads((c.execute("SELECT data FROM meta WHERE key='seq'").fetchone() or ['0'])[0])) + 1
    c.execute("INSERT OR REPLACE INTO meta (key,data) VALUES ('seq',?)", (str(n),))
    return n


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _ts_iso(ts):
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    return _iso(dt.datetime.fromtimestamp(ts, dt.timezone.utc))


def _parse(s):
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _ago(iso):
    t = _parse(iso)
    return (_now() - t).total_seconds() if t else 1e12


# ---------------- Withings HTTP ----------------

def _http(url, form, headers=None, timeout=45):
    h = {'User-Agent': UA, 'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'}
    h.update(headers or {})
    req = urllib.request.Request(url, data=urlencode(form).encode(), method='POST', headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderError(0, 'unreachable', 'Withings did not answer. Check the box is online; the app will retry.') from None


def _envelope(raw):
    """Withings answers HTTP 200 with {"status": <code>, "body": {...}, "error": "..."}; status 0 = success."""
    try:
        d = json.loads(raw or b'{}')
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    st = d.get('status')
    return (st if isinstance(st, int) else -1), (d.get('body') if isinstance(d.get('body'), dict) else {}), str(d.get('error') or '')


def _token_call(form):
    k = _keys()
    if not k.get('client_id') or not k.get('client_secret'):
        raise Problem(409, 'Save your Withings Client ID and Client Secret first (step 4).')
    form = dict(form, action='requesttoken', client_id=k['client_id'], client_secret=k['client_secret'])
    http_status, raw, _ = _http(TOKEN_URL, form)
    status, body, err = _envelope(raw)
    return (status if http_status == 200 else http_status), body, err


def _sign(values):
    """Withings signature: HMAC-SHA256 (client secret) of the parameter values sorted by key, joined with commas."""
    k = _keys()
    msg = ','.join(str(values[key]) for key in sorted(values))
    return hmac.new(k['client_secret'].encode(), msg.encode(), hashlib.sha256).hexdigest()


def _nonce():
    """Signature v2 - Getnonce: a single-use token, valid 30 minutes, for calls that must be signed."""
    k = _keys()
    p = {'action': 'getnonce', 'client_id': k['client_id'], 'timestamp': int(time.time())}
    p['signature'] = _sign(p)
    _, raw, _ = _http(API_BASE + '/v2/signature', p, timeout=15)
    status, body, _ = _envelope(raw)
    if status != 0 or not body.get('nonce'):
        raise ProviderError(status, 'nonce_failed', 'Withings did not give a nonce.')
    return body['nonce']


def test_keys():
    """Exchange a made-up code. With a real client Withings complains about the code; with a wrong one, about the client."""
    k = _keys()
    if not k.get('client_id') or not k.get('client_secret'):
        raise Problem(409, 'Save your Withings Client ID and Client Secret first.')
    status, _, err = _token_call({'grant_type': 'authorization_code', 'code': 'wizard-key-check-' + secrets.token_hex(8),
                                  'redirect_uri': k.get('redirect_uri') or FALLBACK_REDIRECT})
    e = err.lower()
    if 'client' in e or 'secret' in e:
        result, msg = 'invalid', 'Withings did not accept this Client ID and Client Secret. Copy both again from your Withings application and save.'
    elif 'code' in e or 'grant' in e or 'authorization' in e:
        result, msg = 'valid', 'Withings recognised your Client ID and Client Secret. You can connect now.'
    elif 'redirect' in e or 'callback' in e:
        result, msg = 'unknown', 'Withings recognised the keys but not the callback URL. Check it matches the Registered URL in your Withings application.'
    elif status == RATE_LIMITED:
        result, msg = 'unknown', 'Withings is busy right now. Try the test again in a minute.'
    else:
        result, msg = 'unknown', 'Withings gave an unexpected answer, so the keys could not be confirmed. Try again shortly.'
    with LOCK:
        k = _keys()
        if k:
            k.update(verified=result, checked_at=_iso(_now()))
            _write_secret('client.json', json.dumps(k))
    return {'result': result, 'message': msg}


def save_keys(body):
    cid = str(body.get('client_id') or '').strip()
    secret = str(body.get('client_secret') or '').strip()
    redirect = str(body.get('redirect_uri') or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9._~-]{8,200}', cid):
        raise Problem(400, 'That Client ID does not look right. Copy it again from your Withings application.')
    if not re.fullmatch(r'[\x21-\x7e]{8,300}', secret):
        raise Problem(400, 'That Client Secret does not look right. Copy it again from your Withings application.')
    u = urlsplit(redirect)
    if u.scheme != 'https' or not u.hostname or len(redirect) > 255:
        raise Problem(400, 'The callback URL must be the exact https address you registered in your Withings application.')
    if u.hostname in ('localhost', '127.0.0.1') or re.fullmatch(r'[\d.]+', u.hostname) or ':' in u.hostname:
        raise Problem(400, 'Withings refuses localhost and IP addresses as callback URLs. Use this box\u2019s https address.')
    with LOCK:
        old = _keys()
        changed = old.get('client_id') != cid or old.get('client_secret') != secret
        _write_secret('client.json', json.dumps({'client_id': cid, 'client_secret': secret, 'redirect_uri': redirect,
                                                 'saved_at': _iso(_now()), 'verified': None}))
        if changed and _tokens():
            _save_tokens(None)
            _update_state(auth='needs_login', pending=None)
    return setup_info()


def forget_keys():
    with LOCK:
        _shred('client.json')
        _save_tokens(None)
        _update_state(pending=None, auth=None)
    return setup_info()


def _mask(v):
    v = str(v or '')
    return ('•' * 6 + v[-4:]) if len(v) > 8 else '•' * 6


def setup_info():
    k = _keys()
    return {'dashboard_url': DASHBOARD_URL, 'signup_url': SIGNUP_URL, 'plans_url': PLANS_URL,
            'suggested_name': SUGGESTED_APP_NAME, 'suggested_description': SUGGESTED_DESCRIPTION,
            'default_redirect': None, 'allow_localhost': False, 'fallback_redirect': FALLBACK_REDIRECT,
            'fallback_label': 'Use a public placeholder address instead (paste back within 30 seconds)',
            'redirect_note': 'Withings will send you to this placeholder page after you allow access. Copy its whole address and paste it into step 5 within 30 seconds.',
            'paste_hint': 'Withings then opens the placeholder page. Copy its whole address and paste it below within 30 seconds (Withings codes expire fast).',
            'scopes': SCOPES, 'scope_separator': ',',
            'keys_saved': bool(k.get('client_id') and k.get('client_secret')),
            'client_id_hint': _mask(k.get('client_id')) if k.get('client_id') else None, 'secret_saved': bool(k.get('client_secret')),
            'redirect_uri': k.get('redirect_uri'), 'verified': k.get('verified'), 'checked_at': k.get('checked_at'),
            'connected': bool(_tokens().get('refresh_token') or _tokens().get('access_token'))}


# ---------------- OAuth sign-in ----------------

def login_start():
    k = _keys()
    if not k.get('client_id') or not k.get('client_secret'):
        raise Problem(409, 'Save your Withings Client ID and Client Secret first (step 4).')
    state = secrets.token_hex(16)
    with LOCK:
        pend = [p for p in (_state().get('pending') or []) if _ago(p.get('at')) < STATE_TTL][-4:]
        pend.append({'state': state, 'at': _iso(_now())})
        _update_state(pending=pend)
    q = {'response_type': 'code', 'client_id': k['client_id'], 'scope': SCOPE, 'redirect_uri': k['redirect_uri'], 'state': state}
    return {'authorize_url': AUTH_URL + '?' + urlencode(q), 'redirect_uri': k['redirect_uri'],
            # the app finishes by itself only when the callback is its own address; anything else is pasted back
            'paste_back': not urlsplit(k['redirect_uri']).path.rstrip('/').endswith(PREFIX + '/callback') or
                          urlsplit(k['redirect_uri']).hostname == urlsplit(FALLBACK_REDIRECT).hostname}


def login_finish(pasted):
    raw = str(pasted or '').strip()
    if not raw or len(raw) > 4096:
        raise Problem(400, 'Paste the whole address from the browser bar after you allowed access.')
    u = urlsplit(raw if '://' in raw else 'http://x/?' + raw.lstrip('?'))
    q = {a: b[0] for a, b in parse_qs(u.query).items()}
    if q.get('error'):
        _update_state(pending=None)
        msg = 'You chose not to allow access.' if q['error'] == 'access_denied' else 'Withings did not finish the sign-in.'
        raise Problem(400, msg + ' Tap Connect with Withings to try again.')
    code, state = q.get('code'), q.get('state')
    if not code or not state:
        raise Problem(400, 'That address has no sign-in code. Paste the whole address, starting with https.')
    with LOCK:
        pend = [p for p in (_state().get('pending') or []) if _ago(p.get('at')) < STATE_TTL]
        if not any(secrets.compare_digest(p['state'], state) for p in pend):
            raise Problem(400, 'This sign-in link expired or was already used. Tap Connect with Withings to start again.')
        _update_state(pending=[p for p in pend if p['state'] != state])
    k = _keys()
    status, body, err = _token_call({'grant_type': 'authorization_code', 'code': code, 'redirect_uri': k.get('redirect_uri')})
    if status != 0 or not body.get('access_token'):
        e = err.lower()
        if 'client' in e or 'secret' in e:
            raise Problem(400, 'Withings did not accept the saved Client ID and Client Secret. Replace the keys and try again.')
        if status == RATE_LIMITED:
            raise Problem(429, 'Withings is busy right now. Tap Connect with Withings again in a minute.')
        raise Problem(400, 'Withings refused this code. Codes last only 30 seconds and work once, and the callback URL must match. Tap Connect with Withings again.')
    _store_tokens(body)
    _update_state(auth='connected', last_error=None, connected_at=_iso(_now()))
    start_sync_thread(full=not _state().get('backfilled'))
    return status_info()


def _store_tokens(d, old=None):
    t = {'access_token': d['access_token'], 'refresh_token': d.get('refresh_token') or (old or {}).get('refresh_token'),
         'expires_at': time.time() + int(d.get('expires_in') or 10800), 'scope': d.get('scope') or (old or {}).get('scope') or SCOPE,
         'userid': str(d.get('userid') or (old or {}).get('userid') or '')}
    _save_tokens(t)
    return t


def _access_token(force=False):
    with TOKEN_LOCK:
        t = _tokens()
        if not t.get('access_token') and not t.get('refresh_token'):
            raise Problem(409, 'Not connected to Withings. Open the Withings app and tap Connect with Withings.')
        if not force and t.get('access_token') and t.get('expires_at', 0) - 120 > time.time():
            return t['access_token']
        if not t.get('refresh_token'):
            _needs_login('Withings sign-in expired. Connect again in the Withings app.')
        status, body, err = _token_call({'grant_type': 'refresh_token', 'refresh_token': t['refresh_token']})
        if status == 0 and body.get('access_token'):
            return _store_tokens(body, t)['access_token']   # persist the rotated refresh token before using it
        if status in AUTH_FAILED or status == 503 or 'refresh' in err.lower() or 'token' in err.lower():
            _needs_login('Withings ended this sign-in. Connect again in the Withings app.')
        raise ProviderError(status, 'refresh_failed', 'Withings could not renew the sign-in right now. The app will retry.')


def _needs_login(msg):
    _save_tokens(None)
    _update_state(auth='needs_login', last_error=msg)
    raise Problem(401, msg)


def api_call(path, params):
    """POST a Withings data service with the bearer token. Returns the body, or raises ProviderError(277) for plan refusals."""
    refreshed, status = False, 0
    for attempt in range(RETRIES + 1):
        http_status, raw, _ = _http(API_BASE + path, params, headers={'Authorization': 'Bearer ' + _access_token()})
        status, body, err = _envelope(raw)
        if http_status == 429:
            status = RATE_LIMITED
        if status == 0:
            return body
        if status in AUTH_FAILED and not refreshed:
            refreshed = True
            _access_token(force=True)
            continue
        if status in AUTH_FAILED:
            _needs_login('Withings ended this sign-in. Connect again in the Withings app.')
        if status == PLAN_REFUSED:
            raise ProviderError(PLAN_REFUSED, 'plan', err or 'Not in this developer app\u2019s Withings plan.')
        if status == RATE_LIMITED or http_status >= 500 or status in (522, 2555):
            if attempt >= RETRIES:
                break
            time.sleep(min(120.0, BACKOFF * (2 ** attempt)))
            continue
        raise ProviderError(status, 'api_error', f'Withings answered {status} for {params.get("action")}: {err[:120]}')
    if status == RATE_LIMITED:
        _update_state(rate_limited_until=time.time() + 900)
        raise ProviderError(429, 'rate_limited', 'Withings asked the app to slow down. Syncing resumes in about 15 minutes.')
    raise ProviderError(status, 'api_error', f'Withings answered {status}. The app will retry.')


# ---------------- sync ----------------

def _rec_key(kind, r):
    if kind == 'activity':
        d = str(r.get('date') or '')
        return f"{d}|{r.get('hash_deviceid') or r.get('deviceid') or r.get('brand') or ''}", d, d
    rid = r.get('id')
    if rid is None:
        rid = f"{r.get('startdate')}|{r.get('model_id', r.get('model'))}"
    return str(rid), str(r.get('date') or '')[:10] or None, _ts_iso(r.get('startdate'))


def _save(kind, rows):
    """Upsert Withings records; a row's changed_seq moves only when its data really changed."""
    with LOCK:
        c = db()
        with c:
            seq = _next_seq(c)
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                rid, day, start = _rec_key(kind, r)
                if not rid or not day or not DATE_RE.match(day):
                    continue
                if kind == 'sleep' and r.get('completed') is False:
                    continue   # a night still being recorded; it comes again once complete
                c.execute('''INSERT INTO records (kind,id,day,start,data,changed_seq) VALUES (?,?,?,?,?,?)
                             ON CONFLICT(kind,id) DO UPDATE SET day=excluded.day, start=excluded.start,
                               changed_seq=CASE WHEN records.data IS excluded.data THEN records.changed_seq ELSE excluded.changed_seq END,
                               data=excluded.data''',
                          (kind, rid, day, start, json.dumps(r, sort_keys=True), seq))
        c.close()


def _plan_limited(kind):
    p = (_state().get('plan_limited') or {}).get(kind)
    return bool(p) and time.time() - p < PLAN_RECHECK


def _pull(kind, path, action, key, basic, advanced, since_ts):
    """Everything modified since since_ts, paged with more/offset. Advanced fields first unless the plan refused them."""
    fields = basic if _plan_limited(kind) else basic + advanced
    n, offset = 0, None
    for _ in range(10000):
        p = {'action': action, 'lastupdate': int(since_ts), 'data_fields': ','.join(fields)}
        if offset:
            p['offset'] = offset
        try:
            body = api_call(path, p)
        except ProviderError as e:
            if e.status != PLAN_REFUSED or fields == basic:
                raise
            fields = basic   # the free plan refuses the whole request; ask again without the advanced fields
            with LOCK:
                pl = dict(_state().get('plan_limited') or {}, **{kind: time.time()})
                _update_state(plan_limited=pl)
            continue
        rows = body.get(key) or []
        _save(kind, rows)
        n += len(rows)
        _update_state(progress={'kind': kind, 'records': n})
        more = body.get('more')
        offset = body.get('offset')
        if not more or more in (0, '0', False) or not offset:
            break
        time.sleep(REQUEST_GAP)
    if fields != basic and _state().get('plan_limited', {}).get(kind):
        with LOCK:
            pl = dict(_state().get('plan_limited') or {})
            pl.pop(kind, None)
            _update_state(plan_limited=pl)
    return n


def sync(full=False):
    if not SYNC_LOCK.acquire(blocking=False):
        return dict(status_info(), note='A sync is already running.')
    try:
        st = _state()
        if (st.get('rate_limited_until') or 0) > time.time():
            raise ProviderError(429, 'rate_limited', 'Withings asked the app to slow down. Syncing resumes shortly.')
        full = full or not st.get('backfilled')
        _update_state(syncing=_iso(_now()), sync_kind='backfill' if full else 'incremental')
        if full:
            since = (_now() - dt.timedelta(days=HISTORY_DAYS)).timestamp()
        else:
            since = ((_parse(st.get('last_sync')) or _now()) - dt.timedelta(hours=OVERLAP_HOURS)).timestamp()
        counts = {}
        for kind, path, action, key, basic, adv in COLLECTIONS:
            counts[kind] = _pull(kind, path, action, key, basic, adv, since)
            time.sleep(REQUEST_GAP)
        _update_state(last_sync=_iso(_now()), last_error=None, backfilled=True, auth='connected', syncing=None, progress=None,
                      sync_kind=None, last_counts=counts, rate_limited_until=None)
        return dict(status_info(), pulled=counts)
    except (Problem, ProviderError) as e:
        _update_state(last_error=e.message, syncing=None, progress=None, sync_kind=None)
        raise
    finally:
        SYNC_LOCK.release()


def _safe_sync(full=False):
    try:
        sync(full)
    except (Problem, ProviderError):
        pass
    except Exception:
        _update_state(last_error='Sync failed unexpectedly; it will retry.', syncing=None, progress=None)


def start_sync_thread(full=False):
    threading.Thread(target=_safe_sync, args=(full,), daemon=True).start()


def _scheduler():
    time.sleep(5)
    while True:
        st = _state()
        if _tokens().get('refresh_token') or _tokens().get('access_token'):
            due = not st.get('backfilled') or _ago(st.get('last_sync')) >= SYNC_MINUTES * 60
            if due and (st.get('rate_limited_until') or 0) <= time.time():
                _safe_sync(full=not st.get('backfilled'))
        WAKE.wait(60)
        WAKE.clear()


# ---------------- the health-data contract (wizard.health-data/1, see docs/health-data-contract.md) ----------------

CONTRACT = 'wizard.health-data/1'
SOURCE = {'id': APP_ID, 'provider': 'withings', 'name': NAME, 'app_path': PREFIX + '/'}
# Intensity minutes are open-ended (max None): Health reads them as "34 intensity minutes". scale_max 60 only sets the
# drawing scale and Health's target fractions; with no readiness from Withings, Health greys activity matching anyway.
LOAD = {'label': 'Intensity minutes', 'max': None, 'scale_max': 60, 'unit': 'min'}
SCALES = {'readiness': None,
          'sleep_score': {'label': 'Sleep score', 'min': 0, 'max': 100},
          'load': {'label': 'Intensity minutes', 'min': 0, 'max': None, 'scale_max': 60, 'unit': 'min', 'formula': 'withings-intensity-minutes/1',
                   'goal': 22, 'goal_note': 'about 150 moderate-equivalent minutes a week (WHO), 22 a day'}}


def _r(v, n=1):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return round(v) if n == 0 else round(v, n)


def _hours(s):
    return round(s / 3600, 2) if isinstance(s, (int, float)) and not isinstance(s, bool) else None


def _mins(s):
    return round(s / 60, 1) if isinstance(s, (int, float)) and not isinstance(s, bool) else None


def intensity_minutes(moderate_s, intense_s):
    """Withings activity -> the day's load (formula withings-intensity-minutes/1): moderate minutes + 2 x intense
    minutes, the usual moderate-equivalent count (one vigorous minute counts as two). None when Withings sent neither."""
    if not any(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (moderate_s, intense_s)):
        return None
    return int(round((moderate_s or 0) / 60 + 2 * (intense_s or 0) / 60))


def _dataset():
    st = _state()
    if not st.get('dataset'):
        st = _update_state(dataset=secrets.token_hex(12))
    return st['dataset']


def _n_sleep(s, main=True):
    d = s.get('data') or {}
    parts = [d.get(k) for k in ('lightsleepduration', 'deepsleepduration', 'remsleepduration') if isinstance(d.get(k), (int, float))]
    asleep = d.get('total_sleep_time')
    if not isinstance(asleep, (int, float)):
        asleep = d.get('asleepduration') if isinstance(d.get('asleepduration'), (int, float)) else (sum(parts) if parts else None)
    a, b = s.get('startdate'), s.get('enddate')
    in_bed = d.get('total_timeinbed')
    if not isinstance(in_bed, (int, float)) and isinstance(a, (int, float)) and isinstance(b, (int, float)):
        in_bed = b - a
    eff = d.get('sleep_efficiency')
    out = {'start': _ts_iso(a), 'end': _ts_iso(b), 'asleep_h': _hours(asleep), 'in_bed_h': _hours(in_bed),
           'awake_h': _hours(d.get('wakeupduration') if d.get('wakeupduration') is not None else d.get('waso')),
           'light_h': _hours(d.get('lightsleepduration')), 'deep_h': _hours(d.get('deepsleepduration')), 'rem_h': _hours(d.get('remsleepduration')),
           'score': _r(d.get('sleep_score'), 0),
           'efficiency_pct': _r(eff * 100 if isinstance(eff, (int, float)) and eff <= 1 else eff, 0),
           'respiratory_rate': _r(d.get('rr_average'))}
    if main:
        out['need_h'] = None   # Withings gives no sleep need; Health falls back to hours
    return out


def _hrv(d):
    v = [x for x in (d.get('rmssd_start_avg'), d.get('rmssd_end_avg')) if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return round(sum(v) / len(v), 1) if v else None


def _n_workout(r):
    d = r.get('data') or {}
    a, b = r.get('startdate'), r.get('enddate')
    pause = d.get('pause_duration') if isinstance(d.get('pause_duration'), (int, float)) else 0
    mins = round(((b - a) - pause) / 60) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
    zones = {k: _mins(d.get(f'hr_zone_{i}')) for i, k in enumerate(('light', 'moderate', 'intense', 'maximal'))}
    return {'id': str(r.get('id')), 'sport': CATEGORIES.get(r.get('category'), 'Workout'), 'start': _ts_iso(a), 'end': _ts_iso(b),
            'minutes': mins, 'load': None, 'avg_hr_bpm': d.get('hr_average'), 'max_hr_bpm': d.get('hr_max'),
            'calories_kcal': _r(d.get('calories'), 0), 'energy_kj': None,
            'distance_m': _r(d.get('distance') if d.get('distance') else d.get('manual_distance'), 0),
            'zones_min': None,   # Withings has four zones of its own (light..maximal), kept in extras
            'extras': {'withings': {'category': r.get('category'), 'steps': d.get('steps'), 'elevation': d.get('elevation'),
                                    'intensity': d.get('intensity'), 'spo2_average': d.get('spo2_average'), 'hr_min_bpm': d.get('hr_min'),
                                    'hr_zones_min': zones if any(v is not None for v in zones.values()) else None}}}


def _pick_activity(rows):
    """One activity row per date: Withings can list several sources; keep the tracker's, else the one with most steps."""
    if not rows:
        return None
    return sorted(rows, key=lambda x: (bool(x.get('is_tracker')), x.get('steps') or 0), reverse=True)[0]


def _n_day(day, rows):
    sleeps = sorted(rows.get('sleep') or [], key=lambda s: ((s.get('data') or {}).get('total_sleep_time') or 0), reverse=True)
    main = sleeps[0] if sleeps else None
    act = _pick_activity(rows.get('activity') or []) or {}
    works = sorted(rows.get('workout') or [], key=lambda w: w.get('startdate') or 0)
    md = (main or {}).get('data') or {}
    sleep = None
    if main:
        sleep = _n_sleep(main)
        sleep['naps'] = [_n_sleep(s, main=False) for s in sleeps[1:]]
    im = intensity_minutes(act.get('moderate'), act.get('intense'))
    mods = [x.get('modified') for x in [main or {}, act] + works if isinstance(x.get('modified'), (int, float))]
    plan = _state().get('plan_limited') or {}
    return {
        'date': day, 'updated_at': _ts_iso(max(mods)) if mods else None, 'readiness': None,
        'hrv_ms': _hrv(md), 'resting_hr_bpm': _r(md.get('hr_min'), 0),
        'spo2_pct': None, 'skin_temp_c': None, 'skin_temp_delta_c': None,
        'respiratory_rate': _r(md.get('rr_average')), 'sleep': sleep,
        'load': dict(LOAD, value=im) if im is not None else None,
        'calories_kcal': _r(act.get('totalcalories'), 0), 'energy_kj': None,
        'avg_hr_bpm': act.get('hr_average'), 'max_hr_bpm': act.get('hr_max'), 'steps': act.get('steps'),
        'workouts': [_n_workout(w) for w in works],
        'extras': {'withings': {
            'load_formula': 'withings-intensity-minutes/1' if im is not None else None,
            'soft_min': _mins(act.get('soft')), 'moderate_min': _mins(act.get('moderate')), 'intense_min': _mins(act.get('intense')),
            'active_calories_kcal': _r(act.get('calories'), 0), 'distance_m': _r(act.get('distance'), 0), 'floors': act.get('elevation'),
            'sleep_hr_average_bpm': md.get('hr_average'), 'sleep_hr_max_bpm': md.get('hr_max'),
            'hrv_rmssd_start_ms': md.get('rmssd_start_avg'), 'hrv_rmssd_end_ms': md.get('rmssd_end_avg'),
            'sleep_latency_min': _mins(md.get('sleep_latency')), 'wakeup_count': md.get('wakeupcount'), 'waso_min': _mins(md.get('waso')),
            'out_of_bed_count': md.get('out_of_bed_count'), 'rem_episodes': md.get('nb_rem_episodes'),
            'snoring_min': _mins(md.get('snoring')), 'breathing_disturbances': md.get('breathing_disturbances_intensity'),
            'sleep_device_model': (main or {}).get('model_id'), 'activity_source_tracker': act.get('is_tracker'),
            'plan_limited': sorted(k for k, v in plan.items() if v and time.time() - v < PLAN_RECHECK) or None}},
    }


def _build_days(dates):
    if not dates:
        return []
    with LOCK:
        c = db()
        try:
            marks = ','.join('?' * len(dates))
            by = {}
            for x in c.execute(f'SELECT kind, day, data FROM records WHERE day IN ({marks}) ORDER BY start', list(dates)):
                by.setdefault(x['day'], {}).setdefault(x['kind'], []).append(json.loads(x['data']))
        finally:
            c.close()
    return [_n_day(day, by.get(day, {})) for day in dates]


def _token(d):
    return base64.urlsafe_b64encode(json.dumps(d, separators=(',', ':')).encode()).decode().rstrip('=')


def _untoken(s):
    try:
        d = json.loads(base64.urlsafe_b64decode(s + '=' * (-len(s) % 4)))
        if isinstance(d, dict):
            return d
    except (ValueError, TypeError):
        pass
    raise Problem(400, 'page is not a page token from this source.')


def _cursor_seq(s):
    if s in (None, ''):
        return 0
    m = re.fullmatch(r'n1\.(\d{1,15})', str(s))
    if not m:
        raise Problem(400, 'changed_since is not a cursor from this source.')
    return int(m.group(1))


def _seq_now(c):
    row = c.execute("SELECT data FROM meta WHERE key='seq'").fetchone()
    return int(json.loads(row['data'])) if row else 0


def _check_day(v, name):
    if v is None or v == '':
        return None
    if not DATE_RE.match(str(v)):
        raise Problem(400, f'{name} must be YYYY-MM-DD.')
    return str(v)


def contract_days(args):
    """GET /api/days: normalized day records, oldest first, paged; optionally only days changed since a cursor."""
    frm, to = _check_day(args.get('from'), 'from'), _check_day(args.get('to'), 'to')
    if frm and to and frm > to:
        raise Problem(400, 'from must be on or before to.')
    try:
        limit = max(1, min(500, int(args.get('limit') or 100)))
    except (TypeError, ValueError):
        raise Problem(400, 'limit must be a number.') from None
    page = _untoken(str(args['page'])) if args.get('page') else {}
    changed = 'changed_since' in args
    since = _cursor_seq(args.get('changed_since')) if changed else 0
    after = page.get('after') or ''
    with LOCK:
        c = db()
        try:
            hw = int(page.get('hw') or _seq_now(c))
            q, p = ['SELECT DISTINCT day FROM records WHERE day IS NOT NULL AND day>?'], [after]
            if frm:
                q.append('AND day>=?'); p.append(frm)
            if to:
                q.append('AND day<=?'); p.append(to)
            if changed:
                q.append('AND day IN (SELECT day FROM records WHERE changed_seq>? AND changed_seq<=?)')
                p += [since, hw]
            q.append('ORDER BY day LIMIT ?'); p.append(limit + 1)
            dates = [r['day'] for r in c.execute(' '.join(q), p)]
        finally:
            c.close()
    more = len(dates) > limit
    dates = dates[:limit]
    return {'contract': CONTRACT, 'source': APP_ID, 'dataset': _dataset(), 'days': _build_days(dates),
            'next_page': _token({'after': dates[-1], 'hw': hw}) if more else None, 'cursor': f'n1.{hw}'}


def _counts():
    with LOCK:
        c = db()
        out = {r['kind']: r['n'] for r in c.execute('SELECT kind, COUNT(*) n FROM records GROUP BY kind')}
        span = c.execute('SELECT MIN(day) a, MAX(day) b, COUNT(DISTINCT day) n FROM records').fetchone()
        c.close()
    return out, span['a'], span['b'], span['n']


def status_info():
    k, t, st = _keys(), _tokens(), _state()
    keys = bool(k.get('client_id') and k.get('client_secret'))
    if not (t.get('refresh_token') or t.get('access_token')):
        state = 'needs_login' if keys and st.get('auth') == 'needs_login' else 'not_configured'
    elif st.get('syncing') and _ago(st.get('syncing')) < 7200:
        state = 'syncing'
    elif (st.get('rate_limited_until') or 0) > time.time():
        state = 'rate_limited'
    else:
        state = 'connected'
    counts, first, last, ndays = _counts()
    nxt = None
    if state in ('connected', 'rate_limited') and st.get('last_sync'):
        nxt = _iso(_parse(st['last_sync']) + dt.timedelta(minutes=SYNC_MINUTES))
    live = state in ('connected', 'syncing', 'rate_limited')
    limited = sorted(x for x, v in (st.get('plan_limited') or {}).items() if v and time.time() - v < PLAN_RECHECK)
    return {
        'contract': CONTRACT, 'source': SOURCE, 'status': state, 'version': VERSION,
        'account': {'name': None} if live or counts else None, 'dataset': _dataset(),
        'last_sync': st.get('last_sync'), 'next_sync': nxt, 'sync_every_minutes': SYNC_MINUTES,
        'history_from': first, 'latest_date': last,
        'counts': {'days': ndays, 'sleeps': counts.get('sleep', 0), 'workouts': counts.get('workout', 0)},
        'error': st.get('last_error'), 'scales': SCALES,
        'setup': {'keys_saved': keys, 'keys_verified': k.get('verified'), 'redirect_uri': k.get('redirect_uri') if keys else None,
                  'backfilled': bool(st.get('backfilled')), 'sync_kind': st.get('sync_kind') if state == 'syncing' else None,
                  'progress': st.get('progress') if state == 'syncing' else None, 'plan_limited': limited,
                  'plan_note': ('Your Withings developer plan leaves out sleep score, overnight HRV and breathing rate; '
                                'everything else syncs.') if 'sleep' in limited else None},
    }


def disconnect(forget=False):
    t, k = _tokens(), _keys()
    if t.get('userid') and k.get('client_id') and k.get('client_secret'):
        try:   # best effort: Withings' revoke is a signed call (nonce + signature), per the docs
            p = {'action': 'revoke', 'client_id': k['client_id'], 'nonce': _nonce(), 'userid': t['userid']}
            p['signature'] = _sign({x: p[x] for x in ('action', 'client_id', 'nonce')})
            _http(TOKEN_URL, p, timeout=15)
        except (ProviderError, KeyError):
            pass
    with LOCK:
        _save_tokens(None)
        for p in list(DATA.glob('withings.sqlite3*')):
            p.unlink(missing_ok=True)
        _shred('state.json')
        if forget:
            _shred('client.json')
    return status_info()


# ---------------- MCP ----------------

_NONE = {'type': 'object', 'properties': {}, 'additionalProperties': False}
TOOLS = [
    {'name': 'health_data_status', 'description': "Withings connector status (contract wizard.health-data/1): not_configured, needs_login, syncing, connected or rate_limited; last/next sync, history span, record counts and whether the developer plan withholds advanced sleep fields. Never returns keys or tokens. If not connected, send the owner the Withings app link [Withings](/wizard-withings/); its own screen walks him through setup. Never ask for Withings keys, passwords or codes in chat. For health questions use the Health app's tools instead.",
     'inputSchema': _NONE, 'annotations': {'readOnlyHint': True}},
    {'name': 'health_data_sync', 'description': 'Ask the Withings connector to pull new data from Withings now (it also syncs every 20 minutes). Returns at once; check health_data_status for progress. Reads Withings only.',
     'inputSchema': _NONE, 'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True}},
    {'name': 'health_data_days', 'description': 'Raw contract read: normalized Withings day records (no readiness score; sleep with stages, score, HR, breathing rate and HRV when the plan allows; intensity minutes as load; steps, calories, workouts, extras.withings), oldest first, paged with next_page; pass changed_since=<cursor> to get only changed days. For owner questions prefer the Health app tools.',
     'inputSchema': {'type': 'object', 'properties': {
         'from': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'}, 'to': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'},
         'limit': {'type': 'integer', 'description': 'days per page, default 100, max 500'},
         'page': {'type': 'string', 'description': 'next_page from the previous reply'},
         'changed_since': {'type': 'string', 'description': 'cursor from an earlier reply; empty string = everything'}},
         'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
]


def request_sync(full=False):
    s = status_info()
    if s['status'] in ('not_configured', 'needs_login'):
        raise Problem(409, 'Withings is not connected. The owner finishes setup in the Withings app (/wizard-withings/).')
    start_sync_thread(full=full)
    return dict(status_info(), note='Sync started.')


def call(name, a):
    if name == 'health_data_status':
        return status_info()
    if name == 'health_data_sync':
        return request_sync()
    if name == 'health_data_days':
        return contract_days(a)
    raise KeyError(name)


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            try:
                v = call(p.get('name'), p.get('arguments') or {})
            except KeyError:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            r = {'content': [{'type': 'text', 'text': json.dumps(v, ensure_ascii=False)}], 'structuredContent': v, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except (Problem, ProviderError) as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    except (ValueError, TypeError, AttributeError):
        r = {'content': [{'type': 'text', 'text': 'Invalid arguments.'}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


# ---------------- HTTP ----------------

def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


_LOOPBACK = {f'127.0.0.1:{PORT}', f'localhost:{PORT}', f'{APP_ID}:{PORT}'}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}
TRUSTED_NAMES = {h.split(':')[0] for h in TRUSTED_HOSTS}
CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


def _host_ok(host):
    host = (host or '').lower()
    return host in TRUSTED_HOSTS or host.split(':')[0] in TRUSTED_NAMES


class Handler(BaseHTTPRequestHandler):
    def refused(self, write=False):
        host = (self.headers.get('Host') or '').lower()
        if not _host_ok(host):
            self.send(403, {'error': 'Unknown host.'}); return True
        origin = (self.headers.get('Origin') or '').lower()
        if origin and origin not in TRUSTED_ORIGINS and urlsplit(origin).netloc != host:
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        if write and not origin and (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        return False

    def forwarded(self):
        return any(self.headers.get(h) for h in FORWARDED)

    def send(self, code, body=None, kind='application/json', extra=None):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def route(self):
        p = urlsplit(self.path).path
        return '/' if p == PREFIX else p[len(PREFIX):] if p.startswith(PREFIX + '/') else p

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise Problem(415, 'Use application/json.')
        size = int(self.headers.get('Content-Length', '0') or 0)
        if not 0 <= size <= 65536:
            raise Problem(413, 'Request too large.')
        try:
            d = json.loads(self.rfile.read(size) or b'{}')
        except ValueError:
            raise Problem(400, 'Invalid JSON.') from None
        if not isinstance(d, dict):
            raise Problem(400, 'Expected a JSON object.')
        return d

    def callback_page(self, ok, message):
        home = PREFIX + '/' if self.path.startswith(PREFIX) else '/'
        title = f'{NAME} connected' if ok else f'{NAME} sign-in did not finish'
        esc = lambda s: s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        html = (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{title}</title><body style="font:16px/1.5 system-ui;margin:2rem;max-width:36rem">'
                f'<h1 style="font-size:1.4rem">{title}</h1><p>{esc(message)}</p><p><a href="{home}">Open {NAME}</a></p>')
        self.send(200 if ok else 400, html.encode(), 'text/html; charset=utf-8')

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.refused():
            return
        path = self.route()
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query, keep_blank_values=True).items()}
        try:
            if path in ('/healthz', '/api/health'):
                s = status_info()
                return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION, 'status': s['status'],
                                       'connected': s['status'] in ('connected', 'syncing', 'rate_limited'), 'last_sync': s['last_sync']})
            if path == '/api/status':
                return self.send(200, status_info())
            if path == '/api/setup':
                return self.send(200, setup_info())
            if path == '/api/days':
                if self.forwarded():
                    return self.send(403, {'error': 'Health data reads are box-only.'})
                return self.send(200, contract_days(query))
            if path in ('/callback', '/oauth/callback'):   # the callback URL registered on Withings points here
                if self.command == 'HEAD' or 'code' not in query and 'error' not in query:
                    return self.callback_page(True, f'This is the {NAME} app\u2019s callback address.')   # Withings may probe it
                try:
                    login_finish('http://x' + self.path)
                    return self.callback_page(True, f'Your {NAME} data is downloading now. You can close this tab and go back to the {NAME} app.')
                except (Problem, ProviderError) as e:
                    return self.callback_page(False, e.message)
            if path == '/openapi.json':
                return self.send(200, (ROOT / 'openapi.json').read_bytes())
            name = 'index.html' if path == '/' else path[1:]
            kind = STATIC.get(Path(name).suffix)
            if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
                return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
            self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid query.'})

    def do_POST(self):
        if self.refused(write=True):
            return
        path = self.route()
        try:
            if path == '/mcp':
                if self.forwarded():
                    return self.send(403, {'error': 'MCP is box-only.'})
                out = rpc(self.body())
                return self.send(202 if out is None else 200, out)
            b = self.body()
            if path == '/api/keys':
                return self.send(200, save_keys(b))
            if path == '/api/keys/test':
                return self.send(200, test_keys())
            if path == '/api/keys/forget':
                return self.send(200, forget_keys())
            if path == '/api/oauth/start':
                return self.send(200, login_start())
            if path == '/api/oauth/finish':
                return self.send(200, login_finish(b.get('url')))
            if path == '/api/sync':
                return self.send(202, request_sync(full=bool(b.get('full'))))
            if path == '/api/disconnect':
                return self.send(200, disconnect(forget=bool(b.get('forget_keys'))))
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except ProviderError as e:
            self.send(429 if e.code == 'rate_limited' else 502, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid request.'})

    def log_message(self, *a):
        pass


def main():
    secure_files()
    st = _state()
    if st.get('syncing'):
        _update_state(syncing=None, progress=None, sync_kind=None)
    threading.Thread(target=_scheduler, daemon=True).start()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(PORT)), Handler).serve_forever()

# ---- wizard.app-switches/1 ------------------------------------------------------------------------------------------
# Owner feature switches for the Tracker's Try it (wizard-os docs/APP_SWITCHES.md). Shared block: the same text ships in
# every Wizard app (template: wizard-app-manager templates/hello-wizard). Every new feature ships behind a switch, off by
# default: append it to SWITCHES with the issue it delivers, e.g.
#   {'id': 'compact-header', 'name': 'Compact top bar', 'description': '...', 'default': False,
#    'issues': ['humanitylabs-org/<repo>#20']}
# and gate the feature on switch_on('compact-header'). State lives on the box (DATA_DIR/switches.json), so the app's
# page, the Tracker (through the App Manager) and the agent see one state. GET/POST /api/switches, owner only: the
# box's Tailscale Serve owner login with X-Wizard-Switch: 1; never a bearer, agent grant or box-local host.
import json as _sw_json
import os as _sw_os
import re as _sw_re
import threading as _sw_threading
from pathlib import Path as _SwPath
from urllib.parse import urlsplit as _sw_urlsplit

SWITCHES = []
_SW_CONTRACT = 'wizard.app-switches/1'
_SW_ROUTE = _sw_re.compile(r'(?:/[a-z0-9][a-z0-9-]{0,62})?/api/switches/?')
_SW_LOCAL = {'localhost', '127.0.0.1', '::1', '[::1]', '0.0.0.0'}
_sw_lock = _sw_threading.Lock()


def _sw_file():
    return _SwPath(_sw_os.environ.get('WIZARD_SWITCHES_DIR') or _sw_os.environ.get('DATA_DIR') or '/data') / 'switches.json'


def _sw_saved():
    try:
        saved = _sw_json.loads(_sw_file().read_text())
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def switches_doc():
    saved = _sw_saved()
    return {'contract': _SW_CONTRACT, 'switches': [
        dict(s, on=saved[s['id']] if isinstance(saved.get(s['id']), bool) else bool(s.get('default'))) for s in SWITCHES]}


def switch_on(switch_id):
    return next((s['on'] for s in switches_doc()['switches'] if s['id'] == switch_id), False)


def _sw_set(switch_id, on):
    with _sw_lock:
        ids = {s['id'] for s in SWITCHES}
        saved = {k: v for k, v in _sw_saved().items() if k in ids and isinstance(v, bool)}
        saved[switch_id] = on
        f = _sw_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + '.tmp')
        tmp.write_text(_sw_json.dumps(saved))
        _sw_os.replace(tmp, f)
    return switches_doc()


def _sw_owner(h, write):
    hd = h.headers
    if hd.get('Authorization') or hd.get('X-Wizard-Switch') != '1':
        return False
    if (hd.get('Sec-Fetch-Site') or 'same-origin').lower() not in ('same-origin', 'none'):
        return False
    host = (hd.get('Host') or '').strip().lower()
    name = host.split(']')[0] + ']' if host.startswith('[') else host.rsplit(':', 1)[0]
    if not name or name in _SW_LOCAL or '.' not in name:      # box-local and container hosts never prove the owner
        return False
    origin = hd.get('Origin')
    trusted = {o.strip() for o in (_sw_os.environ.get('WIZARD_TRUSTED_ORIGINS') or '').split(',') if o.strip()}
    if origin and _sw_urlsplit(origin).netloc.lower() != host and origin not in trusted:
        return False
    if write and not origin:
        return False
    logins = [x.strip() for x in (hd.get_all('Tailscale-User-Login') or [])]
    owners = {x.strip().lower() for x in (_sw_os.environ.get('WIZARD_OWNER_LOGINS') or '').split(',') if x.strip()}
    return len(logins) == 1 and '@' in logins[0] and (not owners or logins[0].lower() in owners)


def _sw_send(h, code, doc):
    body = _sw_json.dumps(doc).encode()
    h.send_response(code)
    h.send_header('Content-Type', 'application/json')
    h.send_header('Cache-Control', 'no-store')
    h.send_header('Content-Length', str(len(body)))
    h.end_headers()
    if h.command != 'HEAD':
        h.wfile.write(body)


def _sw_handle(h):
    """Serve /api/switches (or /<app>/api/switches) and return True; False for every other request."""
    if not _SW_ROUTE.fullmatch(_sw_urlsplit(h.path).path):
        return False
    if h.command in ('GET', 'HEAD'):
        if not _sw_owner(h, False):
            _sw_send(h, 403, {'error': "Only this Wizard's owner can see these switches.", 'code': 'owner_only'})
        else:
            _sw_send(h, 200, switches_doc())
        return True
    if not _sw_owner(h, True):
        _sw_send(h, 403, {'error': "Only this Wizard's owner can change these switches.", 'code': 'owner_only'})
        return True
    try:
        if (h.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
            raise ValueError
        size = int(h.headers.get('Content-Length') or '0')
        if size <= 0 or size > 4096:
            raise ValueError
        body = _sw_json.loads(h.rfile.read(size))
        if not isinstance(body, dict) or not isinstance(body.get('id'), str) or not isinstance(body.get('on'), bool) \
                or set(body) - {'id', 'on'}:
            raise ValueError
    except (ValueError, TypeError):
        _sw_send(h, 400, {'error': 'Send JSON {"id": "...", "on": true|false}', 'code': 'invalid_input'})
        return True
    if body['id'] not in {s['id'] for s in SWITCHES}:
        _sw_send(h, 404, {'error': 'No such switch', 'code': 'unknown_switch'})
        return True
    try:
        _sw_send(h, 200, _sw_set(body['id'], body['on']))
    except OSError:
        _sw_send(h, 500, {'error': 'storage_failure'})
    return True


def wizard_switches_install(handler_cls):
    """Route the switch endpoint ahead of the app's own GET/HEAD/POST handlers."""
    for method in ('do_GET', 'do_HEAD', 'do_POST'):
        orig = getattr(handler_cls, method, None)
        if getattr(orig, '_wizard_switches', False):
            continue

        def wrapped(self, _orig=orig):
            if _sw_handle(self):
                return None
            if _orig is None:
                return self.send_error(501)
            return _orig(self)
        wrapped._wizard_switches = True
        setattr(handler_cls, method, wrapped)
    return handler_cls
# ---- end wizard.app-switches/1 ---------------------------------------------------------------------------------------

wizard_switches_install(Handler)


if __name__ == '__main__':
    main()
