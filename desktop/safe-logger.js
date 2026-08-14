"use strict";

function isBrokenPipe(error) {
  return Boolean(error && error.code === "EPIPE");
}

function attachPipeGuard(sink) {
  if (!sink || typeof sink.on !== "function") return;
  sink.on("error", (error) => {
    if (isBrokenPipe(error)) return;
  });
}

function stringify(value) {
  if (value instanceof Error) return value.stack || value.message;
  if (typeof value === "string") return value;
  try { return JSON.stringify(value); } catch { return String(value); }
}

function createSafeLogger(sink = process.stdout, errorSink = sink) {
  attachPipeGuard(sink);
  if (errorSink !== sink) attachPipeGuard(errorSink);
  const write = (target, args) => {
    if (!target || typeof target.write !== "function") return;
    try { target.write(`${args.map(stringify).join(" ")}\n`); } catch (error) {
      if (!isBrokenPipe(error)) return;
    }
  };
  return {
    log: (...args) => write(sink, args),
    debug: (...args) => write(sink, args),
    info: (...args) => write(sink, args),
    warn: (...args) => write(errorSink, args),
    error: (...args) => write(errorSink, args),
  };
}

module.exports = { createSafeLogger, isBrokenPipe };
