#!/usr/bin/env python3
"""Royal Inn Tartus - booking requests and availability.

Runs behind nginx on 127.0.0.1:8081, standard library only.
Data lives in SQLite under DATA_DIR (a persistent volume, /data in the container).

Public:   GET  /api/health
          GET  /api/availability?in=YYYY-MM-DD&out=YYYY-MM-DD&room=<id>
          GET  /api/quote?in=...&out=...&guests=2[&room=<id>]   prices and availability per room type
          POST /api/booking                    (JSON)
Panel:    POST /api/admin/login | /api/admin/logout
          GET  /api/admin/me
          GET  /api/admin/bookings?status=new|confirmed|cancelled|all
          POST /api/admin/bookings/<ref>       {"status": "...", "note": "..."}
          GET  /api/admin/closures             POST /api/admin/closures {room, from, to, note}
          POST /api/admin/closures/<id>/delete
          GET  /api/admin/inventory            POST /api/admin/inventory {"<room>": n, ...}
          GET  /api/admin/export.csv
          GET/POST /api/admin/prices           base rate per room type
          GET  /api/admin/rates                POST /api/admin/rates {room, from, to, price, price2, days, note}
          POST /api/admin/rates/<id>/delete
          POST /api/admin/bookings/new         booking taken by phone / at the desk
          GET  /api/admin/occupancy?from=YYYY-MM-DD&days=31

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
# rates confirmed by the hotel (Nov 2026), USD per night incl. tax; price2 = two or more guests. None = on request
DEFAULT_PRICES = {'standard': (90, 100), 'triple': (130, None), 'junior-suite': (None, None), 'royal-suite': (None, None), 'two-bedroom-royal-suite': (None, None)}
BREAKFAST = 10
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
        CREATE TABLE IF NOT EXISTS prices(room TEXT PRIMARY KEY, price INTEGER, price2 INTEGER);
        CREATE TABLE IF NOT EXISTS rates(
            id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT, date_from TEXT NOT NULL, date_to TEXT NOT NULL,
            price INTEGER, price2 INTEGER, days TEXT, note TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS offers(
            id INTEGER PRIMARY KEY AUTOINCREMENT, percent INTEGER NOT NULL, title_ar TEXT, title_en TEXT,
            date_from TEXT NOT NULL, date_to TEXT NOT NULL, min_nights INTEGER NOT NULL DEFAULT 1, created TEXT);
        ''')
        cols = [r[1] for r in con.execute('PRAGMA table_info(bookings)')]
        for c, t in (('total', 'INTEGER'), ('source', 'TEXT'), ('room_no', 'TEXT'), ('paid', 'INTEGER'), ('arrived', 'TEXT'), ('departed', 'TEXT')):
            if c not in cols:
                con.execute(f'ALTER TABLE bookings ADD COLUMN {c} {t}')
        for k, (a, b) in DEFAULT_PRICES.items():
            con.execute('INSERT OR IGNORE INTO prices(room,price,price2) VALUES(?,?,?)', (k, a, b))
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


def nightly(con, room, guests, nights):
    """Price for each night (None when the hotel gives the rate on request). Later rules win over earlier ones."""
    base = con.execute('SELECT price, price2 FROM prices WHERE room=?', (room,)).fetchone()
    rules = con.execute('SELECT * FROM rates WHERE (room=? OR room IS NULL) AND date_from <= ? AND date_to >= ? ORDER BY id',
                        (room, nights[-1].isoformat(), nights[0].isoformat())).fetchall()
    out = []
    for n in nights:
        p1, p2 = (base['price'], base['price2']) if base else (None, None)
        for r in rules:
            if not (r['date_from'] <= n.isoformat() <= r['date_to']):
                continue
            if r['days'] and str(n.weekday()) not in r['days'].split(','):
                continue
            if r['price'] is not None:
                p1, p2 = r['price'], r['price2']
        out.append(p2 if (p2 and guests >= 2) else p1)
    return out


def discounted(con, per, nights):
    """Apply the hotel's own offers (set in the panel) to nightly prices. An offer covers a night when the night is
    inside its dates and the stay is at least its minimum length; when two overlap, the larger discount is used.
    Returns (prices after discount, largest percent applied or None)."""
    offers = con.execute('SELECT * FROM offers WHERE date_from <= ? AND date_to >= ? AND min_nights <= ?',
                         (nights[-1].isoformat(), nights[0].isoformat(), len(nights))).fetchall()
    if not offers:
        return per, None
    out, best = [], None
    for n, p in zip(nights, per):
        pct = max([o['percent'] for o in offers if o['date_from'] <= n.isoformat() <= o['date_to']] or [0])
        if p is not None and pct:
            best = max(best or 0, pct)
            p = int(round(p * (100 - pct) / 100.0))
        out.append(p)
    return out, best


def left(con, room, nights):
    """Rooms of this type still free on the busiest night, or None when the hotel has not set a room count."""
    inv = con.execute('SELECT count FROM inventory WHERE room=?', (room,)).fetchone()
    if inv is None:
        return None
    a, b = nights[0], nights[-1] + dt.timedelta(days=1)
    booked = con.execute("SELECT check_in, check_out FROM bookings WHERE status='confirmed' AND room=? AND check_in < ? AND check_out > ?",
                         (room, b.isoformat(), a.isoformat())).fetchall()
    used = max(sum(1 for x in booked if x['check_in'] <= n.isoformat() < x['check_out']) for n in nights)
    return max(inv['count'] - used, 0)


def quote(con, a, b, guests, room=None):
    nights = [a + dt.timedelta(days=i) for i in range((b - a).days)]
    out = []
    for k, names in ROOMS.items():
        if room and k != room:
            continue
        base = nightly(con, k, guests, nights)
        per, pct = discounted(con, base, nights)
        total = None if any(x is None for x in per) else sum(per)
        base_total = None if any(x is None for x in base) else sum(base)
        out.append({'id': k, 'name_en': names[0], 'name_ar': names[1], 'available': availability(con, a, b, k),
                    'left': left(con, k, nights), 'total': total, 'nightly': per,
                    'base_total': base_total if pct and base_total != total else None, 'offer_percent': pct if base_total != total else None})
    return out


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
def notify_email(b):
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


def smtp_send(m):
    host = os.environ['SMTP_HOST']; port = int(os.environ.get('SMTP_PORT', '465'))
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        s = smtplib.SMTP(host, port, timeout=15); s.starttls()
    if os.environ.get('SMTP_USER'):
        s.login(os.environ['SMTP_USER'], os.environ.get('SMTP_PASS', ''))
    s.send_message(m); s.quit()


def mail_confirmation(b):
    """When the hotel confirms a booking and the guest left an e-mail address, send them the confirmation link.
    Only works once the hotel's mailbox is set up (SMTP_HOST, SMTP_USER, SMTP_PASS)."""
    if not os.environ.get('SMTP_HOST') or not b.get('email'):
        return
    try:
        en = b.get('lang') == 'en'
        link = 'https://royalinntartus.com/api/confirmation/%s/%s' % (b['ref'], letter_key(b['ref']))
        m = EmailMessage()
        m['Subject'] = ('Booking confirmed %s - Royal Inn Tartus' if en else 'تأكيد الحجز %s - فندق رويال إن طرطوس') % b['ref']
        m['From'] = os.environ.get('SMTP_USER') or os.environ.get('NOTIFY_TO', '')
        m['To'] = b['email']
        if en:
            lines = ['Dear %s,' % b['name'], '', 'Your booking at Royal Inn Tartus is confirmed.', 'Booking number: %s' % b['ref'],
                     'Check-in: %s (from 14:00)' % b['check_in'], 'Check-out: %s (until 12:00)' % b['check_out'],
                     ('Total: $%s, tax included' % b['total']) if b.get('total') else '', '', 'Your confirmation letter: ' + link, '',
                     'Reservations, Royal Inn Hotel', HOTEL['phone']]
        else:
            lines = ['السيد/ة %s المحترم/ة،' % b['name'], '', 'تم تأكيد حجزكم في فندق رويال إن طرطوس.', 'رقم الحجز: %s' % b['ref'],
                     'الوصول: %s (من الساعة 14:00)' % b['check_in'], 'المغادرة: %s (حتى الساعة 12:00)' % b['check_out'],
                     ('المبلغ الإجمالي: %s$ شامل الضريبة' % b['total']) if b.get('total') else '', '', 'رسالة التأكيد: ' + link, '',
                     'إدارة الحجوزات، فندق رويال إن', HOTEL['phone']]
        m.set_content('\n'.join(lines))
        smtp_send(m)
    except Exception as e:
        print('confirmation mail failed:', type(e).__name__, flush=True)


def notify_telegram(b):
    """Instant message to the hotel's Telegram chat. Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."""
    token, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        return
    try:
        import urllib.request, urllib.parse
        room = ROOMS.get(b['room'] or '', ('-', 'أي غرفة'))[1]
        text = '\n'.join([
            f"🔔 طلب حجز جديد {b['ref']}", f"الاسم: {b['name']}", f"الهاتف: {b['phone']}",
            f"الوصول: {b['check_in']}", f"المغادرة: {b['check_out']} ({b['nights']} ليالٍ)", f"الضيوف: {b['guests']}",
            f"الغرفة: {room}", f"الفطور: {'نعم' if b['breakfast'] else 'لا'}",
            f"المبلغ: ${b['total']}" if b.get('total') else 'المبلغ: عند الطلب', f"ملاحظة: {b['message'] or '-'}",
            '', 'https://royalinntartus.com/panel/'])
        data = urllib.parse.urlencode({'chat_id': chat, 'text': text, 'disable_web_page_preview': 'true'}).encode()
        urllib.request.urlopen(urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', data=data), timeout=12).read()
    except Exception as e:  # never loses the request, never prints the token
        print('telegram notify failed:', type(e).__name__, flush=True)


def notify(b):
    notify_email(b)
    notify_telegram(b)


# ---------------------------------------------------------------- HTTP
def letter_key(ref):
    """The confirmation letter opens only with this key, so a booking number alone is not enough to read it."""
    return hmac.new(secret(), ('letter:' + ref).encode(), hashlib.sha256).hexdigest()[:20]


def row(r):
    d = dict(r)
    d['breakfast'] = bool(d['breakfast'])
    d['room_name'] = ROOMS.get(d.get('room') or '', ('Any room', 'أي غرفة'))[1]
    d['letter'] = '/api/confirmation/%s/%s' % (d['ref'], letter_key(d['ref']))
    return d


HOTEL = {'phone': '+963 998 730 730', 'wa': '963998730730', 'email': 'res@royalinn.org', 'site': 'royalinntartus.com'}
LETTER = {
    'ar': dict(dir='rtl', title='تأكيد حجز', hello='السيد/ة {name} المحترم/ة،', intro='يسرّنا تأكيد حجزكم في فندق رويال إن طرطوس. تفاصيل الحجز:',
               ref='رقم الحجز', guest='اسم الضيف', cin='تاريخ الوصول', cout='تاريخ المغادرة', nights='عدد الليالي', guests='عدد الضيوف', room='نوع الغرفة',
               bf='الفطور', yes='مع الفطور (10$ للشخص في اليوم)', no='غير مشمول (متوفر بـ 10$ للشخص)', total='المبلغ الإجمالي', ask='يُحدَّد من الفندق', tax='شامل الضريبة',
               from14='من الساعة 14:00', until12='حتى الساعة 12:00', terms='معلومات مهمة',
               t=['الدفع في الفندق نقداً، أو بحوالة إلى مصرف سوري بالدولار الأمريكي.', 'الإلغاء مجاني حتى 48 ساعة قبل موعد الوصول.',
                  'الاستقبال مفتوح على مدار 24 ساعة، وموقف السيارات مجاني للضيوف.', 'يُرجى إحضار هذا التأكيد أو رقم الحجز عند الوصول.'],
               bye='ننتظركم بكل سرور.', sign='إدارة الحجوزات، فندق رويال إن', addr='الكورنيش البحري، طرطوس، سوريا', issued='تاريخ الإصدار', print='طباعة / حفظ PDF',
               status='الحالة', confirmed='مؤكَّد', pending_h='الحجز لم يُؤكَّد بعد', pending_p='سيصلكم التأكيد من الفندق قريباً.', cancelled_h='هذا الحجز ملغى',
               none='الرابط غير صحيح.'),
    'en': dict(dir='ltr', title='Booking Confirmation', hello='Dear {name},', intro='We are pleased to confirm your booking at Royal Inn Tartus. Your booking details:',
               ref='Booking number', guest='Guest name', cin='Check-in', cout='Check-out', nights='Nights', guests='Guests', room='Room type',
               bf='Breakfast', yes='Included ($10 per person per day)', no='Not included (available at $10 per person)', total='Total amount', ask='To be confirmed by the hotel', tax='tax included',
               from14='from 14:00', until12='until 12:00', terms='Good to know',
               t=['Payment is made at the hotel in cash, or by transfer to a Syrian bank in US dollars.', 'Cancellation is free up to 48 hours before arrival.',
                  'Reception is open 24 hours and parking is free for guests.', 'Please bring this confirmation or your booking number on arrival.'],
               bye='We look forward to welcoming you.', sign='Reservations, Royal Inn Hotel', addr='Sea Corniche, Tartus, Syria', issued='Issued', print='Print / save as PDF',
               status='Status', confirmed='Confirmed', pending_h='This booking is not confirmed yet', pending_p='The hotel will send your confirmation shortly.', cancelled_h='This booking was cancelled',
               none='This link is not valid.'),
}


def letter_html(r):
    """Printable confirmation letter. Styles and the print button come from static files (the site allows no inline code)."""
    import html as _h
    e = lambda x: _h.escape(str(x), quote=True)
    L = LETTER['en' if (r and r['lang'] == 'en') else 'ar']; lang = 'en' if L['dir'] == 'ltr' else 'ar'
    fd = lambda ds: '/'.join(reversed(ds.split('-')))
    head = ('<!DOCTYPE html>\n<html lang="%s" dir="%s">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="robots" content="noindex, nofollow">\n<title>%s | Royal Inn Tartus</title>\n<link rel="icon" href="/favicon.ico">\n'
            '<link rel="stylesheet" href="/assets/css/fonts-g.css?v=1">\n<link rel="stylesheet" href="/panel/letter.css?v=1">\n<script src="/panel/letter.js?v=1" defer></script>\n</head>\n<body>\n'
            % (lang, L['dir'], e(L['title'])))
    top = ('<header class="lh"><img src="/assets/img/logo-stacked.svg" alt="Royal Inn" width="150" height="109">'
           '<div class="lh-c"><b>Royal Inn Hotel · فندق رويال إن</b><span>%s</span><span dir="ltr">%s · %s</span><span dir="ltr">%s</span></div></header>'
           % (e(L['addr']), HOTEL['phone'], HOTEL['email'], HOTEL['site']))
    if r is None or r['status'] != 'confirmed':
        msg = L['none'] if r is None else L['cancelled_h'] if r['status'] == 'cancelled' else L['pending_h']
        sub = '' if r is None or r['status'] == 'cancelled' else '<p>%s</p>' % e(L['pending_p'])
        return head + '<main class="sheet">' + top + '<h1>%s</h1>%s</main>\n</body>\n</html>\n' % (e(msg), sub)
    room = ROOMS.get(r['room'] or '', ('To be assigned by the hotel', 'تُحدَّد من الفندق'))[0 if lang == 'en' else 1]
    total = ('$%d <small>(%s)</small>' % (r['total'], e(L['tax']))) if r['total'] else e(L['ask'])
    rows = [(L['ref'], '<b dir="ltr">%s</b>' % e(r['ref'])), (L['status'], '<span class="ok">%s</span>' % e(L['confirmed'])), (L['guest'], e(r['name'])),
            (L['cin'], '<b dir="ltr">%s</b> <small>%s</small>' % (fd(r['check_in']), e(L['from14']))), (L['cout'], '<b dir="ltr">%s</b> <small>%s</small>' % (fd(r['check_out']), e(L['until12']))),
            (L['nights'], r['nights']), (L['guests'], r['guests']), (L['room'], e(room)), (L['bf'], e(L['yes'] if r['breakfast'] else L['no'])), (L['total'], '<b class="tot">%s</b>' % total)]
    table = '<table class="lt">' + ''.join('<tr><th>%s</th><td>%s</td></tr>' % (e(k), v) for k, v in rows) + '</table>'
    terms = '<h2>%s</h2><ul>%s</ul>' % (e(L['terms']), ''.join('<li>%s</li>' % e(x) for x in L['t']))
    foot = ('<p class="bye">%s</p><p class="sign">%s</p><p class="issued">%s: <span dir="ltr">%s</span></p>'
            % (e(L['bye']), e(L['sign']), e(L['issued']), fd(dt.date.today().isoformat())))
    return (head + '<div class="tools"><button type="button" data-print>%s</button></div><main class="sheet">' % e(L['print']) + top
            + '<h1>%s</h1><p class="hello">%s</p><p>%s</p>' % (e(L['title']), e(L['hello'].format(name=r['name'])), e(L['intro']))
            + table + terms + foot + '</main>\n</body>\n</html>\n')


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
        m = re.fullmatch(r'/api/confirmation/(RI-[A-Z0-9]{6})/([0-9a-f]{20})', p)
        if m:
            if limited('lt:' + self.ip(), 40, 600):
                return self.send(429, {'ok': False, 'error': 'busy'})
            r = None
            if hmac.compare_digest(letter_key(m.group(1)), m.group(2)):
                with db() as con:
                    r = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
            return self.send(200 if r else 404, raw=letter_html(r).encode('utf-8'), ctype='text/html; charset=utf-8', extra={'X-Robots-Tag': 'noindex, nofollow', 'Referrer-Policy': 'no-referrer'})
        if p == '/api/offers':
            with db() as con:
                rows = con.execute('SELECT * FROM offers WHERE date_to >= ? ORDER BY percent DESC, date_from', (dt.date.today().isoformat(),)).fetchall()
            return self.send(200, {'ok': True, 'items': [{'percent': r['percent'], 'title_ar': r['title_ar'], 'title_en': r['title_en'],
                                                         'from': r['date_from'], 'to': r['date_to'], 'min_nights': r['min_nights']} for r in rows]})
        if p == '/api/prices':
            with db() as con:
                rows = {r['room']: {'price': r['price'], 'price2': r['price2']} for r in con.execute('SELECT * FROM prices') if r['room'] in ROOMS}
            return self.send(200, {'ok': True, 'rooms': rows})
        if p == '/api/quote':
            if limited('av:' + self.ip(), 60, 60):
                return self.send(429, {'ok': False, 'error': 'busy'})
            dates, err = check_dates(q.get('in'), q.get('out'))
            room = q.get('room') or None
            try:
                guests = max(1, min(12, int(q.get('guests') or 2)))
            except ValueError:
                guests = 2
            if err or (room and room not in ROOMS):
                return self.send(400, {'ok': False, 'error': 'dates'})
            with db() as con:
                rooms = quote(con, dates[0], dates[1], guests, room)
                any_open = availability(con, dates[0], dates[1], None)
            return self.send(200, {'ok': True, 'nights': (dates[1] - dates[0]).days, 'open': any_open, 'rooms': rooms, 'breakfast': BREAKFAST})
        if p == '/api/booking/status':
            # a guest checks their own request: booking number + last four digits of the phone they gave
            if limited('st:' + self.ip(), 12, 600):
                return self.send(429, {'ok': False, 'error': 'busy'})
            ref = (q.get('ref') or '').strip().upper()
            last4 = re.sub(r'\D', '', q.get('phone') or '')[-4:]
            if not re.fullmatch(r'RI-[A-Z0-9]{6}', ref) or len(last4) != 4:
                return self.send(400, {'ok': False, 'error': 'fields'})
            with db() as con:
                r = con.execute('SELECT * FROM bookings WHERE ref=?', (ref,)).fetchone()
            if not r or re.sub(r'\D', '', r['phone'])[-4:] != last4:
                return self.send(404, {'ok': False, 'error': 'none'})
            return self.send(200, {'ok': True, 'ref': r['ref'], 'status': r['status'], 'in': r['check_in'], 'out': r['check_out'],
                                   'nights': r['nights'], 'guests': r['guests'], 'room': r['room'], 'total': r['total'], 'breakfast': bool(r['breakfast'])})
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
                if p == '/api/admin/dashboard':
                    # one screen for the front desk: today, tonight, and the next two weeks
                    today = dt.date.today(); ts = today.isoformat()
                    inv = {r['room']: r['count'] for r in con.execute('SELECT * FROM inventory')}
                    live = con.execute("SELECT * FROM bookings WHERE status!='cancelled' AND check_out >= ? AND check_in <= ?",
                                       (ts, (today + dt.timedelta(days=14)).isoformat())).fetchall()
                    cls = con.execute('SELECT room, date_from, date_to FROM closures WHERE date_to >= ?', (ts,)).fetchall()
                    conf = [x for x in live if x['status'] == 'confirmed']

                    def night(ds, k):
                        return sum(1 for x in conf if x['room'] == k and x['check_in'] <= ds < x['check_out'])
                    rooms = []
                    for k, v in ROOMS.items():
                        booked = night(ts, k); cnt = inv.get(k)
                        closed = any((c['room'] in (None, k)) and c['date_from'] <= ts <= c['date_to'] for c in cls)
                        rooms.append({'id': k, 'name': v[1], 'count': cnt, 'booked': booked, 'closed': closed,
                                      'free': None if cnt is None else max(0, cnt - booked)})
                    known = [r for r in rooms if r['count'] is not None]
                    total = sum(r['count'] for r in known) if len(known) == len(rooms) else None
                    week = []
                    for i in range(14):
                        d = today + dt.timedelta(days=i); ds = d.isoformat()
                        week.append({'date': ds, 'booked': sum(1 for x in conf if x['check_in'] <= ds < x['check_out']),
                                     'new': sum(1 for x in live if x['status'] == 'new' and x['check_in'] <= ds < x['check_out']),
                                     'closed': any(c['room'] is None and c['date_from'] <= ds <= c['date_to'] for c in cls)})
                    counts = {s: con.execute('SELECT COUNT(*) FROM bookings WHERE status=?', (s,)).fetchone()[0] for s in STATUSES}
                    newest = con.execute("SELECT * FROM bookings WHERE status='new' ORDER BY created DESC LIMIT 5").fetchall()
                    return self.send(200, {
                        'ok': True, 'today': ts, 'counts': counts, 'total_rooms': total, 'inventory_set': len(known),
                        'booked_tonight': sum(1 for x in conf if x['check_in'] <= ts < x['check_out']),
                        'in_house': sum(1 for x in conf if x['arrived'] and not x['departed']),
                        'unassigned_tonight': sum(1 for x in conf if not x['room'] and x['check_in'] <= ts < x['check_out']),
                        'arrivals': [row(x) for x in conf if x['check_in'] == ts],
                        'departures': [row(x) for x in conf if x['check_out'] == ts],
                        'tomorrow': [row(x) for x in conf if x['check_in'] == (today + dt.timedelta(days=1)).isoformat()],
                        'newest': [row(x) for x in newest], 'rooms': rooms, 'week': week})
                if p == '/api/admin/closures':
                    rows = con.execute('SELECT * FROM closures WHERE date_to >= ? ORDER BY date_from', (dt.date.today().isoformat(),)).fetchall()
                    return self.send(200, {'ok': True, 'items': [dict(r) for r in rows]})
                if p == '/api/admin/inventory':
                    inv = {r['room']: r['count'] for r in con.execute('SELECT * FROM inventory')}
                    return self.send(200, {'ok': True, 'rooms': [{'id': k, 'name': v[1], 'count': inv.get(k)} for k, v in ROOMS.items()]})
                if p == '/api/admin/prices':
                    rows = {r['room']: r for r in con.execute('SELECT * FROM prices')}
                    return self.send(200, {'ok': True, 'rooms': [{'id': k, 'name': v[1], 'price': rows[k]['price'] if k in rows else None,
                                                                   'price2': rows[k]['price2'] if k in rows else None} for k, v in ROOMS.items()]})
                if p == '/api/admin/offers':
                    rows = con.execute('SELECT * FROM offers ORDER BY date_to DESC, id DESC LIMIT 100').fetchall()
                    return self.send(200, {'ok': True, 'today': dt.date.today().isoformat(), 'items': [dict(r) for r in rows]})
                if p == '/api/admin/rates':
                    rows = con.execute('SELECT * FROM rates WHERE date_to >= ? ORDER BY date_from', (dt.date.today().isoformat(),)).fetchall()
                    return self.send(200, {'ok': True, 'items': [dict(r) for r in rows]})
                if p == '/api/admin/occupancy':
                    start = parse_day(q.get('from')) or dt.date.today()
                    try:
                        days = max(7, min(62, int(q.get('days') or 31)))
                    except ValueError:
                        days = 31
                    end = start + dt.timedelta(days=days)
                    dates = [start + dt.timedelta(days=i) for i in range(days)]
                    bks = con.execute("SELECT room, status, check_in, check_out FROM bookings WHERE status!='cancelled' AND check_in < ? AND check_out > ?",
                                      (end.isoformat(), start.isoformat())).fetchall()
                    inv = {r['room']: r['count'] for r in con.execute('SELECT * FROM inventory')}
                    cls = con.execute('SELECT room, date_from, date_to FROM closures WHERE date_from < ? AND date_to >= ?', (end.isoformat(), start.isoformat())).fetchall()
                    rows = []
                    for k, v in ROOMS.items():
                        cells = []
                        for d in dates:
                            ds = d.isoformat()
                            conf = sum(1 for x in bks if x['room'] == k and x['status'] == 'confirmed' and x['check_in'] <= ds < x['check_out'])
                            new = sum(1 for x in bks if x['room'] == k and x['status'] == 'new' and x['check_in'] <= ds < x['check_out'])
                            closed = any((c['room'] in (None, k)) and c['date_from'] <= ds <= c['date_to'] for c in cls)
                            cells.append([conf, new, 1 if closed else 0])
                        rows.append({'id': k, 'name': v[1], 'count': inv.get(k), 'cells': cells})
                    unassigned = [[sum(1 for x in bks if not x['room'] and x['status'] == s and x['check_in'] <= d.isoformat() < x['check_out']) for s in ('confirmed', 'new')] for d in dates]
                    return self.send(200, {'ok': True, 'dates': [d.isoformat() for d in dates], 'rows': rows, 'any': unassigned})
                if p == '/api/admin/export.csv':
                    out = io.StringIO(); w = csv.writer(out)
                    cols = ['ref', 'created', 'status', 'source', 'check_in', 'check_out', 'nights', 'guests', 'room', 'breakfast', 'total', 'paid', 'room_no', 'arrived', 'departed', 'name', 'phone', 'email', 'message', 'staff_note']
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
                total = None
                if v['room']:
                    _n = [a + dt.timedelta(days=i) for i in range((b - a).days)]
                    per, _pct = discounted(con, nightly(con, v['room'], v['guests'], _n), _n)
                    total = None if any(x is None for x in per) else sum(per)
                    if total is not None and v['breakfast']:
                        total += BREAKFAST * v['guests'] * len(per)
                rec = dict(total=total, source='web', ref=ref, created=now(), lang=v['lang'], status='new', check_in=a.isoformat(), check_out=b.isoformat(),
                           nights=(b - a).days, guests=v['guests'], room=v['room'], breakfast=v['breakfast'], name=v['name'],
                           phone=v['phone'], email=v['email'], message=v['message'])
                con.execute('INSERT INTO bookings(ref,created,lang,status,check_in,check_out,nights,guests,room,breakfast,name,phone,email,message,total,source) '
                            'VALUES(:ref,:created,:lang,:status,:check_in,:check_out,:nights,:guests,:room,:breakfast,:name,:phone,:email,:message,:total,:source)', rec)
            threading.Thread(target=notify, args=(rec,), daemon=True).start()
            return self.send(200, {'ok': True, 'ref': ref, 'available': avail, 'total': total})

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
            if p == '/api/admin/bookings/new':
                f, t = parse_day(d.get('in')), parse_day(d.get('out'))
                name, phone = clean(d.get('name'), 80), clean(d.get('phone'), 24)
                room = d.get('room') or None
                try:
                    guests = max(1, min(12, int(d.get('guests') or 2)))
                    total = int(d['total']) if d.get('total') not in (None, '') else None
                except (TypeError, ValueError):
                    return self.send(400, {'ok': False, 'error': 'fields'})
                if not f or not t or t <= f or (t - f).days > 120 or len(name) < 2 or (room and room not in ROOMS):
                    return self.send(400, {'ok': False, 'error': 'fields'})
                with LOCK, db() as con:
                    ref = new_ref()
                    while con.execute('SELECT 1 FROM bookings WHERE ref=?', (ref,)).fetchone():
                        ref = new_ref()
                    if total is None and room:
                        per = nightly(con, room, guests, [f + dt.timedelta(days=i) for i in range((t - f).days)])
                        total = None if any(x is None for x in per) else sum(per)
                    con.execute('INSERT INTO bookings(ref,created,lang,status,check_in,check_out,nights,guests,room,breakfast,name,phone,email,message,total,source,staff_note,updated) '
                                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                (ref, now(), 'ar', 'confirmed', f.isoformat(), t.isoformat(), (t - f).days, guests, room,
                                 1 if d.get('breakfast') else 0, name, phone or '-', None, clean(d.get('message'), 600) or None, total, 'desk',
                                 clean(d.get('note'), 400) or None, now()))
                return self.send(200, {'ok': True, 'ref': ref})
            if p == '/api/admin/prices':
                with db() as con:
                    for k in ROOMS:
                        if k not in d or not isinstance(d[k], dict):
                            continue
                        vals = []
                        for key in ('price', 'price2'):
                            x = d[k].get(key)
                            if x in (None, ''):
                                vals.append(None); continue
                            try:
                                x = int(x)
                            except (TypeError, ValueError):
                                return self.send(400, {'ok': False})
                            if not 0 < x < 100000:
                                return self.send(400, {'ok': False})
                            vals.append(x)
                        con.execute('INSERT INTO prices(room,price,price2) VALUES(?,?,?) ON CONFLICT(room) DO UPDATE SET price=excluded.price, price2=excluded.price2', (k, *vals))
                return self.send(200, {'ok': True})
            if p == '/api/admin/offers':
                f, t = parse_day(d.get('from')), parse_day(d.get('to') or d.get('from'))
                try:
                    pct = int(d['percent']); mn = int(d.get('min_nights') or 1)
                except (KeyError, TypeError, ValueError):
                    return self.send(400, {'ok': False, 'error': 'percent'})
                if not f or not t or t < f or (t - f).days > 400 or not 1 <= pct <= 70 or not 1 <= mn <= 30:
                    return self.send(400, {'ok': False, 'error': 'fields'})
                with db() as con:
                    con.execute('INSERT INTO offers(percent,title_ar,title_en,date_from,date_to,min_nights,created) VALUES(?,?,?,?,?,?,?)',
                                (pct, clean(d.get('title_ar'), 80) or None, clean(d.get('title_en'), 80) or None, f.isoformat(), t.isoformat(), mn, now()))
                return self.send(200, {'ok': True})
            m = re.fullmatch(r'/api/admin/offers/(\d+)/delete', p)
            if m:
                with db() as con:
                    con.execute('DELETE FROM offers WHERE id=?', (int(m.group(1)),))
                return self.send(200, {'ok': True})
            if p == '/api/admin/rates':
                f, t = parse_day(d.get('from')), parse_day(d.get('to') or d.get('from'))
                room = d.get('room') or None
                days = ','.join(sorted({str(int(x)) for x in (d.get('days') or []) if str(x).isdigit() and 0 <= int(x) <= 6}))
                try:
                    price = int(d['price']); price2 = int(d['price2']) if d.get('price2') not in (None, '') else None
                except (KeyError, TypeError, ValueError):
                    return self.send(400, {'ok': False, 'error': 'price'})
                if not f or not t or t < f or (t - f).days > 400 or (room and room not in ROOMS) or not 0 < price < 100000:
                    return self.send(400, {'ok': False, 'error': 'dates'})
                with db() as con:
                    con.execute('INSERT INTO rates(room,date_from,date_to,price,price2,days,note,created) VALUES(?,?,?,?,?,?,?,?)',
                                (room, f.isoformat(), t.isoformat(), price, price2, days or None, clean(d.get('note'), 120) or None, now()))
                return self.send(200, {'ok': True})
            m = re.fullmatch(r'/api/admin/rates/(\d+)/delete', p)
            if m:
                with db() as con:
                    con.execute('DELETE FROM rates WHERE id=?', (int(m.group(1)),))
                return self.send(200, {'ok': True})
            m = re.fullmatch(r'/api/admin/bookings/(RI-[A-Z0-9]{6})/stay', p)
            if m:
                # change the check-out date: extend or shorten a stay. {out, preview} -> what it costs; without preview it is saved.
                new_out = parse_day(d.get('out'))
                with LOCK, db() as con:
                    cur = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
                    if not cur:
                        return self.send(404, {'ok': False})
                    cin, old_out = dt.date.fromisoformat(cur['check_in']), dt.date.fromisoformat(cur['check_out'])
                    if not new_out or new_out <= cin or new_out == old_out or (new_out - cin).days > 120:
                        return self.send(400, {'ok': False, 'error': 'dates'})
                    a, b = (old_out, new_out) if new_out > old_out else (new_out, old_out)
                    nights = [a + dt.timedelta(days=i) for i in range((b - a).days)]
                    sgn = 1 if new_out > old_out else -1
                    free = True if sgn < 0 or cur['status'] != 'confirmed' else availability(con, a, b, cur['room'])
                    amount = None
                    if cur['room']:
                        per, _pct = discounted(con, nightly(con, cur['room'], cur['guests'], nights), nights)
                        if not any(x is None for x in per):
                            amount = sum(per) + (BREAKFAST * cur['guests'] * len(per) if cur['breakfast'] else 0)
                    total = None if (cur['total'] is None or amount is None) else max(0, cur['total'] + sgn * amount)
                    res = {'ok': True, 'nights': sgn * len(nights), 'amount': None if amount is None else sgn * amount, 'total': total,
                           'new_nights': (new_out - cin).days, 'available': free}
                    if d.get('preview'):
                        return self.send(200, res)
                    if not free and not d.get('force'):
                        return self.send(409, dict(res, ok=False, error='full'))
                    con.execute('UPDATE bookings SET check_out=?, nights=?, total=?, updated=? WHERE ref=?',
                                (new_out.isoformat(), (new_out - cin).days, total if total is not None else cur['total'], now(), m.group(1)))
                    r = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
                return self.send(200, dict(res, item=row(r)))
            m = re.fullmatch(r'/api/admin/bookings/(RI-[A-Z0-9]{6})', p)
            if m:
                st = d.get('status')
                if st is not None and st not in STATUSES:
                    return self.send(400, {'ok': False})
                with LOCK, db() as con:
                    cur = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
                    if not cur:
                        return self.send(404, {'ok': False})
                    try:
                        paid = cur['paid'] if 'paid' not in d else (None if d['paid'] in (None, '') else max(0, min(1000000, int(d['paid']))))
                    except (TypeError, ValueError):
                        return self.send(400, {'ok': False, 'error': 'paid'})
                    room_no = cur['room_no'] if 'room_no' not in d else (clean(d.get('room_no'), 12) or None)
                    arrived = cur['arrived'] if 'arrived' not in d else (now() if d['arrived'] else None)
                    departed = cur['departed'] if 'departed' not in d else (now() if d['departed'] else None)
                    con.execute('UPDATE bookings SET status=?, staff_note=?, room_no=?, paid=?, arrived=?, departed=?, updated=? WHERE ref=?',
                                (st or cur['status'], clean(d.get('note', cur['staff_note']), 400) or None, room_no, paid, arrived, departed, now(), m.group(1)))
                    r = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
                if st == 'confirmed' and cur['status'] != 'confirmed' and r['email']:
                    threading.Thread(target=mail_confirmation, args=(dict(r),), daemon=True).start()
                return self.send(200, {'ok': True, 'item': row(r), 'mailed': bool(st == 'confirmed' and cur['status'] != 'confirmed' and r['email'] and os.environ.get('SMTP_HOST'))})
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
