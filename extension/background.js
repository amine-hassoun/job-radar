// Service worker: the only place that talks to the Lambda. Content scripts are
// subject to the page's CORS rules; the worker is not (host_permissions covers it).
importScripts('config.js');

async function post(body) {
  const res = await fetch(JOB_RADAR_CONFIG.endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-job-radar-key': JOB_RADAR_CONFIG.key },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { status: res.status, body: data };
}

// Local mirror of server state, so buttons render instantly without a request:
//   captured: { jobId: score }   jobs stored on the server (rebuilt by sync)
//   deleted:  { jobId: timestamp } jobs I deleted after applying (so record mode never re-captures them)
async function bumpStats(result) {
  const { stats = { stored: 0, duplicates: 0, errors: 0 } } = await chrome.storage.local.get('stats');
  if (result.status === 200 && result.body.stored) stats.stored += 1;
  else if (result.status === 200 && result.body.duplicates) stats.duplicates += 1;
  else stats.errors += 1;
  await chrome.storage.local.set({ stats });
  await chrome.action.setBadgeText({ text: String(stats.stored) });
  await chrome.action.setBadgeBackgroundColor({ color: stats.errors ? '#B3261E' : '#1A3C5E' });
}

async function sendJob(job) {
  const result = await post({ jobs: [job] });
  await bumpStats(result);
  if (result.status === 200 && (result.body.stored || result.body.duplicates)) {
    const { captured = {} } = await chrome.storage.local.get('captured');
    captured[job.jobId] = result.body.scores?.[0] ?? captured[job.jobId] ?? 0;
    await chrome.storage.local.set({ captured });
  }
  return result;
}

async function sync() {
  const result = await post({ action: 'list', source: 'linkedin-extension' });
  if (result.status === 200) {
    const captured = {};
    for (const job of result.body.jobs) captured[job.jobId] = job.score;
    await chrome.storage.local.set({ captured, lastSync: Date.now() });
  }
  return result;
}

async function deleteJobs(ids, source = 'linkedin-extension') {
  const result = await post({ action: 'delete', job_ids: ids, source });
  // The local mirror only tracks LinkedIn captures; RemoteOK deletions are remembered server-side (tombstone).
  if (result.status === 200 && source === 'linkedin-extension') {
    const { captured = {}, deleted = {} } = await chrome.storage.local.get(['captured', 'deleted']);
    for (const id of ids) {
      delete captured[id];
      deleted[id] = Date.now();
    }
    await chrome.storage.local.set({ captured, deleted });
  }
  return result;
}

const HANDLERS = {
  JOB_RADAR_SEND: (msg) => sendJob(msg.job),
  JOB_RADAR_API: (msg) => post(msg.body), // list / export, read-only
  JOB_RADAR_SYNC: () => sync(),
  JOB_RADAR_DELETE: (msg) => deleteJobs(msg.ids, msg.source),
};

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const handler = HANDLERS[msg?.type];
  if (!handler) return;
  handler(msg)
    .then(sendResponse)
    .catch(async (err) => {
      const result = { status: 0, body: {}, error: err.message };
      if (msg.type === 'JOB_RADAR_SEND') await bumpStats(result);
      sendResponse(result);
    });
  return true; // keep the message channel open for the async response
});

chrome.runtime.onStartup.addListener(() => sync().catch(() => {}));
chrome.runtime.onInstalled.addListener(() => sync().catch(() => {}));
