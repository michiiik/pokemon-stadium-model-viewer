(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    catalog: [], model: null, animation: null, frame: 0, playing: false, loop: true,
    speed: 1, lastTime: 0, camera: { yaw: 0.55, pitch: 0.25, distance: 5, panX: 0, panY: 0 },
    cameraAnchor: null,
    view: { textures: true, lighting: true, wireframe: false, axes: true, bounds: false, skeleton: false, boneNames: false },
    activeProvider: "stadium1", providers: [], tabs: {}, dual: false,
  };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function clamp(value, lo, hi) { return Math.max(lo, Math.min(hi, value)); }
  function fetchJson(url) { return fetch(url).then((r) => r.json().then((body) => r.ok ? body : Promise.reject(new Error(body.error || `HTTP ${r.status}`)))); }
  function ensureTab(provider) { if (!state.tabs[provider]) state.tabs[provider] = { catalog: [], model: null, animation: null, frame: 0, playing: false }; return state.tabs[provider]; }
  function persistActiveTab() {
    const tab = ensureTab(state.activeProvider);
    Object.assign(tab, { catalog: state.catalog, model: state.model, animation: state.animation, frame: state.frame, playing: state.playing });
    if (state.model) {
      try { localStorage.setItem(`pms-viewer-selection:${state.activeProvider}`, JSON.stringify({ modelPath: state.model._path, animationId: state.animation?.id ?? null, frame: state.frame })); } catch (_) { /* storage can be unavailable in private/file contexts */ }
    }
  }
  function restoreTab(provider) {
    const tab = ensureTab(provider);
    state.catalog = tab.catalog || []; state.model = tab.model || null; state.animation = tab.animation || null;
    state.frame = tab.frame || 0; state.playing = !!tab.playing; state.cameraAnchor = null;
  }
  function savedSelection(provider) {
    try { const raw = localStorage.getItem(`pms-viewer-selection:${provider}`); return raw ? JSON.parse(raw) : null; } catch (_) { return null; }
  }
  function providerName(provider) { return provider === "stadium1" ? "Stadium 1" : provider === "stadium2" ? "Stadium 2" : provider; }

  const canvas = $("viewport");
  const gl = canvas.getContext("webgl2", { alpha: false, antialias: true }) || canvas.getContext("webgl", { alpha: false, antialias: true });
  let renderer = null;

  // mat4Identity/mat4Multiply/mat4Translate/mat4Scale/mat4RotateXYZ/
  // vec3Transform/n64Degrees/buildBoneMatrices come from bone_math.js.
  function mat4Perspective(fovy, aspect, near, far) {
    const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
    return [f / aspect,0,0,0, 0,f,0,0, 0,0,(far + near) * nf,-1, 0,0,(2 * far * near) * nf,0];
  }

  class WebGLRenderer {
    constructor(context) {
      this.gl = context; this.textures = new Map(); this.program = null; this.locations = {};
      this.init();
    }
    compile(type, source) {
      const shader = this.gl.createShader(type); this.gl.shaderSource(shader, source); this.gl.compileShader(shader);
      if (!this.gl.getShaderParameter(shader, this.gl.COMPILE_STATUS)) throw new Error(this.gl.getShaderInfoLog(shader));
      return shader;
    }
    init() {
      const vertex = this.compile(this.gl.VERTEX_SHADER, `attribute vec3 aPosition; attribute vec2 aUv; attribute vec4 aColor; attribute vec3 aNormal; uniform mat4 uMvp; varying vec2 vUv; varying vec4 vColor; varying vec3 vNormal; void main(){gl_Position=uMvp*vec4(aPosition,1.0);vUv=aUv;vColor=aColor;vNormal=aNormal;}`);
      const fragment = this.compile(this.gl.FRAGMENT_SHADER, `precision mediump float; uniform sampler2D uTexture; uniform bool uUseTexture; uniform bool uLighting; uniform bool uAlphaCutout; uniform bool uMirrorClampS; uniform bool uMirrorClampT; varying vec2 vUv; varying vec4 vColor; varying vec3 vNormal; float mirrorClamp(float value){float mirrored=abs(value);if(mirrored>1.0)mirrored=2.0-mirrored;return clamp(mirrored,0.0,1.0);} void main(){vec2 sampleUv=vUv;if(uMirrorClampS)sampleUv.x=mirrorClamp(sampleUv.x);if(uMirrorClampT)sampleUv.y=mirrorClamp(sampleUv.y);vec4 tex=uUseTexture?texture2D(uTexture,sampleUv):vec4(1.0);if(uAlphaCutout && tex.a*vColor.a<0.5)discard;vec3 light=vec3(1.0);if(uLighting){float diffuse=max(dot(normalize(vNormal),normalize(vec3(0.45,0.8,0.6))),0.0);light=vec3(0.28+diffuse*0.72);}gl_FragColor=vec4(tex.rgb*vColor.rgb*light,tex.a*vColor.a);}`);
      this.program = this.gl.createProgram(); this.gl.attachShader(this.program, vertex); this.gl.attachShader(this.program, fragment); this.gl.linkProgram(this.program);
      if (!this.gl.getProgramParameter(this.program, this.gl.LINK_STATUS)) throw new Error(this.gl.getProgramInfoLog(this.program));
      this.locations = { position: this.gl.getAttribLocation(this.program, "aPosition"), uv: this.gl.getAttribLocation(this.program, "aUv"), color: this.gl.getAttribLocation(this.program, "aColor"), normal: this.gl.getAttribLocation(this.program, "aNormal"), mvp: this.gl.getUniformLocation(this.program, "uMvp"), texture: this.gl.getUniformLocation(this.program, "uTexture"), useTexture: this.gl.getUniformLocation(this.program, "uUseTexture"), lighting: this.gl.getUniformLocation(this.program, "uLighting"), alphaCutout: this.gl.getUniformLocation(this.program, "uAlphaCutout"), mirrorClampS: this.gl.getUniformLocation(this.program, "uMirrorClampS"), mirrorClampT: this.gl.getUniformLocation(this.program, "uMirrorClampT") };
      this.gl.enable(this.gl.DEPTH_TEST); this.gl.enable(this.gl.CULL_FACE); this.gl.cullFace(this.gl.BACK);
    }
    textureFor(item) {
      if (!item || !item.rgba) return null;
      const key = item.cacheKey || `${item.id ?? "texture"}:${item.rgba}`;
      if (this.textures.has(key)) return this.textures.get(key);
      const glc = this.gl, texture = glc.createTexture(); glc.bindTexture(glc.TEXTURE_2D, texture);
      const binary = Uint8Array.from(atob(item.rgba), (c) => c.charCodeAt(0));
      glc.texImage2D(glc.TEXTURE_2D, 0, glc.RGBA, item.width, item.height, 0, glc.RGBA, glc.UNSIGNED_BYTE, binary);
      glc.texParameteri(glc.TEXTURE_2D, glc.TEXTURE_MIN_FILTER, glc.LINEAR); glc.texParameteri(glc.TEXTURE_2D, glc.TEXTURE_MAG_FILTER, glc.LINEAR); glc.texParameteri(glc.TEXTURE_2D, glc.TEXTURE_WRAP_S, glc.REPEAT); glc.texParameteri(glc.TEXTURE_2D, glc.TEXTURE_WRAP_T, glc.REPEAT);
      this.textures.set(key, texture); return texture;
    }
    applyTextureWrap(texture, wrapS, wrapT) {
      if (!texture) return;
      const glc = this.gl, modes = { repeat: glc.REPEAT, mirror: glc.MIRRORED_REPEAT, "mirror-clamp": glc.CLAMP_TO_EDGE, clamp: glc.CLAMP_TO_EDGE };
      glc.bindTexture(glc.TEXTURE_2D, texture);
      glc.texParameteri(glc.TEXTURE_2D, glc.TEXTURE_WRAP_S, modes[wrapS] || glc.REPEAT);
      glc.texParameteri(glc.TEXTURE_2D, glc.TEXTURE_WRAP_T, modes[wrapT] || glc.REPEAT);
    }
    drawBuffers(vertices, indices, mvp, mode, texture, useTexture, lighting, colorOverride, wrapS, wrapT, alphaCutout = false) {
      if (!vertices.length || !indices.length) return;
      const glc = this.gl, flat = [];
      for (const v of vertices) flat.push(v[0], v[1], v[2]);
      const uv = [], colors = [];
      for (const v of vertices) {
        uv.push(v[3] || 0, v[4] || 0);
        const source = colorOverride || (v.length >= 9 ? v.slice(5, 9) : [255,255,255,255]);
        colors.push(...source.map((x) => (x ?? 255) / 255));
      }
      const normals = []; for (const v of vertices) normals.push(v.length >= 12 ? v[9] : 0, v.length >= 12 ? v[10] : 1, v.length >= 12 ? v[11] : 0);
      const pb = glc.createBuffer(); glc.bindBuffer(glc.ARRAY_BUFFER, pb); glc.bufferData(glc.ARRAY_BUFFER, new Float32Array(flat), glc.STREAM_DRAW); glc.enableVertexAttribArray(this.locations.position); glc.vertexAttribPointer(this.locations.position, 3, glc.FLOAT, false, 0, 0);
      const ub = glc.createBuffer(); glc.bindBuffer(glc.ARRAY_BUFFER, ub); glc.bufferData(glc.ARRAY_BUFFER, new Float32Array(uv), glc.STREAM_DRAW); glc.enableVertexAttribArray(this.locations.uv); glc.vertexAttribPointer(this.locations.uv, 2, glc.FLOAT, false, 0, 0);
      const cb = glc.createBuffer(); glc.bindBuffer(glc.ARRAY_BUFFER, cb); glc.bufferData(glc.ARRAY_BUFFER, new Float32Array(colors), glc.STREAM_DRAW); glc.enableVertexAttribArray(this.locations.color); glc.vertexAttribPointer(this.locations.color, 4, glc.FLOAT, false, 0, 0);
      const nb = glc.createBuffer(); glc.bindBuffer(glc.ARRAY_BUFFER, nb); glc.bufferData(glc.ARRAY_BUFFER, new Float32Array(normals), glc.STREAM_DRAW); glc.enableVertexAttribArray(this.locations.normal); glc.vertexAttribPointer(this.locations.normal, 3, glc.FLOAT, false, 0, 0);
      const ib = glc.createBuffer(); glc.bindBuffer(glc.ELEMENT_ARRAY_BUFFER, ib); glc.bufferData(glc.ELEMENT_ARRAY_BUFFER, new Uint16Array(indices), glc.STREAM_DRAW);
      glc.uniformMatrix4fv(this.locations.mvp, false, new Float32Array(mvp)); glc.uniform1i(this.locations.useTexture, useTexture ? 1 : 0); glc.uniform1i(this.locations.lighting, lighting ? 1 : 0); glc.uniform1i(this.locations.alphaCutout, alphaCutout ? 1 : 0); glc.uniform1i(this.locations.mirrorClampS, wrapS === "mirror-clamp" ? 1 : 0); glc.uniform1i(this.locations.mirrorClampT, wrapT === "mirror-clamp" ? 1 : 0);
      glc.activeTexture(glc.TEXTURE0); glc.bindTexture(glc.TEXTURE_2D, texture || null); glc.uniform1i(this.locations.texture, 0); glc.drawElements(mode, indices.length, glc.UNSIGNED_SHORT, 0);
      [pb, ub, cb, nb, ib].forEach((buffer) => glc.deleteBuffer(buffer));
    }
    render(model, animation, frame, view) {
      if (!gl) return;
      const glc = this.gl, ratio = canvas.clientWidth / Math.max(1, canvas.clientHeight); glc.viewport(0, 0, canvas.width, canvas.height); glc.clearColor(0.035, 0.05, 0.075, 1); glc.clear(glc.COLOR_BUFFER_BIT | glc.DEPTH_BUFFER_BIT); glc.useProgram(this.program);
      if (!model) return;
      const poses = animation && animation.curve && animation.curve.poses ? animation.curve.poses[Math.min(Math.floor(frame), animation.curve.poses.length - 1)] : null;
      // Keep the camera in world space while animation poses change. The
      // previous implementation recomputed the target and fit radius from
      // the current pose every frame, which made the viewport follow and
      // zoom with moving limbs. Anchor framing to the model's base pose once
      // per loaded model; the optional bounds overlay still shows the pose.
      if (!state.cameraAnchor || state.cameraAnchor.model !== model) {
        const baseBounds = computeBounds(model, buildBoneMatrices(model.skeleton?.bones || [], null));
        const baseCenter = [(baseBounds.min[0]+baseBounds.max[0])/2, (baseBounds.min[1]+baseBounds.max[1])/2, (baseBounds.min[2]+baseBounds.max[2])/2];
        const baseRadius = Math.max(0.01, Math.max(baseBounds.max[0]-baseBounds.min[0], baseBounds.max[1]-baseBounds.min[1], baseBounds.max[2]-baseBounds.min[2]) * 0.5);
        state.cameraAnchor = { model, center: baseCenter, radius: baseRadius };
      }
      const center = state.cameraAnchor.center, radius = state.cameraAnchor.radius;
      const cam = state.camera, eye = [center[0] + Math.sin(cam.yaw) * Math.cos(cam.pitch) * cam.distance * radius, center[1] + Math.sin(cam.pitch) * cam.distance * radius, center[2] + Math.cos(cam.yaw) * Math.cos(cam.pitch) * cam.distance * radius];
      const target = [center[0] + cam.panX * radius, center[1] + cam.panY * radius, center[2]]; const viewM = lookAt(eye, target, [0,1,0]); const boneMatrices = buildBoneMatrices(model.skeleton?.bones || [], poses, viewM); const bounds = computeBounds(model, boneMatrices); const projection = mat4Perspective(Math.PI / 4, ratio, 0.01, Math.max(1000, cam.distance * radius * 10)); const mvp = mat4Multiply(projection, viewM);
      const prepared = [];
      for (const mesh of model.meshes || []) {
        if (!mesh.indices?.length) continue;
        const material = mesh.material || {};
        const textureItem = textureItemFor(model, animation, frame, material);
        const texture = view.textures ? this.textureFor(textureItem) : null;
        this.applyTextureWrap(texture, material.wrapS, material.wrapT);
        const outVertices = [];
        for (const vertex of mesh.vertices || []) {
          let position = vertex.position || [0,0,0];
          // The N64 RSP transforms vertices when a display list loads them
          // (G_VTX), not when a later list's triangles reference them, so each
          // vertex carries the bone that was active at load time.
          const boneId = (vertex.bone !== null && vertex.bone !== undefined) ? vertex.bone : mesh.bone;
          const bone = (boneId !== null && boneId !== undefined) ? boneMatrices.get(boneId) : null;
          if (bone) position = vec3Transform(bone, position);
          const uv = vertex.uv || [0,0];
          // N64 Vtx s/t values are 5-bit fixed-point texel coordinates. The
          // shader needs normalized coordinates for the selected descriptor.
          const normalizedUv = textureItem ? [uv[0] / Math.max(1, textureItem.width || 1), uv[1] / Math.max(1, textureItem.height || 1)] : uv;
          let n = vertex.normal || [0, 1, 0];
          if (bone) n = normalize([bone[0]*n[0] + bone[4]*n[1] + bone[8]*n[2], bone[1]*n[0] + bone[5]*n[1] + bone[9]*n[2], bone[2]*n[0] + bone[6]*n[1] + bone[10]*n[2]]);
          outVertices.push([position[0], position[1], position[2], ...normalizedUv, ...(vertex.color || [255,255,255,255]), n[0], n[1], n[2]]);
        }
        let alphaMode = material.alphaMode || textureItem?.alphaMode || (textureItem?.hasAlpha ? "blend" : "opaque");
        if (material.translucent) alphaMode = "blend";
        prepared.push({ mesh, material, texture, outVertices, alphaMode, translucent: alphaMode === "blend" });
      }
      const drawMesh = (entry) => {
        const { mesh, material, texture, outVertices } = entry;
        const expressionLayer = material.renderLayer === 'expression';
        if (expressionLayer) { glc.disable(glc.DEPTH_TEST); glc.depthMask(false); }
        else glc.enable(glc.DEPTH_TEST);
        if (material.doubleSided) glc.disable(glc.CULL_FACE); else glc.enable(glc.CULL_FACE); if (entry.translucent) { glc.enable(glc.BLEND); glc.blendFunc(glc.SRC_ALPHA, glc.ONE_MINUS_SRC_ALPHA); } else glc.disable(glc.BLEND);
        // Lit N64 Vtx records carry normals in the final four bytes. The
        // source display lists select lighting, so use the Geo cmd23 color
        // (normally white) rather than exposing those normal bytes as colors.
        const color = material.lighting ? (material.color || [255,255,255,255]) :
          (material.texture !== null && material.texture !== undefined ? (material.color || [255,255,255,255]) : null);
        this.drawBuffers(outVertices, mesh.indices, mvp, glc.TRIANGLES, texture, !!texture && view.textures, view.lighting && material.lighting !== false, color, material.wrapS, material.wrapT, entry.alphaMode === "cutout");
      };
      // Source-like translucent ordering: the RDP draws opaque surfaces first
      // (RM_AA_OPA_SURF, depth compare + update) and alpha-blended
      // RM_AA_XLU_SURF surfaces last, still testing depth but never writing
      // it, so translucent layers cannot punch holes in the opaque body.
      for (const entry of prepared) if (!entry.translucent && entry.material.renderLayer !== 'expression') drawMesh(entry);
      glc.depthMask(false);
      for (const entry of prepared) if (entry.translucent && entry.material.renderLayer !== 'expression') drawMesh(entry);
      for (const entry of prepared) if (entry.material.renderLayer === 'expression') drawMesh(entry);
      glc.enable(glc.DEPTH_TEST);
      glc.depthMask(true);
      glc.disable(glc.BLEND);
      if (view.wireframe) { glc.disable(glc.CULL_FACE); for (const entry of prepared) this.drawBuffers(entry.outVertices, triangleLines(entry.mesh.indices), mvp, glc.LINES, null, false, false, [110, 220, 205, 255]); }
      if (view.axes) this.drawLineSet(axisLines(radius * 1.25), mvp, [255,255,255,255]);
      if (view.bounds) this.drawLineSet(boxLines(bounds), mvp, [245,199,106,255]);
      if (view.skeleton && model.skeleton?.bones?.length) this.drawLineSet(skeletonLines(model.skeleton.bones, boneMatrices), mvp, [138,180,255,255]);
      updateBoneLabels(model.skeleton?.bones || [], boneMatrices, mvp, view.skeleton && view.boneNames);
    }
    drawLineSet(lines, mvp, color) { const verts = []; const indices = []; lines.forEach((line, i) => { verts.push([line[0][0],line[0][1],line[0][2],0,0,255,255,255,255], [line[1][0],line[1][1],line[1][2],0,0,255,255,255,255]); indices.push(i*2, i*2+1); }); this.drawBuffers(verts, indices, mvp, this.gl.LINES, null, false, false, color); }
  }

  function lookAt(eye, target, up) {
    let z = normalize([eye[0]-target[0], eye[1]-target[1], eye[2]-target[2]]), x = normalize(cross(up, z)), y = cross(z, x); return [x[0],y[0],z[0],0, x[1],y[1],z[1],0, x[2],y[2],z[2],0, -dot(x,eye),-dot(y,eye),-dot(z,eye),1];
  }
  function cross(a,b) { return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]; }
  function dot(a,b) { return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]; }
  function normalize(v) { const l = Math.hypot(...v) || 1; return v.map((x)=>x/l); }
  function computeBounds(model, boneMatrices) {
    const min=[Infinity,Infinity,Infinity], max=[-Infinity,-Infinity,-Infinity];
    let meshes = model.meshes || [];
    // Blended display-list branches are commonly effects/shadows rather than
    // the model silhouette. Keep drawing them, but do not let an S2 billboard
    // with the 0xFFFFFFFF unit-scale sentinel push the camera fit off-screen.
    const solidMeshes = meshes.filter((mesh)=>{
      const material = mesh.material || {};
      return !material.translucent && material.alphaMode !== 'blend';
    });
    if (solidMeshes.length) meshes = solidMeshes;
    meshes.forEach((mesh)=>{
      (mesh.vertices||[]).forEach((v)=>{
        const boneId=(v.bone !== null && v.bone !== undefined) ? v.bone : mesh.bone;
        const bone=(boneId !== null && boneId !== undefined) ? boneMatrices?.get(boneId) : null;
        const p=bone ? vec3Transform(bone, v.position||[0,0,0]) : (v.position||[0,0,0]);
        p.forEach((x,i)=>{ min[i]=Math.min(min[i],x); max[i]=Math.max(max[i],x); });
      });
    });
    if (!Number.isFinite(min[0])) return {min:[-1,-1,-1],max:[1,1,1]};
    return {min,max};
  }
  function triangleLines(indices) { const out=[]; for(let i=0;i+2<indices.length;i+=3) out.push(indices[i],indices[i+1], indices[i+1],indices[i+2], indices[i+2],indices[i]); return out; }
  function axisLines(size) { return [[[0,0,0],[size,0,0]], [[0,0,0],[0,size,0]], [[0,0,0],[0,0,size]]]; }
  function boxLines(b) { const a=b.min,c=b.max; const p=[[a[0],a[1],a[2]],[c[0],a[1],a[2]],[c[0],c[1],a[2]],[a[0],c[1],a[2]],[a[0],a[1],c[2]],[c[0],a[1],c[2]],[c[0],c[1],c[2]],[a[0],c[1],c[2]]]; return [[p[0],p[1]],[p[1],p[2]],[p[2],p[3]],[p[3],p[0]],[p[4],p[5]],[p[5],p[6]],[p[6],p[7]],[p[7],p[4]],[p[0],p[4]],[p[1],p[5]],[p[2],p[6]],[p[3],p[7]]]; }
  function skeletonLines(bones, matrices) { return bones.filter((b)=>b.parent !== null && matrices.has(b.id) && matrices.has(b.parent)).map((b)=>[vec3Transform(matrices.get(b.id),[0,0,0]),vec3Transform(matrices.get(b.parent),[0,0,0])]); }
  function updateBoneLabels(bones, matrices, mvp, enabled) { const root=$('bone-labels'); root.innerHTML=''; if(!enabled) return; bones.forEach((b)=>{ const p=vec3Transform(matrices.get(b.id)||mat4Identity(),[0,0,0]); const clip=project(p,mvp); if(clip[3]>0){ const x=clip[0]/clip[3], y=clip[1]/clip[3]; const el=document.createElement('span'); el.className='bone-label'; el.textContent=b.name||`bone_${b.id}`; el.style.left=`${(x*.5+.5)*100}%`; el.style.top=`${(1-(y*.5+.5))*100}%`; root.appendChild(el); } }); }
  function project(p,m) { return [m[0]*p[0]+m[4]*p[1]+m[8]*p[2]+m[12],m[1]*p[0]+m[5]*p[1]+m[9]*p[2]+m[13],m[2]*p[0]+m[6]*p[1]+m[10]*p[2]+m[14],m[3]*p[0]+m[7]*p[1]+m[11]*p[2]+m[15]]; }

  function textureItemFor(model, animation, frame, material) {
    const textures = model.textures || [];
    let descriptor = material.textureDescriptor;
    const track = animation?.eventTrack;
    const slot = Number(material.textureAnimIndex);
    if (track?.supported && Number.isInteger(slot) && slot >= 0 && slot < (track.slotCount || 0) && track.mapping?.length) {
      const segment = track.segments?.[slot];
      if (segment) {
        const sourceFrame = Math.max(0, Math.min(Math.floor(frame), Math.max(0, (track.frameCount || 1) - 1)));
        const mappingIndex = sourceFrame < segment[0] ? sourceFrame + segment[1] : segment[0] + segment[1] - 1;
        const mapped = track.mapping[mappingIndex];
        if (Number.isInteger(mapped)) descriptor = mapped;
      }
    }
    if (Number.isInteger(descriptor)) {
      const selected = textures.find((item) => item.descriptor === descriptor);
      if (selected) {
        const palette = Number(material.textureSecondDescriptor);
        const key = Number.isInteger(palette) ? String(palette) : null;
        const rgba = key && selected.paletteVariants ? selected.paletteVariants[key] : null;
        if (rgba) {
          return {
            ...selected,
            rgba,
            hasAlpha: !!selected.paletteAlpha?.[key],
            cacheKey: `${selected.id}:palette:${key}`,
          };
        }
        return selected;
      }
    }
    return material.texture !== null && material.texture !== undefined ? textures[material.texture] : null;
  }

  if (gl) { try { renderer = new WebGLRenderer(gl); } catch (e) { $('diagnostics').textContent = `WebGL initialization failed: ${e.message}`; } }
  else $('diagnostics').textContent = 'WebGL is unavailable in this browser; resource browsing and diagnostics remain available.';

  function renderProviderTabs() {
    const root = $('provider-tabs');
    if (!state.dual) { root.hidden = true; root.innerHTML = ''; return; }
    root.hidden = false;
    root.innerHTML = state.providers.map((info) => `<button class="${info.provider === state.activeProvider ? 'active' : ''}" data-provider="${escapeHtml(info.provider)}">${escapeHtml(providerName(info.provider))}</button>`).join('');
    root.querySelectorAll('[data-provider]').forEach((el) => el.addEventListener('click', () => activateProvider(el.dataset.provider)));
  }
  function updateProviderHeader() {
    const info = state.providers.find((item) => item.provider === state.activeProvider);
    const label = providerName(state.activeProvider).toUpperCase();
    $('provider-label').textContent = `${label} / RESOURCE INSPECTOR`;
    $('health').textContent = info ? `${info.provider} · ${info.modelCount} models` : 'Provider loading…';
    document.title = `Pokémon ${label} Model Viewer`;
  }
  function renderCatalog() {
    const query = $('search').value.trim().toLowerCase();
    const queryTokens = query.split(/\s+/).filter(Boolean);
    const matchesQuery = (value) => !queryTokens.length || queryTokens.every((token) => value.includes(token));
    const items = state.catalog.filter((item) => matchesQuery(JSON.stringify(item).toLowerCase()));
    $('catalog-status').textContent = `${items.length} model resource${items.length === 1 ? '' : 's'}${state.catalog.length !== items.length ? ` · ${state.catalog.length} total` : ''}`;
    $('model-list').innerHTML = items.map((item) => `<div class="resource-item ${state.model && state.model._path === item.path ? 'selected' : ''}" data-path="${escapeHtml(item.path)}"><div class="resource-name">${escapeHtml(item.name)}</div><div class="resource-meta">${item.animations?.length || 0} animation slot${(item.animations?.length || 0) === 1 ? '' : 's'} · ${item.size || 0} bytes</div>${(item.diagnostics || []).some((d) => d.severity === 'error') ? '<div class="resource-warn">resource has diagnostics</div>' : ''}</div>`).join('') || '<div class="muted">No matching model resources.</div>';
    document.querySelectorAll('.resource-item').forEach((el) => el.addEventListener('click', () => loadModel(el.dataset.path)));
    const animations = [];
    state.catalog.forEach((item) => (item.animations || []).forEach((a) => animations.push({ ...a, modelPath: item.path, modelName: item.name })));
    const filtered = animations.filter((a) => {
      const searchable = `${JSON.stringify(a)} ${a.modelName}`.toLowerCase();
      return matchesQuery(searchable);
    });
    $('animation-list').innerHTML = filtered.map((a) => `<div class="animation-item ${a.supported ? '' : 'unsupported'}" data-model="${escapeHtml(a.modelPath)}" data-animation="${a.id}"><span>${escapeHtml(a.name)} <small>(${escapeHtml(a.modelName)})</small></span><span>${a.frameCount}f</span></div>`).join('') || '<span>No matching animations.</span>';
    document.querySelectorAll('.animation-item').forEach((el) => el.addEventListener('click', async () => loadModel(el.dataset.model, Number(el.dataset.animation))));
  }
  function renderAnimationList() {
    renderCatalog();
    if (!state.model) return;
    const list = state.model.animations || [];
    const staticSelected = !state.animation;
    const staticRow = `<div class="animation-item ${staticSelected ? 'selected' : ''}" data-animation="-1"><span>Static/base pose</span><span>—</span></div>`;
    $('animation-list').innerHTML = staticRow + (list.map((a) => `<div class="animation-item ${a.supported ? '' : 'unsupported'} ${state.animation && state.animation.id === a.id ? 'selected' : ''}" data-animation="${a.id}"><span>${escapeHtml(a.name)}</span><span>${a.frameCount}f</span></div>`).join('') || '<span>No animation slots found.</span>');
    document.querySelectorAll('.animation-item').forEach((el) => el.addEventListener('click', () => { const id = Number(el.dataset.animation); selectAnimation(id < 0 ? null : id); }));
  }
  async function loadModel(path, animationId) {
    try {
      $('selection-info').textContent = 'Loading…';
      const model = await fetchJson(`/api/model?provider=${encodeURIComponent(state.activeProvider)}&path=${encodeURIComponent(path)}`);
      model._path = path; state.model = model; state.cameraAnchor = null; state.animation = null; state.frame = 0; state.playing = false; $('viewport-empty').style.display = 'none';
      if (animationId !== undefined) selectAnimation(animationId);
      else { const first = (model.animations || []).find((item) => item.supported); selectAnimation(first ? first.id : null); }
    } catch (e) { $('selection-info').textContent = `Unable to load resource: ${e.message}`; }
  }
  function selectAnimation(id) {
    if (!state.model) return;
    state.animation = id === null || id === undefined || id < 0 ? null : (state.model.animations || []).find((a) => a.id === id) || null;
    state.frame = 0; state.playing = !!state.animation?.supported; persistActiveTab(); updateUi(); renderAnimationList();
  }
  function updateUi() {
    const model = state.model, animation = state.animation;
    $('play').textContent = state.playing ? 'Pause' : 'Play'; $('timeline').max = String(Math.max(0, (animation?.frameCount || 1) - 1)); $('timeline').value = String(Math.floor(state.frame)); $('speed-value').textContent = `${state.speed.toFixed(1)}×`;
    if (model) { const frameCount = animation?.frameCount || 0; $('selection-info').innerHTML = `<div class="title">${escapeHtml(model.name || model._path)}</div><div class="sub">Model ID ${model.modelId ?? '—'} · ${model.meshes?.length || 0} mesh${(model.meshes?.length || 0) === 1 ? '' : 'es'} · ${model.skeleton?.bones?.length || 0} bones</div><div class="sub">Animation: ${escapeHtml(animation?.name || 'none')} · ID ${animation?.id ?? '—'} · ${frameCount} frames · current ${frameCount ? Math.floor(state.frame) : '—'}</div>`; renderDiagnostics(model); }
    else { $('selection-info').textContent = 'No model selected.'; }
  }
  function renderDiagnostics(model) { const all=[...(model.diagnostics||[])]; if(model.animationSlotCount && !model.animations?.some((a)=>a.supported)) all.push({severity:'warning',code:'animation-unavailable',message:'Animation slots exist, but no curve was decoded for playback.'}); $('diagnostics').innerHTML=all.length?all.map((d)=>`<div class="diag ${d.severity==='error'?'error':''}"><code>${escapeHtml(d.code)}</code> — ${escapeHtml(d.message)}</div>`).join(''):'<span>No missing or unsupported resources reported.</span>'; }
  function resize() { const dpr=Math.min(2,window.devicePixelRatio||1), rect=canvas.getBoundingClientRect(); canvas.width=Math.max(1,Math.floor(rect.width*dpr)); canvas.height=Math.max(1,Math.floor(rect.height*dpr)); }
  function frameStep(delta) { const count=state.animation?.frameCount||1; state.frame=clamp(Math.floor(state.frame)+delta,0,Math.max(0,count-1)); persistActiveTab(); updateUi(); }

  async function restoreSavedModel(provider) {
    const saved = savedSelection(provider);
    if (!saved?.modelPath || !state.catalog.some((item) => item.path === saved.modelPath)) return;
    const animationId = Object.prototype.hasOwnProperty.call(saved, 'animationId') ? saved.animationId : undefined;
    await loadModel(saved.modelPath, animationId);
    if (Number.isFinite(saved.frame) && state.animation?.frameCount) state.frame = clamp(Number(saved.frame), 0, state.animation.frameCount - 1);
    persistActiveTab(); updateUi();
  }
  async function activateProvider(provider) {
    if (!state.providers.some((item) => item.provider === provider)) return;
    if (provider === state.activeProvider && state.catalog.length) return;
    persistActiveTab(); state.activeProvider = provider; restoreTab(provider); state.cameraAnchor = null;
    renderProviderTabs(); updateProviderHeader(); renderCatalog(); renderAnimationList(); updateUi();
    if (!state.model) await restoreSavedModel(provider);
  }
  async function initializeViewer() {
    const health = await fetchJson('/api/health');
    state.dual = health.mode === 'dual';
    state.providers = state.dual ? (health.providers || []) : [health];
    if (!state.providers.length) throw new Error('no providers were returned by the server');
    state.activeProvider = health.defaultProvider || state.providers[0].provider;
    state.providers.forEach((info) => ensureTab(info.provider));
    renderProviderTabs(); updateProviderHeader();
    await Promise.all(state.providers.map(async (info) => {
      try {
        const data = await fetchJson(`/api/catalog?provider=${encodeURIComponent(info.provider)}`);
        ensureTab(info.provider).catalog = data.models || [];
      } catch (error) {
        ensureTab(info.provider).catalog = [];
        ensureTab(info.provider).catalogError = error.message;
      }
    }));
    restoreTab(state.activeProvider); renderProviderTabs(); renderCatalog(); renderAnimationList(); updateUi();
    await restoreSavedModel(state.activeProvider);
  }

  $('search').addEventListener('input',renderCatalog); $('play').addEventListener('click',()=>{if(state.animation?.supported){state.playing=!state.playing;persistActiveTab();updateUi();}}); $('step-back').addEventListener('click',()=>frameStep(-1)); $('step-forward').addEventListener('click',()=>frameStep(1)); $('timeline').addEventListener('input',(e)=>{state.frame=Number(e.target.value);state.playing=false;persistActiveTab();updateUi();}); $('loop').addEventListener('change',(e)=>state.loop=e.target.checked); $('speed').addEventListener('input',(e)=>{state.speed=Number(e.target.value);updateUi();});
  ['textures','lighting','wireframe','axes','bounds','skeleton','boneNames'].forEach((name)=>{ const id=`toggle-${name.toLowerCase()}`; const el=$(id); if(el) el.addEventListener('change',(e)=>{state.view[name]=e.target.checked;}); });
  document.querySelectorAll('[data-preset]').forEach((el)=>el.addEventListener('click',()=>{const p=el.dataset.preset;state.camera.yaw=p==='side'?Math.PI/2:p==='top'?0:0;state.camera.pitch=p==='top'?Math.PI/2-.02:0.05;})); $('reset-camera').addEventListener('click',()=>{state.camera={yaw:.55,pitch:.25,distance:5,panX:0,panY:0};});
  let drag=null; canvas.addEventListener('pointerdown',(e)=>{drag={x:e.clientX,y:e.clientY,button:e.button,shift:e.shiftKey};canvas.setPointerCapture(e.pointerId);}); canvas.addEventListener('pointermove',(e)=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;drag.x=e.clientX;drag.y=e.clientY;if(drag.shift||drag.button===2){state.camera.panX+=dx*.003;state.camera.panY-=dy*.003;}else{state.camera.yaw+=dx*.008;state.camera.pitch=clamp(state.camera.pitch+dy*.008,-1.5,1.5);}}); canvas.addEventListener('pointerup',()=>drag=null); canvas.addEventListener('contextmenu',(e)=>e.preventDefault()); canvas.addEventListener('wheel',(e)=>{e.preventDefault();state.camera.distance=clamp(state.camera.distance*(1+e.deltaY*.001),1,30);},{passive:false}); window.addEventListener('resize',resize); window.addEventListener('keydown',(e)=>{if(e.key==='1'){state.camera.yaw=0;state.camera.pitch=.05;}if(e.key==='2'){state.camera.yaw=Math.PI/2;state.camera.pitch=.05;}if(e.key==='3'){state.camera.yaw=0;state.camera.pitch=Math.PI/2-.02;}if(e.key.toLowerCase()==='r')state.camera={yaw:.55,pitch:.25,distance:5,panX:0,panY:0};});

  function tick(time) { const dt=state.lastTime?Math.min(.1,(time-state.lastTime)/1000):0;state.lastTime=time;if(state.playing&&state.animation?.supported){state.frame+=dt*30*state.speed;const end=Math.max(1,state.animation.frameCount);if(state.frame>=end){if(state.loop)state.frame%=end;else{state.frame=end-1;state.playing=false;}}updateUi();}resize();if(renderer)renderer.render(state.model,state.animation,state.frame,state.view);requestAnimationFrame(tick); }
  // Deep links: ?provider=stadium2&model=s2-model:150&animation=0&frame=0
  // (animation -1 selects the static/base pose). Optional view toggles:
  // &axes=0&skeleton=1&bonenames=1
  const deepLink=new URLSearchParams(location.search); ['axes','textures','lighting','wireframe','bounds','skeleton'].forEach((name)=>{if(deepLink.has(name))state.view[name]=deepLink.get(name)!=='0';}); if(deepLink.has('bonenames'))state.view.boneNames=deepLink.get('bonenames')!=='0'; if(deepLink.has('yaw'))state.camera.yaw=Number(deepLink.get('yaw'))||0; if(deepLink.has('pitch'))state.camera.pitch=Number(deepLink.get('pitch'))||0; if(deepLink.has('distance'))state.camera.distance=clamp(Number(deepLink.get('distance'))||5,1,30);
  initializeViewer().then(async()=>{const requestedProvider=deepLink.get('provider'); if(requestedProvider && state.dual) await activateProvider(requestedProvider); if(deepLink.get('model')){await loadModel(deepLink.get('model'),deepLink.has('animation')?Number(deepLink.get('animation')):undefined); if(deepLink.has('frame')){state.frame=clamp(Number(deepLink.get('frame'))||0,0,Math.max(0,(state.animation?.frameCount||1)-1));persistActiveTab();updateUi();}}}).catch((e)=>{$('health').textContent=`Provider error: ${e.message}`;$('catalog-status').textContent='Catalog unavailable.';}); updateUi(); requestAnimationFrame(tick);
})();
