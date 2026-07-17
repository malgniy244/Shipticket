// bulk_patch.js — Bulk Mode frontend controller (Decision 19)
// Batch dashboard, multi-file drag-drop upload, Phase A/B intercept, confirm intercept.

// ── Bulk Mode state ──
let activeBatchId = null;       // batch currently being reviewed in sub-job flow
let activeSubJobId = null;      // sub-job currently in Phase A/B review
let activeSubJobExpectedCount = null; // this file's expected ticket count (not the batch total)
let batchPollTimer = null;      // setInterval handle for dashboard refresh
let nbBatchType = null;         // new-batch form: selected batch type
let _showArchived = false;      // toggle: show archived batches in dashboard

// ── Screen helpers ──
function showBulkScreen() {
  hide('new-job-screen'); hide('new-batch-screen');
  hide('progress-screen'); hide('phase-a-screen');
  hidePhaseBScreen(); hideReconcileScreen();
  show('bulk-screen');
  el('main-area').style.display = '';
  loadBatches();
  if (batchPollTimer) clearInterval(batchPollTimer);
  batchPollTimer = setInterval(loadBatches, 5000);
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
// Incremental rendering: never replace a card that already exists in the DOM.
// Only insert new cards and update dynamic fields (ledger pills, sub-job table)
// in existing cards. This means the upload form is never destroyed by the poll.

async function loadBatches() {
  try {
    const qs = _showArchived ? '?show_archived=1' : '';
    const batches = await api('GET', `/api/batches${qs}`);
    renderBatchesList(batches);
  } catch(e) {
    const container = el('batches-container');
    if (!container.querySelector('.batch-card')) {
      container.innerHTML = `<div class="error-msg">Failed to load batches: ${e.message}</div>`;
    }
  }
}

function ledgerPillClass(ledger) {
  if (ledger.reconciled) return 'ok';
  if (ledger.missing.length > 0) return 'warn';
  return 'err';
}

function renderBatchesList(batches) {
  const container = el('batches-container');

  // Update the archived-toggle button label
  const toggleBtn = el('toggle-archived-btn');
  if (toggleBtn) toggleBtn.textContent = _showArchived ? 'Hide Archived' : 'Show Archived';

  if (!batches || batches.length === 0) {
    if (!container.querySelector('.batch-card')) {
      container.innerHTML = `<div style="color:var(--soft);font-size:.95rem;padding:32px 0;text-align:center;">No batches yet. Click <strong>+ New Batch</strong> to create one.</div>`;
    }
    return;
  }

  // Remove the "no batches" placeholder if present
  const placeholder = container.querySelector('div:not(.batch-card)');
  if (placeholder && !placeholder.classList.contains('batch-card')) placeholder.remove();

  // Sort: open first, then by created_at desc
  batches.sort((a,b) => {
    if (a.status === 'open' && b.status !== 'open') return -1;
    if (b.status === 'open' && a.status !== 'open') return 1;
    return b.created_at - a.created_at;
  });

  // Remove cards that are no longer in the response (e.g. deleted)
  const returnedIds = new Set(batches.map(b => b.batch_id));
  container.querySelectorAll('.batch-card').forEach(card => {
    const id = card.id.replace('batch-card-', '');
    if (!returnedIds.has(id)) card.remove();
  });

  batches.forEach(b => {
    const existing = document.getElementById(`batch-card-${b.batch_id}`);
    if (existing) {
      _updateBatchCardDynamic(existing, b);
    } else {
      const div = document.createElement('div');
      div.innerHTML = renderBatchCard(b);
      const card = div.firstElementChild;
      container.prepend(card);
      // Immediately hydrate the sub-job table so it doesn't show the placeholder
      _updateBatchCardDynamic(card, b);
      attachBatchCardListeners(b);
    }
  });
}

function _buildActionsHTML(b) {
  const ledger = b.ledger;
  const hasConfirmed = (b.sub_jobs || []).some(sj => sj.status === 'confirmed');
  // Allow delete if: no confirmed sub-jobs (empty/abandoned only), OR batch is archived (ZIPs already saved)
  const canDelete = !hasConfirmed || b.archived;
  const isArchived = b.archived;
  return `
    <button class="btn-sm primary add-file-btn" data-batch="${b.batch_id}">+ Add File</button>
    ${ledger.reconciled ? `<button class="btn-sm success download-batch-btn" data-batch="${b.batch_id}">&#8681; Download Batch ZIP</button>` : ''}
    ${isArchived
      ? `<button class="btn-sm secondary unarchive-batch-btn" data-batch="${b.batch_id}" style="font-size:.75rem;">Unarchive</button>`
      : `<button class="btn-sm secondary archive-batch-btn" data-batch="${b.batch_id}" style="font-size:.75rem;">Archive</button>`}
    ${canDelete ? `<button class="btn-sm danger delete-batch-btn" data-batch="${b.batch_id}" style="font-size:.75rem;">Delete</button>` : ''}
  `;
}

function _attachActionsListeners(cardEl, b) {
  const batchId = b.batch_id;
  const addBtn = cardEl.querySelector(`.add-file-btn[data-batch="${batchId}"]`);
  if (addBtn) addBtn.addEventListener('click', () => _openAddFileForm(batchId));
  const dlBtn = cardEl.querySelector(`.download-batch-btn[data-batch="${batchId}"]`);
  if (dlBtn) dlBtn.addEventListener('click', () => _downloadBatch(batchId, dlBtn));
  const archBtn = cardEl.querySelector(`.archive-batch-btn[data-batch="${batchId}"]`);
  if (archBtn) archBtn.addEventListener('click', () => _archiveBatch(batchId));
  const unarchBtn = cardEl.querySelector(`.unarchive-batch-btn[data-batch="${batchId}"]`);
  if (unarchBtn) unarchBtn.addEventListener('click', () => _unarchiveBatch(batchId));
  const delBtn = cardEl.querySelector(`.delete-batch-btn[data-batch="${batchId}"]`);
  if (delBtn) delBtn.addEventListener('click', () => _deleteBatch(batchId));
}

function _updateBatchCardDynamic(cardEl, b) {
  // Update status label
  const titleEl = cardEl.querySelector('.batch-title span[style]');
  if (titleEl) {
    const archivedTag = b.archived ? ' <span style="font-size:.7rem;color:var(--soft);font-weight:400;">[archived]</span>' : '';
    titleEl.innerHTML = (b.status === 'complete' ? '✓ Complete' : 'Open') + archivedTag;
    titleEl.style.color = b.status === 'complete' ? 'var(--success)' : 'var(--warn)';
  }

  // Update subtitle (file count)
  const subtitleEl = cardEl.querySelector('.batch-header div > div:last-child');
  if (subtitleEl) {
    subtitleEl.textContent = `${b.batch_type.toUpperCase()} · ${b.whitelist_count} tickets · ${b.sub_job_count} file${b.sub_job_count!==1?'s':''}`;
  }

  // Update ledger pills
  const ledger = b.ledger;
  const ledgerEl = cardEl.querySelector('.batch-ledger');
  if (ledgerEl) {
    const missingText = ledger.missing.length > 0
      ? `<span class="ledger-pill err">${ledger.missing.length} missing</span>` : '';
    const reconcileText = ledger.reconciled
      ? `<span class="ledger-pill ok">&#10003; Reconciled</span>` : '';
    ledgerEl.innerHTML = `
      <span class="ledger-pill info">${ledger.total_claimed}/${ledger.total_expected} claimed</span>
      <span class="ledger-pill info">${ledger.confirmed_sub_jobs} confirmed</span>
      ${missingText}${reconcileText}`;
  }

  // Update actions area and sub-job table (only if upload form is NOT open)
  const formEl = cardEl.querySelector('.add-sub-job-form');
  const formOpen = formEl && !formEl.classList.contains('hidden');
  if (!formOpen) {
    const actionsEl = cardEl.querySelector('.batch-actions');
    if (actionsEl) {
      actionsEl.innerHTML = _buildActionsHTML(b);
      _attachActionsListeners(cardEl, b);
    }

    // Update sub-job table
    const subJobRows = (b.sub_jobs || []).map(sj => {
      const statusCls = sj.status || 'queued';
      const reviewBtn = ['ready','confirmed'].includes(sj.status)
        ? `<button class="btn-sm primary sj-review-btn" data-batch="${b.batch_id}" data-sj="${sj.id}">Review</button>` : '';
      const abandonBtn = !['confirmed','abandoned'].includes(sj.status)
        ? `<button class="btn-sm danger sj-abandon-btn" data-batch="${b.batch_id}" data-sj="${sj.id}">Abandon</button>` : '';
      const unconfirmBtn = sj.status === 'confirmed'
        ? `<button class="btn-sm danger sj-unconfirm-btn" data-batch="${b.batch_id}" data-sj="${sj.id}">Un-confirm</button>` : '';
      const removeBtn = sj.status === 'abandoned'
        ? `<button class="btn-sm danger sj-remove-btn" data-batch="${b.batch_id}" data-sj="${sj.id}" title="Remove this abandoned sub-job from the batch">&times; Remove</button>` : '';
      return `<tr>
        <td style="font-family:monospace;font-size:.75rem;">${sj.id.slice(0,8)}&hellip;</td>
        <td>${sj.filename || '—'}</td>
        <td><span class="sj-status ${statusCls}">${statusCls}</span></td>
        <td>${sj.expected_count}</td>
        <td>${sj.total_pages || '—'}</td>
        <td style="display:flex;gap:6px;flex-wrap:wrap;">${reviewBtn}${abandonBtn}${unconfirmBtn}${removeBtn}</td>
      </tr>`;
    }).join('');

    let sjTableEl = cardEl.querySelector('.sub-job-table-container');
    if (!sjTableEl) {
      sjTableEl = document.createElement('div');
      sjTableEl.className = 'sub-job-table-container';
      const ledgerEl2 = cardEl.querySelector('.batch-ledger');
      if (ledgerEl2 && ledgerEl2.nextSibling) {
        cardEl.insertBefore(sjTableEl, ledgerEl2.nextSibling);
      } else {
        cardEl.appendChild(sjTableEl);
      }
    }
    if (b.sub_jobs && b.sub_jobs.length > 0) {
      sjTableEl.innerHTML = `<table class="sub-job-table">
        <thead><tr>
          <th>Sub-job</th><th>File</th><th>Status</th><th>Expected</th><th>Pages</th><th>Actions</th>
        </tr></thead>
        <tbody>${subJobRows}</tbody>
      </table>`;
    } else {
      sjTableEl.innerHTML = `<div style="color:var(--soft);font-size:.85rem;padding:8px 0;">No files uploaded yet.</div>`;
    }
    // Re-attach sub-job action listeners
    cardEl.querySelectorAll(`.sj-review-btn[data-batch="${b.batch_id}"]`).forEach(btn => {
      btn.addEventListener('click', () => _reviewSubJob(b.batch_id, btn.dataset.sj));
    });
    cardEl.querySelectorAll(`.sj-abandon-btn[data-batch="${b.batch_id}"]`).forEach(btn => {
      btn.addEventListener('click', () => _abandonSubJob(b.batch_id, btn.dataset.sj));
    });
    cardEl.querySelectorAll(`.sj-unconfirm-btn[data-batch="${b.batch_id}"]`).forEach(btn => {
      btn.addEventListener('click', () => _unconfirmSubJob(b.batch_id, btn.dataset.sj));
    });
    cardEl.querySelectorAll(`.sj-remove-btn[data-batch="${b.batch_id}"]`).forEach(btn => {
      btn.addEventListener('click', () => _removeSubJob(b.batch_id, btn.dataset.sj));
    });
  }
}

function renderBatchCard(b) {
  const ledger = b.ledger;
  const statusLabel = b.status === 'complete' ? '&#10003; Complete' : 'Open';
  const statusColor = b.status === 'complete' ? 'color:var(--success);font-weight:700;' : 'color:var(--warn);font-weight:700;';
  const archivedTag = b.archived ? ' <span style="font-size:.7rem;color:var(--soft);font-weight:400;">[archived]</span>' : '';

  const missingText = ledger.missing.length > 0
    ? `<span class="ledger-pill err">${ledger.missing.length} missing</span>` : '';
  const reconcileText = ledger.reconciled
    ? `<span class="ledger-pill ok">&#10003; Reconciled</span>` : '';

  return `<div class="batch-card" id="batch-card-${b.batch_id}">
    <div class="batch-header">
      <div>
        <div class="batch-title">Batch <span class="batch-id-label">${b.batch_id.slice(0,8)}&hellip;</span>
          <span style="${statusColor}font-size:.82rem;margin-left:8px;">${statusLabel}${archivedTag}</span>
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
    <div class="sub-job-table-container">
      <div style="color:var(--soft);font-size:.85rem;padding:8px 0;">No files uploaded yet.</div>
    </div>
    <div class="batch-actions">
      ${_buildActionsHTML(b)}
    </div>
    <div class="add-sub-job-form hidden" id="add-sj-form-${b.batch_id}">
      <h4>Add files to this batch</h4>
      <div class="dropzone" id="sj-dropzone-${b.batch_id}">
        <div class="dropzone-label">Drag &amp; drop PDF files here, or click to browse</div>
        <input type="file" accept=".pdf" multiple id="sj-pdf-${b.batch_id}" style="display:none;">
      </div>
      <div class="file-count-table hidden" id="sj-count-table-${b.batch_id}">
        <p style="font-size:.82rem;color:var(--soft);margin:8px 0 4px;">Enter the expected ticket count for each file:</p>
        <table class="sub-job-table" id="sj-count-rows-${b.batch_id}">
          <thead><tr><th>File</th><th>Expected tickets <span class="req">*</span></th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
      <div class="error-msg hidden" id="sj-error-${b.batch_id}"></div>
      <div style="display:flex;gap:8px;margin-top:8px;">
        <button class="btn-primary sj-upload-btn" data-batch="${b.batch_id}" style="font-size:.88rem;padding:7px 16px;" disabled>Start Detection</button>
        <button class="btn-outline sj-cancel-btn" data-batch="${b.batch_id}" style="font-size:.82rem;">Cancel</button>
      </div>
    </div>
  </div>`;
}

// ── Action helpers ──

function _openAddFileForm(batchId) {
  const form = el(`add-sj-form-${batchId}`);
  if (!form) return;
  form.classList.toggle('hidden');
}

function _downloadBatch(batchId, btn) {
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Building…';
  fetch(`/api/batches/${batchId}/download`)
    .then(r => {
      if (!r.ok) return r.text().then(t => { throw new Error(t.startsWith('<') ? 'Server error' : t); });
      return r.blob().then(blob => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = url;
        const cd = r.headers.get('Content-Disposition') || '';
        const m = cd.match(/filename="([^"]+)"/);
        a.download = m ? m[1] : `batch_${batchId.slice(0,8)}.zip`;
        a.click();
        btn.textContent = '\u2713 Downloaded';
      });
    })
    .catch(e => {
      alert('Download failed: ' + e.message);
      btn.disabled = false;
      btn.textContent = '\u2B07 Download Batch ZIP';
    });
}

function _archiveBatch(batchId) {
  api('POST', `/api/batches/${batchId}/archive`)
    .then(() => loadBatches())
    .catch(e => alert('Archive failed: ' + e.message));
}

function _unarchiveBatch(batchId) {
  api('POST', `/api/batches/${batchId}/unarchive`)
    .then(() => loadBatches())
    .catch(e => alert('Unarchive failed: ' + e.message));
}

function _deleteBatch(batchId) {
  if (!confirm(`Delete batch ${batchId.slice(0,8)}…? This cannot be undone.`)) return;
  fetch(`/api/batches/${batchId}`, {method:'DELETE'})
    .then(r => {
      if (!r.ok) return r.text().then(t => { throw new Error(t.startsWith('<') ? 'Server error' : t); });
      return r.json();
    })
    .then(() => {
      const card = el(`batch-card-${batchId}`);
      if (card) card.remove();
      loadBatches();
    })
    .catch(e => alert('Delete failed: ' + e.message));
}

function _reviewSubJob(batchId, sjId) {
  activeBatchId = batchId;
  activeSubJobId = sjId;
  jobId = sjId;
  api('GET', `/api/batches/${batchId}`)
    .then(batchData => {
      whitelist = batchData.whitelist;
      batchType = batchData.batch_type;
      activeJobFastMode = !!batchData.fast_mode;
      // Get this sub-job's expected_count from the batch sub_jobs list
      const sjSummary = (batchData.sub_jobs || []).find(s => s.id === sjId);
      activeSubJobExpectedCount = sjSummary ? (sjSummary.expected_count || null) : null;
      return api('GET', `/api/jobs/${sjId}/review`);
    })
    .then(rs => {
      reviewState = rs;
      // Set totalPages from review state so renderPhaseA can build the thumbnail strip
      if (rs.total_pages) totalPages = rs.total_pages;
      hideBulkScreens();
      hide('new-job-screen');
      el('main-area').style.display = '';
      buildPhaseA();
    })
    .catch(e => alert('Failed to load sub-job: ' + e.message));
}

function _abandonSubJob(batchId, sjId) {
  if (!confirm(`Abandon sub-job ${sjId.slice(0,8)}…? This cannot be undone without re-uploading.`)) return;
  api('POST', `/api/batches/${batchId}/sub-jobs/${sjId}/abandon`)
    .then(() => loadBatches())
    .catch(e => alert('Abandon failed: ' + e.message));
}

function _unconfirmSubJob(batchId, sjId) {
  if (!confirm(`Un-confirm sub-job ${sjId.slice(0,8)}\u2026? This will release its tickets back to the batch pool.`)) return;
  api('POST', `/api/batches/${batchId}/sub-jobs/${sjId}/unconfirm`)
    .then(() => loadBatches())
    .catch(e => alert('Un-confirm failed: ' + e.message));
}

function _removeSubJob(batchId, sjId) {
  if (!confirm(`Remove abandoned sub-job ${sjId.slice(0,8)}\u2026 from this batch? This cannot be undone.`)) return;
  fetch(`/api/batches/${batchId}/sub-jobs/${sjId}`, {method: 'DELETE'})
    .then(r => {
      if (!r.ok) return r.text().then(t => { throw new Error(t.startsWith('<') ? 'Server error' : t); });
      return r.json();
    })
    .then(() => loadBatches())
    .catch(e => alert('Remove failed: ' + e.message));
}

// ── Multi-file dropzone and per-file expected-count table ──

function _initDropzone(batchId) {
  const dropzone = el(`sj-dropzone-${batchId}`);
  const fileInput = el(`sj-pdf-${batchId}`);
  const countTable = el(`sj-count-table-${batchId}`);
  const uploadBtn = document.querySelector(`.sj-upload-btn[data-batch="${batchId}"]`);
  if (!dropzone || !fileInput || !countTable || !uploadBtn) return;

  // Click on dropzone opens file picker
  dropzone.addEventListener('click', () => fileInput.click());

  // Drag-and-drop
  dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('dragover'); });
  dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
  dropzone.addEventListener('drop', e => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
    _handleFiles(batchId, Array.from(e.dataTransfer.files).filter(f => f.name.toLowerCase().endsWith('.pdf')));
  });

  // File input change
  fileInput.addEventListener('change', () => {
    _handleFiles(batchId, Array.from(fileInput.files));
  });
}

function _handleFiles(batchId, files) {
  if (!files || files.length === 0) return;
  const countTable = el(`sj-count-table-${batchId}`);
  const tbody = countTable.querySelector('tbody');
  const uploadBtn = document.querySelector(`.sj-upload-btn[data-batch="${batchId}"]`);
  if (!tbody || !uploadBtn) return;

  // Store files on the form element for later retrieval
  const form = el(`add-sj-form-${batchId}`);
  form._pendingFiles = files;

  // Build per-file rows
  tbody.innerHTML = files.map((f, i) => `
    <tr>
      <td style="font-size:.82rem;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${f.name}">${f.name}</td>
      <td><input type="number" min="1" class="sj-count-input" data-idx="${i}" style="width:80px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;" placeholder="e.g. 5"></td>
    </tr>
  `).join('');

  countTable.classList.remove('hidden');

  // Validate: all count inputs must be filled with ≥1
  const validateCounts = () => {
    const inputs = tbody.querySelectorAll('.sj-count-input');
    const allFilled = Array.from(inputs).every(inp => inp.value && parseInt(inp.value) >= 1);
    uploadBtn.disabled = !allFilled;
  };
  tbody.querySelectorAll('.sj-count-input').forEach(inp => inp.addEventListener('input', validateCounts));
  validateCounts();
}

function attachBatchCardListeners(b) {
  const batchId = b.batch_id;
  _initDropzone(batchId);
  // Note: _attachActionsListeners is called by _updateBatchCardDynamic, so we do NOT
  // call it here to avoid double-attaching listeners (which would double-toggle the form).

  // Upload button: submit all pending files sequentially
  const uploadBtn = document.querySelector(`.sj-upload-btn[data-batch="${batchId}"]`);
  if (uploadBtn) {
    uploadBtn.addEventListener('click', async () => {
      const form = el(`add-sj-form-${batchId}`);
      const files = form._pendingFiles || [];
      const countTable = el(`sj-count-table-${batchId}`);
      const inputs = countTable ? countTable.querySelectorAll('.sj-count-input') : [];
      if (files.length === 0) return;

      const errEl = el(`sj-error-${batchId}`);
      errEl.classList.add('hidden');
      uploadBtn.disabled = true;

      // Upload files one at a time; navigate into the first one's review flow
      let firstSubJobData = null;
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const expected = parseInt(inputs[i] ? inputs[i].value : '0');
        if (!expected || expected < 1) continue;
        uploadBtn.innerHTML = `<span class="spinner"></span>Uploading ${i+1}/${files.length}…`;
        try {
          const fd = new FormData();
          fd.append('file', file);
          fd.append('expected_count', String(expected));
          const r = await fetch(`/api/batches/${batchId}/sub-jobs`, {method:'POST', body: fd});
          if (!r.ok) { const t = await r.text(); throw new Error(t.startsWith('<') ? 'Server error' : t); }
          const d = await r.json();
          if (!firstSubJobData) firstSubJobData = d;
        } catch(e) {
          errEl.textContent = `Error uploading ${file.name}: ${e.message}`;
          errEl.classList.remove('hidden');
          uploadBtn.disabled = false;
          uploadBtn.textContent = 'Start Detection';
          return;
        }
      }

      if (!firstSubJobData) return;

      // Navigate into the first sub-job's review flow
      activeBatchId = batchId;
      activeSubJobId = firstSubJobData.sub_job_id;
      activeSubJobExpectedCount = firstSubJobData.expected_count || null;
      jobId = firstSubJobData.sub_job_id;
      totalPages = firstSubJobData.total_pages;
      whitelist = firstSubJobData.whitelist;
      batchType = firstSubJobData.batch_type;
      activeJobFastMode = !!firstSubJobData.fast_mode;
      hideBulkScreens();
      hide('new-job-screen');
      const modeLabel = activeJobFastMode
        ? '\u26a1 FAST MODE \u2014 reading first pages of pink-bounded blocks only'
        : '\u25a0 FULL MODE \u2014 reading every page';
      el('progress-title').textContent = modeLabel;
      show('progress-screen');
      el('main-area').style.display = '';
      pollStatus();
    });
  }

  // Cancel upload form
  const cancelBtn = document.querySelector(`.sj-cancel-btn[data-batch="${batchId}"]`);
  if (cancelBtn) cancelBtn.addEventListener('click', () => {
    el(`add-sj-form-${batchId}`).classList.add('hidden');
  });
}

// ── Archive toggle button (in bulk-screen header) ──
(function() {
  const toggleBtn = el('toggle-archived-btn');
  if (toggleBtn) {
    toggleBtn.addEventListener('click', () => {
      _showArchived = !_showArchived;
      loadBatches();
    });
  }
})();

// ── Sub-job confirm: override the final-confirm-btn for sub-job flow ──
// For bulk sub-jobs: call the batch confirm endpoint (returns JSON + claims ledger),
// then navigate back to the batch dashboard. No per-file ZIP download.
el('final-confirm-btn').addEventListener('click', async function(e) {
  if (!activeSubJobId) return; // not a bulk sub-job — let the original listener handle it
  e.stopImmediatePropagation();
  el('final-confirm-btn').disabled = true;
  el('final-confirm-btn').innerHTML = '<span class="spinner"></span>Confirming…';
  try {
    const r = await fetch(`/api/batches/${activeBatchId}/sub-jobs/${activeSubJobId}/confirm`, {method:'POST'});
    if (!r.ok) {
      const t = await r.text();
      let msg = t;
      try { const j = JSON.parse(t); msg = j.detail || j.message || t; } catch(_) {}
      throw new Error(msg.startsWith('<') ? 'Server error (unexpected HTML response)' : msg);
    }
    const data = await r.json();
    // Tickets claimed — navigate back to batch dashboard
    const batchId = activeBatchId;
    activeSubJobId = null;
    activeSubJobExpectedCount = null;
    jobId = null;
    reviewState = null;
    hideReconcileScreen();
    showBulkScreen();
    // showBulkScreen() calls loadBatches() immediately, but the server may still be
    // persisting the confirm. Fire a second refresh after 800ms to ensure the
    // Download button appears without needing to wait for the 5s poll cycle.
    setTimeout(loadBatches, 800);
  } catch(err) {
    alert('Confirm failed: ' + err.message);
    el('final-confirm-btn').disabled = false;
    el('final-confirm-btn').textContent = 'Confirm Boundaries →';
  }
}, true); // capture phase so it fires before the original listener

// ── Phase A counter: use this file's expected_count, not the batch whitelist length ──
(function() {
  const origRender = window.renderPhaseA || (typeof renderPhaseA !== 'undefined' ? renderPhaseA : null);
  // We patch renderPhaseA after it's defined by overriding the counter update.
  // The cleanest approach: override the counter element text after orig() runs.
  const origBuildPhaseA = buildPhaseA;
  window.buildPhaseA = function() {
    origBuildPhaseA();
    // Patch the boundary counter to use this file's expected_count
    if (activeSubJobId && activeSubJobExpectedCount != null) {
      const expected = activeSubJobExpectedCount - 1; // N tickets → N-1 splits
      const actual = boundaries.length - 1;
      const counter = el('phase-a-boundary-counter');
      if (counter) {
        if (actual === expected) {
          counter.innerHTML = `<span style="color:var(--success);font-weight:600">&#10003; ${actual} of ${expected} splits placed (${activeSubJobExpectedCount} tickets)</span>`;
        } else {
          const diff = expected - actual;
          const msg = diff > 0 ? `${diff} split${diff>1?'s':''} missing` : `${-diff} extra split${-diff>1?'s':''}`;
          counter.innerHTML = `<span style="color:var(--danger);font-weight:600">&#9888; ${actual} of ${expected} splits placed &mdash; ${msg} (${activeSubJobExpectedCount} tickets expected)</span>`;
        }
      }
      // Also inject Back-to-Batch button
      const header = document.querySelector('.phase-header');
      if (header && !header.querySelector('.back-to-batch-btn')) {
        const btn = document.createElement('button');
        btn.className = 'btn-outline back-to-batch-btn';
        btn.style.cssText = 'font-size:.82rem;padding:5px 12px;';
        btn.textContent = '\u2190 Back to Batch';
        btn.addEventListener('click', () => {
          activeSubJobId = null;
          activeSubJobExpectedCount = null;
          jobId = null;
          reviewState = null;
          hide('phase-a-screen');
          showBulkScreen();
        });
        header.insertBefore(btn, header.firstChild);
      }
    }
  };

  // Use a MutationObserver on the counter element so every divider click (which calls
  // renderPhaseA internally) gets the corrected expected count.
  const counterEl = el('phase-a-boundary-counter');
  if (counterEl) {
    const obs = new MutationObserver(() => {
      if (!activeSubJobId || activeSubJobExpectedCount == null) return;
      // Prevent re-entrant firing
      obs.disconnect();
      const expected = activeSubJobExpectedCount - 1;
      const actual = boundaries.length - 1;
      if (actual === expected) {
        counterEl.innerHTML = `<span style="color:var(--success);font-weight:600">&#10003; ${actual} of ${expected} splits placed (${activeSubJobExpectedCount} tickets)</span>`;
      } else {
        const diff = expected - actual;
        const msg = diff > 0 ? `${diff} split${diff>1?'s':''} missing` : `${-diff} extra split${-diff>1?'s':''}`;
        counterEl.innerHTML = `<span style="color:var(--danger);font-weight:600">&#9888; ${actual} of ${expected} splits placed &mdash; ${msg} (${activeSubJobExpectedCount} tickets expected)</span>`;
      }
      obs.observe(counterEl, {childList: true, subtree: true, characterData: true});
    });
    obs.observe(counterEl, {childList: true, subtree: true, characterData: true});
  }
})();
