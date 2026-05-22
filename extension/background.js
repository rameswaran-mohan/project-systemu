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
