/* Royal Inn Tartus — small helpers. No tracking, no cookies; the only requests go to the hotel's own booking server. */
(function () {
  'use strict';
  var d = document, root = d.documentElement;
  root.classList.add('js');
  var conn = navigator.connection || {};
  var lite = !!conn.saveData || /(^|-)[23]g$/.test(conn.effectiveType || '');
  var calm = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var auto = !lite && !calm;

  /* language choice lives only on this device */
  function remember(lang) { try { localStorage.setItem('ri-lang', lang); } catch (e) {} }
  if (root.hasAttribute('data-site')) { remember(root.lang); }
  d.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('[data-setlang]');
    if (a) { remember(a.getAttribute('data-setlang')); }
  });

  /* phone menu: close on Escape or when a click lands outside it */
  d.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { each('details.menu[open]', function (m) { m.open = false; }); }
  });
  d.addEventListener('click', function (e) {
    each('details.menu[open]', function (m) {
      if (!m.contains(e.target) || (e.target.closest && e.target.closest('.menu-panel a'))) { m.open = false; }
    });
  });

  function each(sel, fn, scope) { Array.prototype.forEach.call((scope || d).querySelectorAll(sel), fn); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function iso(t) { return t.getFullYear() + '-' + pad(t.getMonth() + 1) + '-' + pad(t.getDate()); }

  /* booking: dates first, then guest details; the request goes to the hotel's own server (or WhatsApp as a fallback) */
  each('[data-booking]', function (box) {
    var f = function (n) { return box.querySelector('[name=' + n + ']'); };
    var ci = f('in'), co = f('out'), guests = f('guests'), room = f('room');
    var wa = box.querySelector('[data-wa-link]'), mail = box.querySelector('[data-mail-link]'), sum = box.querySelector('[data-sum]');
    var more = box.querySelector('[data-more]'), next = box.querySelector('[data-next]'), err = box.querySelector('[data-err]');
    var avail = box.querySelector('[data-avail]'), done = box.querySelector('[data-done]'), send = box.querySelector('[data-send]');
    var L = box.dataset, now = new Date(), api = L.api, lastCheck = '', sending = false;
    var combo = '', comboName = '';                               // a mix of room types picked from the suggestions
    ci.min = iso(now);
    co.min = iso(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1));
    function nights() {
      if (!ci.value || !co.value) { return 0; }
      var a = ci.value.split('-'), b = co.value.split('-');
      return Math.round((Date.UTC(+b[0], +b[1] - 1, +b[2]) - Date.UTC(+a[0], +a[1] - 1, +a[2])) / 864e5);
    }
    function cap() {
      var o = room.value ? room.options[room.selectedIndex] : null;
      return (o && +o.getAttribute('data-max')) || 2;          // 'any room' is counted in rooms for two
    }
    function roomsNeeded() { return Math.max(1, Math.ceil(+guests.value / cap())); }
    function rate() {
      var o = room.value ? room.options[room.selectedIndex] : null;
      if (!o) { return 0; }
      var p1 = +o.getAttribute('data-price') || 0, p2 = +o.getAttribute('data-price2') || 0;
      if (!p1) { return 0; }
      var g = +guests.value, n = roomsNeeded(), sum = 0;
      for (var i = 0; i < n; i++) {                              // guests spread evenly: 5 in rooms for two -> 2, 2, 1
        var k = Math.floor(g / n) + (i < g % n ? 1 : 0);
        sum += (p2 && k >= 2) ? p2 : p1;
      }
      return sum;
    }
    var comboPrice = '';
    function partyLine(n) {
      var base = L.lnights + ': ' + n + ' · ' + L.lguests + ': ' + guests.value;
      if (!room.value) { return comboName ? base + ' · ' + comboName + (comboPrice ? ' · ' + comboPrice : '') : base; }   // 'any room': the chosen option says how many rooms
      var r = roomsNeeded();
      return base + (r > 1 ? ' · ' + L.lrooms + ': ' + r : '');
    }
    function dropCombo() { combo = ''; comboName = ''; comboPrice = ''; }
    function text(ref) {
      var lines = [L.hello], n = nights();
      if (ref) { lines.push(L.lref + ': ' + ref); }
      if (f('name').value.trim()) { lines.push(L.lname + ': ' + f('name').value.trim()); }
      if (ci.value) { lines.push(L.lin + ': ' + ci.value); }
      if (co.value) { lines.push(L.lout + ': ' + co.value); }
      if (n > 0) { lines.push(L.lnights + ': ' + n); }
      lines.push(L.lguests + ': ' + guests.value);
      if (room.value && roomsNeeded() > 1) { lines.push(L.lrooms + ': ' + roomsNeeded()); }
      if (room.value) { lines.push(L.lroom + ': ' + room.options[room.selectedIndex].text); }
      else if (comboName) { lines.push(L.lroom + ': ' + comboName); }
      if (f('breakfast').checked) { lines.push(L.lbf + ': ' + L.yes); }
      if (f('message').value.trim()) { lines.push(f('message').value.trim()); }
      return lines.join('\n');
    }
    function waUrl(ref) { return 'https://wa.me/' + L.wa + '?text=' + encodeURIComponent(text(ref)); }
    function money(n) { return '\u2066$' + n + '\u2069'; }   // keeps '$580' whole inside Arabic text
    /* choose rooms yourself: a stepper per room type, priced by the server for the exact mix */
    var pickState = {}, pickTimer = 0, pickRefresh = null, pickOpen = false;
    function picker(j, n) {
      var box2 = d.createElement('div'); box2.className = 'picker';
      var h = d.createElement('p'); h.className = 'opt-h'; h.textContent = L.pickh; box2.appendChild(h);
      var g = +guests.value, stat = d.createElement('p'); stat.className = 'pick-stat';
      function capNow() { return j.rooms.reduce(function (a, r) { return a + (pickState[r.id] || 0) * r.cap; }, 0); }
      function count() { return Object.keys(pickState).reduce(function (a, k) { return a + (pickState[k] || 0); }, 0); }
      function refresh() {
        var c = capNow(), m = count();
        stat.textContent = L.pickcap.replace('{c}', c).replace('{g}', g);
        stat.className = 'pick-stat' + (c >= g && m <= g && m > 0 ? ' ok' : '');
        Array.prototype.forEach.call(box2.querySelectorAll('[data-step]'), function (b) {
          var id = b.getAttribute('data-id'), v = pickState[id] || 0, mx = +b.getAttribute('data-max');
          if (b.getAttribute('data-step') === '1') { b.disabled = v >= mx || m >= g; } else { b.disabled = v <= 0; }
          var out = box2.querySelector('[data-val="' + id + '"]'); if (out) { out.textContent = v; }
        });
        clearTimeout(pickTimer);
        if (!(c >= g && m <= g && m > 0)) { if (combo && combo === pickKey()) { dropCombo(); sum.textContent = partyLine(n); } return; }
        pickTimer = setTimeout(function () {
          var key = pickKey();
          fetch(api + 'quote?in=' + ci.value + '&out=' + co.value + '&guests=' + guests.value + '&combo=' + encodeURIComponent(key), { credentials: 'omit' })
            .then(function (r) { return r.json(); }).then(function (q) {
              if (!q.ok || !q.pick || q.pick.invalid || key !== pickKey()) { return; }
              var p = q.pick, names = [];
              Object.keys(p.rooms).forEach(function (k) { var op = room.querySelector('option[value="' + k + '"]'); names.push(p.rooms[k] + ' × ' + (op ? op.textContent : k)); });
              combo = key; comboName = names.join(' + ');
              comboPrice = p.total ? L.ltotal + ' \u2066$' + p.total + '\u2069' : L.ask;
              stat.textContent = (p.available ? '' : L.availNo + ' ') + (p.total ? money(p.per_night) + ' ' + L.pernight + ' · ' + comboPrice : L.ask);
              stat.className = 'pick-stat ' + (p.available ? 'ok' : 'full');
              Array.prototype.forEach.call(avail.querySelectorAll('[data-combo]'), function (x) { x.setAttribute('aria-pressed', x.getAttribute('data-combo') === key ? 'true' : 'false'); });
              sum.textContent = partyLine(n); sum.hidden = false; wa.href = waUrl();
            }).catch(function () {});
        }, 250);
      }
      function pickKey() { var o = {}; Object.keys(pickState).sort().forEach(function (k) { if (pickState[k] > 0) { o[k] = pickState[k]; } }); return JSON.stringify(o); }
      j.rooms.forEach(function (r) {
        var free = j.free && j.free[r.id] != null ? j.free[r.id] : g, mx = Math.min(free, g);
        if (pickState[r.id] > mx) { pickState[r.id] = mx; }
        var row = d.createElement('div'); row.className = 'pick-row' + (mx <= 0 ? ' off' : '');
        var op = room.querySelector('option[value="' + r.id + '"]');
        var info = d.createElement('div'), nm = d.createElement('b'), sub = d.createElement('small');
        nm.textContent = op ? op.textContent : (L.lang === 'ar' ? r.name_ar : r.name_en);
        sub.textContent = L.upto.replace('{n}', r.cap) + ' · ' + (r.one_night ? money(r.one_night) + ' ' + L.pernight : L.ask) + (free < 3 && free > 0 && free !== g ? ' · ' + L.lleft.replace('{n}', free) : '');
        info.appendChild(nm); info.appendChild(sub);
        var st = d.createElement('div'); st.className = 'stepper';
        [['-1', '−'], ['0', ''], ['1', '+']].forEach(function (x) {
          if (x[0] === '0') { var v = d.createElement('output'); v.setAttribute('data-val', r.id); v.textContent = pickState[r.id] || 0; st.appendChild(v); return; }
          var b = d.createElement('button'); b.type = 'button'; b.textContent = x[1]; b.setAttribute('data-step', x[0]); b.setAttribute('data-id', r.id); b.setAttribute('data-max', mx);
          b.setAttribute('aria-label', (x[0] === '1' ? '+ ' : '− ') + nm.textContent);
          b.addEventListener('click', function () { pickState[r.id] = Math.max(0, (pickState[r.id] || 0) + +x[0]); refresh(); });
          st.appendChild(b);
        });
        row.appendChild(info); row.appendChild(st); box2.appendChild(row);
      });
      box2.appendChild(stat); pickRefresh = refresh; refresh();
      return box2;
    }
    function checkAvail() {
      var n = nights(), key = ci.value + '|' + co.value + '|' + room.value + '|' + guests.value;
      if (!api || n <= 0 || !window.fetch) { avail.hidden = true; lastCheck = ''; return; }
      if (key === lastCheck) { return; }
      lastCheck = key;
      fetch(api + 'quote?in=' + ci.value + '&out=' + co.value + '&guests=' + guests.value + '&room=' + encodeURIComponent(room.value), { credentials: 'omit' })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (key !== lastCheck || !j.ok) { return; }
          avail.textContent = '';
          var ok = room.value ? (j.rooms[0] && j.rooms[0].available) : j.open;
          if (room.value || !ok) { dropCombo(); }
          var head = d.createElement('b'); head.textContent = ok ? L.availYes : L.availNo; avail.appendChild(head);
          if (room.value && j.rooms[0]) {
            var tot = j.rooms[0].total, line = partyLine(n);
            var r0 = j.rooms[0], deal = r0.base_total ? ' (' + L.ofOff.replace('{p}', r0.offer_percent) + ' · ' + L.ofWas.replace('{p}', money(r0.base_total)) + ')' : '';
            sum.textContent = tot ? line + ' · ' + L.est.replace('{p}', money(tot)) + deal : line + ' · ' + L.ask; sum.hidden = false;
          }
          if (ok) {
            var list = j.rooms.filter(function (r) { return r.available; });
            if (room.value && list[0] && list[0].left !== null && list[0].left <= 3) {
              avail.appendChild(d.createTextNode(' ' + L.left.replace('{n}', list[0].left)));
            }
            var g0 = +guests.value;
            var single = room.value ? [] : j.rooms.filter(function (r) { return r.available && r.rooms === 1 && g0 >= (r.min || 1) && g0 <= r.cap; });
            if (!room.value && single.length) {
              // the party fits in one room: list only the room types made for that many guests, like a hotel booking engine
              dropCombo();
              single.sort(function (a, b) { return (a.total == null) - (b.total == null) || (a.total || 0) - (b.total || 0); });
              var sh1 = d.createElement('p'); sh1.className = 'opt-h'; sh1.textContent = L.fith.replace('{g}', g0); avail.appendChild(sh1);
              var ul1 = d.createElement('ul'); ul1.className = 'book-opts';
              single.forEach(function (r) {
                var li = d.createElement('li'), bt = d.createElement('button'); bt.type = 'button'; bt.setAttribute('data-pick', r.id);
                var op = room.querySelector('option[value="' + r.id + '"]'), nm = d.createElement('span'); nm.className = 'opt-name';
                var t1 = d.createElement('span'); t1.textContent = op ? op.textContent : r.id; nm.appendChild(t1);
                var t2 = d.createElement('small'); t2.textContent = L.upto.replace('{n}', r.cap) + (r.left != null && r.left <= 3 ? ' · ' + L.lleft.replace('{n}', r.left) : ''); nm.appendChild(t2);
                var pr = d.createElement('span'); pr.className = 'opt-price';
                if (r.total) {
                  var a1 = d.createElement('b'); a1.textContent = money(5 * Math.round(r.total / n / 5)); pr.appendChild(a1); pr.appendChild(d.createTextNode(' ' + L.pernight));
                  var a2 = d.createElement('small'); a2.textContent = L.ltotal + ' \u2066$' + r.total + '\u2069' + (r.base_total ? ' (' + L.ofOff.replace('{p}', r.offer_percent) + ')' : ''); pr.appendChild(a2);
                } else { pr.textContent = L.ask; }
                bt.appendChild(nm); bt.appendChild(pr); li.appendChild(bt); ul1.appendChild(li);
              });
              avail.appendChild(ul1);
            } else if (!room.value) {
              var ul = d.createElement('ul'); ul.className = 'book-opts';
              var opts = j.options || [];
              if (combo && !opts.some(function (o) { return JSON.stringify(o.rooms) === combo; })) { dropCombo(); }
              opts.forEach(function (o, i) {
                var li = d.createElement('li'), bt = d.createElement('button'), key = JSON.stringify(o.rooms);
                bt.type = 'button'; bt.setAttribute('data-combo', key); bt.setAttribute('aria-pressed', key === combo ? 'true' : 'false');
                var nm = d.createElement('span'), parts = []; nm.className = 'opt-name';
                Object.keys(o.rooms).forEach(function (k) {
                  var op = room.querySelector('option[value="' + k + '"]'), ln = d.createElement('span');
                  ln.textContent = o.rooms[k] + ' × ' + (op ? op.textContent : k); nm.appendChild(ln); parts.push(ln.textContent);
                });
                nm.setAttribute('data-label', parts.join(' + '));            // in the page's own language
                var pr = d.createElement('span'); pr.className = 'opt-price';
                if (o.total) {
                  var a1 = d.createElement('b'); a1.textContent = money(o.per_night); pr.appendChild(a1); pr.appendChild(d.createTextNode(' ' + L.pernight));
                  var a2 = d.createElement('small');
                  a2.textContent = L.ltotal + ' ' + (o.base_total ? '' : '') + '\u2066$' + o.total + '\u2069' + (o.base_total ? ' (' + L.ofOff.replace('{p}', o.offer_percent) + ')' : '');
                  pr.appendChild(a2);
                } else { pr.textContent = L.ask; }
                if (i === 0 && o.total) { var tag = d.createElement('em'); tag.textContent = L.lowest; nm.appendChild(tag); }
                bt.appendChild(nm); bt.appendChild(pr); li.appendChild(bt); ul.appendChild(li);
              });
              if (ul.children.length) { var sh = d.createElement('p'); sh.className = 'opt-h'; sh.textContent = L.sugh; avail.appendChild(sh); }
              avail.appendChild(ul);
              var pk = picker(j, n), tg = d.createElement('button'); tg.type = 'button'; tg.className = 'pick-toggle'; tg.textContent = L.picktoggle;
              pk.hidden = !pickOpen; tg.setAttribute('aria-expanded', pickOpen ? 'true' : 'false');
              tg.addEventListener('click', function () { pickOpen = !pickOpen; pk.hidden = !pickOpen; tg.setAttribute('aria-expanded', pickOpen ? 'true' : 'false'); });
              avail.appendChild(tg); avail.appendChild(pk);
              var keep = combo && ul.querySelector('[data-combo=\'' + combo + '\']');
              if (keep) { var ps = keep.querySelector('.opt-price small'); comboPrice = ps ? ps.textContent : ''; sum.textContent = partyLine(n); sum.hidden = false; }
            }
          }
          avail.className = 'book-avail ' + (ok ? 'is-ok' : 'is-full'); avail.hidden = false;
        }).catch(function () { avail.hidden = true; });
    }
    avail.addEventListener('click', function (e) {
      var bt = e.target.closest && e.target.closest('[data-pick]');
      if (bt) { room.value = bt.getAttribute('data-pick'); update(); return; }
      var cb = e.target.closest && e.target.closest('[data-combo]');
      if (cb) {
        combo = cb.getAttribute('data-combo'); comboName = cb.querySelector('.opt-name').getAttribute('data-label');
        pickState = JSON.parse(combo);
        if (pickRefresh) { pickRefresh(); }
        Array.prototype.forEach.call(avail.querySelectorAll('[data-combo]'), function (x) { x.setAttribute('aria-pressed', x === cb ? 'true' : 'false'); });
        var pr = cb.querySelector('.opt-price small'); comboPrice = pr ? pr.textContent : '';
        sum.textContent = partyLine(nights()); sum.hidden = false; wa.href = waUrl();
      }
    });
    var note = d.createElement('p'); note.className = 'book-note'; note.hidden = true; note.textContent = L.roomsnote;
    var rl = room.closest('label'); rl.parentNode.insertBefore(note, rl.nextSibling);
    function showNote() {
      note.hidden = roomsNeeded() < 2;
      var g = +guests.value;
      Array.prototype.forEach.call(room.options, function (o) {
        if (!o.value) { return; }
        var mx = +o.getAttribute('data-max') || 2, mn = +o.getAttribute('data-min') || 1, k = Math.ceil(g / mx);
        o.disabled = g < k * mn;                                   // e.g. a Triple Room is not offered to one guest
      });
      if (room.value && room.options[room.selectedIndex].disabled) { room.value = ''; }
    }
    showNote();
    function update() {
      showNote();
      if (ci.value) {
        var p = ci.value.split('-'), nx = new Date(+p[0], +p[1] - 1, +p[2] + 1);
        co.min = iso(nx);
        if (co.value && co.value <= ci.value) { co.value = iso(nx); }
      }
      wa.href = waUrl();
      if (mail) { mail.href = 'mailto:' + L.mail + '?subject=' + encodeURIComponent(L.subject) + '&body=' + encodeURIComponent(text()); }
      var n = nights();
      if (n > 0) {
        var line = partyLine(n), pr = rate();
        if (pr) { line += ' · ' + L.est.replace('{p}', money(pr * n)); }
        sum.textContent = line; sum.hidden = false;
      } else { sum.hidden = true; }
      checkAvail();
    }
    if (!err.id) { err.id = 'bk-err-' + Math.random().toString(36).slice(2, 8); }
    function clearBad() {
      each('.field', function (x) { x.classList.remove('bad'); }, box);
      each('[aria-invalid]', function (x) { x.removeAttribute('aria-invalid'); x.removeAttribute('aria-describedby'); }, box);
    }
    function fail(msg, fields) {
      err.textContent = msg; err.hidden = false; clearBad();
      var first = null;
      (fields || []).forEach(function (k) {
        var map = { dates: ['in', 'out'] }, names = map[k] || [k];
        names.forEach(function (nm) {
          var i = f(nm); if (!i) { return; }
          i.closest('.field').classList.add('bad'); i.setAttribute('aria-invalid', 'true'); i.setAttribute('aria-describedby', err.id);
          first = first || i;
        });
      });
      if (first) { first.focus(); }
    }
    function pastDate() { return ci.value && ci.value < iso(new Date()); }
    next.addEventListener('click', function () {
      if (nights() <= 0 || pastDate()) { fail(L.errDates, ['dates']); return; }
      err.hidden = true; clearBad();
      more.hidden = false; next.hidden = true; f('name').focus();
    });
    box.addEventListener('submit', function (e) {
      e.preventDefault();
      if (sending) { return; }
      var bad = [];
      if (nights() <= 0 || pastDate()) { bad.push('dates'); }
      if (f('name').value.trim().length < 2) { bad.push('name'); }
      if (f('phone').value.replace(/\D/g, '').length < 7) { bad.push('phone'); }
      if (f('email').value && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(f('email').value)) { bad.push('email'); }
      if (bad.length) { fail(bad[0] === 'dates' ? L.errDates : L.errFields, bad); return; }
      if (!api || !window.fetch) { window.open(waUrl(), '_blank', 'noopener'); return; }
      sending = true; send.disabled = true; err.hidden = true;
      var body = { lang: L.lang, 'in': ci.value, out: co.value, guests: +guests.value, room: room.value, combo: (!room.value && combo) ? combo : undefined, name: f('name').value.trim(),
                   phone: f('phone').value.trim(), email: f('email').value.trim(), message: f('message').value.trim(),
                   breakfast: f('breakfast').checked, website: f('website').value };
      fetch(api + 'booking', { method: 'POST', credentials: 'omit', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
        .then(function (r) { return r.json().then(function (j) { j.code = r.status; return j; }); })
        .then(function (j) {
          sending = false; send.disabled = false;
          if (j.ok) {
            box.querySelector('[data-ref]').textContent = j.ref;
            box.querySelector('[data-wa-done]').href = waUrl(j.ref);
            more.hidden = true; box.querySelector('.book-grid').hidden = true; sum.hidden = true; avail.hidden = true;
            done.hidden = false; done.focus();
            // on phones, open WhatsApp with the request already written, so the hotel receives it there too
            if (window.matchMedia && window.matchMedia('(pointer:coarse)').matches) { setTimeout(function () { window.location.href = waUrl(j.ref); }, 1200); }
          } else if (j.code === 429) { fail(L.errBusy); }
          else if (j.fields) { fail(L.errFields, j.fields); }
          else { fail(L.errNet); }
        })
        .catch(function () { sending = false; send.disabled = false; fail(L.errNet); });
    });
    box.addEventListener('input', update);
    box.addEventListener('change', update);
    update();
  });

  /* room prices: the hotel can change them in its panel, so the page refreshes them from the server.
     While an offer open to one-night stays runs today, every price shows the two-guest rate before and after the discount. */
  var TWO = { ar: 'لشخصين', en: 'for two', tr: '2 kişi' }[root.lang] || 'for two';
  var OCC = { triple: { ar: 'لـ3 أشخاص', en: 'for three', tr: '3 kişi' }, 'royal-family': { ar: 'حتى 4 أشخاص', en: 'up to four', tr: 'en fazla 4 kişi' } };
  function occ(id) { return (OCC[id] && OCC[id][root.lang]) || TWO; }        // whom the price is for
  function twoRate(rooms, id) {                                  // price per night for two guests, as the booking server counts it
    var p = rooms[id === 'royal-family' ? 'royal-suite' : id], sd = rooms.standard; if (!p || p.price == null) { return null; }
    if (id === 'standard') { return p.price2 || p.price; }
    if (id === 'royal-family') { var rs = rooms['royal-suite']; return (rs && rs.price != null && sd && sd.price != null) ? rs.price + (sd.price2 || sd.price) : null; }   // the suite with its connecting room
    return p.price;
  }
  function off5(p, pct) { return 5 * Math.round(p * (100 - pct) / 500); }   // offer prices to the nearest 5 dollars, as the server counts them
  function liveOffer(items) {
    var today = new Date(), t = today.getFullYear() + '-' + ('0' + (today.getMonth() + 1)).slice(-2) + '-' + ('0' + today.getDate()).slice(-2);
    return (items || []).filter(function (o) { return o.from <= t && t <= o.to && o.min_nights <= 1; })[0] || null;
  }
  var priceData = null;
  (function () {
    var tags = d.querySelectorAll('[data-rp]'), api = root.getAttribute('data-api');
    if (!api || !window.fetch) { return; }
    var get = function (u) { return fetch(api + u, { credentials: 'omit' }).then(function (r) { return r.json(); }).catch(function () { return {}; }); };
    priceData = Promise.all([get('prices'), get('offers')]).then(function (res) {
      var j = res[0], off = liveOffer(res[1].ok ? res[1].items : []);
      if (!j.ok) { return null; }
      Array.prototype.forEach.call(tags, function (t) {
        var id = t.getAttribute('data-rp'), p = j.rooms[id] || (id === 'royal-family' ? j.rooms['royal-suite'] : null); if (!p) { return; }
        var frm = t.getAttribute('data-frm'), per = t.getAttribute('data-per');
        t.textContent = '';
        if (off) {
          var r2 = twoRate(j.rooms, id);
          if (r2 == null) { var bq = d.createElement('b'); bq.textContent = t.getAttribute('data-ask'); t.appendChild(bq); return; }
          var was = d.createElement('s'); was.className = 'ltr was'; was.textContent = '$' + r2;
          var now = d.createElement('b'); now.className = 'ltr'; now.textContent = '$' + off5(r2, off.percent);
          t.appendChild(was); t.appendChild(d.createTextNode(' ')); t.appendChild(now);
          t.appendChild(d.createTextNode(' ' + occ(id) + (per ? ' ' + per : '')));
          return;
        }
        var p1 = p.price, p2 = p.price2, sd = j.rooms.standard;
        if (id === 'royal-family') { p = j.rooms['royal-suite'] || {}; p1 = p.price; p2 = null; }
        if (id === 'royal-family' && p1 != null) {                 // sold with its connecting room, charged at the Standard rate
          if (!sd || sd.price == null) { p1 = null; } else { p1 += (sd.price2 || sd.price); p2 = null; }
        }
        if (p1 == null) { var b0 = d.createElement('b'); b0.textContent = t.getAttribute('data-ask'); t.appendChild(b0); return; }
        var b = d.createElement('b'); b.className = 'ltr'; b.textContent = '$' + p1;
        if (t.hasAttribute('data-two') && p2) { b.textContent += ' / $' + p2; }
        if (frm) { t.appendChild(d.createTextNode(frm + ' ')); } t.appendChild(b); if (per) { t.appendChild(d.createTextNode(' ' + per)); }
      });
      return { rooms: j.rooms, offer: off };
    });
  })();

  /* phone booking bar: stays out of the way while the booking box itself is on screen */
  each('.mbar', function (bar) {
    var card = bar.parentNode.querySelector('[data-booking]');
    if (!card || !('IntersectionObserver' in window)) { bar.classList.add('show'); return; }
    new IntersectionObserver(function (en) { bar.classList.toggle('show', !en[en.length - 1].isIntersecting); }).observe(card);
  });

  /* videos: nothing is fetched until a clip is on screen; never on Save-Data, 2G or reduced motion */
  function source(v) {
    if (v.getAttribute('src')) { return; }
    var s = v.dataset.src;
    if (v.dataset.srcLand) { s = matchMedia('(min-aspect-ratio: 1/1)').matches ? v.dataset.srcLand : v.dataset.srcPort; }
    v.src = s;
  }
  function play(v) { source(v); var p = v.play(); if (p && p.catch) { p.catch(function () {}); } }
  function button(v) { return v.parentNode.querySelector('[data-vtoggle]') || v.parentNode.parentNode.querySelector('[data-vtoggle]'); }
  each('video[data-src],video[data-src-land]', function (v) {
    var b = button(v);
    v.addEventListener('playing', function () { v.classList.add('on'); if (b) { b.classList.add('playing'); b.setAttribute('aria-pressed', 'true'); } });
    v.addEventListener('pause', function () { if (b) { b.classList.remove('playing'); b.setAttribute('aria-pressed', 'false'); } });
  });
  d.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-vtoggle]');
    if (!b) { return; }
    var v = b.parentNode.querySelector('video');
    if (!v) { return; }
    if (v.paused) { delete v.dataset.stopped; play(v); } else { v.dataset.stopped = '1'; v.pause(); }
  });
  var vio = null;
  if (auto && 'IntersectionObserver' in window) {
    vio = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        var v = en.target;
        if (en.isIntersecting) { if (!v.dataset.stopped) { play(v); } } else if (!v.paused) { v.pause(); }
      });
    }, { threshold: 0.3 });
  }

  /* gentle fade-in for blocks that start below the fold */
  var rio = null;
  if (!calm && 'IntersectionObserver' in window) {
    rio = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.remove('pre'); rio.unobserve(en.target); }
      });
    }, { rootMargin: '0px 0px -7% 0px' });
  }

  function scan() {
    if (vio) {
      each('video[data-src]:not([data-seen]),video[data-src-land]:not([data-seen])', function (v) {
        if (v.offsetParent === null) { return; }
        v.setAttribute('data-seen', ''); vio.observe(v);
      });
    }
    if (rio) {
      each('.rv:not([data-seen])', function (el) {
        if (el.offsetParent === null) { return; }
        el.setAttribute('data-seen', '');
        if (el.getBoundingClientRect().top > window.innerHeight * 0.92) { el.classList.add('pre'); }
        rio.observe(el);
      });
    }
  }
  if (d.readyState === 'complete') { scan(); } else { window.addEventListener('load', scan); }

  /* scroll-linked motion: drifting hero and image bands, progress line under the header.
     The hero title itself never fades or shifts: it scrolls away with the page like any other text. */
  var ticking = false;
  function frame() {
    ticking = false;
    var vh = window.innerHeight, y = window.pageYOffset, full = root.scrollHeight - vh;
    each('[data-px]', function (el) {
      if (el.offsetParent === null) { return; }
      var r = el.parentNode.getBoundingClientRect();
      if (r.bottom < -120 || r.top > vh + 120) { return; }
      var c = (r.top + r.height / 2 - vh / 2) / vh;
      el.style.transform = 'translate3d(0,' + (c * parseFloat(el.getAttribute('data-px')) * -100).toFixed(1) + 'px,0)';
    });
    each('.prog', function (p) { p.style.transform = 'scaleX(' + (full > 0 ? Math.min(1, y / full) : 0).toFixed(4) + ')'; });
  }
  function onScroll() { if (!ticking) { ticking = true; window.requestAnimationFrame(frame); } }
  if (!calm) {
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    onScroll();
  }

  /* gallery: moves on its own while on screen, stops as soon as the visitor takes over */
  each('[data-auto]', function (rail) {
    var timer = null, hold = false;
    function step() {
      if (hold || rail.offsetParent === null || !rail.firstElementChild) { return; }
      var dir = getComputedStyle(rail).direction === 'rtl' ? -1 : 1;
      var w = rail.firstElementChild.getBoundingClientRect().width + 16;
      if (Math.abs(rail.scrollLeft) >= rail.scrollWidth - rail.clientWidth - 4) { rail.scrollTo({ left: 0, behavior: 'smooth' }); }
      else { rail.scrollBy({ left: w * dir, behavior: 'smooth' }); }
    }
    ['pointerdown', 'touchstart', 'mouseenter', 'focusin'].forEach(function (ev) { rail.addEventListener(ev, function () { hold = true; }, { passive: true }); });
    ['mouseleave', 'focusout'].forEach(function (ev) { rail.addEventListener(ev, function () { hold = false; }); });
    if (auto && 'IntersectionObserver' in window) {
      new IntersectionObserver(function (en) {
        if (en[en.length - 1].isIntersecting) { if (!timer) { timer = setInterval(step, 3200); } }
        else { clearInterval(timer); timer = null; }
      }, { threshold: 0.4 }).observe(rail);
    }
  });

  /* room carousel arrows */
  d.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-rail]');
    if (!b) { return; }
    var rail = d.getElementById(b.getAttribute('aria-controls'));
    if (!rail || !rail.firstElementChild) { return; }
    var step = rail.firstElementChild.getBoundingClientRect().width + 16;
    var dir = getComputedStyle(rail).direction === 'rtl' ? -1 : 1;
    rail.scrollBy({ left: step * dir * (b.getAttribute('data-rail') === 'next' ? 1 : -1), behavior: calm ? 'auto' : 'smooth' });
  });

  /* booking status check: booking number + last four phone digits */
  [].forEach.call(d.querySelectorAll('[data-status]'), function (f) {
    var out = f.querySelector('[data-out]'), L = f.dataset, busy = false;
    function show(cls, lines) {
      out.textContent = ''; out.className = 'status-out ' + cls;
      lines.forEach(function (t, i) { var p = d.createElement(i ? 'p' : 'strong'); p.textContent = t; out.appendChild(p); });
      out.hidden = false;
    }
    f.addEventListener('submit', function (ev) {
      ev.preventDefault();
      if (busy) { return; }
      var ref = f.ref.value.trim().toUpperCase().replace(/\s+/g, ''), ph = f.phone.value.replace(/\D/g, '');
      if (ref && ref.indexOf('RI-') !== 0) { ref = 'RI-' + ref.replace(/^RI/, ''); }
      if (!/^RI-[A-Z0-9]{6}$/.test(ref) || ph.length !== 4) { return show('bad', [L.none]); }
      busy = true;
      fetch(L.api + 'booking/status?ref=' + encodeURIComponent(ref) + '&phone=' + ph, { headers: { Accept: 'application/json' } })
        .then(function (r) { return r.json().then(function (j) { j.http = r.status; return j; }); })
        .then(function (j) {
          busy = false;
          if (j.http === 429) { return show('bad', [L.busy]); }
          if (!j.ok) { return show('bad', [L.none]); }
          var opt = j.room && d.querySelector('[data-booking] select[name=room] option[value="' + j.room + '"]');
          var line = [j['in'] + ' → ' + j.out, j.nights + ' ' + L.nights, j.guests + ' ' + L.guests];
          if (opt) { line.push(opt.textContent); }
          var lines = [L[j.status] || L['new'], j.ref + ' · ' + line.join(' · ')];
          if (j.total) { lines.push(L.total + ': $' + j.total); }
          show(j.status === 'confirmed' ? 'ok' : j.status === 'cancelled' ? 'bad' : 'wait', lines);
          if (j.status === 'confirmed' && L.wa) {
            var a = d.createElement('a'); a.className = 'btn btn-line'; a.target = '_blank'; a.rel = 'noopener';
            a.href = 'https://wa.me/' + L.wa + '?text=' + encodeURIComponent(L.extendMsg.replace('{ref}', j.ref)); a.textContent = L.extend; out.appendChild(a);
          }
        })
        .catch(function () { busy = false; show('bad', [L.none]); });
    });
  });

  /* the hotel's own offer, when one is set in the panel: a bar above the header and a block on the booking form */
  (function () {
    var bar = d.querySelector('[data-promo-bar]'), boxes = d.querySelectorAll('[data-promo]');
    var api = root.getAttribute('data-api');
    if (!api || !window.fetch || (!bar && !boxes.length)) { return; }
    function dm(s) { var p = s.split('-'); return p[2] + '/' + p[1]; }
    fetch(api + 'offers', { credentials: 'omit' }).then(function (r) { return r.json(); }).then(function (j) {
      var o = j.ok && j.items && j.items[0];
      if (!o) { return; }
      var ar = root.lang === 'ar', title = (ar ? o.title_ar : root.lang === 'en' ? o.title_en : '') || '';   // other languages use their own wording
      if (bar) {
        var B = bar.dataset, b1 = d.createElement('b'), s1 = d.createElement('span'), c1 = d.createElement('u');
        b1.textContent = B.off.replace('{p}', o.percent);
        s1.textContent = (title || B.lead) + ' · ' + B.dates.replace('{a}', dm(o.from)).replace('{b}', dm(o.to));
        c1.textContent = B.cta;
        bar.appendChild(b1); bar.appendChild(s1); bar.appendChild(c1); bar.hidden = false;
      }
      [].forEach.call(boxes, function (box) {
        var L = box.closest('[data-booking]').dataset;
        var n = d.createElement('strong'), w = d.createElement('div'), h = d.createElement('b'), p1 = d.createElement('span'), p2 = d.createElement('small');
        n.textContent = L.ofOff.replace('{p}', o.percent);
        h.textContent = title || L.ofLead;
        p1.textContent = L.ofDates.replace('{a}', dm(o.from)).replace('{b}', dm(o.to)) + (o.min_nights > 1 ? ' · ' + L.ofMin.replace('{n}', o.min_nights) : '');
        p2.textContent = L.ofNote;
        w.appendChild(h); w.appendChild(p1); w.appendChild(p2); box.appendChild(n); box.appendChild(w); box.hidden = false;
        if (priceData) {
          priceData.then(function (pd) {
            if (!pd || !pd.offer || pd.offer.percent !== o.percent) { return; }
            var ul = d.createElement('ul'); ul.className = 'promo-rates';
            var sel = box.closest('[data-booking]').querySelector('[name=room]');
            ['standard', 'triple', 'junior-suite', 'royal-suite', 'royal-family'].forEach(function (id) {
              var r2 = twoRate(pd.rooms, id); if (r2 == null) { return; }
              var op = sel && sel.querySelector('option[value="' + id + '"]');
              var li = d.createElement('li'), nm = d.createElement('span'), pr = d.createElement('span');
              nm.textContent = (op ? op.textContent : id) + ' · ' + occ(id);
              var s0 = d.createElement('s'); s0.className = 'ltr'; s0.textContent = '$' + r2;
              var b0 = d.createElement('b'); b0.className = 'ltr'; b0.textContent = '$' + off5(r2, o.percent);
              pr.appendChild(s0); pr.appendChild(d.createTextNode(' ')); pr.appendChild(b0);
              li.appendChild(nm); li.appendChild(pr); ul.appendChild(li);
            });
            if (ul.children.length) { w.appendChild(ul); }
          });
        }
      });
    }).catch(function () {});
  })();

  /* business requests (groups, companies, agencies, weddings): saved on the hotel's server, WhatsApp as fallback */
  each('[data-inquiry]', function (f) {
    var D = f.dataset, err = f.querySelector('[data-err]'), done = f.querySelector('[data-done]');
    var kind = f.querySelector('[name=kind]');
    var h = (location.hash || '').replace('#', '');
    if (h && kind.querySelector('option[value="' + h + '"]')) { kind.value = h; }
    function val(n) { var x = f.querySelector('[name=' + n + ']'); return x ? x.value.trim() : ''; }
    function summary() {
      var k = kind.options[kind.selectedIndex].text, parts = [D.waMsg, k + ': ' + val('name')];
      ['company', 'phone', 'date', 'guests', 'rooms', 'nights', 'message'].forEach(function (n) { if (val(n)) { parts.push(val(n)); } });
      return parts.join('\n');
    }
    f.addEventListener('submit', function (e) {
      e.preventDefault();
      err.hidden = true;
      each('.field.bad', function (x) { x.classList.remove('bad'); }, f);
      var bad = [];
      if (val('name').length < 2) { bad.push('name'); }
      if (val('phone').replace(/\D/g, '').length < 7) { bad.push('phone'); }
      if (bad.length) {
        bad.forEach(function (n) { f.querySelector('[name=' + n + ']').closest('.field').classList.add('bad'); });
        err.textContent = D.errFields; err.hidden = false; return;
      }
      var waHref = 'https://wa.me/' + D.wa + '?text=' + encodeURIComponent(summary());
      if (!D.api || !window.fetch) { window.open(waHref, '_blank', 'noopener'); return; }
      var body = { lang: D.lang, website: val('website') };
      ['kind', 'name', 'company', 'phone', 'email', 'date', 'guests', 'rooms', 'nights', 'message'].forEach(function (n) { body[n] = val(n); });
      var btn = f.querySelector('[data-send]'); btn.disabled = true;
      fetch(D.api + 'inquiry', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
        .then(function (r) { return r.json().then(function (j) { j.status = r.status; return j; }); })
        .then(function (j) {
          btn.disabled = false;
          if (j.ok) {
            each('.field, .inq-actions', function (x) { x.hidden = true; }, f);
            f.querySelector('[data-ref]').textContent = '#' + j.id;
            f.querySelector('[data-wa-done]').href = 'https://wa.me/' + D.wa + '?text=' + encodeURIComponent(summary() + '\n' + D.doneId + ': #' + j.id);
            done.hidden = false; done.focus();
          } else if (j.error === 'fields' && j.fields) {
            j.fields.forEach(function (n) { var x = f.querySelector('[name=' + n + ']'); if (x) { x.closest('.field').classList.add('bad'); } });
            err.textContent = D.errFields; err.hidden = false;
          } else { err.textContent = j.status === 429 ? D.errBusy : D.errNet; err.hidden = false; }
        })
        .catch(function () { btn.disabled = false; err.textContent = D.errNet; err.hidden = false; });
    });
  });

  window.RoyalInn = { scan: function () { scan(); onScroll(); } };
})();

/* room photo sliders: live "n / total" counter */
(function () {
  var els = document.querySelectorAll('[data-slides]');
  for (var i = 0; i < els.length; i++) {
    (function (tr) {
      var n = tr.parentNode.querySelector('.rm-n b'); if (!n) { return; }
      var tick = 0;
      tr.addEventListener('scroll', function () {
        if (tick) { return; }
        tick = requestAnimationFrame(function () {
          tick = 0;
          var w = tr.children[0] ? tr.children[0].getBoundingClientRect().width + 10 : 1;
          n.textContent = Math.min(tr.children.length, Math.round(Math.abs(tr.scrollLeft) / w) + 1);
        });
      }, { passive: true });
    })(els[i]);
  }
})();
