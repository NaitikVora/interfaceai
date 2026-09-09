// Resolves a TableCellSpec to the XPaths of every matching cell (0, 1 or many).
// Evaluated with page.evaluate(script, spec). Pure read-only.
(spec) => {
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const xpathOf = (el) => {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1) {
      const tag = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (!parent) {
        parts.unshift(tag);
        break;
      }
      const sameTag = Array.from(parent.children).filter((c) => c.tagName === cur.tagName);
      parts.unshift(sameTag.length > 1 ? `${tag}[${sameTag.indexOf(cur) + 1}]` : tag);
      cur = parent;
    }
    return "/" + parts.join("/");
  };

  const results = [];
  for (const table of document.querySelectorAll("table")) {
    if (table.querySelector("table")) continue;
    const rows = Array.from(table.rows);
    const headerRow = rows.find((r) => r.querySelector("th"));
    const headers = headerRow ? Array.from(headerRow.cells).map((c) => norm(c.textContent)) : [];

    if (spec.table_headers && spec.table_headers.length) {
      const wanted = spec.table_headers.map(norm);
      if (!wanted.every((h) => headers.includes(h))) continue;
    }

    let columnIndex = spec.column_index;
    if (spec.column_header != null) {
      columnIndex = headers.indexOf(norm(spec.column_header));
      if (columnIndex < 0) continue;
    }
    if (columnIndex == null || columnIndex < 0) continue;

    for (const row of rows) {
      if (row === headerRow) continue;
      const cells = Array.from(row.cells);
      if (!cells.some((c) => norm(c.textContent) === norm(spec.row_match))) continue;
      const cell = cells[columnIndex];
      if (cell) results.push(xpathOf(cell));
    }
  }
  return results;
}
