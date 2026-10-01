(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const jobsRoot = document.getElementById('jobs');
  const formMessage = document.getElementById('form-message');
  const cookieMessage = document.getElementById('cookie-message');
  const cookieStatus = document.getElementById('cookie-status');
  const cookieDomains = document.getElementById('cookie-domains');
  const pathMessage = document.getElementById('download-path-message');
  const pathInput = document.getElementById('download-path');
  const browserModal = document.getElementById('browser-modal');
  const browserFrame = document.getElementById('browser-frame');
  const browserMessage = document.getElementById('browser-message');
  const engineForm = document.getElementById('yt-dlp-form');
  const engineVersion = document.getElementById('yt-dlp-version');
  const engineSource = document.getElementById('yt-dlp-source');
  const engineChannel = document.getElementById('yt-dlp-channel');
  const engineMessage = document.getElementById('yt-dlp-message');
  const engineUpdate = document.getElementById('update-yt-dlp');
  const engineRollback = document.getElementById('rollback-yt-dlp');
  let engineRequestPending = false;
  let engineStatus = null;
  let activeAuthJob = null;
  let timer = null;
  const autoPromptedJobs = new Set();
  const statusNames = {QUEUED:'排隊中', RUNNING:'下載中', NEEDS_AUTH:'需要登入', SUCCEEDED:'完成', FAILED:'失敗', CANCELLED:'已取消'};

  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('X-CSRF-Token', csrf);
    const response = await fetch(url, { ...options, headers, credentials: 'same-origin' });
    let body = {};
    try { body = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
    return body;
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  }

  function renderEngine(data) {
    engineStatus = data;
    const busy = Boolean(data.busy || engineRequestPending);
    engineVersion.textContent = `${data.version} (${data.channel === 'image' ? '內建' : data.channel})`;
    engineSource.textContent = data.source === 'persistent' ? '已保存版本' : '內建版本';
    engineSource.classList.toggle('configured', data.source === 'persistent');
    engineUpdate.disabled = busy;
    engineUpdate.textContent = busy ? '處理中…' : '更新 yt-dlp';
    engineChannel.disabled = busy;
    engineRollback.disabled = busy || !data.rollback_available;
    engineRollback.title = data.rollback_version ? `回復至 ${data.rollback_version}` : '尚無可回復版本';
    engineMessage.textContent = [data.message, data.warning].filter(Boolean).join(' ');
    engineMessage.style.color = data.phase === 'FAILED' || data.warning ? 'var(--red)' : busy ? 'var(--amber)' : 'var(--green)';
  }

  async function refreshEngine() {
    if (!engineForm) return;
    try {
      renderEngine(await api('/api/settings/yt-dlp'));
    } catch (error) {
      engineMessage.textContent = error.message;
      engineMessage.style.color = 'var(--red)';
    }
  }

  async function changeEngine(action) {
    if (engineRequestPending || engineStatus?.busy) return;
    engineRequestPending = true;
    if (engineStatus) renderEngine(engineStatus);
    try {
      const options = {method: 'POST'};
      if (action === 'update') {
        options.headers = {'Content-Type': 'application/json'};
        options.body = JSON.stringify({channel: engineChannel.value});
      }
      const result = await api(`/api/settings/yt-dlp/${action}`, options);
      engineRequestPending = false;
      renderEngine(result);
    } catch (error) {
      engineRequestPending = false;
      if (engineStatus) renderEngine(engineStatus);
      engineMessage.textContent = error.message;
      engineMessage.style.color = 'var(--red)';
    }
  }

  engineForm?.addEventListener('submit', event => {
    event.preventDefault();
    changeEngine('update');
  });
  engineRollback?.addEventListener('click', () => changeEngine('rollback'));

  function renderJobs(jobs) {
    if (!jobs.length) {
      jobsRoot.innerHTML = '<div class="empty-state"><div class="empty-icon">↧</div><h3>還沒有下載工作</h3><p class="muted">新增一個影片網址後，進度會顯示在這裡。</p></div>';
      return;
    }
    jobsRoot.innerHTML = jobs.map(job => {
      const status = escapeHtml(job.status);
      const statusName = escapeHtml(statusNames[job.status] || job.status);
      const actions = [];
      if (job.status === 'SUCCEEDED') actions.push(`<a class="button primary small-button" href="/api/jobs/${encodeURIComponent(job.id)}/file">下載檔案</a>`);
      if (job.can_login && ['NEEDS_AUTH', 'FAILED'].includes(job.status)) actions.push(`<button class="secondary small-button login-action" data-job-id="${escapeHtml(job.id)}">登入並重試</button>`);
      if (job.cookies_configured && ['FAILED', 'NEEDS_AUTH'].includes(job.status)) actions.push(`<button class="quiet small-button cookie-retry-action" data-job-id="${escapeHtml(job.id)}">使用 cookies 重試</button>`);
      if (['QUEUED', 'RUNNING'].includes(job.status)) actions.push(`<button class="quiet small-button cancel-action" data-job-id="${escapeHtml(job.id)}">取消</button>`);
      const progress = job.status === 'RUNNING' ? `<div class="progress"><span style="width:${Math.max(0,Math.min(100,job.progress))}%"></span></div><div class="progress-label">${Number(job.progress).toFixed(1)}%</div>` : '';
      const error = job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : '';
      return `<article class="job-card" data-job-id="${escapeHtml(job.id)}"><div class="job-main"><div class="job-topline"><span class="status status-${status.toLowerCase()}">${statusName}</span><span class="job-date">${escapeHtml(job.created_at)}</span></div><div class="job-url">${escapeHtml(job.url)}</div>${progress}${error}</div><div class="job-actions">${actions.join('')}</div></article>`;
    }).join('');
  }

  async function refreshJobs() {
    if (!jobsRoot) return;
    try {
      const data = await api('/api/jobs', { method: 'GET', headers: { 'X-CSRF-Token': csrf } });
      renderJobs(data.jobs);
      const configured = Boolean(data.cookies_configured || data.jobs.some(job => job.cookies_configured));
      if (cookieStatus) {
        cookieStatus.classList.toggle('configured', configured);
        cookieStatus.textContent = configured ? '已設定' : '尚未設定';
        document.getElementById('remove-cookies').disabled = !configured;
        if (cookieDomains) cookieDomains.textContent = data.cookie_domains?.length ? `已保存網域：${data.cookie_domains.join('、')}` : '尚未保存有效 cookies。';
      }
      const pendingAuth = data.jobs.find(job => ['NEEDS_AUTH', 'FAILED'].includes(job.status) && job.can_login && !job.cookies_configured && !autoPromptedJobs.has(job.id));
      if (pendingAuth && browserModal?.hidden) {
        autoPromptedJobs.add(pendingAuth.id);
        await openAuthBrowser(pendingAuth.id);
      }
    } catch (error) {
      if (error.message.includes('登入')) window.location.assign('/login');
    }
  }

  document.getElementById('download-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    formMessage.textContent = '';
    const input = document.getElementById('video-url');
    try {
      await api('/api/jobs', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({url: input.value}) });
      input.value = '';
      await refreshJobs();
    } catch (error) { formMessage.textContent = error.message; }
  });

  document.getElementById('cookie-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    cookieMessage.textContent = '';
    const files = document.getElementById('cookie-file').files;
    if (!files.length) return;
    const data = new FormData();
    for (const file of files) data.append('file', file);
    try {
      const result = await api('/api/cookies', { method: 'POST', body: data });
      cookieStatus.textContent = '已設定'; cookieStatus.classList.add('configured');
      document.getElementById('remove-cookies').disabled = false;
      document.getElementById('cookie-file').value = '';
      cookieMessage.style.color = 'var(--green)'; cookieMessage.textContent = 'Cookies 已合併匯入，其他網站的登入狀態已保留。';
      if (cookieDomains) cookieDomains.textContent = `已保存網域：${result.cookie_domains.join('、')}`;
      await refreshJobs();
    } catch (error) { cookieMessage.style.color = 'var(--red)'; cookieMessage.textContent = error.message; }
  });

  document.getElementById('download-path-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    pathMessage.textContent = '';
    const button = event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      const result = await api('/api/settings/download-path', {
        method: 'PUT',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({path: pathInput.value})
      });
      pathInput.value = result.path;
      document.querySelector('.path-result code').textContent = result.full_path;
      pathMessage.style.color = 'var(--green)';
      pathMessage.textContent = '下載位置已更新；新工作會使用此位置。';
    } catch (error) {
      pathMessage.style.color = 'var(--red)';
      pathMessage.textContent = error.message;
    } finally { button.disabled = false; }
  });

  document.getElementById('remove-cookies')?.addEventListener('click', async () => {
    try {
      await api('/api/cookies', { method: 'DELETE' });
      cookieStatus.textContent = '尚未設定'; cookieStatus.classList.remove('configured');
      document.getElementById('remove-cookies').disabled = true;
      cookieMessage.style.color = 'var(--green)'; cookieMessage.textContent = '已移除 cookies。';
      await refreshJobs();
    } catch (error) { cookieMessage.style.color = 'var(--red)'; cookieMessage.textContent = error.message; }
  });

  document.getElementById('clear-queue')?.addEventListener('click', async event => {
    if (!window.confirm('清空所有尚未開始的工作？正在下載的工作和已下載檔案不會變更。')) return;
    await clearJobs('queue', event.currentTarget);
  });

  document.getElementById('clear-history')?.addEventListener('click', async event => {
    if (!window.confirm('清除已結束、失敗、需要登入及已取消工作的紀錄？已下載的影片檔案會保留。')) return;
    await clearJobs('history', event.currentTarget);
  });

  async function clearJobs(scope, button) {
    button.disabled = true;
    try {
      const result = await api('/api/jobs/clear', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({scope})
      });
      formMessage.style.color = 'var(--green)';
      formMessage.textContent = scope === 'queue' ? `已清除 ${result.cleared} 個待下載工作。` : `已清除 ${result.cleared} 筆工作紀錄；影片檔案已保留。`;
      await refreshJobs();
    } catch (error) {
      formMessage.style.color = 'var(--red)';
      formMessage.textContent = error.message;
    } finally { button.disabled = false; }
  }

  jobsRoot?.addEventListener('click', async event => {
    const loginButton = event.target.closest('.login-action');
    const retryCookie = event.target.closest('.cookie-retry-action');
    const cancelButton = event.target.closest('.cancel-action');
    try {
      if (loginButton) {
        activeAuthJob = loginButton.dataset.jobId;
        await openAuthBrowser(activeAuthJob);
      } else if (retryCookie) {
        await api(`/api/jobs/${encodeURIComponent(retryCookie.dataset.jobId)}/retry-cookies`, { method: 'POST' });
        await refreshJobs();
      } else if (cancelButton) {
        await api(`/api/jobs/${encodeURIComponent(cancelButton.dataset.jobId)}/cancel`, { method: 'POST' });
        await refreshJobs();
      }
    } catch (error) {
      if (loginButton) browserMessage.textContent = error.message;
      else formMessage.textContent = error.message;
    }
  });

  async function openAuthBrowser(jobId) {
    activeAuthJob = jobId;
    browserMessage.textContent = '正在啟動遠端瀏覽器…';
    browserModal.hidden = false;
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/auth-session`, { method: 'POST' });
      // noVNC prepends a slash to its `path` option, so keep this relative.
      const path = `api/browser/ws?ticket=${encodeURIComponent(result.ticket)}`;
      const params = new URLSearchParams({autoconnect:'true', resize:'remote', path});
      browserFrame.src = `/novnc/vnc.html?${params.toString()}`;
      browserMessage.textContent = '請在上方瀏覽器完成此網站的登入與驗證，再按「使用登入狀態重試」。';
    } catch (error) {
      browserMessage.textContent = error.message;
    }
  }

  document.getElementById('retry-with-auth')?.addEventListener('click', async () => {
    if (!activeAuthJob) return;
    browserMessage.textContent = '正在儲存登入工作階段並重新排入下載…';
    try {
      await api(`/api/jobs/${encodeURIComponent(activeAuthJob)}/retry-auth`, { method: 'POST' });
      closeModal();
      await refreshJobs();
    } catch (error) { browserMessage.textContent = error.message; }
  });

  function closeModal() {
    browserModal.hidden = true;
    browserFrame.src = 'about:blank';
    browserMessage.textContent = '';
    activeAuthJob = null;
  }
  browserModal?.querySelectorAll('[data-close-modal]').forEach(element => element.addEventListener('click', closeModal));

  if (jobsRoot) {
    refreshJobs();
    refreshEngine();
    timer = window.setInterval(async () => { await Promise.all([refreshJobs(), refreshEngine()]); }, 3000);
    window.addEventListener('pagehide', () => window.clearInterval(timer), {once:true});
  }
})();
