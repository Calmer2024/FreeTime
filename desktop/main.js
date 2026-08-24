const { app, BrowserWindow, Menu, shell, screen, ipcMain, nativeImage } = require("electron");
const { spawn } = require("child_process");
const { createSafeLogger } = require("./safe-logger");
const fs = require("fs");
const path = require("path");
const http = require("http");
const { getLoopbackUrl, resolveBackendDir, resolvePortFile } = require("./backend-paths");
const { fitBoundsToWorkArea, getDisplayWindowMetrics } = require("./window-layout");
const { autoUpdater } = require("electron-updater");

// Electron GUI 打包环境中的 stdout/stderr 可能在父进程退出后断开，统一通过安全日志写入。
const logger = createSafeLogger(process.stdout, process.stderr);

const DEV_MODE = !app.isPackaged;
const DEFAULT_PORT = 8000;
// A newly installed PyInstaller bundle can spend several minutes in the first
// Windows security scan. Process exit is handled separately, so keep a wide
// cold-start window without hiding real backend crashes.
const BACKEND_START_TIMEOUT_MS = 600000;

let mainWindow = null;
let pythonProcess = null;
let currentDisplayId = null;
let displaySyncTimer = null;
let PORT = DEFAULT_PORT;
let BASE_URL = getLoopbackUrl(PORT);

function taskbarBadgeImage(count) {
  const label = count > 99 ? "99+" : String(count);
  const fontSize = label.length > 2 ? 10 : 14;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32"><circle cx="22" cy="10" r="9" fill="#25252a" stroke="#fff" stroke-width="2"/><text x="22" y="10" fill="#fff" font-family="Segoe UI,Arial,sans-serif" font-size="${fontSize}" font-weight="700" text-anchor="middle" dominant-baseline="central">${label}</text></svg>`;
  return nativeImage.createFromDataURL(`data:image/svg+xml;base64,${Buffer.from(svg).toString("base64")}`);
}

function setTaskbarBadge(count) {
  if (!mainWindow || mainWindow.isDestroyed() || process.platform !== "win32") return;
  const value = Math.max(0, Math.floor(Number(count) || 0));
  mainWindow.setOverlayIcon(value ? taskbarBadgeImage(value) : null, value ? `${value} 个已结束任务` : "无已结束任务");
}

ipcMain.on("taskbar-badge", (_event, count) => setTaskbarBadge(count));

ipcMain.handle("import-to-coolnote", async (_event, filePath) => {
  if (process.platform !== "win32" || typeof filePath !== "string" || !filePath.trim()) {
    throw new Error("当前系统不支持直接导入 CoolNote");
  }
  const candidates = [
    process.env.COOLNOTE_EXE,
    "D:\\soft\\Calmer\\CoolNote\\coolnote.exe",
    path.join(process.env.LOCALAPPDATA || "", "CoolNote", "CoolNote.exe"),
  ].filter(Boolean);
  const executable = candidates.find(candidate => fs.existsSync(candidate));
  if (!executable) throw new Error("未找到已安装的 CoolNote");
  spawn(executable, [filePath], { detached: true, stdio: "ignore" }).unref();
  return true;
});

function setupSilentUpdates() {
  if (DEV_MODE) return;
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.allowDowngrade = false;
  autoUpdater.logger = logger;
  autoUpdater.on("update-available", info => logger.log(`[Update] 发现新版本 ${info.version}，后台下载`));
  autoUpdater.on("update-downloaded", info => logger.log(`[Update] ${info.version} 已就绪，将在退出时静默安装`));
  autoUpdater.on("error", error => logger.warn(`[Update] 检查或下载失败: ${error.message}`));
  autoUpdater.checkForUpdates().catch(error => logger.warn(`[Update] 检查失败: ${error.message}`));
}

// ========== 端口管理 ==========

function readPortFromFile() {
  const portFile = app.isPackaged
    ? resolvePortFile(process.resourcesPath)
    : path.join(__dirname, ".port");
  try {
    if (fs.existsSync(portFile)) {
      const port = parseInt(fs.readFileSync(portFile, "utf-8").trim(), 10);
      if (port > 0 && port < 65536) {
        PORT = port;
        BASE_URL = getLoopbackUrl(PORT);
        logger.log(`[Port] 读取端口: ${PORT}`);
        return true;
      }
    }
  } catch (e) {
    // 忽略
  }
  return false;
}

// ========== Python 后端管理 ==========

function getBackendDir() {
  if (app.isPackaged) {
    // electron-builder preserves the resource directory name under resources/.
    return resolveBackendDir(process.resourcesPath);
  }
  return path.join(__dirname, "..");
}

function getBackendExePath() {
  if (app.isPackaged) {
    return path.join(getBackendDir(), "freetime-backend.exe");
  }
  return null;
}

function startPythonBackend() {
  const exePath = getBackendExePath();
  if (!exePath) {
    logger.log("[Dev] 跳过 Python 启动（开发模式）");
    return Promise.resolve();
  }

  return new Promise((resolve, reject) => {
    const portFile = resolvePortFile(process.resourcesPath);
    try {
      fs.rmSync(portFile, { force: true });
    } catch (e) {
      logger.warn(`[Port] 无法移除旧端口文件: ${e.message}`);
    }

    logger.log(`[Backend] 启动 Python 后端: ${exePath}`);
    logger.log(`[Backend] 工作目录: ${getBackendDir()}`);

    pythonProcess = spawn(exePath, [], {
      cwd: getBackendDir(),
      stdio: ["ignore", "pipe", "pipe"],
      env: {
        ...process.env,
        PYTHONIOENCODING: "utf-8",
        PYTHONUNBUFFERED: "1",
        FREETIME_DATA_DIR: app.getPath("userData"),
      },
    });

    let settled = false;
    let portDiscovered = false;
    let errorTail = "";

    pythonProcess.stdout.on("data", (data) => {
      const line = data.toString().trim();
      logger.log(`[Backend] ${line}`);
      // 从输出中提取端口号
      const portMatch = line.match(/端口:\s*(\d+)/);
      if (portMatch) {
        PORT = parseInt(portMatch[1], 10);
        BASE_URL = getLoopbackUrl(PORT);
        portDiscovered = true;
        logger.log(`[Port] 检测到端口: ${PORT}`);
      }
    });

    pythonProcess.stderr.on("data", (data) => {
      const message = data.toString().trim();
      errorTail = `${errorTail}\n${message}`.trim().slice(-1200);
      logger.log(`[Backend] ${message}`);
    });

    pythonProcess.on("error", (err) => {
      logger.error("[Backend] 启动失败:", err.message);
      if (!settled) {
        settled = true;
        reject(err);
      }
    });

    pythonProcess.on("close", (code) => {
      logger.log(`[Backend] 进程退出, code=${code}`);
      pythonProcess = null;
      if (!settled) {
        settled = true;
        const detail = errorTail ? `: ${errorTail}` : "";
        reject(new Error(`Python 后端异常退出 (code=${code})${detail}`));
      }
    });

    // 等待后端就绪
    waitForBackend(BACKEND_START_TIMEOUT_MS, () => {
      if (readPortFromFile()) portDiscovered = true;
      return portDiscovered;
    })
      .then(() => {
        if (settled) return;
        settled = true;
        logger.log("[Backend] 后端已就绪");
        resolve();
      })
      .catch((err) => {
        if (settled) return;
        settled = true;
        reject(err);
      });
  });
}

function waitForBackend(timeoutMs = 30000, isPortReady = () => true) {
  const startTime = Date.now();

  return new Promise((resolve, reject) => {
    const check = () => {
      if (!isPortReady()) {
        retry();
        return;
      }

      const req = http.get(`${BASE_URL}/api/health`, (res) => {
        if (res.statusCode === 200) {
          resolve();
        } else {
          retry();
        }
      });

      req.on("error", () => retry());
      req.setTimeout(2000, () => {
        req.destroy();
        retry();
      });
    };

    const retry = () => {
      if (Date.now() - startTime > timeoutMs) {
        reject(new Error("后端启动超时"));
        return;
      }
      setTimeout(check, 500);
    };

    check();
  });
}

function stopPythonBackend() {
  if (pythonProcess) {
    logger.log("[Backend] 正在关闭 Python 后端...");
    if (process.platform === "win32") {
      try {
        pythonProcess.kill("SIGINT");
      } catch (e) {
        // 进程可能已退出
      }
    } else {
      pythonProcess.kill("SIGTERM");
    }

    setTimeout(() => {
      if (pythonProcess) {
        try {
          pythonProcess.kill("SIGKILL");
        } catch (e) {
          // 进程可能已退出
        }
      }
    }, 5000);
  }
}

// ========== 窗口管理 ==========

function createWindow() {
  const primaryDisplay = screen.getPrimaryDisplay();
  const metrics = getDisplayWindowMetrics(primaryDisplay.workAreaSize);

  mainWindow = new BrowserWindow({
    width: metrics.width,
    height: metrics.height,
    minWidth: metrics.minWidth,
    minHeight: metrics.minHeight,
    useContentSize: true,
    center: true,
    title: "FreeTime",
    icon: getIconPath(),
    frame: true,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
      zoomFactor: 1.0,
    },
    show: false,
    backgroundColor: "#ffffff",
  });
  currentDisplayId = primaryDisplay.id;

  // 暴露当前显示器倍率供样式诊断使用；布局仍由原生 CSS 像素/DIP 驱动。
  mainWindow.webContents.on("did-finish-load", () => {
    const display = screen.getDisplayMatching(mainWindow.getBounds());
    exposeDisplayScale(display);
  });

  mainWindow.on("move", () => {
    clearTimeout(displaySyncTimer);
    displaySyncTimer = setTimeout(() => {
      if (!mainWindow) return;
      const display = screen.getDisplayMatching(mainWindow.getBounds());
      if (display.id !== currentDisplayId) syncWindowToDisplay(display);
    }, 120);
  });

  // 完全移除菜单栏
  Menu.setApplicationMenu(null);

  // 加载主页面（先显示加载动画）
  mainWindow.loadURL("data:text/html," + encodeURIComponent(getLoadingHTML()));

  // 后端就绪后加载真实页面
  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  // 处理外部链接
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function exposeDisplayScale(display) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.insertCSS(`:root { --electron-scale-factor: ${display.scaleFactor || 1}; }`);
}

function syncWindowToDisplay(display) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const metrics = getDisplayWindowMetrics(display.workAreaSize);
  mainWindow.setMinimumSize(metrics.minWidth, metrics.minHeight);

  const bounds = mainWindow.getBounds();
  const fittedBounds = fitBoundsToWorkArea(bounds, display.workArea);
  if (Object.keys(fittedBounds).some((key) => fittedBounds[key] !== bounds[key])) {
    mainWindow.setBounds(fittedBounds);
  }

  currentDisplayId = display.id;
  exposeDisplayScale(display);
}

function getIconPath() {
  if (app.isPackaged) {
    const icoPath = path.join(process.resourcesPath, "icon.ico");
    if (fs.existsSync(icoPath)) return icoPath;
  }
  return path.join(__dirname, "icon.ico");
}

function getLoadingHTML() {
  return `<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>FreeTime</title>
<style>
  body { margin:0; display:flex; align-items:center; justify-content:center;
         height:100vh; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         background:#fff; }
  .box { text-align:center; }
  .spinner { width:40px; height:40px; border:3px solid #f0f0f0;
             border-top-color:#6366f1; border-radius:50%;
             animation:spin 0.8s linear infinite; margin:0 auto 16px; }
  @keyframes spin { to { transform:rotate(360deg); } }
  p { color:#888; font-size:14px; }
</style></head>
<body><div class="box"><div class="spinner"></div><p>正在启动 FreeTime...</p></div>
</body></html>`;
}

function loadMainPage() {
  if (mainWindow) {
    mainWindow.loadURL(BASE_URL);
  }
}

// ========== 应用生命周期 ==========

app.whenReady().then(async () => {
  // 移除菜单栏
  Menu.setApplicationMenu(null);
  setupSilentUpdates();

  // 尝试从文件读取端口
  readPortFromFile();

  // 先创建窗口（显示加载动画）
  createWindow();

  screen.on("display-metrics-changed", (_event, display) => {
    if (!mainWindow) return;
    const activeDisplay = screen.getDisplayMatching(mainWindow.getBounds());
    if (activeDisplay.id === display.id) syncWindowToDisplay(display);
  });

  try {
    await startPythonBackend();
    // 启动后再次读取端口
    readPortFromFile();
    // Only navigate to the backend after it has passed the health check.
    loadMainPage();
  } catch (err) {
    logger.error("[App] 后端启动失败:", err.message);
    // 显示错误页面
    if (mainWindow) {
      mainWindow.loadURL("data:text/html," + encodeURIComponent(
        `<!DOCTYPE html><html><head><meta charset="utf-8"><title>FreeTime</title>
        <style>body{margin:0;display:flex;align-items:center;justify-content:center;
        height:100vh;font-family:-apple-system,sans-serif;background:#fff;}
        .box{text-align:center;} h2{color:#e53e3e;} p{color:#666;font-size:14px;}</style>
        </head><body><div class="box"><h2>启动失败</h2>
        <p>Python 后端无法启动，请检查是否安装了 Python 3.10+<br><br>
        错误信息: ${err.message}</p></div></body></html>`
      ));
    }
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  stopPythonBackend();
  app.quit();
});

app.on("before-quit", () => {
  stopPythonBackend();
});

// 阻止二次启动
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}
