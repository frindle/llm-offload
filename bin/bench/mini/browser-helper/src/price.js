'use strict';

// Pure helper: also inlined into the page by scrape.js, so keep it free of node APIs.
function parsePrice(text) {
  const m = /[0-9.]+/.exec(String(text));
  return m ? parseFloat(m[0]) : null;
}

if (typeof module !== 'undefined') module.exports = { parsePrice };
