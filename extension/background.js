// Connect to the Python Orchestrator via Local HTTP
const LOCAL_SERVER_URL = "http://localhost:49494/event";

// Listen for messages from injected content.js scripts
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "DOM_EVENT") {
    // Inject Chrome tab metadata
    const payload = {
      ...message.payload,
      tab_id: sender.tab ? sender.tab.id : null,
      tab_title: sender.tab ? sender.tab.title : "Unknown",
    };

    // Forward to Python Orchestrator via HTTP POST
    fetch(LOCAL_SERVER_URL, {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify(payload)
    }).catch(err => {
        // Silently ignore if sharing_on isn't currently recording
        // console.debug("sharing_on not recording/reachable:", err);
    });
  }
});

// ── U-1b: "Send to systemu" ─────────────────────────────────────────────────
// A context-menu item that POSTs the current page/selection to the U-1a task
// API. The page's own text is NEVER composed into an instruction here — it is
// sent as a structured `source_page` field and fenced SERVER-side, so a page
// containing "ignore previous instructions" cannot become one.
//
// Note the deliberate contrast with the capture path above: that fetch swallows
// every error because systemu may simply not be recording. This one must NOT —
// the operator explicitly asked for something, so a failure is always surfaced.

const SYSTEMU_MENU_ID = "systemu_send";
const SYSTEMU_DEFAULT_ENDPOINT = "http://localhost:8080";

function systemuSettings() {
  return new Promise((resolve) => {
    chrome.storage.local.get(["systemu_endpoint", "systemu_token"], (v) =>
      resolve({
        endpoint: (v && v.systemu_endpoint) || SYSTEMU_DEFAULT_ENDPOINT,
        token: (v && v.systemu_token) || "",
      })
    );
  });
}

function systemuNotify(badge, message) {
  // Surfaced through the toolbar badge rather than a notification, so U-1b adds
  // no permission beyond contextMenus + storage.
  try {
    chrome.action.setBadgeText({ text: badge });
    chrome.action.setTitle({ title: message });
    setTimeout(() => {
      try { chrome.action.setBadgeText({ text: "" }); } catch (e) {}
    }, 4000);
  } catch (e) {
    console.warn("systemu:", badge, message);
  }
}

chrome.runtime.onInstalled.addListener(() => {
  try {
    chrome.contextMenus.removeAll(() => {
      chrome.contextMenus.create({
        id: SYSTEMU_MENU_ID,
        title: "Send to systemu",
        contexts: ["selection", "page"],
      });
    });
  } catch (e) {
    console.warn("systemu: context menu unavailable", e);
  }
});

if (chrome.contextMenus && chrome.contextMenus.onClicked) {
  chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    if (info.menuItemId !== SYSTEMU_MENU_ID) return;

    const { endpoint, token } = await systemuSettings();
    if (!token) {
      // NEVER a silent failure — the operator is told, and taken to options.
      systemuNotify("!", "systemu: no API token set. Opening options…");
      try { chrome.runtime.openOptionsPage(); } catch (e) {}
      return;
    }

    const body = {
      prompt: info.selectionText
        ? "Handle the selected text from this page."
        : "Handle this page.",
      source_page: {
        url: (tab && tab.url) || info.pageUrl || "",
        title: (tab && tab.title) || "",
        selection: info.selectionText || "",
      },
    };

    try {
      const res = await fetch(endpoint.replace(/\/+$/, "") + "/api/tasks", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          Authorization: "Bearer " + token,
        },
        body: JSON.stringify(body),
      });
      if (res.status === 401) {
        systemuNotify("!", "systemu: token rejected. Check the extension options.");
        return;
      }
      if (res.status === 429) {
        systemuNotify("!", "systemu: rate limited — try again in a minute.");
        return;
      }
      if (!res.ok) {
        const t = await res.text();
        systemuNotify("!", "systemu: submit failed (" + res.status + ") " + t.slice(0, 120));
        return;
      }
      systemuNotify("OK", "systemu: task submitted.");
    } catch (err) {
      systemuNotify("!", "systemu: could not reach " + endpoint + " — " + err);
    }
  });
}
