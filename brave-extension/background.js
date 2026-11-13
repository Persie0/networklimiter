"use strict";

// In-memory state is scoped to this browser session; tab IDs are not stable
// across restarts. Debugger permission is deliberately visible to the user.
const active = new Map();
const errors = new Map();
const DEFAULTS = { autoNewTabs: false, downloadKib: 100 };

function isPage(tab) {
  return Boolean(tab && /^https?:\/\//.test(tab.url || ""));
}
function validKib(value) {
  return Number.isInteger(value) && value >= 1 && value <= 1_000_000;
}
function message(error) {
  return String(error?.message || error);
}

async function settings() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(DEFAULTS)) };
}

async function throttle(tabId, downloadKib) {
  if (!validKib(downloadKib)) throw new Error("Rate must be 1–1,000,000 KiB/s");
  const tab = await chrome.tabs.get(tabId);
  if (!isPage(tab)) throw new Error("Only HTTP/HTTPS tabs are supported");
  const debuggee = { tabId };
  const wasAttached = active.has(tabId);
  try {
    if (!wasAttached) await chrome.debugger.attach(debuggee, "1.3");
    await chrome.debugger.sendCommand(debuggee, "Network.enable");
    const bytes = downloadKib * 1024;
    // Newer Chromium: tab-targeted throttling, without overriding navigator state.
    try {
      await chrome.debugger.sendCommand(debuggee, "Network.emulateNetworkConditionsByRule", {
        matchedNetworkConditions: [{
          urlPattern: "", latency: 0, downloadThroughput: bytes,
          uploadThroughput: -1,
        }],
      });
    } catch (_unsupported) {
      // Compatible with older Brave/Chromium builds.
      await chrome.debugger.sendCommand(debuggee, "Network.emulateNetworkConditions", {
        offline: false, latency: 0, downloadThroughput: bytes,
        uploadThroughput: -1,
      });
    }
    active.set(tabId, downloadKib);
    errors.delete(tabId);
  } catch (error) {
    errors.set(tabId, message(error));
    if (!wasAttached) {
      try { await chrome.debugger.detach(debuggee); } catch (_) { /* unattached */ }
    }
    throw error;
  }
}

async function unthrottle(tabId) {
  try {
    if (active.has(tabId)) {
      await chrome.debugger.detach({ tabId });
    }
  } catch (_) {
    // The browser may already have detached us (e.g., DevTools opened).
  } finally {
    active.delete(tabId);
    errors.delete(tabId);
  }
}

chrome.debugger.onDetach.addListener((target, reason) => {
  if (target.tabId !== undefined) {
    active.delete(target.tabId);
    errors.set(target.tabId, "Debugger detached: " + reason);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  active.delete(tabId);
  errors.delete(tabId);
});

async function applyAuto(tabId) {
  try {
    const config = await settings();
    if (config.autoNewTabs && !active.has(tabId)) {
      await throttle(tabId, config.downloadKib);
    }
  } catch (error) {
    errors.set(tabId, message(error));
  }
}

chrome.tabs.onUpdated.addListener((tabId, change, tab) => {
  if (change.status === "complete" && isPage(tab)) {
    void applyAuto(tabId);
  }
});

chrome.runtime.onStartup.addListener(async () => {
  const config = await settings();
  if (!config.autoNewTabs) return;
  for (const tab of await chrome.tabs.query({})) {
    if (isPage(tab)) void applyAuto(tab.id);
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
  (async () => {
    switch (msg.op) {
      case "list":
        return { active: Object.fromEntries(active), errors: Object.fromEntries(errors),
                 settings: await settings() };
      case "set":
        await throttle(msg.tabId, msg.downloadKib);
        return { applied: true };
      case "clear":
        await unthrottle(msg.tabId);
        return { applied: false };
      case "config":
        if (typeof msg.autoNewTabs !== "boolean" || !validKib(msg.downloadKib))
          throw new Error("Invalid settings");
        await chrome.storage.local.set({
          autoNewTabs: msg.autoNewTabs, downloadKib: msg.downloadKib,
        });
        return { saved: true };
      case "all":
        const config = await settings();
        const result = { succeeded: 0, failed: 0 };
        for (const tab of await chrome.tabs.query({})) {
          if (!isPage(tab)) continue;
          try {
            await throttle(tab.id, config.downloadKib);
            result.succeeded++;
          } catch (_) {
            result.failed++;
          }
        }
        return result;
      default:
        throw new Error("Unknown command");
    }
  })().then(
    (result) => respond({ ok: true, result }),
    (error) => respond({ ok: false, error: message(error) })
  );
  return true;
});
