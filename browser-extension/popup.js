const pairingDetails = document.getElementById("pairingDetails");
const apiBase = document.getElementById("apiBase");
const instanceId = document.getElementById("instanceId");
const ticket = document.getElementById("ticket");
const statusEl = document.getElementById("status");

async function loadState() {
  const stored = await chrome.storage.local.get(["apiBase", "instanceId", "extensionId"]);
  apiBase.value = stored.apiBase || "";
  instanceId.value = stored.instanceId || "";
  statusEl.textContent = stored.extensionId ? `Paired: ${stored.extensionId}` : "Not paired";
}

function parsePairingDetails(rawText) {
  const text = String(rawText || "");
  const pairs = {};
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*([^:]+):\s*(.+?)\s*$/);
    if (!match) continue;
    pairs[match[1].trim().toLowerCase()] = match[2].trim();
  }
  return {
    apiBase: pairs["admin api"] || "",
    instanceId: pairs.instance || "",
    ticket: pairs.ticket || "",
  };
}

function applyPairingDetails(rawText) {
  const parsed = parsePairingDetails(rawText);
  if (parsed.apiBase) apiBase.value = parsed.apiBase;
  if (parsed.instanceId) instanceId.value = parsed.instanceId;
  if (parsed.ticket) ticket.value = parsed.ticket;
  const complete = parsed.apiBase && parsed.instanceId && parsed.ticket;
  statusEl.textContent = complete ? "Pairing details ready. Click Pair Browser." : "Could not find all pairing details.";
  return complete;
}

document.getElementById("parse-details").addEventListener("click", () => {
  applyPairingDetails(pairingDetails.value);
});

pairingDetails.addEventListener("input", () => {
  applyPairingDetails(pairingDetails.value);
});

document.getElementById("pair").addEventListener("click", async () => {
  const base = apiBase.value.trim().replace(/\/+$/, "");
  const instance = instanceId.value.trim();
  const pairingTicket = ticket.value.trim();
  if (!base || !instance || !pairingTicket) {
    statusEl.textContent = "Base URL, instance ID, and ticket are required.";
    return;
  }
  statusEl.textContent = "Pairing...";
  const response = await fetch(`${base}/api/browser-extension/pairing/complete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instance_id: instance, pairing_ticket: pairingTicket, label: "Chrome Extension" })
  });
  const payload = await response.json();
  if (!response.ok) {
    statusEl.textContent = payload.error || "Pairing failed.";
    return;
  }
  await chrome.storage.local.set({
    apiBase: base,
    instanceId: payload.instance_id,
    extensionId: payload.extension_id,
    extensionToken: payload.extension_token
  });
  statusEl.textContent = `Paired: ${payload.extension_id}`;
  chrome.runtime.sendMessage({ type: "softnix-paired" });
});

document.getElementById("grant-site")?.addEventListener("click", async () => {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  if (!tab || !tab.url) {
    statusEl.textContent = "No active tab found.";
    return;
  }
  let origin = "";
  try {
    const parsed = new URL(tab.url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("unsupported");
    origin = `${parsed.origin}/*`;
  } catch (_) {
    statusEl.textContent = "Current tab is not an http(s) website.";
    return;
  }
  const granted = await chrome.permissions.request({ origins: [origin] });
  statusEl.textContent = granted ? `Granted ${origin}` : `Permission not granted for ${origin}`;
});

void loadState();
