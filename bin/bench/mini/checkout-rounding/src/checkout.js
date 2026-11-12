'use strict';

function roundCents(x) {
  return Math.floor(x * 100) / 100;
}

function lineTotal(price, qty) {
  return roundCents(price * qty);
}

module.exports = { roundCents, lineTotal };
