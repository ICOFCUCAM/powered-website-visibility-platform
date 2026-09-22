/* Live build logs.
 *
 * Progressive enhancement only: the page already rendered every line the
 * server had, and every action on it is a plain form. This adds the lines
 * that arrive afterwards, and does nothing at all if it fails to load.
 */
(function () {
  var log = document.getElementById('log');
  if (!log || log.dataset.terminal === 'true') return;

  var status = document.getElementById('log-status');
  var shortId = log.dataset.shortId;
  var cursor = parseInt(log.dataset.cursor || '0', 10);
  var placeholder = log.querySelector('.log-empty');

  // Only auto-scroll while the reader is already at the bottom. Yanking the
  // view back down while someone is reading an error further up is the most
  // irritating thing a live log can do.
  function atBottom() {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  }

  function append(entry) {
    if (placeholder) { placeholder.remove(); placeholder = null; }
    var stick = atBottom();
    var line = document.createElement('span');
    line.className = 'log-line log-' + entry.stream;
    line.textContent = entry.line;
    log.appendChild(line);
    if (stick) log.scrollTop = log.scrollHeight;
  }

  var source = new EventSource('/api/deployments/' + shortId + '/logs/stream?after=' + cursor);

  source.onmessage = function (event) {
    var entry = JSON.parse(event.data);
    cursor = entry.seq;
    append(entry);
  };

  source.addEventListener('done', function (event) {
    source.close();
    var result = JSON.parse(event.data);
    status.textContent = 'finished — ' + result.status;
    // Reload so the page's actions match the new state: a finished deploy
    // offers promote and redeploy, which the streaming page did not.
    setTimeout(function () { window.location.reload(); }, 900);
  });

  source.onerror = function () {
    // EventSource reconnects by itself; say so rather than looking frozen.
    status.textContent = 'reconnecting…';
  };

  log.scrollTop = log.scrollHeight;
})();
