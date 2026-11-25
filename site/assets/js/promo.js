/* Royal Inn: the offer bar is filled before first paint (from the last answer the visitor's browser saw, else the
   offer known when the site was built), so the page does not jump when the live answer arrives. */
(function () {
  'use strict';
  window.riPromoBar = function (bar, o) {
    var d = document, B = bar.dataset, lang = d.documentElement.lang;
    function dm(s) { var p = s.split('-'); return p[2] + '/' + p[1]; }
    var title = (lang === 'ar' ? o.title_ar : lang === 'en' ? o.title_en : '') || '';
    var key = [o.percent, o.from, o.to, title].join('|');
    if (bar.getAttribute('data-shown') === key) { return; }
    bar.textContent = '';
    var b1 = d.createElement('b'), s1 = d.createElement('span'), c1 = d.createElement('u');
    b1.textContent = B.off.replace('{p}', o.percent);
    s1.textContent = (title || B.lead) + ' · ' + B.dates.replace('{a}', dm(o.from)).replace('{b}', dm(o.to));
    c1.textContent = B.cta;
    bar.appendChild(b1); bar.appendChild(s1); bar.appendChild(c1);
    bar.setAttribute('data-shown', key); bar.hidden = false;
  };
  var bar = document.currentScript && document.currentScript.previousElementSibling, o = null, seen = false;
  if (!bar || !bar.hasAttribute('data-promo-bar')) { return; }
  try { var c = localStorage.getItem('ri_offer'); if (c !== null) { o = JSON.parse(c); seen = true; } } catch (e) { o = null; }
  if (!seen) { try { o = JSON.parse(bar.getAttribute('data-default') || 'null'); } catch (e2) { o = null; } }
  var today = new Date().toISOString().slice(0, 10);
  if (o && o.percent && o.from && o.to && o.to >= today) { window.riPromoBar(bar, o); }
})();
