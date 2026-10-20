chrome.action.onClicked.addListener(async () => {
  const url = chrome.runtime.getURL("dashboard.html");
  const tabs = await chrome.tabs.query({url});
  if (tabs[0]) await chrome.tabs.update(tabs[0].id, {active: true});
  else await chrome.tabs.create({url});
});
