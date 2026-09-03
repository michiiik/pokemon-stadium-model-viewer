#!/usr/bin/env node
// Synthetic transform fixture for the Stadium 1 viewer bone builder.
//
// The chain below is evaluated against the source matrix sequence in
// disasm/src/12D80.c (Geo_NodeAnimatedPart) and disasm/src/F420.c:
//   func_80012428  scale stack init to (1,1,1)
//   func_8000F730  local rotation + translation scaled by the stack top
//   func_80012458  scale stack push (cumulative *= node scale)
//   func_8000FBB0  compose with the parent's pre-scale (+32 slot) matrix
//   func_8000FDE4  apply the new cumulative scale
// The expected values are hand-derived from that sequence, not from the
// implementation under test.
"use strict";

const path = require("path");
const math = require(path.join(__dirname, "stadium1_viewer", "bone_math.js"));

const EPS = 1e-4;
let failures = 0;

function check(label, actual, expected) {
  const ok = expected.every((value, index) => Math.abs(actual[index] - value) <= EPS);
  if (!ok) {
    failures += 1;
    console.error(`FAIL ${label}: got [${actual.map((v) => v.toFixed(4))}], expected [${expected}]`);
  } else {
    console.log(`ok ${label}`);
  }
}

const bones = [
  // Root with a non-uniform, non-unit scale.
  { id: 0, parent: null, flags: 0, joint: -1, position: [0, 0, 0], rotation: [0, 0, 0], scale: [2, 3, 4] },
  // Child with a translation and a 90 degree X rotation (16384 = 90 deg in
  // the N64 angle unit), unit scale.
  { id: 1, parent: 0, flags: 0, joint: -1, position: [10, 0, 0], rotation: [16384, 0, 0], scale: [1, 1, 1] },
  // flags & 1 child: full TRS local composed with the parent's final matrix.
  { id: 2, parent: 1, flags: 1, joint: -1, position: [0, 0, 5], rotation: [0, 0, 0], scale: [1, 1, 1] },
  // Normal child of bone 1 with its own scale: the scale stack accumulates.
  { id: 3, parent: 1, flags: 0, joint: -1, position: [0, 0, 5], rotation: [0, 0, 0], scale: [2, 2, 2] },
];

const matrices = math.buildBoneMatrices(bones, null);
const origin = (id) => math.vec3Transform(matrices.get(id), [0, 0, 0]);

// Bone 0: world = S(2,3,4); origin stays at the root.
check("bone0 origin", origin(0), [0, 0, 0]);

// Bone 1: translation scaled by the parent's cumulative scale -> (20,0,0).
// The old approximation (parent world * T * R * S) would give the same
// origin here, so also check a rotated vertex under the conjugated scale:
// (0,1,0) -> S(cum) -> (0,3,0) -> Rx90 -> (0,0,3) -> +T = (20,0,3).
// The approximation would produce (20,0,4).
check("bone1 origin", origin(1), [20, 0, 0]);
check("bone1 rotated vertex", math.vec3Transform(matrices.get(1), [0, 1, 0]), [20, 0, 3]);

// Bone 2 (flags & 1): origin = bone1 world applied to (0,0,5):
// S(2,3,4) -> (0,0,20) -> Rx90 -> (0,-20,0) -> +T = (20,-20,0).
check("bone2 origin", origin(2), [20, -20, 0]);

// Bone 3: scale stack top is still (2,3,4), so the translation becomes
// (0,0,20); composed with bone1's pre-scale matrix T(20,0,0)*Rx90 the origin
// is (20,-20,0), and the cumulative scale (4,6,8) maps vertex (1,0,0) to
// (24,-20,0). Plain TRS composition would double-apply the parent's scale.
check("bone3 origin", origin(3), [20, -20, 0]);
check("bone3 scaled vertex", math.vec3Transform(matrices.get(3), [1, 0, 0]), [24, -20, 0]);

// flags & 2: source func_80010228 camera-facing branch. The child origin is
// still placed through the parent matrix, while its axes use the supplied
// view orientation and preserve the parent's axis lengths.
const billboardView = [
  0, 0, -1, 0,
  0, 1, 0, 0,
  1, 0, 0, 0,
  0, 0, 0, 1,
];
const billboardBones = [
  { id: 10, parent: null, flags: 0, joint: -1, position: [0, 0, 0], rotation: [0, 0, 0], scale: [2, 3, 4] },
  { id: 11, parent: 10, flags: 3, joint: -1, position: [1, 2, 3], rotation: [0, 0, 0], scale: [1, 1, 1] },
];
const billboardMatrices = math.buildBoneMatrices(billboardBones, null, billboardView);
check("billboard origin", math.vec3Transform(billboardMatrices.get(11), [0, 0, 0]), [2, 6, 12]);
check("billboard x axis", math.vec3Transform(billboardMatrices.get(11), [1, 0, 0]), [2, 6, 14]);

if (failures) {
  console.error(`${failures} bone transform check(s) failed`);
  process.exit(1);
}
console.log("All bone transform checks passed.");
