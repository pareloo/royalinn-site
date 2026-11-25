/* Royal Inn - booking panel. Talks only to /api/admin on this site. */
(function () {
  'use strict';
  var $ = function (s) { return document.querySelector(s); };
  var each = function (sel, fn) { Array.prototype.forEach.call(document.querySelectorAll(sel), fn); };
  var tab = 'home', ROOMS = [], ITEMS = [], AGS = [], RULES = { checkout: '12:00', checkin: '14:00' };
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
    ['home', 'closures', 'inventory', 'occupancy', 'add', 'prices', 'roominfo', 'offers', 'agencies', 'rack', 'rules', 'inq'].forEach(function (v) { $('#view-' + v).hidden = tab !== v; });
    if (list) loadList(); else if (tab === 'home') loadHome(); else if (tab === 'closures') loadClosures(); else if (tab === 'inventory') loadInventory();
    else if (tab === 'occupancy') loadOcc(); else if (tab === 'add') { rooms(); agencies(); } else if (tab === 'agencies') loadAgencies(); else if (tab === 'rack') { rooms(); loadRack(); } else if (tab === 'rules') loadRules(); else if (tab === 'prices') loadPrices(); else if (tab === 'offers') loadOffers(); else if (tab === 'roominfo') loadRoomInfo(); else if (tab === 'inq') loadInq();
    inqBadge();
  }

  // the count of waiting requests shows on the menu and in the browser tab title, on every screen
  function badge(n) { var b = $('#c-new'); b.textContent = n; b.hidden = !n; document.title = (n ? '(' + n + ') ' : '') + 'لوحة الحجوزات | Royal Inn'; }


  /* ---------------------------------------------------------------- business requests from the website */
  var INQ_ST = 'open';
  var INQ_LABEL = { 'new': 'جديد', contacted: 'تم التواصل', won: 'تم الاتفاق', lost: 'لم يتم' };
  function inqBadge() {
    api('inquiries?status=new').then(function (j) { if (!j.ok) return; var b = $('#c-inq'); var n = j.counts['new'] || 0; b.textContent = n; b.hidden = !n; });
  }
  each('#inq-filter .chip', function (c) {
    c.addEventListener('click', function () { INQ_ST = c.getAttribute('data-st'); each('#inq-filter .chip', function (x) { x.classList.toggle('on', x === c); }); loadInq(); });
  });
  function loadInq() {
    api('inquiries?status=' + INQ_ST).then(function (j) {
      if (gone(j)) return;
      var box = $('#inq-list'); box.textContent = '';
      if (!j.items || !j.items.length) { box.appendChild(el('p', 'empty', 'لا توجد طلبات هنا بعد.')); return; }
      j.items.forEach(function (r) {
        var c = el('div', 'card');
        var h = el('div', 'row-head');
        h.appendChild(el('strong', null, '#' + r.id + ' · ' + r.kind_label));
        h.appendChild(el('span', 'pill st-' + r.status, INQ_LABEL[r.status] || r.status));
        c.appendChild(h);
        var info = [r.name, r.company, r.phone, r.email, r.date ? 'التاريخ: ' + fmt(r.date) : '', r.guests ? 'الأشخاص: ' + r.guests : '',
                    r.rooms ? 'الغرف: ' + r.rooms : '', r.nights ? 'الليالي: ' + r.nights : '', 'وصل: ' + (r.created || '').slice(0, 16).replace('T', ' ')];
        c.appendChild(el('p', 'muted', info.filter(Boolean).join(' · ')));
        if (r.message) c.appendChild(el('p', null, r.message));
        var act = el('div', 'actions');
        var hello = r.lang === 'ar' ? 'مرحباً ' + r.name + '، معكم فندق رويال إن طرطوس بخصوص طلبكم على موقعنا.' : 'Hello ' + r.name + ', this is Royal Inn Tartus regarding your request on our website.';
        var wa = el('a', 'btn wa', 'رد على واتساب'); wa.href = 'https://wa.me/' + digits(r.phone) + '?text=' + encodeURIComponent(hello); wa.target = '_blank'; wa.rel = 'noopener'; act.appendChild(wa);
        var tel = el('a', 'btn ghost', 'اتصال'); tel.href = 'tel:' + r.phone.replace(/[^0-9+]/g, ''); act.appendChild(tel);
        [['contacted', 'تم التواصل'], ['won', 'تم الاتفاق'], ['lost', 'لم يتم']].forEach(function (x) {
          if (r.status === x[0]) return;
          var b = el('button', 'ghost', x[1]); b.type = 'button';
          b.addEventListener('click', function () { api('inquiries/' + r.id, { status: x[0] }).then(function (k) { if (!gone(k)) { toast('تم الحفظ.'); loadInq(); inqBadge(); } }); });
          act.appendChild(b);
        });
        c.appendChild(act);
        var nf = el('form', 'note-form');
        var ni = el('input'); ni.type = 'text'; ni.maxLength = 600; ni.placeholder = 'ملاحظة داخلية (السعر المعروض، موعد الاتصال…)'; ni.value = r.staff_note || '';
        var ns = el('button', 'ghost', 'حفظ الملاحظة'); ns.type = 'submit';
        nf.appendChild(ni); nf.appendChild(ns);
        nf.addEventListener('submit', function (e) { e.preventDefault(); api('inquiries/' + r.id, { staff_note: ni.value }).then(function (k) { if (!gone(k)) toast('تم حفظ الملاحظة.'); }); });
        c.appendChild(nf);
        box.appendChild(c);
      });
    });
  }

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
      var sub = what === 'new' ? fmt(b.check_in) + ' ← ' + fmt(b.check_out) + ' · ' + b.room_name : b.room_name + ' · ' + b.nights + ' ليالٍ · ' + b.guests + ' ضيوف' + (b.rooms > 1 ? ' · ' + b.rooms + ' غرف' : '');
      if (b.status === 'new') sub += ' · لم يُؤكَّد بعد'; if (b.room_no) sub += ((b.room_nos || []).length > 1 ? ' · غرف ' : ' · غرفة ') + b.room_no; if (b.agency_name) sub += ' · ' + b.agency_name;
      r.appendChild(el('span', 'm-sub', sub));
      if (b.phone) { var a = el('a', 'btn ghost', 'اتصال'); a.href = 'tel:' + b.phone.replace(/[^0-9+]/g, ''); r.appendChild(a); }
      box.appendChild(r);
    });
  }
  function waLink(b, text, label, cls) { var a = el('a', 'btn ' + (cls || 'wa'), label); a.target = '_blank'; a.rel = 'noopener'; a.href = 'https://wa.me/' + digits(b.phone) + '?text=' + encodeURIComponent(text); return a; }
  function lateRule(en) {
    if (RULES.late_on !== '1') return '';
    return en ? ' Late check-out is subject to availability: ' + RULES.late_half_pct + '% of the nightly rate until ' + RULES.late_half_until + ', a full extra night after that.'
      : ' المغادرة المتأخرة حسب التوفر: ' + RULES.late_half_pct + '% من سعر الليلة حتى الساعة ' + RULES.late_half_until + '، وبعدها ليلة إضافية كاملة.';
  }
  function act(ref, path, data, msg) { return api('bookings/' + ref + (path || ''), data).then(function (j) { if (j.ok) { toast(msg); loadHome(); } else if (!gone(j)) toast(j.error === 'rate' ? 'لا يوجد سعر لهذه الغرفة. اكتب المبلغ يدوياً من بطاقة الحجز.' : 'لم يتم الحفظ'); return j; }); }
  var lastSoon = '';
  function deskAlerts(j) {
    var box = $('#home-desk'); box.textContent = '';
    function block(cls, title, items, each2) {
      if (!items.length) return;
      var c = el('div', 'card desk-alert ' + cls); c.appendChild(el('h2', null, title + ' (' + items.length + ')'));
      items.forEach(function (b) {
        var r = el('div', 'mini'); r.appendChild(el('b', null, b.name + (b.room_no ? ((b.room_nos || []).length > 1 ? ' · غرف ' : ' · غرفة ') + b.room_no : '')));
        var sub = el('span', 'm-sub'); r.appendChild(sub); each2(b, r, sub); box.appendChild(c); c.appendChild(r);
      });
    }
    block('soon', 'مغادرة قريبة: الموعد الساعة ' + RULES.checkout, j.due_soon, function (b, r, sub) {
      sub.textContent = (b.minutes_left > 0 ? 'باقٍ ' + b.minutes_left + ' دقيقة' : 'انتهى الموعد، ضمن المهلة') + ' · ' + b.room_name + (b.reminded ? ' · تم تذكير الضيف' : '');
      if (b.phone && b.phone !== '-') {
        var t = b.lang === 'en' ? 'Hello ' + b.name + ', a kind reminder from Royal Inn Tartus: check-out is today at ' + RULES.checkout + '. If you would like to stay longer, please let reception know now.' + lateRule(true)
          : 'مرحباً ' + b.name + '، تذكير لطيف من فندق رويال إن طرطوس: موعد المغادرة اليوم الساعة ' + RULES.checkout + '. إذا رغبتم بتمديد الإقامة أخبروا الاستقبال الآن.' + lateRule(false);
        var w = waLink(b, t, b.reminded ? 'تذكير مرة أخرى' : 'ذكّر الضيف (واتساب)'); w.addEventListener('click', function () { api('bookings/' + b.ref, { reminded: true, note: b.staff_note || '' }); }); r.appendChild(w);
      }
      var d = el('button', 'ghost btn', 'تسجيل المغادرة'); d.type = 'button'; d.addEventListener('click', function () { act(b.ref, '', { departed: true, note: b.staff_note || '' }, 'تم تسجيل المغادرة'); }); r.appendChild(d);
    });
    block('late', 'تأخّروا عن المغادرة', j.late, function (b, r, sub) {
      sub.textContent = (b.late_minutes != null ? 'متأخر ' + Math.floor(b.late_minutes / 60) + ' س ' + (b.late_minutes % 60) + ' د' : 'موعد المغادرة كان ' + fmt(b.check_out)) + (b.late_fee ? ' · رسم مسجَّل $' + b.late_fee : '');
      if (RULES.late_on === '1' && b.fee != null && b.stage === 'half') { var h = el('button', 'ghost btn', 'رسم نصف يوم $' + b.fee); h.type = 'button'; h.addEventListener('click', function () { if (confirm('إضافة رسم مغادرة متأخرة $' + b.fee + ' إلى حساب ' + b.name + '؟')) act(b.ref, '/late', { kind: 'half' }, 'أُضيف الرسم'); }); r.appendChild(h); }
      var nx = new Date(); nx.setDate(nx.getDate() + 1);
      var n1 = el('button', 'gold btn', 'ليلة إضافية (+1)'); n1.type = 'button';
      n1.addEventListener('click', function () {
        api('bookings/' + b.ref + '/stay', { out: iso(nx), preview: true }).then(function (p) {
          if (!p.ok) { toast('لم يتم الحفظ'); return; }
          var q = 'تمديد إقامة ' + b.name + ' حتى ' + fmt(iso(nx)) + (p.amount != null ? ' · الفرق +$' + p.amount : '') + (p.available ? '' : '\nتنبيه: الغرف من هذا النوع مكتملة الليلة.') + '\nمتابعة؟';
          if (confirm(q)) act(b.ref, '/stay', { out: iso(nx), force: true }, 'أُضيفت ليلة');
        });
      });
      r.appendChild(n1);
      if (b.phone && b.phone !== '-') r.appendChild(waLink(b, b.lang === 'en' ? 'Hello ' + b.name + ', this is Royal Inn Tartus reception. Check-out time (' + RULES.checkout + ') has passed. Would you like to extend your stay?' + lateRule(true)
        : 'مرحباً ' + b.name + '، معكم استقبال فندق رويال إن طرطوس. انتهى موعد المغادرة (' + RULES.checkout + '). هل ترغبون بتمديد الإقامة؟' + lateRule(false), 'واتساب'));
      var d = el('button', 'ghost btn', 'تسجيل المغادرة'); d.type = 'button'; d.addEventListener('click', function () { act(b.ref, '', { departed: true, note: b.staff_note || '' }, 'تم تسجيل المغادرة'); }); r.appendChild(d);
    });
    block('noshow', 'لم يُسجَّل وصولهم بعد', j.no_show, function (b, r, sub) {
      sub.textContent = 'كان الوصول ' + fmt(b.check_in) + ' · ' + b.room_name;
      var a1 = el('button', 'gold btn', 'وصل الضيف'); a1.type = 'button'; a1.addEventListener('click', function () { act(b.ref, '', { arrived: true, note: b.staff_note || '' }, 'تم تسجيل الوصول'); }); r.appendChild(a1);
      if (b.phone && b.phone !== '-') { var t = el('a', 'btn ghost', 'اتصال'); t.href = 'tel:' + b.phone.replace(/[^0-9+]/g, ''); r.appendChild(t); }
      var ns = el('button', 'ghost danger btn', 'لم يحضر (إلغاء)'); ns.type = 'button'; ns.addEventListener('click', function () { if (confirm('تسجيل عدم حضور ' + b.name + ' وإلغاء الحجز؟ الغرفة تعود متاحة.')) act(b.ref, '', { no_show: true }, 'سُجِّل عدم الحضور'); }); r.appendChild(ns);
    });
    // a browser notification the first time a guest enters the reminder window (only if the manager allowed notifications)
    var key = j.due_soon.map(function (b) { return b.ref; }).join(',');
    if (key && key !== lastSoon && window.Notification && Notification.permission === 'granted') { try { new Notification('Royal Inn: مغادرة قريبة', { body: j.due_soon.map(function (b) { return b.name + (b.room_no ? ' (' + b.room_no + ')' : ''); }).join('، ') }); } catch (e) {} }
    lastSoon = key;
    if (window.Notification && Notification.permission === 'default') {
      var nb = el('button', 'ghost sm', 'تفعيل التنبيهات على هذا الجهاز'); nb.type = 'button'; nb.addEventListener('click', function () { Notification.requestPermission().then(function () { loadHome(); }); });
      var w2 = el('p', 'muted small'); w2.appendChild(nb); box.appendChild(w2);
    }
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
      RULES = j.rules || RULES; deskAlerts(j);
      var fr = $('#home-free'); fr.textContent = '';
      $('#free-hint').textContent = j.units_free.length ? 'غرف غير مشغولة وغير محجوزة اليوم. الرمادي يحتاج تنظيفاً أولاً.' : 'تظهر هنا أرقام الغرف المتاحة بعد إضافة أرقام الغرف من «الغرف».';
      j.units_free.forEach(function (u) { fr.appendChild(el('span', 'chip' + (u.state === 'dirty' ? ' dirty' : ''), u.no + ' · ' + roomName(u.room) + (u.view ? ' · ' + VIEWS[u.view] : ''))); });
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
  var agLoaded = false;
  function loadList() {
    (RULES.deposit_info !== undefined ? Promise.resolve() : api('settings').then(function (j) { if (j && j.settings) RULES = j.settings; })).then(function () { return agLoaded ? null : agencies(); }).then(function () { return ROOMS.length ? null : rooms(); }).then(function () { return api('bookings?status=' + tab); }).then(function (j) {
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
    if (b.room_no) h.appendChild(el('span', 'badge rno', ((b.room_nos || []).length > 1 ? 'غرف ' : 'غرفة ') + b.room_no));
    h.appendChild(el('span', 'ref', b.ref));
    c.appendChild(h);
    var body = el('div', 'bk-body');

    var stay = el('div', 'stay');
    [['الوصول', b.check_in], ['المغادرة', b.check_out]].forEach(function (x) {
      var d = el('div', 'stay-d'); d.appendChild(el('span', null, x[0] + ' · ' + DAYS_LONG[day(x[1]).getDay()])); d.appendChild(el('b', null, fmt(x[1]))); stay.appendChild(d);
    });
    stay.appendChild(el('span', 'stay-n', b.nights + ' ليالٍ · ' + b.guests + ' ضيوف' + (b.rooms > 1 ? ' · ' + b.rooms + ' غرف' : '')));
    var tot = el('div', 'stay-t'); tot.appendChild(el('span', null, 'المبلغ الإجمالي')); tot.appendChild(el('b', null, b.total ? '$' + b.total : 'يُحدَّد'));
    if (b.paid != null && b.total) tot.appendChild(el('span', b.total - b.paid > 0 ? 'due' : 'paidok', b.total - b.paid > 0 ? 'مدفوع $' + b.paid + ' · الباقي $' + (b.total - b.paid) : 'مدفوع بالكامل'));
    stay.appendChild(tot);
    body.appendChild(stay);

    var dl = el('dl', 'facts');
    [['الغرفة', b.room_name], ['الفطور', b.breakfast ? 'نعم' : 'لا'], ['المصدر', b.source_name + (b.agency_name ? ': ' + b.agency_name + (b.pay_by === 'agency' ? ' (تدفع الوكالة' + (b.net != null ? ' $' + b.net : '') + ')' : ' (يدفع الضيف)') : '') + (b.referrer ? ': ' + b.referrer : '')],
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
    var roomNo = null, paid = null, ext = null, agSel = null, paySel = null;
    if (b.status === 'confirmed') {
      var desk = el('div', 'desk');
      // one room number per room: a family suite gets its suite and its connecting room, a mixed booking one box per room
      var SL = b.slots && b.slots.length ? b.slots : [b.room || null], have = (b.room_nos || []).slice(), boxes = [];
      var rn = el('div', 'rnos'); rn.appendChild(el('span', 'rnos-h', SL.length > 1 ? 'أرقام الغرف (' + SL.length + ')' : 'رقم الغرفة'));
      var hint = el('small', 'muted'), FREE = null;
      SL.forEach(function (k, n) {
        var lb = el('label', 'rno-l'), tag = k === 'royal-connect' ? 'الغرفة المتصلة' : (k ? roomName(k) : 'غرفة');
        var same = SL.filter(function (x) { return x === k; }).length > 1 ? ' ' + (SL.slice(0, n + 1).filter(function (x) { return x === k; }).length) : '';
        lb.appendChild(el('span', 'rno-t', tag + same));
        var inp = el('input'); inp.type = 'text'; inp.maxLength = 12; inp.dir = 'ltr'; inp.placeholder = 'اختر من المتاح';
        inp.value = have[n] || ''; inp.setAttribute('aria-label', 'رقم الغرفة: ' + tag + same);
        var dl = el('datalist'); dl.id = 'free-' + b.ref + '-' + n; inp.setAttribute('list', dl.id);
        inp.addEventListener('focus', function () {
          var fill = function () {
            dl.textContent = '';
            var taken = boxes.map(function (x) { return x !== inp && x.value.trim(); });
            var list = FREE.filter(function (u) { return (!k || u.room === k) && taken.indexOf(u.no) === -1; });
            (list.length ? list : FREE).forEach(function (u) { var o = el('option'); o.value = u.no; o.label = roomName(u.room) + (u.view ? ' · ' + VIEWS[u.view] : '') + (u.state === 'dirty' ? ' · تحتاج تنظيف' : ''); dl.appendChild(o); });
            hint.textContent = list.length ? 'المتاح (' + tag + '): ' + list.map(function (u) { return u.no + (u.view ? ' (' + VIEWS[u.view] + ')' : ''); }).slice(0, 12).join('، ') : 'لا غرف متاحة من هذا النوع في هذه التواريخ';
          };
          if (FREE) return fill();
          api('units/free?in=' + b.check_in + '&out=' + b.check_out + '&ref=' + b.ref).then(function (j) { if (!j.ok || !j.defined) return; FREE = j.free; fill(); });
        });
        lb.appendChild(inp); lb.appendChild(dl); rn.appendChild(lb); boxes.push(inp);
      });
      rn.appendChild(hint); desk.appendChild(rn);
      roomNo = { get value() { return boxes.map(function (x) { return x.value.trim(); }).filter(Boolean); } };
      var l2 = el('label', null, 'المدفوع $'); paid = el('input'); paid.type = 'number'; paid.min = 0; paid.inputMode = 'numeric'; paid.value = b.paid == null ? '' : b.paid; l2.appendChild(paid); desk.appendChild(l2);
      var l4 = el('label', null, 'الوكالة'); agSel = el('select'); var o0 = el('option', null, 'بدون وكالة'); o0.value = ''; agSel.appendChild(o0);
      AGS.forEach(function (a) { var o = el('option', null, a.name); o.value = a.id; agSel.appendChild(o); }); agSel.value = b.agency_id || ''; l4.appendChild(agSel); desk.appendChild(l4);
      var l5 = el('label', null, 'من يدفع؟'); paySel = el('select'); [['guest', 'الضيف'], ['agency', 'الوكالة']].forEach(function (x) { var o = el('option', null, x[1]); o.value = x[0]; paySel.appendChild(o); }); paySel.value = b.pay_by === 'agency' ? 'agency' : 'guest'; l5.appendChild(paySel); desk.appendChild(l5);
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
        ? 'Hello ' + b.name + ', this is Royal Inn Tartus about your booking request ' + b.ref + ': ' + b.check_in + ' to ' + b.check_out + ', ' + b.guests + ' guests' + (b.rooms > 1 ? ' in ' + b.rooms + ' rooms' : '') + (b.total ? ', total $' + b.total : '') + '.'
        : 'مرحباً ' + b.name + '، معك فندق رويال إن طرطوس بخصوص طلب الحجز ' + b.ref + ': من ' + fmt(b.check_in) + ' إلى ' + fmt(b.check_out) + '، ' + b.guests + ' ضيوف' + (b.rooms > 1 ? ' في ' + b.rooms + ' غرف' : '') + room + (b.total ? '، المبلغ الإجمالي ' + b.total + '$' : '') + '.';
      wa.href = 'https://wa.me/' + digits(b.phone) + '?text=' + encodeURIComponent(text);
      act.appendChild(wa);
      var tel = el('a', 'btn ghost', 'اتصال'); tel.href = 'tel:' + b.phone.replace(/[^0-9+]/g, ''); act.appendChild(tel);
      if (b.status === 'new') {
        // ask the guest for the deposit that secures the booking (amount typed by the staff, transfer text from the settings)
        var dep = el('div', 'deposit'), dl = el('label', null, 'مبلغ الدفعة $'), di = el('input'); di.type = 'number'; di.min = 1; di.dir = 'ltr'; di.inputMode = 'numeric'; dl.appendChild(di); dep.appendChild(dl);
        var da = el('a', 'btn gold', 'اطلب الدفعة (واتساب)'); da.target = '_blank'; da.rel = 'noopener';
        var depText = function () {
          var info = (RULES && RULES.deposit_info) || '';
          var amt = di.value ? di.value + '$' : '';
          return b.lang === 'en'
            ? 'Hello ' + b.name + ', thank you for your booking request ' + b.ref + ' at Royal Inn Tartus (' + b.check_in + ' to ' + b.check_out + (b.total ? ', total $' + b.total : '') + '). To secure the booking please send a deposit' + (amt ? ' of ' + amt : '') + ' by money transfer inside Syria. ' + info + ' Your booking is confirmed once we receive it.'
            : 'مرحباً ' + b.name + '، شكراً لطلب الحجز ' + b.ref + ' في فندق رويال إن طرطوس (من ' + fmt(b.check_in) + ' إلى ' + fmt(b.check_out) + room + (b.total ? '، المبلغ الإجمالي ' + b.total + '$' : '') + '). لتثبيت الحجز يُرجى إرسال دفعة مقدّمة' + (amt ? ' بقيمة ' + amt : '') + '. ' + info + ' يتم تأكيد الحجز فور وصول الدفعة.';
        };
        var upd = function () { da.href = 'https://wa.me/' + digits(b.phone) + '?text=' + encodeURIComponent(depText()); };
        di.addEventListener('input', upd); upd(); dep.appendChild(da);
        body.appendChild(dep);
      }
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
      if (b.email) { var mb = el('button', 'ghost', 'أرسل التأكيد بالبريد'); mb.type = 'button'; mb.addEventListener('click', function () { mb.disabled = true; api('bookings/' + b.ref + '/mail', {}).then(function (j) { mb.disabled = false; if (gone(j)) return; toast(j.ok ? 'أُرسل البريد إلى ' + b.email : j.error === 'mail_off' ? 'البريد غير مفعَّل بعد' : 'فشل إرسال البريد'); }); }); act.appendChild(mb); }
    }
    if (b.status !== 'confirmed') act.appendChild(button('تأكيد الحجز', 'gold', 'confirmed'));
    if (b.status !== 'cancelled') act.appendChild(button('إلغاء', 'ghost danger', 'cancelled'));
    if (b.status !== 'new') act.appendChild(button('إرجاع إلى جديد', 'ghost', 'new'));
    if (b.status === 'confirmed') {
      var today = iso(new Date());
      if (!b.arrived && b.check_in <= today) act.appendChild(flag('تسجيل الوصول', 'gold', { arrived: true }, 'تم تسجيل وصول الضيف'));
      if (b.arrived && !b.departed) act.appendChild(flag('تسجيل المغادرة', 'ghost', { departed: true }, 'تم تسجيل المغادرة. اطلب الآن تقييماً من الضيف من بطاقة الحجز.'));
      if (b.departed && b.phone && b.phone !== '-') {
        // after check-out: thank the guest and ask for a Google review (one tap, the staff press Send)
        var rv = 'https://search.google.com/local/writereview?placeid=ChIJGYQQvXl-IRURS1x8zFqxc1I';
        var ta = 'https://www.tripadvisor.com/UserReviewEdit-g304152-d15360933';
        var tx = b.lang === 'en'
          ? 'Hello ' + b.name + ', thank you for staying at Royal Inn Tartus. We hope you enjoyed your stay. If you have a minute, we would be grateful for your review on Google: ' + rv + ' and on Tripadvisor: ' + ta
          : 'مرحباً ' + b.name + '، شكراً لإقامتكم في فندق رويال إن طرطوس، ونتمنى أن تكون إقامتكم قد نالت إعجابكم. يسعدنا جداً تقييمكم على Google، يستغرق دقيقة واحدة: ' + rv + ' \nوعلى Tripadvisor: ' + ta;
        var ra = el('a', 'btn wa', 'اطلب تقييماً (واتساب)'); ra.href = 'https://wa.me/' + digits(b.phone) + '?text=' + encodeURIComponent(tx); ra.target = '_blank'; ra.rel = 'noopener'; act.appendChild(ra);
      }
      if (!b.departed) { var eb = el('button', 'ghost', 'تمديد / تعديل المغادرة'); eb.type = 'button'; eb.addEventListener('click', function () { ext.hidden = !ext.hidden; }); act.appendChild(eb); }
    }
    // edit the booking itself: guest, dates, room type, guests, breakfast, amount
    if (b.status !== 'cancelled') {
      var ed = el('form', 'ext edit'); ed.hidden = true;
      var F = {};
      [['name', 'الاسم', 'text', b.name], ['phone', 'الهاتف', 'tel', b.phone === '-' ? '' : b.phone], ['email', 'البريد', 'email', b.email || ''], ['in', 'الوصول', 'date', b.check_in], ['out', 'المغادرة', 'date', b.check_out],
       ['guests', 'الضيوف', 'number', b.guests], ['total', 'المبلغ $ (فارغ = يُحسب تلقائياً)', 'number', '']].forEach(function (x) {
        var l = el('label', null, x[1]), i = el('input'); i.type = x[2]; i.value = x[3]; if (x[2] !== 'text') i.dir = 'ltr'; if (x[0] === 'guests') { i.min = 1; i.max = 40; } F[x[0]] = i; l.appendChild(i); ed.appendChild(l);
      });
      var lr = el('label', null, 'نوع الغرفة'), rs = el('select'); var o1 = el('option', null, 'غير محدد'); o1.value = ''; rs.appendChild(o1);
      ROOMS.forEach(function (r) { var o = el('option', null, r.name); o.value = r.id; rs.appendChild(o); }); rs.value = b.room || ''; lr.appendChild(rs); ed.appendChild(lr);
      var lb = el('label', 'inline'), cb = el('input'); cb.type = 'checkbox'; cb.checked = b.breakfast; lb.appendChild(cb); lb.appendChild(document.createTextNode(' مع الفطور')); ed.appendChild(lb);
      var einfo = el('p', 'ext-info', 'عدّل ما تريد ثم اضغط «حفظ التعديل». المبلغ يُعاد حسابه إذا تغيّرت التواريخ أو الغرفة أو عدد الضيوف.'); ed.appendChild(einfo);
      var esv = el('button', 'gold', 'حفظ التعديل'); esv.type = 'submit'; ed.appendChild(esv);
      ed.addEventListener('submit', function (ev) {
        ev.preventDefault();
        if (F.out.value <= F['in'].value) { toast('تاريخ المغادرة يجب أن يكون بعد تاريخ الوصول'); return; }
        var data = { name: F.name.value, phone: F.phone.value, email: F.email.value, 'in': F['in'].value, out: F.out.value, guests: F.guests.value, total: F.total.value, room: rs.value, breakfast: cb.checked };
        data.preview = true;
        api('bookings/' + b.ref + '/edit', data).then(function (j) {
          if (gone(j)) return; if (!j.ok) { toast(j.error === 'guests' ? 'عدد الضيوف أكبر من المسموح لهذه الغرفة (الحد الأقصى ' + j.max + ')' : 'تحقّق من الاسم والتواريخ والبريد'); return; }
          var q = 'حفظ التعديل؟' + (j.total !== j.old_total ? '\nالمبلغ: ' + (j.old_total == null ? 'غير محدد' : '$' + j.old_total) + ' ← ' + (j.total == null ? 'يُحدَّد من الفندق' : '$' + j.total) : '') + (j.available ? '' : '\nتنبيه: الغرف من هذا النوع مكتملة أو التواريخ مغلقة.');
          if (!confirm(q)) return;
          data.preview = false; data.force = !j.available;
          api('bookings/' + b.ref + '/edit', data).then(function (k) { if (k.ok) { toast(k.room_no_cleared ? 'تم الحفظ. رقم الغرفة السابق محجوز في التواريخ الجديدة، اختر رقماً آخر.' : 'تم حفظ التعديل'); loadList(); } else if (!gone(k)) toast('لم يتم الحفظ'); });
        });
      });
      body.appendChild(ed);
      var eb2 = el('button', 'ghost', 'تعديل الحجز'); eb2.type = 'button'; eb2.addEventListener('click', function () { ed.hidden = !ed.hidden; }); act.appendChild(eb2);
    } else {
      var del = el('button', 'ghost danger', 'حذف نهائي'); del.type = 'button';
      del.addEventListener('click', function () { if (!confirm('حذف الحجز ' + b.ref + ' (' + b.name + ') نهائياً؟ لا يمكن التراجع.')) return; api('bookings/' + b.ref + '/delete', {}).then(function (j) { if (j.ok) { toast('تم الحذف'); loadList(); } else if (!gone(j)) toast('لم يتم الحذف'); }); });
      act.appendChild(del);
    }
    var save = el('button', 'ghost', b.status === 'confirmed' ? 'حفظ' : 'حفظ الملاحظة'); save.type = 'button';
    save.addEventListener('click', function () { update(b.ref, null, note.value, roomNo ? { room_no: roomNo.value, paid: paid.value, agency_id: agSel.value, pay_by: paySel.value } : null); });
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
      if (j.ok) { if (st === 'confirmed' && tab === 'new') { toast(j.mailed ? 'تم تأكيد الحجز وأُرسل التأكيد إلى بريد الزبون.' : 'تم تأكيد الحجز. أرسل الآن رسالة التأكيد للزبون.'); return go('active'); }
        toast(st === 'confirmed' ? 'تم تأكيد الحجز' : st === 'cancelled' ? 'تم الإلغاء' : st === 'new' ? 'أُعيد إلى الطلبات الجديدة' : 'تم الحفظ'); loadList(); }
      else if (j.error === 'room_taken') toast('الغرفة ' + (j.no || '') + ' محجوزة في هذه التواريخ للضيف: ' + j.by);
      else if (j.error === 'room_twice') toast('رقم الغرفة نفسه مكتوب مرتين');
      else if (j.error === 'room_count') toast('هذا الحجز يحتاج ' + j.max + ' أرقام غرف فقط');
      else if (j.error === 'full') {
        if (confirm('لا توجد غرف شاغرة من هذا النوع في هذه التواريخ (أو التواريخ مغلقة). تأكيده سيعني حجزاً زائداً. هل تؤكّده رغم ذلك؟')) { var m2 = more || {}; m2.force = true; update(ref, st, note, m2); }
        else loadList();
      }
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
        th.appendChild(el('span', 'dn', DAYS[dd.getDay()])); th.appendChild(el('span', null, p[2] + '/' + p[1])); th.classList.add('pick'); th.tabIndex = 0; th.setAttribute('role', 'button');
        th.addEventListener('click', function () { loadDay(ds); }); th.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') loadDay(ds); }); hr.appendChild(th);
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
  function loadDay(ds) {
    api('day?date=' + ds).then(function (j) {
      if (gone(j) || !j.ok) return;
      $('#day-title').textContent = longDate(j.date);
      mini($('#day-arr'), j.arrivals, 'لا وصول في هذا اليوم.'); mini($('#day-dep'), j.departures, 'لا مغادرة في هذا اليوم.'); mini($('#day-stay'), j.staying, 'لا مقيمين.');
      $('#day-box').hidden = false; try { $('#day-box').scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch (e) {}
    });
  }
  $('#day-close').addEventListener('click', function () { $('#day-box').hidden = true; });
  $('#occ-prev').addEventListener('click', function () { occFrom.setDate(occFrom.getDate() - 30); loadOcc(); });
  $('#occ-next').addEventListener('click', function () { occFrom.setDate(occFrom.getDate() + 30); loadOcc(); });
  $('#occ-today').addEventListener('click', function () { occFrom = new Date(); loadOcc(); });

  /* ---------------------------------------------------------------- desk booking */
  function srcFields() { var v = $('#add-form').source.value; each('#add-form [data-if]', function (x) { x.hidden = x.getAttribute('data-if') !== v; }); }
  $('#add-form').source.addEventListener('change', srcFields);
  $('#add-form').addEventListener('submit', function (e) {
    e.preventDefault(); var f = e.target;
    if (f.source.value === 'agency' && !f.agency_id.value) { toast('اختر الوكالة، أو أضفها أولاً من «الوكالات والحسابات»'); return; }
    if (f.out.value <= f['in'].value) { toast('تاريخ المغادرة يجب أن يكون بعد تاريخ الوصول'); return; }
    api('bookings/new', { name: f.name.value, phone: f.phone.value, email: f.email.value, 'in': f['in'].value, out: f.out.value, room: f.room.value,
      guests: f.guests.value, total: f.total.value, note: f.note.value, breakfast: f.breakfast.checked, source: f.source.value,
      agency_id: f.agency_id.value, pay_by: f.pay_by.value, referrer: f.referrer.value, lang: f.lang.value }).then(function (j) {
      if (j.ok) { f.reset(); srcFields(); toast('تم حفظ الحجز ' + j.ref + (j.mailed ? ' · أُرسل التأكيد بالبريد' : '')); go('active'); } else if (j.error === 'guests') toast('عدد الضيوف أكبر من المسموح لهذه الغرفة (الحد الأقصى ' + j.max + ')'); else if (!gone(j) && j.status) toast('تحقّق من الاسم والتواريخ');
    });
  });

  /* ---------------------------------------------------------------- room rack */
  var UST = { free: 'جاهزة', occupied: 'مشغولة', arriving: 'محجوزة', leaving: 'مغادرة اليوم', dirty: 'تحتاج تنظيف', out: 'خارج الخدمة' };
  function loadRack() {
    var di = $('#rack-date'); if (!di.value) di.value = iso(new Date());
    api('units?date=' + di.value).then(function (j) {
      if (gone(j) || !j.ok) return;
      var box = $('#rack'); box.textContent = ''; var cnt = {};
      if (!j.units.length) { box.appendChild(el('p', 'empty', 'لم تُضَف أرقام الغرف بعد. أضفها من النموذج في الأسفل.')); $('#rack-sum').textContent = ''; return; }
      var groups = {}, suites = [], conns = [];
      j.units.forEach(function (u) {
        cnt[u.state] = (cnt[u.state] || 0) + 1; (groups[u.room_name] = groups[u.room_name] || []).push(u);
        if (u.room === 'royal-suite') suites.push(u.no); if (u.room === 'royal-connect') conns.push(u);
      });
      if (conns.length) {
        // one switch for all connecting rooms, beside the per-room buttons
        var mc = el('div', 'card merge-card'), on = conns.filter(function (u) { return u.merge; }).length;
        mc.appendChild(el('h2', null, 'دمج الغرف المتصلة مع جناح رويال'));
        mc.appendChild(el('p', 'muted', 'الغرفة المدمجة تُعرض على الموقع ضمن «جناح رويال مع غرفة متصلة» (حتى 4 ضيوف). غير المدمجة تُباع كغرفة ستاندرد، والجناح يُباع وحده لضيفين. الآن: ' + on + ' مدمجة · ' + (conns.length - on) + ' ستاندرد.'));
        var br = el('div', 'row');
        [['دمج الكل', true], ['فصل الكل', false]].forEach(function (x) {
          var bb = el('button', 'ghost sm', x[0]); bb.type = 'button';
          bb.addEventListener('click', function () {
            Promise.all(conns.map(function (u) { return api('units/' + u.id, { merge: x[1] }); })).then(function () { toast(x[1] ? 'دُمجت كل الغرف المتصلة' : 'فُصلت كل الغرف المتصلة'); loadRack(); });
          });
          br.appendChild(bb);
        });
        mc.appendChild(br); box.appendChild(mc);
      }
      $('#rack-sum').textContent = Object.keys(UST).filter(function (k) { return cnt[k]; }).map(function (k) { return UST[k] + ': ' + cnt[k]; }).join(' · ');
      Object.keys(groups).forEach(function (g) {
        var c = el('div', 'card'); c.appendChild(el('h2', null, g + ' (' + groups[g].length + ')')); var grid = el('div', 'rack');
        groups[g].forEach(function (u) {
          var t = el('div', 'unit u-' + u.state);
          t.appendChild(el('b', null, u.no)); t.appendChild(el('span', 'u-st', UST[u.state] + (u.floor ? ' · ط ' + u.floor : '')));
          if (u.guest) t.appendChild(el('span', 'u-g', u.guest + ' · حتى ' + fmt(u.until).slice(0, 5)));
          var row = el('div', 'u-act');
          function b(label, data, del) { var x = el('button', 'ghost sm', label); x.type = 'button'; x.addEventListener('click', function () { if (del && !confirm('حذف الغرفة ' + u.no + ' من القائمة؟')) return; api('units/' + u.id + (del ? '/delete' : ''), data).then(function () { loadRack(); }); }); row.appendChild(x); }
          if (u.clean === 'dirty') b('نُظِّفت', { state: 'clean' });
          else if (u.clean === 'clean' && !u.guest) b('تحتاج تنظيف', { state: 'dirty' });
          if (u.clean === 'out') b('إعادة للخدمة', { state: 'clean' }); else if (!u.guest) { b('خارج الخدمة', { state: 'out' }); b('حذف', {}, true); }
          t.appendChild(row);
          // the room's view, so the desk can give a sea view to whoever asked for one
          var vs = el('select', 'sm u-view'); vs.setAttribute('aria-label', 'إطلالة الغرفة ' + u.no); vs.appendChild(new Option('الإطلالة؟', ''));
          Object.keys(VIEWS).forEach(function (k) { vs.appendChild(new Option(VIEWS[k], k, false, k === u.view)); });
          vs.addEventListener('change', function () { api('units/' + u.id, { view: vs.value }).then(function (j) { if (j.ok) toast('حُفظت إطلالة الغرفة ' + u.no); }); });
          t.appendChild(vs);
          if (u.room === 'royal-connect') {
            // a connecting room: merged with its suite (sold as "Royal Suite with connecting room") or sold as a Standard room
            var mg = el('div', 'u-merge');
            var tg = el('button', 'sm ' + (u.merge ? 'on' : 'ghost'), u.merge ? '✓ مدمجة مع الجناح' : 'تُباع كغرفة ستاندرد');
            tg.type = 'button'; tg.title = 'اضغط للتبديل';
            tg.addEventListener('click', function () { api('units/' + u.id, { merge: !u.merge }).then(function () { toast(u.merge ? 'أصبحت تُباع كغرفة ستاندرد' : 'دُمجت مع الجناح'); loadRack(); }); });
            var ps = el('select', 'sm'); ps.appendChild(new Option('الجناح المرتبط…', ''));
            suites.forEach(function (n) { ps.appendChild(new Option('جناح ' + n, n, false, n === u.pair)); });
            ps.addEventListener('change', function () { api('units/' + u.id, { pair: ps.value }).then(function () { toast('حُفظ الربط'); }); });
            mg.appendChild(tg); mg.appendChild(ps); t.appendChild(mg);
          }
          grid.appendChild(t);
        });
        c.appendChild(grid); box.appendChild(c);
      });
    });
  }
  $('#rack-date').addEventListener('change', loadRack);
  $('#rack-today').addEventListener('click', function () { $('#rack-date').value = iso(new Date()); loadRack(); });
  $('#unit-form').addEventListener('submit', function (e) {
    e.preventDefault(); var f = e.target;
    api('units', { no: f.no.value, to: f.to.value, room: f.room.value, floor: f.floor.value, view: f.view.value }).then(function (j) {
      if (j.ok) { toast('أُضيفت ' + j.added + ' غرفة' + (j.skipped ? ' · موجودة مسبقاً: ' + j.skipped : '')); f.no.value = ''; f.to.value = ''; loadRack(); } else if (!gone(j) && j.status) toast('تحقّق من الأرقام ونوع الغرفة');
    });
  });

  /* ---------------------------------------------------------------- room descriptions shown on the website and in the booking form */
  var RDEF = null;
  function loadRoomInfo() {
    var box = $('#ri-rows'); box.textContent = '';
    var defs = RDEF ? Promise.resolve(RDEF) : fetch('/assets/data/rooms.json').then(function (r) { return r.json(); }).then(function (j) { RDEF = j; return j; }).catch(function () { return {}; });
    Promise.all([defs, api('room-info')]).then(function (res) {
      var D = res[0] || {}, j = res[1];
      if (gone(j) || !j.rooms) return;
      j.rooms.forEach(function (r) {
        var d = D[r.id] || {}, c = el('form', 'card ri');
        var hd = el('div', 'ri-head');
        if (d.img && d.img[0]) { var im = el('img'); im.src = '/assets/img/' + d.img[0] + '-360.webp'; im.alt = ''; im.width = 120; im.height = 90; im.loading = 'lazy'; hd.appendChild(im); }
        var tt = el('div'); tt.appendChild(el('h2', null, r.name));
        tt.appendChild(el('span', 'muted small', (r.ar || r.en || r.tr) ? 'نص الفندق ظاهر الآن على الموقع' : 'يظهر الآن نص الموقع الأصلي'));
        hd.appendChild(tt); c.appendChild(hd);
        [['ar', 'العربية'], ['en', 'English'], ['tr', 'Türkçe']].forEach(function (L) {
          var lb = el('label', null, L[1]), ta = el('textarea'); ta.name = L[0]; ta.rows = 3; ta.maxLength = 600;
          ta.dir = L[0] === 'ar' ? 'rtl' : 'ltr'; ta.value = r[L[0]] || (d[L[0]] && d[L[0]].desc) || '';
          lb.appendChild(ta); c.appendChild(lb);
        });
        var bt = el('div', 'btns'), sv = el('button', 'gold', 'حفظ'), rs = el('button', 'ghost', 'إرجاع نص الموقع الأصلي'); sv.type = 'submit'; rs.type = 'button';
        rs.addEventListener('click', function () {
          if (!confirm('إرجاع وصف «' + r.name + '» إلى نص الموقع الأصلي؟')) return;
          api('room-info', { room: r.id, ar: '', en: '', tr: '' }).then(function (x) { if (x.ok) { toast('عاد النص الأصلي'); loadRoomInfo(); } });
        });
        c.addEventListener('submit', function (e) {
          e.preventDefault(); var data = { room: r.id };
          ['ar', 'en', 'tr'].forEach(function (k) { var v = c[k].value.trim(); data[k] = (d[k] && v === d[k].desc) ? '' : v; });   // unchanged text stays the site's own
          api('room-info', data).then(function (x) { if (x.ok) { toast('حُفظ الوصف. يظهر على الموقع وفي نموذج الحجز خلال دقيقتين.'); loadRoomInfo(); } else if (!gone(x)) toast('لم يتم الحفظ'); });
        });
        bt.appendChild(sv); bt.appendChild(rs); c.appendChild(bt); box.appendChild(c);
      });
    });
  }

  /* ---------------------------------------------------------------- hotel times and policies */
  function loadRules() {
    api('settings').then(function (j) {
      if (gone(j) || !j.ok) return; var f = $('#rules-form'), s = j.settings; RULES = s;
      ['checkin', 'checkout', 'remind_hours', 'late_grace', 'late_half_until', 'late_half_pct', 'late_full_pct'].forEach(function (k) { f[k].value = s[k]; });
      ['late_on', 'noshow_on', 'early_note'].forEach(function (k) { f[k].checked = s[k] === '1'; });
      f.deposit_info.value = s.deposit_info || '';
      $('#rules-clock').textContent = 'ساعة الفندق الآن: ' + j.clock;
      $('#mail-state').textContent = j.mail ? 'الحالة: مفعَّل. الرسائل تُرسل تلقائياً.' : 'الحالة: غير مفعَّل بعد. يجب ربط صندوق بريد من إعدادات الخادم (يقوم به محمود).';
    });
  }
  $('#rules-form').addEventListener('submit', function (e) {
    e.preventDefault(); var f = e.target, d = {};
    ['checkin', 'checkout', 'remind_hours', 'late_grace', 'late_half_until', 'late_half_pct', 'late_full_pct'].forEach(function (k) { d[k] = f[k].value; });
    ['late_on', 'noshow_on', 'early_note'].forEach(function (k) { d[k] = f[k].checked; });
    d.deposit_info = f.deposit_info.value;
    api('settings', d).then(function (j) { if (j.ok) { toast('تم الحفظ'); loadRules(); } else if (!gone(j) && j.status) toast(j.error === 'late_half_until' ? 'موعد نصف اليوم يجب أن يكون بعد موعد المغادرة والمهلة' : 'تحقّق من القيم'); });
  });

  $('#mailtest-form').addEventListener('submit', function (e) {
    e.preventDefault(); api('mail-test', { to: e.target.to.value }).then(function (j) { if (gone(j)) return; toast(j.ok ? 'أُرسلت الرسالة التجريبية' : j.error === 'mail_off' ? 'البريد غير مفعَّل بعد' : 'فشل الإرسال: تحقّق من إعدادات البريد'); });
  });

  /* ---------------------------------------------------------------- agencies and their accounts */
  var agOpen = null;
  function agencies() {
    return api('agencies').then(function (j) {
      AGS = j.items || AGS; if (j.items) agLoaded = true;
      each('.agency-select', function (sel) { var v = sel.value; while (sel.options.length > 1) sel.remove(1); AGS.forEach(function (a) { var o = el('option', null, a.name + ' (' + a.commission + '%)'); o.value = a.id; sel.appendChild(o); }); sel.value = v; });
      return j;
    });
  }
  function money(n) { return (n < 0 ? '−' : '') + '$' + Math.abs(n); }
  function loadAgencies() {
    if (agOpen) return openAgency(agOpen);
    $('#ag-detail').hidden = true; $('#ag-list-view').hidden = false;
    agencies().then(function (j) {
      if (gone(j)) return;
      var box = $('#agencies'); box.textContent = '';
      if (!AGS.length) { box.appendChild(el('p', 'empty', 'لا توجد وكالات بعد. أضف أول وكالة من النموذج أعلاه.')); return; }
      AGS.forEach(function (a) {
        var c = el('div', 'card closure');
        c.appendChild(el('strong', null, a.name));
        c.appendChild(el('span', 'muted', 'عمولة ' + a.commission + '% · ' + a.bookings + ' حجوزات مؤكَّدة · مبيعات ' + money(a.sales)));
        c.appendChild(el('span', a.balance > 0 ? 'due' : 'paidok', a.balance > 0 ? 'عليها ' + money(a.balance) : a.balance < 0 ? 'لها رصيد ' + money(-a.balance) : 'لا رصيد مستحق'));
        var b = el('button', 'ghost', 'فتح الحساب'); b.type = 'button'; b.addEventListener('click', function () { openAgency(a.id); }); c.appendChild(b);
        box.appendChild(c);
      });
    });
  }
  function openAgency(id) {
    agOpen = id;
    api('agencies/' + id).then(function (j) {
      if (gone(j)) return;
      if (!j.ok) { agOpen = null; return loadAgencies(); }
      var a = j.agency, s = j.sums;
      $('#ag-list-view').hidden = true; $('#ag-detail').hidden = false;
      $('#ag-name').textContent = a.name;
      $('#ag-info').textContent = ['عمولة ' + a.commission + '%', a.contact, a.phone, a.email, a.note].filter(Boolean).join(' · ');
      $('#ag-statement').href = '/api/admin/agencies/' + id + '/statement'; $('#ag-prices').href = j.pricelist;
      var k = $('#ag-kpis'); k.textContent = '';
      k.appendChild(kpi(s.bookings, 'حجوزات مؤكَّدة')); k.appendChild(kpi(money(s.sales), 'إجمالي المبيعات')); k.appendChild(kpi(money(s.due), 'المستحق على الوكالة'));
      k.appendChild(kpi(money(s.paid), 'دفعت الوكالة')); k.appendChild(kpi(money(Math.abs(s.balance)), s.balance >= 0 ? 'الرصيد المتبقي عليها' : 'رصيد لها', s.balance > 0 ? 'warn' : 'gold')); k.appendChild(kpi(money(s.commission_owed), 'عمولة للوكالة'));
      var t = $('#ag-bookings'); t.textContent = '';
      var hr = el('tr'); ['الرقم', 'الضيف', 'الوصول', 'المغادرة', 'الإجمالي', 'الدافع', 'المستحق', 'العمولة'].forEach(function (h) { hr.appendChild(el('th', null, h)); }); t.appendChild(hr);
      if (!j.bookings.length) { var e0 = el('tr'), td0 = el('td', 'empty', 'لا توجد حجوزات مؤكَّدة لهذه الوكالة.'); td0.colSpan = 8; e0.appendChild(td0); t.appendChild(e0); }
      j.bookings.forEach(function (b) {
        var tot = b.total || 0, nt = b.net == null ? tot : b.net, ag = b.pay_by === 'agency', tr = el('tr');
        [b.ref, b.name, fmt(b.check_in), fmt(b.check_out), '$' + tot, ag ? 'الوكالة' : 'الضيف', ag ? '$' + nt : '—', ag ? '—' : '$' + (tot - nt)].forEach(function (v) { tr.appendChild(el('td', null, v)); });
        t.appendChild(tr);
      });
      var pb = $('#ag-pays'); pb.textContent = '';
      j.payments.forEach(function (x) {
        var r = el('div', 'mini'); r.appendChild(el('b', null, '$' + x.amount)); r.appendChild(el('span', 'm-sub', fmt(x.date) + (x.note ? ' · ' + x.note : '')));
        var d = el('button', 'ghost danger', 'حذف'); d.type = 'button'; d.className += ' btn';
        d.addEventListener('click', function () { if (!confirm('حذف هذه الدفعة ($' + x.amount + ')؟')) return; api('agency-payments/' + x.id + '/delete', {}).then(function () { toast('تم الحذف'); openAgency(id); }); });
        r.appendChild(d); pb.appendChild(r);
      });
      $('#pay-form').date.value = $('#pay-form').date.value || iso(new Date());
      $('#ag-del').onclick = function () {
        if (!confirm('حذف الوكالة «' + a.name + '» مع دفعاتها المسجَّلة؟')) return;
        api('agencies/' + id + '/delete', {}).then(function (k) { if (k.ok) { toast('تم حذف الوكالة'); agOpen = null; agLoaded = false; loadAgencies(); } else if (!gone(k)) toast(k.error === 'has_bookings' ? 'لا يمكن الحذف: توجد حجوزات مسجَّلة على هذه الوكالة.' : 'لم يتم الحذف'); });
      };
      $('#ag-edit').onclick = function () {
        var f = $('#ag-form'); f.id.value = a.id; f.name.value = a.name; f.commission.value = a.commission; f.contact.value = a.contact || ''; f.phone.value = a.phone || ''; f.email.value = a.email || ''; f.note.value = a.note || '';
        $('#ag-form-title').textContent = 'تعديل: ' + a.name; agOpen = null; $('#ag-detail').hidden = true; $('#ag-list-view').hidden = false; window.scrollTo(0, 0);
      };
    });
  }
  $('#ag-back').addEventListener('click', function () { agOpen = null; loadAgencies(); });
  $('#ag-form').addEventListener('submit', function (e) {
    e.preventDefault(); var f = e.target;
    api('agencies' + (f.id.value ? '/' + f.id.value : ''), { name: f.name.value, commission: f.commission.value, contact: f.contact.value, phone: f.phone.value, email: f.email.value, note: f.note.value }).then(function (j) {
      if (j.ok) { f.reset(); f.id.value = ''; $('#ag-form-title').textContent = 'وكالة جديدة'; toast('تم حفظ الوكالة'); loadAgencies(); } else if (!gone(j) && j.status) toast('تحقّق من الاسم والعمولة (0 إلى 50)');
    });
  });
  $('#pay-form').addEventListener('submit', function (e) {
    e.preventDefault(); var f = e.target; if (!agOpen) return;
    api('agencies/' + agOpen + '/payment', { amount: f.amount.value, date: f.date.value, note: f.note.value }).then(function (j) {
      if (j.ok) { f.amount.value = ''; f.note.value = ''; toast('تم تسجيل الدفعة'); openAgency(agOpen); } else if (!gone(j) && j.status) toast('تحقّق من المبلغ');
    });
  });

  /* ---------------------------------------------------------------- prices */
var VIEWS = { sea: 'إطلالة على البحر', side: 'إطلالة جانبية', city: 'إطلالة على المدينة', none: 'بدون إطلالة' };
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
