const fs = require("fs");
const path = require("path");

function resolveBackendDir(resourcesPath, existsSync = fs.existsSync) {
  const packagedDir = path.join(resourcesPath, "freetime-backend");
  return existsSync(packagedDir) ? packagedDir : resourcesPath;
}

function resolvePortFile(resourcesPath, existsSync = fs.existsSync) {
  return path.join(resolveBackendDir(resourcesPath, existsSync), ".port");
}

function getLoopbackUrl(port) {
  return `http://127.0.0.1:${port}`;
}

module.exports = { getLoopbackUrl, resolveBackendDir, resolvePortFile };
