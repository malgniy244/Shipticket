#!/usr/bin/env node
// Validate index.html: JS syntax check + required ID presence check
const fs = require('fs');
const html = fs.readFileSync('webapp/static/index.html', 'utf8');

// Extract the script content (greedy match to get the full block)
const scriptStart = html.indexOf('<script>');
const scriptEnd = html.lastIndexOf('</script>');
if (scriptStart < 0 || scriptEnd < 0) {
  console.error('No script block found');
  process.exit(1);
}
const scriptContent = html.slice(scriptStart + 8, scriptEnd);

try {
  new Function(scriptContent);
  console.log('JS syntax OK');
} catch(e) {
  console.error('JS syntax error:', e.message);
  process.exit(1);
}

// Check all required IDs exist in HTML
const ids = [
  'bulk-mode-btn','bulk-screen','new-batch-screen',
  'nb-bt-tib','nb-bt-nontib','nb-wl-input','nb-fast-mode-cb',
  'nb-submit-btn','nb-cancel-btn','nb-error',
  'bulk-new-batch-btn','batches-container'
];
const missing = ids.filter(id => html.indexOf('id="' + id + '"') < 0);
if (missing.length > 0) {
  console.error('Missing HTML IDs:', missing);
  process.exit(1);
}
console.log('All', ids.length, 'required IDs present');
console.log('Validation passed.');
