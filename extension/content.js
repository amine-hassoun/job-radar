console.log('[job-radar] content script loaded', window.location.href);

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

function capture() {
  const jobId = getJobId();
  const title = firstText(SELECTORS.title);
  const company = firstText(SELECTORS.company);
  const place = firstText(SELECTORS.place)?.split('·')[0].trim() || null;
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