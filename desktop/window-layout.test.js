const test = require("node:test");
const assert = require("node:assert/strict");
const { fitBoundsToWorkArea, getDisplayWindowMetrics } = require("./window-layout");

test("uses the three-card minimum on a standard desktop", () => {
  assert.deepEqual(getDisplayWindowMetrics({ width: 1707, height: 1019 }), {
    minWidth: 1160,
    minHeight: 720,
    width: 1400,
    height: 876,
  });
});

test("DIP metrics are invariant across display scale factors", () => {
  const workArea = { width: 1280, height: 800 };
  assert.deepEqual(
    getDisplayWindowMetrics({ ...workArea, scaleFactor: 1 }),
    getDisplayWindowMetrics({ ...workArea, scaleFactor: 2.25 })
  );
});

test("small displays receive usable fallback constraints", () => {
  assert.deepEqual(getDisplayWindowMetrics({ width: 1024, height: 600 }), {
    minWidth: 1008,
    minHeight: 560,
    width: 1008,
    height: 560,
  });
});

test("window bounds remain inside the active work area", () => {
  assert.deepEqual(
    fitBoundsToWorkArea(
      { x: 1500, y: -20, width: 1400, height: 900 },
      { x: 0, y: 0, width: 1280, height: 800 }
    ),
    { x: 0, y: 0, width: 1280, height: 800 }
  );
});
