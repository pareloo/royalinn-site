/* Royal Inn - booking panel. Talks only to /api/admin on this site. */
(function () {
  'use strict';
  var $ = function (s) { return document.querySelector(s); };
  var each = function (sel, fn) { Array.prototype.forEach.call(document.querySelectorAll(sel), fn); };
  var tab = 'home', ROOMS = [], ITEMS = [];
  var LABEL = { 'new': 'جديد', confirmed: 'مؤكَّد', cancelled: 'ملغى' };
  var DAYS = ['أحد', 'إثن', 'ثلا', 'أرب', 'خمي', 'جمع', 'سبت'];
  var DAYS_LONG = ['الأحد', 'الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت'];
  var MONTHS = ['كانون الثاني', 'شباط', 'آذار', 'نيسان', 'أيار', 'حزيران', 'تموز', 'آب', 'أيلول', 'تشرين الأول', 'تشرين الثاني', 'كانون الأول'];

  function api(path, data) {
    var opt = { credentials: 'same-origin', headers: {} };
    if (data !== undefined) { opt.method = 'POST'; opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(data); }
    return fetch('/api/admin/' + path, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { j.status = r.status; return j; });
    }, function () { toast('تعذّر الاتصال بالخادم. تحقّق من الإنترنت.'); return { status: 0 }; });
  }
  function toast(t) { var el = $('#toast'); el.textContent = t; el.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(function () { el.hidden = true; }, 3000); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function fmt(d) { var p = d.split('-'); return p[2] + '/' + p[1] + '/' + p[0]; }
  function day(ds) { var p = ds.split('-'); return new Date(+p[0], +p[1] - 1, +p[2]); }
  function longDate(ds) { var d = day(ds); return DAYS_LONG[d.getDay()] + ' ' + d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear(); }
  function iso(t) { return t.getFullYear() + '-' + ('0' + (t.getMonth() + 1)).slice(-2) + '-' + ('0' + t.getDate()).slice(-2); }
  function digits(p) { var x = p.replace(/[^0-9]/g, ''); if (x.indexOf('00') === 0) x = x.slice(2); else if (x.indexOf('09') === 0 && x.length === 10) x = '963' + x.slice(1); return x; }
  function gone(j) { if (j.status === 401) { show('login'); return true; } return false; }

  function show(which) {
    $('#login').hidden = which !== 'login'; $('#app').hidden = which !== 'app'; $('#logout').hidden = which !== 'app';
  }

  function start() {
    $('#top-date').textContent = longDate(iso(new Date()));
    api('me').then(function (j) {
      if (j.auth) { show('app'); load(); }
      else { show('login'); if (j.status && !j.configured) { $('#login-msg').textContent = 'اللوحة غير مفعّلة بعد: يجب إضافة كلمة المرور في إعدادات الخادم (PANEL_PASSWORD).'; } }
    });
  }

  $('#login-form').addEventListener('submit', function (e) {
    e.preventDefault();
    api('login', { password: e.target.password.value }).then(function (j) {
      if (j.ok) { e.target.reset(); show('app'); go('home'); }
      else if (j.status) { toast(j.error === 'busy' ? 'محاولات كثيرة، انتظر ربع ساعة.' : j.error === 'not_configured' ? 'اللوحة غير مفعّلة بعد.' : 'كلمة المرور غير صحيحة.'); }
    });
  });
  $('#logout').addEventListener('click', function () { api('logout', {}).then(function () { show('login'); }); });

  function go(t) {
    tab = t;
    each('.tabs button', function (x) { var on = x.getAttribute('data-tab') === t; x.classList.toggle('on', on); if (on && x.scrollIntoView) { try { x.scrollIntoView({ block: 'nearest', inline: 'center' }); } catch (e) {} } });
    window.scrollTo(0, 0);
    load();
  }
  each('.tabs button', function (b) { b.addEventListener('click', function () { go(b.getAttribute('data-tab')); }); });

  function load() {
    var list = tab === 'new' || tab === 'active' || tab === 'all';
    $('#view-list').hidden = !list;
    ['home', 'closures', 'inventory', 'occupancy', 'add', 'prices', 'offers'].forEach(function (v) { $('#view-' + v).hidden = tab !== v; });
    if (list) loadList(); else if (tab === 'home') loadHome(); else if (tab === 'closures') loadClosures(); else if (tab === 'inventory') loadInventory();
    else if (tab === 'occupancy') loadOcc(); else if (tab === 'add') rooms(); else if (tab === 'prices') loadPrices(); else if (tab === 'offers') loadOffers();
  }

  // the count of waiting requests shows on the menu and in the browser tab title, on every screen
  function badge(n) { var b = $('#c-new'); b.textContent = n; b.hidden = !n; document.title = (n ? '(' + n + ') ' : '') + 'لوحة الحجوزات | Royal Inn'; }

  /* ---------------------------------------------------------------- home */
  function kpi(n, label, cls, target) {
    var k = el(target ? 'button' : 'div', 'kpi' + (cls ? ' ' + cls : ''));
    if (target) { k.type = 'button'; k.addEventListener('click', function () { go(target); }); }
    k.appendChild(el('span', 'kpi-n', String(n))); k.appendChild(el('span', 'kpi-l', label));
    return k;
  }
  function mini(box, items, empty, what) {
    box.textContent = '';
    if (!items.length) { box.appendChild(el('p', 'empty', empty)); return; }
    items.forEach(function (b) {
      var r = el('div', 'mini');
      r.appendChild(el('b', null, b.name));
      var sub = what === 'new' ? fmt(b.check_in) + ' ← ' + fmt(b.check_out) + ' · ' + b.room_name : b.room_name + ' · ' + b.nights + ' ليالٍ · ' + b.guests + ' ضيوف';
      r.appendChild(el('span', 'm-sub', sub));
      if (b.phone) { var a = el('a', 'btn ghost', 'اتصال'); a.href = 'tel:' + b.phone.replace(/[^0-9+]/g, ''); r.appendChild(a); }
      box.appendChild(r);
    });
  }
  function loadHome() {
    api('dashboard').then(function (j) {
      if (gone(j) || !j.ok) return;
      badge(j.counts['new'] || 0);
      $('#home-title').textContent = 'اليوم، ' + longDate(j.today);
      var set = j.inventory_set === j.rooms.length, total = j.total_rooms;
      var alertBox = $('#home-alert'); alertBox.textContent = '';
      if (!set) {
        var n = el('div', 'notice');
        n.appendChild(el('span', null, j.inventory_set ? 'عدد الغرف غير مكتمل لبعض الأنواع. ' : 'لم يُدخَل عدد الغرف بعد. '));
        n.appendChild(el('b', null, 'الموقع لا يعرف متى تمتلئ الغرف، ويقبل الطلبات دائماً.'));
        var bt = el('button', 'gold sm', 'أدخل عدد الغرف'); bt.type = 'button'; bt.addEventListener('click', function () { go('inventory'); }); n.appendChild(bt);
        alertBox.appendChild(n);
      }
      var k = $('#kpis'); k.textContent = '';
      k.appendChild(kpi(j.counts['new'] || 0, 'طلبات جديدة تنتظر الرد', (j.counts['new'] ? 'hot' : ''), 'new'));
      k.appendChild(kpi(j.arrivals.length, 'وصول اليوم'));
      k.appendChild(kpi(j.departures.length, 'مغادرة اليوم'));
      k.appendChild(kpi(j.booked_tonight, 'غرف محجوزة الليلة', '', 'occupancy'));
      $('#home-inhouse').textContent = 'ضيوف سُجِّل وصولهم وما زالوا في الفندق: ' + j.in_house;
      k.appendChild(kpi(total == null ? '–' : Math.max(0, total - j.booked_tonight), 'غرف متاحة الليلة'));
      k.appendChild(kpi(total ? Math.round(100 * j.booked_tonight / total) + '%' : '–', 'نسبة الإشغال الليلة'));

      $('#rooms-hint').textContent = set ? 'المحجوز من كل نوع هذه الليلة (حجوزات مؤكَّدة) والمتاح منه.' : 'تظهر الأعداد المتاحة هنا بعد إدخال عدد الغرف.';
      var rb = $('#home-rooms'); rb.textContent = '';
      j.rooms.forEach(function (r) {
        var full = r.count != null && r.booked >= r.count;
        var row = el('div', 'rm' + (r.closed ? ' closed' : full ? ' full' : ''));
        row.appendChild(el('span', 'rm-name', r.name));
        row.appendChild(el('span', 'rm-num', r.count == null ? String(r.booked) : r.booked + ' / ' + r.count));
        var bar = el('div', 'rm-bar'), fill = el('i'); fill.style.width = (r.count ? Math.min(100, 100 * r.booked / r.count) : (r.booked ? 100 : 0)) + '%'; bar.appendChild(fill); row.appendChild(bar);
        row.appendChild(el('span', 'rm-sub', r.closed ? 'مغلق اليوم' : r.count == null ? 'العدد غير محدد' : full ? 'مكتمل' : 'متاح: ' + r.free));
        rb.appendChild(row);
      });
      if (j.unassigned_tonight) rb.appendChild(el('p', 'muted small', 'حجوزات مؤكَّدة بدون نوع غرفة محدد الليلة: ' + j.unassigned_tonight));

      var wk = $('#home-week'); wk.textContent = '';
      j.week.forEach(function (d, i) {
        var dd = day(d.date), c = el('div', 'wd' + (d.closed ? ' closed' : total && d.booked >= total ? ' full' : d.booked ? ' some' : '') + (i === 0 ? ' today' : ''));
        c.appendChild(el('span', null, DAYS[dd.getDay()]));
        var b = el('b', null, String(d.booked)); if (d['new']) b.appendChild(el('sup', null, '+' + d['new'])); c.appendChild(b);
        c.appendChild(el('span', null, ('0' + dd.getDate()).slice(-2) + '/' + ('0' + (dd.getMonth() + 1)).slice(-2)));
        wk.appendChild(c);
      });
      mini($('#home-arr'), j.arrivals, 'لا يوجد وصول اليوم.');
      mini($('#home-dep'), j.departures, 'لا توجد مغادرة اليوم.');
      mini($('#home-tom'), j.tomorrow, 'لا يوجد وصول غداً.');
      mini($('#home-new'), j.newest, 'لا توجد طلبات جديدة.', 'new');
    });
  }
  $('#home-refresh').addEventListener('click', function () { loadHome(); toast('تم التحديث'); });

  /* ---------------------------------------------------------------- booking lists */
  function loadList() {
    api('bookings?status=' + tab).then(function (j) {
      if (gone(j) || !j.items) return;
      badge((j.counts && j.counts['new']) || 0);
      ITEMS = j.items;
      $('#list-h').textContent = { 'new': 'طلبات جديدة', active: 'الحجوزات القادمة', all: 'كل الطلبات' }[tab];
      $('#list-title').textContent = { 'new': 'طلبات بانتظار الرد', active: 'حجوزات لم تنتهِ بعد، حسب تاريخ الوصول', all: 'آخر 500 طلب' }[tab] + ' (' + ITEMS.length + ')';
      draw();
    });
  }
  function draw() {
    var q = $('#list-search').value.trim().toLowerCase(), box = $('#list'); box.textContent = '';
    var items = !q ? ITEMS : ITEMS.filter(function (b) {
      return (b.name + ' ' + b.ref + ' ' + b.phone + ' ' + (b.email || '')).toLowerCase().indexOf(q) !== -1 || digits(b.phone).indexOf(q.replace(/[^0-9]/g, '') || '~') !== -1;
    });
    if (!items.length) { box.appendChild(el('p', 'empty', q ? 'لا توجد نتيجة لهذا البحث.' : 'لا توجد طلبات هنا.')); return; }
    items.forEach(function (b) { box.appendChild(card(b)); });
  }
  $('#list-search').addEventListener('input', draw);

  function card(b) {
    var c = el('article', 'card bk st-' + b.status);
    var h = el('div', 'bk-head');
    h.appendChild(el('strong', 'name', b.name));
    h.appendChild(el('span', 'badge', LABEL[b.status]));
    if (b.departed) h.appendChild(el('span', 'badge gone', 'غادر')); else if (b.arrived) h.appendChild(el('span', 'badge in', 'في الفندق'));
    if (b.room_no) h.appendChild(el('span', 'badge rno', 'غرفة ' + b.room_no));
    h.appendChild(el('span', 'ref', b.ref));
    c.appendChild(h);
    var body = el('div', 'bk-body');

    var stay = el('div', 'stay');
    [['الوصول', b.check_in], ['المغادرة', b.check_out]].forEach(function (x) {
      var d = el('div', 'stay-d'); d.appendChild(el('span', null, x[0] + ' · ' + DAYS_LONG[day(x[1]).getDay()])); d.appendChild(el('b', null, fmt(x[1]))); stay.appendChild(d);
    });
    stay.appendChild(el('span', 'stay-n', b.nights + ' ليالٍ · ' + b.guests + ' ضيوف'));
    var tot = el('div', 'stay-t'); tot.appendChild(el('span', null, 'المبلغ الإجمالي')); tot.appendChild(el('b', null, b.total ? '$' + b.total : 'يُحدَّد'));
    if (b.paid != null && b.total) tot.appendChild(el('span', b.total - b.paid > 0 ? 'due' : 'paidok', b.total - b.paid > 0 ? 'مدفوع $' + b.paid + ' · الباقي $' + (b.total - b.paid) : 'مدفوع بالكامل'));
    stay.appendChild(tot);
    body.appendChild(stay);

    var dl = el('dl', 'facts');
    [['الغرفة', b.room_name], ['الفطور', b.breakfast ? 'نعم' : 'لا'], ['المصدر', b.source === 'desk' ? 'هاتف / استقبال' : 'الموقع'],
     ['الهاتف', b.phone || '-'], ['البريد', b.email || '-'], ['وصل الطلب (UTC)', b.created.replace('T', ' ').slice(0, 16)]].forEach(function (f) {
      var w = el('div'); w.appendChild(el('dt', null, f[0])); var dd = el('dd', null, String(f[1]));
      if (f[0] === 'الهاتف' || f[0] === 'البريد' || f[0].indexOf('UTC') !== -1) dd.dir = 'ltr';
      w.appendChild(dd); dl.appendChild(w);
    });
    body.appendChild(dl);
    if (b.message) body.appendChild(el('p', 'msg', b.message));

    var note = el('input', 'note'); note.type = 'text'; note.maxLength = 400; note.placeholder = 'ملاحظة داخلية (لا يراها الزبون)'; note.value = b.staff_note || '';
    note.setAttribute('aria-label', 'ملاحظة داخلية');
    body.appendChild(note);

    // front desk: room number, amount paid, arrival and departure, extending the stay
    var roomNo = null, paid = null, ext = null;
    if (b.status === 'confirmed') {
      var desk = el('div', 'desk');
      var l1 = el('label', null, 'رقم الغرفة'); roomNo = el('input'); roomNo.type = 'text'; roomNo.maxLength = 12; roomNo.value = b.room_no || ''; roomNo.dir = 'ltr'; l1.appendChild(roomNo); desk.appendChild(l1);
      var l2 = el('label', null, 'المدفوع $'); paid = el('input'); paid.type = 'number'; paid.min = 0; paid.inputMode = 'numeric'; paid.value = b.paid == null ? '' : b.paid; l2.appendChild(paid); desk.appendChild(l2);
      body.appendChild(desk);
      ext = el('div', 'ext'); ext.hidden = true;
      var l3 = el('label', null, 'تاريخ المغادرة الجديد'), nd = el('input'); nd.type = 'date'; nd.min = iso(new Date(day(b.check_in).getTime() + 864e5)); nd.value = b.check_out; l3.appendChild(nd); ext.appendChild(l3);
      var info = el('p', 'ext-info', 'اختر التاريخ الجديد لترى الفرق.'); ext.appendChild(info);
      var okb = el('button', 'gold', 'حفظ التاريخ الجديد'); okb.type = 'button'; okb.disabled = true; ext.appendChild(okb);
      var last = null;
      nd.addEventListener('change', function () {
        okb.disabled = true; last = null;
        if (!nd.value || nd.value === b.check_out) { info.textContent = 'اختر التاريخ الجديد لترى الفرق.'; return; }
        api('bookings/' + b.ref + '/stay', { out: nd.value, preview: true }).then(function (j) {
          if (gone(j)) return;
          if (!j.ok) { info.textContent = 'التاريخ غير صحيح.'; return; }
          last = j;
          var t = j.nights > 0 ? 'تمديد ' + j.nights + ' ليالٍ' : 'تقصير ' + (-j.nights) + ' ليالٍ';
          if (j.amount != null) t += ' · الفرق ' + (j.amount > 0 ? '+' : '−') + '$' + Math.abs(j.amount) + (j.total != null ? ' · المجموع الجديد $' + j.total : '');
          else t += ' · السعر يُحدَّد من الفندق';
          if (!j.available) t += ' · تنبيه: الغرف من هذا النوع مكتملة أو التواريخ مغلقة في الليالي الإضافية.';
          info.textContent = t; info.className = 'ext-info' + (j.available ? '' : ' warn'); okb.disabled = false;
        });
      });
      okb.addEventListener('click', function () {
        if (!last) return;
        if (!last.available && !confirm('الغرف مكتملة في الليالي الإضافية. هل تريد التمديد مع ذلك؟')) return;
        okb.disabled = true;
        api('bookings/' + b.ref + '/stay', { out: nd.value, force: !last.available }).then(function (j) {
          if (j.ok) { toast(last.nights > 0 ? 'تم تمديد الإقامة' : 'تم تعديل تاريخ المغادرة'); loadList(); } else if (!gone(j) && j.status) { toast('لم يتم الحفظ'); okb.disabled = false; }
        });
      });
      body.appendChild(ext);
    }

    var act = el('div', 'actions');
    if (b.phone) {
      var wa = el('a', 'btn wa', 'واتساب للزبون'); wa.target = '_blank'; wa.rel = 'noopener';
      var room = b.room_name === 'أي غرفة' ? '' : ' (' + b.room_name + ')';
      var text = b.lang === 'en'
        ? 'Hello ' + b.name + ', this is Royal Inn Tartus about your booking request ' + b.ref + ': ' + b.check_in + ' to ' + b.check_out + ', ' + b.guests + ' guests' + (b.total ? ', total $' + b.total : '') + '.'
        : 'مرحباً ' + b.name + '، معك فندق رويال إن طرطوس بخصوص طلب الحجز ' + b.ref + ': من ' + fmt(b.check_in) + ' إلى ' + fmt(b.check_out) + '، ' + b.guests + ' ضيوف' + room + (b.total ? '، المبلغ الإجمالي ' + b.total + '$' : '') + '.';
      wa.href = 'https://wa.me/' + digits(b.phone) + '?text=' + encodeURIComponent(text);
      act.appendChild(wa);
      var tel = el('a', 'btn ghost', 'اتصال'); tel.href = 'tel:' + b.phone.replace(/[^0-9+]/g, ''); act.appendChild(tel);
    }
    if (b.status === 'confirmed') {
      // confirmation letter: one tap sends the guest a link to it; it also prints as a PDF
      var url = location.origin + b.letter;
      if (b.phone) {
        var cw = el('a', 'btn gold', 'أرسل رسالة التأكيد'); cw.target = '_blank'; cw.rel = 'noopener';
        var ct = b.lang === 'en'
          ? 'Hello ' + b.name + ', your booking at Royal Inn Tartus is confirmed (' + b.ref + '). Your confirmation letter: ' + url
          : 'مرحباً ' + b.name + '، تم تأكيد حجزكم في فندق رويال إن طرطوس (' + b.ref + '). رسالة التأكيد: ' + url;
        cw.href = 'https://wa.me/' + digits(b.phone) + '?text=' + encodeURIComponent(ct);
        act.insertBefore(cw, act.firstChild);
      }
      var lt = el('a', 'btn ghost', 'رسالة التأكيد (طباعة)'); lt.href = b.letter; lt.target = '_blank'; lt.rel = 'noopener'; act.appendChild(lt);
    }
    if (b.status !== 'confirmed') act.appendChild(button('تأكيد الحجز', 'gold', 'confirmed'));
    if (b.status !== 'cancelled') act.appendChild(button('إلغاء', 'ghost danger', 'cancelled'));
    if (b.status !== 'new') act.appendChild(button('إرجاع إلى جديد', 'ghost', 'new'));
    if (b.status === 'confirmed') {
      var today = iso(new Date());
      if (!b.arrived && b.check_in <= today) act.appendChild(flag('تسجيل الوصول', 'gold', { arrived: true }, 'تم تسجيل وصول الضيف'));
      if (b.arrived && !b.departed) act.appendChild(flag('تسجيل المغادرة', 'ghost', { departed: true }, 'تم تسجيل المغادرة'));
      if (!b.departed) { var eb = el('button', 'ghost', 'تمديد / تعديل المغادرة'); eb.type = 'button'; eb.addEventListener('click', function () { ext.hidden = !ext.hidden; }); act.appendChild(eb); }
    }
    var save = el('button', 'ghost', b.status === 'confirmed' ? 'حفظ' : 'حفظ الملاحظة'); save.type = 'button';
    save.addEventListener('click', function () { update(b.ref, null, note.value, roomNo ? { room_no: roomNo.value, paid: paid.value } : null); });
    act.appendChild(save);
    body.appendChild(act);
    c.appendChild(body);
    return c;

    function flag(label, cls, data, msg) {
      var x = el('button', cls, label); x.type = 'button';
      x.addEventListener('click', function () { x.disabled = true; data.note = note.value; api('bookings/' + b.ref, data).then(function (j) { if (j.ok) { toast(msg); loadList(); } else if (!gone(j)) toast('لم يتم الحفظ'); }); });
      return x;
    }
    function button(label, cls, st) {
      var x = el('button', cls, label); x.type = 'button';
      x.addEventListener('click', function () {
        if (st === 'cancelled' && !confirm('إلغاء طلب ' + b.name + '؟')) return;
        x.disabled = true;
        update(b.ref, st, note.value);
      });
      return x;
    }
  }

  function update(ref, st, note, more) {
    var data = { note: note }; if (st) data.status = st;
    if (more) { for (var k in more) data[k] = more[k]; }
    api('bookings/' + ref, data).then(function (j) {
      if (j.ok) { if (st === 'confirmed' && tab === 'new') { toast('تم تأكيد الحجز. أرسل الآن رسالة التأكيد للزبون.'); return go('active'); }
        toast(st === 'confirmed' ? 'تم تأكيد الحجز' : st === 'cancelled' ? 'تم الإلغاء' : st === 'new' ? 'أُعيد إلى الطلبات الجديدة' : 'تم الحفظ'); loadList(); }
      else if (!gone(j) && j.status) { toast('لم يتم الحفظ'); loadList(); }
    });
  }

  /* ---------------------------------------------------------------- rooms, closures, inventory */
  function roomName(id) { for (var i = 0; i < ROOMS.length; i++) if (ROOMS[i].id === id) return ROOMS[i].name; return 'كل الغرف'; }

  function rooms() {
    return api('inventory').then(function (j) {
      ROOMS = j.rooms || ROOMS;
      each('#closure-room, .room-select', function (sel) {
        if (sel.options.length === 1) ROOMS.forEach(function (r) { var o = el('option', null, r.name); o.value = r.id; sel.appendChild(o); });
      });
      return j;
    });
  }

  function loadClosures() {
    rooms().then(function () { return api('closures'); }).then(function (j) {
      if (gone(j)) return;
      var box = $('#closures'); box.textContent = '';
      if (!j.items || !j.items.length) { box.appendChild(el('p', 'empty', 'لا توجد تواريخ مغلقة.')); return; }
      j.items.forEach(function (x) {
        var c = el('div', 'card closure');
        c.appendChild(el('strong', null, fmt(x.date_from) + ' ← ' + fmt(x.date_to)));
        c.appendChild(el('span', 'muted', roomName(x.room) + (x.note ? ' · ' + x.note : '')));
        var del = el('button', 'ghost danger', 'فتح التواريخ'); del.type = 'button';
        del.addEventListener('click', function () { api('closures/' + x.id + '/delete', {}).then(function () { toast('فُتحت التواريخ'); loadClosures(); }); });
        c.appendChild(del); box.appendChild(c);
      });
    });
  }
  $('#closure-form').addEventListener('submit', function (e) {
    e.preventDefault(); var f = e.target;
    if (f.to.value < f.from.value) { toast('تاريخ النهاية قبل تاريخ البداية'); return; }
    api('closures', { from: f.from.value, to: f.to.value, room: f.room.value, note: f.note.value }).then(function (j) {
      if (j.ok) { f.reset(); toast('تم الإغلاق'); loadClosures(); } else if (j.status) toast('تحقّق من التواريخ');
    });
  });

  function invSum() {
    var s = 0; each('#inv-rows input', function (i) { s += +i.value || 0; }); $('#inv-sum').textContent = s;
  }
  function loadInventory() {
    rooms().then(function (j) {
      if (gone(j)) return;
      var box = $('#inv-rows'); box.textContent = '';
      ROOMS.forEach(function (r) {
        var l = el('label', 'inv', r.name); var i = el('input'); i.type = 'number'; i.min = 0; i.max = 500; i.name = r.id; i.inputMode = 'numeric';
        i.value = r.count == null ? '' : r.count; i.placeholder = 'غير محدد'; i.addEventListener('input', invSum); l.appendChild(i); box.appendChild(l);
      });
      invSum();
    });
  }
  $('#inv-form').addEventListener('submit', function (e) {
    e.preventDefault(); var data = {};
    Array.prototype.forEach.call(e.target.querySelectorAll('input'), function (i) { data[i.name] = i.value === '' ? null : +i.value; });
    api('inventory', data).then(function (j) { if (j.ok) { toast('تم حفظ عدد الغرف'); loadInventory(); } else if (j.status) toast('لم يتم الحفظ'); });
  });

  /* ---------------------------------------------------------------- occupancy */
  var occFrom = new Date();
  function loadOcc() {
    api('occupancy?from=' + iso(occFrom) + '&days=31').then(function (j) {
      if (gone(j) || !j.dates) return;
      var today = iso(new Date()), t = $('#occ'); t.textContent = '';
      var sums = j.dates.map(function () { return 0; }), cap = 0, capKnown = true;
      var hr = el('tr'); hr.appendChild(el('th', 'sticky', 'الغرفة'));
      j.dates.forEach(function (ds) {
        var p = ds.split('-'), dd = day(ds);
        var th = el('th', (dd.getDay() === 5 || dd.getDay() === 6 ? 'we' : '') + (ds === today ? ' today' : ''));
        th.appendChild(el('span', 'dn', DAYS[dd.getDay()])); th.appendChild(el('span', null, p[2] + '/' + p[1])); hr.appendChild(th);
      });
      t.appendChild(hr);
      j.rows.forEach(function (r) {
        if (r.count == null) capKnown = false; else cap += r.count;
        var tr = el('tr'); tr.appendChild(el('th', 'sticky', r.name + (r.count != null ? ' (' + r.count + ')' : '')));
        r.cells.forEach(function (c, i) {
          var td = el('td'); var conf = c[0], nw = c[1], closed = c[2]; sums[i] += conf;
          td.textContent = conf ? (r.count != null ? conf + '/' + r.count : conf) : (r.count != null ? '0/' + r.count : '·');
          var cls = closed ? 'closed' : (r.count != null && conf >= r.count) ? 'full' : conf ? 'some' : '';
          td.className = cls + (j.dates[i] === today ? ' today' : '');
          if (nw) { td.appendChild(el('sup', 'nw', '+' + nw)); }
          tr.appendChild(td);
        });
        t.appendChild(tr);
      });
      var tr = el('tr'); tr.appendChild(el('th', 'sticky', 'بدون نوع محدد'));
      j.any.forEach(function (c, i) { sums[i] += c[0]; var td = el('td', (c[0] ? 'some' : '') + (j.dates[i] === today ? ' today' : ''), c[0] ? String(c[0]) : '·'); if (c[1]) td.appendChild(el('sup', 'nw', '+' + c[1])); tr.appendChild(td); });
      t.appendChild(tr);
      var sr = el('tr', 'sum'); sr.appendChild(el('th', 'sticky', 'المجموع' + (capKnown && cap ? ' (' + cap + ')' : '')));
      sums.forEach(function (s, i) { sr.appendChild(el('td', j.dates[i] === today ? 'today' : null, capKnown && cap ? s + '/' + cap : String(s))); });
      t.appendChild(sr);
    });
  }
  $('#occ-prev').addEventListener('click', function () { occFrom.setDate(occFrom.getDate() - 30); loadOcc(); });
  $('#occ-next').addEventListener('click', function () { occFrom.setDate(occFrom.getDate() + 30); loadOcc(); });
  $('#occ-today').addEventListener('click', function () { occFrom = new Date(); loadOcc(); });

  /* ---------------------------------------------------------------- desk booking */
  $('#add-form').addEventListener('submit', function (e) {
    e.preventDefault(); var f = e.target;
    if (f.out.value <= f['in'].value) { toast('تاريخ المغادرة يجب أن يكون بعد تاريخ الوصول'); return; }
    api('bookings/new', { name: f.name.value, phone: f.phone.value, 'in': f['in'].value, out: f.out.value, room: f.room.value,
      guests: f.guests.value, total: f.total.value, note: f.note.value, breakfast: f.breakfast.checked }).then(function (j) {
      if (j.ok) { f.reset(); toast('تم حفظ الحجز ' + j.ref); go('active'); } else if (!gone(j) && j.status) toast('تحقّق من الاسم والتواريخ');
    });
  });

  /* ---------------------------------------------------------------- prices */
  function loadPrices() {
    rooms().then(function () { return api('prices'); }).then(function (j) {
      if (gone(j) || !j.rooms) return;
      var tb = $('#price-rows'); tb.textContent = '';
      j.rooms.forEach(function (r) {
        var tr = el('tr'); tr.appendChild(el('td', null, r.name));
        ['price', 'price2'].forEach(function (k) { var td = el('td'), i = el('input'); i.type = 'number'; i.min = 1; i.name = r.id + '.' + k; i.inputMode = 'numeric'; i.value = r[k] == null ? '' : r[k]; i.placeholder = k === 'price' ? 'عند الطلب' : '-'; i.setAttribute('aria-label', r.name + (k === 'price' ? ' السعر' : ' لشخصين+')); td.appendChild(i); tr.appendChild(td); });
        tb.appendChild(tr);
      });
      return api('rates');
    }).then(function (j) {
      if (!j) return;
      var box = $('#rates'); box.textContent = '';
      if (!j.items || !j.items.length) { box.appendChild(el('p', 'empty', 'لا توجد أسعار خاصة.')); return; }
      j.items.forEach(function (x) {
        var c = el('div', 'card closure');
        c.appendChild(el('strong', null, fmt(x.date_from) + ' ← ' + fmt(x.date_to) + ' · $' + x.price + (x.price2 ? ' / $' + x.price2 : '')));
        var days = x.days ? ' · ' + x.days.split(',').map(function (d) { return DAYS[(+d + 1) % 7]; }).join('، ') : '';
        c.appendChild(el('span', 'muted', roomName(x.room) + days + (x.note ? ' · ' + x.note : '')));
        var del = el('button', 'ghost danger', 'حذف'); del.type = 'button';
        del.addEventListener('click', function () { if (!confirm('حذف هذا السعر الخاص؟')) return; api('rates/' + x.id + '/delete', {}).then(function () { toast('تم الحذف'); loadPrices(); }); });
        c.appendChild(del); box.appendChild(c);
      });
    });
  }
  $('#price-form').addEventListener('submit', function (e) {
    e.preventDefault(); var data = {};
    Array.prototype.forEach.call(e.target.querySelectorAll('input'), function (i) {
      var p = i.name.split('.'); data[p[0]] = data[p[0]] || {}; data[p[0]][p[1]] = i.value === '' ? null : +i.value;
    });
    api('prices', data).then(function (j) { if (j.status) toast(j.ok ? 'تم حفظ الأسعار. الموقع يعرضها الآن.' : 'تحقّق من الأرقام'); });
  });
  $('#rate-form').addEventListener('submit', function (e) {
    e.preventDefault(); var f = e.target;
    if (f.to.value < f.from.value) { toast('تاريخ النهاية قبل تاريخ البداية'); return; }
    var days = Array.prototype.filter.call(f.querySelectorAll('[name=d]'), function (c) { return c.checked; }).map(function (c) { return +c.value; });
    api('rates', { from: f.from.value, to: f.to.value, room: f.room.value, price: f.price.value, price2: f.price2.value, note: f.note.value, days: days }).then(function (j) {
      if (j.ok) { f.reset(); toast('تمت الإضافة'); loadPrices(); } else if (j.status) toast('تحقّق من التواريخ والسعر');
    });
  });

  /* ---------------------------------------------------------------- offers */
  function offerPreview() {
    var f = $('#offer-form'), p = +f.percent.value, box = $('#offer-preview'); box.textContent = '';
    if (!p || p < 1 || p > 70) return;
    box.appendChild(el('strong', null, 'خصم ' + p + '%'));
    var w = el('div');
    w.appendChild(el('b', null, f.title_ar.value.trim() || 'عرض الحجز المباشر'));
    if (f.from.value && f.to.value) w.appendChild(el('span', null, 'للإقامات من ' + fmt(f.from.value).slice(0, 5) + ' إلى ' + fmt(f.to.value).slice(0, 5) + (+f.min_nights.value > 1 ? ' · ' + f.min_nights.value + ' ليالٍ على الأقل' : '')));
    w.appendChild(el('small', null, 'مثال: غرفة بـ $100 تصبح $' + Math.round(100 - p)));
    box.appendChild(w);
  }
  $('#offer-form').addEventListener('input', offerPreview);
  function loadOffers() {
    api('offers').then(function (j) {
      if (gone(j) || !j.items) return;
      var box = $('#offers'); box.textContent = '';
      if (!j.items.length) { box.appendChild(el('p', 'empty', 'لا توجد عروض. الموقع يعرض الأسعار العادية.')); return; }
      j.items.forEach(function (x) {
        var live = x.date_to >= j.today, c = el('div', 'card closure' + (live ? '' : ' past'));
        c.appendChild(el('strong', 'offer-pct', 'خصم ' + x.percent + '%'));
        c.appendChild(el('span', null, (x.title_ar || 'عرض الحجز المباشر') + ' · ' + fmt(x.date_from) + ' ← ' + fmt(x.date_to) + (x.min_nights > 1 ? ' · ' + x.min_nights + ' ليالٍ على الأقل' : '')));
        c.appendChild(el('span', 'badge', live ? (x.date_from <= j.today ? 'ظاهر على الموقع' : 'ظاهر على الموقع (يبدأ لاحقاً)') : 'انتهى'));
        var del = el('button', 'ghost danger', live ? 'إيقاف العرض' : 'حذف'); del.type = 'button';
        del.addEventListener('click', function () { if (!confirm('إيقاف هذا العرض؟ سيختفي من الموقع فوراً.')) return; api('offers/' + x.id + '/delete', {}).then(function () { toast('تم إيقاف العرض'); loadOffers(); }); });
        c.appendChild(del); box.appendChild(c);
      });
    });
  }
  $('#offer-form').addEventListener('submit', function (e) {
    e.preventDefault(); var f = e.target;
    if (f.to.value < f.from.value) { toast('تاريخ النهاية قبل تاريخ البداية'); return; }
    api('offers', { percent: f.percent.value, min_nights: f.min_nights.value, from: f.from.value, to: f.to.value, title_ar: f.title_ar.value, title_en: f.title_en.value }).then(function (j) {
      if (j.ok) { f.reset(); offerPreview(); toast('تم نشر العرض على الموقع'); loadOffers(); } else if (!gone(j) && j.status) toast('تحقّق من النسبة (1 إلى 70) والتواريخ');
    });
  });

  /* refresh quietly every minute; never while someone is typing a note */
  setInterval(function () {
    if ($('#app').hidden || document.hidden) return;
    var a = document.activeElement, typing = a && (a.tagName === 'INPUT' || a.tagName === 'SELECT');
    if (tab === 'home') loadHome();
    else if ((tab === 'new' || tab === 'active') && !typing) loadList();
    else api('bookings?status=new').then(function (j) { if (j.counts) badge(j.counts['new'] || 0); });
  }, 60000);
  start();
})();
