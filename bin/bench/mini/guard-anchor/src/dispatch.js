'use strict';

function dispatch(job, opts) {
  if (!job) return { sent: false, why: 'no job' };
  const log = (opts && opts.log) || (() => {});
  log('dispatch ' + job.id);
  const best = job.queues[0];
  if (!best) return { sent: false, why: 'no queue' };
  if (opts && opts.dry) return { sent: false, why: 'dry run', queue: best.name };
  if (job.retries > 3) throw new Error('too many retries');
  log('sent to ' + best.name);
  return { sent: true, queue: best.name };
}

module.exports = { dispatch };
