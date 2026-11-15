const out = document.getElementById('out');
const toggle = document.getElementById('record');
const statsEl = document.getElementById('stats');

function renderStats(stats = { stored: 0, duplicates: 0, errors: 0 }) {
  statsEl.textContent = `Saved ${stats.stored} · already had ${stats.duplicates} · errors ${stats.errors}`;
}

chrome.storage.local.get(['recordMode', 'stats']).then(({ recordMode, stats }) => {
  toggle.checked = !!recordMode;
  renderStats(stats);
});

toggle.addEventListener('change', () => chrome.storage.local.set({ recordMode: toggle.checked }));
chrome.storage.onChanged.addListener((changes) => changes.stats && renderStats(changes.stats.newValue));

function summarize({ status, body, error }) {
  if (status === 0) return `❌ Network error: ${error}`;
  if (status === 401) return '❌ Server rejected the key (check config.js)';
  if (status !== 200) return `❌ Server error (${status})`;
  if (body.stored) return `✅ Saved · score ${body.scores?.[0] ?? '?'}`;
  if (body.duplicates) return '↺ Already captured';
  return '⚠️ Server rejected the posting (failed validation)';
}

document.getElementById('capture').addEventListener('click', async () => {
  out.textContent = 'Capturing…';
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

  let res;
  try {
    res = await chrome.tabs.sendMessage(tab.id, { type: 'JOB_RADAR_CAPTURE' });
  } catch (err) {
    out.textContent = `❌ ${err.message}\nNo content script here: not a LinkedIn jobs page, or reload the tab.`;
    return;
  }
  if (!res.ok) {
    out.textContent = `❌ ${res.error}`;
    return;
  }

  out.textContent = `Sending "${res.job.title}"…`;
  const result = await chrome.runtime.sendMessage({ type: 'JOB_RADAR_SEND', job: res.job });
  out.textContent = `${summarize(result)}\n${res.job.title} — ${res.job.company}\njobId ${res.job.jobId}`;
});
