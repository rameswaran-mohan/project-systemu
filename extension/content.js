// Helper mapping for PII redacting
const SENSITIVE_INPUT_TYPES = ["password", "email", "tel", "hidden"];

// Function to generate a robust XPath for any DOM element
function getXPath(element) {
  if (element.id !== '')
    return `id("${element.id}")`;
  
  if (element === document.body)
    return element.tagName.toLowerCase();

  let ix = 0;
  const siblings = element.parentNode.childNodes;
  for (let i = 0; i < siblings.length; i++) {
    const sibling = siblings[i];
    if (sibling === element) {
      return getXPath(element.parentNode) + '/' + element.tagName.toLowerCase() + '[' + (ix + 1) + ']';
    }
    if (sibling.nodeType === 1 && sibling.tagName === element.tagName) {
      ix++;
    }
  }
}

// Function to safely extract contextual text for the element
function getElementText(element) {
  // If it's an input field, get placeholder, name or label
  if (element.tagName === "INPUT" || element.tagName === "TEXTAREA" || element.tagName === "SELECT") {
    let name = element.getAttribute("name") || element.getAttribute("placeholder") || element.getAttribute("aria-label");
    return name ? name.trim() : "";
  }
  
  // Otherwise try innerText
  if (element.innerText) {
    let text = element.innerText.trim();
    // Truncate to avoid sending massive payloads for large divs
    return text.length > 50 ? text.substring(0, 50) + "..." : text;
  }
  
  // Final fallback (e.g., SVG icons might have an aria-label)
  return element.getAttribute("aria-label") || "";
}

// Global click hook
document.addEventListener("mousedown", (event) => {
  const target = event.target;
  
  // We only care about interactive elements or things that look like them
  let clickable = target.closest("a, button, input, select, textarea, [role='button'], [tabindex]");
  if (!clickable) {
      // If user clicked a generic div/span, see if we can still extract some text context
      clickable = target; 
  }

  const tagName = clickable.tagName.toLowerCase();
  let value = clickable.value || "";

  // Privacy Redaction Before it leaves the browser!
  if (tagName === "input" && SENSITIVE_INPUT_TYPES.includes(clickable.type)) {
      value = "[REDACTED]";
  }

  const payload = {
    url: window.location.href,
    action: "mouse_click",
    element_tag: tagName,
    element_type: clickable.type || "unknown",
    element_text: getElementText(clickable),
    element_xpath: getXPath(clickable),
    value: value
  };

  // Send to background.js
  chrome.runtime.sendMessage({
    type: "DOM_EVENT",
    payload: payload
  });
}, true); // Use capture phase to ensure we intercept before preventDefault() is called

// Capture input submissions (when a user presses enter or field loses focus)
document.addEventListener("change", (event) => {
  const target = event.target;
  if (!target || !target.tagName) return;

  const tagName = target.tagName.toLowerCase();
  if (tagName !== "input" && tagName !== "textarea" && tagName !== "select") return;

  let value = target.value || "";
  
  if (tagName === "input" && SENSITIVE_INPUT_TYPES.includes(target.type)) {
    value = "[REDACTED]";
  }

  const payload = {
    url: window.location.href,
    action: "input_change",
    element_tag: tagName,
    element_type: target.type || "unknown",
    element_text: getElementText(target),
    element_xpath: getXPath(target),
    value: value
  };

  chrome.runtime.sendMessage({
    type: "DOM_EVENT",
    payload: payload
  });
}, true);
