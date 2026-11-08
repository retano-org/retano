/* The animated "stages": small framed canvases beside the text, where customers (little people)
   act out one idea each. Nothing moves behind text, only stages on screen are animated, and
   visitors who ask their system for reduced motion get a still picture.

     attract   hero: customers who stopped buying get a message and come back to the store; the
               card beside them tells one customer's story (Sara, or the one under the mouse)
     hundred   why: out of 100 first-time buyers, 65 never come back, and Retano finds them
     audience  calculator: 200 SMS recipients; how many buy follows the conversion rate
     crowd     solutions: customers sort themselves into three groups; the chosen one lights up
     loop      before/after: customers who drifted away rejoin the circle as the slider moves
     falling   lost sales: every month some customers quietly drop away
     retained  final call: loyal customers circling the store, Sara among them

   Each stage is an element with data-stage="<name>" holding a <canvas>, and sometimes a few HTML
   labels the scene places. landing.js passes values in through window.RetanoScene (rate, order,
   seg, compare, lost, lostN, avg) and calls RetanoScene.refresh(). Every customer is one little
   person, and the colours mean the same everywhere: teal bought again, amber at risk, grey stopped
   buying, rose lost. */
(function () {
  'use strict';

  var S = window.RetanoScene = window.RetanoScene || {};
  var reduce = !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);
  var TAU = Math.PI * 2;
  var COLOR = { sleep: '#C3CCD8', teal: '#00A6A2', amber: '#F5A524', rose: '#F2547D', white: '#FFFFFF', gold: '#FFD27A' };
  var fa = new Intl.NumberFormat('fa-IR');
  var fa1 = new Intl.NumberFormat('fa-IR', { maximumFractionDigits: 1 });

  // ---------- Helpers ----------
  function rng(seed) {
    return function () {
      seed = seed + 0x6D2B79F5 | 0;
      var t = Math.imul(seed ^ seed >>> 15, 1 | seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  function clamp01(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }
  function smooth(v) { v = clamp01(v); return v * v * (3 - 2 * v); }
  function inOut(v) { v = clamp01(v); return v < .5 ? 4 * v * v * v : 1 - Math.pow(-2 * v + 2, 3) / 2; }
  function lerp(a, b, k) { return a + (b - a) * k; }
  // Moves a value toward its target without overshooting; frame-rate independent.
  function approach(cur, target, rate, dt) {
    var next = cur + (target - cur) * (1 - Math.exp(-rate * dt));
    return Math.abs(target - next) < .002 ? target : next;
  }
  function shuffled(n, rand) {
    var a = [], i, j, t;
    for (i = 0; i < n; i++) a.push(i);
    for (i = n - 1; i > 0; i--) { j = Math.floor(rand() * (i + 1)); t = a[i]; a[i] = a[j]; a[j] = t; }
    return a;
  }
  function rgba(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + a + ')';
  }

  // ---------- Little people, drawn once per colour and pixel size ----------
  var sprites = {};
  function sprite(color, px, halo) {
    var key = color + px + (halo ? 'h' : '');
    var c = sprites[key];
    if (c) return c;
    var box = px * 2;
    c = document.createElement('canvas');
    c.width = c.height = box;
    var g = c.getContext('2d'), cx = box / 2, top = (box - px) / 2 - px * .05;
    if (halo) {
      var rg = g.createRadialGradient(cx, box / 2, 0, cx, box / 2, box / 2);
      rg.addColorStop(0, rgba(color, .32));
      rg.addColorStop(1, rgba(color, 0));
      g.fillStyle = rg;
      g.fillRect(0, 0, box, box);
    }
    g.fillStyle = color;
    g.beginPath();
    g.arc(cx, top + px * .27, px * .2, 0, TAU);
    g.fill();
    g.beginPath();
    g.moveTo(cx - px * .37, top + px);
    g.bezierCurveTo(cx - px * .37, top + px * .64, cx - px * .2, top + px * .53, cx, top + px * .53);
    g.bezierCurveTo(cx + px * .2, top + px * .53, cx + px * .37, top + px * .64, cx + px * .37, top + px);
    g.closePath();
    g.fill();
    sprites[key] = c;
    return c;
  }
  function person(st, x, y, size, color, alpha, halo) {
    if (alpha <= .01) return;
    var img = sprite(color, Math.max(4, Math.round(size * st.dpr)), halo), d = img.width / st.dpr;
    st.ctx.globalAlpha = alpha > 1 ? 1 : alpha;
    st.ctx.drawImage(img, x - d / 2, y - d / 2, d, d);
  }
  // A person part-way between two colours (k = 0 is "from", 1 is "to").
  function mix(st, x, y, size, from, to, k, alpha, haloTo) {
    if (k < .99) person(st, x, y, size, from, (1 - k) * alpha, false);
    if (k > .01) person(st, x, y, size, to, k * alpha, haloTo);
  }

  // "1.8 million" as its number and its unit, so the number can sit in its own left-to-right run.
  function shortMoney(v) {
    if (v >= 1e9) return [fa1.format(v / 1e9), 'میلیارد'];
    if (v >= 1e6) return [fa1.format(v / 1e6), 'میلیون'];
    return [fa.format(Math.round(v / 1e3)), 'هزار'];
  }
  // Writes "+1.8 million" (or "−…") into a node, keeping the sign beside its number.
  function signedMoney(node, sign, v, unitTail) {
    var parts = shortMoney(v), b = document.createElement('bdi');
    b.dir = 'ltr';
    b.textContent = sign + parts[0];
    node.textContent = '';
    node.appendChild(b);
    node.appendChild(document.createTextNode(' ' + parts[1] + (unitTail || '')));
  }

  var SCENES = {};

  // ---------- 1. Hero: customers come back to the store ----------
  // Each customer loops: away (amber if at risk, grey if they stopped buying), a message travels to
  // them from the store, they light up and walk back with a faint trail, their purchase rises from
  // the store, they circle it for a while, then slowly drift off again. The card tells the story of
  // one customer, Sara, or of whichever customer the mouse is on; a name tag sits above them.
  var NAMES = ['نگار', 'آرش', 'الهام', 'مریم', 'رضا', 'نیما', 'زهرا', 'علی', 'سپیده', 'کاوه', 'مهسا', 'حسین', 'پریا', 'امید', 'لیلا', 'بهار'];
  var FAMILY = ['کریمی', 'احمدی', 'رضایی', 'حسینی', 'موسوی', 'جعفری', 'صادقی', 'رحیمی', 'کاظمی', 'نوری'];
  var MONTHS = ['فروردین', 'اردیبهشت', 'خرداد', 'تیر', 'مرداد', 'شهریور', 'مهر', 'آبان', 'آذر', 'دی', 'بهمن', 'اسفند'];
  var AMOUNTS = [950000, 1200000, 1500000, 1800000, 2400000];
  // Phases of one loop, as a share of the customer's period.
  var MSG = .13, GO = .18, HOME = .3, LEAVE = .9;

  SCENES.attract = function (st) {
    var P = [], n = 0, cx = 0, cy = 0, m = 1, hw = 1, hh = 1, size = 14, coreR = 46;
    var el = st.el, pings = [], hovered = null, sara = null;
    var nEl = el.querySelector('[data-today-n]'), sumEl = el.querySelector('[data-today-sum]');
    var receipts = el.querySelector('.receipts'), tag = el.querySelector('[data-focus-tag]');
    var card = el.querySelector('.pcard'), steps = card ? card.querySelectorAll('[data-pc-step]') : [];
    var cardKey = '', shownN = -1, today = 0, todaySum = 0, lastReceipt = -9, tagX = -1, tagY = -1;

    function build(count) {
      var rand = rng(7), order = shuffled(count, rand);
      P = [];
      for (var i = 0; i < count; i++) {
        var T = 38 + rand() * 20, first = 1.2 + order[i] * 1.1, fi = Math.floor(rand() * FAMILY.length);
        P.push({
          kind: rand() < .4 ? 'risk' : 'lost',
          a0: rand() * TAU, hr: .4 + rand() * .22, spin: .05 + rand() * .05,
          aa0: i * 2.39996 + rand() * .3, aa: 0, ar: Math.pow(rand(), .8), wf: .25 + rand() * .3, wp: rand() * TAU,
          T: T, first: first, off: GO * T - first, state: -1, u: 0, lit: 0, x: 0, y: 0, ax: 0, ay: 0, hx: 0, hy: 0,
          name: NAMES[i % NAMES.length] + ' ' + FAMILY[fi], days: 40 + Math.floor(rand() * 180),
          since: MONTHS[Math.floor(rand() * 12)] + (rand() < .5 ? ' ۱۴۰۱' : ' ۱۴۰۲'),
          amount: AMOUNTS[Math.floor(rand() * AMOUNTS.length)], offer: rand() < .5 ? 15 : 20
        });
      }
      // Sara: at risk, away at the upper left, with a shorter loop so her story repeats while people watch.
      sara = P[0];
      sara.kind = 'risk'; sara.aa0 = 200 * Math.PI / 180; sara.ar = .5; sara.T = 26; sara.first = 2.4;
      sara.off = GO * sara.T - sara.first; sara.wf = .3;
      sara.name = 'سارا محمدی'; sara.days = 124; sara.since = 'اسفند ۱۴۰۲'; sara.amount = 1800000; sara.offer = 20;
      sara.hr = .52;
      n = count;
    }
    // Where a customer waits while away, before the small sway is added.
    function awayAt(p, aa) {
      var ca = Math.cos(aa), sa = Math.sin(aa);
      var edge = Math.min(hw / Math.max(.001, Math.abs(ca)), hh / Math.max(.001, Math.abs(sa)));
      var r = p.kind === 'risk' ? Math.min(edge, m * (.66 + .14 * p.ar)) : lerp(Math.min(m * .82, edge * .9), edge, p.ar);
      return { x: cx + ca * r, y: cy + sa * r, r: r };
    }
    // Nobody waits under the card, the counter or the legend.
    function zones() {
      var out = [], b = el.getBoundingClientRect();
      [card, el.querySelector('.today'), el.querySelector('.stage__legend')].forEach(function (z, i) {
        if (!z || !z.offsetWidth) return;
        // The counter grows toward the left as the sales add up, so keep some room there.
        var r = z.getBoundingClientRect(), grow = i === 1 ? 80 : 0;
        out.push({ l: r.left - b.left - size - grow, r: r.right - b.left + size, t: r.top - b.top - size, b: r.bottom - b.top + size });
      });
      return out;
    }
    function inZone(zs, x, y) {
      for (var i = 0; i < zs.length; i++) if (x > zs[i].l && x < zs[i].r && y > zs[i].t && y < zs[i].b) return true;
      return false;
    }

    function money(v) {
      if (v >= 1e9) return fa1.format(v / 1e9) + ' میلیارد تومان';
      if (v >= 1e6) return fa1.format(v / 1e6) + ' میلیون تومان';
      return fa.format(v) + ' تومان';
    }
    function setToday(count, sum) {
      if (count === shownN) return;
      shownN = count;
      if (nEl) nEl.textContent = fa.format(count) + ' نفر';
      if (sumEl) sumEl.textContent = money(sum);
    }
    function receipt(p, still) {
      if (!receipts) return;
      var r = document.createElement('span');
      r.className = 'receipt' + (still ? ' is-still' : '');
      r.innerHTML = '<svg viewBox="0 0 24 24"><path d="M6 7h12l-1 13H7z"/><path d="M9 7a3 3 0 0 1 6 0"/></svg>';
      var amt = document.createElement('span');
      signedMoney(amt, '+', p.amount);
      r.appendChild(amt);
      if (!still) r.addEventListener('animationend', function () { r.remove(); });
      receipts.appendChild(r);
    }
    // The card: who the customer is and how far along their way back they are.
    function showCard(p) {
      if (!card) return;
      var at = p.state === 1 ? 1 : p.state === 2 ? 2 : p.state === 3 ? 3 : 0;
      var key = P.indexOf(p) + ':' + at;
      if (key === cardKey) return;
      var whoChanged = cardKey.split(':')[0] !== String(P.indexOf(p));
      cardKey = key;
      if (whoChanged) {
        card.setAttribute('data-kind', p.kind);
        card.querySelector('[data-pc-av]').textContent = p.name.charAt(0);
        card.querySelector('[data-pc-name]').textContent = p.name;
        card.querySelector('[data-pc-since]').textContent = 'مشتری از ' + p.since;
        steps[0].textContent = fa.format(p.days) + ' روز خرید نکرده بود';
        steps[1].textContent = 'پیامک ' + fa.format(p.offer) + '٪ تخفیف برایش رفت';
        steps[2].textContent = 'در راه فروشگاه…';
        steps[3].textContent = 'خرید کرد: ' + shortMoney(p.amount).join(' ') + ' تومان';
      }
      for (var i = 0; i < steps.length; i++) {
        steps[i].className = i < at ? 'is-done' : i === at ? (at === 0 ? 'is-away' : 'is-now') : '';
      }
    }

    el.addEventListener('pointermove', function (e) {
      if (e.pointerType !== 'mouse') return;
      var r = st.canvas.getBoundingClientRect();
      var px = e.clientX - r.left, py = e.clientY - r.top, best = null, bd = 28 * 28;
      for (var i = 0; i < n; i++) {
        var dx = P[i].x - px, dy = P[i].y - py, d = dx * dx + dy * dy;
        if (d < bd) { bd = d; best = P[i]; }
      }
      if (best && best !== hovered) { hovered = best; if (reduce && st.w) st.still(); }
    });
    el.addEventListener('pointerleave', function () { hovered = null; if (reduce && st.w) st.still(); });

    function bubble(ctx, x, y, alpha) {
      var w = 24, h = 16, r = 7, x0 = x - w / 2, y0 = y - h / 2;
      ctx.globalAlpha = alpha;
      ctx.fillStyle = '#fff'; ctx.strokeStyle = COLOR.teal; ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(x0 + r, y0); ctx.arcTo(x0 + w, y0, x0 + w, y0 + h, r); ctx.arcTo(x0 + w, y0 + h, x0, y0 + h, r);
      ctx.arcTo(x0, y0 + h, x0, y0, 3); ctx.arcTo(x0, y0, x0 + w, y0, r); ctx.closePath();
      ctx.fill(); ctx.stroke();
      ctx.fillStyle = COLOR.teal;
      for (var i = -1; i <= 1; i++) { ctx.beginPath(); ctx.arc(x + i * 5, y, 1.6, 0, TAU); ctx.fill(); }
    }
    // The curved path a message takes from the store to a customer.
    function msgPath(p) {
      var dx = p.ax - cx, dy = p.ay - cy, d = Math.max(1, Math.hypot(dx, dy));
      var sx = cx + dx / d * coreR, sy = cy + dy / d * coreR;
      return { sx: sx, sy: sy, qx: (sx + p.ax) / 2 + dy / d * d * .18, qy: (sy + p.ay) / 2 - dx / d * d * .18 };
    }

    return {
      layout: function (w, h) {
        cx = w / 2; cy = h / 2; m = Math.min(w, h) / 2;
        size = Math.max(10, Math.min(17, m / 15));
        hw = w / 2 - size; hh = h / 2 - size * 1.1;
        coreR = w < 420 ? 40 : 48;
        var want = w < 420 ? 30 : 40;
        if (want !== n) { build(want); cardKey = ''; }
        // Each customer keeps their direction if they can; otherwise they step around until they
        // are clear of the card, the counter, the legend and the customers already placed.
        var zs = zones(), placed = [], gap = size * 1.6;
        for (var i = 0; i < n; i++) {
          var p = P[i], best = p.aa0, ok = false;
          for (var k = 0; k < 60 && !ok; k++) {
            var aa = p.aa0 + (k % 2 ? 1 : -1) * Math.ceil(k / 2) * .09, a = awayAt(p, aa);
            if (inZone(zs, a.x, a.y)) continue;
            if (k === 0 || best === p.aa0) best = aa;
            ok = true;
            for (var j = 0; j < placed.length; j++) {
              var dx = placed[j].x - a.x, dy = placed[j].y - a.y;
              if (dx * dx + dy * dy < gap * gap) { ok = false; break; }
            }
            if (ok) best = aa;
          }
          p.aa = best;
          placed.push(awayAt(p, best));
        }
        tagX = tagY = -1;
      },
      step: function (dt, t) {
        var still = dt >= 1, home = 0, homeSum = 0;
        for (var i = 0; i < n; i++) {
          var p = P[i], u, state, k, lit;
          if (t < p.first - (GO - MSG) * p.T) u = 0;
          else u = ((t + p.off) / p.T) % 1;
          // The still picture: Sara on her way back, and one message on its way.
          if (still && p === sara) u = GO + (HOME - GO) * .3;
          else if (still && i === 1) u = MSG + (GO - MSG) * .6;
          state = u < MSG ? 0 : u < GO ? 1 : u < HOME ? 2 : u < LEAVE ? 3 : 4;
          var ha = p.a0 + p.spin * t;
          p.hx = cx + Math.cos(ha) * p.hr * m; p.hy = cy + Math.sin(ha) * p.hr * m;
          var aa = p.aa + Math.sin(t * p.wf + p.wp) * .04, a = awayAt(p, aa);
          var sway = Math.sin(t * p.wf * .7 + p.wp * 1.7) * size * .25;
          p.ax = a.x + Math.cos(aa) * sway; p.ay = a.y + Math.sin(aa) * sway;
          if (state <= 1) { p.x = p.ax; p.y = p.ay; lit = 0; }
          else if (state === 2) { k = inOut((u - GO) / (HOME - GO)); p.x = lerp(p.ax, p.hx, k); p.y = lerp(p.ay, p.hy, k); lit = smooth((u - GO) / .03); }
          else if (state === 3) { p.x = p.hx; p.y = p.hy; lit = 1; home++; homeSum += p.amount; }
          else { k = inOut((u - LEAVE) / (1 - LEAVE)); p.x = lerp(p.hx, p.ax, k); p.y = lerp(p.hy, p.ay, k); lit = 1 - smooth((u - LEAVE) / .05); }
          if (!still) {
            if (p.state === 1 && state === 2) pings.push({ x: p.ax, y: p.ay, t: t });
            if (p.state === 2 && state === 3) {
              today++; todaySum += p.amount;
              if (t - lastReceipt > 1.3 || p === sara) { lastReceipt = t; receipt(p, false); }
            }
          }
          p.state = state; p.u = u; p.lit = lit;
        }
        if (still) {
          today = home; todaySum = homeSum; shownN = -1;
          if (receipts && !receipts.firstChild) receipt(sara, true);
        }
        setToday(today, todaySum);
        for (i = pings.length - 1; i >= 0; i--) if (t - pings[i].t > 1.4 || still) pings.splice(i, 1);
        var f = hovered || sara;
        showCard(f);
        if (tag) {
          var tx = Math.round(f.x), ty = Math.round(f.y - size * .62);
          if (tx !== tagX || ty !== tagY) { tagX = tx; tagY = ty; tag.style.left = tx + 'px'; tag.style.top = ty + 'px'; }
          if (tag.textContent !== f.name.split(' ')[0]) tag.textContent = f.name.split(' ')[0];
          if (!tag.classList.contains('is-on')) tag.classList.add('is-on');
        }
        return true;
      },
      draw: function (ctx) {
        var g = ctx.createRadialGradient(cx, cy, 0, cx, cy, m * .8), i, p, k;
        g.addColorStop(0, 'rgba(0,166,162,.10)');
        g.addColorStop(1, 'rgba(0,166,162,0)');
        ctx.fillStyle = g;
        ctx.fillRect(0, 0, st.w, st.h);
        ctx.strokeStyle = 'rgba(0,166,162,.18)';
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 6]);
        ctx.beginPath(); ctx.arc(cx, cy, m * .4, 0, TAU); ctx.stroke();
        ctx.beginPath(); ctx.arc(cx, cy, m * .62, 0, TAU); ctx.stroke();
        // Messages on their way: a dashed path and a small speech bubble moving along it.
        for (i = 0; i < n; i++) {
          p = P[i];
          if (p.state !== 1) continue;
          k = (p.u - MSG) / (GO - MSG);
          var q = msgPath(p), e = inOut(k), fade = Math.min(1, k * 6, (1 - k) * 8);
          ctx.globalAlpha = .55 * fade;
          ctx.strokeStyle = COLOR.teal; ctx.lineWidth = 1.5; ctx.setLineDash([3, 5]);
          ctx.beginPath(); ctx.moveTo(q.sx, q.sy); ctx.quadraticCurveTo(q.qx, q.qy, p.ax, p.ay); ctx.stroke();
          ctx.setLineDash([]);
          var bx = (1 - e) * (1 - e) * q.sx + 2 * (1 - e) * e * q.qx + e * e * p.ax;
          var by = (1 - e) * (1 - e) * q.sy + 2 * (1 - e) * e * q.qy + e * e * p.ay;
          bubble(ctx, bx, by, Math.min(1, fade * 1.5));
        }
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = COLOR.teal;
        ctx.lineWidth = 1.5;
        for (i = 0; i < pings.length; i++) {
          k = (st.t - pings[i].t) / 1.4;
          ctx.globalAlpha = (1 - k) * .45;
          ctx.beginPath(); ctx.arc(pings[i].x, pings[i].y, size * (.5 + k * 1.2), 0, TAU); ctx.stroke();
        }
        // Customers walking back leave a faint trail.
        for (i = 0; i < n; i++) {
          p = P[i];
          if (p.state !== 2) continue;
          k = (p.u - GO) / (HOME - GO);
          for (var j = 1; j <= 3; j++) {
            var kk = inOut(Math.max(0, k - j * .07));
            person(st, lerp(p.ax, p.hx, kk), lerp(p.ay, p.hy, kk), size, COLOR.teal, [.3, .17, .08][j - 1] * Math.min(1, k * 4), false);
          }
        }
        for (i = 0; i < n; i++) {
          p = P[i];
          mix(st, p.x, p.y, size, p.kind === 'risk' ? COLOR.amber : COLOR.sleep, COLOR.teal, p.lit, 1, true);
        }
        // A ring around the customer the card is about.
        var f = hovered || sara;
        ctx.globalAlpha = .9;
        ctx.strokeStyle = f.lit > .5 ? COLOR.teal : f.kind === 'risk' ? COLOR.amber : '#98A2B3';
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(f.x, f.y, size * 1.05, 0, TAU); ctx.stroke();
      }
    };
  };

  // ---------- 2. Why: out of 100 first-time buyers, 65 never come back ----------
  // The hundred appear column by column; then 65 of them turn grey and drift away a little; last, a
  // dashed frame is drawn around them with the label "Retano finds these 65".
  SCENES.hundred = function (st) {
    var P = [], size = 10, gb = null, fr = 0, mob = false, rand = rng(4), jit = [], i;
    var keepL = st.el.querySelector('[data-lbl="keep"]'), findL = st.el.querySelector('[data-lbl="find"]');
    for (i = 0; i < 100; i++) jit.push(rand() - .5);
    function box(list) {
      var b = { l: 1e9, r: -1e9, t: 1e9, b: -1e9 }, pad = size * .75;
      list.forEach(function (p) { b.l = Math.min(b.l, p.gx); b.r = Math.max(b.r, p.gx); b.t = Math.min(b.t, p.gy); b.b = Math.max(b.b, p.gy); });
      b.l -= pad; b.r += pad; b.t -= pad; b.b += pad;
      return b;
    }
    function at(node, x, y) { if (node) { node.style.left = x + 'px'; node.style.top = y + 'px'; } }
    function on(node, v) { if (node && node.classList.contains('is-on') !== v) node.classList.toggle('is-on', v); }
    function roundRect(ctx, b, r) {
      ctx.beginPath(); ctx.moveTo(b.l + r, b.t); ctx.arcTo(b.r, b.t, b.r, b.b, r); ctx.arcTo(b.r, b.b, b.l, b.b, r);
      ctx.arcTo(b.l, b.b, b.l, b.t, r); ctx.arcTo(b.l, b.t, b.r, b.t, r); ctx.closePath();
    }
    return {
      layout: function (w, h) {
        var cell, keep = [], grey = [];
        mob = w < 560; P = [];
        if (!mob) {
          var COLS = 20, ROWS = 5, top = 40, drift = 22;
          cell = Math.min((w - 24 - drift) / COLS, (h - top - 10) / ROWS, 46);
          size = cell * .74;
          var ox = (w - cell * COLS - drift) / 2 + drift;
          for (i = 0; i < 100; i++) {
            var c = Math.floor(i / ROWS), r = i % ROWS, x = ox + (COLS - .5 - c) * cell, y = top + (r + .5) * cell, p;
            if (c < 7) p = { x: x, y: y, gx: x, gy: y, ga: 1, at: .1 + c * .035, gat: 99, grey: false };
            else {
              var gk = (c - 7) / 12;
              p = { x: x, y: y, gx: x - gk * gk * drift, gy: y + jit[i] * 3, ga: 1 - .55 * gk, at: .1 + c * .035, gat: 1.1 + (c - 7) * .09 + r * .02, grey: true };
            }
            P.push(p);
          }
        } else {
          var C2 = 10, top1 = 30;
          cell = Math.min((w - 16) / C2, 30);
          size = cell * .74;
          var ox2 = (w - cell * C2) / 2, top2 = top1 + 4 * cell + 44;
          for (i = 0; i < 35; i++) {
            var kx = ox2 + (C2 - .5 - i % C2) * cell, ky = top1 + (Math.floor(i / C2) + .5) * cell;
            P.push({ x: kx, y: ky, gx: kx, gy: ky, ga: 1, at: .1 + Math.floor(i / C2) * .1, gat: 99, grey: false });
          }
          for (i = 0; i < 65; i++) {
            var row = Math.floor(i / C2), gx = ox2 + (C2 - .5 - i % C2) * cell, gy = top2 + (row + .5) * cell;
            P.push({ x: gx, y: gy, gx: gx, gy: gy, ga: 1 - .5 * row / 6, at: .5 + row * .1, gat: 1.3 + row * .12 + (i % C2) * .02, grey: true });
          }
        }
        P.forEach(function (p) { (p.grey ? grey : keep).push(p); });
        gb = box(grey);
        var kb = box(keep);
        at(keepL, (kb.l + kb.r) / 2, kb.t - 6);
        at(findL, (gb.l + gb.r) / 2, gb.t);
      },
      enter: function (first, away) {
        if (!first && away < 2) return;
        st.t = 0;
      },
      step: function (dt, t) {
        var T = dt >= 1 ? 99 : t;
        for (var i = 0; i < P.length; i++) {
          var p = P[i];
          p.a = smooth((T - p.at) / .4);
          p.g = p.grey ? inOut((T - p.gat) / .7) : 0;
        }
        fr = smooth((T - 2.7) / .6);
        on(keepL, T > .9);
        on(findL, T > 2.9);
        return T < 3.6;
      },
      draw: function (ctx) {
        for (var i = 0; i < P.length; i++) {
          var p = P[i];
          if (p.a <= .01) continue;
          mix(st, lerp(p.x, p.gx, p.g), lerp(p.y, p.gy, p.g), size * (.7 + .3 * p.a), COLOR.teal, COLOR.sleep, p.g, p.a * lerp(1, p.ga, p.g), false);
        }
        if (fr > .01) {
          ctx.globalAlpha = fr;
          roundRect(ctx, gb, 14);
          ctx.fillStyle = 'rgba(0,166,162,.04)'; ctx.fill();
          ctx.strokeStyle = COLOR.teal; ctx.lineWidth = 1.6; ctx.setLineDash([5, 6]); ctx.stroke(); ctx.setLineDash([]);
        }
      }
    };
  };

  // ---------- 3. Calculator: 200 SMS recipients ----------
  // A wave of messages crosses the grid from right to left, then the buyers turn amber. The number
  // of buyers follows the conversion rate slider.
  SCENES.audience = function (st) {
    var COLS = 20, ROWS = 10, N = COLS * ROWS;
    var rand = rng(11), order = shuffled(N, rand), rank = [];
    var P = [], cell = 1, ox = 0, oy = 0, size = 8, buyers = 0, i, tags = [], tagOrder = 0;
    order.forEach(function (idx, k) { rank[idx] = k; });
    // The first three buyers show what they spent.
    for (i = 0; i < 3; i++) {
      var tg = document.createElement('span');
      tg.className = 'buy-tag';
      tg.setAttribute('aria-hidden', 'true');
      st.el.appendChild(tg);
      tags.push(tg);
    }
    for (i = 0; i < N; i++) P.push({ c: i % COLS, r: Math.floor(i / COLS), j: rand() * .08, lit: 0, buy: 0, pop: 0 });

    function sync() {
      buyers = Math.max(0, Math.min(N, Math.round(N * (+S.rate || 1.5) / 100)));
      var o = +S.order || 1800000;
      if (o !== tagOrder) { tagOrder = o; tags.forEach(function (tg) { signedMoney(tg, '+', o); }); }
    }
    sync();

    return {
      sync: sync,
      layout: function (w, h) {
        var pad = Math.max(8, Math.min(w, h) * .05);
        cell = Math.min((w - pad * 2) / COLS, (h - pad * 2) / ROWS);
        size = cell * .7;
        ox = (w - cell * COLS) / 2; oy = (h - cell * ROWS) / 2;
        tags.forEach(function (tg, k) {
          var idx = order[k];
          tg.style.left = (ox + (COLS - .5 - idx % COLS) * cell) + 'px';
          tg.style.top = (oy + (Math.floor(idx / COLS) + .5) * cell - cell * .5) + 'px';
        });
      },
      // Plays the wave again when the reader comes back to it after a while.
      enter: function (first, away) {
        if (!first && away < 2) return;
        st.t = 0;
        P.forEach(function (p) { p.lit = 0; p.buy = 0; p.pop = 0; });
      },
      step: function (dt, t) {
        var moving = t < 3.5, still = dt >= 1;
        for (var i = 0; i < N; i++) {
          var p = P[i], at = .4 + p.c * .085 + p.j;
          var litT = t >= at ? 1 : 0, buyT = rank[i] < buyers && t >= at + .6 ? 1 : 0;
          if (buyT && p.buy < .02 && !still) p.pop = 1;
          var nl = still ? litT : approach(p.lit, litT, 8, dt);
          var nb = still ? buyT : approach(p.buy, buyT, 5, dt);
          if (nl !== p.lit || nb !== p.buy || p.pop > 0) moving = true;
          p.lit = nl; p.buy = nb;
          p.pop = still ? 0 : Math.max(0, p.pop - dt * 2.2);
        }
        tags.forEach(function (tg, k) {
          var on = k < buyers && P[order[k]].buy > .6;
          if (tg.classList.contains('is-on') !== on) tg.classList.toggle('is-on', on);
        });
        return moving;
      },
      draw: function () {
        for (var i = 0; i < N; i++) {
          var p = P[i], x = ox + (COLS - .5 - p.c) * cell, y = oy + (p.r + .5) * cell;
          var s = size * (1 + .35 * Math.sin(p.pop * Math.PI));
          if (p.buy < .99) mix(st, x, y, s, COLOR.sleep, COLOR.teal, p.lit, 1 - p.buy, false);
          if (p.buy > .01) person(st, x, y, s, COLOR.amber, p.buy, true);
        }
      }
    };
  };

  // ---------- 4. Solutions: the customers sort themselves into three groups ----------
  // At first everyone is mixed; then each customer walks to their group (loyal, new, lost). The group
  // of the chosen tab lights up and the others fade. landing.js draws the message's way to the phone
  // from the point S.crowdAnchor(group) gives.
  SCENES.crowd = function (st) {
    var G = 3, N = 13, cols = [COLOR.teal, COLOR.amber, COLOR.rose], xs = [.83, .5, .17];
    var P = [], size = 12, cy = 0, R = 1, w0 = 1, active = 0, glow = [0, 0, 0], sorted = false, rand = rng(30);
    var lbls = [0, 1, 2].map(function (g) { return st.el.querySelector('[data-lbl="' + g + '"]'); });
    var tag = st.el.querySelector('[data-lbl="sara"]'), sara = null;
    for (var g = 0; g < G; g++) for (var i = 0; i < N; i++) P.push({ g: g, i: i, sx: rand(), sy: rand(), delay: rand() * .7, jx: rand() - .5, jy: rand() - .5, a: .85, pop: 0 });
    function sync() {
      var a = Math.max(0, Math.min(G - 1, Math.round(+S.seg || 0)));
      if (a !== active) {
        active = a;
        P.forEach(function (p) { if (p.g === a) p.pop = 1 + p.i * .04; });
      }
      lbls.forEach(function (l, k) { if (l) l.classList.toggle('is-active', k === active); });
    }
    S.crowdAnchor = function (grp) { return { x: w0 * xs[grp], y: cy, r: R + size * 1.1 }; };
    return {
      sync: sync,
      layout: function (w, h) {
        w0 = w; size = Math.max(11, Math.min(15, h / 12)); cy = h * .42; R = Math.min(w * .12, h * .3);
        sara = null;
        P.forEach(function (p) {
          var rr = R * Math.sqrt((p.i + .5) / N), a = p.i * 2.39996 + p.g;
          p.tx = w * xs[p.g] + Math.cos(a) * rr + p.jx * 3;
          p.ty = cy + Math.sin(a) * rr * .9 + p.jy * 3;
          p.x0 = size + p.sx * (w - size * 2);
          p.y0 = size + p.sy * (h * .78 - size * 2);
          if (p.g === 2 && (!sara || p.ty < sara.ty)) sara = p;
        });
        lbls.forEach(function (l, k) { if (l) { l.style.left = (w * xs[k]) + 'px'; l.style.top = (cy + R + size * 1.25) + 'px'; } });
        if (tag && sara) { tag.style.left = sara.tx + 'px'; tag.style.top = (sara.ty - size * .62) + 'px'; }
        sync();
      },
      enter: function (first) { if (first) st.t = 0; },
      step: function (dt, t) {
        var still = dt >= 1, T = still ? 99 : t, moving = T < 2.8;
        var nowSorted = T > 2.2;
        if (nowSorted !== sorted) {
          sorted = nowSorted;
          lbls.forEach(function (l) { if (l) l.classList.toggle('is-on', sorted); });
          if (tag) tag.classList.toggle('is-on', sorted);
        }
        for (var i = 0; i < P.length; i++) {
          var p = P[i], target = !sorted ? .85 : p.g === active ? 1 : .3;
          p.k = inOut((T - .3 - p.delay) / 1.2);
          var na = still ? target : approach(p.a, target, 5, dt);
          if (na !== p.a) moving = true;
          p.a = na;
          if (p.pop > 0) { p.pop = still ? 0 : Math.max(0, p.pop - dt * 2.4); moving = true; }
        }
        for (var g = 0; g < G; g++) {
          var gt = sorted && g === active ? 1 : 0, ng = still ? gt : approach(glow[g], gt, 5, dt);
          if (ng !== glow[g]) moving = true;
          glow[g] = ng;
        }
        return moving;
      },
      draw: function (ctx) {
        for (var g = 0; g < G; g++) {
          if (glow[g] < .01) continue;
          var gx = w0 * xs[g], rg = ctx.createRadialGradient(gx, cy, 0, gx, cy, R + size * 1.6);
          rg.addColorStop(0, rgba(cols[g], .12 * glow[g])); rg.addColorStop(1, rgba(cols[g], 0));
          ctx.fillStyle = rg; ctx.beginPath(); ctx.arc(gx, cy, R + size * 1.6, 0, TAU); ctx.fill();
          ctx.globalAlpha = glow[g];
          ctx.strokeStyle = rgba(cols[g], .45); ctx.lineWidth = 1.4; ctx.setLineDash([4, 6]);
          ctx.beginPath(); ctx.arc(gx, cy, R + size * 1.1, 0, TAU); ctx.stroke(); ctx.setLineDash([]);
          ctx.globalAlpha = 1;
        }
        for (var i = 0; i < P.length; i++) {
          var p = P[i], pop = p.pop > 1 ? 0 : p.pop;
          person(st, lerp(p.x0, p.tx, p.k), lerp(p.y0, p.ty, p.k), size * (1 + .3 * Math.sin(pop * Math.PI)), cols[p.g], p.a, sorted && p.g === active);
        }
      }
    };
  };

  // ---------- 6. Before / after: the circle closes ----------
  // Every customer has a seat around the store. Before Retano only 35% keep theirs; the rest drift
  // away after the first purchase. Moving the slider to "after" brings them back one by one.
  SCENES.loop = function (st) {
    var N = 20, KEEP = 7, P = [], churn = [], cx = 0, cy = 0, m = 1, size = 12, R = 1, v = 0, i;
    var rand = rng(5), seats = {};
    for (i = 0; i < KEEP; i++) seats[Math.round(i * N / KEEP)] = true;
    for (i = 0; i < N; i++) {
      var seat = i / N * TAU;
      var p = { seat: seat, keep: !!seats[i], sa: seat + (rand() - .5) * .5, sr: .8 + rand() * .14, wf: .2 + rand() * .25, wp: rand() * TAU, th: 0, j: 1, x: 0, y: 0 };
      if (!p.keep) { p.j = 0; churn.push(p); }
      P.push(p);
    }
    var order = shuffled(churn.length, rand);
    churn.forEach(function (p, k) { p.th = (order[k] + .5) / churn.length; });

    function sync() { v = clamp01(+S.compare || 0); }
    sync();

    return {
      sync: sync,
      layout: function (w, h) {
        cx = w / 2; cy = h / 2; m = Math.min(w, h) / 2;
        size = Math.max(10, Math.min(16, m / 9));
        R = m * .52;
      },
      step: function (dt, t) {
        var spin = t * .1;
        for (var i = 0; i < N; i++) {
          var p = P[i];
          var target = p.keep ? 1 : smooth((v - p.th * .85) / .15);
          p.j = dt >= 1 ? target : approach(p.j, target, 4, dt);
          var a = p.seat + spin, hx = cx + Math.cos(a) * R, hy = cy + Math.sin(a) * R;
          var sa = p.sa + spin * .3 + Math.sin(t * p.wf + p.wp) * .08;
          var sr = (p.sr + Math.sin(t * p.wf * .8 + p.wp) * .03) * (m - size);
          var e = inOut(p.j);
          p.x = lerp(cx + Math.cos(sa) * sr, hx, e);
          p.y = lerp(cy + Math.sin(sa) * sr, hy, e);
        }
        return true;
      },
      draw: function (ctx) {
        ctx.strokeStyle = 'rgba(0,166,162,' + (.14 + v * .3).toFixed(3) + ')';
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(cx, cy, R, 0, TAU); ctx.stroke();
        for (var i = 0; i < N; i++) {
          var p = P[i];
          mix(st, p.x, p.y, size, COLOR.rose, COLOR.teal, p.j, p.keep ? 1 : .65 + .35 * p.j, true);
        }
      }
    };
  };

  // ---------- 7. Lost sales: customers who drop away ----------
  // Rows of customers. The share set by the slider turns rose; each of those slowly falls and fades,
  // then the seat fills again, so the loss keeps repeating the way it does every month.
  SCENES.falling = function (st) {
    var COLS = 0, ROWS = 5, N = 0, P = [], cell = 1, ox = 0, oy = 0, size = 10, drop = 1, lost = 0;
    // The counter: customers lost so far this month and the sales they took with them. It starts
    // again from one when it reaches the month's total set by the slider.
    var nEl = st.el.querySelector('[data-fell-n]'), sumEl = st.el.querySelector('[data-fell-sum]'), fell = 0, shown = -1;
    function count(v) {
      if (v === shown) return;
      shown = v;
      if (nEl) nEl.textContent = fa.format(v) + ' نفر';
      if (sumEl) signedMoney(sumEl, '−', v * (+S.avg || 1800000), ' تومان');
    }

    function build(cols) {
      var rand = rng(3), order;
      COLS = cols; N = COLS * ROWS; order = shuffled(N, rand);
      P = [];
      for (var i = 0; i < N; i++) P.push({ c: i % COLS, r: Math.floor(i / COLS), rank: 0, T: 8 + rand() * 5, off: rand() * 13, dx: rand() - .5, lw: 0, fy: 0, fx: 0, a: 1, u: 0 });
      order.forEach(function (idx, k) { P[idx].rank = k; });
    }
    function sync() {
      var f = +S.lost;
      if (!(f >= 0)) f = .3;
      lost = Math.round(N * Math.min(.6, f));
    }

    return {
      sync: sync,
      layout: function (w, h) {
        var cols = w < 420 ? 10 : 12;
        if (cols !== COLS) build(cols);
        sync();
        var pad = Math.max(10, w * .04);
        cell = Math.min((w - pad * 2) / COLS, (h * .58) / ROWS);
        size = cell * .68;
        ox = (w - cell * COLS) / 2; oy = pad * .6;
        drop = h - size * .6 - (oy + cell * ROWS);
      },
      step: function (dt, t) {
        var still = dt >= 1;
        for (var i = 0; i < N; i++) {
          var p = P[i], target = p.rank < lost ? 1 : 0;
          p.lw = still ? target : approach(p.lw, target, 3, dt);
          var u = ((t + p.off) / p.T) % 1, fall = 0, alpha = 1, k;
          if (still || u < .4) { fall = 0; alpha = 1; }
          else if (u < .78) { k = (u - .4) / .38; fall = k * k; alpha = 1 - smooth((k - .55) / .45); }
          else if (u < .88) { fall = 1; alpha = 0; }
          else { fall = 0; alpha = smooth((u - .88) / .12); }
          if (!still && p.lw > .5 && p.u < .78 && u >= .78) {
            fell++;
            if (fell > Math.max(1, +S.lostN || 150)) fell = 1;
          }
          p.u = u;
          var w = smooth(p.lw);
          p.fy = fall * (drop + (ROWS - p.r - .5) * cell) * w;
          p.fx = fall * p.dx * cell * w;
          p.a = 1 - (1 - alpha) * w;
        }
        if (still) fell = Math.max(1, +S.lostN || 150);
        count(fell);
        return true;
      },
      draw: function (ctx) {
        var top = oy + cell * ROWS;
        var g = ctx.createLinearGradient(0, top, 0, st.h);
        g.addColorStop(0, 'rgba(242,84,125,0)');
        g.addColorStop(1, 'rgba(242,84,125,.08)');
        ctx.fillStyle = g;
        ctx.fillRect(0, top, st.w, st.h - top);
        for (var i = 0; i < N; i++) {
          var p = P[i];
          mix(st, ox + (COLS - .5 - p.c) * cell + p.fx, oy + (p.r + .5) * cell + p.fy, size, COLOR.teal, COLOR.rose, p.lw, p.a, false);
        }
      }
    };
  };

  // ---------- 9. Final call: loyal customers circling the store, Sara among them ----------
  SCENES.retained = function (st) {
    var RINGS = [{ r: .42, n: 8, spin: .08 }, { r: .64, n: 13, spin: -.055 }, { r: .86, n: 18, spin: .04 }];
    var P = [], cx = 0, cy = 0, m = 1, size = 12, burstAt = -99;
    var rand = rng(9), tag = st.el.querySelector('[data-sara]'), tagX = -1, tagY = -1;
    RINGS.forEach(function (ring, ri) {
      for (var i = 0; i < ring.n; i++) P.push({ ring: ri, a: i / ring.n * TAU + ri * .3, gold: rand() < .3, delay: .2 + ri * .35 + rand() * .7 });
    });
    var SARA = P.length - 5;  // one of the outer ring
    return {
      layout: function (w, h) {
        cx = w / 2; cy = h / 2; m = Math.min(w, h) / 2 - 6;
        size = Math.max(10, Math.min(16, m / 11));
      },
      burst: function () { burstAt = st.t; },
      step: function () { return true; },
      draw: function (ctx) {
        var t = st.t, i;
        ctx.strokeStyle = 'rgba(255,255,255,.2)';
        ctx.lineWidth = 1;
        ctx.setLineDash([2, 6]);
        for (i = 0; i < RINGS.length; i++) { ctx.beginPath(); ctx.arc(cx, cy, RINGS[i].r * m, 0, TAU); ctx.stroke(); }
        ctx.setLineDash([]);
        var bk = (t - burstAt) / 1.6;
        if (bk >= 0 && bk < 1) {
          ctx.globalAlpha = (1 - bk) * .7;
          ctx.strokeStyle = '#fff';
          ctx.lineWidth = 2;
          ctx.beginPath(); ctx.arc(cx, cy, m * (.2 + bk * .8), 0, TAU); ctx.stroke();
        }
        for (i = 0; i < P.length; i++) {
          var p = P[i], ring = RINGS[p.ring], k = smooth((t - p.delay) / 1.6);
          if (k <= 0) continue;
          var a = p.a + ring.spin * t, r = ring.r * m * (1 + (1 - k) * .45);
          // After a successful request everyone turns gold for a moment, ring by ring.
          var since = t - burstAt - p.ring * .25, flash = 0;
          if (since >= 0 && since < 2) flash = since < .3 ? since / .3 : 1 - (since - .3) / 1.7;
          var x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
          mix(st, x, y, size, COLOR.white, COLOR.gold, Math.max(p.gold || i === SARA ? 1 : 0, flash), k, true);
          if (i === SARA) {
            ctx.globalAlpha = k * .9;
            ctx.strokeStyle = COLOR.gold; ctx.lineWidth = 2;
            ctx.beginPath(); ctx.arc(x, y, size * 1.05, 0, TAU); ctx.stroke();
            if (tag) {
              var tx = Math.round(x), ty = Math.round(y - size * .7);
              if (tx !== tagX || ty !== tagY) { tagX = tx; tagY = ty; tag.style.left = tx + 'px'; tag.style.top = ty + 'px'; }
              if (tag.classList.contains('is-on') !== k > .6) tag.classList.toggle('is-on', k > .6);
            }
          }
        }
      }
    };
  };

  // ---------- Stage plumbing ----------
  function Stage(el, kind) {
    this.el = el;
    this.canvas = el.querySelector('canvas');
    this.ctx = this.canvas.getContext('2d');
    this.w = 0; this.h = 0; this.dpr = 1;
    this.t = 0; this.visible = false; this.dirty = true; this.seen = false; this.hiddenAt = 0;
    this.scene = SCENES[kind](this);
  }
  Stage.prototype.resize = function () {
    var w = this.canvas.clientWidth, h = this.canvas.clientHeight;
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    if (!w || !h || (w === this.w && h === this.h && dpr === this.dpr)) return false;
    this.w = w; this.h = h; this.dpr = dpr;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.scene.layout(w, h);
    this.dirty = true;
    return true;
  };
  Stage.prototype.render = function (dt) {
    if (!this.w) return;
    var moving = this.scene.step(dt, this.t);
    if (!moving && !this.dirty) return;
    this.dirty = false;
    var ctx = this.ctx;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);
    ctx.globalAlpha = 1;
    this.scene.draw(ctx);
    ctx.globalAlpha = 1;
  };
  // Reduced motion: one settled picture, redrawn only when a value changes.
  Stage.prototype.still = function () {
    this.t = 90;
    this.scene.step(30, this.t);
    this.dirty = true;
    this.render(30);
  };

  var stages = [], running = false, last = 0;
  function frame(now) {
    var dt = Math.min(.05, Math.max(0, (now - last) / 1000)), any = false;
    last = now;
    for (var i = 0; i < stages.length; i++) {
      var s = stages[i];
      if (!s.visible) continue;
      any = true;
      s.t += dt;
      s.render(dt);
    }
    if (any) requestAnimationFrame(frame); else running = false;
  }
  function wake() {
    if (reduce || running) return;
    running = true;
    last = performance.now();
    requestAnimationFrame(frame);
  }
  function stageOf(el) {
    for (var i = 0; i < stages.length; i++) if (stages[i].el === el || stages[i].canvas === el) return stages[i];
    return null;
  }
  function resized(s) { if (s && s.resize() && reduce) s.still(); }

  S.refresh = function () {
    stages.forEach(function (s) {
      if (s.scene.sync) s.scene.sync();
      s.dirty = true;
      if (reduce && s.w) s.still();
    });
    wake();
  };
  S.burst = function () {
    if (reduce) return;
    stages.forEach(function (s) { if (s.scene.burst) s.scene.burst(); });
    wake();
  };

  var els = document.querySelectorAll('[data-stage]');
  for (var i = 0; i < els.length; i++) {
    var kind = els[i].getAttribute('data-stage'), cv = els[i].querySelector('canvas');
    if (SCENES[kind] && cv && cv.getContext) stages.push(new Stage(els[i], kind));
  }
  if (!stages.length) return;

  stages.forEach(function (s) { s.resize(); });
  if ('ResizeObserver' in window) {
    var ro = new ResizeObserver(function (entries) { entries.forEach(function (en) { resized(stageOf(en.target)); }); });
    stages.forEach(function (s) { ro.observe(s.canvas); });
  } else {
    window.addEventListener('resize', function () { stages.forEach(resized); });
  }

  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        var s = stageOf(en.target);
        if (!s) return;
        if (en.isIntersecting) {
          if (s.visible) return;
          var away = s.hiddenAt ? (performance.now() - s.hiddenAt) / 1000 : Infinity;
          s.visible = true;
          resized(s);
          if (s.scene.enter) s.scene.enter(!s.seen, away);
          s.seen = true;
          s.dirty = true;
          if (reduce) s.still(); else wake();
        } else if (s.visible) {
          s.visible = false;
          s.hiddenAt = performance.now();
        }
      });
    }, { rootMargin: '40px 0px' });
    stages.forEach(function (s) { io.observe(s.el); });
  } else {
    stages.forEach(function (s) { s.visible = true; s.seen = true; if (reduce) s.still(); });
    wake();
  }
})();
