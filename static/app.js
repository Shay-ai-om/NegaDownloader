(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const jobsRoot = document.getElementById('jobs');
  const formMessage = document.getElementById('form-message');
  const cookieMessage = document.getElementById('cookie-message');
  const cookieStatus = document.getElementById('cookie-status');
  const pathMessage = document.getElementById('download-path-message');
  const pathInput = document.getElementById('download-path');
  const browserModal = document.getElementById('browser-modal');
  const browserFrame = document.getElementById('browser-frame');
  const browserMessage = document.getElementById('browser-message');
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
      }
      const pendingAuth = data.jobs.find(job => job.status === 'NEEDS_AUTH' && job.can_login && !job.cookies_configured && !autoPromptedJobs.has(job.id));
      if (pendingAuth && browserModal?.hidden) {
        autoPromptedJobs.add(pendingAuth.id);
        openAuthBrowser(pendingAuth.id);
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
    const file = document.getElementById('cookie-file').files[0];
    if (!file) return;
    const data = new FormData(); data.append('file', file);
    try {
      await api('/api/cookies', { method: 'POST', body: data });
      cookieStatus.textContent = '已設定'; cookieStatus.classList.add('configured');
      document.getElementById('remove-cookies').disabled = false;
      document.getElementById('cookie-file').value = '';
      cookieMessage.style.color = 'var(--green)'; cookieMessage.textContent = 'cookies.txt 已匯入。';
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
      browserMessage.textContent = '請在上方瀏覽器登入 Instagram；完成後再按下方按鈕。';
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
    timer = window.setInterval(refreshJobs, 3000);
    window.addEventListener('pagehide', () => window.clearInterval(timer), {once:true});
  }
})();
