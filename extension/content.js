const JR_VERSION = '0.3.4';
console.log(`[job-radar] content script loaded v${JR_VERSION}`, window.location.href);

const SELECTORS = {
  title: [
    '.job-details-jobs-unified-top-card__job-title h1',
    '.job-details-jobs-unified-top-card__job-title',
    '.jobs-unified-top-card__job-title',
    'h1',
  ],
  company: [
    '.job-details-jobs-unified-top-card__company-name',
    '.jobs-unified-top-card__company-name',
  ],
  place: [
    '.job-details-jobs-unified-top-card__primary-description-container',
    '.jobs-unified-top-card__bullet',
  ],
  description: [
    '#job-details',
    '.jobs-description__content',
    '.jobs-description-content__text',
    '.jobs-box__html-content',
  ],
};

function firstText(selectors) {
  for (const sel of selectors) {
    const text = document.querySelector(sel)?.innerText?.trim();
    if (text) return text;
  }
  return null;
}

// Fallback: find the "About the job" heading, walk up to a container with real text.
function descriptionByHeading() {
  const heading = [...document.querySelectorAll('h2, h3, span')].find(
    (el) => el.textContent.trim().toLowerCase() === 'about the job'
  );
  if (!heading) return null;
  let node = heading.parentElement;
  while (node && node !== document.body && node.innerText.trim().length < 200) {
    node = node.parentElement;
  }
  return node && node !== document.body ? node.innerText.trim() : null;
}

function getJobId() {
  const url = new URL(window.location.href);
  return (
    url.searchParams.get('currentJobId') ||
    url.pathname.match(/\/jobs\/view\/(\d+)/)?.[1] ||
    null
  );
}

// LinkedIn's newer layout has no h1, no #job-details and obfuscated class names, so we also locate
// things by structure: the detail pane is the ancestor of the Save button that holds "About the job".
const BTN_ID = 'job-radar-btn';
const HOST_ID = 'job-radar-host';
const SAVE_RE = /^(save|saved|unsave|enregistrer|enregistré|ne plus enregistrer)\b/i;
const META_RE = /\b(ago|applicants?|clicked apply|hours?|days?|weeks?|months?|minutes?|il y a|candidat)/i;
const isSaveButton = (b) => b.id !== BTN_ID && SAVE_RE.test((b.getAttribute('aria-label') || b.innerText || '').trim());

// Save buttons inside <li> belong to the results list cards, never to the detail pane.
const findSaveButton = () => [...document.querySelectorAll('button')].find((b) => isSaveButton(b) && !b.closest('li')) || null;

function findPane() {
  const save = findSaveButton();
  if (!save) return null;
  const hasHeading = (node) =>
    [...node.querySelectorAll('h2, h3, span, p')].some((e) => /^about the job$/i.test(e.textContent.trim()));
  let node = save.parentElement;
  while (node && node !== document.body && !hasHeading(node)) node = node.parentElement;
  return node && node !== document.body ? node : null;
}

function structural() {
  const jobId = getJobId();
  const root = findPane() || document;
  const titleLink =
    (jobId && root.querySelector(`a[href*="/jobs/view/${jobId}"]`)) || root.querySelector('a[href*="/jobs/view/"]');
  const companyLink = [...root.querySelectorAll('a[href*="/company/"]')].find((a) => a.innerText?.trim());
  let place = null;
  if (titleLink) {
    // The first <p> after the title that has several spans, one of them like "4 days ago" / "48 applicants".
    const meta = [...root.querySelectorAll('p')].find(
      (p) =>
        titleLink.compareDocumentPosition(p) & Node.DOCUMENT_POSITION_FOLLOWING &&
        p.querySelectorAll('span').length >= 2 &&
        [...p.querySelectorAll('span')].some((sp) => META_RE.test(sp.textContent))
    );
    place = meta?.querySelector('span')?.innerText?.trim() || null;
  }
  return {
    title: titleLink?.innerText?.trim() || null,
    company: companyLink?.innerText?.trim() || null,
    place,
  };
}

function capture() {
  const jobId = getJobId();
  const fallback = structural();
  const title = firstText(SELECTORS.title) || fallback.title;
  const company = firstText(SELECTORS.company) || fallback.company;
  const place = (firstText(SELECTORS.place)?.split('·')[0].trim()) || fallback.place || null;
  const description = (firstText(SELECTORS.description) || descriptionByHeading())
    ?.replace(/^about the job\s*/i, '');

  const required = { jobId, title, company, description };
  const missing = Object.entries(required).filter(([, v]) => !v).map(([k]) => k);
  if (missing.length) return { ok: false, error: `Couldn't find: ${missing.join(', ')}` };
  if (description.length < 100) return { ok: false, error: 'Description suspiciously short' };

  return {
    ok: true,
    job: {
      source: 'linkedin-extension',
      jobId,
      title,
      company,
      location: place,
      description,
      url: `https://www.linkedin.com/jobs/view/${jobId}/`,
      capturedAt: new Date().toISOString(),
    },
  };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === 'JOB_RADAR_CAPTURE') {
    const result = capture();
    console.log('[job-radar]', result);
    sendResponse(result);
  }
});

// ---- Record mode: auto-capture each posting you OPEN (never navigates for you) ----
// LinkedIn is a single-page app, so we poll the URL instead of waiting for page loads.
const MAX_ATTEMPTS = 6; // ~12 s for the DOM to catch up with a newly selected job
const sentJobIds = new Set();
let attemptedJobId = null;
let attempts = 0;
let lastDescription = null;

async function recordTick() {
  try {
    const { recordMode } = await chrome.storage.local.get('recordMode');
    if (!recordMode) return;

    const jobId = getJobId();
    if (!jobId || sentJobIds.has(jobId)) return;
    const { captured = {}, deleted = {} } = await chrome.storage.local.get(['captured', 'deleted']);
    if (jobId in captured || jobId in deleted) {
      sentJobIds.add(jobId);
      return;
    }
    if (jobId !== attemptedJobId) {
      attemptedJobId = jobId;
      attempts = 0;
    }
    if (attempts >= MAX_ATTEMPTS) return;
    attempts += 1;

    const result = capture();
    if (!result.ok) return;
    // Same text as the previous job = the right-hand pane hasn't re-rendered yet. Retry next tick.
    if (result.job.description === lastDescription) return;

    lastDescription = result.job.description;
    sentJobIds.add(jobId);
    chrome.runtime.sendMessage({ type: 'JOB_RADAR_SEND', job: result.job });
  } catch (err) {
    // "Extension context invalidated" after a reload: harmless, the page just needs a refresh.
    console.debug('[job-radar] record tick skipped:', err.message);
  }
}

setInterval(recordTick, 2000);

// ---- Capture button: injected next to Save in the job detail pane ----
const LABELS = {
  idle: 'Capture',
  busy: 'Capturing…',
  captured: '✓ Captured',
  applied: '✓ Applied',
  error: 'Retry capture',
};

// LinkedIn's layout around the Apply/Save row is fragile: inserting anything next to it squeezed or overlapped
// the buttons. So we never insert into it. Our button lives in a fixed-position host on <body> and is placed
// beside Save from its on-screen rectangle; it hides when Save is covered (modal, sticky bar) or scrolled away.
function makeHost(btn) {
  const host = document.createElement('div');
  host.id = HOST_ID;
  Object.assign(host.style, { position: 'fixed', zIndex: 9999, display: 'flex', alignItems: 'stretch' });
  host.appendChild(btn);
  return host;
}

function positionHost(host, anchor) {
  const btn = host.querySelector('button');
  if (!btn) return;
  if (!anchor) {
    // No Save button found: float above LinkedIn's messaging dock instead.
    Object.assign(host.style, { display: 'flex', left: 'auto', top: 'auto', right: '24px', bottom: '90px' });
    btn.style.height = '';
    return;
  }
  const r = anchor.getBoundingClientRect();
  let visible = r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
  if (visible && typeof document.elementFromPoint === 'function') {
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    visible = !!hit && (hit === anchor || anchor.contains(hit)); // Save is actually on top, not under a modal/bar
  }
  if (!visible) {
    host.style.display = 'none';
    return;
  }
  Object.assign(host.style, {
    display: 'flex',
    left: `${Math.round(r.right + 8)}px`,
    top: `${Math.round(r.top)}px`,
    right: 'auto',
    bottom: 'auto',
  });
  btn.style.height = `${Math.round(r.height)}px`; // same height as LinkedIn's pill
}

function setBtn(btn, state, detail) {
  btn.dataset.state = state;
  (btn.__label || btn).textContent = LABELS[state];
  btn.setAttribute('aria-label', LABELS[state]);
  btn.disabled = state === 'busy' || state === 'captured' || state === 'applied';
  btn.title =
    state === 'captured' ? `Stored. Fit score ${detail ?? '?'}/100` :
    state === 'applied' ? 'Deleted from storage after applying' :
    state === 'error' ? detail || 'Capture failed' : '';
}

function makeBtn(anchor) {
  let btn;
  if (anchor) {
    // Clone LinkedIn's own Save button so classes, fonts and colours match in any theme. cloneNode copies
    // no event listeners; strip ids and tracking attributes so LinkedIn's delegated handlers ignore it.
    btn = anchor.cloneNode(true);
    for (const attr of [...btn.attributes]) {
      if (attr.name.startsWith('data-') || ['id', 'aria-pressed', 'aria-describedby', 'aria-labelledby'].includes(attr.name)) {
        btn.removeAttribute(attr.name);
      }
    }
    btn.querySelectorAll('[id]').forEach((n) => n.removeAttribute('id'));
    btn.querySelectorAll('svg, img').forEach((n) => n.remove());
    btn.__label = [...btn.querySelectorAll('*')].find((e) => !e.children.length && e.textContent.trim()) || null;
    Object.assign(btn.style, { flex: '0 0 auto', width: 'auto', minWidth: '0', margin: '0', whiteSpace: 'nowrap' });
  } else {
    btn = document.createElement('button');
    // Fallback when LinkedIn's markup changes: a floating button (above the messaging dock) that always works.
    Object.assign(btn.style, {
      padding: '10px 18px',
      borderRadius: '999px', border: '1px solid #1A3C5E', background: '#1A3C5E', color: '#fff',
      font: '600 14px system-ui', cursor: 'pointer',
    });
  }
  btn.id = BTN_ID;
  btn.type = 'button';
  btn.addEventListener('click', async (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (btn.dataset.state !== 'idle' && btn.dataset.state !== 'error') return;
    setBtn(btn, 'busy');
    const result = capture();
    if (!result.ok) return setBtn(btn, 'error', result.error);
    try {
      const res = await chrome.runtime.sendMessage({ type: 'JOB_RADAR_SEND', job: result.job });
      const done = res.status === 200 && (res.body.stored || res.body.duplicates);
      if (done) setBtn(btn, 'captured', res.body.scores?.[0]);
      else setBtn(btn, 'error', res.status === 401 ? 'Server rejected the key (check config.js)' : `Failed (${res.status || res.error})`);
    } catch (err) {
      setBtn(btn, 'error', err.message);
    }
  });
  return btn;
}

let lastMode = null;
function logMode(mode) {
  if (mode !== lastMode) {
    lastMode = mode;
    console.info(`[job-radar] button: ${mode}`);
  }
}

async function uiTick() {
  try {
    const jobId = getJobId();
    let host = document.getElementById(HOST_ID);
    if (!jobId) {
      host?.remove();
      logMode('no currentJobId in the URL, nothing to show');
      return;
    }
    const anchor = findSaveButton();
    logMode(anchor ? 'beside the Save button (overlay)' : 'floating (Save button not found)');
    if (host && (host.dataset.anchored === '1') !== !!anchor) {
      host.remove(); // look source changed (cloned vs standalone style), rebuild
      host = null;
    }
    if (!host || !host.querySelector('button')) {
      host?.remove();
      const fresh = makeBtn(anchor);
      host = makeHost(fresh);
      host.dataset.anchored = anchor ? '1' : '0';
      setBtn(fresh, 'idle');
      document.body.appendChild(host);
    }
    positionHost(host, anchor);
    const btn = host.querySelector('button');
    if (btn.dataset.state === 'busy') return;

    const { captured = {}, deleted = {} } = await chrome.storage.local.get(['captured', 'deleted']);
    if (jobId in deleted) setBtn(btn, 'applied');
    else if (jobId in captured) setBtn(btn, 'captured', captured[jobId]);
    else if (btn.dataset.state !== 'error' || btn.dataset.job !== jobId) setBtn(btn, 'idle');
    btn.dataset.job = jobId;
  } catch (err) {
    console.warn('[job-radar] button error:', err.message);
  }
}

// Keep the overlay glued to Save while scrolling, resizing or when LinkedIn re-flows the page.
let frame = 0;
function reposition() {
  frame = 0;
  const host = document.getElementById(HOST_ID);
  if (host) positionHost(host, findSaveButton());
}
const scheduleReposition = () => { if (!frame) frame = requestAnimationFrame(reposition); };
window.addEventListener('scroll', scheduleReposition, true); // capture: LinkedIn scrolls an inner pane, not the window
window.addEventListener('resize', scheduleReposition);
setInterval(scheduleReposition, 300);

setInterval(uiTick, 1000);

// Refresh the local mirror of server state at most every 10 minutes per page load.
chrome.storage.local.get('lastSync').then(({ lastSync = 0 }) => {
  if (Date.now() - lastSync > 10 * 60 * 1000) chrome.runtime.sendMessage({ type: 'JOB_RADAR_SYNC' }).catch(() => {});
});
