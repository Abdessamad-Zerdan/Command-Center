// Site-wide behaviour every page shares (Alpine stores, motion layer,
// scroll reveal, card exits...). Was inline in base.html, re-sent with
// every page; now one cached file. Loaded synchronously right after the
// deferred Alpine tag, as before, so its alpine:init hooks are in place
// before Alpine starts.
document.addEventListener('alpine:init', () => {
  // Global, site-wide — any page can call $store.toast.show(message, undoFn)
  // for a lightweight "X happened — Undo" notification. undoFn is optional;
  // omit it for a plain toast with no undo action.
  Alpine.store('toast', {
    visible: false,
    message: '',
    undoFn: null,
    timer: null,
    show(message, undoFn = null) {
      clearTimeout(this.timer);
      this.message = message;
      this.undoFn = undoFn;
      this.visible = true;
      this.timer = setTimeout(() => this.dismiss(), 5000);
    },
    dismiss() {
      clearTimeout(this.timer);
      this.visible = false;
      this.undoFn = null;
    },
    runUndo() {
      if (this.undoFn) this.undoFn();
      this.dismiss();
    },
  });
});

// Cmd/Ctrl+K anywhere on the site opens this — a static route list,
// filtered client-side, no server round-trip. Registered on the
// window so it fires regardless of which element currently has
// focus, same reasoning as the nav rail's own window-level Escape
// handler.
function commandPalette() {
  return {
    open: false,
    query: '',
    active: 0,
    items: [
      { label: 'Brief', href: '/brief' },
      { label: 'Calendar', href: '/calendar' },
      { label: 'Map', href: '/map' },
      { label: 'Competitions', href: '/competitions' },
      { label: 'Research', href: '/research' },
      { label: 'Targets', href: '/targets' },
      { label: 'Projects', href: '/projects' },
      { label: 'Schedule', href: '/schedule' },
      { label: 'History', href: '/history' },
      { label: 'Report', href: '/history/report' },
      { label: 'Settings', href: '/settings' },
      { label: 'Settings — Invites', href: '/settings/invites' },
      { label: 'Settings — Recurring schedule', href: '/settings/schedule' },
      { label: 'Settings — Activity', href: '/settings/activity' },
      { label: 'Settings — Triage rules', href: '/settings/triage-rules' },
      { label: 'Settings — Lane labels', href: '/settings/lane-labels' },
      { label: 'Settings — Knowledge', href: '/settings/knowledge' },
      { label: 'Portfolio', href: '/' },
    ],
    get filtered() {
      const q = this.query.trim().toLowerCase();
      if (!q) return this.items;
      return this.items.filter((i) => i.label.toLowerCase().includes(q));
    },
    onKeydown(e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        this.open ? this.close() : this.launch();
      }
    },
    launch() {
      this.open = true;
      this.query = '';
      this.active = 0;
      this.$nextTick(() => this.$refs.paletteInput.focus());
    },
    close() {
      this.open = false;
    },
    move(delta) {
      const n = this.filtered.length;
      if (!n) return;
      this.active = ((this.active + delta) % n + n) % n;
    },
    go() {
      const item = this.filtered[this.active];
      if (item) window.location.href = item.href;
    },
  };
}

// Shared by every task card's "⋯" actions menu (item_card.html). The
// menu panel is x-teleport'd to <body> — cards in the main lane grid
// are height-capped with a scrolling overflow (see item_card.html's
// own height-cap class), and an absolutely-positioned dropdown living
// *inside* that card would get silently clipped by its own
// scrollbar. Teleporting it out and positioning it with `fixed`
// coordinates computed from the trigger's own bounding rect sidesteps
// that entirely. Alpine still binds the teleported markup to this
// same component scope (including the parent card's `removed` and
// `$store.counts`), so the fetch handlers work exactly as if the
// panel had never moved.
function cardMenu() {
  return {
    open: false,
    top: 0,
    left: 0,
    // Each card's trigger has its own @click.stop (so opening a menu
    // doesn't also fire the card's own "open item detail" click
    // handler) — but .stop also means the click never bubbles up to
    // document, so a *different* card's already-open panel would
    // never see it as an "outside" click and would just stay open
    // forever alongside the new one. window.__openCardMenu tracks
    // whichever instance is currently open so toggle() can close it
    // explicitly before opening a new one.
    close() {
      this.open = false;
      if (window.__openCardMenu === this) window.__openCardMenu = null;
    },
    toggle() {
      if (this.open) {
        this.close();
        return;
      }
      if (window.__openCardMenu) window.__openCardMenu.close();
      const rect = this.$refs.trigger.getBoundingClientRect();
      this.open = true;
      window.__openCardMenu = this;
      this.$nextTick(() => {
        const panelRect = this.$refs.panel.getBoundingClientRect();
        let top = rect.bottom + 4;
        if (top + panelRect.height > window.innerHeight - 8) {
          top = rect.top - panelRect.height - 4;
        }
        let left = rect.right - panelRect.width;
        left = Math.max(8, Math.min(left, window.innerWidth - panelRect.width - 8));
        this.top = top;
        this.left = left;
      });
    },
  };
}

// Scroll reveal: any element with [data-reveal] rises + fades in the
// first time it enters the viewport. No Alpine plugin required (only
// core alpinejs is loaded) — plain IntersectionObserver instead.
//
// window.observeReveals is exposed so any code that swaps in new
// server-rendered HTML after load (e.g. the assistant widget's
// refreshLanes(), which replaces #lanes-container's outerHTML) can
// re-run this — otherwise the freshly inserted [data-reveal]
// elements would sit at their initial opacity:0 forever, since the
// one-time DOMContentLoaded observer never saw them.
//
// Everything that enters together is staggered as one choreographed
// wave — sorted top-left to bottom-right, the way an After Effects
// sequence cascades — rather than each element picking its own delay.
// The delay is cleared once the reveal finishes, so it never lingers
// into the card's hover lift.
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
window.reducedMotion = reducedMotion;

function observeReveals(root) {
  root = root || document;
  // Only arm elements that start below the fold: anything already on
  // screen stays visible, so a slow script load never leaves a blank page.
  const revealEls = [...root.querySelectorAll('[data-reveal]:not(.is-visible)')]
    .filter((el) => {
      if (reducedMotion.matches) return false;
      if (el.classList.contains('reveal-armed')) return true;
      if (el.getBoundingClientRect().top < window.innerHeight - 40) return false;
      el.classList.add('reveal-armed');
      return true;
    });
  if (!revealEls.length) return;
  if (!window.__revealObserver) {
    window.__revealObserver = new IntersectionObserver((entries) => {
      const entering = entries
        .filter((entry) => entry.isIntersecting)
        .sort((a, b) => {
          const ra = a.boundingClientRect, rb = b.boundingClientRect;
          return (ra.top + ra.left * 0.45) - (rb.top + rb.left * 0.45);
        });
      entering.forEach((entry, i) => {
        const el = entry.target;
        const delay = Math.min(i * 35, 210);
        el.style.transitionDelay = delay + 'ms';
        el.classList.add('is-visible');
        window.__revealObserver.unobserve(el);
        setTimeout(() => {
          el.style.transitionDelay = '';
          el.classList.add('reveal-done');
        }, delay + 450);
      });
    }, { threshold: 0.1, rootMargin: '0px 0px -40px 0px' });
  }
  revealEls.forEach((el) => window.__revealObserver.observe(el));
}
window.observeReveals = observeReveals;
// One frame after DOMContentLoaded: the CSS is prebuilt (no more runtime
// Tailwind), so layout is final by then and "below the fold" is real —
// no need to wait for every image and font (`load`) as before.
function observeRevealsOnceLaidOut() {
  requestAnimationFrame(() => observeReveals());
}
if (document.readyState !== 'loading') observeRevealsOnceLaidOut();
else document.addEventListener('DOMContentLoaded', observeRevealsOnceLaidOut);

// A soft light that follows the cursor across any .card-lift surface —
// CSS reads --mx/--my; this only writes them, on whichever card is
// actually under the pointer.
document.addEventListener('pointermove', (e) => {
  if (e.pointerType !== 'mouse') return;
  const card = e.target.closest && e.target.closest('.card-lift');
  if (!card) return;
  const r = card.getBoundingClientRect();
  card.style.setProperty('--mx', (e.clientX - r.left) + 'px');
  card.style.setProperty('--my', (e.clientY - r.top) + 'px');
}, { passive: true });

// html.is-scrolled drives the frosted sticky header.
(function () {
  let ticking = false;
  function update() {
    document.documentElement.classList.toggle('is-scrolled', window.scrollY > 8);
    ticking = false;
  }
  window.addEventListener('scroll', () => {
    if (!ticking) { ticking = true; requestAnimationFrame(update); }
  }, { passive: true });
  document.addEventListener('DOMContentLoaded', update);
})();

// Graceful card exit: a short acknowledgement (a check for "done"),
// then the card folds its height to zero so the cards below glide up
// instead of jumping. `after` runs once it has fully folded — that's
// where callers flip their own `removed` flag. cardExitCancel (used by
// Undo) restores the card immediately if it's still mid-exit.
const EXIT_PROPS = ['height', 'overflow', 'transition', 'margin-bottom', 'padding-top', 'padding-bottom', 'border-width', 'opacity', 'transform', 'filter'];
function clearExitStyles(el, kind) {
  EXIT_PROPS.forEach((p) => el.style.removeProperty(p));
  el.classList.remove('is-exiting', 'exit-' + kind);
}
window.cardExit = function (el, kind, after) {
  if (!el || reducedMotion.matches) { after(); return; }
  const token = {};
  el.__exit = { token, kind };
  el.__exitKind = kind;
  el.classList.add('is-exiting', 'exit-' + kind);
  const hold = kind === 'done' ? 560 : 120;
  setTimeout(() => {
    if (!el.__exit || el.__exit.token !== token) return;
    el.style.height = el.offsetHeight + 'px';
    el.style.overflow = 'hidden';
    void el.offsetHeight;
    el.style.transition = 'height 440ms cubic-bezier(0.22, 1, 0.36, 1), margin 440ms cubic-bezier(0.22, 1, 0.36, 1), padding 440ms cubic-bezier(0.22, 1, 0.36, 1), border-width 440ms ease, opacity 280ms ease, transform 440ms cubic-bezier(0.22, 1, 0.36, 1), filter 280ms ease';
    el.style.height = '0px';
    el.style.marginBottom = '0px';
    el.style.paddingTop = '0px';
    el.style.paddingBottom = '0px';
    el.style.borderWidth = '0px';
    el.style.opacity = '0';
    el.style.transform = kind === 'snooze' ? 'translateX(10px) scale(0.98)' : 'scale(0.97)';
    el.style.filter = 'blur(2px)';
    setTimeout(() => {
      if (!el.__exit || el.__exit.token !== token) return;
      el.__exit = null;
      after();
      // After Alpine's own x-show leave has hidden it, drop the fold
      // styles so an Undo brings it back at its natural height.
      setTimeout(() => {
        if (el.classList.contains('is-exiting')) clearExitStyles(el, kind);
      }, 260);
    }, 450);
  }, hold);
};
// The small date-and-time label on cards: '13 Oct, 14:05' (year only
// when it isn't this one). stampFull is the hover title. Same format as
// timestamps.stamp on the server-rendered task cards.
window.stamp = function (iso) {
  if (!iso) return '';
  const when = new Date(iso);
  if (isNaN(when)) return '';
  const opts = { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false };
  if (when.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return when.toLocaleString('en-GB', opts);
};
window.stampFull = function (iso) {
  if (!iso) return '';
  const when = new Date(iso);
  return isNaN(when) ? '' : when.toLocaleString('en-GB', {
    weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
};
window.cardExitCancel = function (el) {
  if (!el || !el.classList.contains('is-exiting')) return;
  el.__exit = null;
  clearExitStyles(el, el.__exitKind);
};
