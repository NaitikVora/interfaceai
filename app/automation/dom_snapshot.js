// Builds a bounded, structured observation of the current document.
// Evaluated with page.evaluate(script, limits). Pure read-only; never mutates the page.
(limits) => {
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none";
  };

  const cssPath = (el) => {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.body) {
      if (cur.id && document.querySelectorAll("#" + CSS.escape(cur.id)).length === 1) {
        parts.unshift("#" + CSS.escape(cur.id));
        return parts.join(" > ");
      }
      let part = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (parent) {
        const sameTag = Array.from(parent.children).filter((c) => c.tagName === cur.tagName);
        if (sameTag.length > 1) part += `:nth-of-type(${sameTag.indexOf(cur) + 1})`;
      }
      parts.unshift(part);
      cur = parent;
    }
    return "body > " + parts.join(" > ");
  };

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

  const TEXT_INPUT_TYPES = new Set(["", "text", "password", "email", "tel", "url", "number", "date"]);
  const roleOf = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit.toLowerCase();
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "a") return el.hasAttribute("href") ? "link" : "";
    if (tag === "button") return "button";
    if (tag === "select") return el.multiple || el.size > 1 ? "listbox" : "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      if (["button", "submit", "reset", "image"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "search") return "searchbox";
      if (type === "number") return "spinbutton";
      if (type === "range") return "slider";
      if (TEXT_INPUT_TYPES.has(type)) return "textbox";
      return "";
    }
    return "";
  };

  const labelText = (el) => {
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) return norm(lab.textContent);
    }
    const wrapping = el.closest("label");
    if (wrapping) return norm(wrapping.textContent);
    return null;
  };

  const accessibleName = (el, role, label) => {
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel) return norm(ariaLabel);
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const parts = labelledBy.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean);
      if (parts.length) return norm(parts.map((p) => p.textContent).join(" "));
    }
    if (label) return label;
    const tag = el.tagName.toLowerCase();
    if (tag === "input") {
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (["button", "submit", "reset"].includes(type)) {
        return norm(el.value) || (type === "submit" ? "Submit" : type === "reset" ? "Reset" : "");
      }
      if (type === "image") return norm(el.getAttribute("alt") || el.value);
    }
    if (role === "button" || role === "link" || tag === "button" || tag === "a") {
      const imgAlt = Array.from(el.querySelectorAll("img[alt]")).map((i) => i.getAttribute("alt")).join(" ");
      return norm(el.textContent + " " + imgAlt);
    }
    const title = el.getAttribute("title");
    if (title) return norm(title);
    const placeholder = el.getAttribute("placeholder");
    if (placeholder) return norm(placeholder);
    return "";
  };

  const CONTAINER_SELECTOR =
    "form, nav, main, aside, header, footer, section, fieldset, [role], [id], td[class], div[class], table[class]";
  const containerOf = (el) => {
    let cur = el.parentElement;
    while (cur && cur !== document.body && cur !== document.documentElement) {
      if (cur.matches(CONTAINER_SELECTOR)) {
        const attrs = {};
        for (const key of ["id", "action", "role", "class"]) {
          const v = cur.getAttribute(key);
          if (v) attrs[key] = norm(v);
        }
        return { tag: cur.tagName.toLowerCase(), attributes: attrs, css: cssPath(cur) };
      }
      cur = cur.parentElement;
    }
    return null;
  };

  const CONTROL_SELECTOR =
    "a[href], button, input, select, textarea, [role=button], [role=link], [role=textbox], " +
    "[role=combobox], [role=checkbox], [role=radio], [role=menuitem], [role=tab], [onclick]";

  const controls = [];
  let controlsTruncated = false;
  const seen = new Set();
  for (const el of document.querySelectorAll(CONTROL_SELECTOR)) {
    if (seen.has(el)) continue;
    seen.add(el);
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (el.tagName === "INPUT" && type === "hidden") continue;
    if (!isVisible(el)) continue;
    if (controls.length >= limits.max_controls) {
      controlsTruncated = true;
      break;
    }
    const role = roleOf(el);
    const label = labelText(el);
    const attrs = {};
    for (const key of ["name", "id", "type", "title", "placeholder", "href", "alt"]) {
      const v = el.getAttribute(key);
      if (v) attrs[key] = key === "href" ? el.getAttribute("href") : norm(v);
    }
    if (el.tagName === "INPUT" && ["button", "submit", "reset"].includes(type) && el.value) {
      attrs.value = norm(el.value);
    } else if (
      (el.tagName === "INPUT" || el.tagName === "TEXTAREA") &&
      type !== "password" &&
      el.value
    ) {
      attrs.current_value = clip(norm(el.value), 60);
    }
    let options = null;
    if (el.tagName === "SELECT") {
      options = Array.from(el.options).slice(0, 30).map((o) => norm(o.textContent));
      const sel = el.selectedOptions[0];
      if (sel) attrs.current_value = norm(sel.textContent);
    }
    const rect = el.getBoundingClientRect();
    const textContent = ["A", "BUTTON", "TD", "TH", "SPAN", "DIV"].includes(el.tagName)
      ? clip(norm(el.textContent), 80) || null
      : null;
    controls.push({
      ref: `c${controls.length + 1}`,
      tag: el.tagName.toLowerCase(),
      role,
      name: clip(accessibleName(el, role, label), 120),
      label: label ? clip(label, 120) : null,
      text: textContent,
      attributes: attrs,
      enabled: !(el.disabled || el.getAttribute("aria-disabled") === "true"),
      visible: true,
      checked: type === "checkbox" || type === "radio" ? !!el.checked : null,
      options,
      container: containerOf(el),
      css: cssPath(el),
      xpath: xpathOf(el),
      bbox: [rect.x, rect.y, rect.width, rect.height],
    });
  }

  const tables = [];
  for (const table of document.querySelectorAll("table")) {
    if (tables.length >= limits.max_tables) break;
    if (!isVisible(table) || table.querySelector("table")) continue;
    const rows = Array.from(table.rows);
    if (rows.length === 0) continue;
    const headerRow = rows.find((r) => r.querySelector("th"));
    const headers = headerRow
      ? Array.from(headerRow.cells).map((c) => clip(norm(c.textContent), limits.max_cell_chars))
      : [];
    const dataRows = rows.filter((r) => r !== headerRow && r.querySelector("td"));
    if (dataRows.length === 0) continue;
    const shown = dataRows.slice(0, limits.max_table_rows).map((r) =>
      Array.from(r.cells)
        .slice(0, limits.max_table_cols)
        .map((c) => clip(norm(c.innerText !== undefined ? c.innerText : c.textContent), limits.max_cell_chars))
    );
    tables.push({
      ref: `t${tables.length + 1}`,
      headers: headers.slice(0, limits.max_table_cols),
      rows: shown,
      total_rows: dataRows.length,
      truncated: dataRows.length > shown.length,
      css: cssPath(table),
    });
  }

  const headings = Array.from(document.querySelectorAll("h1, h2, h3, h4, h5, h6"))
    .filter(isVisible)
    .map((h) => clip(norm(h.textContent), 120))
    .filter(Boolean)
    .slice(0, 10);

  const MESSAGE_SELECTOR =
    "[role=alert], [role=status], .msg-error, .msg-info, .msg-ok, .error, .alert, .notice, .warning, .message";
  const messages = Array.from(document.querySelectorAll(MESSAGE_SELECTOR))
    .filter(isVisible)
    .map((m) => clip(norm(m.innerText), 300))
    .filter(Boolean)
    .slice(0, 8);

  const fullText = norm(document.body ? document.body.innerText : "");
  const textTruncated = fullText.length > limits.max_text_chars;

  return {
    url: location.href,
    title: norm(document.title),
    headings,
    messages,
    text: textTruncated ? fullText.slice(0, limits.max_text_chars) : fullText,
    text_truncated: textTruncated,
    controls,
    controls_truncated: controlsTruncated,
    tables,
  };
}
