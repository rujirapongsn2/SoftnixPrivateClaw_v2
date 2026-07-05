function findByLabel(text) {
  const needle = String(text || "").trim().toLowerCase();
  if (!needle) return null;
  const controls = queryAllDeep(
    "input, textarea, select, button, a[href], [role='button'], [role='link'], [contenteditable='true']"
  );
  for (const control of controls) {
    const haystack = [
      control.getAttribute("aria-label"),
      control.getAttribute("aria-labelledby"),
      control.getAttribute("name"),
      control.getAttribute("placeholder"),
      control.getAttribute("title"),
      control.getAttribute("value"),
      control.id,
      control.textContent
    ].filter(Boolean).join(" ").toLowerCase();
    if (haystack.includes(needle)) return control;
  }
  for (const label of queryAllDeep("label")) {
    if (!label.textContent || !label.textContent.toLowerCase().includes(needle)) continue;
    if (label.htmlFor) {
      const target = document.getElementById(label.htmlFor);
      if (target) return target;
    }
    const nested = label.querySelector("input, textarea, select, button");
    if (nested) return nested;
  }
  return null;
}

function target(selectorOrLabel) {
  if (!selectorOrLabel) return null;
  try {
    const selected = queryDeep(selectorOrLabel);
    if (selected) return selected;
  } catch (_) {
  }
  return findByLabel(selectorOrLabel);
}

async function waitForTarget(selectorOrLabel, timeoutMs = 8000) {
  const deadline = Date.now() + Math.max(250, timeoutMs);
  let found = target(selectorOrLabel);
  while (!found && Date.now() < deadline) {
    await sleep(250);
    found = target(selectorOrLabel);
  }
  return found;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function openRoots(root = document) {
  const roots = [root];
  const walk = (node) => {
    if (!node || !node.querySelectorAll) return;
    for (const el of Array.from(node.querySelectorAll("*"))) {
      if (el.shadowRoot) {
        roots.push(el.shadowRoot);
        walk(el.shadowRoot);
      }
    }
  };
  walk(root);
  return roots;
}

function queryAllDeep(selector) {
  const results = [];
  for (const root of openRoots()) {
    try {
      results.push(...Array.from(root.querySelectorAll(selector)));
    } catch (_) {
      return [];
    }
  }
  return Array.from(new Set(results));
}

function queryDeep(selector) {
  return queryAllDeep(selector)[0] || null;
}

function visibleText(root = document.body) {
  const parts = [];
  const collect = (node) => {
    if (!node) return;
    if (node.nodeType === Node.TEXT_NODE) {
      const text = String(node.nodeValue || "").replace(/\s+/g, " ").trim();
      if (text) parts.push(text);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return;
    const el = node;
    if (el.nodeType === Node.ELEMENT_NODE) {
      const style = window.getComputedStyle(el);
      if (style && (style.display === "none" || style.visibility === "hidden")) return;
      const tag = String(el.tagName || "").toLowerCase();
      if (["script", "style", "noscript", "template"].includes(tag)) return;
    }
    for (const child of Array.from(el.childNodes || [])) collect(child);
    if (el.shadowRoot) collect(el.shadowRoot);
  };
  collect(root);
  return parts.join("\n").replace(/\n{3,}/g, "\n\n");
}

function sameOriginFrameDocuments() {
  const docs = [];
  for (const frame of Array.from(document.querySelectorAll("iframe, frame")).slice(0, 20)) {
    try {
      if (frame.contentDocument && frame.contentWindow && frame.contentWindow.location.origin === location.origin) {
        docs.push({
          url: frame.contentWindow.location.href,
          title: frame.contentDocument.title || "",
          document: frame.contentDocument
        });
      }
    } catch (_) {
    }
  }
  return docs;
}

function showSoftnixOverlay(message) {
  let overlay = document.getElementById("softnix-browser-activity");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "softnix-browser-activity";
    overlay.style.cssText = [
      "position:fixed",
      "right:18px",
      "bottom:18px",
      "z-index:2147483647",
      "padding:10px 12px",
      "border-radius:8px",
      "background:#102033",
      "color:#fff",
      "font:13px/1.35 system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
      "box-shadow:0 8px 28px rgba(15,23,42,.24)",
      "max-width:280px",
      "pointer-events:none",
      "opacity:.95"
    ].join(";");
    document.documentElement.appendChild(overlay);
  }
  overlay.textContent = message || "Softnix browser automation";
  return overlay;
}

async function flashActivity(message, ms = 450) {
  const overlay = showSoftnixOverlay(message);
  await sleep(ms);
  return overlay;
}

async function highlightElement(el, label = "Target") {
  if (!el || typeof el.getBoundingClientRect !== "function") return;
  showSoftnixOverlay(label);
  el.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
  await sleep(450);
  const previousOutline = el.style.outline;
  const previousBoxShadow = el.style.boxShadow;
  el.style.outline = "3px solid #2f9cf4";
  el.style.boxShadow = "0 0 0 6px rgba(47,156,244,.22)";
  await sleep(550);
  el.style.outline = previousOutline;
  el.style.boxShadow = previousBoxShadow;
}

async function visiblePageScan() {
  showSoftnixOverlay("Reading page content...");
  const originalY = window.scrollY || 0;
  const maxY = Math.max(
    0,
    document.documentElement.scrollHeight - window.innerHeight,
    document.body?.scrollHeight ? document.body.scrollHeight - window.innerHeight : 0
  );
  window.scrollTo({ top: 0, behavior: "smooth" });
  await sleep(450);
  if (maxY > 120) {
    const steps = Math.min(6, Math.max(2, Math.ceil(maxY / Math.max(window.innerHeight * 0.75, 360))));
    for (let i = 1; i <= steps; i += 1) {
      const top = Math.round((maxY * i) / steps);
      showSoftnixOverlay(`Reading page content... ${i}/${steps}`);
      window.scrollTo({ top, behavior: "smooth" });
      await sleep(520);
    }
  }
  showSoftnixOverlay("Finished reading page");
  await sleep(250);
  window.scrollTo({ top: originalY, behavior: "smooth" });
  await sleep(350);
}

function optionValueFor(select, value) {
  const raw = String(value || "");
  const wanted = raw.trim().toLowerCase();
  for (const option of Array.from(select.options || [])) {
    if (String(option.value || "").trim().toLowerCase() === wanted) return option.value;
    if (String(option.textContent || "").trim().toLowerCase() === wanted) return option.value;
  }
  return raw;
}

function setValue(el, value) {
  el.focus();
  const tag = String(el.tagName || "").toLowerCase();
  const type = String(el.getAttribute("type") || "").toLowerCase();
  if (type === "checkbox") {
    const normalized = String(value).trim().toLowerCase();
    el.checked = ["1", "true", "yes", "y", "on", "checked"].includes(normalized);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return;
  }
  if (type === "radio") {
    const normalized = String(value).trim().toLowerCase();
    const shouldCheck = !normalized || ["1", "true", "yes", "y", "on", "checked"].includes(normalized)
      || String(el.value || "").trim().toLowerCase() === normalized;
    if (shouldCheck) el.checked = true;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return;
  }
  if (tag === "select") {
    el.value = optionValueFor(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return;
  }
  if (el.isContentEditable) {
    el.textContent = String(value);
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: String(value) }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return;
  }
  el.value = value;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function inspectForm() {
  return queryAllDeep("input, textarea, select, [contenteditable='true']").slice(0, 120).map((el) => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute("type") || "",
    name: el.getAttribute("name") || "",
    id: el.id || "",
    placeholder: el.getAttribute("placeholder") || "",
    ariaLabel: el.getAttribute("aria-label") || "",
    label: associatedLabelText(el),
    required: Boolean(el.required),
    disabled: Boolean(el.disabled),
    options: el.tagName.toLowerCase() === "select"
      ? Array.from(el.options || []).slice(0, 40).map((option) => ({
          text: String(option.textContent || "").trim(),
          value: option.value || ""
        }))
      : undefined
  }));
}

function associatedLabelText(el) {
  if (!el) return "";
  if (el.id) {
    const direct = queryDeep(`label[for="${CSS.escape(el.id)}"]`);
    if (direct) return String(direct.textContent || "").replace(/\s+/g, " ").trim();
  }
  const parent = el.closest ? el.closest("label") : null;
  return parent ? String(parent.textContent || "").replace(/\s+/g, " ").trim() : "";
}

function extractLinksFrom(root, baseUrl) {
  return Array.from(root.querySelectorAll("a[href]")).slice(0, 180).map((anchor) => {
    let href = "";
    try {
      href = new URL(anchor.getAttribute("href"), baseUrl).href;
    } catch (_) {
      href = anchor.getAttribute("href") || "";
    }
    return {
      text: String(anchor.textContent || "").replace(/\s+/g, " ").trim().slice(0, 180),
      href
    };
  }).filter((item) => item.href);
}

function extractPage() {
  const framePages = sameOriginFrameDocuments().map((frame) => ({
    url: frame.url,
    title: frame.title,
    text: visibleText(frame.document.body).slice(0, 6000),
    links: extractLinksFrom(frame.document, frame.url).slice(0, 80)
  }));
  const links = [
    ...extractLinksFrom(document, location.href),
    ...queryAllDeep("a[href]").map((anchor) => {
      let href = "";
      try {
        href = new URL(anchor.getAttribute("href"), location.href).href;
      } catch (_) {
        href = anchor.getAttribute("href") || "";
      }
      return {
        text: String(anchor.textContent || "").replace(/\s+/g, " ").trim().slice(0, 180),
        href
      };
    }).filter((item) => item.href)
  ];
  const uniqueLinks = [];
  const seen = new Set();
  for (const link of links) {
    const key = `${link.href}|${link.text}`;
    if (seen.has(key)) continue;
    seen.add(key);
    uniqueLinks.push(link);
  }
  const text = visibleText(document.body)
    .replace(/\s+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim()
    .slice(0, 16000);
  return {
    url: location.href,
    title: document.title || "",
    text,
    links: uniqueLinks.slice(0, 180),
    frames: framePages,
    metrics: pageMetrics()
  };
}

function pageMetrics() {
  return {
    url: location.href,
    title: document.title || "",
    textLength: visibleText(document.body).length,
    linkCount: queryAllDeep("a[href]").length,
    controlCount: queryAllDeep("button, a[href], [role='button'], [role='link']").length,
    scrollY: window.scrollY || 0,
    scrollHeight: Math.max(document.documentElement.scrollHeight || 0, document.body?.scrollHeight || 0),
    viewportHeight: window.innerHeight || 0,
    viewportWidth: window.innerWidth || 0,
    devicePixelRatio: window.devicePixelRatio || 1
  };
}

function pageSignature() {
  const metrics = pageMetrics();
  return `${metrics.url}|${metrics.textLength}|${metrics.linkCount}|${metrics.scrollHeight}`;
}

function visibleCandidate(el) {
  if (!el || typeof el.getBoundingClientRect !== "function") return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
}

function findPaginationControl(preferred) {
  if (preferred) {
    const preferredEl = target(preferred);
    if (preferredEl && visibleCandidate(preferredEl)) return { label: preferred, kind: "preferred" };
  }
  const labels = [
    "load more",
    "show more",
    "more",
    "next",
    "next page",
    "older",
    "ดูเพิ่มเติม",
    "โหลดเพิ่ม",
    "ถัดไป",
    "หน้าถัดไป",
    "เพิ่มเติม"
  ];
  const controls = queryAllDeep("button, a[href], [role='button'], [role='link']").filter(visibleCandidate);
  for (const label of labels) {
    const exact = controls.find((el) => {
      const text = [
        el.textContent,
        el.getAttribute("aria-label"),
        el.getAttribute("title"),
        el.getAttribute("rel")
      ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim().toLowerCase();
      return text === label || text.includes(label);
    });
    if (exact) {
      const text = String(exact.textContent || exact.getAttribute("aria-label") || exact.getAttribute("title") || label)
        .replace(/\s+/g, " ")
        .trim();
      return { label: text || label, kind: label.includes("more") || label.includes("เพิ่ม") ? "load_more" : "next" };
    }
  }
  return null;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const task = message && message.task;
  if (!task) return false;
  (async () => {
    try {
    if (task.action === "inspect_form") {
      await flashActivity("Inspecting form fields...");
      sendResponse({ status: "completed", summary: "Inspected form fields", fields: inspectForm() });
      return;
    }
    if (task.action === "page_metrics") {
      sendResponse({ status: "completed", summary: "Collected page metrics", metrics: pageMetrics(), signature: pageSignature() });
      return;
    }
    if (task.action === "find_pagination_control") {
      const control = findPaginationControl(task.selector_or_label || "");
      sendResponse({
        status: control ? "completed" : "not_found",
        summary: control ? `Found pagination control: ${control.label}` : "No pagination control found",
        control
      });
      return;
    }
    if (task.action === "extract_page") {
      if (!task.silent) await visiblePageScan();
      sendResponse({ status: "completed", summary: "Extracted page text and links", page: extractPage() });
      return;
    }
    if (task.action === "fill") {
      const rawFields = Array.isArray(task.fields)
        ? task.fields.map((field) => [
            field && (field.selector || field.name || field.label || field.selector_or_label),
            field && field.value
          ])
        : Object.entries(task.fields || {});
      const filled = [];
      const missing = [];
      for (const [key, value] of rawFields) {
        if (!key) continue;
        const el = await waitForTarget(key);
        if (el) {
          await highlightElement(el, `Filling ${key}`);
          setValue(el, String(value));
          await sleep(220);
          filled.push(String(key));
        } else {
          missing.push(String(key));
        }
      }
      if (task.selector_or_label) {
        const el = await waitForTarget(task.selector_or_label);
        if (el) {
          await highlightElement(el, `Filling ${task.selector_or_label}`);
          setValue(el, String(task.value || ""));
          filled.push(String(task.selector_or_label));
        } else {
          missing.push(String(task.selector_or_label));
        }
      }
      sendResponse({
        status: "completed",
        summary: filled.length ? "Filled browser form fields" : "No matching browser form fields found",
        filled,
        missing
      });
      return;
    }
    if (task.action === "select") {
      const el = await waitForTarget(task.selector_or_label);
      if (!el) throw new Error("Select target not found");
      await highlightElement(el, `Selecting ${task.selector_or_label}`);
      setValue(el, String(task.value || ""));
      sendResponse({ status: "completed", summary: "Selected browser field value" });
      return;
    }
    if (task.action === "wait") {
      const timeout = Number.parseInt(String(task.value || "8000"), 10);
      if (!task.selector_or_label) {
        const ms = Number.isFinite(timeout) ? (timeout < 1000 ? timeout * 1000 : timeout) : 8000;
        await sleep(Math.max(250, Math.min(ms, 30000)));
        sendResponse({ status: "completed", summary: "Waited without a target selector" });
        return;
      }
      const el = await waitForTarget(task.selector_or_label, Number.isFinite(timeout) ? timeout : 8000);
      if (!el) throw new Error("Wait target not found");
      await highlightElement(el, `Found ${task.selector_or_label}`);
      sendResponse({ status: "completed", summary: `Wait target found: ${task.selector_or_label}` });
      return;
    }
    if (task.action === "scroll") {
      const raw = String(task.value || task.selector_or_label || "down").trim().toLowerCase();
      if (raw === "top") {
        window.scrollTo({ top: 0, behavior: "smooth" });
      } else if (raw === "bottom") {
        window.scrollTo({ top: Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0), behavior: "smooth" });
      } else if (raw === "up") {
        window.scrollBy({ top: -Math.round(window.innerHeight * 0.8), behavior: "smooth" });
      } else if (/^-?\d+$/.test(raw)) {
        window.scrollBy({ top: Number.parseInt(raw, 10), behavior: "smooth" });
      } else {
        window.scrollBy({ top: Math.round(window.innerHeight * 0.8), behavior: "smooth" });
      }
      await flashActivity(`Scrolled ${raw}`);
      sendResponse({ status: "completed", summary: `Scrolled ${raw}`, scrollY: window.scrollY });
      return;
    }
    if (task.action === "scroll_to") {
      const top = Math.max(0, Number.parseInt(String(task.value || "0"), 10) || 0);
      window.scrollTo({ top, behavior: task.smooth === false ? "auto" : "smooth" });
      await sleep(task.smooth === false ? 120 : 420);
      sendResponse({ status: "completed", summary: `Scrolled to ${top}`, metrics: pageMetrics() });
      return;
    }
    if (task.action === "click" || task.action === "submit") {
      const el = await waitForTarget(task.selector_or_label) || findByLabel(task.action === "submit" ? "submit" : "");
      if (!el) throw new Error("Click target not found");
      await highlightElement(el, task.action === "submit" ? "Submitting" : `Clicking ${task.selector_or_label || "target"}`);
      sendResponse({ status: "completed", summary: `${task.action} click completed` });
      setTimeout(() => el.click(), 50);
      return;
    }
    sendResponse({ status: "failed", error: `Unsupported action: ${task.action}` });
    } catch (error) {
      sendResponse({ status: "failed", error: String(error && error.message ? error.message : error) });
    }
  })();
  return true;
});
