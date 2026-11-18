/* Royal Inn - booking panel. Talks only to /api/admin on this site. */
(function () {
  'use strict';
  var $ = function (s) { return document.querySelector(s); };
  var tab = 'new', ROOMS = [];
  var LABEL = { 'new': 'جديد', confirmed: 'مؤكَّد', cancelled: 'ملغى' };

  function api(path, data) {
    var opt = { credentials: 'same-origin', headers: {} };
    if (data !== undefined) { opt.method = 'POST'; opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(data); }
    return fetch('/api/admin/' + path, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { j.status = r.status; return j; });
    });
  }
  function toast(t) { var el = $('#toast'); el.textContent = t; el.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(function () { el.hidden = true; }, 2600); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function fmt(d) { var p = d.split('-'); return p[2] + '/' + p[1] + '/' + p[0]; }
  function digits(p) { var x = p.replace(/[^0-9]/g, ''); if (x.indexOf('00') === 0) x = x.slice(2); else if (x.indexOf('09') === 0 && x.length === 10) x = '963' + x.slice(1); return x; }

  function show(which) {
    $('#login').hidden = which !== 'login'; $('#app').hidden = which !== 'app'; $('#logout').hidden = which !== 'app';
  }

  function start() {
    api('me').then(function (j) {
      if (j.auth) { show('app'); load(); }
      else { show('login'); if (!j.configured) { $('#login-msg').textContent = 'اللوحة غير مفعّلة بعد: يجب إضافة كلمة المرور في إعدادات الخادم (PANEL_PASSWORD).'; } }
    }).catch(function () { show('login'); $('#login-msg').textContent = 'تعذّر الاتصال بالخادم.'; });
  }

  $('#login-form').addEventListener('submit', function (e) {
    e.preventDefault();
    api('login', { password: e.target.password.value }).then(function (j) {
      if (j.ok) { e.target.reset(); show('app'); load(); }
      else { toast(j.error === 'busy' ? 'محاولات كثيرة، انتظر ربع ساعة.' : j.error === 'not_configured' ? 'اللوحة غير مفعّلة بعد.' : 'كلمة المرور غير صحيحة.'); }
    });
  });
  $('#logout').addEventListener('click', function () { api('logout', {}).then(function () { show('login'); }); });

  Array.prototype.forEach.call(document.querySelectorAll('.tabs button'), function (b) {
    b.addEventListener('click', function () {
      tab = b.getAttribute('data-tab');
      Array.prototype.forEach.call(document.querySelectorAll('.tabs button'), function (x) { x.classList.toggle('on', x === b); });
      load();
    });
  });

  function load() {
    var list = tab === 'new' || tab === 'active' || tab === 'all';
    $('#view-list').hidden = !list;
    ['closures', 'inventory', 'occupancy', 'add', 'prices'].forEach(function (v) { $('#view-' + v).hidden = tab !== v; });
    if (list) loadList(); else if (tab === 'closures') loadClosures(); else if (tab === 'inventory') loadInventory();
    else if (tab === 'occupancy') loadOcc(); else if (tab === 'add') rooms(); else if (tab === 'prices') loadPrices();
  }

  function loadList() {
    api('bookings?status=' + tab).then(function (j) {
      if (j.status === 401) return show('login');
      badge((j.counts && j.counts['new']) || 0);
      var box = $('#list'); box.textContent = '';
      $('#list-title').textContent = { 'new': 'طلبات بانتظار الرد', active: 'حجوزات لم تنتهِ بعد، حسب تاريخ الوصول', all: 'آخر 500 طلب' }[tab];
      if (!j.items || !j.items.length) { box.appendChild(el('p', 'empty', 'لا توجد طلبات هنا.')); return; }
      j.items.forEach(function (b) { box.appendChild(card(b)); });
    });
  }

  function card(b) {
    var c = el('article', 'card bk st-' + b.status);
    var h = el('div', 'bk-head');
    h.appendChild(el('strong', 'name', b.name));
    h.appendChild(el('span', 'badge', LABEL[b.status]));
    h.appendChild(el('span', 'ref', b.ref));
    c.appendChild(h);
    var dl = el('dl', 'facts');
    [['الوصول', fmt(b.check_in)], ['المغادرة', fmt(b.check_out)], ['الليالي', b.nights], ['الضيوف', b.guests],
     ['الغرفة', b.room_name], ['الفطور', b.breakfast ? 'نعم' : 'لا'], ['المبلغ', b.total ? '$' + b.total : 'يُحدَّد'], ['المصدر', b.source === 'desk' ? 'هاتف / استقبال' : 'الموقع'],
     ['الهاتف', b.phone], ['البريد', b.email || '-']].forEach(function (f) {
      var w = el('div'); w.appendChild(el('dt', null, f[0])); var dd = el('dd', null, String(f[1]));
      if (f[0] === 'الهاتف' || f[0] === 'البريد') dd.dir = 'ltr';
      w.appendChild(dd); dl.appendChild(w);
    });
    c.appendChild(dl);
    if (b.message) c.appendChild(el('p', 'msg', b.message));
    c.appendChild(el('p', 'muted small', 'وصل الطلب: ' + b.created.replace('T', ' ').slice(0, 16) + ' (UTC)'));

    var note = el('input', 'note'); note.type = 'text'; note.maxLength = 400; note.placeholder = 'ملاحظة داخلية (لا يراها الزبون)'; note.value = b.staff_note || '';
    c.appendChild(note);

    var act = el('div', 'actions');
    var wa = el('a', 'btn wa', 'واتساب للزبون'); wa.target = '_blank'; wa.rel = 'noopener';
    var room = b.room_name === 'أي غرفة' ? '' : ' (' + b.room_name + ')';
    var text = b.lang === 'en'
      ? 'Hello ' + b.name + ', this is Royal Inn Tartus about your booking request ' + b.ref + ': ' + b.check_in + ' to ' + b.check_out + ', ' + b.guests + ' guests' + (b.total ? ', total $' + b.total : '') + '.'
      : 'مرحباً ' + b.name + '، معك فندق رويال إن طرطوس بخصوص طلب الحجز ' + b.ref + ': من ' + fmt(b.check_in) + ' إلى ' + fmt(b.check_out) + '، ' + b.guests + ' ضيوف' + room + (b.total ? '، المبلغ الإجمالي ' + b.total + '$' : '') + '.';
    wa.href = 'https://wa.me/' + digits(b.phone) + '?text=' + encodeURIComponent(text);
    act.appendChild(wa);
    var tel = el('a', 'btn ghost', 'اتصال'); tel.href = 'tel:' + b.phone.replace(/[^0-9+]/g, ''); act.appendChild(tel);
    if (b.status !== 'confirmed') act.appendChild(button('تأكيد الحجز', 'gold', 'confirmed'));
    if (b.status !== 'cancelled') act.appendChild(button('إلغاء', 'ghost danger', 'cancelled'));
    if (b.status !== 'new') act.appendChild(button('إرجاع إلى جديد', 'ghost', 'new'));
    var save = el('button', 'ghost', 'حفظ الملاحظة'); save.type = 'button';
    save.addEventListener('click', function () { update(b.ref, null, note.value); });
    act.appendChild(save);
    c.appendChild(act);
    return c;

    function button(label, cls, st) {
      var x = el('button', cls, label); x.type = 'button';
      x.addEventListener('click', function () {
        if (st === 'cancelled' && !confirm('إلغاء طلب ' + b.name + '؟')) return;
        update(b.ref, st, note.value);
      });
      return x;
    }
  }

  function update(ref, st, note) {
    var data = { note: note }; if (st) data.status = st;
    api('bookings/' + ref, data).then(function (j) {
      if (j.ok) { toast(st === 'confirmed' ? 'تم تأكيد الحجز' : st === 'cancelled' ? 'تم الإلغاء' : 'تم الحفظ'); loadList(); }
      else if (j.status === 401) show('login'); else toast('لم يتم الحفظ');
    });
  }

  function roomName(id) { for (var i = 0; i < ROOMS.length; i++) if (ROOMS[i].id === id) return ROOMS[i].name; return 'كل الغرف'; }

  function rooms() {
    return api('inventory').then(function (j) {
      ROOMS = j.rooms || [];
      Array.prototype.forEach.call(document.querySelectorAll('#closure-room, .room-select'), function (sel) {
        if (sel.options.length === 1) ROOMS.forEach(function (r) { var o = el('option', null, r.name); o.value = r.id; sel.appendChild(o); });
      });
      return j;
    });
  }

  function loadClosures() {
    rooms().then(function () { return api('closures'); }).then(function (j) {
      if (j.status === 401) return show('login');
      var box = $('#closures'); box.textContent = '';
      if (!j.items || !j.items.length) { box.appendChild(el('p', 'empty', 'لا توجد تواريخ مغلقة.')); return; }
      j.items.forEach(function (x) {
        var c = el('div', 'card closure');
        c.appendChild(el('strong', null, fmt(x.date_from) + ' ← ' + fmt(x.date_to)));
        c.appendChild(el('span', 'muted', roomName(x.room) + (x.note ? ' · ' + x.note : '')));
        var del = el('button', 'ghost danger', 'فتح التواريخ'); del.type = 'button';
        del.addEventListener('click', function () { api('closures/' + x.id + '/delete', {}).then(loadClosures); });
        c.appendChild(del); box.appendChild(c);
      });
    });
  }
  $('#closure-form').addEventListener('submit', function (e) {
    e.preventDefault(); var f = e.target;
    api('closures', { from: f.from.value, to: f.to.value, room: f.room.value, note: f.note.value }).then(function (j) {
      if (j.ok) { f.reset(); toast('تم الإغلاق'); loadClosures(); } else toast('تحقّق من التواريخ');
    });
  });

  function loadInventory() {
    rooms().then(function (j) {
      if (j.status === 401) return show('login');
      var box = $('#inv-rows'); box.textContent = '';
      ROOMS.forEach(function (r) {
        var l = el('label', 'inv', r.name); var i = el('input'); i.type = 'number'; i.min = 0; i.max = 500; i.name = r.id;
        i.value = r.count == null ? '' : r.count; i.placeholder = 'غير محدد'; l.appendChild(i); box.appendChild(l);
      });
    });
  }
  $('#inv-form').addEventListener('submit', function (e) {
    e.preventDefault(); var data = {};
    Array.prototype.forEach.call(e.target.querySelectorAll('input'), function (i) { data[i.name] = i.value === '' ? null : +i.value; });
    api('inventory', data).then(function (j) { toast(j.ok ? 'تم الحفظ' : 'لم يتم الحفظ'); });
  });

  /* occupancy */
  var occFrom = new Date();
  function iso(t) { return t.getFullYear() + '-' + ('0' + (t.getMonth() + 1)).slice(-2) + '-' + ('0' + t.getDate()).slice(-2); }
  var DAYS = ['أحد', 'إثن', 'ثلا', 'أرب', 'خمي', 'جمع', 'سبت'];
  function loadOcc() {
    api('occupancy?from=' + iso(occFrom) + '&days=31').then(function (j) {
      if (j.status === 401) return show('login');
      var t = $('#occ'); t.textContent = '';
      var hr = el('tr'); hr.appendChild(el('th', 'sticky', 'الغرفة'));
      j.dates.forEach(function (ds) { var p = ds.split('-'), dd = new Date(+p[0], +p[1] - 1, +p[2]); var th = el('th', dd.getDay() === 5 || dd.getDay() === 6 ? 'we' : null); th.appendChild(el('span', 'dn', DAYS[dd.getDay()])); th.appendChild(el('span', null, p[2] + '/' + p[1])); hr.appendChild(th); });
      t.appendChild(hr);
      j.rows.forEach(function (r) {
        var tr = el('tr'); tr.appendChild(el('th', 'sticky', r.name + (r.count != null ? ' (' + r.count + ')' : '')));
        r.cells.forEach(function (c) {
          var td = el('td'); var conf = c[0], nw = c[1], closed = c[2];
          td.textContent = conf ? (r.count != null ? conf + '/' + r.count : conf) : (r.count != null ? '0/' + r.count : '·');
          if (closed) td.className = 'closed'; else if (r.count != null && conf >= r.count) td.className = 'full';
          else if (conf) td.className = 'some';
          if (nw) { var s = el('sup', 'nw', '+' + nw); td.appendChild(s); }
          tr.appendChild(td);
        });
        t.appendChild(tr);
      });
      var tr = el('tr'); tr.appendChild(el('th', 'sticky', 'بدون نوع محدد'));
      j.any.forEach(function (c) { var td = el('td', c[0] ? 'some' : null, c[0] ? String(c[0]) : '·'); if (c[1]) td.appendChild(el('sup', 'nw', '+' + c[1])); tr.appendChild(td); });
      t.appendChild(tr);
    });
  }
  $('#occ-prev').addEventListener('click', function () { occFrom.setDate(occFrom.getDate() - 30); loadOcc(); });
  $('#occ-next').addEventListener('click', function () { occFrom.setDate(occFrom.getDate() + 30); loadOcc(); });

  /* desk booking */
  $('#add-form').addEventListener('submit', function (e) {
    e.preventDefault(); var f = e.target;
    api('bookings/new', { name: f.name.value, phone: f.phone.value, 'in': f['in'].value, out: f.out.value, room: f.room.value,
      guests: f.guests.value, total: f.total.value, note: f.note.value, breakfast: f.breakfast.checked }).then(function (j) {
      if (j.ok) { f.reset(); toast('تم حفظ الحجز ' + j.ref); } else toast('تحقّق من الاسم والتواريخ');
    });
  });

  /* prices */
  function loadPrices() {
    rooms().then(function () { return api('prices'); }).then(function (j) {
      if (j.status === 401) return show('login');
      var tb = $('#price-rows'); tb.textContent = '';
      j.rooms.forEach(function (r) {
        var tr = el('tr'); tr.appendChild(el('td', null, r.name));
        ['price', 'price2'].forEach(function (k) { var td = el('td'), i = el('input'); i.type = 'number'; i.min = 1; i.name = r.id + '.' + k; i.value = r[k] == null ? '' : r[k]; i.placeholder = k === 'price' ? 'عند الطلب' : '-'; td.appendChild(i); tr.appendChild(td); });
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
        del.addEventListener('click', function () { api('rates/' + x.id + '/delete', {}).then(loadPrices); });
        c.appendChild(del); box.appendChild(c);
      });
    });
  }
  $('#price-form').addEventListener('submit', function (e) {
    e.preventDefault(); var data = {};
    Array.prototype.forEach.call(e.target.querySelectorAll('input'), function (i) {
      var p = i.name.split('.'); data[p[0]] = data[p[0]] || {}; data[p[0]][p[1]] = i.value === '' ? null : +i.value;
    });
    api('prices', data).then(function (j) { toast(j.ok ? 'تم حفظ الأسعار' : 'تحقّق من الأرقام'); });
  });
  $('#rate-form').addEventListener('submit', function (e) {
    e.preventDefault(); var f = e.target;
    var days = Array.prototype.filter.call(f.querySelectorAll('[name=d]'), function (c) { return c.checked; }).map(function (c) { return +c.value; });
    api('rates', { from: f.from.value, to: f.to.value, room: f.room.value, price: f.price.value, price2: f.price2.value, note: f.note.value, days: days }).then(function (j) {
      if (j.ok) { f.reset(); toast('تمت الإضافة'); loadPrices(); } else toast('تحقّق من التواريخ والسعر');
    });
  });

  // the count of waiting requests shows on the tab button and in the browser tab title, on every screen
  function badge(n) { $('#c-new').textContent = n; document.title = (n ? '(' + n + ') ' : '') + 'لوحة الحجوزات | Royal Inn'; }
  setInterval(function () {
    if ($('#app').hidden) return;
    if (tab === 'new' || tab === 'active') loadList();
    else api('bookings?status=new').then(function (j) { if (j.counts) badge(j.counts['new'] || 0); });
  }, 60000);
  start();
})();
