import { test } from "node:test";
import assert from "node:assert/strict";
import {
  dedupeArtifacts,
  duplicateCandidateGroups,
  hiddenDuplicates,
  provisionallyHidden,
} from "../.test-dist/artifact-dedupe.js";

const work = "sales_by_region_2026.xlsx";
const pub = ".deliveries/ab12/sales_by_region_2026.xlsx";

test("identical working file and published copy show once, published copy kept", () => {
  const paths = [work, pub, "chart.png"];
  const hidden = hiddenDuplicates(paths, new Map([[work, "h1"], [pub, "h1"]]));
  assert.deepEqual(dedupeArtifacts(paths, hidden), [pub, "chart.png"]);
});

test("same name, different content keeps both", () => {
  const paths = [work, pub];
  assert.deepEqual(dedupeArtifacts(paths, hiddenDuplicates(paths, new Map([[work, "h1"], [pub, "h2"]]))), paths);
});

test("unknown fingerprint never hides a file", () => {
  const paths = [work, pub];
  assert.deepEqual(dedupeArtifacts(paths, hiddenDuplicates(paths, new Map([[pub, "h1"]]))), paths);
});

test("while loading, only a working file shadowed by a published copy is held back", () => {
  assert.deepEqual([...provisionallyHidden([work, pub, "a/x.csv", "b/x.csv"])], [work]);
  assert.deepEqual(duplicateCandidateGroups([work, pub, work]), [[pub, work]]);
  assert.deepEqual(dedupeArtifacts([work, work], new Set()), [work]);
});
