// An extension module for sweep2Q4Extensions.test.ts: its first activate()
// fails (as a load during a server restart would), every later one works.
// The count is global because "Try again" imports the module under a new URL,
// which is a fresh module instance.
export default {
  activate(api) {
    const g = globalThis;
    g.__extRetryAttempts = (g.__extRetryAttempts || 0) + 1;
    if (g.__extRetryAttempts === 1) throw new Error("server restarting");
    api.ui.registerSurface("pane", (host) => {
      host.el.textContent = "ready";
    });
  },
};
