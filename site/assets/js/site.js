/* Royal Inn Tartus — small helpers. No tracking, no cookies, no network requests. */
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

  /* booking request: build the WhatsApp / email text on the device, nothing is sent from the page itself */
  each('[data-booking]', function (box) {
    var ci = box.querySelector('[name=in]'), co = box.querySelector('[name=out]');
    var guests = box.querySelector('[name=guests]'), room = box.querySelector('[name=room]');
    var wa = box.querySelector('[data-wa-link]'), mail = box.querySelector('[data-mail-link]'), sum = box.querySelector('[data-sum]');
    var L = box.dataset, now = new Date();
    ci.min = iso(now);
    co.min = iso(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1));
    function update() {
      if (ci.value) {
        var p = ci.value.split('-'), next = new Date(+p[0], +p[1] - 1, +p[2] + 1);
        co.min = iso(next);
        if (co.value && co.value <= ci.value) { co.value = iso(next); }
      }
      var lines = [L.hello], nights = 0;
      if (ci.value) { lines.push(L.lin + ': ' + ci.value); }
      if (co.value) { lines.push(L.lout + ': ' + co.value); }
      if (ci.value && co.value) {
        var a = ci.value.split('-'), b = co.value.split('-');
        nights = Math.round((Date.UTC(+b[0], +b[1] - 1, +b[2]) - Date.UTC(+a[0], +a[1] - 1, +a[2])) / 864e5);
      }
      if (nights > 0) { lines.push(L.lnights + ': ' + nights); }
      lines.push(L.lguests + ': ' + guests.value);
      if (room.value) { lines.push(L.lroom + ': ' + room.options[room.selectedIndex].text); }
      var text = lines.join('\n');
      wa.href = 'https://wa.me/' + L.wa + '?text=' + encodeURIComponent(text);
      if (mail) { mail.href = 'mailto:' + L.mail + '?subject=' + encodeURIComponent(L.subject) + '&body=' + encodeURIComponent(text); }
      if (sum) {
        if (nights > 0) {
          var line = L.lnights + ': ' + nights + ' · ' + L.lguests + ': ' + guests.value;
          var price = room.value ? +room.options[room.selectedIndex].getAttribute('data-price') : 0;
          if (price) { line += ' · ' + L.est.replace('{p}', '$' + price * nights); }
          sum.textContent = line; sum.hidden = false;
        } else { sum.hidden = true; }
      }
    }
    box.addEventListener('input', update);
    box.addEventListener('change', update);
    update();
  });

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

  window.RoyalInn = { scan: function () { scan(); onScroll(); } };
})();
