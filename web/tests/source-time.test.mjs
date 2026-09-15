import test from "node:test";
import assert from "node:assert/strict";

import { formatSourceTime, parseSourceTime } from "../src/utils/sourceTime.js";

test("timezone-less source timestamps are UTC and display in Shanghai", () => {
  assert.equal(
    formatSourceTime("2026-09-14 17:44:21", { includeYear: true }),
    "2026-09-15 01:44",
  );
});

test("timestamps carrying an offset keep the same Shanghai instant", () => {
  assert.equal(formatSourceTime("2026-09-15T01:44:21+08:00"), "09-15 01:44");
});

test("invalid and missing timestamps remain readable", () => {
  assert.equal(parseSourceTime("not-a-time"), null);
  assert.equal(formatSourceTime("not-a-time"), "not-a-time");
  assert.equal(formatSourceTime(""), "时间未记录");
});
