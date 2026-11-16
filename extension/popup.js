const $ = (id) => document.getElementById(id);
const out = $('out');
const msg = $('msg');
let listed = []; // jobs currently shown (summaries, no descriptions)
let source = 'linkedin-extension'; // which tab is open: 'linkedin-extension' | 'remoteok'
const TABS = { 'linkedin-extension': 'tab-linkedin', remoteok: 'tab-remoteok' };

const api = (body) => chrome.runtime.sendMessage({ type: 'JOB_RADAR_API', body });

function note(text) {
  msg.textContent = text;
}

// ---- record mode + stats ----
function renderStats(stats = { stored: 0, duplicates: 0, errors: 0 }) {
  $('stats').textContent = `Saved ${stats.stored} · already had ${stats.duplicates} · errors ${stats.errors}`;
}
chrome.storage.local.get(['recordMode', 'stats']).then(({ recordMode, stats }) => {
  $('record').checked = !!recordMode;
  renderStats(stats);
});
$('record').addEventListener('change', () => chrome.storage.local.set({ recordMode: $('record').checked }));
chrome.storage.onChanged.addListener((changes) => changes.stats && renderStats(changes.stats.newValue));

// ---- capture the current job ----
function summarize({ status, body, error }) {
  if (status === 0) return `❌ Network error: ${error}`;
  if (status === 401) return '❌ Server rejected the key (check config.js)';
  if (status !== 200) return `❌ Server error (${status})`;
  if (body.stored) return `✅ Saved · score ${body.scores?.[0] ?? '?'}`;
  if (body.duplicates) return '↺ Already captured';
  return '⚠️ Server rejected the posting (failed validation)';
}

$('capture').addEventListener('click', async () => {
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
  const result = await chrome.runtime.sendMessage({ type: 'JOB_RADAR_SEND', job: res.job });
  out.textContent = `${summarize(result)}\n${res.job.title} — ${res.job.company}`;
  loadList();
});

// ---- list of captured jobs ----
const checkedIds = () => [...document.querySelectorAll('#jobs input[type=checkbox]:checked')].map((c) => c.value);

function render(jobs) {
  listed = jobs;
  const box = $('jobs');
  box.replaceChildren(); // textContent/DOM APIs only: job titles come from the web and must never be parsed as HTML
  $('all').checked = false;
  $('count').textContent = `${jobs.length} job${jobs.length === 1 ? '' : 's'}`;
  if (!jobs.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent =
      source === 'remoteok'
        ? 'No RemoteOK match yet. The daily fetch runs at 04:00 UTC.'
        : $('fits').checked ? 'No captured job fits your profile yet.' : 'Nothing captured yet.';
    box.appendChild(empty);
    return;
  }
  for (const job of jobs) {
    const row = document.createElement('label');
    row.className = 'job';

    const check = document.createElement('input');
    check.type = 'checkbox';
    check.value = job.jobId;

    const score = document.createElement('span');
    score.className = 'score';
    score.textContent = job.score;

    const text = document.createElement('div');
    const title = document.createElement('a');
    title.href = job.url;
    title.target = '_blank';
    title.rel = 'noreferrer';
    title.textContent = `${job.title} — ${job.company}`;
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = [job.location, (job.matched || []).slice(0, 5).join(', ')].filter(Boolean).join(' · ');
    text.append(title, meta);
    if (job.flags?.length) {
      const flags = document.createElement('div');
      flags.className = 'meta flag';
      flags.textContent = `⚠ ${job.flags.join(', ')}`;
      text.appendChild(flags);
    }

    row.append(check, score, text);
    box.appendChild(row);
  }
}

async function loadList() {
  note('Loading…');
  const res = await api({ action: 'list', source, only_fits: $('fits').checked });
  if (res.status !== 200) {
    note(res.status === 401 ? '❌ Server rejected the key (check config.js)' : `❌ Could not load (${res.status || res.error})`);
    return;
  }
  render(res.body.jobs);
  note('');
}

$('fits').addEventListener('change', loadList);
$('all').addEventListener('change', () => {
  document.querySelectorAll('#jobs input[type=checkbox]').forEach((c) => (c.checked = $('all').checked));
});

// ---- copy for Claude ----
function toPrompt(jobs) {
  const head = `Write a tailored cover letter for each of the ${jobs.length} job(s) below (job-radar captures, best fit first).\n\n`;
  const blocks = jobs.map((j, i) =>
    [
      `## ${i + 1}. ${j.title} — ${j.company}`,
      `Location: ${j.location || 'n/a'} | Fit score: ${j.score}/100 | Matched: ${(j.matched || []).join(', ') || '-'}`,
      j.flags?.length ? `Flags: ${j.flags.join(', ')}` : null,
      `URL: ${j.url}`,
      '',
      (j.description || '').trim(),
    ]
      .filter((line) => line !== null)
      .join('\n')
  );
  return head + blocks.join('\n\n---\n\n');
}

$('copy').addEventListener('click', async () => {
  const ids = checkedIds().length ? checkedIds() : listed.map((j) => j.jobId);
  if (!ids.length) return note('Nothing to copy.');
  note(`Fetching ${ids.length} description(s)…`);
  const jobs = [];
  for (let i = 0; i < ids.length; i += 100) {
    const res = await api({ action: 'export', source, job_ids: ids.slice(i, i + 100) });
    if (res.status !== 200) return note(`❌ Export failed (${res.status || res.error})`);
    jobs.push(...res.body.jobs);
  }
  const text = toPrompt(jobs);
  await navigator.clipboard.writeText(text);
  note(`✅ Copied ${jobs.length} job(s), ${text.length.toLocaleString()} characters. Paste into Claude.`);
});

// ---- delete after applying ----
$('del').addEventListener('click', async () => {
  const ids = checkedIds();
  if (!ids.length) return note('Check the jobs you applied to first.');
  const warning =
    source === 'remoteok'
      ? `Remove ${ids.length} RemoteOK job(s)? Their descriptions are erased; they won't come back in the daily fetch.`
      : `Delete ${ids.length} job(s) from storage? Use this after you've applied. It can't be undone.`;
  if (!confirm(warning)) return;
  const res = await chrome.runtime.sendMessage({ type: 'JOB_RADAR_DELETE', ids, source });
  if (res.status !== 200) return note(`❌ Delete failed (${res.status || res.error})`);
  await loadList();
  note(`🗑 Deleted ${res.body.deleted} job(s).`);
});

// ---- tabs ----
function selectTab(next) {
  source = next;
  for (const [src, id] of Object.entries(TABS)) $(id).classList.toggle('active', src === source);
  $('heading').textContent = source === 'remoteok' ? 'RemoteOK matches' : 'Captured jobs';
  chrome.storage.local.set({ tab: source });
  return loadList();
}
$('tab-linkedin').addEventListener('click', () => selectTab('linkedin-extension'));
$('tab-remoteok').addEventListener('click', () => selectTab('remoteok'));

chrome.storage.local.get('tab').then(({ tab }) => selectTab(tab in TABS ? tab : 'linkedin-extension'));
