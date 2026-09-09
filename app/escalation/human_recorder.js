// Installed into the live page. Reports human clicks/changes/submits during HUMAN_CONTROL via a
// Playwright binding named __cuaHumanEvent. Password values are never reported.
(() => {
  if (window.__cuaHumanRecorderInstalled) return;
  window.__cuaHumanRecorderInstalled = true;
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, 120);
  const describe = (el) => {
    if (!el || !el.tagName) return {};
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    const label = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    const isButtonLike = ["submit", "button", "reset"].includes(type);
    let value = null;
    if (type === "password") value = "[masked]";
    else if ("value" in el && tag !== "button" && !isButtonLike) value = norm(el.value);
    let text = null;
    if (tag === "a" || tag === "button") text = norm(el.textContent);
    else if (isButtonLike) text = norm(el.value);
    return {
      tag,
      type,
      name: el.getAttribute("name") || null,
      label: label
        ? norm(label.textContent)
        : el.getAttribute("aria-label") || el.getAttribute("title") || null,
      text,
      href: el.getAttribute("href") || null,
      value,
    };
  };
  const report = (kind, el) => {
    try {
      window.__cuaHumanEvent({ kind, url: location.href, ...describe(el) });
    } catch (e) {
      /* binding not available on this page */
    }
  };
  const interactive = "a,button,input,select,textarea,[role=button]";
  document.addEventListener(
    "click",
    (e) => report("click", (e.target.closest && e.target.closest(interactive)) || e.target),
    true
  );
  document.addEventListener("change", (e) => report("change", e.target), true);
  document.addEventListener(
    "submit",
    (e) => report("submit", e.target.querySelector("[type=submit]") || e.target),
    true
  );
})();
