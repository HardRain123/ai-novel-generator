const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("desktopRuntime", {
  isDesktop: true,
  platform: process.platform,
});
