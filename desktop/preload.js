const { contextBridge, ipcRenderer } = require("electron");

// 安全地暴露 API 给渲染进程
contextBridge.exposeInMainWorld("electronAPI", {
  platform: process.platform,
  isElectron: true,
  setTaskbarBadge: (count) => ipcRenderer.send("taskbar-badge", Number(count) || 0),
  importToCoolNote: (filePath) => ipcRenderer.invoke("import-to-coolnote", filePath),
});
