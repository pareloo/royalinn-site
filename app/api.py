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
        CREATE TABLE IF NOT EXISTS agencies(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, contact TEXT, phone TEXT, email TEXT,
            commission INTEGER NOT NULL DEFAULT 0, note TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS agency_payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, agency_id INTEGER NOT NULL, amount INTEGER NOT NULL, date TEXT NOT NULL, note TEXT, created TEXT);
        ''')
        cols = [r[1] for r in con.execute('PRAGMA table_info(bookings)')]
        for c, t in (('total', 'INTEGER'), ('source', 'TEXT'), ('room_no', 'TEXT'), ('paid', 'INTEGER'), ('arrived', 'TEXT'), ('departed', 'TEXT'),
                     ('agency_id', 'INTEGER'), ('pay_by', 'TEXT'), ('referrer', 'TEXT'), ('net', 'INTEGER')):
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
SOURCES = {'web': 'الموقع', 'desk': 'الاستقبال', 'phone': 'الهاتف', 'agency': 'وكالة', 'referral': 'عن طريق شخص'}
AGENCIES = {}          # id -> name, refreshed whenever agencies are read or changed


def load_agencies(con):
    AGENCIES.clear()
    AGENCIES.update({r['id']: r['name'] for r in con.execute('SELECT id, name FROM agencies')})


def net_of(con, total, agency_id):
    """What the agency owes the hotel for a booking: the total minus the agency's commission."""
    if total is None or not agency_id:
        return None
    a = con.execute('SELECT commission FROM agencies WHERE id=?', (agency_id,)).fetchone()
    return None if a is None else int(round(total * (100 - a['commission']) / 100.0))


def agency_sums(con, aid):
    """The agency's account with the hotel, from confirmed bookings and recorded payments."""
    bks = con.execute("SELECT * FROM bookings WHERE agency_id=? AND status='confirmed' ORDER BY check_in", (aid,)).fetchall()
    pays = con.execute('SELECT * FROM agency_payments WHERE agency_id=? ORDER BY date, id', (aid,)).fetchall()
    sales = sum(b['total'] or 0 for b in bks)
    due = sum(b['net'] if b['net'] is not None else (b['total'] or 0) for b in bks if b['pay_by'] == 'agency')
    commission = sum((b['total'] or 0) - (b['net'] if b['net'] is not None else (b['total'] or 0)) for b in bks if b['pay_by'] != 'agency')
    paid = sum(x['amount'] for x in pays)
    return bks, pays, {'bookings': len(bks), 'sales': sales, 'due': due, 'paid': paid, 'balance': due - paid, 'commission_owed': commission}


def letter_key(ref):
    """The confirmation letter opens only with this key, so a booking number alone is not enough to read it."""
    return hmac.new(secret(), ('letter:' + ref).encode(), hashlib.sha256).hexdigest()[:20]


def row(r):
    d = dict(r)
    d['breakfast'] = bool(d['breakfast'])
    d['room_name'] = ROOMS.get(d.get('room') or '', ('Any room', 'أي غرفة'))[1]
    d['letter'] = '/api/confirmation/%s/%s' % (d['ref'], letter_key(d['ref']))
    d['agency_name'] = AGENCIES.get(d.get('agency_id')) if d.get('agency_id') else None
    d['source_name'] = SOURCES.get(d.get('source') or 'web', 'الموقع')
    return d


HOTEL = {'phone': '+963 998 730 730', 'wa': '963998730730', 'email': 'res@royalinn.org', 'site': 'royalinntartus.com'}
# Labels are printed in both languages: the guest's language first, the other one small beside it.
LBL = {'ref': ('Confirmation no.', 'رقم التأكيد'), 'status': ('Status', 'الحالة'), 'issued': ('Date of issue', 'تاريخ الإصدار'), 'to': ('Guest name', 'اسم الضيف'),
       'phone': ('Phone', 'الهاتف'), 'company': ('Company / agency', 'الشركة / الوكالة'), 'via': ('Booked through', 'الحجز عن طريق'),
       'cin': ('Arrival', 'الوصول'), 'cout': ('Departure', 'المغادرة'), 'nights': ('Nights', 'الليالي'), 'guests': ('Guests', 'الضيوف'),
       'room': ('Room type', 'نوع الغرفة'), 'room_no': ('Room number', 'رقم الغرفة'), 'bf': ('Breakfast', 'الفطور'), 'rate': ('Average rate per night', 'متوسط سعر الليلة'),
       'total': ('Total amount', 'المبلغ الإجمالي'), 'paid': ('Paid', 'المدفوع'), 'due': ('Balance due at the hotel', 'المتبقي للدفع في الفندق'), 'pay': ('Payment', 'الدفع')}
SEC = {'res': ('Reservation', 'بيانات الحجز'), 'stay': ('Stay', 'الإقامة'), 'rate': ('Rate and payment', 'السعر والدفع'), 'pol': ('Hotel policies', 'سياسات الفندق')}
VIA = {'web': ('Hotel website', 'موقع الفندق'), 'desk': ('Hotel reception', 'استقبال الفندق'), 'phone': ('Telephone', 'الهاتف'), 'agency': ('Travel agency', 'وكالة سفر'), 'referral': ('Referral', 'توصية')}
LETTER = {
    'ar': dict(dir='rtl', title='تأكيد حجز', hello='السيد/ة {name} المحترم/ة،', intro='يسرّنا تأكيد حجزكم في فندق رويال إن طرطوس، ونتطلع إلى استقبالكم.',
               yes='مشمول (10$ للشخص في اليوم)', no='غير مشمول (متوفر بـ 10$ للشخص)', ask='يُحدَّد من الفندق', tax='شامل الضريبة', any='تُحدَّد من الفندق',
               from14='من الساعة 14:00', until12='حتى الساعة 12:00', confirmed='مؤكَّد', by_agency='يُسدَّد عن طريق الوكالة: {a}', at_hotel='في الفندق نقداً، أو بحوالة إلى مصرف سوري بالدولار الأمريكي',
               t=['الدفع في الفندق نقداً، أو بحوالة إلى مصرف سوري بالدولار الأمريكي.', 'الإلغاء مجاني حتى 48 ساعة قبل موعد الوصول.',
                  'الاستقبال مفتوح على مدار 24 ساعة، وموقف السيارات مجاني للضيوف.', 'يُرجى إحضار هذا التأكيد أو رقم الحجز عند الوصول.'],
               bye='ننتظركم بكل سرور.', sign='إدارة الحجوزات، فندق رويال إن', addr='الكورنيش البحري، طرطوس، سوريا', print='طباعة / حفظ PDF',
               pending_h='الحجز لم يُؤكَّد بعد', pending_p='سيصلكم التأكيد من الفندق قريباً.', cancelled_h='هذا الحجز ملغى', none='الرابط غير صحيح.'),
    'en': dict(dir='ltr', title='Booking Confirmation', hello='Dear {name},', intro='We are pleased to confirm your booking at Royal Inn Tartus and look forward to welcoming you.',
               yes='Included ($10 per person per day)', no='Not included (available at $10 per person)', ask='To be confirmed by the hotel', tax='tax included', any='To be assigned by the hotel',
               from14='from 14:00', until12='until 12:00', confirmed='Confirmed', by_agency='Settled through the agency: {a}', at_hotel='At the hotel in cash, or by transfer to a Syrian bank in US dollars',
               t=['Payment is made at the hotel in cash, or by transfer to a Syrian bank in US dollars.', 'Cancellation is free up to 48 hours before arrival.',
                  'Reception is open 24 hours and parking is free for guests.', 'Please bring this confirmation or your booking number on arrival.'],
               bye='We look forward to welcoming you.', sign='Reservations, Royal Inn Hotel', addr='Sea Corniche, Tartus, Syria', print='Print / save as PDF',
               pending_h='This booking is not confirmed yet', pending_p='The hotel will send your confirmation shortly.', cancelled_h='This booking was cancelled', none='This link is not valid.'),
}


def _esc(x):
    import html as _h
    return _h.escape(str(x), quote=True)


def _fd(ds):
    return '/'.join(reversed(ds.split('-')))


def doc_head(lang, direction, title):
    return ('<!DOCTYPE html>\n<html lang="%s" dir="%s">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="robots" content="noindex, nofollow">\n<title>%s | Royal Inn Tartus</title>\n<link rel="icon" href="/favicon.ico">\n'
            '<link rel="stylesheet" href="/assets/css/fonts-g.css?v=1">\n<link rel="stylesheet" href="/panel/letter.css?v=2">\n<script src="/panel/letter.js?v=1" defer></script>\n</head>\n<body>\n'
            % (lang, direction, _esc(title)))


def doc_band(title, sub=''):
    """Photo header used by every printed document (the hotel's own sunset view)."""
    return ('<header class="band"><img class="band-logo" src="/assets/img/logo-light.svg" alt="Royal Inn" width="150" height="60">'
            '<div class="band-t"><h1>%s</h1>%s</div></header>' % (title, sub))


def doc_foot(addr):
    return ('<footer class="lf"><b>Royal Inn Hotel · فندق رويال إن</b><span>%s</span><span dir="ltr">%s · %s · %s</span></footer>'
            % (_esc(addr), HOTEL['phone'], HOTEL['email'], HOTEL['site']))


def letter_html(r):
    """Printable confirmation letter. Styles and the print button come from static files (the site allows no inline code)."""
    e = _esc
    lang = 'en' if (r and r['lang'] == 'en') else 'ar'
    L = LETTER[lang]; i, j = (0, 1) if lang == 'en' else (1, 0)
    head = doc_head(lang, L['dir'], L['title'])
    if r is None or r['status'] != 'confirmed':
        msg = L['none'] if r is None else L['cancelled_h'] if r['status'] == 'cancelled' else L['pending_h']
        sub = '' if r is None or r['status'] == 'cancelled' else '<p>%s</p>' % e(L['pending_p'])
        return head + '<main class="sheet">' + doc_band(e(msg)) + '<div class="pad">%s</div>' % sub + doc_foot(L['addr']) + '</main>\n</body>\n</html>\n'
    keys = r.keys()
    g = lambda k: r[k] if k in keys else None
    lab = lambda k: '%s <i dir="auto">%s</i>' % (e(LBL[k][i]), e(LBL[k][j]))
    sec = lambda k, rows: ('<section class="sec"><h2>%s <i>%s</i></h2><table class="lt">%s</table></section>'
                           % (e(SEC[k][i]), e(SEC[k][j]), ''.join('<tr><th>%s</th><td>%s</td></tr>' % (lab(a), v) for a, v in rows)))
    agency = AGENCIES.get(g('agency_id')) if g('agency_id') else None
    room = ROOMS[r['room']][0 if lang == 'en' else 1] if r['room'] in ROOMS else L['any']
    res = [('ref', '<b dir="ltr">%s</b>' % e(r['ref'])), ('status', '<span class="ok">%s</span>' % e(L['confirmed'])),
           ('to', e(r['name']))]
    if r['phone'] and r['phone'] != '-':
        res.append(('phone', '<span dir="ltr">%s</span>' % e(r['phone'])))
    if agency:
        res.append(('company', e(agency)))
    src = g('source') or 'web'
    res.append(('via', e(VIA.get(src, VIA['web'])[i]) + ((' · ' + e(g('referrer'))) if (src == 'referral' and g('referrer')) else '')))
    res.append(('issued', '<span dir="ltr">%s</span>' % _fd(dt.date.today().isoformat())))
    stay = [('cin', '<b dir="ltr">%s</b> <small>%s</small>' % (_fd(r['check_in']), e(L['from14']))),
            ('cout', '<b dir="ltr">%s</b> <small>%s</small>' % (_fd(r['check_out']), e(L['until12']))),
            ('nights', r['nights']), ('guests', r['guests']), ('room', e(room))]
    if g('room_no'):
        stay.append(('room_no', '<span dir="ltr">%s</span>' % e(g('room_no'))))
    stay.append(('bf', e(L['yes'] if r['breakfast'] else L['no'])))
    if agency and g('pay_by') == 'agency':      # a voucher: the guest pays the agency, so no amounts are printed
        rate = [('pay', e(L['by_agency'].format(a=agency)))]
    elif r['total']:
        paid = g('paid') or 0
        rate = [('rate', '$%d' % round(r['total'] / max(1, r['nights']))), ('total', '<b class="tot">$%d</b> <small>(%s)</small>' % (r['total'], e(L['tax'])))]
        if paid:
            rate += [('paid', '$%d' % paid), ('due', '<b>$%d</b>' % max(0, r['total'] - paid))]
        rate.append(('pay', e(L['at_hotel'])))
    else:
        rate = [('total', e(L['ask'])), ('pay', e(L['at_hotel']))]
    pol = '<section class="sec"><h2>%s <i>%s</i></h2><ul class="pol">%s</ul></section>' % (e(SEC['pol'][i]), e(SEC['pol'][j]), ''.join('<li>%s</li>' % e(x) for x in L['t']))
    band = doc_band(e(L['title']) + (' <i>%s</i>' % e(LETTER['en' if lang == 'ar' else 'ar']['title'])), '<p dir="ltr">%s</p>' % e(r['ref']))
    return (head + '<div class="tools"><button type="button" data-print>%s</button></div><main class="sheet">' % e(L['print']) + band
            + '<div class="pad"><p class="hello">%s</p><p>%s</p>' % (e(L['hello'].format(name=r['name'])), e(L['intro']))
            + sec('res', res) + '<div class="two">' + sec('stay', stay) + sec('rate', rate) + '</div>' + pol
            + '<p class="bye">%s</p><p class="sign">%s</p></div>' % (e(L['bye']), e(L['sign'])) + doc_foot(L['addr']) + '</main>\n</body>\n</html>\n')


def pricelist_html(con, agency=None):
    """Printable rate sheet in Arabic and English. With an agency it also prints that agency's net rates."""
    e = _esc
    today = dt.date.today().isoformat()
    com = agency['commission'] if agency else 0
    money = lambda v: 'عند الطلب<i class="blk">on request</i>' if v is None else '$%d' % v
    net = lambda v: '' if v is None else '$%d' % round(v * (100 - com) / 100.0)
    prices = {x['room']: x for x in con.execute('SELECT * FROM prices')}
    th = '<tr><th>نوع الغرفة <i>Room type</i></th><th>شخص واحد <i>1 guest</i></th><th>شخصان أو أكثر <i>2+ guests</i></th>%s</tr>' % ('<th>الصافي للوكالة <i>Net</i></th>' if agency else '')
    body = ''
    for k, (en, ar) in ROOMS.items():
        pr = prices.get(k); p1 = pr['price'] if pr else None; p2 = pr['price2'] if pr else None
        nt = ' / '.join(x for x in (net(p1), net(p2) if p2 else '') if x)
        body += '<tr><td><b>%s</b> <i>%s</i></td><td>%s</td><td>%s</td>%s</tr>' % (e(ar), e(en), money(p1), money(p2) if p2 else ('—' if p1 is not None else money(None)), ('<td dir="ltr" class="num">%s</td>' % (nt or '—')) if agency else '')
    seasons = con.execute('SELECT * FROM rates WHERE date_to >= ? ORDER BY date_from, id', (today,)).fetchall()
    srows = ''
    for x in seasons:
        nm = ROOMS[x['room']] if x['room'] in ROOMS else ('All rooms', 'كل الغرف')
        srows += '<tr><td><span dir="ltr">%s – %s</span></td><td><b>%s</b> <i>%s</i></td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
            _fd(x['date_from']), _fd(x['date_to']), e(nm[1]), e(nm[0]), money(x['price']) if x['price'] is not None else '—', ('$%d' % x['price2']) if x['price2'] else '—', e(x['note'] or ''))
    season = ('<section class="sec"><h2>أسعار خاصة بتواريخ محددة <i>Rates for specific dates</i></h2><table class="grid"><tr><th>التاريخ <i>Dates</i></th><th>الغرفة <i>Room</i></th>'
              '<th>شخص <i>1 guest</i></th><th>شخصان+ <i>2+</i></th><th>ملاحظة <i>Note</i></th></tr>%s</table></section>' % srows) if srows else ''
    offers = con.execute('SELECT * FROM offers WHERE date_to >= ? ORDER BY date_from', (today,)).fetchall()
    off = ('<section class="sec"><h2>العروض الحالية <i>Current offers</i></h2><ul class="pol">%s</ul></section>' % ''.join(
        '<li><b>%d%%</b> · %s · <span dir="ltr">%s – %s</span>%s</li>' % (o['percent'], e(o['title_ar'] or o['title_en'] or ''), _fd(o['date_from']), _fd(o['date_to']),
                                                                       (' · %d+ ليالٍ / nights' % o['min_nights']) if o['min_nights'] > 1 else '') for o in offers)) if offers else ''
    notes = [('الأسعار بالدولار الأمريكي للغرفة في الليلة، شاملة الضريبة.', 'Rates are in US dollars per room per night, tax included.'),
             ('الفطور اختياري: %d$ للشخص في اليوم.' % BREAKFAST, 'Breakfast is optional: $%d per person per day.' % BREAKFAST),
             ('الوصول من 14:00 والمغادرة حتى 12:00.', 'Check-in from 14:00, check-out until 12:00.'),
             ('الإلغاء مجاني حتى 48 ساعة قبل الوصول.', 'Free cancellation up to 48 hours before arrival.'),
             ('الأسعار قابلة للتغيير، والسعر المؤكَّد هو المذكور في رسالة التأكيد.', 'Rates may change; the confirmed rate is the one on the confirmation letter.')]
    who = ('<p class="hello">%s · <span>عمولة / commission %d%%</span></p>' % (e(agency['name']), com)) if agency else ''
    return (doc_head('ar', 'rtl', 'قائمة الأسعار') + '<div class="tools"><button type="button" data-print>طباعة / حفظ PDF</button></div><main class="sheet">'
            + doc_band('قائمة الأسعار <i>Rate sheet</i>', '<p>%s <span dir="ltr">%s</span></p>' % ('أسعار الوكالات · Agency rates ·' if agency else '', _fd(today)))
            + '<div class="pad">' + who + '<section class="sec"><h2>الأسعار الأساسية <i>Standard rates</i></h2><table class="grid">' + th + body + '</table></section>' + season + off
            + '<section class="sec"><h2>ملاحظات <i>Notes</i></h2><ul class="pol">%s</ul></section></div>' % ''.join('<li>%s<i class="blk" dir="ltr">%s</i></li>' % (e(x), e(y)) for x, y in notes)
            + doc_foot('الكورنيش البحري، طرطوس، سوريا · Sea Corniche, Tartus, Syria') + '</main>\n</body>\n</html>\n')


def statement_html(a, bks, pays, sums):
    """Agency account statement: confirmed bookings, payments received and the balance."""
    e = _esc
    rows = ''
    for b in bks:
        tot = b['total'] or 0; nt = b['net'] if b['net'] is not None else tot
        by_ag = b['pay_by'] == 'agency'
        rows += '<tr><td dir="ltr">%s</td><td>%s</td><td dir="ltr">%s – %s</td><td>%d</td><td>$%d</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
            e(b['ref']), e(b['name']), _fd(b['check_in']), _fd(b['check_out']), b['nights'], tot, 'الوكالة' if by_ag else 'الضيف',
            ('$%d' % nt) if by_ag else '—', '—' if by_ag else ('$%d' % (tot - nt)))
    prow = ''.join('<tr><td dir="ltr">%s</td><td>$%d</td><td>%s</td></tr>' % (_fd(x['date']), x['amount'], e(x['note'] or '')) for x in pays)
    bal = sums['balance']
    summ = [('عدد الحجوزات المؤكدة', sums['bookings']), ('إجمالي المبيعات', '$%d' % sums['sales']), ('المستحق على الوكالة (الصافي)', '$%d' % sums['due']),
            ('المدفوع من الوكالة', '$%d' % sums['paid']), ('الرصيد المتبقي على الوكالة' if bal >= 0 else 'رصيد دائن للوكالة', '<b class="tot">$%d</b>' % abs(bal)),
            ('عمولة مستحقة للوكالة (حجوزات دفعها الضيف)', '$%d' % sums['commission_owed'])]
    contact = ' · '.join(e(x) for x in (a['contact'], a['phone'], a['email']) if x)
    return (doc_head('ar', 'rtl', 'كشف حساب') + '<div class="tools"><button type="button" data-print>طباعة / حفظ PDF</button></div><main class="sheet">'
            + doc_band('كشف حساب <i>Statement of account</i>', '<p>%s · <span dir="ltr">%s</span></p>' % (e(a['name']), _fd(dt.date.today().isoformat())))
            + '<div class="pad"><p class="hello">%s · عمولة %d%%</p>%s' % (e(a['name']), a['commission'], ('<p dir="auto">%s</p>' % contact) if contact else '')
            + '<section class="sec"><h2>الحجوزات المؤكدة <i>Bookings</i></h2><table class="grid sm"><tr><th>الرقم</th><th>الضيف</th><th>التواريخ</th><th>ليالٍ</th><th>الإجمالي</th><th>الدافع</th><th>المستحق</th><th>العمولة</th></tr>%s</table></section>' % (rows or '<tr><td colspan="8">لا توجد حجوزات مؤكدة.</td></tr>')
            + '<section class="sec"><h2>الدفعات المستلمة <i>Payments</i></h2><table class="grid sm"><tr><th>التاريخ</th><th>المبلغ</th><th>ملاحظة</th></tr>%s</table></section>' % (prow or '<tr><td colspan="3">لا توجد دفعات.</td></tr>')
            + '<section class="sec"><h2>الخلاصة <i>Summary</i></h2><table class="lt">%s</table></section></div>' % ''.join('<tr><th>%s</th><td>%s</td></tr>' % (e(k), v) for k, v in summ)
            + doc_foot('الكورنيش البحري، طرطوس، سوريا') + '</main>\n</body>\n</html>\n')


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
                    load_agencies(con)
            return self.send(200 if r else 404, raw=letter_html(r).encode('utf-8'), ctype='text/html; charset=utf-8', extra={'X-Robots-Tag': 'noindex, nofollow', 'Referrer-Policy': 'no-referrer'})
        m = re.fullmatch(r'/api/pricelist(?:/(\d+)/([0-9a-f]{20}))?', p)
        if m:
            agency = None
            with db() as con:
                if m.group(1):
                    if not hmac.compare_digest(letter_key('agency:' + m.group(1)), m.group(2)):
                        return self.send(404, {'ok': False})
                    agency = con.execute('SELECT * FROM agencies WHERE id=?', (int(m.group(1)),)).fetchone()
                    if not agency:
                        return self.send(404, {'ok': False})
                html_doc = pricelist_html(con, agency)
            return self.send(200, raw=html_doc.encode('utf-8'), ctype='text/html; charset=utf-8', extra={'X-Robots-Tag': 'noindex, nofollow'})
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
                    load_agencies(con)
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
                    load_agencies(con)
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
                if p == '/api/admin/agencies':
                    load_agencies(con)
                    items = []
                    for a in con.execute('SELECT * FROM agencies ORDER BY name').fetchall():
                        _b, _p, sums = agency_sums(con, a['id'])
                        items.append(dict(dict(a), **sums))
                    return self.send(200, {'ok': True, 'items': items})
                m = re.fullmatch(r'/api/admin/agencies/(\d+)', p)
                if m:
                    a = con.execute('SELECT * FROM agencies WHERE id=?', (int(m.group(1)),)).fetchone()
                    if not a:
                        return self.send(404, {'ok': False})
                    load_agencies(con)
                    bks, pays, sums = agency_sums(con, a['id'])
                    return self.send(200, {'ok': True, 'agency': dict(a), 'sums': sums, 'bookings': [row(b) for b in bks], 'payments': [dict(x) for x in pays],
                                           'pricelist': '/api/pricelist/%d/%s' % (a['id'], letter_key('agency:%d' % a['id']))})
                m = re.fullmatch(r'/api/admin/agencies/(\d+)/statement', p)
                if m:
                    a = con.execute('SELECT * FROM agencies WHERE id=?', (int(m.group(1)),)).fetchone()
                    if not a:
                        return self.send(404, {'ok': False})
                    bks, pays, sums = agency_sums(con, a['id'])
                    return self.send(200, raw=statement_html(a, bks, pays, sums).encode('utf-8'), ctype='text/html; charset=utf-8')
                if p == '/api/admin/day':
                    d0 = parse_day(q.get('date')) or dt.date.today(); ds = d0.isoformat()
                    load_agencies(con)
                    rows = con.execute("SELECT * FROM bookings WHERE status!='cancelled' AND check_in <= ? AND check_out >= ? ORDER BY check_in", (ds, ds)).fetchall()
                    return self.send(200, {'ok': True, 'date': ds,
                                           'arrivals': [row(x) for x in rows if x['check_in'] == ds],
                                           'departures': [row(x) for x in rows if x['check_out'] == ds],
                                           'staying': [row(x) for x in rows if x['check_in'] < ds < x['check_out']]})
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
                    cols = ['ref', 'created', 'status', 'source', 'check_in', 'check_out', 'nights', 'guests', 'room', 'breakfast', 'total', 'paid', 'room_no', 'arrived', 'departed', 'name', 'phone', 'email', 'message', 'staff_note', 'pay_by', 'net', 'referrer']
                    load_agencies(con)
                    w.writerow(cols + ['agency'])
                    for r in con.execute('SELECT * FROM bookings ORDER BY created'):
                        w.writerow(['' if r[c] is None else r[c] for c in cols] + [AGENCIES.get(r['agency_id'], '') if r['agency_id'] else ''])
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
                        if total is not None and d.get('breakfast'):
                            total += BREAKFAST * guests * len(per)
                    src = d.get('source') if d.get('source') in ('desk', 'phone', 'agency', 'referral') else 'desk'
                    try:
                        aid = int(d['agency_id']) if (src == 'agency' and d.get('agency_id')) else None
                    except (TypeError, ValueError):
                        aid = None
                    if aid and not con.execute('SELECT 1 FROM agencies WHERE id=?', (aid,)).fetchone():
                        aid = None
                    pay_by = 'agency' if (aid and d.get('pay_by') == 'agency') else 'guest'
                    con.execute('INSERT INTO bookings(ref,created,lang,status,check_in,check_out,nights,guests,room,breakfast,name,phone,email,message,total,source,staff_note,updated,agency_id,pay_by,referrer,net) '
                                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                (ref, now(), 'en' if d.get('lang') == 'en' else 'ar', 'confirmed', f.isoformat(), t.isoformat(), (t - f).days, guests, room,
                                 1 if d.get('breakfast') else 0, name, phone or '-', None, clean(d.get('message'), 600) or None, total, src,
                                 clean(d.get('note'), 400) or None, now(), aid, pay_by, clean(d.get('referrer'), 80) or None, net_of(con, total, aid)))
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
            m = re.fullmatch(r'/api/admin/agencies(?:/(\d+))?', p)
            if m:
                name = clean(d.get('name'), 80)
                try:
                    com = max(0, min(50, int(d.get('commission') or 0)))
                except (TypeError, ValueError):
                    return self.send(400, {'ok': False, 'error': 'commission'})
                if len(name) < 2:
                    return self.send(400, {'ok': False, 'error': 'name'})
                vals = (name, clean(d.get('contact'), 80) or None, clean(d.get('phone'), 24) or None, clean(d.get('email'), 120) or None, com, clean(d.get('note'), 300) or None)
                with LOCK, db() as con:
                    if m.group(1):
                        con.execute('UPDATE agencies SET name=?, contact=?, phone=?, email=?, commission=?, note=? WHERE id=?', vals + (int(m.group(1)),))
                    else:
                        con.execute('INSERT INTO agencies(name,contact,phone,email,commission,note,created) VALUES(?,?,?,?,?,?,?)', vals + (now(),))
                    load_agencies(con)
                return self.send(200, {'ok': True})
            m = re.fullmatch(r'/api/admin/agencies/(\d+)/payment', p)
            if m:
                day = parse_day(d.get('date')) or dt.date.today()
                try:
                    amount = int(d['amount'])
                except (KeyError, TypeError, ValueError):
                    return self.send(400, {'ok': False, 'error': 'amount'})
                if not 0 < amount < 10000000:
                    return self.send(400, {'ok': False, 'error': 'amount'})
                with db() as con:
                    if not con.execute('SELECT 1 FROM agencies WHERE id=?', (int(m.group(1)),)).fetchone():
                        return self.send(404, {'ok': False})
                    con.execute('INSERT INTO agency_payments(agency_id,amount,date,note,created) VALUES(?,?,?,?,?)', (int(m.group(1)), amount, day.isoformat(), clean(d.get('note'), 200) or None, now()))
                return self.send(200, {'ok': True})
            m = re.fullmatch(r'/api/admin/agency-payments/(\d+)/delete', p)
            if m:
                with db() as con:
                    con.execute('DELETE FROM agency_payments WHERE id=?', (int(m.group(1)),))
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
                    _t = total if total is not None else cur['total']
                    con.execute('UPDATE bookings SET check_out=?, nights=?, total=?, net=?, updated=? WHERE ref=?',
                                (new_out.isoformat(), (new_out - cin).days, _t, net_of(con, _t, cur['agency_id']), now(), m.group(1)))
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
                    aid, pay_by, src = cur['agency_id'], cur['pay_by'], cur['source']
                    if 'agency_id' in d:
                        try:
                            aid = int(d['agency_id']) if d['agency_id'] else None
                        except (TypeError, ValueError):
                            aid = None
                        if aid and not con.execute('SELECT 1 FROM agencies WHERE id=?', (aid,)).fetchone():
                            aid = None
                        pay_by = 'agency' if (aid and d.get('pay_by') == 'agency') else 'guest'
                        src = cur['source'] or 'web'       # a website request keeps its origin even when an agency is attached
                        if aid and src in ('desk', 'phone'):
                            src = 'agency'
                        elif not aid and src == 'agency':
                            src = 'desk'
                    referrer = cur['referrer'] if 'referrer' not in d else (clean(d.get('referrer'), 80) or None)
                    con.execute('UPDATE bookings SET status=?, staff_note=?, room_no=?, paid=?, arrived=?, departed=?, agency_id=?, pay_by=?, source=?, referrer=?, net=?, updated=? WHERE ref=?',
                                (st or cur['status'], clean(d.get('note', cur['staff_note']), 400) or None, room_no, paid, arrived, departed, aid, pay_by, src, referrer, net_of(con, cur['total'], aid), now(), m.group(1)))
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
