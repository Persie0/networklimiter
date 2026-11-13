"use strict";
const $ = (id) => document.getElementById(id);
const send = async (msg) => {
  const response = await chrome.runtime.sendMessage(msg);
  if (!response?.ok) throw new Error(response?.error || "Extension not responding");
  return response.result;
};
function val(input) {
  const num = Number(input.value);
  if (!Number.isInteger(num) || num < 1 || num > 1_000_000)
    throw new Error("Enter a number between 1 and 1,000,000 KiB/s");
  return num;
}
function notify(value, error = false) {
  $("notice").textContent = value;
  $("notice").style.color = error ? "#ffabab" : "#7fe6c6";
}
async function refresh() {
  const data = await send({ op: "list" });
  $("default").value = data.settings.downloadKib;
  $("auto").checked = data.settings.autoNewTabs;
  const items = await chrome.tabs.query({});
  const host = $("tabs");
  host.replaceChildren();
  for (const tab of items.filter((t) => /^https?:\/\//.test(t.url || ""))) {
    const row = document.createElement("div");
    row.className = "tab";
    const head = document.createElement("div");
    head.className = "tabhead";
    const title = document.createElement("span");
    title.className = "title";
    title.title = tab.title || tab.url || "";
    title.textContent = tab.title || "Untitled";
    head.append(title);
    const info = document.createElement("div");
    info.className = "tabinfo";
    info.textContent = tab.url || "";
    const controls = document.createElement("div");
    controls.className = "controls";
    const rate = document.createElement("input");
    rate.type = "number";
    rate.min = "1";
    rate.max = "1000000";
    rate.value = data.active[tab.id] || data.settings.downloadKib;
    const apply = document.createElement("button");
    apply.textContent = data.active[tab.id] ? "Update" : "Limit";
    apply.addEventListener("click", async () => {
      try {
        await send({ op: "set", tabId: tab.id, downloadKib: val(rate) });
        notify("Limit applied to this tab");
        await refresh();
      } catch (error) { notify(error.message, true); }
    });
    controls.append(rate, apply);
    if (data.active[tab.id]) {
      const clear = document.createElement("button");
      clear.textContent = "Remove";
      clear.addEventListener("click", async () => {
        try {
          await send({ op: "clear", tabId: tab.id });
          notify("Tab is unlimited");
          await refresh();
        } catch (error) { notify(error.message, true); }
      });
      controls.append(clear);
    }
    const state = document.createElement("div");
    state.className = "status";
    state.textContent = data.active[tab.id]
      ? "Active · " + data.active[tab.id] + " KiB/s"
      : (data.errors[tab.id] || "No tab limit");
    if (data.errors[tab.id]) state.classList.add("error");
    row.append(head, info, controls, state);
    host.append(row);
  }
  if (!host.childElementCount) host.textContent = "No HTTP/HTTPS tabs open.";
}
async function save() {
  try {
    await send({ op: "config", downloadKib: val($("default")),
      autoNewTabs: $("auto").checked });
    notify("Default saved");
  } catch (error) { notify(error.message, true); }
}
$("default").addEventListener("change", save);
$("auto").addEventListener("change", save);
$("refresh").addEventListener("click", () => refresh().catch((e) => notify(e.message, true)));
$("all").addEventListener("click", async () => {
  try {
    await save();
    const result = await send({ op: "all" });
    notify(result.succeeded + " tabs limited; " + result.failed + " failed");
    await refresh();
  } catch (error) { notify(error.message, true); }
});
refresh().catch((e) => notify(e.message, true));
