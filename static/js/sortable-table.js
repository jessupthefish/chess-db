// Click-to-sort table headers. Any <table> with <th data-key="..."> headers
// and matching data-<key> attributes on each <tbody> row becomes sortable —
// used by the player-openings breakdown and the opening-explorer "next
// moves" table.
export function initSortableTable(table) {
  if (!table) return;
  const tbody = table.querySelector('tbody');
  let sortState = {};

  table.querySelectorAll('th[data-key]').forEach((th) => {
    th.style.cursor = 'pointer';
    th.addEventListener('click', () => {
      const key = th.dataset.key;
      const type = th.dataset.sort;
      const asc = !(sortState[key] === 'asc');
      sortState = { [key]: asc ? 'asc' : 'desc' };

      const rows = Array.from(tbody.querySelectorAll('tr'));
      rows.sort((a, b) => {
        let av = a.dataset[key], bv = b.dataset[key];
        if (type === 'num') { av = parseFloat(av); bv = parseFloat(bv); }
        if (av < bv) return asc ? -1 : 1;
        if (av > bv) return asc ? 1 : -1;
        return 0;
      });
      rows.forEach((r) => tbody.appendChild(r));
    });
  });
}
