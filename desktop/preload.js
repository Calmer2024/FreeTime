const { contextBridge } = require("electron");

// 安全地暴露 API 给渲染进程
contextBridge.exposeInMainWorld("electronAPI", {
  platform: process.platform,
  isElectron: true,
});
