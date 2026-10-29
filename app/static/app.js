/* Callout Roster — small progressive enhancements. The app works without this file. */
(function () {
  'use strict';

  // Case- and accent-insensitive matching for the person picker and the People search.
  function fold(text) { return (text || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, ''); }

  // Confirm dialogs for destructive forms (data-confirm="…").
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (form.dataset && form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      e.preventDefault();
    }
  });

  // Press-and-hold helper: hold an element matching `selector` for 500ms to
  // run `onHold(el)`. Taps on links/buttons inside it and scrolling do not count.
  function longPress(selector, onHold) {
    var timer = null, pressed = null;
    function clear() { if (timer) clearTimeout(timer); timer = null; if (pressed) pressed.classList.remove('pressing'); pressed = null; }
    document.addEventListener('pointerdown', function (e) {
      var el = e.target.closest && e.target.closest(selector);
      if (!el || e.target.closest('a, button, form, input, textarea, select')) return;
      pressed = el; el.classList.add('pressing');
      timer = setTimeout(function () { clear(); if (navigator.vibrate) navigator.vibrate(20); onHold(el); }, 500);
    });
    ['pointerup', 'pointercancel', 'pointerleave', 'scroll'].forEach(function (ev) { document.addEventListener(ev, clear, true); });
    document.addEventListener('contextmenu', function (e) { if (e.target.closest && e.target.closest(selector)) e.preventDefault(); });
  }
  function wireSheet(sheet) {
    sheet.addEventListener('click', function (e) { if (e.target === sheet || e.target.hasAttribute('data-close')) sheet.close(); });
  }

  // Keep the lead's place in the list: remember the scroll position when a
  // sheet form is submitted and restore it when the page comes back.
  var SCROLL_KEY = 'callout-roster:scroll';
  document.addEventListener('submit', function (e) {
    if (e.target.closest && e.target.closest('.sheet')) {
      try { sessionStorage.setItem(SCROLL_KEY, window.location.pathname + '|' + window.scrollY); } catch (err) {}
    }
  });
  try {
    var saved = sessionStorage.getItem(SCROLL_KEY);
    if (saved) {
      sessionStorage.removeItem(SCROLL_KEY);
      var parts = saved.split('|');
      if (parts[0] === window.location.pathname) {
        if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
        var y = parseInt(parts[1], 10) || 0;
        // Instant, not smooth, and again after load: the browser's own
        // load-time scroll (to top or to the #anchor) would otherwise win.
        var restore = function () { window.scrollTo({ top: y, left: 0, behavior: 'instant' }); };
        restore();
        window.addEventListener('load', function () { restore(); requestAnimationFrame(restore); });
      }
    }
  } catch (err) {}

  // Leads: hold a member's row to set their status in a sheet that looks
  // exactly like the member's own status card.
  var sheet = document.getElementById('mark-sheet');
  if (sheet && sheet.showModal) {
    wireSheet(sheet);
    longPress('.resp-row.markable', function (row) {
      document.getElementById('mark-roster-id').value = row.dataset.rosterId;
      document.getElementById('mark-title').textContent = row.dataset.name;
      sheet.querySelectorAll('.status-btn').forEach(function (b) {
        var cur = b.dataset.status === row.dataset.status;
        b.classList.toggle('current', cur); b.setAttribute('aria-pressed', cur ? 'true' : 'false');
      });
      sheet.showModal();
    });
  }

  // After a lead marks someone En route the page carries an open ETA sheet;
  // promote it to a modal so it sits over the list like the other sheets.
  var etaSheet = document.getElementById('eta-sheet');
  if (etaSheet && etaSheet.showModal) { etaSheet.removeAttribute('open'); wireSheet(etaSheet); etaSheet.showModal(); }

  // People page: Edit opens a sheet filled from the row (no reload).
  var personSheet = document.getElementById('person-sheet');
  if (personSheet && personSheet.showModal) {
    if (personSheet.hasAttribute('open')) { personSheet.removeAttribute('open'); personSheet.showModal(); }
    wireSheet(personSheet);
    var form = document.getElementById('person-form');
    document.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('.edit-person');
      if (!btn) return;
      var row = btn.closest('.person-row'), d = row.dataset;
      e.preventDefault();
      form.action = form.dataset.actionBase + d.personId;
      document.getElementById('person-title').textContent = d.name || d.username;
      var src = { authentik: 'from Authentik', header: 'signed in', manual: 'added by hand' }[d.source] || d.source;
      document.getElementById('person-meta').textContent = d.username + ' \u00b7 ' + src + ' \u00b7 seen ' + d.seen;
      document.getElementById('person-name-field').hidden = d.source === 'authentik';
      document.getElementById('person-name').value = d.name;
      var phone = document.getElementById('person-phone');
      phone.value = d.phoneApp; phone.placeholder = d.phoneSynced || '555-0100';
      document.getElementById('person-phone-hint').textContent = d.phoneSynced
        ? 'Authentik has ' + d.phoneSynced + '; a number here overrides it, blank goes back to it.'
        : 'Used for the Call buttons and the record.';
      var revert = document.getElementById('person-revert-btn');
      revert.hidden = !(d.phoneApp && d.phoneSynced && d.phoneApp !== d.phoneSynced);
      revert.textContent = 'Use Authentik number (' + d.phoneSynced + ')';
      var act = document.getElementById('person-active-btn');
      act.formAction = form.dataset.actionBase + d.personId + '/active';
      act.value = d.active === '1' ? '0' : '1';
      act.textContent = d.active === '1' ? 'Deactivate' : 'Reactivate';
      personSheet.showModal();
      phone.focus();
    });
  }

  // People page: type to narrow the list by name, username or phone.
  var peopleSearch = document.getElementById('people-search');
  if (peopleSearch) {
    var peopleRows = Array.prototype.map.call(document.querySelectorAll('.person-row'), function (row) {
      var d = row.dataset;
      return { row: row, key: fold([d.name, d.username, d.phoneApp, d.phoneSynced].join(' ')) };
    });
    var peopleCount = document.getElementById('people-count'), peopleNone = document.getElementById('people-none');
    function filterPeople() {
      var words = fold(peopleSearch.value).split(/\s+/).filter(Boolean), shown = 0;
      peopleRows.forEach(function (it) {
        var ok = words.every(function (w) { return it.key.indexOf(w) !== -1; });
        it.row.hidden = !ok; if (ok) shown++;
      });
      peopleCount.textContent = words.length ? shown + ' of ' + peopleRows.length : peopleRows.length;
      peopleNone.hidden = shown > 0;
    }
    peopleSearch.addEventListener('input', filterPeople);
    peopleSearch.addEventListener('search', filterPeople);
    if (peopleSearch.value) filterPeople();
  }

  // Hold the tactical brief to edit it.
  var briefSheet = document.getElementById('brief-sheet');
  if (briefSheet && briefSheet.showModal) {
    wireSheet(briefSheet);
    longPress('.brief[data-editable]', function () { briefSheet.showModal(); briefSheet.querySelector('textarea').focus(); });
  }

  // A long brief shows four lines until tapped open, so the buttons and the
  // list stay within reach. The button only appears when something is cut.
  // Re-run after a live update replaces the brief.
  function initBrief() {
    var briefText = document.querySelector('.brief-text.clamped');
    var briefMore = document.querySelector('.brief-more');
    if (!briefText || !briefMore || briefMore.dataset.wired) return;
    briefMore.dataset.wired = '1';
    if (briefText.scrollHeight > briefText.clientHeight + 2) briefMore.hidden = false;
    else briefText.classList.remove('clamped');
    briefMore.addEventListener('click', function () {
      var open = briefText.classList.toggle('clamped') === false;
      briefMore.textContent = open ? 'Show less' : 'Show all';
      briefMore.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }
  initBrief();

  // Glance chips filter the team lists. The filter lives in the URL hash so
  // it survives the periodic reload and a shared link.
  var currentFilter = 'all', applyFilter = null;
  if (document.querySelector('.glance .chip')) {
    var COMING = { acknowledged: 1, en_route: 1, on_scene: 1 };
    var labels = { not_contacted: 'not contacted', no_reply: 'no reply', coming: 'coming', on_scene: 'on scene', unavailable: 'unavailable' };
    function rowMatches(row, f) {
      var s = row.dataset.state;
      if (f === 'all') return true;
      if (f === 'coming') return !!COMING[s];
      return s === f;
    }
    applyFilter = function (f, scroll) {
      if (!labels[f]) f = 'all';
      currentFilter = f;
      document.querySelectorAll('.glance .chip').forEach(function (c) { c.classList.toggle('active', c.dataset.filter === f); c.setAttribute('aria-pressed', c.dataset.filter === f ? 'true' : 'false'); });
      document.querySelectorAll('.resp-group').forEach(function (group) {
        var rows = group.querySelectorAll('.resp-row'), shown = 0;
        rows.forEach(function (row) { var ok = rowMatches(row, f); row.hidden = !ok; if (ok) shown++; });
        var empty = group.querySelector('.filter-empty');
        if (empty) empty.hidden = !(f !== 'all' && rows.length && !shown);
      });
      var note = document.querySelector('.filter-note');
      if (note) { note.hidden = f === 'all'; document.getElementById('filter-label').textContent = labels[f] || ''; }
      var hash = f === 'all' ? '' : '#f=' + f;
      if (window.location.hash !== hash) history.replaceState(null, '', window.location.pathname + window.location.search + hash);
      if (scroll) document.getElementById('teams').scrollIntoView({ block: 'start' });
    };
    document.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('[data-filter]');
      if (!btn) return;
      var f = btn.dataset.filter;
      if (btn.classList.contains('active')) f = 'all';
      applyFilter(f, f !== 'all');
    });
    var m = /^#f=(\w+)$/.exec(window.location.hash);
    if (m) applyFilter(m[1], false);
  }

  // Searchable person picker: <select data-picker> becomes a search box with a
  // filtered list. The select stays in the form (hidden) and carries the value,
  // so the form posts exactly as it does without JavaScript.
  var pickerCount = 0;
  document.querySelectorAll('select[data-picker]').forEach(function (select) {
    var id = 'picker-' + (++pickerCount);
    var required = select.required;
    var items = Array.prototype.filter.call(select.options, function (o) { return o.value; }).map(function (o) {
      return { value: o.value, label: o.textContent, key: fold(o.textContent) };
    });
    var wrap = document.createElement('div');
    wrap.className = 'picker';
    var input = document.createElement('input');
    input.type = 'search'; input.className = 'input'; input.autocomplete = 'off'; input.spellcheck = false;
    input.placeholder = 'Type a name or number…';
    input.setAttribute('role', 'combobox'); input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false'); input.setAttribute('aria-controls', id);
    var list = document.createElement('ul');
    list.className = 'picker-list'; list.id = id; list.setAttribute('role', 'listbox'); list.hidden = true;
    var count = document.createElement('span');
    count.className = 'field-hint picker-count';
    wrap.appendChild(input); wrap.appendChild(list); wrap.appendChild(count);
    select.hidden = true; select.required = false;
    select.parentNode.insertBefore(wrap, select);
    var shown = [], active = -1;
    var current = select.selectedOptions[0];
    if (current && current.value) input.value = current.textContent;

    function matches() {
      var words = fold(input.value).split(/[\s,()]+/).filter(Boolean);
      return items.filter(function (it) { return words.every(function (w) { return it.key.indexOf(w) !== -1; }); });
    }
    function render() {
      shown = matches(); active = shown.length ? 0 : -1;
      list.textContent = '';
      shown.forEach(function (it, i) {
        var li = document.createElement('li');
        li.id = id + '-' + i; li.setAttribute('role', 'option'); li.textContent = it.label;
        li.dataset.index = i;
        list.appendChild(li);
      });
      if (!shown.length) {
        var none = document.createElement('li');
        none.className = 'picker-empty'; none.textContent = 'Nobody matches \u201c' + input.value.trim() + '\u201d';
        list.appendChild(none);
      }
      count.textContent = input.value.trim() ? shown.length + ' of ' + items.length : items.length + ' to choose from';
      highlight(); open(true);
    }
    function highlight() {
      Array.prototype.forEach.call(list.children, function (li, i) {
        li.classList.toggle('active', i === active); li.setAttribute('aria-selected', i === active ? 'true' : 'false');
      });
      if (active >= 0) { input.setAttribute('aria-activedescendant', id + '-' + active); list.children[active].scrollIntoView({ block: 'nearest' }); }
      else input.removeAttribute('aria-activedescendant');
    }
    function open(yes) { list.hidden = !yes; input.setAttribute('aria-expanded', yes ? 'true' : 'false'); }
    function choose(it) {
      select.value = it.value; input.value = it.label; input.setCustomValidity('');
      open(false);
    }

    // With someone already chosen, focusing selects the text (typing replaces it) and lists everyone.
    input.addEventListener('focus', function () {
      if (select.value) { input.select(); renderAll(); } else render();
    });
    function renderAll() {
      var typed = input.value; input.value = ''; render(); input.value = typed; input.select();
      active = shown.findIndex(function (it) { return it.value === select.value; }); highlight();
    }
    input.addEventListener('input', function () { select.value = ''; input.setCustomValidity(''); render(); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (list.hidden) { render(); return; }
        if (shown.length) { active = (active + (e.key === 'ArrowDown' ? 1 : -1) + shown.length) % shown.length; highlight(); }
      } else if (e.key === 'Enter' && !list.hidden) {
        e.preventDefault();
        if (active >= 0) choose(shown[active]);
      } else if (e.key === 'Escape' && !list.hidden) {
        e.preventDefault(); open(false);
      }
    });
    // pointerdown + preventDefault keeps focus in the box, so the list does not close before the tap lands.
    list.addEventListener('pointerdown', function (e) { e.preventDefault(); });
    list.addEventListener('click', function (e) {
      var li = e.target.closest('li[data-index]');
      if (li) choose(shown[+li.dataset.index]);
    });
    input.addEventListener('blur', function () {
      open(false);
      if (!select.value && input.value.trim()) {
        var hit = matches();
        if (hit.length === 1) choose(hit[0]);
      }
    });
    select.form.addEventListener('submit', function (e) {
      if (!select.value && input.value.trim()) {
        var hit = matches();
        if (hit.length === 1) choose(hit[0]);
      }
      var msg = '';
      if (input.value.trim() && !select.value) msg = 'Pick someone from the list.';
      else if (required && !select.value) msg = 'Choose a person.';
      if (msg) { e.preventDefault(); e.stopImmediatePropagation(); input.setCustomValidity(msg); input.reportValidity(); }
    });
  });

  // "Use my current location" (needs HTTPS or localhost; the proxy provides TLS).
  var geoBtn = document.getElementById('geo-btn');
  var geoStatus = document.getElementById('geo-status');
  if (geoBtn) {
    geoBtn.addEventListener('click', function () {
      if (!navigator.geolocation) { say('Location not available in this browser.'); return; }
      say('Locating…');
      geoBtn.disabled = true;
      navigator.geolocation.getCurrentPosition(function (pos) {
        setLatLon(pos.coords.latitude.toFixed(5), pos.coords.longitude.toFixed(5));
        say('±' + Math.round(pos.coords.accuracy) + ' m');
        geoBtn.disabled = false;
      }, function (err) {
        say(err.code === 1 ? 'Location permission denied.' : 'Could not get a fix; type it in.');
        geoBtn.disabled = false;
      }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 30000 });
    });
  }

  function setLatLon(lat, lon) {
    var c = document.getElementById('coords');
    if (c) c.value = lat + ', ' + lon;
  }
  function say(msg) { if (geoStatus) geoStatus.textContent = msg; }

  // Live updates while a callout is open. Every few seconds the page asks the
  // server for a version number (the id of the last event). Only when it has
  // moved does it fetch the page again, in the background, and replace just the
  // regions marked data-live whose content differs. Nothing scrolls, nothing
  // flashes, open sheets and half-typed fields are left alone, and the member's
  // own status card is never changed under their thumb. Whatever did change
  // glows for a moment. Coming back to the app checks straight away.
  var liveUrl = document.body.dataset.liveUrl;
  if (liveUrl && window.fetch && window.DOMParser) {
    var liveVersion = document.body.dataset.version || '0';
    var livePoll = Math.max(5, parseInt(document.body.dataset.livePoll || '10', 10)) * 1000;
    var liveBusy = false;
    var pageUrl = window.location.pathname + window.location.search.replace(/[?&](ok|err|eta)=[^&]*/g, function (m) { return m[0] === '?' ? '?' : ''; }).replace(/\?&/, '?').replace(/\?$/, '');
    function flash(el) { el.classList.remove('changed'); void el.offsetWidth; el.classList.add('changed'); setTimeout(function () { el.classList.remove('changed'); }, 2000); }
    // The ETA countdown "(in 11m)" ticks every minute; that is not a change worth a glow.
    function rowKey(r) { return r.innerHTML.replace(/\([^)]*\)/g, ''); }
    function swapTeams(oldEl, newEl) {
      var before = {};
      oldEl.querySelectorAll('.resp-row').forEach(function (r) { before[r.id] = rowKey(r); });
      oldEl.innerHTML = newEl.innerHTML;
      oldEl.querySelectorAll('.resp-row').forEach(function (r) { if (before[r.id] !== rowKey(r)) flash(r); });
    }
    function swapChildren(oldEl, newEl, selector, keyAttr) {
      var before = {};
      oldEl.querySelectorAll(selector).forEach(function (c) { before[c.getAttribute(keyAttr)] = c.textContent; });
      oldEl.innerHTML = newEl.innerHTML;
      oldEl.querySelectorAll(selector).forEach(function (c) { if (before[c.getAttribute(keyAttr)] !== c.textContent) flash(c); });
    }
    function applyPage(html) {
      var doc = new DOMParser().parseFromString(html, 'text/html');
      if (!doc.body || !doc.body.dataset.liveUrl) { window.location.replace(pageUrl); return; }   // closed meanwhile: show the closed page
      doc.querySelectorAll('[data-live]').forEach(function (fresh) {
        var key = fresh.dataset.live, cur = document.querySelector('[data-live="' + key + '"]');
        if (!cur || cur.innerHTML === fresh.innerHTML) return;
        if (key === 'teams') swapTeams(cur, fresh);
        else if (key === 'glance') swapChildren(cur, fresh, '.chip', 'data-filter');
        else if (key === 'callouts') swapChildren(cur, fresh, '.callout-card', 'href');
        else cur.innerHTML = fresh.innerHTML;
      });
      if (applyFilter) applyFilter(currentFilter, false);
      initBrief();
    }
    function liveCheck() {
      if (liveBusy || document.visibilityState !== 'visible') return;
      var el = document.activeElement;
      if (el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return;
      if (document.querySelector('dialog[open], .pressing')) return;
      liveBusy = true;
      fetch(liveUrl, { cache: 'no-store', credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (j) {
          if (String(j.v) === liveVersion) return;
          return fetch(pageUrl, { cache: 'no-store', credentials: 'same-origin' })
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
            .then(function (html) { applyPage(html); liveVersion = String(j.v); document.body.dataset.version = liveVersion; });
        })
        .catch(function () {})   // offline or signed out: try again next tick
        .then(function () { liveBusy = false; });
    }
    setInterval(liveCheck, livePoll);
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') liveCheck(); });
    window.addEventListener('pageshow', function (e) { if (e.persisted) liveCheck(); });
  }
})();
