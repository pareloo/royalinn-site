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
import csv, datetime as dt, hashlib, hmac, io, json, math, os, re, secrets, smtplib, sqlite3, threading, time
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
    'royal-family': ('Royal Suite with connecting room', 'جناح رويال مع غرفة متصلة'),
}
FAMILY = 'royal-family'
UNIT_TYPES = ('standard', 'triple', 'junior-suite', 'royal-suite')   # physical rooms; the connecting rooms are CONNECT units
# The Royal Suite is sold on its own (two guests) or merged with its connecting room (up to four; the connecting room at the Standard rate).
# Which connecting rooms are merged is set per room in the panel's room rack; an unmerged connecting room is sold as a Standard room.
# The two-bedroom suite is no longer sold (hotel, Nov 2026); its name stays for old bookings.
CONNECT = 'royal-connect'
# most guests per room type (hotel, Nov 2026); the Royal Suite counts with its connecting room
MAX_GUESTS = {'standard': 2, 'triple': 3, 'junior-suite': 2, 'royal-suite': 2, 'royal-family': 4}
# fewest guests a room is sold to (hotel, 25 Nov 2026): the Triple Room is for three; the suite with its connecting room for three or four
MIN_GUESTS = {'triple': 3, 'royal-family': 3}


def type_fits(room, guests):
    """Can the party take rooms of this one type? Enough rooms for everyone, and each room with at least its minimum."""
    n = rooms_needed(room, guests)
    return guests >= n * MIN_GUESTS.get(room, 1)
MAX_ANY = max(MAX_GUESTS.values())


MAX_PARTY = 40   # larger parties use the groups request form


def too_many(room, guests):
    """No extra beds: a bigger party gets more rooms, so only the party size itself is limited."""
    return guests > MAX_PARTY


def rooms_needed(room, guests):
    """Rooms of this type for the party: every room holds at most MAX_GUESTS (no extra beds).
    'Any room' is counted in Standard rooms (two guests each); the hotel can change it in the panel."""
    cap = MAX_GUESTS.get(room, MAX_GUESTS['standard']) if room else MAX_GUESTS['standard']
    return max(1, -(-int(guests) // cap))


def _g(b, k):
    try:
        return b[k]
    except (KeyError, IndexError):
        return None


def units(b):
    """Rooms a booking holds, by type: {'triple': 1, 'standard': 2}."""
    c = _g(b, 'combo')
    if c:
        try:
            return {k: int(v) for k, v in json.loads(c).items() if k in ROOMS}
        except (ValueError, TypeError, AttributeError):
            pass
    return {b['room']: (_g(b, 'rooms') or 1)} if _g(b, 'room') else {}


def room_nos(v):
    """Room numbers a booking holds: '101, 102' -> ['101', '102'] (one booking can hold several rooms)."""
    return [x for x in re.split(r'[\s,،;+/]+', str(v or '')) if x][:40]


def slots(b):
    """The physical rooms a booking needs numbers for, in order: a family suite needs its suite and its connecting room."""
    out = []
    for k, n in units(b).items():
        for _ in range(n):
            out += ['royal-suite', CONNECT] if k == FAMILY else [k]
    return out or [None] * max(1, _g(b, 'rooms') or 1)


def r5(x):
    """Money the guest sees or pays: whole dollars to the nearest 5, no small change (hotel, 25 Nov 2026)."""
    return int(5 * math.floor(x / 5.0 + 0.5))      # halves round up (112.5 -> 115), like the site's own Math.round


def held(u):
    """What a booking takes from the hotel: a merged Royal Suite holds one suite and one connecting room."""
    u = dict(u)
    if u.get(FAMILY):
        u['royal-suite'] = u.get('royal-suite', 0) + u[FAMILY]
    return u


def room_label(b, i, fallback):
    """'Triple Room × 1 + Standard Room × 2' (i = 0 English, 1 Arabic)."""
    u = units(b)
    if not u:
        return NAMES[b['room']][i] if _g(b, 'room') in NAMES else fallback
    return ' + '.join(NAMES[k][i] + ((' × %d' % n) if (n > 1 or len(u) > 1) else '') for k, n in u.items())


def parse_combo(raw, guests):
    """A room combination chosen on the site: {type: count}. None when invalid (too small for the party, empty rooms, unknown type)."""
    try:
        c = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        if not isinstance(c, dict) or any(type(v) is not int or v < 1 for v in c.values()):
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    if not c or any(k not in ROOMS for k in c) or sum(MIN_GUESTS.get(k, 1) * n for k, n in c.items()) > guests or sum(MAX_GUESTS[k] * n for k, n in c.items()) < guests:
        return None
    return c      # every room holds at least one guest; guests may each take their own room


def allocate(combo, guests):
    """Guests per room for a combination: one guest in every room first, then the rest where it adds least to the price."""
    rooms = [[k, MIN_GUESTS.get(k, 1)] for k, n in sorted(combo.items()) for _ in range(n)]
    left = guests - len(rooms)
    # rooms whose price does not change with more guests fill first; Standard rooms (single/double rate) last
    order = sorted(range(len(rooms)), key=lambda i: (rooms[i][0] == 'standard', rooms[i][0] == FAMILY))
    for i in order:
        add = min(left, MAX_GUESTS[rooms[i][0]] - rooms[i][1])
        rooms[i][1] += add; left -= add
    return [(k, g) for k, g in rooms]


def combo_nightly(con, combo, guests, nights, memo=None):
    memo = {} if memo is None else memo
    tot = [0] * len(nights); parts = []
    for k, g in allocate(combo, guests):
        if (k, g) not in memo:
            memo[(k, g)] = _one_room(con, k, g, nights)
        per = memo[(k, g)]; parts.append(per)
        for i, x in enumerate(per):
            tot[i] = None if (x is None or tot[i] is None) else tot[i] + x
    return Nights(tot, parts)


def free_units(con, a, b, skip=None):
    """Rooms still free of each type on the busiest night of the stay; None for types without a room count."""
    nights = [a + dt.timedelta(days=i) for i in range((b - a).days)]
    inv = {r['room']: r['count'] for r in con.execute('SELECT room, count FROM inventory')}
    booked = con.execute("SELECT * FROM bookings WHERE status='confirmed' AND check_in < ? AND check_out > ? AND ref != ?",
                         (b.isoformat(), a.isoformat(), skip or '')).fetchall()
    out = {}
    for k in ROOMS:
        if k not in inv:
            out[k] = None; continue
        used = max(sum(held(units(x)).get(k, 0) for x in booked if x['check_in'] <= n.isoformat() < x['check_out']) for n in nights)
        out[k] = max(inv[k] - used, 0)
    return out


def closed(con, a, b):
    """Room types the hotel closed on any night of the stay ('*' = the whole hotel)."""
    nights = [a + dt.timedelta(days=i) for i in range((b - a).days)]
    out = set()
    for r in con.execute('SELECT room, date_from, date_to FROM closures WHERE date_from < ? AND date_to >= ?', (b.isoformat(), a.isoformat())):
        f, t = dt.date.fromisoformat(r['date_from']), dt.date.fromisoformat(r['date_to'])
        if any(f <= n <= t for n in nights):
            out.add(r['room'] or '*')
    return out


def combo_free(con, a, b, combo, skip=None):
    shut = closed(con, a, b)
    if '*' in shut:
        return False
    free = free_units(con, a, b, skip)
    return all(k not in shut and (free[k] is None or free[k] >= n) for k, n in held(combo).items())


def suggest(con, a, b, guests, limit=4):
    """Room combinations for the party that are free on these dates, cheapest first, each with its price per night and total.
    Only priced room types are combined; suites priced on request appear on their own."""
    nights = [a + dt.timedelta(days=i) for i in range((b - a).days)]
    shut = closed(con, a, b)
    if '*' in shut:
        return []
    free = free_units(con, a, b)
    memo = {}
    types = [k for k in ROOMS if k not in shut and free[k] != 0]
    priced = [k for k in types if None not in _one_room(con, k, 1, nights)]
    cap = {k: min(-(-guests // 1), free[k] if free[k] is not None else guests) for k in types}
    found = []

    def walk(i, combo, room_cap, rooms):
        if room_cap >= guests:
            found.append(dict(combo)); return
        if i == len(priced) or rooms >= guests:
            return
        k = priced[i]
        for n in range(0, cap[k] + 1):
            if rooms + n > guests:
                break
            if n:
                combo[k] = n
            walk(i + 1, combo, room_cap + n * MAX_GUESTS[k], rooms + n)
            if room_cap + n * MAX_GUESTS[k] >= guests:
                break
        combo.pop(k, None)
    walk(0, {}, 0, 0)
    for k in types:                        # a suite on request, alone, when it fits the party
        if k not in priced and MAX_GUESTS[k] >= guests and None in _one_room(con, k, 1, nights[:1]) and not any(_one_room(con, k, 1, nights)):
            found.append({k: 1})
    found = [c for c in found if sum(MIN_GUESTS.get(k, 1) * n for k, n in c.items()) <= guests and combo_free(con, a, b, c)]
    opts = []
    for c in found:
        base = combo_nightly(con, c, guests, nights, memo)
        per, pct = discounted(con, base, nights)
        total = None if None in per else sum(per)
        base_total = None if None in base else sum(base)
        opts.append({'rooms': c, 'count': sum(c.values()), 'total': total, 'per_night': None if total is None else r5(total / len(nights)),
                     'base_total': base_total if pct and base_total != total else None, 'offer_percent': pct if base_total != total else None,
                     'name_en': room_label({'combo': json.dumps(c)}, 0, ''), 'name_ar': room_label({'combo': json.dumps(c)}, 1, '')})
    opts.sort(key=lambda o: (o['total'] is None, o['total'] or 0, o['count']))
    seen, out = set(), []
    fewest = min((o['count'] for o in opts if o['total'] is not None), default=None)
    for o in opts:                          # cheapest options, plus the one with the fewest rooms
        key = json.dumps(o['rooms'], sort_keys=True)
        if key in seen:
            continue
        if len(out) < limit - 1 or (o['count'] == fewest and not any(x['count'] == fewest for x in out)) or o['total'] is None:
            out.append(o); seen.add(key)
        if len(out) >= limit + 1:
            break
    return out[:limit + 1]


def free_units_safe(a, b):
    with db() as con:
        shut = closed(con, a, b); f = free_units(con, a, b)
    return {k: (0 if ('*' in shut or k in shut) else v) for k, v in f.items()}


def party_ar(b):
    """'5' or '5 · عدد الغرف: 3' for staff messages."""
    n = (b['rooms'] if 'rooms' in b.keys() else None) or 1
    return '%s · عدد الغرف: %s' % (b['guests'], n) if n > 1 else str(b['guests'])


def split(room, guests):
    """Guests in each room, filled evenly: 5 in Standard rooms -> [2, 2, 1]."""
    n = rooms_needed(room, guests)
    return [guests // n + (1 if i < guests % n else 0) for i in range(n)]
NAMES = dict(ROOMS, **{'two-bedroom-royal-suite': ('Two-Bedroom Royal Suite', 'جناح رويال بغرفتَي نوم'),
                       CONNECT: ('Connecting room (Royal Suite)', 'غرفة متصلة بجناح رويال')})
STATUSES = ('new', 'confirmed', 'cancelled')
# rates confirmed by the hotel (Nov 2026), USD per night incl. tax; price2 = two or more guests. None = on request
DEFAULT_PRICES = {'standard': (90, 100), 'triple': (130, None), 'junior-suite': (130, None), 'royal-suite': (150, None)}   # royal-family = suite + Standard rate
BREAKFAST = 10
MAX_NIGHTS, MAX_AHEAD_DAYS = 60, 550
os.environ['TZ'] = os.environ.get('HOTEL_TZ', '<+03>-3')
time.tzset()
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
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS room_info(room TEXT PRIMARY KEY, ar TEXT, en TEXT, tr TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS units(
            id INTEGER PRIMARY KEY AUTOINCREMENT, no TEXT NOT NULL UNIQUE, room TEXT NOT NULL, floor TEXT, state TEXT NOT NULL DEFAULT 'clean', note TEXT);
        CREATE TABLE IF NOT EXISTS agencies(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, contact TEXT, phone TEXT, email TEXT,
            commission INTEGER NOT NULL DEFAULT 0, note TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS inquiries(
            id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, lang TEXT, kind TEXT NOT NULL, name TEXT NOT NULL, company TEXT,
            phone TEXT NOT NULL, email TEXT, date TEXT, guests INTEGER, rooms INTEGER, nights INTEGER, message TEXT,
            status TEXT NOT NULL DEFAULT 'new', staff_note TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS agency_payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, agency_id INTEGER NOT NULL, amount INTEGER NOT NULL, date TEXT NOT NULL, note TEXT, created TEXT);
        ''')
        cols = [r[1] for r in con.execute('PRAGMA table_info(bookings)')]
        for c, t in (('total', 'INTEGER'), ('source', 'TEXT'), ('room_no', 'TEXT'), ('paid', 'INTEGER'), ('arrived', 'TEXT'), ('departed', 'TEXT'),
                     ('agency_id', 'INTEGER'), ('pay_by', 'TEXT'), ('referrer', 'TEXT'), ('net', 'INTEGER'),
                     ('reminded', 'TEXT'), ('late_fee', 'INTEGER'), ('no_show', 'INTEGER'), ('rooms', 'INTEGER'), ('combo', 'TEXT'), ('req_room', 'TEXT'), ('req_combo', 'TEXT')):
            if c not in cols:
                con.execute(f'ALTER TABLE bookings ADD COLUMN {c} {t}')
        ucols = [r[1] for r in con.execute('PRAGMA table_info(units)')]
        for c, t in (('pair', 'TEXT'), ('merge', 'INTEGER DEFAULT 1'), ('view', 'TEXT')):
            if c not in ucols:
                con.execute(f'ALTER TABLE units ADD COLUMN {c} {t}')
        # five room types (hotel, 25 Nov 2026): until now 'royal-suite' meant the suite with its connecting room
        if not con.execute("SELECT 1 FROM settings WHERE k='split_royal_2611'").fetchone():
            con.execute("UPDATE bookings SET room=? WHERE room='royal-suite'", (FAMILY,))
            for r in con.execute("SELECT ref, combo FROM bookings WHERE combo LIKE '%royal-suite%'").fetchall():
                try:
                    c = json.loads(r['combo']); c[FAMILY] = c.pop('royal-suite'); con.execute('UPDATE bookings SET combo=? WHERE ref=?', (json.dumps(c), r['ref']))
                except (ValueError, KeyError):
                    pass
            inv = con.execute("SELECT count FROM inventory WHERE room='royal-suite'").fetchone()
            if inv and not con.execute("SELECT 1 FROM inventory WHERE room=?", (FAMILY,)).fetchone():
                con.execute('INSERT INTO inventory(room,count) VALUES(?,?)', (FAMILY, inv['count']))
            sync_inventory(con)
            con.execute("INSERT INTO settings(k,v) VALUES('split_royal_2611','1')")
        # New Year offer asked for by the hotel (25 Nov 2026): 20% off every night until 31 December. Added once;
        # if the hotel deletes it in the panel it stays deleted.
        if not con.execute("SELECT 1 FROM settings WHERE k='seed_offer_ny2027'").fetchone():
            con.execute('INSERT INTO offers(percent,title_ar,title_en,date_from,date_to,min_nights,created) VALUES(?,?,?,?,?,?,?)',
                        (20, 'عرض خاص لفترة محدودة', 'Limited-time offer', '2026-11-25', '2026-12-27', 1, dt.datetime.now().isoformat(timespec='seconds')))
            con.execute("INSERT INTO settings(k,v) VALUES('seed_offer_ny2027','1')")
        # the hotel wants it presented as an offer that starts now, not a New Year offer (25 Nov 2026)
        if not con.execute("SELECT 1 FROM settings WHERE k='rename_offer_ny2027'").fetchone():
            con.execute("UPDATE offers SET title_ar=?, title_en=? WHERE title_en='New Year offer'", ('عرض خاص لفترة محدودة', 'Limited-time offer'))
            con.execute("INSERT INTO settings(k,v) VALUES('rename_offer_ny2027','1')")
        # the offer ends on the night of 27 December: New Year nights are sold at the normal rate (asked 25 Nov 2026)
        if not con.execute("SELECT 1 FROM settings WHERE k='offer_ends_2712'").fetchone():
            con.execute("UPDATE offers SET date_to='2026-12-27' WHERE title_en='Limited-time offer' AND date_to='2026-12-31'")
            con.execute("INSERT INTO settings(k,v) VALUES('offer_ends_2712','1')")
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
    if a < today or b <= a or (b - a).days > MAX_NIGHTS or a > today + dt.timedelta(days=MAX_AHEAD_DAYS):
        return None, 'dates'
    return (a, b), None


def booking_fits(con, r):
    """Would confirming this booking keep every night within the hotel's rooms (closures included)?"""
    a, b = dt.date.fromisoformat(r['check_in']), dt.date.fromisoformat(r['check_out'])
    c = _g(r, 'combo')
    if c:
        try:
            return combo_free(con, a, b, json.loads(c), r['ref'])
        except ValueError:
            pass
    return availability(con, a, b, r['room'], r['ref'], need=_g(r, 'rooms') or rooms_needed(r['room'], r['guests']))


DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')


def availability(con, a, b, room, skip=None, need=1):
    if room == FAMILY:
        return combo_free(con, a, b, {FAMILY: need}, skip) and _availability(con, a, b, None, skip)
    return _availability(con, a, b, room, skip, need)


def _availability(con, a, b, room, skip=None, need=1):
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
            booked = con.execute("SELECT * FROM bookings WHERE status='confirmed' AND check_in < ? AND check_out > ? AND ref != ?",
                                 (b.isoformat(), a.isoformat(), skip or '')).fetchall()
            for n in nights:
                used = sum(held(units(x)).get(room, 0) for x in booked if dt.date.fromisoformat(x['check_in']) <= n < dt.date.fromisoformat(x['check_out']))
                if used + need > inv['count']:
                    return False
    return True


class Nights(list):
    """Nightly prices for several rooms together; .parts keeps each room's own prices, so offers round per room."""
    def __init__(self, vals, parts):
        super().__init__(vals); self.parts = parts


def nightly(con, room, guests, nights):
    """Price for each night for the whole party, over as many rooms of this type as it needs."""
    if room and room in MAX_GUESTS and guests > MAX_GUESTS[room]:
        parts = [_one_room(con, room, g, nights) for g in split(room, guests)]
        return Nights([None if any(p[i] is None for p in parts) else sum(p[i] for p in parts) for i in range(len(nights))], parts)
    return _one_room(con, room, guests, nights)


def _one_room(con, room, guests, nights):
    """Price for each night of one room. The Royal Suite adds its connecting room at the Standard rate (single rate, double rate from 4 guests)."""
    if room == FAMILY:
        # the connecting room is always charged at the Standard rate for two (hotel, 25 Nov 2026)
        a = _nightly(con, 'royal-suite', min(guests, 2), nights); b = _nightly(con, 'standard', 2, nights)
        return [None if (x is None or y is None) else x + y for x, y in zip(a, b)]
    return _nightly(con, room, guests, nights)


def _nightly(con, room, guests, nights):
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
            # each room is discounted on its own, down to a multiple of 5: the guest always gets at least the full percent (hotel, 25 Nov 2026)
            rooms = [x[len(out)] for x in getattr(per, 'parts', None) or []] or [p]
            p = sum(int(round(x * (100 - pct))) // 500 * 5 for x in rooms)
        out.append(p)
    return out, best


def left(con, room, nights):
    """Rooms of this type still free on the busiest night, or None when the hotel has not set a room count."""
    inv = con.execute('SELECT count FROM inventory WHERE room=?', (room,)).fetchone()
    if inv is None:
        return None
    a, b = nights[0], nights[-1] + dt.timedelta(days=1)
    booked = con.execute("SELECT * FROM bookings WHERE status='confirmed' AND check_in < ? AND check_out > ?",
                         (b.isoformat(), a.isoformat())).fetchall()
    used = max(sum(held(units(x)).get(room, 0) for x in booked if x['check_in'] <= n.isoformat() < x['check_out']) for n in nights)
    return max(inv['count'] - used, 0)


def quote(con, a, b, guests, room=None):
    nights = [a + dt.timedelta(days=i) for i in range((b - a).days)]
    out = []
    for k, names in ROOMS.items():
        if room and k != room:
            continue
        need = rooms_needed(k, guests)
        base = nightly(con, k, guests, nights)
        per, pct = discounted(con, base, nights)
        total = None if any(x is None for x in per) else sum(per)
        base_total = None if any(x is None for x in base) else sum(base)
        one, _p1 = discounted(con, _one_room(con, k, min(MAX_GUESTS[k], 2), nights), nights)
        out.append({'id': k, 'name_en': names[0], 'name_ar': names[1], 'available': availability(con, a, b, k, need=need), 'rooms': need,
                    'cap': MAX_GUESTS[k], 'min': MIN_GUESTS.get(k, 1), 'fits': type_fits(k, guests), 'one_night': None if None in one else r5(sum(one) / len(nights)),
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
    phone = clean(str(d.get('phone') or '').translate(DIGITS), 24)
    email = clean(d.get('email'), 254)
    room = clean(d.get('room'), 40)
    msg = clean(d.get('message'), 600)
    g = d.get('guests', 2)
    guests = g if (isinstance(g, int) and not isinstance(g, bool)) else (int(g) if isinstance(g, str) and g.strip().isdigit() else 0)
    if len(name) < 2:
        errs.append('name')
    if not PHONE_RE.match(phone) or len(re.sub(r'\D', '', phone)) < 7:
        errs.append('phone')
    if email and not EMAIL_RE.match(email):
        errs.append('email')
    if room and room not in ROOMS:
        errs.append('room')
    if not 1 <= guests <= MAX_PARTY:
        errs.append('guests')
    lang = 'en' if d.get('lang') in ('en', 'tr') else 'ar'   # Turkish visitors get English letters and mails
    if room in ROOMS and not d.get('combo') and not type_fits(room, guests):
        errs.append('room')            # e.g. a Triple Room for one guest
    combo = None
    if d.get('combo'):
        combo = parse_combo(d.get('combo'), guests)
        if combo is None:
            errs.append('room')
        elif len(combo) == 1:
            room = next(iter(combo))
    return errs, dict(combo=combo, dates=dates, name=name, phone=phone, email=email or None, room=room or None, message=msg or None,
                      guests=guests, breakfast=1 if d.get('breakfast') in (True, 1, '1', 'on', 'true') else 0, lang=lang)


# ---------------------------------------------------------------- business requests (agencies, companies, weddings, groups)
KINDS = {'agency': 'وكالة سفر', 'company': 'شركة / مؤسسة', 'wedding': 'عرس أو مناسبة', 'group': 'مجموعة أو عائلة كبيرة',
         'long': 'إقامة طويلة', 'other': 'طلب آخر'}
INQ_STATUSES = ('new', 'contacted', 'won', 'lost')


def validate_inquiry(d):
    errs = []
    kind = clean(d.get('kind'), 20)
    name, company = clean(d.get('name'), 80), clean(d.get('company'), 100)
    phone, email = clean(d.get('phone'), 24), clean(d.get('email'), 254)
    msg = clean(d.get('message'), 1000)
    date = parse_day(d.get('date')) if d.get('date') else None
    def num(k, hi):
        try:
            v = int(d.get(k) or 0)
        except (TypeError, ValueError):
            return None
        return v if 0 <= v <= hi else None
    guests, rooms, nights = num('guests', 3000), num('rooms', 65), num('nights', 365)
    if kind not in KINDS:
        errs.append('kind')
    if len(name) < 2:
        errs.append('name')
    if not PHONE_RE.match(phone) or len(re.sub(r'\D', '', phone)) < 7:
        errs.append('phone')
    if email and not EMAIL_RE.match(email):
        errs.append('email')
    if d.get('date') and not date:
        errs.append('date')
    if guests is None:
        errs.append('guests')
    if rooms is None:
        errs.append('rooms')
    if nights is None:
        errs.append('nights')
    lang = d.get('lang') if d.get('lang') in ('en', 'tr') else 'ar'
    return errs, dict(kind=kind, name=name, company=company or None, phone=phone, email=email or None, message=msg or None,
                      date=date.isoformat() if date else None, guests=guests or None, rooms=rooms or None, nights=nights or None, lang=lang)


def inquiry_text(r):
    lines = [f"📩 طلب جديد من الموقع #{r['id']}: {KINDS.get(r['kind'], r['kind'])}", f"الاسم: {r['name']}"]
    if r.get('company'):
        lines.append(f"الجهة: {r['company']}")
    lines.append(f"الهاتف: {r['phone']}")
    if r.get('email'):
        lines.append(f"البريد: {r['email']}")
    if r.get('date'):
        lines.append(f"التاريخ: {r['date']}")
    for k, lab in (('guests', 'عدد الأشخاص'), ('rooms', 'عدد الغرف'), ('nights', 'عدد الليالي')):
        if r.get(k):
            lines.append(f"{lab}: {r[k]}")
    lines.append(f"اللغة: {r['lang']}")
    lines.append(f"التفاصيل: {r.get('message') or '-'}")
    hello = {'en': 'Hello, this is Royal Inn Tartus regarding your request on our website.',
             'tr': 'Hello, this is Royal Inn Tartus regarding your request on our website.'}.get(r['lang'], 'مرحباً، معكم فندق رويال إن طرطوس بخصوص طلبكم على موقعنا.')
    lines.append('\nالرد على واتساب:\n' + wa_link(r['phone'], hello))
    return '\n'.join(lines)


def notify_inquiry(r):
    send_staff(inquiry_text(r), f"طلب جديد من الموقع #{r['id']} - {KINDS.get(r['kind'], '')} - Royal Inn")


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
def act_url(ref):
    return 'https://royalinntartus.com/api/act/%s/%s' % (ref, letter_key('act:' + ref))


def wa_link(phone, text):
    import urllib.parse
    dg = re.sub(r'\D', '', phone or '')
    if dg.startswith('00'):
        dg = dg[2:]
    elif dg.startswith('0'):
        dg = '963' + dg[1:]
    return 'https://wa.me/%s?text=%s' % (dg, urllib.parse.quote(text)) if len(dg) >= 8 else None


def deposit_text(b, S):
    room = room_label(b, 1, '')
    if b.get('lang') == 'en':
        return ('Hello %s, thank you for your booking request %s at Royal Inn Tartus (%s to %s%s). To secure the booking please send a deposit by money transfer inside Syria. %s Your booking is confirmed once we receive it.'
                % (b['name'], b['ref'], b['check_in'], b['check_out'], (', total $%s' % b['total']) if b.get('total') else '', S.get('deposit_info', '')))
    return ('مرحباً %s، شكراً لطلب الحجز %s في فندق رويال إن طرطوس (من %s إلى %s%s%s). لتثبيت الحجز يُرجى إرسال دفعة مقدّمة. %s يتم تأكيد الحجز فور وصول الدفعة.'
            % (b['name'], b['ref'], _fd(b['check_in']), _fd(b['check_out']), (' · ' + room) if room else '', ('، المبلغ الإجمالي %s$' % b['total']) if b.get('total') else '', S.get('deposit_info', '')))


def notify_email(b):
    """New request -> the hotel's mailbox, with buttons to ask for the deposit, confirm or refuse without opening the panel."""
    to = os.environ.get('NOTIFY_TO')
    if not os.environ.get('SMTP_HOST') or not to:
        return
    try:
        with db() as con:
            S = settings(con)
        room = room_label(b, 1, 'أي غرفة')
        rows = [('رقم الطلب', b['ref']), ('الاسم', b['name']), ('الهاتف', '\u2066%s\u2069' % b['phone']), ('البريد', b.get('email')),
                ('الوصول', _fd(b['check_in'])), ('المغادرة', '%s (%s ليالٍ)' % (_fd(b['check_out']), b['nights'])), ('الضيوف', party_ar(b)),
                ('نوع الغرفة', room), ('الفطور', 'نعم' if b['breakfast'] else 'لا'), ('المبلغ الإجمالي', ('%s$' % b['total']) if b.get('total') else 'يُحدَّد من الفندق'),
                ('ملاحظة الضيف', b.get('message')), ('لغة الضيف', 'English' if b.get('lang') == 'en' else 'العربية')]
        act = act_url(b['ref']); dep = wa_link(b['phone'], deposit_text(b, S))
        btn = lambda href, label, bg: '<a href="%s" style="display:inline-block;background:%s;color:#fff;text-decoration:none;font-weight:bold;padding:12px 20px;border-radius:8px;margin:4px">%s</a>' % (href, bg, label)
        buttons = ('<p style="text-align:center;margin:18px 0 4px">' + (btn(dep, '١. اطلب الدفعة (واتساب)', '#1f9d55') if dep else '')
                   + btn(act, '٢. تأكيد أو رفض الحجز', '#B48A45') + '</p>'
                   '<p style="text-align:center;color:#64727b;font-size:13px;margin:0">بعد التأكيد أدخلوا الحجز في برنامج الفندق كالعادة.</p>')
        html = mail_html('ar', 'طلب حجز جديد %s' % b['ref'], 'طلب حجز جديد من الموقع', 'وصل طلب حجز من موقع الفندق. الطلب لا يحجز الغرفة قبل التأكيد.', rows, None, ()).replace('</table><p', '</table>' + buttons + '<p', 1)
        if buttons not in html:
            html = html.replace('</td></tr><tr><td style="padding:16px 26px 22px', buttons + '</td></tr><tr><td style="padding:16px 26px 22px', 1)
        text = '\n'.join(['%s: %s' % (k, v) for k, v in rows if v not in (None, '')] + ['', 'اطلب الدفعة (واتساب): ' + (dep or '-'), 'تأكيد أو رفض: ' + act])
        m = EmailMessage()
        m['Subject'] = 'طلب حجز جديد %s · %s · %s' % (b['ref'], b['name'], _fd(b['check_in']))
        from email.utils import formataddr
        m['From'] = formataddr(('Royal Inn - الحجوزات', os.environ.get('MAIL_FROM') or os.environ.get('SMTP_USER') or to)); m['To'] = to
        m.set_content(text); m.add_alternative(html, subtype='html')
        smtp_send(m)
    except Exception as e:  # a failed e-mail never loses the request
        print('notify failed:', type(e).__name__, flush=True)


def act_page(r, done=None, S=None):
    """The page behind the e-mail button: the request, and links to confirm or refuse it."""
    e = _esc
    head = doc_head('ar', 'rtl', 'طلب الحجز')
    if r is None:
        return head + '<main class="sheet">' + doc_band('الرابط غير صحيح') + '</main></body></html>'
    room = room_label(r, 1, 'أي غرفة')
    rows = [('رقم الطلب', '<b dir="ltr">%s</b>' % e(r['ref'])), ('الاسم', e(r['name'])), ('الهاتف', '<span dir="ltr">%s</span>' % e(r['phone'])),
            ('التواريخ', '<span dir="ltr">%s – %s</span> (%s ليالٍ)' % (_fd(r['check_in']), _fd(r['check_out']), r['nights'])), ('الضيوف', party_ar(r)), ('الغرفة', e(room)),
            ('المبلغ', ('%s$' % r['total']) if r['total'] else 'يُحدَّد من الفندق')]
    st = {'new': 'جديد، ينتظر الرد', 'confirmed': 'مؤكَّد', 'cancelled': 'ملغى'}[r['status']]
    tbl = '<section class="sec"><h2>تفاصيل الطلب <i>%s</i></h2><table class="lt">%s</table></section>' % (e(st), ''.join('<tr><th>%s</th><td>%s</td></tr>' % (k, v) for k, v in rows))
    base = '/api/act/%s/%s' % (r['ref'], letter_key('act:' + r['ref']))
    big = lambda href, label, cls: '<a class="bigbtn %s" href="%s"%s>%s</a>' % (cls, e(href), ' target="_blank" rel="noopener"' if href.startswith('https://wa.me') else '', e(label))
    if r['status'] == 'new':
        dep = wa_link(r['phone'], deposit_text(dict(r), S or DEFAULTS))
        acts = ((big(dep, 'اطلب الدفعة من الضيف (واتساب)', 'wa') if dep else '') + big(base + '?do=confirm', 'تأكيد الحجز (بعد استلام الدفعة)', 'ok')
                + big(base + '?do=cancel', 'رفض / إلغاء الطلب', 'no'))
        msg = ''
    elif r['status'] == 'confirmed':
        letter = 'https://royalinntartus.com/api/confirmation/%s/%s' % (r['ref'], letter_key(r['ref']))
        t = ('Hello %s, your booking %s at Royal Inn Tartus is confirmed. Your confirmation letter: %s' % (r['name'], r['ref'], letter)) if r['lang'] == 'en' else \
            ('مرحباً %s، تم تأكيد حجزكم %s في فندق رويال إن طرطوس. رسالة التأكيد: %s' % (r['name'], r['ref'], letter))
        wa = wa_link(r['phone'], t)
        acts = (big(wa, 'أرسل رسالة التأكيد للضيف (واتساب)', 'wa') if wa else '') + big(letter, 'رسالة التأكيد (طباعة / PDF)', 'line')
        msg = '<p class="ok-msg">%s لا تنسوا إدخال الحجز في برنامج الفندق.</p>' % ('تم تأكيد الحجز.' if done == 'confirm' else 'هذا الحجز مؤكَّد.')
    else:
        t = ('Hello %s, we are sorry, we cannot confirm booking %s at Royal Inn Tartus for these dates.' % (r['name'], r['ref'])) if r['lang'] == 'en' else \
            ('مرحباً %s، نعتذر، لا نستطيع تأكيد الحجز %s في فندق رويال إن طرطوس في هذه التواريخ.' % (r['name'], r['ref']))
        wa = wa_link(r['phone'], t)
        acts = big(wa, 'أخبر الضيف (واتساب)', 'wa') if wa else ''
        msg = '<p class="no-msg">%s</p>' % ('تم رفض الطلب، والغرفة تبقى متاحة.' if done == 'cancel' else 'هذا الطلب ملغى.')
    return (head + '<main class="sheet">' + doc_band('طلب حجز <i>Booking request</i>', '<p dir="ltr">%s</p>' % e(r['ref']))
            + '<div class="pad">' + msg + tbl + '<div class="acts">' + acts + '</div><p class="auto">يمكن أيضاً إدارة كل الطلبات من لوحة الحجوزات: royalinntartus.com/panel</p></div>'
            + doc_foot('الكورنيش البحري، طرطوس، سوريا') + '</main>\n</body>\n</html>\n')


def smtp_send(m):
    host = os.environ['SMTP_HOST']; port = int(os.environ.get('SMTP_PORT', '465'))
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        s = smtplib.SMTP(host, port, timeout=15); s.starttls()
    if os.environ.get('SMTP_USER'):
        s.login(os.environ['SMTP_USER'], os.environ.get('SMTP_PASS', ''))
    s.send_message(m); s.quit()


def mail_on():
    """E-mails to guests. Off unless GUEST_MAIL=1: the hotel wants guests to get mail only from its own address."""
    return bool(os.environ.get('SMTP_HOST') and (os.environ.get('MAIL_FROM') or os.environ.get('SMTP_USER')) and os.environ.get('GUEST_MAIL') == '1')


def send_mail(to, subject, text, html=None):
    """One e-mail from the hotel to a guest. True when the mail server accepted it."""
    if not mail_on() or not to:
        return False
    try:
        from email.utils import formataddr
        m = EmailMessage()
        m['Subject'] = subject
        m['From'] = formataddr(('Royal Inn Tartus', os.environ.get('MAIL_FROM') or os.environ['SMTP_USER']))
        m['To'] = to
        if os.environ.get('REPLY_TO'):
            m['Reply-To'] = os.environ['REPLY_TO']
        m.set_content(text)
        if html:
            m.add_alternative(html, subtype='html')
        smtp_send(m)
        return True
    except Exception as e:
        print('guest mail failed:', type(e).__name__, flush=True)
        return False


def mail_html(lang, title, hello, intro, rows, button=None, notes=()):
    """A simple branded e-mail (inline styles, because mail programs ignore style sheets)."""
    import html as _h
    e = lambda x: _h.escape(str(x), quote=True)
    d = 'ltr' if lang == 'en' else 'rtl'; al = 'left' if lang == 'en' else 'right'
    tr = ''.join('<tr><td style="padding:9px 14px;border-bottom:1px solid #ece6da;color:#64727b;width:42%%">%s</td><td style="padding:9px 14px;border-bottom:1px solid #ece6da;color:#17242c;font-weight:bold">%s</td></tr>' % (e(k), e(v)) for k, v in rows if v not in (None, ''))
    btn = ('<p style="margin:22px 0 6px;text-align:center"><a href="%s" style="background:#B48A45;color:#ffffff;text-decoration:none;font-weight:bold;padding:13px 28px;border-radius:8px;display:inline-block">%s</a></p>' % (e(button[1]), e(button[0]))) if button else ''
    nt = ''.join('<p style="margin:6px 0;color:#4a5a63;font-size:13px">%s</p>' % e(n) for n in notes if n)
    return ('<!DOCTYPE html><html lang="%s" dir="%s"><body style="margin:0;background:#F8F4EC;font-family:Tahoma,Arial,sans-serif;font-size:15px;line-height:1.7;color:#17242c">'
            '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="background:#F8F4EC"><tr><td align="center" style="padding:20px 10px">'
            '<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%%;background:#ffffff;border:1px solid #e4ddd0;border-top:5px solid #B48A45;text-align:%s" dir="%s">'
            '<tr><td style="background:#0F3D52;color:#ffffff;padding:20px 26px"><div style="font-size:13px;letter-spacing:3px;color:#e9d9b8">ROYAL INN · TARTUS</div><div style="font-size:23px;font-weight:bold;margin-top:4px">%s</div></td></tr>'
            '<tr><td style="padding:24px 26px 8px"><p style="margin:0 0 8px;font-weight:bold;color:#0F3D52">%s</p><p style="margin:0 0 14px">%s</p>'
            '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="border:1px solid #e4ddd0;border-bottom:0">%s</table>%s%s</td></tr>'
            '<tr><td style="padding:16px 26px 22px;color:#64727b;font-size:13px;border-top:1px solid #ece6da"><b style="color:#0F3D52">Royal Inn Hotel · فندق رويال إن</b><br>%s<br><span dir="ltr">%s · %s</span></td></tr>'
            '</table></td></tr></table></body></html>'
            % (lang, d, al, d, e(title), e(hello), e(intro), tr, btn, nt, e('Sea Corniche, Tartus, Syria' if lang == 'en' else 'الكورنيش البحري، طرطوس، سوريا'), HOTEL['phone'], HOTEL['site']))


def _mail_rows(b, S, en):
    room = room_label(b, 0 if en else 1, '') or None
    L = (('Booking number', 'Arrival', 'Departure', 'Nights', 'Guests', 'Room', 'Breakfast', 'Total', 'from', 'until', 'Included', 'Not included', 'tax included')
         if en else ('رقم الحجز', 'الوصول', 'المغادرة', 'الليالي', 'الضيوف', 'الغرفة', 'الفطور', 'المبلغ الإجمالي', 'من الساعة', 'حتى الساعة', 'مشمول', 'غير مشمول', 'شامل الضريبة'))
    agency_pays = b.get('agency_id') and b.get('pay_by') == 'agency'
    return [(L[0], b['ref']), (L[1], '%s (%s %s)' % (_fd(b['check_in']), L[8], S['checkin'])), (L[2], '%s (%s %s)' % (_fd(b['check_out']), L[9], S['checkout'])),
            (L[3], b['nights']), (L[4], b['guests']), (L[5], room), (L[6], L[10] if b['breakfast'] else L[11]),
            (L[7], ('$%s (%s)' % (b['total'], L[12])) if (b.get('total') and not agency_pays) else None)]


def mail_received(b, S=None):
    """Sent to the guest the moment a request arrives from the website: received, not yet confirmed."""
    S = S or DEFAULTS; en = b.get('lang') == 'en'
    if en:
        sub, title, hello = 'We received your booking request %s - Royal Inn Tartus' % b['ref'], 'Booking request received', 'Dear %s,' % b['name']
        intro = 'Thank you for choosing Royal Inn Tartus. We have received your request and the hotel will contact you shortly to confirm it. Your booking is not confirmed yet.'
        btn = ('Check your booking', 'https://royalinntartus.com/en/contact'); note = 'To check your booking you need the booking number and the last four digits of your phone number.'
    else:
        sub, title, hello = 'استلمنا طلب حجزكم %s - فندق رويال إن طرطوس' % b['ref'], 'تم استلام طلب الحجز', 'السيد/ة %s المحترم/ة،' % b['name']
        intro = 'شكراً لاختياركم فندق رويال إن طرطوس. استلمنا طلبكم وسيتواصل معكم الفندق قريباً لتأكيده. الحجز لم يُؤكَّد بعد.'
        btn = ('تحقّقوا من حجزكم', 'https://royalinntartus.com/ar/contact'); note = 'للتحقق من الحجز تحتاجون رقم الحجز وآخر أربعة أرقام من هاتفكم.'
    rows = _mail_rows(b, S, en)
    text = '\n'.join([hello, '', intro, ''] + ['%s: %s' % (k, v) for k, v in rows if v not in (None, '')] + ['', btn[1], '', 'Royal Inn Hotel · ' + HOTEL['phone']])
    return send_mail(b.get('email'), sub, text, mail_html('en' if en else 'ar', title, hello, intro, rows, btn, (note,)))


def mail_confirmation(b, S=None):
    """Sent to the guest when the hotel confirms the booking (or records one at the desk): the details and the confirmation letter."""
    S = S or DEFAULTS; en = b.get('lang') == 'en'
    link = 'https://royalinntartus.com/api/confirmation/%s/%s' % (b['ref'], letter_key(b['ref']))
    if en:
        sub, title, hello = 'Booking confirmed %s - Royal Inn Tartus' % b['ref'], 'Booking confirmed', 'Dear %s,' % b['name']
        intro = 'We are pleased to confirm your booking at Royal Inn Tartus and look forward to welcoming you.'
        btn = ('Open your confirmation letter', link); notes = ('The booking is secured with a deposit by money transfer inside Syria; the balance is paid at the hotel in cash.', 'Cancellation is free up to 48 hours before arrival.', late_text(S, 'en'))
    else:
        sub, title, hello = 'تأكيد الحجز %s - فندق رويال إن طرطوس' % b['ref'], 'تم تأكيد الحجز', 'السيد/ة %s المحترم/ة،' % b['name']
        intro = 'يسرّنا تأكيد حجزكم في فندق رويال إن طرطوس، ونتطلع إلى استقبالكم.'
        btn = ('افتحوا رسالة التأكيد', link); notes = ('يُثبَّت الحجز بدفعة مقدّمة بحوالة داخل سوريا، ويُدفع الباقي في الفندق نقداً.', 'الإلغاء مجاني حتى 48 ساعة قبل موعد الوصول.', late_text(S, 'ar'))
    rows = _mail_rows(b, S, en)
    text = '\n'.join([hello, '', intro, ''] + ['%s: %s' % (k, v) for k, v in rows if v not in (None, '')] + ['', btn[0] + ': ' + link, ''] + [n for n in notes if n] + ['', 'Royal Inn Hotel · ' + HOTEL['phone']])
    return send_mail(b.get('email'), sub, text, mail_html('en' if en else 'ar', title, hello, intro, rows, btn, notes))


def notify_telegram(b):
    """Instant message to the hotel's Telegram chat. Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."""
    token, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        return
    try:
        import urllib.request, urllib.parse
        room = room_label(b, 1, 'أي غرفة')
        text = '\n'.join([
            f"🔔 طلب حجز جديد {b['ref']}", f"الاسم: {b['name']}", f"الهاتف: {b['phone']}",
            f"الوصول: {b['check_in']}", f"المغادرة: {b['check_out']} ({b['nights']} ليالٍ)", f"الضيوف: {party_ar(b)}",
            f"الغرفة: {room}", f"الفطور: {'نعم' if b['breakfast'] else 'لا'}",
            f"المبلغ: ${b['total']}" if b.get('total') else 'المبلغ: عند الطلب', f"ملاحظة: {b['message'] or '-'}"])
        # one tap forwards the same request to the hotel's WhatsApp (automatic WhatsApp to Syrian numbers is not possible)
        fwd = text + '\n\nتأكيد أو رفض: ' + act_url(b['ref'])
        text += '\n\n➡️ أرسل إلى واتساب الفندق:\n' + wa_link(os.environ.get('HOTEL_WA', '963998730730'), fwd) + '\n\nتأكيد أو رفض:\n' + act_url(b['ref'])
        data = urllib.parse.urlencode({'chat_id': chat, 'text': text, 'disable_web_page_preview': 'true'}).encode()
        urllib.request.urlopen(urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', data=data), timeout=12).read()
    except Exception as e:  # never loses the request, never prints the token
        print('telegram notify failed:', type(e).__name__, flush=True)


def notify(b):
    notify_email(b)
    notify_telegram(b)
    if b.get('email'):
        with db() as con:
            S = settings(con)
        mail_received(b, S)


# ---------------------------------------------------------------- HTTP
# Hotel rules the manager can change in the panel. Defaults follow common hotel practice; the hotel confirms them.
DEFAULTS = {'checkin': '14:00', 'checkout': '12:00', 'remind_hours': '2', 'late_on': '1', 'late_grace': '60', 'late_half_until': '18:00',
            'late_half_pct': '50', 'late_full_pct': '100', 'noshow_on': '0', 'early_note': '1',
            'deposit_info': 'يتم تثبيت الحجز بدفعة مقدّمة عبر حوالة داخل سوريا من خلال شركة الهرم - طرطوس، باسم: محمد شمس الدين وليد العليوي، هاتف: 0934589657. بعد التحويل أرسلوا لنا صورة الإيصال.'}
TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


def settings(con):
    s = dict(DEFAULTS)
    s.update({r['k']: r['v'] for r in con.execute('SELECT k, v FROM settings') if r['k'] in DEFAULTS})
    return s


def hm(t):
    return int(t[:2]) * 60 + int(t[3:])


def night_rate(con, b):
    """Room rate of the booking's last night (used for late check-out fees)."""
    if b['room']:
        last = dt.date.fromisoformat(b['check_out']) - dt.timedelta(days=1)
        per = nightly(con, b['room'], b['guests'], [last])
        if per and per[0] is not None:
            return per[0]
    if b['total'] and b['nights']:
        return r5((b['total'] - (b['late_fee'] or 0)) / b['nights'])
    return None


def desk_state(con):
    """Who is due out soon, who is late and who did not arrive, by the hotel's clock and rules."""
    S = settings(con); n = dt.datetime.now(); ts = n.date().isoformat(); mins = n.hour * 60 + n.minute
    out, grace, half = hm(S['checkout']), int(S['late_grace']), hm(S['late_half_until'])
    rows = con.execute("SELECT * FROM bookings WHERE status='confirmed' AND departed IS NULL AND check_in <= ? AND check_out >= ?",
                       (ts, (n.date() - dt.timedelta(days=7)).isoformat())).fetchall()
    soon, late, noshow = [], [], []
    for b in rows:
        if b['check_out'] == ts and mins < out + grace:
            if mins >= out - int(float(S['remind_hours']) * 60):
                d = row(b); d['minutes_left'] = out - mins; soon.append(d)
        elif b['check_out'] <= ts and (b['arrived'] or b['check_out'] == ts):
            d = row(b); rate = night_rate(con, b)
            stage = 'full' if (b['check_out'] < ts or mins >= half) else 'half'
            pct = int(S['late_full_pct'] if stage == 'full' else S['late_half_pct'])
            d.update(stage=stage, late_minutes=(mins - out) if b['check_out'] == ts else None, rate=rate,
                     fee=None if rate is None else r5(rate * pct / 100.0), pct=pct)
            late.append(d)
        if not b['arrived'] and b['check_in'] < ts and b['check_out'] >= ts:
            noshow.append(row(b))
    return S, soon, late, noshow


def sync_inventory(con, room=None):
    """Once the hotel has entered its room numbers, room counts follow them (rooms out of service do not count).
    Connecting rooms merged with a suite make the 'suite with connecting room' count; unmerged ones count as Standard rooms."""
    us = con.execute('SELECT room, state, merge FROM units').fetchall()
    if not us:
        return
    live = [u for u in us if u['state'] != 'out']
    def n(f):
        return sum(1 for u in live if f(u))
    have = {u['room'] for u in us}
    counts = {k: n(lambda u, k=k: u['room'] == k) for k in UNIT_TYPES if k in have}
    if CONNECT in have:
        if 'standard' in have:    # only once Standard rooms are numbered too; otherwise the hotel's own count stays
            counts['standard'] += n(lambda u: u['room'] == CONNECT and not (u['merge'] if u['merge'] is not None else 1))
        counts[FAMILY] = n(lambda u: u['room'] == CONNECT and (u['merge'] if u['merge'] is not None else 1))
    for k, v in counts.items():
        con.execute('INSERT OR REPLACE INTO inventory(room,count) VALUES(?,?)', (k, v))


def unit_busy(con, no, a, b, skip=None):
    """A confirmed booking that already holds this room number on overlapping nights."""
    for r in con.execute("SELECT ref, name, room_no FROM bookings WHERE status='confirmed' AND departed IS NULL AND room_no IS NOT NULL AND check_in < ? AND check_out > ? AND ref != ?",
                         (b, a, skip or '')):
        if no in room_nos(r['room_no']):
            return r
    return None


def reminder_loop():
    """Once a day, when the first guests are within the reminder window before check-out, tell the hotel (Telegram, e-mail)
    and e-mail the guests who left an address. WhatsApp is sent by the staff with one tap from the panel."""
    while True:
        time.sleep(60)
        try:
            with db() as con:
                S, soon, late, _ns = desk_state(con)
                today = dt.date.today().isoformat()
                due = [b for b in soon if not b.get('reminded')]
                last = con.execute("SELECT v FROM settings WHERE k='_rem_day'").fetchone()
                if soon and (not last or last['v'] != today):
                    con.execute("INSERT OR REPLACE INTO settings(k,v) VALUES('_rem_day',?)", (today,))
                    lines = ['⏰ مغادرة اليوم حتى الساعة %s (%d):' % (S['checkout'], len(soon))]
                    lines += ['• %s%s · %s' % (b['name'], (' · غرفة ' + b['room_no']) if b['room_no'] else '', b['phone']) for b in soon]
                    lines += ['', 'https://royalinntartus.com/panel/']
                    send_staff('\n'.join(lines), 'Check-outs today - Royal Inn')
                if mail_on():
                    for b in due:
                        if b.get('email'):
                            mail_reminder(b, S)
                            con.execute('UPDATE bookings SET reminded=? WHERE ref=?', (now(), b['ref']))
        except Exception as e:
            print('reminder loop:', type(e).__name__, flush=True)


def send_staff(text, subject):
    token, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if token and chat:
        try:
            import urllib.request, urllib.parse
            data = urllib.parse.urlencode({'chat_id': chat, 'text': text, 'disable_web_page_preview': 'true'}).encode()
            urllib.request.urlopen(urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', data=data), timeout=12).read()
        except Exception as e:
            print('telegram notify failed:', type(e).__name__, flush=True)
    if os.environ.get('SMTP_HOST') and os.environ.get('NOTIFY_TO'):
        try:
            m = EmailMessage(); m['Subject'] = subject; m['From'] = os.environ.get('SMTP_USER') or os.environ['NOTIFY_TO']; m['To'] = os.environ['NOTIFY_TO']
            m.set_content(text); smtp_send(m)
        except Exception as e:
            print('mail notify failed:', type(e).__name__, flush=True)


def late_text(S, lang):
    if S['late_on'] != '1':
        return None
    free = hm(S['checkout']) + int(S['late_grace']); ft = '%02d:%02d' % (free // 60, free % 60)
    if lang == 'en':
        return 'Late check-out is subject to availability: after %s and until %s, %s%% of the nightly rate is charged; after %s, a full extra night (%s%%). Please tell reception in advance.' % (
            ft, S['late_half_until'], S['late_half_pct'], S['late_half_until'], S['late_full_pct'])
    return 'المغادرة المتأخرة حسب التوفر: بعد الساعة %s وحتى %s يُحتسب %s%% من سعر الليلة، وبعد الساعة %s تُحتسب ليلة إضافية كاملة (%s%%). يُرجى إبلاغ الاستقبال مسبقاً.' % (
        ft, S['late_half_until'], S['late_half_pct'], S['late_half_until'], S['late_full_pct'])


def mail_reminder(b, S):
    en = b.get('lang') == 'en'; lt = late_text(S, 'en' if en else 'ar')
    if en:
        sub, title, hello = 'Check-out today at %s - Royal Inn Tartus' % S['checkout'], 'Check-out reminder', 'Dear %s,' % b['name']
        intro = 'A kind reminder that check-out is today at %s. If you would like to stay longer, please contact reception: %s' % (S['checkout'], HOTEL['phone'])
    else:
        sub, title, hello = 'موعد المغادرة اليوم الساعة %s - فندق رويال إن طرطوس' % S['checkout'], 'تذكير بموعد المغادرة', 'السيد/ة %s المحترم/ة،' % b['name']
        intro = 'نذكّركم بأن موعد المغادرة اليوم الساعة %s. إذا رغبتم بتمديد الإقامة يُرجى التواصل مع الاستقبال: %s' % (S['checkout'], HOTEL['phone'])
    return send_mail(b.get('email'), sub, '\n'.join([hello, '', intro, lt or '', '', 'Royal Inn Hotel']), mail_html('en' if en else 'ar', title, hello, intro, [], None, (lt,)))


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
    return None if a is None else r5(total * (100 - a['commission']) / 100.0)


def agency_sums(con, aid):
    """The agency's account with the hotel, from confirmed bookings and recorded payments."""
    bks = con.execute("SELECT * FROM bookings WHERE agency_id=? AND status='confirmed' ORDER BY check_in", (aid,)).fetchall()
    pays = con.execute('SELECT * FROM agency_payments WHERE agency_id=? ORDER BY date, id', (aid,)).fetchall()
    sales = sum(b['total'] or 0 for b in bks)
    due = sum(b['net'] if b['net'] is not None else (b['total'] or 0) for b in bks if b['pay_by'] == 'agency')
    commission = sum((b['total'] or 0) - (b['net'] if b['net'] is not None else (b['total'] or 0)) for b in bks if b['pay_by'] != 'agency')
    paid = sum(x['amount'] for x in pays)
    return bks, pays, {'bookings': len(bks), 'sales': sales, 'due': due, 'paid': paid, 'balance': due - paid, 'commission_owed': commission}


def xlsx(sheets):
    """A real Excel workbook, written with the standard library only. sheets = [(name, [(header, width, kind)], rows)].
    kind: 's' text, 'n' number, 'm' money, 'd' date (YYYY-MM-DD), 't' date and time. Right-to-left, frozen header, filter."""
    import zipfile
    from xml.sax.saxutils import escape as x

    def col(i):
        r = ''
        i += 1
        while i:
            i, m = divmod(i - 1, 26); r = chr(65 + m) + r
        return r

    def cell(ref, v, kind, head=False):
        if head:
            return '<c r="%s" s="1" t="inlineStr"><is><t>%s</t></is></c>' % (ref, x(v))
        if v is None or v == '':
            return ''
        try:
            if kind in ('n', 'm'):
                return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, 3 if kind == 'm' else 0, float(v) if isinstance(v, float) else int(v))
            if kind == 'd':
                return '<c r="%s" s="2"><v>%d</v></c>' % (ref, (dt.date.fromisoformat(str(v)[:10]) - dt.date(1899, 12, 30)).days)
            if kind == 't':
                t = dt.datetime.fromisoformat(str(v)).astimezone().replace(tzinfo=None)
                return '<c r="%s" s="4"><v>%.5f</v></c>' % (ref, (t - dt.datetime(1899, 12, 30)).total_seconds() / 86400.0)
        except (TypeError, ValueError):
            pass
        return '<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, x(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(v))))

    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<numFmts count="3"><numFmt numFmtId="164" formatCode="dd/mm/yyyy"/><numFmt numFmtId="165" formatCode="&quot;$&quot;#,##0"/><numFmt numFmtId="166" formatCode="dd/mm/yyyy hh:mm"/></numFmts>'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
              '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF0F3D52"/></patternFill></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
              '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
              '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + ''.join('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1) for i in range(len(sheets))) + '</Types>')
        z.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + ''.join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (x(n), i + 1, i + 1) for i, (n, _c, _r) in enumerate(sheets)) + '</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + ''.join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1) for i in range(len(sheets)))
                   + '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>' % (len(sheets) + 1))
        z.writestr('xl/styles.xml', styles)
        for i, (_n, cols, rows) in enumerate(sheets):
            last = '%s%d' % (col(len(cols) - 1), len(rows) + 1)
            body = '<row r="1" ht="30" customHeight="1">%s</row>' % ''.join(cell('%s1' % col(c), h, 's', True) for c, (h, _w, _k) in enumerate(cols))
            for ri, r in enumerate(rows):
                body += '<row r="%d">%s</row>' % (ri + 2, ''.join(cell('%s%d' % (col(c), ri + 2), r[c], cols[c][2]) for c in range(len(cols))))
            z.writestr('xl/worksheets/sheet%d.xml' % (i + 1), '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       '<sheetViews><sheetView rightToLeft="1" workbookViewId="0"%s><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' % (' tabSelected="1"' if i == 0 else '')
                       + '<cols>%s</cols><sheetData>%s</sheetData><autoFilter ref="A1:%s"/></worksheet>' % (''.join('<col min="%d" max="%d" width="%d" customWidth="1"/>' % (c + 1, c + 1, w) for c, (_h, w, _k) in enumerate(cols)), body, last))
    return buf.getvalue()


def export_book(con):
    load_agencies(con)
    stt = {'new': 'جديد', 'confirmed': 'مؤكَّد', 'cancelled': 'ملغى'}
    cols = [('رقم الحجز', 13, 's'), ('الحالة', 10, 's'), ('المصدر', 14, 's'), ('الوكالة', 20, 's'), ('من يدفع', 10, 's'), ('اسم الضيف', 24, 's'), ('الهاتف', 18, 's'), ('البريد الإلكتروني', 28, 's'),
            ('الوصول', 12, 'd'), ('المغادرة', 12, 'd'), ('الليالي', 8, 'n'), ('الضيوف', 8, 'n'), ('نوع الغرفة', 22, 's'), ('رقم الغرفة', 10, 's'), ('الفطور', 8, 's'),
            ('المبلغ الإجمالي', 14, 'm'), ('رسم المغادرة المتأخرة', 14, 'm'), ('المدفوع', 11, 'm'), ('المتبقي', 11, 'm'), ('الصافي من الوكالة', 14, 'm'),
            ('سُجِّل الوصول', 17, 't'), ('سُجِّلت المغادرة', 17, 't'), ('عدم حضور', 9, 's'), ('عن طريق (شخص)', 18, 's'), ('رسالة الضيف', 36, 's'), ('ملاحظة داخلية', 36, 's'), ('تاريخ الطلب', 17, 't')]
    rows = []
    for r in con.execute('SELECT * FROM bookings ORDER BY check_in DESC, created DESC'):
        tot, paid = r['total'], r['paid']
        rows.append([r['ref'], stt.get(r['status'], r['status']), SOURCES.get(r['source'] or 'web', 'الموقع'), AGENCIES.get(r['agency_id'], '') if r['agency_id'] else '',
                     ('الوكالة' if r['pay_by'] == 'agency' else 'الضيف') if r['agency_id'] else '', r['name'], r['phone'], r['email'], r['check_in'], r['check_out'], r['nights'], r['guests'],
                     room_label(r, 1, 'غير محدد'), r['room_no'], 'نعم' if r['breakfast'] else 'لا', tot, r['late_fee'], paid,
                     None if tot is None or r['status'] == 'cancelled' else max(0, tot - (paid or 0)), r['net'] if r['pay_by'] == 'agency' else None,
                     r['arrived'], r['departed'], 'نعم' if r['no_show'] else '', r['referrer'], r['message'], r['staff_note'], r['created']])
    acols = [('الوكالة', 26, 's'), ('العمولة %', 10, 'n'), ('المسؤول', 20, 's'), ('الهاتف', 18, 's'), ('حجوزات مؤكَّدة', 12, 'n'), ('إجمالي المبيعات', 14, 'm'),
             ('المستحق على الوكالة', 16, 'm'), ('المدفوع', 12, 'm'), ('الرصيد المتبقي', 14, 'm'), ('عمولة مستحقة للوكالة', 16, 'm')]
    arows = []
    for a in con.execute('SELECT * FROM agencies ORDER BY name').fetchall():
        _b, _p, sm = agency_sums(con, a['id'])
        arows.append([a['name'], a['commission'], a['contact'], a['phone'], sm['bookings'], sm['sales'], sm['due'], sm['paid'], sm['balance'], sm['commission_owed']])
    pcols = [('التاريخ', 12, 'd'), ('الوكالة', 26, 's'), ('المبلغ', 12, 'm'), ('ملاحظة', 36, 's')]
    prows = [[x['date'], AGENCIES.get(x['agency_id'], ''), x['amount'], x['note']] for x in con.execute('SELECT * FROM agency_payments ORDER BY date DESC, id DESC')]
    ucols = [('رقم الغرفة', 11, 's'), ('نوع الغرفة', 24, 's'), ('الطابق', 9, 's'), ('الحالة اليوم', 22, 's'), ('الضيف', 24, 's'), ('حتى', 12, 'd')]
    ust = {'free': 'جاهزة', 'occupied': 'مشغولة', 'arriving': 'محجوزة، الضيف لم يصل', 'leaving': 'مغادرة اليوم', 'dirty': 'تحتاج تنظيف', 'out': 'خارج الخدمة'}
    urows = [[u['no'], u['room_name'], u['floor'], ust[u['state']], u['guest'], u['until']] for u in units_view(con, dt.date.today().isoformat())]
    return xlsx([('الحجوزات', cols, rows), ('الوكالات', acols, arows), ('دفعات الوكالات', pcols, prows), ('الغرف', ucols, urows)])


VIEWS = {'sea': ('Sea view', 'إطلالة على البحر'), 'side': ('Side sea view', 'إطلالة جانبية على البحر'),
         'city': ('City view', 'إطلالة على المدينة'), 'none': ('No view', 'بدون إطلالة')}


def _room_defaults():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rooms.json'), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


ROOM_DEFAULTS = _room_defaults()     # names, descriptions and photos the site build wrote next to this file


def room_info(con):
    """Room descriptions the hotel wrote in the panel, per type and language; empty ones fall back to the site's own text."""
    out = {}
    for r in con.execute('SELECT * FROM room_info'):
        v = {k: r[k] for k in ('ar', 'en', 'tr') if r[k]}
        if r['room'] in ROOMS and v:
            out[r['room']] = v
    return out


def units_view(con, ds, free_only=False):
    """The room rack for one day: every numbered room with who is in it, who arrives and who leaves."""
    out = []
    bks = con.execute("SELECT * FROM bookings WHERE status='confirmed' AND room_no IS NOT NULL AND check_in <= ? AND check_out >= ?", (ds, ds)).fetchall()
    for u in con.execute('SELECT * FROM units ORDER BY length(no), no'):
        mine = [b for b in bks if u['no'] in room_nos(b['room_no'])]
        stay = next((b for b in mine if b['check_in'] <= ds < b['check_out'] and not b['departed']), None)
        leave = next((b for b in mine if b['check_out'] == ds and not b['departed']), None)
        st = 'out' if u['state'] == 'out' else 'occupied' if (stay and stay['arrived']) else 'arriving' if stay else 'leaving' if leave else ('dirty' if u['state'] == 'dirty' else 'free')
        if free_only:
            if st in ('free', 'dirty'):
                out.append({'no': u['no'], 'room': u['room'], 'state': st, 'view': _g(u, 'view')})
            continue
        g = stay or leave
        out.append({'id': u['id'], 'no': u['no'], 'room': u['room'], 'room_name': NAMES.get(u['room'], ('', ''))[1], 'floor': u['floor'], 'state': st, 'clean': u['state'], 'note': u['note'], 'view': _g(u, 'view'),
                    'merge': (u['merge'] if u['merge'] is not None else 1) if u['room'] == CONNECT else None, 'pair': u['pair'] if u['room'] == CONNECT else None,
                    'leaving_today': bool(leave), 'guest': g['name'] if g else None, 'ref': g['ref'] if g else None,
                    'until': g['check_out'] if g else None, 'since': g['check_in'] if g else None})
    return out


def letter_key(ref):
    """The confirmation letter opens only with this key, so a booking number alone is not enough to read it."""
    return hmac.new(secret(), ('letter:' + ref).encode(), hashlib.sha256).hexdigest()[:20]


def row(r):
    d = dict(r)
    d['breakfast'] = bool(d['breakfast'])
    d['room_name'] = room_label(r, 1, 'أي غرفة')
    d['slots'] = slots(r); d['room_nos'] = room_nos(d.get('room_no'))
    d['letter'] = '/api/confirmation/%s/%s' % (d['ref'], letter_key(d['ref']))
    d['agency_name'] = AGENCIES.get(d.get('agency_id')) if d.get('agency_id') else None
    d['source_name'] = SOURCES.get(d.get('source') or 'web', 'الموقع')
    return d


HOTEL = {'phone': '+963 998 730 730', 'wa': '963998730730', 'email': 'res@royalinn.org', 'site': 'royalinntartus.com'}
# Labels are printed in both languages: the guest's language first, the other one small beside it.
LBL = {'ref': ('Confirmation no.', 'رقم التأكيد'), 'status': ('Status', 'الحالة'), 'issued': ('Date of issue', 'تاريخ الإصدار'), 'to': ('Guest name', 'اسم الضيف'),
       'phone': ('Phone', 'الهاتف'), 'email': ('E-mail', 'البريد الإلكتروني'), 'company': ('Company / agency', 'الشركة / الوكالة'), 'via': ('Booked through', 'الحجز عن طريق'),
       'cin': ('Arrival', 'الوصول'), 'cout': ('Departure', 'المغادرة'), 'nights': ('Nights', 'الليالي'), 'guests': ('Guests', 'الضيوف'),
       'room': ('Room type', 'نوع الغرفة'), 'room_no': ('Room number', 'رقم الغرفة'), 'bf': ('Breakfast', 'الفطور'), 'rate': ('Average rate per night', 'متوسط سعر الليلة'),
       'late': ('Late check-out fee', 'رسم المغادرة المتأخرة'), 'total': ('Total amount', 'المبلغ الإجمالي'), 'paid': ('Paid', 'المدفوع'), 'due': ('Balance due at the hotel', 'المتبقي للدفع في الفندق'), 'pay': ('Payment', 'الدفع')}
SEC = {'res': ('Guest details', 'بيانات الضيف'), 'stay': ('Stay', 'الإقامة'), 'rate': ('Rate and payment', 'السعر والدفع'), 'pol': ('Hotel policies', 'سياسات الفندق')}
VIA = {'web': ('Hotel website', 'موقع الفندق'), 'desk': ('Hotel reception', 'استقبال الفندق'), 'phone': ('Telephone', 'الهاتف'), 'agency': ('Travel agency', 'وكالة سفر'), 'referral': ('Referral', 'توصية')}
LETTER = {
    'ar': dict(dir='rtl', title='تأكيد حجز', hello='السيد/ة {name} المحترم/ة،', intro='يسرّنا تأكيد حجزكم في فندق رويال إن طرطوس، ونتطلع إلى استقبالكم.',
               yes='مشمول (10$ للشخص في اليوم)', no='غير مشمول (متوفر بـ 10$ للشخص)', ask='يُحدَّد من الفندق', tax='شامل الضريبة', any='تُحدَّد من الفندق',
               from14='من الساعة 14:00', until12='حتى الساعة 12:00', confirmed='مؤكَّد', by_agency='يُسدَّد عن طريق الوكالة: {a}', at_hotel='دفعة مقدّمة بحوالة داخل سوريا لتثبيت الحجز، والباقي في الفندق نقداً',
               t=['يُثبَّت الحجز بدفعة مقدّمة بحوالة داخل سوريا، ويُدفع الباقي في الفندق نقداً.', 'الإلغاء مجاني حتى 48 ساعة قبل موعد الوصول.',
                  'الاستقبال مفتوح على مدار 24 ساعة، وموقف السيارات مجاني للضيوف.', 'يُرجى إحضار هذا التأكيد أو رقم الحجز عند الوصول.'],
               bye='ننتظركم بكل سرور.', sign='إدارة الحجوزات، فندق رويال إن', addr='الكورنيش البحري، طرطوس، سوريا', print='طباعة / حفظ PDF',
               pending_h='الحجز لم يُؤكَّد بعد', pending_p='سيصلكم التأكيد من الفندق قريباً.', cancelled_h='هذا الحجز ملغى', none='الرابط غير صحيح.'),
    'en': dict(dir='ltr', title='Booking Confirmation', hello='Dear {name},', intro='We are pleased to confirm your booking at Royal Inn Tartus and look forward to welcoming you.',
               yes='Included ($10 per person per day)', no='Not included (available at $10 per person)', ask='To be confirmed by the hotel', tax='tax included', any='To be assigned by the hotel',
               from14='from 14:00', until12='until 12:00', confirmed='Confirmed', by_agency='Settled through the agency: {a}', at_hotel='Deposit by money transfer inside Syria to secure the booking; the balance at the hotel in cash',
               t=['The booking is secured with a deposit by money transfer inside Syria; the balance is paid at the hotel in cash.', 'Cancellation is free up to 48 hours before arrival.',
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
            '<link rel="stylesheet" href="/assets/css/fonts-g.css?v=1">\n<link rel="stylesheet" href="/panel/letter.css?v=5">\n<script src="/panel/letter.js?v=1" defer></script>\n</head>\n<body>\n'
            % (lang, direction, _esc(title)))


def doc_band(title, sub=''):
    """Photo header used by every printed document (the hotel's own sunset view)."""
    return ('<header class="band"><img class="band-logo" src="/assets/img/logo-light.svg" alt="Royal Inn" width="150" height="60">'
            '<div class="band-t"><h1>%s</h1>%s</div></header>' % (title, sub))


def doc_foot(addr):
    return ('<footer class="lf"><b>Royal Inn Hotel · فندق رويال إن</b><span>%s</span><span dir="ltr">%s · %s · %s</span></footer>'
            % (_esc(addr), HOTEL['phone'], HOTEL['email'], HOTEL['site']))


def letter_html(r, S=None):
    """Printable confirmation letter. Styles and the print button come from static files (the site allows no inline code)."""
    e = _esc
    lang = 'en' if (r and r['lang'] == 'en') else 'ar'
    S = S or DEFAULTS
    L = dict(LETTER[lang]); i, j = (0, 1) if lang == 'en' else (1, 0)
    L['from14'] = L['from14'].replace('14:00', S['checkin']); L['until12'] = L['until12'].replace('12:00', S['checkout'])
    L['t'] = list(L['t']) + [x for x in (late_text(S, lang),
        (('A confirmed guest who does not arrive and does not cancel is charged the first night.' if lang == 'en' else 'في حال عدم الحضور دون إلغاء تُحتسب الليلة الأولى.') if S['noshow_on'] == '1' else None),
        (('Early check-in before %s is subject to availability.' % S['checkin'] if lang == 'en' else 'الدخول المبكر قبل الساعة %s حسب التوفر.' % S['checkin']) if S['early_note'] == '1' else None)) if x]
    head = doc_head(lang, L['dir'], L['title'])
    if r is None or r['status'] != 'confirmed':
        msg = L['none'] if r is None else L['cancelled_h'] if r['status'] == 'cancelled' else L['pending_h']
        sub = '' if r is None or r['status'] == 'cancelled' else '<p>%s</p>' % e(L['pending_p'])
        return head + '<main class="sheet">' + doc_band(e(msg)) + '<div class="pad">%s</div>' % sub + doc_foot(L['addr']) + '</main>\n</body>\n</html>\n'
    keys = r.keys()
    g = lambda k: r[k] if k in keys else None
    O = LETTER['en' if lang == 'ar' else 'ar']
    lab = lambda k: '%s <i dir="auto">%s</i>' % (e(LBL[k][i]), e(LBL[k][j]))
    sec = lambda k, rows: ('<section class="sec"><h2>%s <i>%s</i></h2><table class="lt">%s</table></section>'
                           % (e(SEC[k][i]), e(SEC[k][j]), ''.join('<tr><th>%s</th><td>%s</td></tr>' % (lab(a), v) for a, v in rows)))
    agency = AGENCIES.get(g('agency_id')) if g('agency_id') else None
    room = room_label(r, 0 if lang == 'en' else 1, L['any'])
    dn = (['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'] if lang == 'en'
          else ['الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت', 'الأحد'])
    wd = lambda ds: dn[dt.date.fromisoformat(ds).weekday()]
    tile = lambda k, big, small, cls='': '<div class="tile %s"><span class="tl">%s</span><b dir="auto">%s</b><span class="ts">%s</span></div>' % (cls, lab(k), big, small)
    # what was booked: photo and description of each room type (the hotel's own text from the panel, else the site's)
    info = (S or {}).get('_rinfo') or {}; lk = 'en' if lang == 'en' else 'ar'
    kinds = list(units(r).items()) or ([(r['room'], _g(r, 'rooms') or 1)] if _g(r, 'room') in ROOMS else [])
    cards = ''
    for k, n in kinds:
        dfl = ROOM_DEFAULTS.get(k, {}); txt = (info.get(k) or {}).get(lk) or (dfl.get(lk) or {}).get('desc') or ''
        im = (dfl.get('img') or [None])[0]
        cards += ('<div class="lroom">%s<div><b>%s</b><p>%s</p></div></div>'
                  % (('<img src="/assets/img/%s-360.jpg" alt="" width="120" height="90">' % e(im)) if im else '',
                     e(((('%d × ' % n) if n > 1 else '') + NAMES.get(k, (k, k))[0 if lk == 'en' else 1])), e(txt)))
    vl = ''.join('<p class="lview">%s %s: %s</p>' % (e(LBL['room_no'][i]), e(x), e(VIEWS[v][0 if lk == 'en' else 1])) for x, v in ((S or {}).get('_views') or []) if v in VIEWS)
    rooms_box = ('<div class="lrooms">%s%s</div>' % (cards, vl)) if cards else ''
    strip = ('<div class="strip">' + tile('cin', _fd(r['check_in']), '%s · %s' % (e(wd(r['check_in'])), e(L['from14'])))
             + tile('cout', _fd(r['check_out']), '%s · %s' % (e(wd(r['check_out'])), e(L['until12'])))
             + tile('nights', r['nights'], '%s: %s' % (e(LBL['guests'][i]), r['guests']), 'num') + tile('room', e(room), ('%s: %s' % (e(LBL['room_no'][i]), e(g('room_no')))) if g('room_no') else e(L['yes'] if r['breakfast'] else L['no']), 'wide') + '</div>')
    res = [('to', '<b>%s</b>' % e(r['name']))]
    if r['phone'] and r['phone'] != '-':
        res.append(('phone', '<span dir="ltr">%s</span>' % e(r['phone'])))
    if g('email'):
        res.append(('email', '<span dir="ltr">%s</span>' % e(g('email'))))
    if agency:
        res.append(('company', e(agency)))
    src = g('source') or 'web'
    res.append(('via', e(VIA.get(src, VIA['web'])[i]) + ((' · ' + e(g('referrer'))) if (src == 'referral' and g('referrer')) else '')))
    res += [('guests', r['guests']), ('bf', e(L['yes'] if r['breakfast'] else L['no']))]
    total_box = ''
    if agency and g('pay_by') == 'agency':      # a voucher: the guest pays the agency, so no amounts are printed
        rate = [('pay', e(L['by_agency'].format(a=agency)))]
    elif r['total']:
        paid = g('paid') or 0; lf = g('late_fee') or 0
        rate = [('rate', '$%d' % r5((r['total'] - lf) / max(1, r['nights']))), ('nights', '%d' % r['nights'])]
        if lf:
            rate.append(('late', '$%d' % lf))
        if paid:
            rate.append(('paid', '$%d' % paid))
        rate.append(('pay', e(L['at_hotel'])))
        due = max(0, r['total'] - paid)
        total_box = ('<div class="total"><div><span>%s</span><b>$%d</b><small>%s</small></div>%s</div>'
                     % (lab('total'), r['total'], e(L['tax']), ('<div class="due"><span>%s</span><b>$%d</b></div>' % (lab('due'), due)) if paid else ''))
    else:
        rate = [('total', e(L['ask'])), ('pay', e(L['at_hotel']))]
    pol = '<section class="sec pols"><h2>%s <i>%s</i></h2><ul class="pol">%s</ul></section>' % (e(SEC['pol'][i]), e(SEC['pol'][j]), ''.join('<li>%s</li>' % e(x) for x in L['t']))
    lh = ('<header class="lh"><img src="/assets/img/logo-stacked.svg" alt="Royal Inn" width="150" height="109">'
          '<div class="lh-c"><b>Royal Inn Hotel · فندق رويال إن</b><span>%s</span><span dir="ltr">%s · %s</span><span dir="ltr">%s</span></div></header>'
          % (e(L['addr']), HOTEL['phone'], HOTEL['email'], HOTEL['site']))
    hero = ('<div class="hero"><div class="hero-t"><h1>%s</h1><p>%s</p></div><div class="refbox"><span>%s</span><b dir="ltr">%s</b><em class="ok">%s</em></div></div>'
            % (e(L['title']), e(O['title']), lab('ref'), e(r['ref']), e(L['confirmed'])))
    signed = ('<div class="signs"><div><p class="bye">%s</p><p class="sign">%s</p><p class="muted" dir="ltr">%s · %s</p></div>'
              '<div class="issue"><span>%s</span><b dir="ltr">%s</b></div></div><p class="auto">%s</p>'
              % (e(L['bye']), e(L['sign']), HOTEL['phone'], HOTEL['email'], lab('issued'), _fd(dt.date.today().isoformat()),
                 e('This confirmation was issued electronically by the hotel reservation system and is valid without a signature.' if lang == 'en'
                   else 'صدر هذا التأكيد إلكترونياً من نظام حجوزات الفندق، وهو صالح بدون توقيع.')))
    return (head + '<div class="tools"><button type="button" data-print>%s</button></div><main class="sheet letter">' % e(L['print']) + lh + hero
            + '<div class="pad"><p class="hello">%s</p><p>%s</p>' % (e(L['hello'].format(name=r['name'])), e(L['intro']))
            + strip + rooms_box + '<div class="two">' + sec('res', res) + '<div>' + sec('rate', rate) + total_box + '</div></div>' + pol + signed + '</div>'
            + '<footer class="lf2"><span>%s</span><span dir="ltr">%s</span></footer>' % (e(L['addr']), HOTEL['site']) + '</main>\n</body>\n</html>\n')


def pricelist_html(con, agency=None):
    """Printable rate sheet in Arabic and English. With an agency it also prints that agency's net rates."""
    e = _esc
    today = dt.date.today().isoformat()
    com = agency['commission'] if agency else 0
    money = lambda v: 'عند الطلب<i class="blk">on request</i>' if v is None else '$%d' % v
    net = lambda v: '' if v is None else '$%d' % r5(v * (100 - com) / 100.0)
    prices = {x['room']: x for x in con.execute('SELECT * FROM prices')}
    th = '<tr><th>نوع الغرفة <i>Room type</i></th><th>شخص واحد <i>1 guest</i></th><th>شخصان أو أكثر <i>2+ guests</i></th>%s</tr>' % ('<th>الصافي للوكالة <i>Net</i></th>' if agency else '')
    body = ''
    for k, (en, ar) in ROOMS.items():
        pr = prices.get(k); p1 = pr['price'] if pr else None; p2 = pr['price2'] if pr else None
        if k == FAMILY:
            pr = prices.get('royal-suite'); p1 = pr['price'] if pr else None
            st = prices.get('standard')
            s2 = (st['price2'] or st['price']) if st else None; s1 = s2    # connecting room at the Standard rate for two
            p1 = None if (p1 is None or s1 is None) else p1 + s1
            p2 = None if (p1 is None or s2 is None) else (pr['price'] + s2)
        nt = ' / '.join(x for x in (net(p1), net(p2) if p2 else '') if x)
        body += '<tr><td><b>%s</b> <i>%s</i></td><td>%s</td><td>%s</td>%s</tr>' % (e(ar), e(en), money(p1), money(p2) if p2 else ('—' if p1 is not None else money(None)), ('<td dir="ltr" class="num">%s</td>' % (nt or '—')) if agency else '')
    seasons = con.execute('SELECT * FROM rates WHERE date_to >= ? ORDER BY date_from, id', (today,)).fetchall()
    srows = ''
    for x in seasons:
        nm = NAMES[x['room']] if x['room'] in NAMES else ('All rooms', 'كل الغرف')
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
                    load_agencies(con); S = dict(settings(con)); S['_rinfo'] = room_info(con)
                    if r and r['room_no']:
                        S['_views'] = [(x, (con.execute('SELECT view FROM units WHERE no=?', (x,)).fetchone() or {'view': None})['view']) for x in room_nos(r['room_no'])]
            return self.send(200 if r else 404, raw=letter_html(r, S if r else None).encode('utf-8'), ctype='text/html; charset=utf-8', extra={'X-Robots-Tag': 'noindex, nofollow', 'Referrer-Policy': 'no-referrer'})
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
        m = re.fullmatch(r'/api/act/(RI-[A-Z0-9]{6})/([0-9a-f]{20})', p)
        if m:
            if not hmac.compare_digest(letter_key('act:' + m.group(1)), m.group(2)):
                return self.send(404, raw=act_page(None).encode('utf-8'), ctype='text/html; charset=utf-8')
            do = q.get('do'); done = None
            with LOCK, db() as con:
                r = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone(); S = settings(con)
                if r and r['status'] == 'new' and do in ('confirm', 'cancel') and not (do == 'confirm' and not booking_fits(con, r)):
                    st = 'confirmed' if do == 'confirm' else 'cancelled'
                    con.execute('UPDATE bookings SET status=?, staff_note=?, updated=? WHERE ref=?',
                                (st, ((r['staff_note'] or '') + ' · %s من البريد' % ('أُكِّد' if st == 'confirmed' else 'رُفض')).strip(' ·'), now(), r['ref']))
                    done = do
                    r = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
                    if st == 'confirmed' and r['email'] and mail_on():
                        threading.Thread(target=mail_confirmation, args=(dict(r), S), daemon=True).start()
                load_agencies(con)
            return self.send(200 if r else 404, raw=act_page(r, done, S).encode('utf-8'), ctype='text/html; charset=utf-8', extra={'X-Robots-Tag': 'noindex, nofollow', 'Cache-Control': 'no-store'})
        if p == '/api/offers':
            with db() as con:
                rows = con.execute('SELECT * FROM offers WHERE date_to >= ? ORDER BY percent DESC, date_from', (dt.date.today().isoformat(),)).fetchall()
            return self.send(200, {'ok': True, 'items': [{'percent': r['percent'], 'title_ar': r['title_ar'], 'title_en': r['title_en'],
                                                         'from': r['date_from'], 'to': r['date_to'], 'min_nights': r['min_nights']} for r in rows]})
        if p == '/api/room-info':
            with db() as con:
                return self.send(200, {'ok': True, 'rooms': room_info(con)}, extra={'Cache-Control': 'public, max-age=120'})
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
                guests = int(q.get('guests') or 2)
                if not 1 <= guests <= MAX_PARTY:
                    return self.send(400, {'ok': False, 'error': 'guests', 'max': MAX_PARTY})
            except ValueError:
                guests = 2
            if err or (room and room not in ROOMS):
                return self.send(400, {'ok': False, 'error': 'dates'})
            if too_many(room, guests):
                return self.send(200, {'ok': True, 'nights': (dates[1] - dates[0]).days, 'open': False, 'rooms': [], 'too_many': True,
                                       'max': MAX_GUESTS.get(room, MAX_ANY) if room else MAX_ANY, 'breakfast': BREAKFAST})
            with db() as con:
                rooms = quote(con, dates[0], dates[1], guests, room)
                options = [] if room else suggest(con, dates[0], dates[1], guests)
                pick = None
                if q.get('combo'):
                    c = parse_combo(q.get('combo'), guests)
                    if c:
                        ns = [dates[0] + dt.timedelta(days=i) for i in range((dates[1] - dates[0]).days)]
                        base = combo_nightly(con, c, guests, ns); per, pct = discounted(con, base, ns)
                        tot = None if None in per else sum(per)
                        pick = {'rooms': c, 'total': tot, 'per_night': None if tot is None else r5(tot / len(ns)),
                                'available': combo_free(con, dates[0], dates[1], c), 'count': sum(c.values())}
                    else:
                        pick = {'invalid': True}
                any_open = bool(options) if not room else availability(con, dates[0], dates[1], None)
            return self.send(200, {'ok': True, 'nights': (dates[1] - dates[0]).days, 'open': any_open, 'rooms': rooms, 'options': options, 'pick': pick,
                                   'free': free_units_safe(dates[0], dates[1]), 'breakfast': BREAKFAST})
        if p == '/api/booking/status':
            # a guest checks their own request: booking number + last four digits of the phone they gave
            if limited('st:' + self.ip(), 12, 600):
                return self.send(429, {'ok': False, 'error': 'busy'})
            ref = (q.get('ref') or '').strip().upper()
            last4 = re.sub(r'\D', '', (q.get('phone') or '').translate(DIGITS))[-4:]
            if not re.fullmatch(r'RI-[A-Z0-9]{6}', ref) or len(last4) != 4:
                return self.send(400, {'ok': False, 'error': 'fields'})
            with db() as con:
                r = con.execute('SELECT * FROM bookings WHERE ref=?', (ref,)).fetchone()
            if not r or re.sub(r'\D', '', r['phone'])[-4:] != last4:
                return self.send(404, {'ok': False, 'error': 'none'})
            def cl(c):
                try:
                    return [{'room': k, 'n': int(v)} for k, v in (json.loads(c) or {}).items() if int(v) > 0] if c else None
                except (ValueError, TypeError, AttributeError):
                    return None
            wr, wc = _g(r, 'req_room'), cl(_g(r, 'req_combo'))
            changed = bool(wr or wc) and (wr != (r['room'] or '-') or wc != cl(_g(r, 'combo')))
            return self.send(200, {'ok': True, 'ref': r['ref'], 'status': r['status'], 'in': r['check_in'], 'out': r['check_out'],
                                   'nights': r['nights'], 'guests': r['guests'], 'room': r['room'], 'combo': cl(_g(r, 'combo')), 'rooms': _g(r, 'rooms'),
                                   'total': r['total'], 'breakfast': bool(r['breakfast']), 'changed': changed,
                                   'was_room': (wr if wr and wr != '-' else None) if changed else None, 'was_combo': wc if changed else None,
                                   'letter': ('/api/confirmation/%s/%s' % (r['ref'], letter_key(r['ref']))) if r['status'] == 'confirmed' else None})
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
                if p == '/api/admin/inquiries':
                    st = q.get('status', 'open')
                    if st == 'open':
                        rows = con.execute("SELECT * FROM inquiries WHERE status IN ('new','contacted') ORDER BY id DESC LIMIT 500").fetchall()
                    elif st in INQ_STATUSES:
                        rows = con.execute('SELECT * FROM inquiries WHERE status=? ORDER BY id DESC LIMIT 500', (st,)).fetchall()
                    else:
                        rows = con.execute('SELECT * FROM inquiries ORDER BY id DESC LIMIT 500').fetchall()
                    counts = {x: con.execute('SELECT COUNT(*) FROM inquiries WHERE status=?', (x,)).fetchone()[0] for x in INQ_STATUSES}
                    items = [dict(r) for r in rows]
                    for it in items:
                        it['kind_label'] = KINDS.get(it['kind'], it['kind'])
                    return self.send(200, {'ok': True, 'items': items, 'counts': counts, 'kinds': KINDS})
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
                        return sum(held(units(x)).get(k, 0) for x in conf if x['check_in'] <= ds < x['check_out'])
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
                        week.append({'date': ds, 'booked': sum(max(1, sum(held(units(x)).values())) for x in conf if x['check_in'] <= ds < x['check_out']),
                                     'new': sum(1 for x in live if x['status'] == 'new' and x['check_in'] <= ds < x['check_out']),
                                     'closed': any(c['room'] is None and c['date_from'] <= ds <= c['date_to'] for c in cls)})
                    counts = {s: con.execute('SELECT COUNT(*) FROM bookings WHERE status=?', (s,)).fetchone()[0] for s in STATUSES}
                    newest = con.execute("SELECT * FROM bookings WHERE status='new' ORDER BY created DESC LIMIT 5").fetchall()
                    return self.send(200, {
                        'ok': True, 'today': ts, 'counts': counts, 'total_rooms': total, 'inventory_set': len(known),
                        'booked_tonight': sum(max(1, sum(held(units(x)).values())) for x in conf if x['check_in'] <= ts < x['check_out']),
                        'in_house': sum(1 for x in conf if x['arrived'] and not x['departed']),
                        'unassigned_tonight': sum((_g(x, 'rooms') or 1) for x in conf if not units(x) and x['check_in'] <= ts < x['check_out']),
                        'arrivals': [row(x) for x in conf if x['check_in'] == ts],
                        'departures': [row(x) for x in conf if x['check_out'] == ts],
                        'tomorrow': [row(x) for x in conf if x['check_in'] == (today + dt.timedelta(days=1)).isoformat()],
                        'newest': [row(x) for x in newest], 'rooms': rooms, 'week': week,
                        **(lambda t: {'rules': t[0], 'due_soon': t[1], 'late': t[2], 'no_show': t[3], 'clock': dt.datetime.now().strftime('%H:%M')})(desk_state(con)),
                        'units_free': units_view(con, ts, free_only=True)})
                if p == '/api/admin/settings':
                    return self.send(200, {'ok': True, 'settings': settings(con), 'clock': dt.datetime.now().strftime('%H:%M'), 'mail': mail_on()})
                if p == '/api/admin/units':
                    d0 = parse_day(q.get('date')) or dt.date.today()
                    return self.send(200, {'ok': True, 'date': d0.isoformat(), 'units': units_view(con, d0.isoformat()),
                                           'rooms': [{'id': k, 'name': ROOMS[k][1]} for k in UNIT_TYPES] + [{'id': CONNECT, 'name': NAMES[CONNECT][1]}]})
                if p == '/api/admin/units/free':
                    a, b = parse_day(q.get('in')), parse_day(q.get('out'))
                    if not a or not b or b <= a:
                        return self.send(400, {'ok': False})
                    us = con.execute("SELECT * FROM units WHERE state!='out' ORDER BY length(no), no").fetchall()
                    free = [{'no': u['no'], 'room': u['room'], 'state': u['state'], 'view': _g(u, 'view'), 'match': (not q.get('room')) or u['room'] == q.get('room')}
                            for u in us if not unit_busy(con, u['no'], a.isoformat(), b.isoformat(), q.get('ref'))]
                    return self.send(200, {'ok': True, 'defined': len(us) > 0, 'free': free})
                if p == '/api/admin/closures':
                    rows = con.execute('SELECT * FROM closures WHERE date_to >= ? ORDER BY date_from', (dt.date.today().isoformat(),)).fetchall()
                    return self.send(200, {'ok': True, 'items': [dict(r) for r in rows]})
                if p == '/api/admin/inventory':
                    inv = {r['room']: r['count'] for r in con.execute('SELECT * FROM inventory')}
                    return self.send(200, {'ok': True, 'rooms': [{'id': k, 'name': v[1], 'count': inv.get(k), 'max': MAX_GUESTS.get(k)} for k, v in ROOMS.items()]})
                if p == '/api/admin/room-info':
                    info = room_info(con)
                    return self.send(200, {'ok': True, 'views': {k: v[1] for k, v in VIEWS.items()},
                                           'rooms': [{'id': k, 'name': v[1], **info.get(k, {})} for k, v in ROOMS.items()]})
                if p == '/api/admin/prices':
                    rows = {r['room']: r for r in con.execute('SELECT * FROM prices')}
                    return self.send(200, {'ok': True, 'rooms': [{'id': k, 'name': v[1], 'price': rows[k]['price'] if k in rows else None,
                                                                   'price2': rows[k]['price2'] if k in rows else None} for k, v in ROOMS.items() if k != FAMILY]})
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
                    bks = con.execute("SELECT room, rooms, combo, status, check_in, check_out FROM bookings WHERE status!='cancelled' AND check_in < ? AND check_out > ?",
                                      (end.isoformat(), start.isoformat())).fetchall()
                    inv = {r['room']: r['count'] for r in con.execute('SELECT * FROM inventory')}
                    cls = con.execute('SELECT room, date_from, date_to FROM closures WHERE date_from < ? AND date_to >= ?', (end.isoformat(), start.isoformat())).fetchall()
                    rows = []
                    for k, v in ROOMS.items():
                        cells = []
                        for d in dates:
                            ds = d.isoformat()
                            conf = sum(held(units(x)).get(k, 0) for x in bks if x['status'] == 'confirmed' and x['check_in'] <= ds < x['check_out'])
                            new = sum(held(units(x)).get(k, 0) for x in bks if x['status'] == 'new' and x['check_in'] <= ds < x['check_out'])
                            closed = any((c['room'] in (None, k)) and c['date_from'] <= ds <= c['date_to'] for c in cls)
                            cells.append([conf, new, 1 if closed else 0])
                        rows.append({'id': k, 'name': v[1], 'count': inv.get(k), 'cells': cells})
                    unassigned = [[sum((_g(x, 'rooms') or 1) for x in bks if not units(x) and x['status'] == s and x['check_in'] <= d.isoformat() < x['check_out']) for s in ('confirmed', 'new')] for d in dates]
                    return self.send(200, {'ok': True, 'dates': [d.isoformat() for d in dates], 'rows': rows, 'any': unassigned})
                if p == '/api/admin/export.xlsx':
                    return self.send(200, raw=export_book(con), ctype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                     extra={'Content-Disposition': 'attachment; filename="royalinn-bookings-%s.xlsx"' % dt.date.today().isoformat()})
                if p == '/api/admin/export.csv':
                    out = io.StringIO(); w = csv.writer(out)
                    cols = ['ref', 'created', 'status', 'source', 'check_in', 'check_out', 'nights', 'guests', 'room', 'breakfast', 'total', 'paid', 'room_no', 'arrived', 'departed', 'name', 'phone', 'email', 'message', 'staff_note', 'pay_by', 'net', 'referrer']
                    load_agencies(con)
                    w.writerow(cols + ['agency'])
                    for r in con.execute('SELECT * FROM bookings ORDER BY created'):
                        safe = lambda v: ("'" + v) if isinstance(v, str) and v[:1] in ('=', '+', '-', '@', '\t', '\r') and not re.fullmatch(r'\+?[0-9 ()\-]+', v) else v
                        w.writerow([safe('' if r[c] is None else r[c]) for c in cols] + [safe(AGENCIES.get(r['agency_id'], '') if r['agency_id'] else '')])
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
                _n = [a + dt.timedelta(days=i) for i in range((b - a).days)]
                combo = v['combo'] or ({v['room']: rooms_needed(v['room'], v['guests'])} if v['room'] else None)
                need = sum(combo.values()) if combo else rooms_needed(None, v['guests'])
                avail = combo_free(con, a, b, combo) if combo else availability(con, a, b, None)
                total = None
                if combo:
                    per, _pct = discounted(con, combo_nightly(con, combo, v['guests'], _n), _n)
                    total = None if any(x is None for x in per) else sum(per)
                    if total is not None and v['breakfast']:
                        total += BREAKFAST * v['guests'] * len(per)
                rec = dict(total=total, source='web', ref=ref, created=now(), lang=v['lang'], status='new', check_in=a.isoformat(), check_out=b.isoformat(),
                           nights=(b - a).days, guests=v['guests'], rooms=need, combo=json.dumps(combo) if (combo and len(combo) > 1) else None, room=v['room'] if (not combo or len(combo) == 1) else None, breakfast=v['breakfast'], name=v['name'],
                           phone=v['phone'], email=v['email'], message=v['message'])
                con.execute('INSERT INTO bookings(ref,created,lang,status,check_in,check_out,nights,guests,rooms,combo,room,breakfast,name,phone,email,message,total,source) '
                            'VALUES(:ref,:created,:lang,:status,:check_in,:check_out,:nights,:guests,:rooms,:combo,:room,:breakfast,:name,:phone,:email,:message,:total,:source)', rec)
            threading.Thread(target=notify, args=(rec,), daemon=True).start()
            return self.send(200, {'ok': True, 'ref': ref, 'available': avail, 'total': total, 'rooms': need})

        if p == '/api/inquiry':
            if d.get('website'):                     # hidden field, filled only by bots
                return self.send(200, {'ok': True, 'id': 0})
            if limited('iq:' + self.ip(), 5, 600):
                return self.send(429, {'ok': False, 'error': 'busy'})
            errs, v = validate_inquiry(d)
            if errs:
                return self.send(400, {'ok': False, 'error': 'fields', 'fields': errs})
            with LOCK, db() as con:
                v['created'] = now()
                cur = con.execute('INSERT INTO inquiries(created,lang,kind,name,company,phone,email,date,guests,rooms,nights,message,status) '
                                  "VALUES(:created,:lang,:kind,:name,:company,:phone,:email,:date,:guests,:rooms,:nights,:message,'new')", v)
                v['id'] = cur.lastrowid
            threading.Thread(target=notify_inquiry, args=(v,), daemon=True).start()
            return self.send(200, {'ok': True, 'id': v['id']})

        if p == '/api/admin/login':
            pw = os.environ.get('PANEL_PASSWORD', '')
            if not pw:
                return self.send(503, {'ok': False, 'error': 'not_configured'})
            t0 = time.time()      # only wrong passwords count: several staff devices behind the hotel's one address can all sign in
            if len([x for x in HITS.get('login:' + self.ip(), []) if t0 - x < 900]) >= 8:
                return self.send(429, {'ok': False, 'error': 'busy'})
            if not hmac.compare_digest(str(d.get('password', '')).encode(), pw.encode()):
                limited('login:' + self.ip(), 8, 900)
                time.sleep(1)
                return self.send(401, {'ok': False, 'error': 'password'})
            tok = sign({'exp': int(time.time()) + SESSION_HOURS * 3600, 'pw': hashlib.sha256(pw.encode()).hexdigest()[:16], 'n': secrets.token_hex(4)})
            return self.send(200, {'ok': True}, extra={'Set-Cookie': f'ri_panel={tok}; Path=/api/admin/; Max-Age={SESSION_HOURS*3600}; HttpOnly; Secure; SameSite=Strict'})
        if p == '/api/admin/logout':
            return self.send(200, {'ok': True}, extra={'Set-Cookie': 'ri_panel=; Path=/api/admin/; Max-Age=0; HttpOnly; Secure; SameSite=Strict'})

        if p.startswith('/api/admin/'):
            if not self.session():
                return self.send(401, {'ok': False, 'error': 'auth'})
            m = re.match(r'^/api/admin/inquiries/(\d+)$', p)
            if m:
                st = d.get('status')
                note = clean(d.get('staff_note'), 600) if 'staff_note' in d else None
                if st is not None and st not in INQ_STATUSES:
                    return self.send(400, {'ok': False})
                with LOCK, db() as con:
                    if not con.execute('SELECT 1 FROM inquiries WHERE id=?', (int(m.group(1)),)).fetchone():
                        return self.send(404, {'ok': False})
                    if st is not None:
                        con.execute('UPDATE inquiries SET status=?, updated=? WHERE id=?', (st, now(), int(m.group(1))))
                    if note is not None:
                        con.execute('UPDATE inquiries SET staff_note=?, updated=? WHERE id=?', (note or None, now(), int(m.group(1))))
                return self.send(200, {'ok': True})
            if p == '/api/admin/bookings/new':
                f, t = parse_day(d.get('in')), parse_day(d.get('out'))
                name, phone = clean(d.get('name'), 80), clean(d.get('phone'), 24)
                room = d.get('room') or None
                try:
                    guests = max(1, min(MAX_PARTY, int(d.get('guests') or 2)))
                    total = int(d['total']) if d.get('total') not in (None, '') else None
                except (TypeError, ValueError):
                    return self.send(400, {'ok': False, 'error': 'fields'})
                email = clean(d.get('email'), 120) or None
                if too_many(room if room in ROOMS else None, guests):
                    return self.send(400, {'ok': False, 'error': 'guests', 'max': MAX_GUESTS.get(room, MAX_ANY) if room else MAX_ANY})
                if not f or not t or t <= f or (t - f).days > 120 or len(name) < 2 or (room and room not in ROOMS) or (email and not EMAIL_RE.match(email)):
                    return self.send(400, {'ok': False, 'error': 'fields'})
                with LOCK, db() as con:
                    ref = new_ref()
                    while con.execute('SELECT 1 FROM bookings WHERE ref=?', (ref,)).fetchone():
                        ref = new_ref()
                    if total is None and room:
                        _ns = [f + dt.timedelta(days=i) for i in range((t - f).days)]
                        per, _pct = discounted(con, nightly(con, room, guests, _ns), _ns)
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
                    con.execute('INSERT INTO bookings(ref,created,lang,status,check_in,check_out,nights,guests,rooms,room,breakfast,name,phone,email,message,total,source,staff_note,updated,agency_id,pay_by,referrer,net) '
                                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                (ref, now(), 'en' if d.get('lang') == 'en' else 'ar', 'confirmed', f.isoformat(), t.isoformat(), (t - f).days, guests, rooms_needed(room, guests), room,
                                 1 if d.get('breakfast') else 0, name, phone or '-', email, clean(d.get('message'), 600) or None, total, src,
                                 clean(d.get('note'), 400) or None, now(), aid, pay_by, clean(d.get('referrer'), 80) or None, net_of(con, total, aid)))
                    r = con.execute('SELECT * FROM bookings WHERE ref=?', (ref,)).fetchone(); S = settings(con)
                mailed = bool(email and mail_on())
                if mailed:
                    threading.Thread(target=mail_confirmation, args=(dict(r), S), daemon=True).start()
                return self.send(200, {'ok': True, 'ref': ref, 'mailed': mailed})
            if p == '/api/admin/room-info':
                # {room, ar, en, tr}: the hotel's own description of a room type; an empty text goes back to the site's text
                k = d.get('room')
                if k not in ROOMS:
                    return self.send(400, {'ok': False, 'error': 'room'})
                v = {x: (str(d.get(x) or '').replace('\r', '').strip()[:600] or None) for x in ('ar', 'en', 'tr')}
                with db() as con:
                    con.execute('INSERT OR REPLACE INTO room_info(room, ar, en, tr, updated) VALUES(?,?,?,?,?)', (k, v['ar'], v['en'], v['tr'], now()))
                    info = room_info(con)
                return self.send(200, {'ok': True, 'room': info.get(k, {})})
            if p == '/api/admin/prices':
                with db() as con:
                    for k in ROOMS:
                        if k == FAMILY or k not in d or not isinstance(d[k], dict):
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
            m = re.fullmatch(r'/api/admin/agencies/(\d+)/delete', p)
            if m:
                with LOCK, db() as con:
                    if con.execute('SELECT 1 FROM bookings WHERE agency_id=?', (int(m.group(1)),)).fetchone():
                        return self.send(409, {'ok': False, 'error': 'has_bookings'})
                    con.execute('DELETE FROM agency_payments WHERE agency_id=?', (int(m.group(1)),))
                    con.execute('DELETE FROM agencies WHERE id=?', (int(m.group(1)),))
                    load_agencies(con)
                return self.send(200, {'ok': True})
            m = re.fullmatch(r'/api/admin/bookings/(RI-[A-Z0-9]{6})/mail', p)
            if m:
                with db() as con:
                    r = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone(); S = settings(con)
                if not r:
                    return self.send(404, {'ok': False})
                if not mail_on():
                    return self.send(200, {'ok': False, 'error': 'mail_off'})
                if not r['email']:
                    return self.send(200, {'ok': False, 'error': 'no_email'})
                sent = mail_confirmation(dict(r), S) if r['status'] == 'confirmed' else mail_received(dict(r), S)
                return self.send(200, {'ok': bool(sent), 'error': None if sent else 'send_failed'})
            if p == '/api/admin/mail-test':
                to = clean(d.get('to'), 120)
                if not EMAIL_RE.match(to or ''):
                    return self.send(400, {'ok': False, 'error': 'to'})
                if not mail_on():
                    return self.send(200, {'ok': False, 'error': 'mail_off'})
                sent = send_mail(to, 'Royal Inn Tartus - test', 'This is a test message from the Royal Inn booking system.\nهذه رسالة تجريبية من نظام حجوزات فندق رويال إن.')
                return self.send(200, {'ok': bool(sent), 'error': None if sent else 'send_failed'})
            m = re.fullmatch(r'/api/admin/bookings/(RI-[A-Z0-9]{6})/delete', p)
            if m:
                # only a cancelled booking can be removed for good (test entries, duplicates)
                with LOCK, db() as con:
                    cur = con.execute('SELECT status FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
                    if not cur:
                        return self.send(404, {'ok': False})
                    if cur['status'] != 'cancelled':
                        return self.send(409, {'ok': False, 'error': 'not_cancelled'})
                    con.execute('DELETE FROM bookings WHERE ref=?', (m.group(1),))
                return self.send(200, {'ok': True})
            m = re.fullmatch(r'/api/admin/bookings/(RI-[A-Z0-9]{6})/edit', p)
            if m:
                # change the guest's details, dates, room type or number of guests; the total is recalculated unless one is given
                with LOCK, db() as con:
                    cur = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
                    if not cur:
                        return self.send(404, {'ok': False})
                    f, t = parse_day(d.get('in')) or dt.date.fromisoformat(cur['check_in']), parse_day(d.get('out')) or dt.date.fromisoformat(cur['check_out'])
                    name = clean(d.get('name', cur['name']), 80); phone = clean(d.get('phone', cur['phone']), 24) or '-'
                    email = clean(d.get('email', cur['email'] or ''), 120) or None
                    room = d.get('room', cur['room']) or None
                    try:
                        guests = max(1, min(MAX_PARTY, int(d.get('guests', cur['guests']))))
                    except (TypeError, ValueError):
                        return self.send(400, {'ok': False, 'error': 'guests'})
                    if len(name) < 2 or t <= f or (t - f).days > 120 or (room and room not in ROOMS) or (email and not EMAIL_RE.match(email)):
                        return self.send(400, {'ok': False, 'error': 'fields'})
                    if too_many(room if room in ROOMS else None, guests):
                        return self.send(400, {'ok': False, 'error': 'guests', 'max': MAX_GUESTS.get(room, MAX_ANY) if room else MAX_ANY})
                    bf = 1 if d.get('breakfast', bool(cur['breakfast'])) else 0
                    keep = _g(cur, 'combo') if room == cur['room'] else None   # a mixed choice stays while it still holds the party
                    if keep and guests != cur['guests'] and not parse_combo(keep, guests):
                        keep = None
                    kc = json.loads(keep) if keep else None
                    stay_changed = (f.isoformat(), t.isoformat(), room, guests, bf) != (cur['check_in'], cur['check_out'], cur['room'], cur['guests'], cur['breakfast'])
                    free = True
                    if stay_changed and cur['status'] == 'confirmed':
                        free = combo_free(con, f, t, kc, cur['ref']) if kc else availability(con, f, t, room, cur['ref'], need=rooms_needed(room, guests))
                    try:
                        total = int(d['total']) if d.get('total') not in (None, '') else None
                    except (TypeError, ValueError):
                        return self.send(400, {'ok': False, 'error': 'total'})
                    if total is None:
                        total = cur['total']
                        if stay_changed:
                            total = None
                            if room or kc:
                                ns = [f + dt.timedelta(days=i) for i in range((t - f).days)]
                                per, _pct = discounted(con, combo_nightly(con, kc, guests, ns) if kc else nightly(con, room, guests, ns), ns)
                                if not any(x is None for x in per):
                                    total = sum(per) + (BREAKFAST * guests * len(per) if bf else 0) + (cur['late_fee'] or 0)
                    res = {'ok': True, 'total': total, 'old_total': cur['total'], 'available': free, 'nights': (t - f).days}
                    if d.get('preview'):
                        return self.send(200, res)
                    if not free and not d.get('force'):
                        return self.send(409, dict(res, ok=False, error='full'))
                    room_no = cur['room_no']
                    if room_no and stay_changed:
                        keep_nos = [x for x in room_nos(room_no) if not unit_busy(con, x, f.isoformat(), t.isoformat(), cur['ref'])]
                        room_no = ', '.join(keep_nos[:len(slots({'combo': keep, 'room': room, 'rooms': rooms_needed(room, guests)}))]) or None   # a room taken on the new dates is dropped; the desk picks another
                    if (room != cur['room'] or keep != _g(cur, 'combo')) and not _g(cur, 'req_room') and not _g(cur, 'req_combo'):
                        # remember what the guest first asked for, once, so their status page can show the change
                        con.execute('UPDATE bookings SET req_room=?, req_combo=? WHERE ref=?', (cur['room'] or '-', _g(cur, 'combo'), m.group(1)))
                    con.execute('UPDATE bookings SET name=?, phone=?, email=?, check_in=?, check_out=?, nights=?, guests=?, rooms=?, combo=?, room=?, breakfast=?, total=?, net=?, room_no=?, updated=? WHERE ref=?',
                                (name, phone, email, f.isoformat(), t.isoformat(), (t - f).days, guests, (sum(units(cur).values()) if keep else rooms_needed(room, guests)), keep, room, bf, total, net_of(con, total, cur['agency_id']), room_no, now(), m.group(1)))
                    r = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
                return self.send(200, dict(res, item=row(r), room_no_cleared=bool(cur['room_no'] and room_nos(room_no) != room_nos(cur['room_no']))))
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
            if p == '/api/admin/settings':
                vals = {}
                for k in DEFAULTS:
                    if k not in d:
                        continue
                    v = str(d[k]).strip()
                    if k == 'deposit_info':
                        v = clean(d[k], 500)
                    elif k in ('checkin', 'checkout', 'late_half_until'):
                        if not TIME_RE.match(v):
                            return self.send(400, {'ok': False, 'error': k})
                    elif k in ('late_on', 'noshow_on', 'early_note'):
                        v = '1' if d[k] in (True, 1, '1', 'on') else '0'
                    else:
                        try:
                            n = float(v) if k == 'remind_hours' else int(v)
                        except ValueError:
                            return self.send(400, {'ok': False, 'error': k})
                        lim = {'remind_hours': (0.5, 12), 'late_grace': (0, 240), 'late_half_pct': (0, 100), 'late_full_pct': (0, 100)}[k]
                        if not lim[0] <= n <= lim[1]:
                            return self.send(400, {'ok': False, 'error': k})
                    vals[k] = v
                with db() as con:
                    S = dict(settings(con), **vals)
                    if hm(S['late_half_until']) <= hm(S['checkout']) + int(S['late_grace']):
                        return self.send(400, {'ok': False, 'error': 'late_half_until'})
                    for k, v in vals.items():
                        con.execute('INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)', (k, v))
                return self.send(200, {'ok': True})
            if p == '/api/admin/units':
                room = d.get('room')
                if room not in UNIT_TYPES and room != CONNECT:
                    return self.send(400, {'ok': False, 'error': 'room'})
                nos = []
                a, b = clean(d.get('no'), 12), clean(d.get('to'), 12)
                if a.isdigit() and b.isdigit() and 0 <= int(b) - int(a) <= 200:
                    nos = [str(x) for x in range(int(a), int(b) + 1)]
                elif a and re.fullmatch(r'[0-9A-Za-z\-]{1,12}', a):
                    nos = [a]
                if not nos:
                    return self.send(400, {'ok': False, 'error': 'no'})
                added = 0
                with LOCK, db() as con:
                    for x in nos:
                        if not con.execute('SELECT 1 FROM units WHERE no=?', (x,)).fetchone():
                            con.execute('INSERT INTO units(no,room,floor,view) VALUES(?,?,?,?)', (x, room, clean(d.get('floor'), 12) or None, d.get('view') if d.get('view') in VIEWS else None)); added += 1
                    sync_inventory(con)
                return self.send(200, {'ok': True, 'added': added, 'skipped': len(nos) - added})
            m = re.fullmatch(r'/api/admin/units/(\d+)(/delete)?', p)
            if m:
                with LOCK, db() as con:
                    u0 = con.execute('SELECT room FROM units WHERE id=?', (int(m.group(1)),)).fetchone()
                    if not u0:
                        return self.send(404, {'ok': False})
                    had_c = bool(con.execute('SELECT 1 FROM units WHERE room=?', (CONNECT,)).fetchone())
                    if m.group(2):
                        con.execute('DELETE FROM units WHERE id=?', (int(m.group(1)),))
                        if not con.execute('SELECT 1 FROM units WHERE room=?', (u0['room'],)).fetchone():
                            con.execute('DELETE FROM inventory WHERE room=?', (u0['room'],))
                    else:
                        if d.get('state') in ('clean', 'dirty', 'out'):
                            con.execute('UPDATE units SET state=? WHERE id=?', (d['state'], int(m.group(1))))
                        if d.get('room') in UNIT_TYPES or d.get('room') == CONNECT:
                            con.execute('UPDATE units SET room=? WHERE id=?', (d['room'], int(m.group(1))))
                        if 'merge' in d:          # a connecting room: merged with its suite, or sold as a Standard room
                            con.execute('UPDATE units SET merge=? WHERE id=?', (1 if d.get('merge') else 0, int(m.group(1))))
                        if 'pair' in d:
                            con.execute('UPDATE units SET pair=? WHERE id=?', (clean(d.get('pair'), 12) or None, int(m.group(1))))
                        if 'view' in d:
                            con.execute('UPDATE units SET view=? WHERE id=?', (d['view'] if d.get('view') in VIEWS else None, int(m.group(1))))
                        if 'note' in d:
                            con.execute('UPDATE units SET note=? WHERE id=?', (clean(d.get('note'), 120) or None, int(m.group(1))))
                    sync_inventory(con)
                    if had_c and not con.execute('SELECT 1 FROM units WHERE room=?', (CONNECT,)).fetchone():
                        con.execute('INSERT OR REPLACE INTO inventory(room,count) VALUES(?,0)', (FAMILY,))   # no connecting room left: nothing to sell as a family suite
                return self.send(200, {'ok': True})
            m = re.fullmatch(r'/api/admin/bookings/(RI-[A-Z0-9]{6})/late', p)
            if m:
                # late check-out fee by the hotel's rule: a share of the last night's rate, added to the total
                with LOCK, db() as con:
                    cur = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
                    if not cur or cur['status'] != 'confirmed':
                        return self.send(404, {'ok': False})
                    S = settings(con); rate = night_rate(con, cur)
                    try:
                        fee = int(d['amount']) if d.get('amount') not in (None, '') else None
                    except (TypeError, ValueError):
                        return self.send(400, {'ok': False})
                    if fee is None:
                        if rate is None:
                            return self.send(400, {'ok': False, 'error': 'rate'})
                        fee = r5(rate * int(S['late_full_pct' if d.get('kind') == 'full' else 'late_half_pct']) / 100.0)
                    if not 0 <= fee <= 100000:
                        return self.send(400, {'ok': False})
                    base = (cur['total'] or 0) - (cur['late_fee'] or 0)       # replaces an earlier late fee, never stacks
                    total = base + fee
                    con.execute('UPDATE bookings SET late_fee=?, total=?, net=?, updated=? WHERE ref=?', (fee or None, total, net_of(con, total, cur['agency_id']), now(), m.group(1)))
                    r = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
                return self.send(200, {'ok': True, 'fee': fee, 'item': row(r)})
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
                    kc = json.loads(_g(cur, 'combo')) if _g(cur, 'combo') else None
                    free = True if sgn < 0 or cur['status'] != 'confirmed' else (
                        combo_free(con, a, b, kc, cur['ref']) if kc else availability(con, a, b, cur['room'], cur['ref'], need=_g(cur, 'rooms') or rooms_needed(cur['room'], cur['guests'])))
                    amount = None
                    if cur['room'] or kc:
                        per, _pct = discounted(con, combo_nightly(con, kc, cur['guests'], nights) if kc else nightly(con, cur['room'], cur['guests'], nights), nights)
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
                    room_no = cur['room_no']
                    if 'room_no' in d:
                        raw = d.get('room_no'); lst = [clean(x, 12) for x in (raw if isinstance(raw, list) else room_nos(raw))]
                        lst = [x for x in lst if x]
                        if len(lst) != len(set(lst)):
                            return self.send(400, {'ok': False, 'error': 'room_twice'})
                        if len(lst) > len(slots(cur)):
                            return self.send(400, {'ok': False, 'error': 'room_count', 'max': len(slots(cur))})
                        room_no = ', '.join(lst) or None
                        if (st or cur['status']) == 'confirmed' and not d.get('force'):
                            for x in lst:
                                if x in room_nos(cur['room_no']):
                                    continue
                                clash = unit_busy(con, x, cur['check_in'], cur['check_out'], cur['ref'])
                                if clash:
                                    return self.send(409, {'ok': False, 'error': 'room_taken', 'by': clash['name'], 'ref': clash['ref'], 'no': x})
                    if st == 'confirmed' and cur['status'] != 'confirmed' and not d.get('force') and not booking_fits(con, cur):
                        return self.send(409, {'ok': False, 'error': 'full'})
                    if d.get('no_show'):
                        st = 'cancelled'; d['note'] = ((cur['staff_note'] or '') + ' · عدم حضور (No-show)').strip(' ·')
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
                    if departed and not cur['departed'] and room_no:
                        for x in room_nos(room_no):
                            con.execute("UPDATE units SET state='dirty' WHERE no=? AND state='clean'", (x,))
                    if d.get('reminded'):
                        con.execute('UPDATE bookings SET reminded=? WHERE ref=?', (now(), m.group(1)))
                    if d.get('no_show'):
                        con.execute('UPDATE bookings SET no_show=1 WHERE ref=?', (m.group(1),))
                    con.execute('UPDATE bookings SET status=?, staff_note=?, room_no=?, paid=?, arrived=?, departed=?, agency_id=?, pay_by=?, source=?, referrer=?, net=?, updated=? WHERE ref=?',
                                (st or cur['status'], clean(d.get('note', cur['staff_note']), 400) or None, room_no, paid, arrived, departed, aid, pay_by, src, referrer, net_of(con, cur['total'], aid), now(), m.group(1)))
                    r = con.execute('SELECT * FROM bookings WHERE ref=?', (m.group(1),)).fetchone()
                    S = settings(con)
                mailed = bool(st == 'confirmed' and cur['status'] != 'confirmed' and r['email'] and mail_on())
                if mailed:
                    threading.Thread(target=mail_confirmation, args=(dict(r), S), daemon=True).start()
                return self.send(200, {'ok': True, 'item': row(r), 'mailed': mailed})
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
    threading.Thread(target=reminder_loop, daemon=True).start()
    print(f'booking api on {HOST}:{PORT}, data in {DATA}', flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
