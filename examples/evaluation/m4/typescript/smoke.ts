import assert from "node:assert/strict";
import { numericSort } from "./candidate.ts";

assert.deepEqual(numericSort([2, 1]), [1, 2]);
console.log("Public smoke passed; this is not the independent acceptance test.");
