/* Page behaviour: links to the panel, reveal on scroll, the two calculators, the sample SMS
   phone (and the message's way to it from its group of customers), the steps timeline,
   before/after, and the free consultation form. Values the animated stages need (conversion rate,
   order size, chosen group, before/after, lost customers) go to window.RetanoScene (stages.js). */
(function () {
  'use strict';

  var cfg = window.RETANO_CONFIG || {};
  var S = window.RetanoScene = window.RetanoScene || {};
  var reduceMotion = !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);
  var fa = new Intl.NumberFormat('fa-IR');
  var fa1 = new Intl.NumberFormat('fa-IR', { maximumFractionDigits: 1 });
  var AVG_ORDER = 1800000;

  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }
  function clamp01(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }
  function refreshScene() { if (typeof S.refresh === 'function') S.refresh(); }

  // ---------- Links to the panel ----------
  if (cfg.panelUrl) $$('[data-panel-link]').forEach(function (a) { a.href = cfg.panelUrl; });

  // ---------- Numbers ----------
  function toLatin(s) {
    return String(s)
      .replace(/[۰-۹]/g, function (d) { return String(d.charCodeAt(0) - 0x06F0); })
      .replace(/[٠-٩]/g, function (d) { return String(d.charCodeAt(0) - 0x0660); })
      .replace(/٫/g, '.');
  }
  function parseNum(s) {
    var n = parseFloat(toLatin(s).replace(/[^\d.]/g, ''));
    return isFinite(n) ? n : 0;
  }
  function money(n) {
    n = Math.max(0, Math.round(n));
    if (n >= 1e9) return fa1.format(n / 1e9) + ' میلیارد تومان';
    if (n >= 1e6) return fa1.format(n / 1e6) + ' میلیون تومان';
    return fa.format(n) + ' تومان';
  }

  // Counts a number up or down to its new value.
  function tween(el, to, format) {
    if (!el) return;
    var from = typeof el.__v === 'number' ? el.__v : 0;
    el.__v = to;
    if (el.__raf) cancelAnimationFrame(el.__raf);
    if (reduceMotion || from === to) { el.textContent = format(to); return; }
    var t0 = performance.now(), dur = 520;
    (function step(now) {
      var k = Math.min(1, (now - t0) / dur);
      var e = 1 - Math.pow(1 - k, 3);
      el.textContent = format(from + (to - from) * e);
      if (k < 1) el.__raf = requestAnimationFrame(step);
    })(t0);
  }

  function fillRange(r) {
    var min = +r.min || 0, max = +r.max || 100;
    r.style.setProperty('--fill', ((+r.value - min) / (max - min) * 100).toFixed(2) + '%');
  }
  $$('input[type="range"]').forEach(function (r) {
    fillRange(r);
    r.addEventListener('input', function () { fillRange(r); });
  });

  // ---------- Header ----------
  var nav = $('.nav');
  var navLinks = $$('.nav__links a');

  // ---------- Reveal on scroll ----------
  var revealEls = $$('[data-reveal]');
  if ('IntersectionObserver' in window && !reduceMotion) {
    var revealIO = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add('in'); revealIO.unobserve(en.target); }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
    revealEls.forEach(function (el) { revealIO.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add('in'); });
  }

  // Highlight the header link of the section being read.
  if ('IntersectionObserver' in window && navLinks.length) {
    var navIO = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        navLinks.forEach(function (a) { a.classList.toggle('is-current', a.getAttribute('href') === '#' + en.target.id); });
      });
    }, { rootMargin: '-45% 0px -50% 0px' });
    navLinks.forEach(function (a) { var s = $(a.getAttribute('href')); if (s) navIO.observe(s); });
  }

  // ---------- 2. Campaign calculator ----------
  var cCount = $('#c-count'), cRate = $('#c-rate'), cCost = $('#c-cost'), cOrder = $('#c-order');
  var outCount = $('[data-out="count"]'), outRate = $('[data-out="rate"]');
  var resCost = $('[data-res="cost"]'), resOrders = $('[data-res="orders"]'), resSales = $('[data-res="sales"]'), resRoi = $('[data-res="roi"]');
  var outBuyers = $('[data-buyers]'), outBuySum = $('[data-buy-sum]');

  function campaign() {
    if (!cCount) return;
    var n = +cCount.value, rate = +cRate.value;
    var cost = parseNum(cCost.value), order = parseNum(cOrder.value);
    var total = n * cost, orders = n * rate / 100, sales = orders * order;
    outCount.textContent = fa.format(n) + ' پیامک';
    outRate.textContent = fa1.format(rate) + '٪';
    tween(resCost, total, money);
    tween(resOrders, orders, function (v) { return '≈ ' + fa.format(Math.round(v)) + ' خرید'; });
    tween(resSales, sales, money);
    resRoi.textContent = total > 0 && sales > 0 ? '≈ ' + fa1.format(sales / total) + ' برابر هزینه کمپین' : '';
    // The stage beside the calculator shows 200 recipients; this is how many of them buy.
    var buyers = Math.round(200 * rate / 100);
    if (outBuyers) outBuyers.textContent = buyers > 0 ? fa.format(buyers) + ' نفر' : 'کمتر از یک نفر';
    if (outBuySum) {
      outBuySum.textContent = '';
      if (buyers > 0 && order > 0) {
        var b = document.createElement('b');
        b.textContent = money(buyers * order);
        outBuySum.appendChild(document.createTextNode('= '));
        outBuySum.appendChild(b);
        outBuySum.appendChild(document.createTextNode(' فروش'));
      }
    }
    S.rate = rate;
    S.order = order;
    refreshScene();
  }
  if (cCount) {
    [cCount, cRate, cCost, cOrder].forEach(function (el) { el.addEventListener('input', campaign); });
    [cCost, cOrder].forEach(function (el) {
      el.addEventListener('blur', function () { var v = parseNum(el.value); el.value = v ? fa.format(v) : ''; });
      el.addEventListener('focus', function () { el.select(); });
    });
    campaign();
  }

  // ---------- 3. A solution for every customer ----------
  var SEGMENTS = [
    {
      goal: 'افزایش خرید با پیشنهادهای اختصاصی',
      sms: [
        'نگار جان، یه خبر خوب برات داریم 🌷\n',
        'محصولی که قبلاً ازش خرید کرده بودی، دوباره موجود شده. برای خرید این محصول، ۱۵٪ تخفیف اختصاصی برات در نظر گرفتیم.\n',
        'مشاهده و خرید: ', { link: 'لینک محصول' }
      ]
    },
    {
      goal: 'ترغیب به خرید مجدد پس از اولین خرید',
      sms: [
        'آرش عزیز، از اینکه اولین خریدت رو از ما انجام دادی خوشحالیم 😊\n',
        'برای خرید بعدی‌ات، ارسال رایگان به‌عنوان هدیه خوش‌آمدگویی برات در نظر گرفتیم.\n',
        'مشاهده محصولات: ', { link: 'لینک فروشگاه' }
      ]
    },
    {
      goal: 'پیشنهاد مناسب برای بازگشت به خرید',
      sms: [
        'سارا جان، جای خریدهای قبلیت توی سفارش‌های ما خالیه 💚\n',
        'خوشحال می‌شیم دوباره میزبانت باشیم. برای خرید مجددت، ۲۰٪ تخفیف در نظر گرفتیم که تا پایان این ماه فرصت استفاده ازش رو داری.\n',
        'کد تخفیف: ', { code: 'BACK20' }, '\n',
        'خرید از فروشگاه: ', { link: 'لینک فروشگاه' }
      ]
    }
  ];

  var tabs = $$('.tab[data-seg]');
  var segPanel = $('#seg-panel'), segGoal = $('[data-goal]'), bubble = $('[data-sms]'), typing = $('.typing');
  var segIndex = 0, segTimer = 0, typeTimer = 0, segAuto = true, segVisible = false;
  var seg = $('.seg'), wire = $('.seg__wire path'), wireSms = $('.seg__sms');
  var crowdCanvas = $('[data-stage="crowd"] canvas'), phoneEl = $('.phone'), smsRaf = 0;
  var WIRE = ['var(--teal)', 'var(--amber)', 'var(--rose)'];
  var SEG_MS = 8000;
  var LAST_SEG = SEGMENTS.length - 1;

  function renderSms(parts) {
    bubble.textContent = '';
    parts.forEach(function (p) {
      if (typeof p === 'string') { bubble.appendChild(document.createTextNode(p)); return; }
      var span = document.createElement('span');
      span.className = p.code ? 'sms-code' : 'sms-link';
      span.textContent = p.code || p.link;
      bubble.appendChild(span);
    });
    var time = document.createElement('time');
    time.textContent = 'همین حالا';
    bubble.appendChild(time);
  }

  // The message's way from the chosen group of customers to the phone: up from the group, over the
  // top and into the side of the phone. Only drawn when the phone stands beside the groups.
  function wirePath(i) {
    if (!seg || !wire || !crowdCanvas || !phoneEl || typeof S.crowdAnchor !== 'function') return '';
    if (getComputedStyle(wire.parentNode).display === 'none') return '';
    var a = S.crowdAnchor(i), sr = seg.getBoundingClientRect(), cr = crowdCanvas.getBoundingClientRect(), pr = phoneEl.getBoundingClientRect();
    if (!cr.width || !pr.width) return '';
    var sx = cr.left - sr.left + a.x, sy = cr.top - sr.top + a.y - a.r;
    var ex = pr.right - sr.left + 2, ey = pr.top - sr.top + Math.min(150, pr.height * .3);
    var top = cr.top - sr.top - 30;
    return 'M' + sx.toFixed(1) + ' ' + sy.toFixed(1) + 'C' + sx.toFixed(1) + ' ' + top.toFixed(1) + ' ' +
      (ex + 70).toFixed(1) + ' ' + ey.toFixed(1) + ' ' + ex.toFixed(1) + ' ' + ey.toFixed(1);
  }
  function sendSms(i, animate) {
    if (!seg || !wire) return;
    seg.style.setProperty('--wire', WIRE[i]);
    var d = wirePath(i);
    wire.setAttribute('d', d);
    cancelAnimationFrame(smsRaf);
    wireSms.classList.remove('is-on');
    if (!d || !animate || reduceMotion) return;
    var len = wire.getTotalLength(), t0 = performance.now(), dur = 850;
    wireSms.classList.add('is-on');
    (function fly(now) {
      var k = Math.min(1, (now - t0) / dur), e = k < .5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      var pt = wire.getPointAtLength(e * len);
      wireSms.style.left = pt.x + 'px';
      wireSms.style.top = pt.y + 'px';
      if (k < 1) smsRaf = requestAnimationFrame(fly); else wireSms.classList.remove('is-on');
    })(t0);
  }
  var wireTimer = 0;
  window.addEventListener('resize', function () {
    clearTimeout(wireTimer);
    wireTimer = setTimeout(function () { sendSms(segIndex, false); }, 120);
  });
  window.addEventListener('load', function () { sendSms(segIndex, false); });

  function showSegment(i, animate) {
    segIndex = i;
    S.seg = i;
    refreshScene();
    sendSms(i, animate);
    var seg = SEGMENTS[i];
    tabs.forEach(function (t, k) {
      var on = k === i;
      t.classList.toggle('is-active', on);
      t.classList.toggle('is-auto', on && segAuto && segVisible && !reduceMotion && i < LAST_SEG);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
    });
    if (segPanel) segPanel.setAttribute('aria-labelledby', 'tab-' + i);
    if (segGoal) segGoal.textContent = seg.goal;

    clearTimeout(typeTimer);
    if (animate && !reduceMotion) {
      bubble.classList.remove('show');
      typing.classList.add('show');
      typeTimer = setTimeout(function () {
        typing.classList.remove('show');
        renderSms(seg.sms);
        bubble.classList.add('show');
      }, 900);
    } else {
      typing.classList.remove('show');
      renderSms(seg.sms);
      bubble.classList.add('show');
    }
    scheduleNextSegment();
  }

  // The samples play through once on their own, then wait on the last one for the reader.
  function scheduleNextSegment() {
    clearTimeout(segTimer);
    if (!segAuto || !segVisible || reduceMotion) return;
    if (segIndex >= LAST_SEG) { segAuto = false; return; }
    segTimer = setTimeout(function () { showSegment(segIndex + 1, true); }, SEG_MS);
  }

  if (tabs.length && bubble) {
    document.documentElement.style.setProperty('--tab-ms', SEG_MS + 'ms');
    tabs.forEach(function (t, k) {
      t.addEventListener('click', function () { segAuto = false; showSegment(k, true); });
      t.addEventListener('keydown', function (e) {
        var next = null;
        if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') next = (k + 1) % tabs.length;
        else if (e.key === 'ArrowRight' || e.key === 'ArrowUp') next = (k + tabs.length - 1) % tabs.length;
        else if (e.key === 'Home') next = 0;
        else if (e.key === 'End') next = tabs.length - 1;
        if (next === null) return;
        e.preventDefault();
        segAuto = false;
        showSegment(next, true);
        tabs[next].focus();
      });
    });
    showSegment(0, false);
    var segSection = $('#solutions');
    if ('IntersectionObserver' in window && segSection) {
      var firstView = true;
      new IntersectionObserver(function (entries) {
        segVisible = entries[0].isIntersecting;
        if (segVisible && firstView) { firstView = false; showSegment(segIndex, true); }
        else if (segVisible) scheduleNextSegment();
        else { clearTimeout(segTimer); tabs.forEach(function (t) { t.classList.remove('is-auto'); }); }
      }, { threshold: 0.35 }).observe(segSection);
    }
  }

  // ---------- 4. Steps timeline ----------
  var stepsList = $('#steps');
  var stepItems = $$('.step');
  function stepsProgress() {
    if (!stepsList) return;
    var rect = stepsList.getBoundingClientRect();
    var line = window.innerHeight * 0.62;
    var p = clamp01((line - rect.top) / Math.max(1, rect.height));
    stepsList.style.setProperty('--sp', p.toFixed(3));
    stepItems.forEach(function (li) { li.classList.toggle('is-on', li.getBoundingClientRect().top + 28 < line); });
  }

  // ---------- 5. Before and after ----------
  var cmpRange = $('#compare-range'), cmpBox = $('.compare');
  var cmpTouched = false, cmpRaf = 0;
  function setCompare(v) {
    if (!cmpRange) return;
    cmpRange.value = v;
    fillRange(cmpRange);
    cmpBox.style.setProperty('--cmp', (v / 100).toFixed(3));
    cmpBox.setAttribute('data-state', v >= 50 ? '1' : '0');
    S.compare = v / 100;
    refreshScene();
  }
  if (cmpRange) {
    cmpRange.addEventListener('input', function () {
      cmpTouched = true;
      cancelAnimationFrame(cmpRaf);
      setCompare(+cmpRange.value);
    });
    setCompare(0);
    if ('IntersectionObserver' in window) {
      var cmpIO = new IntersectionObserver(function (entries) {
        if (!entries[0].isIntersecting) return;
        cmpIO.disconnect();
        if (cmpTouched) return;
        if (reduceMotion) { setCompare(100); return; }
        var t0 = performance.now() + 500, dur = 2600;
        (function step(now) {
          if (cmpTouched) return;
          var k = clamp01((now - t0) / dur);
          var e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
          setCompare(Math.round(e * 100));
          if (k < 1) cmpRaf = requestAnimationFrame(step);
        })(performance.now());
      }, { threshold: 0.55 });
      cmpIO.observe(cmpBox);
    }
  }

  // ---------- 6. Lost sales ----------
  var lCount = $('#l-count'), outLost = $('[data-out="lost"]');
  var resMonth = $('[data-res="lost-month"]'), resYear = $('[data-res="lost-year"]');
  var outLostN = $('[data-lost-n]');
  function lostSales() {
    if (!lCount) return;
    var n = +lCount.value;
    var month = n * AVG_ORDER;
    outLost.textContent = fa.format(n) + ' مشتری';
    S.lostN = n;
    S.avg = AVG_ORDER;
    tween(resMonth, month, money);
    tween(resYear, month * 12, money);
    if (outLostN) outLostN.textContent = fa.format(n) + ' مشتری';
    var min = +lCount.min || 10, max = +lCount.max || 3000;
    S.lost = 0.05 + 0.5 * Math.log(n / min) / Math.log(max / min);
    refreshScene();
  }
  if (lCount) { lCount.addEventListener('input', lostSales); lostSales(); }

  // ---------- 7. Free consultation ----------
  var form = $('#consult-form');
  if (form) {
    var phone = $('#consult-phone'), msg = $('.consult__msg', form), submit = $('button[type="submit"]', form);
    var busy = false;

    var normalizePhone = function (v) {
      v = toLatin(v).replace(/[\s\-().]/g, '');
      if (/^00989\d{9}$/.test(v)) v = '0' + v.slice(4);
      else if (/^\+989\d{9}$/.test(v)) v = '0' + v.slice(3);
      else if (/^989\d{9}$/.test(v)) v = '0' + v.slice(2);
      else if (/^9\d{9}$/.test(v)) v = '0' + v;
      return v;
    };
    var say = function (text, ok) {
      msg.textContent = text;
      msg.className = 'consult__msg ' + (ok ? 'ok' : 'err');
    };
    var persian = /[؀-ۿ]/;

    phone.addEventListener('input', function () {
      phone.removeAttribute('aria-invalid');
      if (msg.classList.contains('err')) { msg.textContent = ''; msg.className = 'consult__msg'; }
    });

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (busy) return;
      var number = normalizePhone(phone.value);
      if (!/^09\d{9}$/.test(number)) {
        phone.setAttribute('aria-invalid', 'true');
        say('شماره موبایل را درست وارد کنید؛ مثلاً ۰۹۱۲۳۴۵۶۷۸۹', false);
        phone.focus();
        return;
      }
      busy = true;
      form.classList.add('is-loading');
      form.classList.remove('is-done');
      submit.setAttribute('aria-busy', 'true');
      msg.textContent = ''; msg.className = 'consult__msg';

      var controller = window.AbortController ? new AbortController() : null;
      var timer = controller ? setTimeout(function () { controller.abort(); }, 15000) : 0;
      // A simple form post (no custom headers) so the browser sends it without a CORS preflight.
      fetch((cfg.apiBase || '') + '/free-consults/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8', 'Accept': 'application/json' },
        body: 'phone_number=' + encodeURIComponent(number),
        signal: controller ? controller.signal : undefined
      }).then(function (res) {
        return res.json().catch(function () { return null; }).then(function (body) { return { res: res, body: body }; });
      }).then(function (r) {
        var serverMsg = r.body && typeof r.body.message === 'string' && persian.test(r.body.message) ? r.body.message : '';
        if (r.res.ok) {
          say('درخواستتان ثبت شد. کارشناسان ریتانو به‌زودی با شما تماس می‌گیرند.', true);
          form.classList.add('is-done');
          phone.value = '';
          if (typeof S.burst === 'function') S.burst();
        } else if (r.res.status === 400) {
          phone.setAttribute('aria-invalid', 'true');
          say(serverMsg || 'شماره واردشده معتبر نیست. لطفاً دوباره بررسی کنید.', false);
        } else if (r.res.status === 429) {
          say(serverMsg || 'تعداد درخواست‌ها زیاد بوده است. لطفاً چند دقیقه دیگر دوباره تلاش کنید.', false);
        } else {
          say(serverMsg || 'ثبت درخواست انجام نشد. لطفاً دوباره تلاش کنید.', false);
        }
      }).catch(function () {
        say('ارتباط با سرور برقرار نشد. لطفاً دوباره تلاش کنید یا با ۰۲۱-۹۱۰۳۶۰۶۰ تماس بگیرید.', false);
      }).then(function () {
        clearTimeout(timer);
        busy = false;
        form.classList.remove('is-loading');
        submit.removeAttribute('aria-busy');
      });
    });
  }

  // ---------- Scroll-driven bits ----------
  var ticking = false, root = document.documentElement;
  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      ticking = false;
      var y = window.pageYOffset;
      if (nav) nav.classList.toggle('is-scrolled', y > 12);
      root.style.setProperty('--p', clamp01(y / Math.max(1, root.scrollHeight - window.innerHeight)).toFixed(4));
      stepsProgress();
    });
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll);
  onScroll();
})();
