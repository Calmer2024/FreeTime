const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { getLoopbackUrl, resolveBackendDir, resolvePortFile } = require("./backend-paths");

test("resolves the packaged backend directory", () => {
  const resources = "C:/FreeTime/resources";
  assert.equal(
    resolveBackendDir(resources, (candidate) =>
      candidate.replaceAll("\\", "/") === "C:/FreeTime/resources/freetime-backend"
    ),
    path.join(resources, "freetime-backend")
  );
});

test("supports legacy flat extra-resource layout", () => {
  const resources = "C:/FreeTime/resources";
  assert.equal(
    resolveBackendDir(resources, () => false),
    resources
  );
});

test("reads the port file beside the packaged backend executable", () => {
  const resources = "C:/FreeTime/resources";
  assert.equal(
    resolvePortFile(resources, (candidate) =>
      candidate.replaceAll("\\", "/") === "C:/FreeTime/resources/freetime-backend"
    ),
    path.join(resources, "freetime-backend", ".port")
  );
});

test("reads the legacy port file from the resources root", () => {
  const resources = "C:/FreeTime/resources";
  assert.equal(resolvePortFile(resources, () => false), path.join(resources, ".port"));
});

test("uses the IPv4 loopback address used by the backend binding", () => {
  assert.equal(getLoopbackUrl(8123), "http://127.0.0.1:8123");
});
