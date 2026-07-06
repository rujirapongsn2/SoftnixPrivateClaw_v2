const POLL_ALARM_NAME = "softnix-browser-poll";
const POLL_ALARM_MINUTES = 0.5;

let polling = false;

function schedulePollAlarm() {
  if (!chrome.alarms || !chrome.alarms.create) return;
  chrome.alarms.create(POLL_ALARM_NAME, { periodInMinutes: POLL_ALARM_MINUTES });
}

async function getState() {
  return chrome.storage.local.get(["apiBase", "instanceId", "extensionId", "extensionToken"]);
}

async function postResult(state, taskId, result) {
  await fetch(`${state.apiBase}/api/browser-extension/tasks/result`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      instance_id: state.instanceId,
      extension_id: state.extensionId,
      extension_token: state.extensionToken,
      task_id: taskId,
      result
    })
  });
}

async function getTabSessions() {
  const state = await chrome.storage.local.get(["tabSessions"]);
  return state.tabSessions && typeof state.tabSessions === "object" ? state.tabSessions : {};
}

async function rememberTabSession(key, tab) {
  if (!key || !tab || !tab.id) return;
  const sessions = await getTabSessions();
  sessions[String(key)] = { tabId: tab.id, windowId: tab.windowId || null, url: tabUrl(tab), updatedAt: Date.now() };
  await chrome.storage.local.set({ tabSessions: sessions });
}

async function forgetTabSession(key) {
  if (!key) return;
  const sessions = await getTabSessions();
  if (sessions[String(key)]) {
    delete sessions[String(key)];
    await chrome.storage.local.set({ tabSessions: sessions });
  }
}

function httpUrl(rawUrl) {
  try {
    const parsed = new URL(rawUrl || "");
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    return parsed;
  } catch (_) {
    return null;
  }
}

function tabUrl(tab) {
  return tab && (tab.url || tab.pendingUrl || "");
}

function sameOrigin(tab, target) {
  const current = httpUrl(tabUrl(tab));
  return Boolean(current && target && canonicalOrigin(current) === canonicalOrigin(target));
}

function sameHref(tab, target) {
  const current = httpUrl(tabUrl(tab));
  return Boolean(current && target && current.href === target.href);
}

async function navigateIfNeeded(tab, targetUrl) {
  const target = httpUrl(targetUrl || "");
  if (!tab || !tab.id || !target) return tab;
  if (sameHref(tab, target)) return tab;
  const updated = await chrome.tabs.update(tab.id, { url: target.href, active: true });
  return waitForTabComplete(updated.id, 15000);
}

function canonicalOrigin(parsed) {
  if (!parsed) return "";
  const host = String(parsed.hostname || "").toLowerCase();
  if (host === "gmail.com" || host.endsWith(".gmail.com") || host === "mail.google.com") {
    return "https://mail.google.com";
  }
  return parsed.origin;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function focusTab(tab) {
  if (!tab || !tab.id) return tab;
  if (tab.windowId) {
    await chrome.windows.update(tab.windowId, { focused: true });
  }
  return chrome.tabs.update(tab.id, { active: true });
}

async function waitForHttpTab(tabId, targetUrl = "") {
  const target = httpUrl(targetUrl);
  let latest = await chrome.tabs.get(tabId);
  for (let i = 0; i < 40; i += 1) {
    const current = httpUrl(tabUrl(latest));
    const targetMatches = !target || (current && canonicalOrigin(current) === canonicalOrigin(target));
    if (current && targetMatches && latest.status === "complete") return latest;
    await new Promise((resolve) => setTimeout(resolve, 250));
    latest = await chrome.tabs.get(tabId);
  }
  return latest;
}

async function waitForTabComplete(tabId, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  let latest = await chrome.tabs.get(tabId);
  while (Date.now() < deadline) {
    latest = await chrome.tabs.get(tabId);
    if (latest.status === "complete" && httpUrl(tabUrl(latest))) return latest;
    await sleep(250);
  }
  return latest;
}

async function findExistingTab(url) {
  const target = httpUrl(url);
  if (!target) return null;
  const tabs = await chrome.tabs.query({});
  const matching = tabs.filter((tab) => sameOrigin(tab, target));
  if (!matching.length) return null;
  const activeCurrentWindow = matching.find((tab) => tab.active && tab.highlighted);
  if (activeCurrentWindow) return activeCurrentWindow;
  const currentWindow = matching.find((tab) => tab.windowId);
  return currentWindow || matching[0];
}

async function tabFromSession(sessionId) {
  if (!sessionId) return null;
  const sessions = await getTabSessions();
  const item = sessions[String(sessionId)];
  if (!item || !item.tabId) return null;
  try {
    const tab = await chrome.tabs.get(item.tabId);
    if (httpUrl(tabUrl(tab))) return tab;
  } catch (_) {
    await forgetTabSession(sessionId);
  }
  return null;
}

async function activeOrNewTab(url, task = {}) {
  const sessionTab = await tabFromSession(task.browser_session_id || "");
  if (sessionTab) {
    const target = httpUrl(url || "");
    const current = httpUrl(tabUrl(sessionTab));
    if (!target || !current || canonicalOrigin(current) === canonicalOrigin(target)) {
      const focused = await focusTab(sessionTab);
      const navigated = await navigateIfNeeded(focused, url || "");
      return waitForHttpTab(navigated.id, url || "");
    }
  }
  if (url) {
    const existing = await findExistingTab(url);
    if (existing) {
      const focused = await focusTab(existing);
      const navigated = await navigateIfNeeded(focused, url);
      const tab = await waitForHttpTab(navigated.id, url);
      tab.softnixReusedExistingTab = true;
      return tab;
    }
    let tab = null;
    try {
      tab = await chrome.tabs.create({ url, active: true });
    } catch (_) {
      const createdWindow = await chrome.windows.create({ url, focused: true, type: "normal" });
      tab = Array.isArray(createdWindow.tabs) && createdWindow.tabs.length ? createdWindow.tabs[0] : null;
    }
    if (!tab || !tab.id) {
      const createdWindow = await chrome.windows.create({ url, focused: true, type: "normal" });
      tab = Array.isArray(createdWindow.tabs) && createdWindow.tabs.length ? createdWindow.tabs[0] : null;
    }
    if (!tab || !tab.id) throw new Error("Unable to open browser window");
    const loaded = await waitForHttpTab(tab.id, url);
    loaded.softnixReusedExistingTab = false;
    return loaded;
  }
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tabs[0]) return waitForHttpTab(tabs[0].id);
  const activeTabs = await chrome.tabs.query({ active: true });
  return activeTabs[0] ? waitForHttpTab(activeTabs[0].id) : null;
}

function originPattern(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "";
    return `${parsed.origin}/*`;
  } catch (_) {
    return "";
  }
}

async function ensureOriginPermission(tab) {
  const pattern = originPattern(tab && tab.url);
  if (!pattern) throw new Error("Browser automation only supports http(s) pages");
  return pattern;
}

async function sendTaskToTab(tab, task) {
  await ensureOriginPermission(tab);
  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["content_script.js"]
  });
  return chrome.tabs.sendMessage(tab.id, { type: "softnix-browser-task", task });
}

function compactPage(page, maxTextChars = 6000) {
  if (!page || typeof page !== "object") return page;
  return {
    ...page,
    text: String(page.text || "").slice(0, maxTextChars),
    links: Array.isArray(page.links) ? page.links.slice(0, 120) : [],
    frames: Array.isArray(page.frames) ? page.frames.slice(0, 10).map((frame) => ({
      ...frame,
      text: String(frame.text || "").slice(0, 3000),
      links: Array.isArray(frame.links) ? frame.links.slice(0, 40) : []
    })) : []
  };
}

async function collectPages(tab, task) {
  const maxPages = Math.max(1, Math.min(20, Number.parseInt(String(task.max_pages || "5"), 10) || 5));
  const maxItems = Math.max(1, Math.min(1000, Number.parseInt(String(task.max_items || "200"), 10) || 200));
  const pages = [];
  const seenUrls = new Set();
  const seenLinks = new Set();
  const combinedLinks = [];
  let stopReason = "max_pages";

  for (let index = 0; index < maxPages; index += 1) {
    await waitForTabComplete(tab.id, 10000);
    const extracted = await sendTaskToTab(tab, { ...task, action: "extract_page", silent: index > 0 });
    const page = compactPage(extracted && extracted.page ? extracted.page : {});
    const url = page.url || tabUrl(await chrome.tabs.get(tab.id));
    pages.push({ index: index + 1, ...page, url });
    seenUrls.add(url);
    for (const link of page.links || []) {
      const key = `${link.href}|${link.text}`;
      if (!seenLinks.has(key)) {
        seenLinks.add(key);
        combinedLinks.push(link);
      }
      if (combinedLinks.length >= maxItems) break;
    }
    if (index === maxPages - 1) break;

    const before = await sendTaskToTab(tab, { action: "page_metrics" });
    const controlResult = await sendTaskToTab(tab, {
      action: "find_pagination_control",
      selector_or_label: task.selector_or_label || ""
    });
    const control = controlResult && controlResult.control;
    if (control && control.label) {
      await sendTaskToTab(tab, { action: "click", selector_or_label: control.label });
      await sleep(1200);
      await waitForTabComplete(tab.id, 10000);
      const latest = await chrome.tabs.get(tab.id);
      const after = await sendTaskToTab(latest, { action: "page_metrics" });
      const changed = (after && after.signature) !== (before && before.signature);
      const latestUrl = tabUrl(latest);
      if (!changed && seenUrls.has(latestUrl)) {
        stopReason = "no_change_after_click";
        break;
      }
      tab = latest;
      continue;
    }

    await sendTaskToTab(tab, { action: "scroll", value: "bottom" });
    await sleep(1400);
    const afterScroll = await sendTaskToTab(tab, { action: "page_metrics" });
    if ((afterScroll && afterScroll.signature) === (before && before.signature)) {
      stopReason = "no_more_pages";
      break;
    }
    stopReason = "infinite_scroll";
  }

  return {
    status: "completed",
    summary: `Collected ${pages.length} page step(s) via browser pagination`,
    pages,
    links: combinedLinks.slice(0, maxItems),
    page_count: pages.length,
    link_count: combinedLinks.length,
    stop_reason: stopReason,
    url: pages[pages.length - 1]?.url || tabUrl(tab)
  };
}

async function captureFullPageScreenshot(tab, task) {
  const metricsResult = await sendTaskToTab(tab, { action: "page_metrics" });
  const metrics = metricsResult.metrics || {};
  const viewportHeight = Math.max(1, Number(metrics.viewportHeight || 800));
  const viewportWidth = Math.max(1, Number(metrics.viewportWidth || 1280));
  const scrollHeight = Math.max(viewportHeight, Number(metrics.scrollHeight || viewportHeight));
  const maxSlices = Math.max(1, Math.min(12, Number.parseInt(String(task.max_pages || "8"), 10) || 8));
  const sliceCount = Math.min(maxSlices, Math.ceil(scrollHeight / viewportHeight));
  const captures = [];

  for (let i = 0; i < sliceCount; i += 1) {
    const top = Math.min(i * viewportHeight, Math.max(0, scrollHeight - viewportHeight));
    await sendTaskToTab(tab, { action: "scroll_to", value: String(top), smooth: false });
    await sleep(250);
    captures.push({
      top,
      dataUrl: await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" })
    });
  }
  await sendTaskToTab(tab, { action: "scroll_to", value: String(metrics.scrollY || 0), smooth: false });

  const bitmaps = [];
  for (const capture of captures) {
    const blob = await (await fetch(capture.dataUrl)).blob();
    bitmaps.push({ top: capture.top, bitmap: await createImageBitmap(blob) });
  }
  const canvas = new OffscreenCanvas(viewportWidth, Math.min(scrollHeight, viewportHeight * sliceCount));
  const context = canvas.getContext("2d");
  for (const item of bitmaps) {
    context.drawImage(item.bitmap, 0, item.top);
    item.bitmap.close();
  }
  const blob = await canvas.convertToBlob({ type: "image/png" });
  const dataUrl = await blobToDataUrl(blob);
  return {
    status: "completed",
    summary: `Captured full-page screenshot (${sliceCount} viewport slice(s))`,
    screenshot: dataUrl,
    screenshot_mode: "full",
    slice_count: sliceCount,
    metrics,
    url: tabUrl(tab),
    title: tab.title || "",
    captured_at: new Date().toISOString()
  };
}

async function blobToDataUrl(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return `data:${blob.type || "image/png"};base64,${btoa(binary)}`;
}

async function closeTrackedTab(task) {
  // No browser_session_id → fall back to whichever tab this extension last
  // acted on, so a plain "close browser" after a few open/extract calls still
  // closes the right tab without the agent having to track a session id.
  const bySession = await tabFromSession(task.browser_session_id || "");
  const tab = bySession || (await tabFromSession("_last"));
  if (!tab || !tab.id) {
    return { status: "completed", summary: "No open browser tab to close." };
  }
  try {
    await chrome.tabs.remove(tab.id);
  } catch (_) {
    // Already closed (e.g. by the user) — that's the desired end state either way.
  }
  await forgetTabSession(task.browser_session_id || "");
  await forgetTabSession("_last");
  return { status: "completed", summary: "Closed the browser tab.", closed_tab_id: tab.id };
}

async function runTask(task) {
  if (task.action === "close") {
    return closeTrackedTab(task);
  }
  const tab = await activeOrNewTab(task.url || "", task);
  if (!tab || !tab.id) throw new Error("No active browser tab available");
  await rememberTabSession(task.browser_session_id || "", tab);
  await rememberTabSession(task.task_id || "", tab);
  await rememberTabSession("_last", tab);
  if (task.action === "open") {
    return {
      status: "completed",
      summary: `Opened ${task.url || tabUrl(tab)}`,
      url: tabUrl(tab),
      reused_existing_tab: Boolean(tab.softnixReusedExistingTab)
    };
  }
  const permittedOrigin = await ensureOriginPermission(tab);
  if (task.action === "screenshot") {
    if (String(task.value || "").toLowerCase() === "full") {
      return { ...(await captureFullPageScreenshot(tab, task)), permitted_origin: permittedOrigin };
    }
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    return {
      status: "completed",
      summary: "Captured visible tab screenshot",
      screenshot: dataUrl,
      url: tabUrl(tab),
      title: tab.title || "",
      captured_at: new Date().toISOString(),
      permitted_origin: permittedOrigin
    };
  }
  if (task.action === "collect_pages") {
    return { ...(await collectPages(tab, task)), permitted_origin: permittedOrigin };
  }
  const normalizedTask = task.action === "text" || task.action === "snapshot"
    ? { ...task, action: "extract_page" }
    : task;
  const response = await sendTaskToTab(tab, normalizedTask);
  return { ...(response || { status: "completed", summary: "Task completed" }), permitted_origin: permittedOrigin };
}

async function pollOnce() {
  const state = await getState();
  if (!state.apiBase || !state.instanceId || !state.extensionId || !state.extensionToken) return;
  const response = await fetch(`${state.apiBase}/api/browser-extension/tasks/poll`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      instance_id: state.instanceId,
      extension_id: state.extensionId,
      extension_token: state.extensionToken
    })
  });
  if (!response.ok) return;
  const payload = await response.json();
  const task = payload.task;
  if (!task || !task.task_id) return;
  try {
    const result = await runTask(task);
    await postResult(state, task.task_id, result);
  } catch (error) {
    await postResult(state, task.task_id, {
      status: "failed",
      error: String(error && error.message ? error.message : error),
      permission_required: error && error.permission_required ? String(error.permission_required) : ""
    });
  }
}

async function pollLoop() {
  if (polling) return;
  polling = true;
  while (polling) {
    await pollOnce();
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
}

chrome.runtime.onInstalled.addListener(() => {
  schedulePollAlarm();
  void pollLoop();
});
chrome.runtime.onStartup.addListener(() => {
  schedulePollAlarm();
  void pollLoop();
});
chrome.runtime.onMessage.addListener((message) => {
  if (message && message.type === "softnix-paired") void pollLoop();
});
if (chrome.alarms && chrome.alarms.onAlarm) {
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm && alarm.name === POLL_ALARM_NAME) void pollLoop();
  });
}

schedulePollAlarm();
void pollLoop();
