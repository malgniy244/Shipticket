// ── Bulk Mode JavaScript (Decision 19) ────────────────────────────────────────
// This block is appended to index.html at build time (see apply_bulk_patch.py).
// It adds: batch state, screen helpers, batch creation, dashboard rendering,
// sub-job upload form, abandon, unconfirm, and batch download.

// ── Bulk Mode state ──
let activeBatchId = null;       // batch currently being reviewed in sub-job flow
let activeSubJobId = null;      // sub-job currently in Phase A/B review
let batchPollTimer = null;      // setInterval handle for dashboard refresh
let nbBatchType = null;         // new-batch form: selected batch type

// ── Screen helpers ──
function showBulkScreen() {
  hide('new-job-screen'); hide('new-batch-screen');
  hide('progress-screen'); hide('phase-a-screen');
  hidePhaseBScreen(); hideReconcileScreen();
  show('bulk-screen');
  el('main-area').style.display = '';
  loadBatches();
  if (batchPollTimer) clearInterval(batchPollTimer);
  batchPollTimer = setInterval(loadBatches, 4000);
}

function hideBulkScreens() {
  hide('bulk-screen'); hide('new-batch-screen');
  if (batchPollTimer) { clearInterval(batchPollTimer); batchPollTimer = null; }
}

function showNewBatch() {
  hideBulkScreens();
  hide('new-job-screen'); hide('progress-screen'); hide('phase-a-screen');
  hidePhaseBScreen(); hideReconcileScreen();
  show('new-batch-screen');
  el('main-area').style.display = '';
  // Reset form
  nbBatchType = null;
  el('nb-bt-tib').classList.remove('selected');
  el('nb-bt-nontib').classList.remove('selected');
  el('nb-wl-input').value = '';
  el('nb-fast-mode-cb').checked = false;
  el('nb-error').classList.add('hidden');
  el('nb-submit-btn').disabled = true;
}

// ── Topbar buttons ──
el('bulk-mode-btn').addEventListener('click', () => {
  hideBulkScreens();
  showBulkScreen();
});

el('bulk-new-batch-btn').addEventListener('click', showNewBatch);
el('nb-cancel-btn').addEventListener('click', showBulkScreen);

// ── New Batch form ──
['nb-bt-tib','nb-bt-nontib'].forEach(id => {
  el(id).addEventListener('click', () => {
    nbBatchType = el(id).dataset.value;
    el('nb-bt-tib').classList.toggle('selected', nbBatchType === 'tib');
    el('nb-bt-nontib').classList.toggle('selected', nbBatchType === 'non_tib');
    el('nb-fast-mode-cb').checked = (nbBatchType === 'non_tib');
    updateNbSubmitBtn();
  });
});
el('nb-wl-input').addEventListener('input', updateNbSubmitBtn);
function updateNbSubmitBtn() {
  el('nb-submit-btn').disabled = !(nbBatchType && el('nb-wl-input').value.trim());
}

el('nb-submit-btn').addEventListener('click', async () => {
  const wlRaw = el('nb-wl-input').value.trim();
  if (!nbBatchType || !wlRaw) return;
  el('nb-error').classList.add('hidden');
  el('nb-submit-btn').disabled = true;
  try {
    const fd = new FormData();
    fd.append('whitelist_raw', wlRaw);
    fd.append('batch_type', nbBatchType);
    fd.append('fast_mode', el('nb-fast-mode-cb').checked ? 'on' : 'off');
    const r = await fetch('/api/batches', {method:'POST', body: fd});
    if (!r.ok) { const t = await r.text(); throw new Error(t.startsWith('<') ? 'Server error' : t); }
    const d = await r.json();
    activeBatchId = d.batch_id;
    showBulkScreen();
  } catch(e) {
    el('nb-error').textContent = 'Error: ' + e.message;
    el('nb-error').classList.remove('hidden');
    el('nb-submit-btn').disabled = false;
  }
});

// ── Batch dashboard ──
async function loadBatches() {
  try {
    const batches = await api('GET', '/api/batches');
    renderBatchesList(batches);
  } catch(e) {
    el('batches-container').innerHTML = `<div class="error-msg">Failed to load batches: ${e.message}</div>`;
  }
}

function renderBatchesList(batches) {
  const container = el('batches-container');
  if (!batches || batches.length === 0) {
    container.innerHTML = `<div style="color:var(--soft);font-size:.95rem;padding:32px 0;text-align:center;">No batches yet. Click <strong>+ New Batch</strong> to create one.</div>`;
    return;
  }
  // Sort: open first, then by created_at desc
  batches.sort((a,b) => {
    if (a.status === 'open' && b.status !== 'open') return -1;
    if (b.status === 'open' && a.status !== 'open') return 1;
    return b.created_at - a.created_at;
  });
  container.innerHTML = batches.map(b => renderBatchCard(b)).join('');
  // Attach event listeners
  batches.forEach(b => attachBatchCardListeners(b));
}

function ledgerPillClass(ledger) {
  if (ledger.reconciled) return 'ok';
  if (ledger.missing.length > 0) return 'warn';
  return 'err';
}

function renderBatchCard(b) {
  const ledger = b.ledger;
  const statusLabel = b.status === 'complete' ? '&#10003; Complete' : 'Open';
  const statusColor = b.status === 'complete' ? 'color:var(--success);font-weight:700;' : 'color:var(--warn);font-weight:700;';
  const subJobRows = (b.sub_jobs || []).map(sj => {
    const statusCls = sj.status || 'queued';
    const reviewBtn = ['ready','confirmed'].includes(sj.status)
      ? `<button class="btn-sm primary sj-review-btn" data-batch="${b.batch_id}" data-sj="${sj.id}">Review</button>` : '';
    const abandonBtn = !['confirmed','abandoned'].includes(sj.status)
      ? `<button class="btn-sm danger sj-abandon-btn" data-batch="${b.batch_id}" data-sj="${sj.id}">Abandon</button>` : '';
    const unconfirmBtn = sj.status === 'confirmed'
      ? `<button class="btn-sm danger sj-unconfirm-btn" data-batch="${b.batch_id}" data-sj="${sj.id}">Un-confirm</button>` : '';
    return `<tr>
      <td style="font-family:monospace;font-size:.75rem;">${sj.id.slice(0,8)}&hellip;</td>
      <td>${sj.filename || '—'}</td>
      <td><span class="sj-status ${statusCls}">${statusCls}</span></td>
      <td>${sj.expected_count}</td>
      <td>${sj.total_pages || '—'}</td>
      <td style="display:flex;gap:6px;flex-wrap:wrap;">${reviewBtn}${abandonBtn}${unconfirmBtn}</td>
    </tr>`;
  }).join('');

  const subJobTable = b.sub_jobs && b.sub_jobs.length > 0 ? `
    <table class="sub-job-table">
      <thead><tr>
        <th>Sub-job</th><th>File</th><th>Status</th><th>Expected</th><th>Pages</th><th>Actions</th>
      </tr></thead>
      <tbody>${subJobRows}</tbody>
    </table>` : `<div style="color:var(--soft);font-size:.85rem;padding:8px 0;">No files uploaded yet.</div>`;

  const missingText = ledger.missing.length > 0
    ? `<span class="ledger-pill err">${ledger.missing.length} missing</span>` : '';
  const reconcileText = ledger.reconciled
    ? `<span class="ledger-pill ok">&#10003; Reconciled</span>` : '';

  return `<div class="batch-card" id="batch-card-${b.batch_id}">
    <div class="batch-header">
      <div>
        <div class="batch-title">Batch <span class="batch-id-label">${b.batch_id.slice(0,8)}&hellip;</span>
          <span style="${statusColor}font-size:.82rem;margin-left:8px;">${statusLabel}</span>
        </div>
        <div style="font-size:.78rem;color:var(--soft);margin-top:2px;">
          ${b.batch_type.toUpperCase()} &middot; ${b.whitelist_count} tickets &middot; ${b.sub_job_count} file${b.sub_job_count!==1?'s':''}
        </div>
      </div>
    </div>
    <div class="batch-ledger">
      <span class="ledger-pill info">${ledger.total_claimed}/${ledger.total_expected} claimed</span>
      <span class="ledger-pill info">${ledger.confirmed_sub_jobs} confirmed</span>
      ${missingText}${reconcileText}
    </div>
    ${subJobTable}
    <div class="batch-actions">
      <button class="btn-sm primary add-file-btn" data-batch="${b.batch_id}">+ Add File</button>
      ${ledger.reconciled ? `<button class="btn-sm success download-batch-btn" data-batch="${b.batch_id}">&#8681; Download Batch ZIP</button>` : ''}
    </div>
    <div class="add-sub-job-form hidden" id="add-sj-form-${b.batch_id}">
      <h4>Upload a file into this batch</h4>
      <div class="field">
        <label>PDF file <span class="req">*</span></label>
        <input type="file" accept=".pdf" id="sj-pdf-${b.batch_id}">
      </div>
      <div class="field">
        <label>Expected ticket count in this file <span class="req">*</span></label>
        <input type="number" min="1" id="sj-expected-${b.batch_id}" placeholder="e.g. 30">
      </div>
      <div class="error-msg hidden" id="sj-error-${b.batch_id}"></div>
      <div style="display:flex;gap:8px;margin-top:8px;">
        <button class="btn-primary sj-upload-btn" data-batch="${b.batch_id}" style="font-size:.88rem;padding:7px 16px;" disabled>Start Detection</button>
        <button class="btn-outline sj-cancel-btn" data-batch="${b.batch_id}" style="font-size:.82rem;">Cancel</button>
      </div>
    </div>
  </div>`;
}

function attachBatchCardListeners(b) {
  const batchId = b.batch_id;

  // Add File toggle
  const addBtn = document.querySelector(`.add-file-btn[data-batch="${batchId}"]`);
  if (addBtn) addBtn.addEventListener('click', () => {
    const form = el(`add-sj-form-${batchId}`);
    form.classList.toggle('hidden');
  });

  // Sub-job upload form validation
  const pdfInput = el(`sj-pdf-${batchId}`);
  const expInput = el(`sj-expected-${batchId}`);
  const uploadBtn = document.querySelector(`.sj-upload-btn[data-batch="${batchId}"]`);
  if (pdfInput && expInput && uploadBtn) {
    const validate = () => {
      uploadBtn.disabled = !(pdfInput.files[0] && expInput.value && parseInt(expInput.value) >= 1);
    };
    pdfInput.addEventListener('change', validate);
    expInput.addEventListener('input', validate);

    uploadBtn.addEventListener('click', async () => {
      const pdf = pdfInput.files[0];
      const expected = parseInt(expInput.value);
      if (!pdf || !expected) return;
      const errEl = el(`sj-error-${batchId}`);
      errEl.classList.add('hidden');
      uploadBtn.disabled = true;
      uploadBtn.innerHTML = '<span class="spinner"></span>Uploading…';
      try {
        const fd = new FormData();
        fd.append('file', pdf);
        fd.append('expected_count', String(expected));
        const r = await fetch(`/api/batches/${batchId}/sub-jobs`, {method:'POST', body: fd});
        if (!r.ok) { const t = await r.text(); throw new Error(t.startsWith('<') ? 'Server error' : t); }
        const d = await r.json();
        // Enter single-job review flow for this sub-job
        activeBatchId = batchId;
        activeSubJobId = d.sub_job_id;
        jobId = d.sub_job_id;
        totalPages = d.total_pages;
        whitelist = d.whitelist;
        batchType = d.batch_type;
        activeJobFastMode = !!d.fast_mode;
        hideBulkScreens();
        hide('new-job-screen');
        const modeLabel = activeJobFastMode
          ? '\u26a1 FAST MODE — reading first pages of pink-bounded blocks only'
          : '\u25a0 FULL MODE — reading every page';
        el('progress-title').textContent = modeLabel;
        show('progress-screen');
        el('main-area').style.display = '';
        pollStatus();
      } catch(e) {
        const errEl2 = el(`sj-error-${batchId}`);
        errEl2.textContent = 'Error: ' + e.message;
        errEl2.classList.remove('hidden');
        uploadBtn.disabled = false;
        uploadBtn.textContent = 'Start Detection';
      }
    });
  }

  // Cancel upload form
  const cancelBtn = document.querySelector(`.sj-cancel-btn[data-batch="${batchId}"]`);
  if (cancelBtn) cancelBtn.addEventListener('click', () => {
    el(`add-sj-form-${batchId}`).classList.add('hidden');
  });

  // Review sub-job
  document.querySelectorAll(`.sj-review-btn[data-batch="${batchId}"]`).forEach(btn => {
    btn.addEventListener('click', async () => {
      const sjId = btn.dataset.sj;
      activeBatchId = batchId;
      activeSubJobId = sjId;
      jobId = sjId;
      // Fetch batch to get whitelist
      try {
        const batchData = await api('GET', `/api/batches/${batchId}`);
        whitelist = batchData.whitelist;
        batchType = batchData.batch_type;
        activeJobFastMode = !!batchData.fast_mode;
        reviewState = await api('GET', `/api/jobs/${sjId}/review`);
        hideBulkScreens();
        hide('new-job-screen');
        el('main-area').style.display = '';
        buildPhaseA();
      } catch(e) {
        alert('Failed to load sub-job: ' + e.message);
      }
    });
  });

  // Abandon sub-job
  document.querySelectorAll(`.sj-abandon-btn[data-batch="${batchId}"]`).forEach(btn => {
    btn.addEventListener('click', async () => {
      const sjId = btn.dataset.sj;
      if (!confirm(`Abandon sub-job ${sjId.slice(0,8)}…? This cannot be undone without re-uploading.`)) return;
      try {
        await api('POST', `/api/batches/${batchId}/sub-jobs/${sjId}/abandon`);
        loadBatches();
      } catch(e) { alert('Abandon failed: ' + e.message); }
    });
  });

  // Un-confirm sub-job
  document.querySelectorAll(`.sj-unconfirm-btn[data-batch="${batchId}"]`).forEach(btn => {
    btn.addEventListener('click', async () => {
      const sjId = btn.dataset.sj;
      if (!confirm(`Un-confirm sub-job ${sjId.slice(0,8)}…? This will release its tickets back to the batch pool.`)) return;
      try {
        await api('POST', `/api/batches/${batchId}/sub-jobs/${sjId}/unconfirm`);
        loadBatches();
      } catch(e) { alert('Un-confirm failed: ' + e.message); }
    });
  });

  // Download batch ZIP
  const dlBtn = document.querySelector(`.download-batch-btn[data-batch="${batchId}"]`);
  if (dlBtn) dlBtn.addEventListener('click', async () => {
    dlBtn.disabled = true;
    dlBtn.innerHTML = '<span class="spinner"></span>Building…';
    try {
      const r = await fetch(`/api/batches/${batchId}/download`);
      if (!r.ok) { const t = await r.text(); throw new Error(t.startsWith('<') ? 'Server error' : t); }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = url;
      const cd = r.headers.get('Content-Disposition') || '';
      const m = cd.match(/filename="([^"]+)"/);
      a.download = m ? m[1] : `batch_${batchId.slice(0,8)}.zip`;
      a.click();
      dlBtn.textContent = '\u2713 Downloaded';
    } catch(e) {
      alert('Download failed: ' + e.message);
      dlBtn.disabled = false;
      dlBtn.textContent = '\u2B07 Download Batch ZIP';
    }
  });
}

// ── Sub-job confirm: override the final-confirm-btn for sub-job flow ──
// When activeSubJobId is set, confirm goes to the batch sub-job endpoint.
// Otherwise it uses the existing single-job endpoint (unchanged).
const _origFinalConfirmHandler = el('final-confirm-btn').onclick;
el('final-confirm-btn').addEventListener('click', async function(e) {
  if (!activeSubJobId) return; // handled by original listener
  e.stopImmediatePropagation();
  el('final-confirm-btn').disabled = true;
  el('final-confirm-btn').innerHTML = '<span class="spinner"></span>Building ZIP…';
  try {
    const fd = new FormData();
    const r = await fetch(`/api/batches/${activeBatchId}/sub-jobs/${activeSubJobId}/confirm`, {method:'POST', body: fd});
    if (!r.ok) {
      const t = await r.text();
      throw new Error(t.startsWith('<') ? 'Server error (unexpected HTML response)' : t);
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url;
    const cd = r.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="([^"]+)"/);
    a.download = m ? m[1] : 'tickets.zip';
    a.click();
    el('final-confirm-btn').textContent = '\u2713 Downloaded';
    el('reconcile-status-text').textContent = 'ZIP downloaded. Return to batch dashboard to continue.';
    // Add "Back to Batch" button
    const backBtn = document.createElement('button');
    backBtn.className = 'btn-outline';
    backBtn.style.cssText = 'margin-left:12px;';
    backBtn.textContent = '\u2190 Back to Batch';
    backBtn.addEventListener('click', () => {
      activeSubJobId = null;
      jobId = null;
      reviewState = null;
      hideReconcileScreen();
      showBulkScreen();
    });
    el('final-confirm-btn').parentNode.appendChild(backBtn);
  } catch(e) {
    alert('Download failed: ' + e.message);
    el('final-confirm-btn').disabled = false;
    el('final-confirm-btn').textContent = 'Confirm & Download ZIP';
  }
}, true); // capture phase so it fires before the original listener

// ── Back-to-batch from Phase A (Esc / back button) ──
// When in sub-job flow, Phase A's "back" should return to batch dashboard.
// The existing Phase A has no back button — Esc from Phase B goes to Phase A.
// We add a "Back to Batch" link in Phase A header when in sub-job mode.
const _origBuildPhaseA = buildPhaseA;
// Patch: after buildPhaseA runs, inject a back-to-batch button if in sub-job mode.
// We do this by wrapping the existing function.
(function() {
  const orig = buildPhaseA;
  window.buildPhaseA = function() {
    orig();
    if (activeSubJobId) {
      const header = document.querySelector('.phase-header');
      if (header && !header.querySelector('.back-to-batch-btn')) {
        const btn = document.createElement('button');
        btn.className = 'btn-outline back-to-batch-btn';
        btn.style.cssText = 'font-size:.82rem;padding:5px 12px;';
        btn.textContent = '\u2190 Back to Batch';
        btn.addEventListener('click', () => {
          activeSubJobId = null;
          jobId = null;
          reviewState = null;
          hide('phase-a-screen');
          showBulkScreen();
        });
        header.insertBefore(btn, header.firstChild);
      }
    }
  };
})();
