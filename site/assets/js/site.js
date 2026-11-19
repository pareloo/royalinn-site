/* Royal Inn Tartus — small helpers. No tracking, no cookies; the only requests go to the hotel's own booking server. */
(function () {
  'use strict';
  var d = document, root = d.documentElement;
  root.classList.add('js');
  var conn = navigator.connection || {};
  var lite = !!conn.saveData || /(^|-)2g$/.test(conn.effectiveType || '');
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
    ci.min = iso(now);
    co.min = iso(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1));
    function nights() {
      if (!ci.value || !co.value) { return 0; }
      var a = ci.value.split('-'), b = co.value.split('-');
      return Math.round((Date.UTC(+b[0], +b[1] - 1, +b[2]) - Date.UTC(+a[0], +a[1] - 1, +a[2])) / 864e5);
    }
    function rate() {
      var o = room.value ? room.options[room.selectedIndex] : null;
      if (!o) { return 0; }
      var p1 = +o.getAttribute('data-price') || 0, p2 = +o.getAttribute('data-price2') || 0;
      return (p2 && +guests.value >= 2) ? p2 : p1;
    }
    function text(ref) {
      var lines = [L.hello], n = nights();
      if (ref) { lines.push(L.lref + ': ' + ref); }
      if (f('name').value.trim()) { lines.push(L.lname + ': ' + f('name').value.trim()); }
      if (ci.value) { lines.push(L.lin + ': ' + ci.value); }
      if (co.value) { lines.push(L.lout + ': ' + co.value); }
      if (n > 0) { lines.push(L.lnights + ': ' + n); }
      lines.push(L.lguests + ': ' + guests.value);
      if (room.value) { lines.push(L.lroom + ': ' + room.options[room.selectedIndex].text); }
      if (f('breakfast').checked) { lines.push(L.lbf + ': ' + L.yes); }
      if (f('message').value.trim()) { lines.push(f('message').value.trim()); }
      return lines.join('\n');
    }
    function waUrl(ref) { return 'https://wa.me/' + L.wa + '?text=' + encodeURIComponent(text(ref)); }
    function money(n) { return '$' + n; }
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
          var head = d.createElement('b'); head.textContent = ok ? L.availYes : L.availNo; avail.appendChild(head);
          if (room.value && j.rooms[0]) {
            var tot = j.rooms[0].total, line = L.lnights + ': ' + n + ' · ' + L.lguests + ': ' + guests.value;
            var r0 = j.rooms[0], deal = r0.base_total ? ' (' + L.ofOff.replace('{p}', r0.offer_percent) + ' · ' + L.ofWas.replace('{p}', money(r0.base_total)) + ')' : '';
            sum.textContent = tot ? line + ' · ' + L.est.replace('{p}', money(tot)) + deal : line + ' · ' + L.ask; sum.hidden = false;
          }
          if (ok) {
            var list = j.rooms.filter(function (r) { return r.available; });
            if (room.value && list[0] && list[0].left !== null && list[0].left <= 3) {
              avail.appendChild(d.createTextNode(' ' + L.left.replace('{n}', list[0].left)));
            }
            if (!room.value) {
              var ul = d.createElement('ul'); ul.className = 'book-opts';
              list.forEach(function (r) {
                var li = d.createElement('li'), bt = d.createElement('button');
                bt.type = 'button'; bt.setAttribute('data-pick', r.id);
                var nm = d.createElement('span'), op = room.querySelector('option[value="' + r.id + '"]'); nm.textContent = op ? op.textContent : (L.lang === 'ar' ? r.name_ar : r.name_en);
                var pr = d.createElement('b');
                if (r.total && r.base_total) { var was = d.createElement('s'); was.textContent = money(r.base_total); pr.appendChild(was); pr.appendChild(d.createTextNode(' ' + money(r.total))); }
                else { pr.textContent = r.total ? money(r.total) : L.ask; }
                bt.appendChild(nm); bt.appendChild(pr); li.appendChild(bt); ul.appendChild(li);
              });
              avail.appendChild(ul);
            }
          }
          avail.className = 'book-avail ' + (ok ? 'is-ok' : 'is-full'); avail.hidden = false;
        }).catch(function () { avail.hidden = true; });
    }
    avail.addEventListener('click', function (e) {
      var bt = e.target.closest && e.target.closest('[data-pick]');
      if (bt) { room.value = bt.getAttribute('data-pick'); update(); }
    });
    function update() {
      if (ci.value) {
        var p = ci.value.split('-'), nx = new Date(+p[0], +p[1] - 1, +p[2] + 1);
        co.min = iso(nx);
        if (co.value && co.value <= ci.value) { co.value = iso(nx); }
      }
      wa.href = waUrl();
      if (mail) { mail.href = 'mailto:' + L.mail + '?subject=' + encodeURIComponent(L.subject) + '&body=' + encodeURIComponent(text()); }
      var n = nights();
      if (n > 0) {
        var line = L.lnights + ': ' + n + ' · ' + L.lguests + ': ' + guests.value, pr = rate();
        if (pr) { line += ' · ' + L.est.replace('{p}', '$' + pr * n); }
        sum.textContent = line; sum.hidden = false;
      } else { sum.hidden = true; }
      checkAvail();
    }
    function fail(msg, fields) {
      err.textContent = msg; err.hidden = false;
      each('.field', function (x) { x.classList.remove('bad'); }, box);
      (fields || []).forEach(function (k) {
        var map = { dates: ['in', 'out'] }, names = map[k] || [k];
        names.forEach(function (nm) { var i = f(nm); if (i) { i.closest('.field').classList.add('bad'); } });
      });
    }
    next.addEventListener('click', function () {
      if (nights() <= 0) { fail(L.errDates, ['dates']); (ci.value ? co : ci).focus(); return; }
      err.hidden = true; each('.field', function (x) { x.classList.remove('bad'); }, box);
      more.hidden = false; next.hidden = true; f('name').focus();
    });
    box.addEventListener('submit', function (e) {
      e.preventDefault();
      if (sending) { return; }
      var bad = [];
      if (nights() <= 0) { bad.push('dates'); }
      if (f('name').value.trim().length < 2) { bad.push('name'); }
      if (f('phone').value.replace(/\D/g, '').length < 7) { bad.push('phone'); }
      if (f('email').value && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(f('email').value)) { bad.push('email'); }
      if (bad.length) { fail(bad[0] === 'dates' ? L.errDates : L.errFields, bad); return; }
      if (!api || !window.fetch) { window.open(waUrl(), '_blank', 'noopener'); return; }
      sending = true; send.disabled = true; err.hidden = true;
      var body = { lang: L.lang, 'in': ci.value, out: co.value, guests: +guests.value, room: room.value, name: f('name').value.trim(),
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

  /* room prices: the hotel can change them in its panel, so the page refreshes them from the server */
  (function () {
    var tags = d.querySelectorAll('[data-rp]'), api = root.getAttribute('data-api');
    if (!tags.length || !api || !window.fetch) { return; }
    fetch(api + 'prices', { credentials: 'omit' }).then(function (r) { return r.json(); }).then(function (j) {
      if (!j.ok) { return; }
      Array.prototype.forEach.call(tags, function (t) {
        var p = j.rooms[t.getAttribute('data-rp')]; if (!p) { return; }
        t.textContent = '';
        if (p.price == null) { var b0 = d.createElement('b'); b0.textContent = t.getAttribute('data-ask'); t.appendChild(b0); return; }
        var b = d.createElement('b'); b.className = 'ltr'; b.textContent = '$' + p.price;
        t.appendChild(d.createTextNode(t.getAttribute('data-frm') + ' ')); t.appendChild(b); t.appendChild(d.createTextNode(' ' + t.getAttribute('data-per')));
      });
    }).catch(function () {});
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
      var ar = root.lang === 'ar', title = (ar ? o.title_ar : o.title_en) || '';
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
      });
    }).catch(function () {});
  })();

  window.RoyalInn = { scan: function () { scan(); onScroll(); } };
})();
