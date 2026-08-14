const test = require("node:test");
const assert = require("node:assert/strict");

const { createSafeLogger } = require("./safe-logger");

test("does not throw when the console pipe is closed (EPIPE)", () => {
  const sink = {
    write() {
      const error = new Error("broken pipe");
      error.code = "EPIPE";
      throw error;
    },
    on() {},
  };

  const logger = createSafeLogger(sink);
  assert.doesNotThrow(() => logger.log("backend output"));
  assert.doesNotThrow(() => logger.warn("warning"));
  assert.doesNotThrow(() => logger.error("error"));
});

test("does not rethrow EPIPE emitted asynchronously by the sink", () => {
  const listeners = {};
  const sink = {
    write() {},
    on(event, listener) {
      listeners[event] = listener;
    },
  };

  createSafeLogger(sink);
  assert.doesNotThrow(() => listeners.error(Object.assign(new Error("broken pipe"), { code: "EPIPE" })));
});
