// Options for "Send to systemu" (U-1b). Endpoint + API token live in
// chrome.storage.local — this browser profile only, never synced.

const $ = (id) => document.getElementById(id);

function load() {
  chrome.storage.local.get(["systemu_endpoint", "systemu_token"], (v) => {
    $("endpoint").value = (v && v.systemu_endpoint) || "http://localhost:8080";
    $("token").value = (v && v.systemu_token) || "";
  });
}

function save() {
  const endpoint = ($("endpoint").value || "").trim().replace(/\/+$/, "");
  const token = ($("token").value || "").trim();
  chrome.storage.local.set(
    { systemu_endpoint: endpoint, systemu_token: token },
    () => {
      $("status").textContent = "Saved";
      setTimeout(() => ($("status").textContent = ""), 2000);
    }
  );
}

document.addEventListener("DOMContentLoaded", load);
$("save").addEventListener("click", save);
