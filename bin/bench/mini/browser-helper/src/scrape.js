'use strict';
const { parsePrice } = require('./price');

async function readPrice(page, selector) {
  const text = await page.textContent(selector);
  return parsePrice(text);
}

module.exports = { readPrice };
