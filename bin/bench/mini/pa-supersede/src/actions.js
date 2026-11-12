'use strict';

// An action: { id, key, status: 'pending' | 'done' | 'superseded' }
function supersedePending(actions, incoming) {
  if (!incoming) return actions.slice();
  return [...actions, incoming];
}

function pendingCount(actions) {
  return actions.filter((a) => a.status === 'pending').length;
}

module.exports = { supersedePending, pendingCount };
