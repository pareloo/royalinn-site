#!/usr/bin/env python3
"""Royal Inn Tartus - booking requests and availability.

Runs behind nginx on 127.0.0.1:8081, standard library only.
Data lives in SQLite under DATA_DIR (a persistent volume, /data in the container).

Public:   GET  /api/health
          GET  /api/availability?in=YYYY-MM-DD&out=YYYY-MM-DD&room=<id>
          POST /api/booking                    (JSON)
Panel:    POST /api/admin/login | /api/admin/logout
          GET  /api/admin/me
          GET  /api/admin/bookings?status=new|confirmed|cancelled|all
          POST /api/admin/bookings/<ref>       {"status": "...", "note": "..."}
          GET  /api/admin/closures             POST /api/admin/closures {room, from, to, note}
          POST /api/admin/closures/<id>/delete
          GET  /api/admin/inventory            POST /api/admin/inventory {"<room>": n, ...}
          GET  /api/admin/export.csv

The panel password comes from the PANEL_PASSWORD environment variable; without it the panel stays locked.
Optional e-mail notice for each new request: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, NOTIFY_TO.
"""
import csv, datetime as dt, hashlib, hmac, io, json, os, re, secrets, smtplib, sqlite3, threading, time
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DATA = os.environ.get('DATA_DIR', '/data')
HOST, PORT = os.environ.get('API_HOST', '127.0.0.1'), int(os.environ.get('API_PORT', '8081'))
ROOMS = {
    'standard': ('Standard Room', 'غرفة ستاندرد'),
    'triple': ('Triple Room', 'غرفة ثلاثية'),
    'junior-suite': ('Junior Suite', 'جناح جونيور'),
    'royal-suite': ('Royal Suite', 'جناح رويال'),
    'two-bedroom-royal-suite': ('Two-Bedroom Royal Suite', 'جناح رويال بغرفتَي نوم'),
}
STATUSES = ('new', 'confirmed', 'cancelled')
MAX_NIGHTS, MAX_AHEAD_DAYS = 60, 550
SESSION_HOURS = 12
LOCK = threading.Lock()


# ---------------------------------------------------------------- storage
def db():
    con = sqlite3.connect(os.path.join(DATA, 'royalinn.db'), timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init():
    os.makedirs(DATA, exist_ok=True)
    with db() as con:
        con.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS bookings(
            ref TEXT PRIMARY KEY, created TEXT NOT NULL, lang TEXT, status TEXT NOT NULL DEFAULT 'new',
            check_in TEXT NOT NULL, check_out TEXT NOT NULL, nights INTEGER NOT NULL, guests INTEGER NOT NULL,
            room TEXT, breakfast INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL, phone TEXT NOT NULL,
            email TEXT, message TEXT, staff_note TEXT, updated TEXT);
        CREATE INDEX IF NOT EXISTS bookings_dates ON bookings(check_in, check_out);
        CREATE TABLE IF NOT EXISTS closures(
            id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT, date_from TEXT NOT NULL, date_to TEXT NOT NULL, note TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS inventory(room TEXT PRIMARY KEY, count INTEGER NOT NULL);
        ''')
    path = os.path.join(DATA, 'secret.key')
    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)


def secret():
    with open(os.path.join(DATA, 'secret.key')) as f:
        return f.read().strip().encode()


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def new_ref():
    alphabet = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
    return 'RI-' + ''.join(secrets.choice(alphabet) for _ in range(6))


# ---------------------------------------------------------------- availability
def parse_day(s):
    try:
        return dt.date.fromisoformat(str(s))
    except ValueError:
        return None


def check_dates(cin, cout):
    a, b = parse_day(cin), parse_day(cout)
    today = dt.date.today()
    if not a or not b:
        return None, 'dates'
    if a < today - dt.timedelta(days=1) or b <= a or (b - a).days > MAX_NIGHTS or a > today + dt.timedelta(days=MAX_AHEAD_DAYS):
        return None, 'dates'
    return (a, b), None


def availability(con, a, b, room):
    """True / False. A night is unavailable if the hotel closed it, or (when a room count is set) all rooms of that type are confirmed."""
    nights = [a + dt.timedelta(days=i) for i in range((b - a).days)]
    rows = con.execute('SELECT room, date_from, date_to FROM closures WHERE date_from < ? AND date_to >= ?', (b.isoformat(), a.isoformat())).fetchall()
    for r in rows:
        if r['room'] and room and r['room'] != room:
            continue
        if r['room'] and not room:
            continue
        f, t = dt.date.fromisoformat(r['date_from']), dt.date.fromisoformat(r['date_to'])
        if any(f <= n <= t for n in nights):
            return False
    if room:
        inv = con.execute('SELECT count FROM inventory WHERE room=?', (room,)).fetchone()
        if inv is not None:
            booked = con.execute("SELECT check_in, check_out FROM bookings WHERE status='confirmed' AND room=? AND check_in < ? AND check_out > ?",
                                 (room, b.isoformat(), a.isoformat())).fetchall()
            for n in nights:
                used = sum(1 for x in booked if dt.date.fromisoformat(x['check_in']) <= n < dt.date.fromisoformat(x['check_out']))
                if used >= inv['count']:
                    return False
    return True


# ---------------------------------------------------------------- validation
PHONE_RE = re.compile(r'^\+?[0-9 ()\-]{7,20}$')
EMAIL_RE = re.compile(r'^[^@\s<>]{1,64}@[^@\s<>]{1,190}\.[A-Za-z]{2,24}$')


def clean(s, n):
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', str(s or '')).strip()
    return s[:n]


def validate_booking(d):
    errs = []
    dates, err = check_dates(d.get('in'), d.get('out'))
    if err:
        errs.append('dates')
    name = clean(d.get('name'), 80)
    phone = clean(d.get('phone'), 24)
    email = clean(d.get('email'), 254)
    room = clean(d.get('room'), 40)
    msg = clean(d.get('message'), 600)
    try:
        guests = int(d.get('guests') or 2)
    except (TypeError, ValueError):
        guests = 0
    if len(name) < 2:
        errs.append('name')
    if not PHONE_RE.match(phone) or len(re.sub(r'\D', '', phone)) < 7:
        errs.append('phone')
    if email and not EMAIL_RE.match(email):
        errs.append('email')
    if room and room not in ROOMS:
        errs.append('room')
    if not 1 <= guests <= 12:
        errs.append('guests')
    lang = 'en' if d.get('lang') == 'en' else 'ar'
    return errs, dict(dates=dates, name=name, phone=phone, email=email or None, room=room or None, message=msg or None,
                      guests=guests, breakfast=1 if d.get('breakfast') in (True, 1, '1', 'on', 'true') else 0, lang=lang)


# ---------------------------------------------------------------- rate limits (memory only)
HITS = {}


def limited(key, limit, window):
    t = time.time()
    with LOCK:
        q = [x for x in HITS.get(key, []) if t - x < window]
        if len(q) >= limit:
            HITS[key] = q
            return True
        q.append(t)
        HITS[key] = q
        if len(HITS) > 5000:
            for k in list(HITS)[:2500]:
                HITS.pop(k, None)
    return False


# ---------------------------------------------------------------- sessions
def sign(payload):
    body = json.dumps(payload, separators=(',', ':')).encode()
    mac = hmac.new(secret(), body, hashlib.sha256).hexdigest()
    return body.hex() + '.' + mac


def unsign(token):
    try:
        body_hex, mac = token.split('.', 1)
        body = bytes.fromhex(body_hex)
    except ValueError:
        return None
    if not hmac.compare_digest(hmac.new(secret(), body, hashlib.sha256).hexdigest(), mac):
        return None
    data = json.loads(body)
    pw = os.environ.get('PANEL_PASSWORD', '')
    if data.get('exp', 0) < time.time() or data.get('pw') != hashlib.sha256(pw.encode()).hexdigest()[:16]:
        return None
    return data


# ---------------------------------------------------------------- notifications
def notify(b):
    host, to = os.environ.get('SMTP_HOST'), os.environ.get('NOTIFY_TO')
    if not host or not to:
        return
    try:
        m = EmailMessage()
        m['Subject'] = f"طلب حجز جديد {b['ref']} - Royal Inn"
        m['From'] = os.environ.get('SMTP_USER', to)
        m['To'] = to
        room = ROOMS.get(b['room'] or '', ('-', 'أي غرفة'))[1]
        m.set_content('\n'.join([
            f"رقم الطلب: {b['ref']}", f"الاسم: {b['name']}", f"الهاتف: {b['phone']}", f"البريد: {b['email'] or '-'}",
            f"الوصول: {b['check_in']}", f"المغادرة: {b['check_out']} ({b['nights']} ليالٍ)", f"الضيوف: {b['guests']}",
            f"نوع الغرفة: {room}", f"الفطور: {'نعم' if b['breakfast'] else 'لا'}", f"ملاحظة: {b['message'] or '-'}",
            '', 'لوحة الحجوزات: https://royalinntartus.com/panel/']))
        port = int(os.environ.get('SMTP_PORT', '465'))
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            s = smtplib.SMTP(host, port, timeout=15)
            s.starttls()
        if os.environ.get('SMTP_USER'):
            s.login(os.environ['SMTP_USER'], os.environ.get('SMTP_PASS', ''))
        s.send_message(m)
        s.quit()
    except Exception as e:  # a failed e-mail never loses the request
        print('notify failed:', type(e).__name__, flush=True)


# ---------------------------------------------------------------- HTTP
def row(r):
    d = dict(r)
    d['breakfast'] = bool(d['breakfast'])
    d['room_name'] = ROOMS.get(d.get('room') or '', ('Any room', 'أي غرفة'))[1]
    return d


class H(BaseHTTPRequestHandler):
    server_version = 'royalinn'
    sys_version = ''

    def log_message(self, fmt, *args):  # no personal data in logs
        pass

    # helpers
    def ip(self):
        return (self.headers.get('X-Real-IP') or self.client_address[0])[:64]

    def send(self, code, obj=None, extra=None, ctype='application/json; charset=utf-8', raw=None):
        body = raw if raw is not None else json.dumps(obj if obj is not None else {}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 16384:
            return None
        ctype = (self.headers.get('Content-Type') or '').split(';')[0].strip()
        if ctype != 'application/json':
            return None
        try:
            return json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            return None

    def session(self):
        for part in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == 'ri_panel':
                return unsign(v)
        return None

    def same_origin(self):
        o = self.headers.get('Origin')
        if not o:
            return True
        host = self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or ''
        return urlparse(o).netloc == host

    # routes
    def do_GET(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p == '/api/health':
            return self.send(200, {'ok': True})
        if p == '/api/availability':
            if limited('av:' + self.ip(), 60, 60):
                return self.send(429, {'ok': False, 'error': 'busy'})
            dates, err = check_dates(q.get('in'), q.get('out'))
            room = q.get('room') or None
            if err or (room and room not in ROOMS):
                return self.send(400, {'ok': False, 'error': 'dates'})
            with db() as con:
                ok = availability(con, dates[0], dates[1], room)
            return self.send(200, {'ok': True, 'available': ok, 'nights': (dates[1] - dates[0]).days})
        if p.startswith('/api/admin/'):
            if p == '/api/admin/me':
                return self.send(200, {'ok': True, 'auth': bool(self.session()), 'configured': bool(os.environ.get('PANEL_PASSWORD'))})
            if not self.session():
                return self.send(401, {'ok': False, 'error': 'auth'})
            with db() as con:
                if p == '/api/admin/bookings':
                    st = q.get('status', 'active')
                    if st == 'all':
                        rows = con.execute('SELECT * FROM bookings ORDER BY created DESC LIMIT 500').fetchall()
                    elif st == 'active':
                        rows = con.execute("SELECT * FROM bookings WHERE status!='cancelled' AND check_out >= ? ORDER BY check_in LIMIT 500",
                                           (dt.date.today().isoformat(),)).fetchall()
                    elif st in STATUSES:
                        rows = con.execute('SELECT * FROM bookings WHERE status=? ORDER BY created DESC LIMIT 500', (st,)).fetchall()
                    else:
                        return self.send(400, {'ok': False})
                    counts = {s: con.execute('SELECT COUNT(*) FROM bookings WHERE status=?', (s,)).fetchone()[0] for s in STATUSES}
                    return self.send(200, {'ok': True, 'items': [row(r) for r in rows], 'counts': counts})
                if p == '/api/admin/closures':
                    rows = con.execute('SELECT * FROM closures WHERE date_to >= ? ORDER BY date_from', (dt.date.today().isoformat(),)).fetchall()
                    return self.send(200, {'ok': True, 'items': [dict(r) for r in rows]})
                if p == '/api/admin/inventory':
                    inv = {r['room']: r['count'] for r in con.execute('SELECT * FROM inventory')}
                    return self.send(200, {'ok': True, 'rooms': [{'id': k, 'name': v[1], 'count': inv.get(k)} for k, v in ROOMS.items()]})
                if p == '/api/admin/export.csv':
                    out = io.StringIO(); w = csv.writer(out)
                    cols = ['ref', 'created', 'status', 'check_in', 'check_out', 'nights', 'guests', 'room', 'breakfast', 'name', 'phone', 'email', 'message', 'staff_note']
                    w.writerow(cols)
                    for r in con.execute('SELECT * FROM bookings ORDER BY created'):
                        w.writerow(['' if r[c] is None else r[c] for c in cols])
                    return self.send(200, raw=('﻿' + out.getvalue()).encode(), ctype='text/csv; charset=utf-8',
                                     extra={'Content-Disposition': 'attachment; filename="royalinn-bookings.csv"'})
        return self.send(404, {'ok': False})

    def do_POST(self):
        p = urlparse(self.path).path
        if not self.same_origin():
            return self.send(403, {'ok': False})
        d = self.body()
        if d is None or not isinstance(d, dict):
            return self.send(400, {'ok': False, 'error': 'body'})

        if p == '/api/booking':
            if d.get('website'):                     # hidden field, filled only by bots
                return self.send(200, {'ok': True, 'ref': new_ref()})
            if limited('bk:' + self.ip(), 5, 600):
                return self.send(429, {'ok': False, 'error': 'busy'})
            errs, v = validate_booking(d)
            if errs:
                return self.send(400, {'ok': False, 'error': 'fields', 'fields': errs})
            a, b = v['dates']
            with LOCK, db() as con:
                ref = new_ref()
                while con.execute('SELECT 1 FROM bookings WHERE ref=?', (ref,)).fetchone():
                    ref = new_ref()
                avail = availability(con, a, b, v['room'])
                rec = dict(ref=ref, created=now(), lang=v['lang'], status='new', check_in=a.isoformat(), check_out=b.isoformat(),
                           nights=(b - a).days, guests=v['guests'], room=v['room'], breakfast=v['breakfast'], name=v['name'],
                           phone=v['phone'], email=v['email'], message=v['message'])
                con.execute('INSERT INTO bookings(ref,created,lang,status,check_in,check_out,nights,guests,room,breakfast,name,phone,email,message) '
                            'VALUES(:ref,:created,:lang,:status,:check_in,:check_out,:nights,:guests,:room,:breakfast,:name,:phone,:email,:message)', rec)
            threading.Thread(target=notify, args=(rec,), daemon=True).start()
            return self.send(200, {'ok': True, 'ref': ref, 'available': avail})

        if p == '/api/admin/login':
            pw = os.environ.get('PANEL_PASSWORD', '')
            if not pw:
                return self.send(503, {'ok': False, 'error': 'not_configured'})
            if limited('login:' + self.ip(), 6, 900):
                return self.send(429, {'ok': False, 'error': 'busy'})
            if not hmac.compare_digest(str(d.get('password', '')).encode(), pw.encode()):
                time.sleep(1)
                return self.send(401, {'ok': False, 'error': 'password'})
            tok = sign({'exp': int(time.time()) + SESSION_HOURS * 3600, 'pw': hashlib.sha256(pw.encode()).hexdigest()[:16], 'n': secrets.token_hex(4)})
            return self.send(200, {'ok': True}, extra={'Set-Cookie': f'ri_panel={tok}; Path=/api/admin/; Max-Age={SESSION_HOURS*3600}; HttpOnly; Secure; SameSite=Strict'})
        if p == '/api/admin/logout':
            return self.send(200, {'ok': True}, extra={'Set-Cookie': 'ri_panel=; Path=/api/admin/; Max-Age=0; HttpOnly; Secure; SameSite=Strict'})

        if p.startswith('/api/admin/'):
            if not self.session():
                return self.send(401, {'ok': False, 'error': 'auth'})
            m = re.fullmatch(r'/api/admin/bookings/(RI-[A-Z0-9]{6})', p)
            if m:
                st = d.get('status')
                if st is not None and st not in STATUSES:
                    return self.send(400, {'ok': False})
                with LOCK, db() as con:
                    cur = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
                    if not cur:
                        return self.send(404, {'ok': False})
                    con.execute('UPDATE bookings SET status=?, staff_note=?, updated=? WHERE ref=?',
                                (st or cur['status'], clean(d.get('note', cur['staff_note']), 400) or None, now(), m.group(1)))
                    r = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
                return self.send(200, {'ok': True, 'item': row(r)})
            if p == '/api/admin/closures':
                f, t = parse_day(d.get('from')), parse_day(d.get('to') or d.get('from'))
                room = d.get('room') or None
                if not f or not t or t < f or (room and room not in ROOMS) or (t - f).days > 400:
                    return self.send(400, {'ok': False, 'error': 'dates'})
                with db() as con:
                    con.execute('INSERT INTO closures(room,date_from,date_to,note,created) VALUES(?,?,?,?,?)',
                                (room, f.isoformat(), t.isoformat(), clean(d.get('note'), 120) or None, now()))
                return self.send(200, {'ok': True})
            m = re.fullmatch(r'/api/admin/closures/(\d+)/delete', p)
            if m:
                with db() as con:
                    con.execute('DELETE FROM closures WHERE id=?', (int(m.group(1)),))
                return self.send(200, {'ok': True})
            if p == '/api/admin/inventory':
                with db() as con:
                    for k in ROOMS:
                        if k not in d:
                            continue
                        val = d[k]
                        if val in (None, ''):
                            con.execute('DELETE FROM inventory WHERE room=?', (k,))
                            continue
                        try:
                            n = int(val)
                        except (TypeError, ValueError):
                            return self.send(400, {'ok': False})
                        if not 0 <= n <= 500:
                            return self.send(400, {'ok': False})
                        con.execute('INSERT INTO inventory(room,count) VALUES(?,?) ON CONFLICT(room) DO UPDATE SET count=excluded.count', (k, n))
                return self.send(200, {'ok': True})
        return self.send(404, {'ok': False})


if __name__ == '__main__':
    init()
    print(f'booking api on {HOST}:{PORT}, data in {DATA}', flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
