// Service worker: the only place that talks to the Lambda. Content scripts are
// subject to the page's CORS rules; the worker is not (host_permissions covers it).
importScripts('config.js');

async function send(job) {
  const res = await fetch(JOB_RADAR_CONFIG.endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-job-radar-key': JOB_RADAR_CONFIG.key },
    body: JSON.stringify({ jobs: [job] }),
  });
  const body = await res.json().catch(() => ({}));
  return { status: res.status, body };
}

async function bump(result) {
  const { stats = { stored: 0, duplicates: 0, errors: 0 } } = await chrome.storage.local.get('stats');
  if (result.status === 200 && result.body.stored) stats.stored += 1;
  else if (result.status === 200 && result.body.duplicates) stats.duplicates += 1;
  else stats.errors += 1;
  await chrome.storage.local.set({ stats });
  await chrome.action.setBadgeText({ text: String(stats.stored) });
  await chrome.action.setBadgeBackgroundColor({ color: stats.errors ? '#B3261E' : '#1A3C5E' });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== 'JOB_RADAR_SEND') return;
  send(msg.job)
    .then(async (result) => { await bump(result); sendResponse(result); })
    .catch(async (err) => {
      const result = { status: 0, body: {}, error: err.message };
      await bump(result);
      sendResponse(result);
    });
  return true; // keep the message channel open for the async response
});
