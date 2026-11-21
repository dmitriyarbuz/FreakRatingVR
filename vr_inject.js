(() => {
  const e = window.__vr_engine;
  const r = (e && e.renderer) || window.__vr_renderer || null;
  if (!r || !e || !e.camera) return { ok: false, error: 'renderer or engine missing', r: !!r, e: !!e };
  const cam = e.camera;

  // ---- Lock renderer pixelRatio so the game's auto-governor cannot lower it ----
  // The game's dynamic-resolution governor re-applies a ratio between 0.75 and 1
  // every frame to hit its 45 FPS target, which makes the canvas shrink and the
  // VR image judder/blur. We clamp setPixelRatio to >= 1.25 and stop the governor.
  try {
    if (r.setPixelRatio && !r.__vr_spr_hooked) {
      const _spr = r.setPixelRatio.bind(r);
      r.setPixelRatio = function (v) { return _spr(Math.max(1.25, (typeof v === 'number' ? v : 1.25))); };
      r.__vr_spr_hooked = true;
    }
    if (r.setPixelRatio) r.setPixelRatio(1.25);
    if (e.graphics) {
      e.graphics.autoOn = false;
      const gov = e.graphics.governor;
      if (gov) {
        gov.ratio = 1.25;
        if (!gov.__vr_frame_off) { gov.frame = function () {}; gov.__vr_frame_off = true; }
      }
    }
  } catch (err) {}

  // ---- Constants exposed for live tuning ----
  window.__vr_ipd = typeof window.__vr_ipd === 'number' ? window.__vr_ipd : 0.1;
  window.__vr_fov = typeof window.__vr_fov === 'number' ? window.__vr_fov : 120;
  window.__vr_aspect = 1.0;
  window.__vr_smooth = typeof window.__vr_smooth === 'number' ? window.__vr_smooth : 0.15;
  window.__vr_lead_ms = typeof window.__vr_lead_ms === 'number' ? window.__vr_lead_ms : 0;

  // ---- Force square projection and configurable FOV ----
  if (!cam.__vr_aspect_forced) {
    let storedAspect = cam.aspect;
    Object.defineProperty(cam, 'aspect', {
      get() { return typeof window.__vr_aspect === 'number' ? window.__vr_aspect : storedAspect; },
      set(v) { storedAspect = v; }, configurable: true
    });
    cam.__vr_aspect_forced = true;
  }
  if (!cam.__vr_fov_forced) {
    let storedFov = cam.fov;
    Object.defineProperty(cam, 'fov', {
      get() { return typeof window.__vr_fov === 'number' ? window.__vr_fov : storedFov; },
      set(v) { storedFov = v; }, configurable: true
    });
    cam.__vr_fov_forced = true;
  }
  cam.updateProjectionMatrix();

  // ---- helpers ----
  const makeVec4 = () => ({
    x: 0, y: 0, z: 0, w: 0,
    copy(v) { this.x = v.x; this.y = v.y; this.z = v.z; this.w = v.w; return this; }
  });
  const orig = e.__vr_origRenderScene || e.renderScene;

  // Quaternion slerp; t > 1 extrapolates along the great circle.
  const qslerp = (a, b, t) => {
    let ax=a[0], ay=a[1], az=a[2], aw=a[3];
    let bx=b[0], by=b[1], bz=b[2], bw=b[3];
    let dot = ax*bx + ay*by + az*bz + aw*bw;
    if (dot < 0) { bx=-bx; by=-by; bz=-bz; bw=-bw; dot=-dot; }
    if (dot > 0.9995) {
      const r = [ax+(bx-ax)*t, ay+(by-ay)*t, az+(bz-az)*t, aw+(bw-aw)*t];
      const n = Math.hypot(r[0],r[1],r[2],r[3]) || 1;
      return r.map(v => v/n);
    }
    const th = Math.acos(Math.min(1, dot)), s = Math.sin(th);
    const wa = Math.sin((1-t)*th)/s, wb = Math.sin(t*th)/s;
    const r = [wa*ax+wb*bx, wa*ay+wb*by, wa*az+wb*bz, wa*aw+wb*bw];
    const n = Math.hypot(r[0],r[1],r[2],r[3]) || 1;
    return r.map(v => v/n);
  };
  const poseHist = [];
  window.__vr_poseHist = poseHist;
  window.__vr_qslerp = qslerp;

  // ---- 3D hands that follow the real controllers ----
  // Constructors are borrowed from an existing scene mesh (no global THREE).
  window.__vr_hands = window.__vr_hands || {};
  window.__vr_handVisible = typeof window.__vr_handVisible === 'number'
    ? window.__vr_handVisible : 1;
  window.__vr_handScale = typeof window.__vr_handScale === 'number'
    ? window.__vr_handScale : 1;
  let handL = null, handR = null;
  function buildHands() {
    if (handL && handR) return true;
    const scene = e.scene;
    if (!scene || typeof scene.traverse !== 'function') return false;
    let sample = null;
    scene.traverse(o => { if (!sample && o.isMesh) sample = o; });
    if (!sample) return false;
    const Mesh = sample.constructor;
    const Box = sample.geometry ? sample.geometry.constructor : null;
    const Mat = sample.material ? sample.material.constructor : null;
    let Group = null;
    scene.traverse(o => { if (!Group && o.isGroup) Group = o.constructor; });
    if (!Box || !Mat) return false;
    if (!Group) Group = function () { this.isGroup = true; this.children = []; };

    const makeMat = (color) => {
      const m = new Mat({ color });
      if ('depthTest' in m) m.depthTest = true;
      if ('transparent' in m) m.transparent = false;
      return m;
    };
    const box = (w, h, d, mat, x, y, z) => {
      const mesh = new Mesh(new Box(w, h, d), mat);
      mesh.position.set(x, y, z);
      return mesh;
    };
    const makeHand = (color) => {
      const g = new Group();
      const mat = makeMat(color);
      // palm
      g.add(box(0.09, 0.025, 0.11, mat, 0, 0, -0.02));
      // four fingers pointing forward (-Z)
      for (let i = 0; i < 4; i++) {
        g.add(box(0.018, 0.02, 0.07, mat, -0.034 + i * 0.023, 0, -0.09));
      }
      // thumb off to the side
      const thumb = box(0.02, 0.02, 0.055, mat, 0.055, 0, -0.03);
      thumb.rotation.y = -0.5;
      g.add(thumb);
      cam.add(g);
      return g;
    };
    handL = makeHand(0x46c8ff);
    handR = makeHand(0xff8a46);
    return true;
  }
  // Try now; if the scene mesh isn't ready yet, retry each frame in the wrapper.
  buildHands();
  window.__vr_handsReady = () => !!(handL && handR);

  // ---- SBS stereo renderer ----
  if (!e.__vr_origRenderScene) {
    e.__vr_origRenderScene = orig;
    e.renderScene = function () {
      if (!window.__vr_stereo) return orig.call(e);
      const W = r.domElement.clientWidth || Math.round(r.domElement.width / r.getPixelRatio());
      const H = r.domElement.clientHeight || Math.round(r.domElement.height / r.getPixelRatio());
      if (!W || !H) return orig();
      const eyeW = W / 2;
      // Single source of truth: the runtime-tunable value from above.
      // (The old 0.064 fallback here silently disagreed with window.__vr_ipd.)
      const ipd = typeof window.__vr_ipd === 'number' ? window.__vr_ipd : 0.1;
      const oldEye = e.view.eye.clone();
      const oldCamPos = cam.position.clone();
      const oldAspect = cam.aspect;
      const oldProj = cam.projectionMatrix.clone();
      const oldProjInv = cam.projectionMatrixInverse.clone();
      const oldViewport = makeVec4(); r.getViewport(oldViewport);
      const oldScissor = makeVec4(); r.getScissor(oldScissor);
      const oldScissorTest = r.getScissorTest();
      const oldAutoClear = r.autoClear;
      const oldAutoClearColor = r.autoClearColor;
      const oldAutoClearDepth = r.autoClearDepth;
      const oldAutoClearStencil = r.autoClearStencil;
      const right = oldCamPos.clone().set(1, 0, 0).applyQuaternion(cam.quaternion).normalize();
      const renderEye = (sign, x) => {
        e.view.eye.copy(oldEye).addScaledVector(right, sign * ipd / 2);
        cam.position.copy(e.view.eye);
        cam.updateMatrixWorld(true);
        r.setViewport(x, 0, eyeW, H);
        r.setScissor(x, 0, eyeW, H);
        r.setScissorTest(true);
        r.autoClear = true;
        r.autoClearColor = true;
        r.autoClearDepth = true;
        r.autoClearStencil = true;
        orig.call(e);
      };
      try {
        cam.updateProjectionMatrix();
        renderEye(-1, 0);
        renderEye(1, eyeW);
        window.__vr_stats = { W, H, eyeW, ipd,
          eye: { x: oldEye.x, y: oldEye.y, z: oldEye.z },
          aspect: cam.aspect, active: true };
      } finally {
        e.view.eye.copy(oldEye);
        cam.position.copy(oldCamPos);
        cam.aspect = oldAspect;
        cam.projectionMatrix.copy(oldProj);
        cam.projectionMatrixInverse.copy(oldProjInv);
        cam.updateMatrixWorld(true);
        r.setViewport(oldViewport.x, oldViewport.y, oldViewport.z, oldViewport.w);
        r.setScissor(oldScissor.x, oldScissor.y, oldScissor.z, oldScissor.w);
        r.setScissorTest(oldScissorTest);
        r.autoClear = oldAutoClear;
        r.autoClearColor = oldAutoClearColor;
        r.autoClearDepth = oldAutoClearDepth;
        r.autoClearStencil = oldAutoClearStencil;
      }
    };
    window.__vr_stereo = true;
  }

  // ---- Apply head pose before SBS render ----
  if (!e.__vr_pose_wrapped) {
    const prev = e.renderScene;
    e.renderScene = function () {
      const pose = window.__vr_pose;
      if (pose && pose.q) {
        // Keep the game's own body yaw in sync with the stick-driven turn.
        if (window.__vr_baseYaw == null) window.__vr_baseYaw = e.yaw || 0;
        let q = pose.q;
        const lead = (window.__vr_lead_ms || 0) / 1000;
        if (lead > 0 && poseHist.length >= 2) {
          const a = poseHist[poseHist.length - 2];
          const b = poseHist[poseHist.length - 1];
          const dt = (b.t - a.t) / 1000;
          if (dt > 0.002 && dt < 0.25) {
            const extra = (performance.now() - b.t) / 1000 + lead;
            q = qslerp(a.q, b.q, Math.min(1 + Math.min(extra, 0.1) / dt, 2.5));
          }
        }
        // Low-pass the camera pose so consecutive game frames change smoothly.
        // The compositor's timewarp then handles the fast head motion on top.
        const alpha = (typeof window.__vr_smooth === 'number') ? window.__vr_smooth : 0.15;
        let yaw = pose.yaw || 0;
        if (alpha > 0) {
          if (!window.__vr_smoothedQ) window.__vr_smoothedQ = q.slice();
          window.__vr_smoothedQ = qslerp(window.__vr_smoothedQ, q, Math.min(1, alpha));
          q = window.__vr_smoothedQ;
          // Body yaw from the right stick is updated in discrete ~30Hz steps;
          // low-pass it the same way so stick turns don't jerk the camera.
          if (window.__vr_smoothedYaw == null) window.__vr_smoothedYaw = yaw;
          let d = yaw - window.__vr_smoothedYaw;
          while (d > Math.PI) d -= 2 * Math.PI;
          while (d < -Math.PI) d += 2 * Math.PI;
          window.__vr_smoothedYaw += d * Math.min(1, alpha);
          yaw = window.__vr_smoothedYaw;
        } else {
          window.__vr_smoothedQ = null;
          window.__vr_smoothedYaw = null;
        }
        // Track the real head yaw as a continuous angle. Do NOT fold it into
        // e.yaw: e.yaw drives the local camera (via view.place and the camera
        // parent), so changing it would turn the view sideways. Instead the
        // net pose() wrapper below adds it only to what other players receive.
        let headYaw = Math.atan2(2 * (q[3] * q[1] + q[0] * q[2]),
                                1 - 2 * (q[1] * q[1] + q[2] * q[2]));
        if (window.__vr_headYawAcc == null) {
          window.__vr_headYawAcc = headYaw;
          window.__vr_headYawBase = headYaw;
        }
        else {
          let dh = headYaw - window.__vr_headYawPrev;
          while (dh > Math.PI) dh -= 2 * Math.PI;
          while (dh < -Math.PI) dh += 2 * Math.PI;
          window.__vr_headYawAcc += dh;
        }
        window.__vr_headYawPrev = headYaw;
        e.yaw = window.__vr_baseYaw + yaw;
        // Report the exact pose this frame was rendered from so the bridge can
        // tag the submitted texture with it (keeps timewarp reference correct).
        window.__vr_render_pose = { q: q.slice(), p: pose.p, t: performance.now() };
        if (window.__vr_ws && window.__vr_ws.readyState === 1) {
          try { window.__vr_ws.send(JSON.stringify({ renderPose: window.__vr_render_pose })); } catch (e) {}
        }
        const x = q[0], y = q[1], z = q[2], w = q[3];
        const sy = Math.sin(yaw * 0.5), cw = Math.cos(yaw * 0.5);
        // qYaw (around world Y) * qHead
        cam.quaternion.set(
          cw * x + sy * z,
          cw * y + sy * w,
          cw * z - sy * x,
          cw * w - sy * y
        );
        cam.updateMatrixWorld(true);
      }
      // Update 3D hands from controller poses (head-local frame). Parented to
      // the camera, so they automatically follow head + body yaw and get stereo.
      if (!handL && !handR) buildHands();
      if (handL && handR) {
        const applyHand = (mesh, data) => {
          if (!mesh) return;
          mesh.visible = !!window.__vr_handVisible && !!(data && data.valid);
          if (data && data.valid && data.p && data.q) {
            const s = window.__vr_handScale;
            mesh.position.set(data.p[0] * s, data.p[1] * s, data.p[2] * s);
            mesh.quaternion.set(data.q[0], data.q[1], data.q[2], data.q[3]);
          }
        };
        applyHand(handL, window.__vr_hands.left);
        applyHand(handR, window.__vr_hands.right);
      }
      return prev.apply(this, arguments);
    };
    e.__vr_pose_wrapped = true;

    // Multiplayer head turn: e.pose() is called twice each frame — once for the
    // local camera (me.setPose) and once for the network (lastSent/callbacks.move).
    // Wrapping e.pose() would rotate the local camera sideways, so instead patch
    // only callbacks.move, which receives the already-built network pose. Adding
    // the accumulated head yaw there reaches other players without touching the
    // local view.
    if (!e.__vr_net_pose_wrapped && e.callbacks && e.callbacks.move) {
      const engine = e;
      const baseMove = e.callbacks.move;
      e.__vr_origMove = baseMove;
      e.callbacks.move = function (p) {
        if (p && p.length >= 4 && window.__vr_headYawAcc != null && window.__vr_baseYaw != null) {
          p[3] = p[3] + (window.__vr_headYawAcc - (window.__vr_headYawBase || 0));
          // Keep lastSent in sync so identical-pose suppression still works.
          try { engine.lastSent = p.map(v => v.toFixed(2)).join(); } catch (err) {}
        }
        return baseMove.apply(this, arguments);
      };
      e.__vr_net_pose_wrapped = true;
    }
  }

  // ---- Dual UI: clone UI siblings into left/right halves ----
  const stage = document.querySelector('[data-world-stage]');
  if (stage && stage.parentElement) {
    const root = stage.parentElement;
    let style = document.getElementById('vr-ui-dual-style');
    if (!style) {
      style = document.createElement('style');
      style.id = 'vr-ui-dual-style';
      style.textContent = `
        .vr-ui-dual > :not([data-world-stage]):not(.vr-ui-clone-layer) { opacity: 0 !important; }
        .vr-ui-clone-layer { position: absolute; top: 0; bottom: 0; width: 50%; overflow: hidden; pointer-events: none; z-index: 100; }
        .vr-ui-clone-layer button, .vr-ui-clone-layer [role="button"], .vr-ui-clone-layer a { pointer-events: auto !important; }
        .vr-ui-clone-layer.vr-ui-left { left: 0; }
        .vr-ui-clone-layer.vr-ui-right { left: 50%; }
        .vr-ui-clone-layer .fixed { position: absolute !important; }
        .vr-ui-clone-layer [style*="position: fixed"] { position: absolute !important; }
      `;
      document.head.appendChild(style);
    }
    let left = root.querySelector('.vr-ui-left');
    let right = root.querySelector('.vr-ui-right');
    if (!left || !right) {
      left = document.createElement('div');
      left.className = 'vr-ui-clone-layer vr-ui-left';
      right = document.createElement('div');
      right.className = 'vr-ui-clone-layer vr-ui-right';
      root.appendChild(left);
      root.appendChild(right);
    }
    const getOriginals = () => [...root.children].filter(c => c !== stage && !c.classList.contains('vr-ui-clone-layer'));
    const cloneUI = () => {
      const originals = getOriginals();
      for (const layer of [left, right]) {
        const frag = document.createDocumentFragment();
        for (const el of originals) {
          const clone = el.cloneNode(true);
          if (clone.id) clone.removeAttribute('id');
          if (clone.querySelectorAll) clone.querySelectorAll('[id]').forEach(n => n.removeAttribute('id'));
          frag.appendChild(clone);
        }
        layer.replaceChildren(frag);
      }
    };
    // Forward clicks from a half-width clone to the original full-width UI.
    for (const layer of [left, right]) {
      layer.addEventListener('click', (ev) => {
        const rect = layer.getBoundingClientRect();
        const x = (ev.clientX - rect.left) / rect.width * window.innerWidth;
        const y = (ev.clientY - rect.top) / rect.height * window.innerHeight;
        const target = document.elementFromPoint(x, y);
        if (target && target !== layer && !layer.contains(target)) {
          target.click();
        }
      }, true);
    }

    let pending = false, last = 0;
    const scheduleClone = () => {
      if (pending) return;
      const wait = Math.max(0, 100 - (performance.now() - last));
      pending = true;
      setTimeout(() => { pending = false; last = performance.now(); cloneUI(); }, wait);
    };
    const observer = new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.target && m.target.closest && m.target.closest('.vr-ui-clone-layer')) continue;
        scheduleClone(); break;
      }
    });
    observer.observe(root, { childList: true, subtree: true, characterData: true, attributes: true });

    window.__vr_setUIMode = function (mode) {
      window.__vr_ui_mode = mode;
      if (mode === 0) { root.classList.add('vr-ui-dual'); left.style.display = 'block'; right.style.display = 'block'; cloneUI(); }
      else if (mode === 1) { root.classList.remove('vr-ui-dual'); left.style.display = 'none'; right.style.display = 'none'; }
      else { root.classList.add('vr-ui-dual'); left.style.display = 'none'; right.style.display = 'none'; }
      return mode;
    };
    window.__vr_setUIMode(0);
  }

  // ---- Auto-reconnecting pose WebSocket ----
  function connectWS() {
    const ws = new WebSocket('ws://127.0.0.1:8777');
    window.__vr_ws = ws;
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.pose) {
          window.__vr_pose = msg.pose;
          poseHist.push({ q: msg.pose.q.slice(), t: performance.now() });
          if (poseHist.length > 6) poseHist.shift();
        }
        if (msg.hands) window.__vr_hands = msg.hands;
        if (msg.uiMode !== undefined && window.__vr_setUIMode) window.__vr_setUIMode(msg.uiMode);
      } catch (e) {}
    };
    ws.onopen = () => { window.__vr_ws_ready = true; };
    ws.onclose = () => { window.__vr_ws_ready = false; setTimeout(connectWS, 1000); };
    ws.onerror = () => {};
  }
  if (window.__vr_ws) { try { window.__vr_ws.onclose = null; window.__vr_ws.close(); } catch (e) {} }
  connectWS();

  return {
    ok: true, stereo: window.__vr_stereo, fov: cam.fov, aspect: cam.aspect,
    poseWrapped: !!e.__vr_pose_wrapped, uiMode: window.__vr_ui_mode
  };
})()
