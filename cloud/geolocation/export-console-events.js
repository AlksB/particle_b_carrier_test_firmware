// Paste into the browser DevTools console on a Particle Console event stream
// page (pause the stream first). The event table is virtualized, so "save
// page" only keeps the visible rows; this scrolls through the whole list and
// downloads every row as events.tsv: name, data, device, published at.
(async () => {
  const grid = document.querySelector('.ReactVirtualized__Grid');
  const seen = new Map();
  const collect = () => grid.querySelectorAll('tr').forEach(tr => {
    const cells = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
    if (cells.length) seen.set(parseInt(tr.style.top), cells.join('\t'));
  });
  const step = grid.clientHeight - 50;
  for (let y = 0; y <= grid.scrollHeight; y += step) {
    grid.scrollTop = y;
    await new Promise(r => setTimeout(r, 200));
    collect();
  }
  const rows = [...seen.entries()].sort((a, b) => a[0] - b[0]).map(e => e[1]);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([rows.join('\n')], { type: 'text/plain' }));
  a.download = 'events.tsv';
  a.click();
  console.log(rows.length, 'rows');
})();
