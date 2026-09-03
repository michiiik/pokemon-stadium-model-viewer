// Matrix helpers and the bone-transform builder for the Stadium 1 viewer.
// The bone builder mirrors the source matrix/scale stacks in
// disasm/src/12D80.c (Geo_NodeAnimatedPart) and disasm/src/F420.c
// (func_8000F730/func_8000F5A8/func_8000FBB0/func_8000FDE4/func_80012428/
// func_80012458) instead of approximating them with plain TRS composition.
// This file is loadable both by the browser (globals) and by Node (tests).
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof self !== "undefined" ? self : globalThis, function () {
  "use strict";

  function mat4Identity() { return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]; }
  function mat4Multiply(a, b) {
    const out = new Array(16).fill(0);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) for (let k = 0; k < 4; k++) out[c * 4 + r] += a[k * 4 + r] * b[c * 4 + k];
    return out;
  }
  function mat4Translate(v) { const m = mat4Identity(); m[12] = v[0]; m[13] = v[1]; m[14] = v[2]; return m; }
  function mat4Scale(v) { const m = mat4Identity(); m[0] = v[0]; m[5] = v[1]; m[10] = v[2]; return m; }
  function mat4RotateXYZ(degrees) {
    const x = degrees[0] * Math.PI / 180, y = degrees[1] * Math.PI / 180, z = degrees[2] * Math.PI / 180;
    const cx = Math.cos(x), sx = Math.sin(x), cy = Math.cos(y), sy = Math.sin(y), cz = Math.cos(z), sz = Math.sin(z);
    const rx = [1,0,0,0, 0,cx,sx,0, 0,-sx,cx,0, 0,0,0,1];
    const ry = [cy,0,-sy,0, 0,1,0,0, sy,0,cy,0, 0,0,0,1];
    const rz = [cz,sz,0,0, -sz,cz,0,0, 0,0,1,0, 0,0,0,1];
    return mat4Multiply(mat4Multiply(rz, ry), rx);
  }
  function vec3Transform(m, v) { return [m[0]*v[0] + m[4]*v[1] + m[8]*v[2] + m[12], m[1]*v[0] + m[5]*v[1] + m[9]*v[2] + m[13], m[2]*v[0] + m[6]*v[1] + m[10]*v[2] + m[14]]; }
  function n64Degrees(v) { return v * 180 / 32768; }

  // Build the world matrix of every bone for one pose.
  //
  // Source semantics reproduced here (Geo_NodeAnimatedPart in 12D80.c):
  // - Normal branch (flags & 3 == 0): func_8000F730 builds the local matrix as
  //   pure rotation with the translation scaled component-wise by the current
  //   cumulative scale stack top (D_800AB970). func_80012458 then pushes the
  //   stack (cumulative *= node scale). func_8000FBB0 composes with the
  //   parent's pre-scale-application matrix (the source's depth+32 slot) when
  //   the parent was itself a normal-branch animated part, and with the
  //   parent's final matrix otherwise. Finally func_8000FDE4 applies the new
  //   cumulative scale to the composed matrix, producing the matrix used for
  //   rendering and by non-animated children.
  // - flags & 1 branch: func_8000F5A8 builds a full TRS local matrix and
  //   func_800122B4 composes it with the parent's final matrix. The scale
  //   stack is not pushed.
  function billboardMatrix(viewMatrix, parentWorld, position) {
    const parentScale = [
      Math.hypot(parentWorld[0], parentWorld[1], parentWorld[2]),
      Math.hypot(parentWorld[4], parentWorld[5], parentWorld[6]),
      Math.hypot(parentWorld[8], parentWorld[9], parentWorld[10]),
    ];
    const translated = vec3Transform(parentWorld, position);
    // Source func_80010228 -> func_8000F88C builds camera-facing axes from
    // the view matrix, preserving the parent matrix's axis lengths.
    return [
      viewMatrix[0] * parentScale[0], viewMatrix[4] * parentScale[0], viewMatrix[8] * parentScale[0], 0,
      viewMatrix[1] * parentScale[1], viewMatrix[5] * parentScale[1], viewMatrix[9] * parentScale[1], 0,
      viewMatrix[2] * parentScale[2], viewMatrix[6] * parentScale[2], viewMatrix[10] * parentScale[2], 0,
      translated[0], translated[1], translated[2], 1,
    ];
  }

  function buildBoneMatrices(bones, poses, viewMatrix) {
    const map = new Map(), visiting = new Set();
    const byId = new Map(bones.map((b) => [b.id, b]));
    const states = new Map();
    function build(id) {
      if (map.has(id)) return map.get(id);
      if (visiting.has(id)) return mat4Identity();
      visiting.add(id);
      const b = byId.get(id);
      const poseIndex = b ? (b.poseIndex ?? b.joint) : null;
      const pose = poses && Number.isInteger(poseIndex) && poseIndex >= 0 ? poses[poseIndex] : null;
      const p = pose?.position || b?.position || [0, 0, 0];
      const r = (pose ? pose.rotation : (b?.rotation || [0, 0, 0])).map(n64Degrees);
      const s = pose?.scale || b?.scale || [1, 1, 1];
      let parentState = null;
      if (b?.parent !== null && b?.parent !== undefined && byId.has(b.parent)) {
        build(b.parent);
        parentState = states.get(b.parent) || null;
      }
      const parentWorld = parentState?.world || mat4Identity();
      let world, composed = null, stackScale = parentState?.stackScale || [1, 1, 1];
      if ((b?.flags || 0) & 2 && viewMatrix) {
        const billboard = billboardMatrix(viewMatrix, parentWorld, p);
        world = mat4Multiply(billboard, mat4Multiply(mat4RotateXYZ(r), mat4Scale(s)));
        composed = world;
        // func_80010228 does not push the cumulative scale stack.
        stackScale = parentState?.stackScale || [1, 1, 1];
      } else if ((b?.flags || 0) & 1) {
        world = mat4Multiply(parentWorld, mat4Multiply(mat4Translate(p), mat4Multiply(mat4RotateXYZ(r), mat4Scale(s))));
      } else {
        const cumulative = [stackScale[0] * s[0], stackScale[1] * s[1], stackScale[2] * s[2]];
        const local = mat4Multiply(
          mat4Translate([p[0] * stackScale[0], p[1] * stackScale[1], p[2] * stackScale[2]]),
          mat4RotateXYZ(r));
        composed = mat4Multiply(parentState?.composed || parentWorld, local);
        world = mat4Multiply(composed, mat4Scale(cumulative));
        stackScale = cumulative;
      }
      states.set(id, { world, composed, stackScale });
      map.set(id, world);
      visiting.delete(id);
      return world;
    }
    bones.forEach((b) => build(b.id));
    return map;
  }

  return { mat4Identity, mat4Multiply, mat4Translate, mat4Scale, mat4RotateXYZ, vec3Transform, n64Degrees, billboardMatrix, buildBoneMatrices };
});
