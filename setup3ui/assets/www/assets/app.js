/* MediaFlow Gateway — shared client behaviour.
   No third-party code, no network calls except the same-origin /health probe. */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  /* ---------- mobile nav ---------- */
  var burger = $('burger'), menu = $('menu');
  if (burger && menu) {
    burger.addEventListener('click', function () { menu.classList.toggle('open'); });
  }

  /* ---------- footer year + active nav ---------- */
  Array.prototype.forEach.call(document.querySelectorAll('[data-year]'), function (el) {
    el.textContent = new Date().getFullYear();
  });

  /* нормализуем: /platform, /platform.html и platform.html — одно и то же */
  var norm = function (p) {
    return (String(p).split('?')[0].split('/').pop() || 'index').replace(/\.html$/, '') || 'index';
  };
  var here = norm(location.pathname);
  Array.prototype.forEach.call(document.querySelectorAll('nav.menu a'), function (a) {
    var href = a.getAttribute('href');
    if (href && norm(href) === here) a.setAttribute('aria-current', 'page');
  });

  /* ---------- live gateway probe ---------- */
  var badge = $('badge');
  if (badge) {
    var headline = $('headline'), stamp = $('stamp'), rtt = $('rtt'),
        svc = $('svc'), ver = $('ver'), raw = $('raw'),
        pillDot = $('pillDot'), pillText = $('pillText'), counter = $('count'),
        auto = $('auto'), edge = $('edge');

    var timer = null, hits = 0, fails = 0;

    function setBadge(cls, text) { badge.className = 'badge ' + cls; badge.textContent = text; }

    function probe() {
      var t0 = performance.now();
      headline.textContent = 'Contacting gateway…';

      fetch('/health', { cache: 'no-store' })
        .then(function (r) {
          return r.text().then(function (body) { return { status: r.status, body: body }; });
        })
        .then(function (res) {
          var ms = Math.round(performance.now() - t0);
          var data = null;
          try { data = JSON.parse(res.body); } catch (e) { /* not json */ }

          hits++;
          rtt.textContent = ms + ' ms';
          raw.textContent = JSON.stringify(data !== null ? data : res.body, null, 2);

          var good = res.status === 200 && data && data.status === 'ok';
          setBadge(good ? 'ok' : 'err', good ? 'operational' : 'HTTP ' + res.status);
          headline.textContent = good
            ? 'Gateway is serving requests'
            : 'Gateway returned an unexpected response';

          if (data) {
            svc.textContent = data.service || '—';
            ver.textContent = data.version || '—';
            if (edge) edge.textContent = 'edge: ' + (data.service || 'gateway') + ' ' + (data.version || '');
          }
          if (pillDot) pillDot.style.background = good ? 'var(--ok)' : 'var(--err)';
          if (pillText) pillText.textContent = good ? 'All systems operational' : 'Degraded response';
        })
        .catch(function () {
          fails++;
          setBadge('err', 'unreachable');
          headline.textContent = 'Could not reach the gateway';
          raw.textContent = 'request failed';
          rtt.textContent = '—';
          if (pillDot) pillDot.style.background = 'var(--err)';
          if (pillText) pillText.textContent = 'Status probe failed';
        })
        .then(function () {
          stamp.textContent = new Date().toLocaleTimeString();
          counter.textContent = 'ok ' + hits + ' / fail ' + fails;
        });
    }

    function schedule() {
      if (timer) clearInterval(timer);
      if (auto.checked) timer = setInterval(probe, 15000);
    }

    var refresh = $('refresh');
    if (refresh) refresh.addEventListener('click', probe);
    if (auto) auto.addEventListener('change', schedule);

    probe();
    schedule();
  }

  /* ---------- edge region tables ---------- */
  var rows = $('rows');
  if (rows) {
    var REGIONS = {
      eu:   [['Frankfurt', 97.4, 11, 28], ['Amsterdam', 96.8, 13, 31], ['Warsaw', 94.1, 18, 44], ['Stockholm', 95.6, 16, 37]],
      cis:  [['Moscow', 98.2, 7, 19], ['Saint Petersburg', 97.1, 9, 24], ['Kyiv', 93.4, 21, 52], ['Almaty', 91.8, 29, 68]],
      apac: [['Singapore', 96.3, 15, 34], ['Tokyo', 95.9, 17, 39], ['Mumbai', 92.7, 24, 58], ['Sydney', 94.5, 19, 41]],
      us:   [['Ashburn', 97.9, 9, 22], ['Chicago', 96.4, 14, 33], ['Dallas', 95.2, 17, 40], ['Los Angeles', 94.8, 19, 45]]
    };

    var paint = function (key) {
      rows.innerHTML = (REGIONS[key] || []).map(function (r) {
        return '<tr><td>' + r[0] + '</td>' +
               '<td class="num">' + r[1].toFixed(1) + '%</td>' +
               '<td class="num">' + r[2] + ' ms</td>' +
               '<td class="num">' + r[3] + ' ms</td>' +
               '<td><div class="bar"><i style="width:' + Math.min(100, Math.round(r[3] / 80 * 100)) + '%"></i></div></td></tr>';
      }).join('');
    };

    var tabs = document.querySelectorAll('.tab');
    Array.prototype.forEach.call(tabs, function (t) {
      t.addEventListener('click', function () {
        Array.prototype.forEach.call(tabs, function (o) {
          o.setAttribute('aria-selected', o === t ? 'true' : 'false');
        });
        paint(t.getAttribute('data-r'));
      });
    });
    paint('eu');
  }

  /* ---------- contact form (demo only) ---------- */
  var form = $('contact');
  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var note = $('contactNote');
      if (note) {
        note.textContent = 'Thanks — your message has been queued. Our team replies within one business day.';
        note.style.color = 'var(--ok)';
      }
      form.reset();
    });
  }
})();
