const { app, BrowserWindow, dialog, shell } = require("electron");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawn, spawnSync } = require("node:child_process");
const { autoUpdater } = require("electron-updater");

app.setName("织梦台");

let API_PORT = Number(process.env.NOVEL_DESKTOP_API_PORT || 0);
const children = [];
let quitting = false;
let mainWindow = null;

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) app.quit();

function allocateApiPort() {
  if (API_PORT > 0) return Promise.resolve(API_PORT);
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close((error) => error ? reject(error) : resolve(address.port));
    });
  });
}

function projectRoot() {
  return app.isPackaged ? process.resourcesPath : path.resolve(__dirname, "..");
}

function userDataRoot() {
  const root = path.join(app.getPath("userData"), "data");
  fs.mkdirSync(path.join(root, "logs"), { recursive: true });
  return root;
}

function secretKey(root) {
  const keyPath = path.join(root, "app-secret.key");
  if (!fs.existsSync(keyPath)) {
    fs.writeFileSync(keyPath, crypto.randomBytes(32).toString("base64"), { encoding: "utf8", mode: 0o600 });
  }
  return fs.readFileSync(keyPath, "utf8").trim();
}

function databaseUrl(root) {
  return `sqlite:///${path.join(root, "data.db").replaceAll("\\", "/")}`;
}

function commonEnv(root) {
  return {
    ...process.env,
    APP_ENV: "production",
    DATABASE_URL: databaseUrl(root),
    APP_SECRET_KEY: secretKey(root),
    WEB_ORIGIN: "null",
    DESKTOP_MODE: "1",
    PYTHONUNBUFFERED: "1",
  };
}

function logFile(root, name, suffix) {
  return fs.openSync(path.join(root, "logs", `${name}.${suffix}.log`), "a");
}

function spawnService(name, executable, args, cwd, env, root) {
  const child = spawn(executable, args, {
    cwd,
    env,
    windowsHide: true,
    stdio: ["ignore", logFile(root, name, "out"), logFile(root, name, "err")],
  });
  child.__serviceName = name;
  children.push(child);
  child.once("error", (error) => {
    if (!quitting) console.error(`${name} failed to start`, error);
  });
  child.once("exit", (code, signal) => {
    if (!quitting && code !== 0) console.error(`${name} exited`, { code, signal });
  });
  return child;
}

function startServices(root, dataRoot) {
  const env = { ...commonEnv(dataRoot), PORT: String(API_PORT) };
  if (app.isPackaged) {
    spawnService("backend", path.join(root, "backend", "backend.exe"), ["--host", "127.0.0.1", "--port", String(API_PORT)], dataRoot, env, dataRoot);
    spawnService("worker", path.join(root, "worker", "worker.exe"), [], dataRoot, env, dataRoot);
    return;
  }

  const python = process.env.PYTHON_EXECUTABLE || path.join(root, ".venv", "Scripts", "python.exe");
  if (!fs.existsSync(python)) throw new Error(`找不到 Python 虚拟环境：${python}`);
  spawnService("backend", python, ["-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", String(API_PORT)], root, env, dataRoot);
  spawnService("worker", python, ["-m", "app.worker"], root, env, dataRoot);
}

function waitForBackend(timeoutMs = 30000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const retry = () => {
      if (Date.now() - started >= timeoutMs) return reject(new Error(`后端在 ${timeoutMs / 1000} 秒内没有启动`));
      setTimeout(check, 250);
    };
    const check = () => {
      const request = http.get(`http://127.0.0.1:${API_PORT}/api/health`, (response) => {
        response.resume();
        if (response.statusCode && response.statusCode < 500) return resolve();
        retry();
      });
      request.on("error", retry);
      request.setTimeout(1500, () => request.destroy());
    };
    check();
  });
}

function stopChild(child) {
  if (!child || child.killed) return;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
  } else {
    child.kill("SIGTERM");
  }
}

function stopServices() {
  quitting = true;
  for (const child of children.reverse()) stopChild(child);
}

async function createWindow() {
  const dataRoot = userDataRoot();
  startServices(projectRoot(), dataRoot);
  await waitForBackend();

  const window = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 1100,
    minHeight: 720,
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.cjs"),
    },
  });
  window.once("ready-to-show", () => window.show());
  const webRoot = app.isPackaged ? path.join(process.resourcesPath, "web") : path.join(projectRoot(), "web", "out");
  window.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  await window.loadFile(path.join(webRoot, "index.html"), {
    query: { apiBase: `http://127.0.0.1:${API_PORT}/api` },
  });
  return window;
}

function configureAutoUpdate(window) {
  if (!app.isPackaged) return;

  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.on("error", (error) => console.warn("update check failed", error));
  autoUpdater.on("update-available", async (info) => {
    const { response } = await dialog.showMessageBox(window, {
      type: "info",
      title: "发现新版本",
      message: `织梦台 ${info.version} 已可更新。`,
      detail: "下载完成后可立即重启安装，也可以下次退出时自动安装。",
      buttons: ["下载更新", "稍后"],
      defaultId: 0,
      cancelId: 1,
    });
    if (response === 0) autoUpdater.downloadUpdate().catch((error) => console.warn("update download failed", error));
  });
  autoUpdater.on("download-progress", (progress) => window.setProgressBar(Math.max(0, Math.min(1, progress.percent / 100))));
  autoUpdater.on("update-downloaded", async (info) => {
    window.setProgressBar(-1);
    const { response } = await dialog.showMessageBox(window, {
      type: "info",
      title: "更新已下载",
      message: `织梦台 ${info.version} 已下载完成。`,
      detail: "重启后将完成安装。未保存的编辑内容请先保存。",
      buttons: ["立即重启安装", "下次启动时安装"],
      defaultId: 0,
      cancelId: 1,
    });
    if (response === 0) autoUpdater.quitAndInstall();
  });
  setTimeout(() => autoUpdater.checkForUpdates().catch((error) => console.warn("update check failed", error)), 1500);
}

app.whenReady().then(async () => {
  if (!hasSingleInstanceLock) return;
  try {
    API_PORT = await allocateApiPort();
    mainWindow = await createWindow();
    configureAutoUpdate(mainWindow);
  } catch (error) {
    console.error(error);
    await dialog.showMessageBox({ type: "error", title: "织梦台启动失败", message: error.message || String(error), detail: "请查看用户数据目录下 data\\logs 中的 backend 和 worker 日志。" });
    app.quit();
  }
});

app.on("second-instance", () => {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
});

app.on("before-quit", stopServices);
app.on("window-all-closed", () => app.quit());
