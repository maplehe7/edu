(function () {
  "use strict";

  const loadingScreen = document.getElementById("loadingScreen");
  const starCanvas = document.getElementById("star-canvas");
  const waveCanvas = document.getElementById("wave-canvas");
  if (!loadingScreen || !starCanvas || !waveCanvas) {
    return;
  }

  const starCtx = starCanvas.getContext("2d", { alpha: true });
  const waveCtx = waveCanvas.getContext("2d", { alpha: true });
  if (!starCtx || !waveCtx) {
    return;
  }

  let stars = [];
  let dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  let waveTime = 0;
  let shootingStar = null;
  const reduceMotion =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const mouse = { x: 0.5, y: 0.5, tx: 0.5, ty: 0.5 };

  function isVisible() {
    return loadingScreen.style.display !== "none";
  }

  function resizeCanvas(canvas) {
    const nextDpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    canvas.width = Math.floor(window.innerWidth * nextDpr);
    canvas.height = Math.floor(window.innerHeight * nextDpr);
    canvas.style.width = window.innerWidth + "px";
    canvas.style.height = window.innerHeight + "px";
    return nextDpr;
  }

  class Star {
    constructor(depth) {
      this.depth = depth;
      this.reset();
    }

    reset() {
      const width = window.innerWidth;
      const height = window.innerHeight;
      this.x = Math.random() * width;
      this.y = Math.random() * height;
      const base = 1 - this.depth;
      this.size = (base * 1.5 + 0.55) * (Math.random() * 0.9 + 0.6);
      const drift = base * 0.18 + 0.04;
      this.vx = (Math.random() - 0.5) * drift;
      this.vy = (Math.random() - 0.5) * drift;
      this.opacity = Math.random() * 0.4 + 0.32;
      this.twinkle = Math.random() * 0.01 + 0.005;
      this.direction = Math.random() < 0.5 ? -1 : 1;
    }

    update() {
      const width = window.innerWidth;
      const height = window.innerHeight;
      this.x += this.vx;
      this.y += this.vy;
      this.opacity += this.twinkle * this.direction;
      if (this.opacity > 1) {
        this.opacity = 1;
        this.direction *= -1;
      }
      if (this.opacity < 0.22) {
        this.opacity = 0.22;
        this.direction *= -1;
      }
      if (this.x < -20) this.x = width + 20;
      if (this.x > width + 20) this.x = -20;
      if (this.y < -20) this.y = height + 20;
      if (this.y > height + 20) this.y = -20;
    }

    draw() {
      starCtx.shadowBlur = 8 * (this.size / 2);
      starCtx.shadowColor = "rgba(255,255,255,.75)";
      starCtx.fillStyle = "rgba(255,255,255," + this.opacity + ")";
      starCtx.beginPath();
      starCtx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
      starCtx.fill();
      starCtx.shadowBlur = 0;
    }
  }

  class ShootingStar {
    constructor() {
      const width = window.innerWidth;
      const height = window.innerHeight;
      const startEdge = Math.random();
      this.x = startEdge < 0.5 ? Math.random() * width * 0.6 : -60;
      this.y = startEdge < 0.5 ? -60 : Math.random() * height * 0.4;
      const angle = (Math.random() * 0.25 + 0.35) * Math.PI;
      const speed = Math.random() * 10 + 18;
      this.vx = Math.cos(angle) * speed;
      this.vy = Math.sin(angle) * speed;
      this.life = 0;
      this.maxLife = Math.random() * 18 + 30;
      this.length = Math.random() * 160 + 220;
      this.width = Math.random() * 1.2 + 1.2;
    }

    update() {
      this.x += this.vx;
      this.y += this.vy;
      this.life += 1;
      return this.life < this.maxLife;
    }

    draw(context) {
      const progress = this.life / this.maxLife;
      const alpha = Math.sin(Math.PI * progress) * 0.75;
      const tailX = this.x - this.vx * 3;
      const tailY = this.y - this.vy * 3;
      const norm = Math.hypot(this.vx, this.vy) || 1;
      const lineX = tailX - (this.vx / norm) * this.length;
      const lineY = tailY - (this.vy / norm) * this.length;
      const gradient = context.createLinearGradient(tailX, tailY, lineX, lineY);
      gradient.addColorStop(0, "rgba(255,255,255," + alpha + ")");
      gradient.addColorStop(0.4, "rgba(34,211,238," + alpha * 0.45 + ")");
      gradient.addColorStop(1, "rgba(59,130,246,0)");
      context.save();
      context.globalCompositeOperation = "lighter";
      context.strokeStyle = gradient;
      context.lineWidth = this.width;
      context.lineCap = "round";
      context.shadowBlur = 14;
      context.shadowColor = "rgba(34,211,238," + alpha * 0.55 + ")";
      context.beginPath();
      context.moveTo(tailX, tailY);
      context.lineTo(lineX, lineY);
      context.stroke();
      context.restore();
    }
  }

  function seedStars() {
    stars = [];
    const count = Math.round(
      Math.min(160, Math.max(90, (window.innerWidth * window.innerHeight) / 14000))
    );
    for (let index = 0; index < count; index += 1) {
      stars.push(new Star(Math.random()));
    }
  }

  function resizeAll() {
    dpr = resizeCanvas(starCanvas);
    starCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    resizeCanvas(waveCanvas);
    waveCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    seedStars();
  }

  function scheduleShootingStar() {
    if (reduceMotion || !isVisible()) {
      return;
    }
    window.setTimeout(function () {
      if (!shootingStar && isVisible()) {
        shootingStar = new ShootingStar();
      }
      scheduleShootingStar();
    }, Math.random() * 2500 + 3500);
  }

  function animateStars() {
    if (!isVisible()) {
      return;
    }
    starCtx.clearRect(0, 0, window.innerWidth, window.innerHeight);
    for (const star of stars) {
      star.update();
      star.draw();
    }
    if (shootingStar) {
      if (!shootingStar.update()) {
        shootingStar = null;
      } else {
        shootingStar.draw(starCtx);
      }
    }
    window.requestAnimationFrame(animateStars);
  }

  function smoothMouse() {
    if (!isVisible()) {
      return;
    }
    mouse.x += (mouse.tx - mouse.x) * 0.06;
    mouse.y += (mouse.ty - mouse.y) * 0.06;
    window.requestAnimationFrame(smoothMouse);
  }

  function drawWaves() {
    if (!isVisible()) {
      return;
    }
    const width = window.innerWidth;
    const height = window.innerHeight;
    waveCtx.clearRect(0, 0, width, height);
    const ampBoost = 1 + (0.9 - mouse.y) * 0.65;
    const phaseShift = (mouse.x - 0.5) * 1.2;
    const horizon = height * (0.56 + (mouse.y - 0.5) * 0.08);
    const gradient = waveCtx.createLinearGradient(0, horizon - 200, 0, height);
    gradient.addColorStop(0, "rgba(34,211,238,0.05)");
    gradient.addColorStop(0.4, "rgba(59,130,246,0.10)");
    gradient.addColorStop(1, "rgba(59,130,246,0.08)");
    const bands = 8;

    for (let band = 0; band < bands; band += 1) {
      const bandTime = waveTime * (0.75 + band * 0.045);
      const baseY = horizon + band * (height * 0.055);
      const amplitude = (12 + band * 7) * ampBoost;
      const frequency = 0.01 + band * 0.0012;
      const speed = 0.75 + band * 0.12;
      const wobble = 0.35 + band * 0.04;

      waveCtx.beginPath();
      waveCtx.moveTo(0, baseY);
      for (let x = 0; x <= width; x += 10) {
        const nextX = x * frequency;
        const y =
          baseY +
          Math.sin(nextX + bandTime * 0.015 * speed + phaseShift) * amplitude +
          Math.sin(nextX * 1.8 - bandTime * 0.01 * speed) * (amplitude * wobble * 0.25);
        waveCtx.lineTo(x, y);
      }
      waveCtx.lineTo(width, height);
      waveCtx.lineTo(0, height);
      waveCtx.closePath();
      waveCtx.fillStyle = gradient;
      waveCtx.fill();
      waveCtx.globalCompositeOperation = "lighter";
      waveCtx.strokeStyle = "rgba(34,211,238," + (0.05 + band * 0.008) + ")";
      waveCtx.lineWidth = 1;
      waveCtx.stroke();
      waveCtx.globalCompositeOperation = "source-over";
    }

    waveTime += 0.95;
    window.requestAnimationFrame(drawWaves);
  }

  window.addEventListener(
    "mousemove",
    function (event) {
      mouse.tx = event.clientX / Math.max(window.innerWidth, 1);
      mouse.ty = event.clientY / Math.max(window.innerHeight, 1);
    },
    { passive: true }
  );
  window.addEventListener("resize", resizeAll);

  resizeAll();
  if (reduceMotion) {
    mouse.tx = 0.5;
    mouse.ty = 0.5;
  } else {
    scheduleShootingStar();
    smoothMouse();
  }
  animateStars();
  drawWaves();
})();

(function () {
  "use strict";

  const gameFrame = document.getElementById("game_frame");
  const loadingScreen = document.getElementById("loadingScreen");
  const progressFill = document.getElementById("progressFill");
  const progressTrack = document.getElementById("progressTrack");
  const launchPanel = document.getElementById("launchPanel");
  const launchFullscreenBtn = document.getElementById("launchFullscreenBtn");
  const launchFrameBtn = document.getElementById("launchFrameBtn");
  const playNote = document.getElementById("playNote");
  const status = document.getElementById("status");
  const startMain = typeof window.main === "function" ? window.main.bind(window) : null;
  const embedUrl =
    typeof window.OCEAN_EMBED_URL === "string" ? window.OCEAN_EMBED_URL.trim() : "";
  const remoteUrl =
    typeof window.OCEAN_REMOTE_URL === "string" ? window.OCEAN_REMOTE_URL.trim() : "";
  const embedTitle =
    typeof window.OCEAN_EMBED_TITLE === "string" && window.OCEAN_EMBED_TITLE.trim()
      ? window.OCEAN_EMBED_TITLE.trim()
      : "Game";
  const initialStatusText =
    typeof window.OCEAN_INITIAL_STATUS === "string" && window.OCEAN_INITIAL_STATUS.trim()
      ? window.OCEAN_INITIAL_STATUS.trim()
      : "Awaiting launch-mode selection";
  const playNoteText =
    typeof window.OCEAN_PLAY_NOTE === "string" ? window.OCEAN_PLAY_NOTE.trim() : "";
  const launchFrameLabel =
    typeof window.OCEAN_LAUNCH_FRAME_LABEL === "string" &&
    window.OCEAN_LAUNCH_FRAME_LABEL.trim()
      ? window.OCEAN_LAUNCH_FRAME_LABEL.trim()
      : "LAUNCH HERE";
  const launchFullscreenLabel =
    typeof window.OCEAN_LAUNCH_FULLSCREEN_LABEL === "string" &&
    window.OCEAN_LAUNCH_FULLSCREEN_LABEL.trim()
      ? window.OCEAN_LAUNCH_FULLSCREEN_LABEL.trim()
      : "LAUNCH FULLSCREEN";
  const remoteLaunchOnly = Boolean(remoteUrl && !embedUrl && !startMain);

  if (
    !gameFrame ||
    !loadingScreen ||
    !progressFill ||
    !progressTrack ||
    !launchPanel ||
    !launchFullscreenBtn ||
    !launchFrameBtn ||
    !status
  ) {
    return;
  }

  const stepLog =
    document.getElementById("stepLog") ||
    (function () {
      const element = document.createElement("div");
      element.id = "stepLog";
      element.setAttribute("aria-live", "polite");
      element.setAttribute("aria-atomic", "false");
      loadingScreen.appendChild(element);
      return element;
    })();

  launchFrameBtn.textContent = launchFrameLabel;
  launchFullscreenBtn.textContent = launchFullscreenLabel;
  if (playNote) {
    if (playNoteText) {
      playNote.textContent = playNoteText;
      playNote.style.display = "";
    } else {
      playNote.style.display = "none";
    }
  }

  let started = false;
  let loadingScreenDismissed = false;
  let launchPanelHideTimer = 0;
  let fakeProgressTimer = 0;
  let fakeProgressValue = 0;
  let handoffPollTimer = 0;
  const stepLogEntries = [];
  const loaderStepEpoch = Date.now();
  let lastLoggedStep = "";
  let lastProgressBucket = -1;
  const requestedLaunchMode = (function () {
    try {
      return new URL(window.location.href).searchParams.get("launchMode") || "";
    } catch (err) {
      return "";
    }
  })();
  const forceFullscreenScrollLock = requestedLaunchMode === "fullscreen";
  const FULLSCREEN_SCROLL_LOCK_ATTR = "data-ocean-fullscreen-lock";
  const fullscreenScrollKeys = new Set([
    " ",
    "Spacebar",
    "ArrowUp",
    "ArrowDown",
    "PageUp",
    "PageDown",
    "Home",
    "End",
  ]);
  const fullscreenScrollCodes = new Set([
    "Space",
    "ArrowUp",
    "ArrowDown",
    "PageUp",
    "PageDown",
    "Home",
    "End",
  ]);

  function safeUrlHost(value) {
    if (!value) {
      return "";
    }
    try {
      return new URL(value, window.location.href).host || "";
    } catch (err) {
      return "";
    }
  }

  function launcherTargetKind() {
    if (remoteLaunchOnly) {
      return "remote_stream";
    }
    if (embedUrl) {
      return "iframe_embed";
    }
    if (startMain) {
      return "inline_bootstrap";
    }
    return "unknown";
  }

  function launcherProgressPhase(percent) {
    if (percent <= 0) {
      return "init";
    }
    if (percent < 20) {
      return "launch-prep";
    }
    if (percent < 45) {
      return "frame-bootstrap";
    }
    if (percent < 75) {
      return "asset-warmup";
    }
    if (percent < 100) {
      return "runtime-handoff";
    }
    return "ready";
  }

  function formatTechnicalStatusText(message) {
    const cleanMessage = String(message || "").replace(/\s+/g, " ").trim();
    if (!cleanMessage) {
      return "";
    }
    if (
      cleanMessage === initialStatusText ||
      cleanMessage === "Choose how you want to launch" ||
      cleanMessage === "Awaiting launch-mode selection"
    ) {
      return "Awaiting launch-mode selection";
    }
    const progressMatch = /^Loading (\d+)%$/.exec(cleanMessage);
    if (progressMatch) {
      const percent = Number(progressMatch[1]);
      return (
        "Handoff progress=" +
        percent +
        "% phase=" +
        launcherProgressPhase(percent)
      );
    }
    switch (cleanMessage) {
      case "Game bootstrap is missing":
        return "Fatal: no bootstrap target detected";
      case "Opening remote stream":
        return "Remote handoff in progress";
      case "Opened fullscreen in a new tab":
        return "Fullscreen handoff opened in new tab";
      case "Opened remote stream in a new tab":
        return "Remote stream opened in new tab";
      case "New tab blocked. Allow popups or use open here.":
      case "New tab blocked. Allow popups or use launch here.":
        return "Popup blocked; new-tab handoff aborted";
      case "Ready":
        return "Runtime handoff complete";
      case "Failed to load game":
        return "Fatal: launcher handoff failed";
      default:
        return cleanMessage;
    }
  }

  function formatTechnicalStepMessage(message) {
    const cleanMessage = String(message || "").replace(/\s+/g, " ").trim();
    if (!cleanMessage) {
      return "";
    }
    const progressMatch = /^Loading (\d+)%$/.exec(cleanMessage);
    if (progressMatch) {
      const percent = Number(progressMatch[1]);
      return (
        "[handoff.progress] value=" +
        percent +
        "% phase=" +
        launcherProgressPhase(percent) +
        " target=" +
        launcherTargetKind()
      );
    }
    switch (cleanMessage) {
      case "Awaiting launch-mode selection":
      case "Choose how you want to launch":
        return "[shell.idle] awaiting launch-mode selection";
      case "Shell initialized":
        return (
          "[shell.init] launcher-ready kind=" +
          launcherTargetKind() +
          " mode=" +
          (requestedLaunchMode || "page") +
          " proto=" +
          window.location.protocol.replace(":", "") +
          " targetHost=" +
          safeUrlHost(remoteUrl || embedUrl || window.location.href)
        );
      case "Launch requested":
        return "[launch] user-activation accepted target=" + launcherTargetKind();
      case "Storage access API unavailable":
        return "[storage] API unavailable; continuing";
      case "Checking storage access":
        return "[storage] hasStorageAccess() probe";
      case "Storage access already granted":
        return "[storage] access already granted";
      case "Requesting storage access":
        return "[storage] requestStorageAccess()";
      case "Storage access request failed":
        return "[storage] requestStorageAccess() failed; continuing";
      case "Storage access check failed":
        return "[storage] hasStorageAccess() failed; continuing";
      case "Preparing embedded frame":
        return "[embed] iframe bootstrap requested host=" + safeUrlHost(embedUrl);
      case "Embedded iframe attached":
        return "[embed] iframe DOM attached host=" + safeUrlHost(embedUrl);
      case "Embedded iframe loaded":
        return "[embed] iframe load event received";
      case "Invoking inline bootstrap":
        return "[bootstrap.inline] main()";
      case "Waiting for runtime handoff":
        return "[handoff] waiting for runtime attachment";
      case "Runtime handoff complete":
        return "[handoff] runtime attachment confirmed";
      case "Remote stream handoff starting":
        return "[handoff.remote] navigating host=" + safeUrlHost(remoteUrl);
      default:
        return cleanMessage;
    }
  }

  function ensureStorageAccess(targetDocument) {
    const storageDocument = targetDocument || document;
    const hasApi =
      storageDocument &&
      typeof storageDocument.hasStorageAccess === "function" &&
      typeof storageDocument.requestStorageAccess === "function";
    if (!hasApi) {
      logLoaderStep("Storage access API unavailable");
      return Promise.resolve();
    }
    logLoaderStep("Checking storage access");
    return Promise.resolve(storageDocument.hasStorageAccess())
      .then(function (hasAccess) {
        if (hasAccess) {
          logLoaderStep("Storage access already granted");
          return;
        }
        logLoaderStep("Requesting storage access");
        return storageDocument.requestStorageAccess().catch(function () {
          logLoaderStep("Storage access request failed");
          // Continue without blocking launch.
        });
      })
      .catch(function () {
        logLoaderStep("Storage access check failed");
        // Continue without blocking launch.
      });
  }

  function logLoaderStep(message) {
    if (!stepLog || typeof message !== "string") {
      return;
    }
    const cleanMessage = message.replace(/\s+/g, " ").trim();
    if (!cleanMessage) {
      return;
    }
    const progressMatch = /^Loading (\d+)%$/.exec(cleanMessage);
    const formattedMessage = formatTechnicalStepMessage(cleanMessage);
    if (!formattedMessage) {
      return;
    }
    if (progressMatch) {
      const percent = Number(progressMatch[1]);
      const bucket =
        percent >= 100 ? 100 : Math.max(0, Math.floor(percent / 10) * 10);
      if (bucket === lastProgressBucket && percent !== 0 && percent !== 100) {
        return;
      }
      lastProgressBucket = bucket;
    } else if (formattedMessage === lastLoggedStep) {
      return;
    } else {
      lastLoggedStep = formattedMessage;
    }
    const elapsedSeconds = ((Date.now() - loaderStepEpoch) / 1000).toFixed(1);
    stepLogEntries.push(elapsedSeconds + "s  " + formattedMessage);
    while (stepLogEntries.length > 8) {
      stepLogEntries.shift();
    }
    stepLog.textContent = stepLogEntries.join("\n");
  }

  function getSameOriginFrameContext(frame) {
    try {
      const frameWindow = frame.contentWindow;
      const frameDocument = frameWindow && frameWindow.document;
      if (!frameWindow || !frameDocument) {
        return null;
      }
      frameDocument.location.href;
      return {
        frameWindow: frameWindow,
        frameDocument: frameDocument,
      };
    } catch (err) {
      return null;
    }
  }

  function suppressEmbeddedLaunchPrompts(frameWindow, frameDocument) {
    const skipCountdownButton = frameDocument.getElementById("skipCountdown");
    const mobileLaunchButton = frameDocument.querySelector(
      "._eaglercraftX_mobile_launch_client"
    );
    const handledByButton = Boolean(skipCountdownButton || mobileLaunchButton);
    if (skipCountdownButton && typeof skipCountdownButton.click === "function") {
      skipCountdownButton.click();
    }
    if (mobileLaunchButton && typeof mobileLaunchButton.click === "function") {
      mobileLaunchButton.click();
    }
    if (handledByButton || typeof frameWindow.main !== "function") {
      return;
    }
    const countdownScreen = frameDocument.getElementById("launch_countdown_screen");
    const mobilePrompt = frameDocument.querySelector("._eaglercraftX_mobile_press_any_key");
    if (!countdownScreen && !mobilePrompt) {
      return;
    }
    if (countdownScreen && countdownScreen.parentNode) {
      countdownScreen.parentNode.removeChild(countdownScreen);
    }
    if (mobilePrompt && mobilePrompt.parentNode) {
      mobilePrompt.parentNode.removeChild(mobilePrompt);
    }
    try {
      frameWindow.main();
    } catch (err) {
      console.warn("Embedded launch prompt bypass failed:", err);
    }
  }

  function monitorEmbeddedRuntime(frame) {
    const startedAt = Date.now();
    const timer = window.setInterval(function () {
      if (!frame.isConnected) {
        window.clearInterval(timer);
        return;
      }
      const context = getSameOriginFrameContext(frame);
      if (!context) {
        window.clearInterval(timer);
        return;
      }
      suppressEmbeddedLaunchPrompts(context.frameWindow, context.frameDocument);
      const countdownScreen = context.frameDocument.getElementById("launch_countdown_screen");
      const mobilePrompt = context.frameDocument.querySelector(
        "._eaglercraftX_mobile_press_any_key"
      );
      if ((!countdownScreen && !mobilePrompt) || Date.now() - startedAt >= 16000) {
        window.clearInterval(timer);
      }
    }, 50);
  }

  function prepareEmbeddedFrame(frame) {
    const context = getSameOriginFrameContext(frame);
    if (!context) {
      return Promise.resolve();
    }
    monitorEmbeddedRuntime(frame);
    return ensureStorageAccess(context.frameDocument);
  }

  function setStatus(text) {
    status.textContent = formatTechnicalStatusText(text);
    logLoaderStep(text);
  }

  function setProgress(progress) {
    const numeric = Number(progress);
    const safeProgress = Number.isFinite(numeric) ? Math.min(1, Math.max(0, numeric)) : 0;
    const percent = Math.round(safeProgress * 100);
    progressFill.style.width = percent + "%";
    loadingScreen.setAttribute("data-progress", String(percent));
    return percent;
  }

  function setProgressVisibility(isVisible) {
    progressTrack.classList.toggle("is-visible", Boolean(isVisible));
  }

  function clearLaunchPanelHideTimer() {
    if (!launchPanelHideTimer) {
      return;
    }
    window.clearTimeout(launchPanelHideTimer);
    launchPanelHideTimer = 0;
  }

  function clearFakeProgressTimer() {
    if (!fakeProgressTimer) {
      return;
    }
    window.clearInterval(fakeProgressTimer);
    fakeProgressTimer = 0;
  }

  function clearHandoffPollTimer() {
    if (!handoffPollTimer) {
      return;
    }
    window.clearInterval(handoffPollTimer);
    handoffPollTimer = 0;
  }

  function clearGameFrame() {
    while (gameFrame.firstChild) {
      gameFrame.removeChild(gameFrame.firstChild);
    }
  }

  function isFullscreenActive() {
    return Boolean(
      document.fullscreenElement ||
        document.webkitFullscreenElement ||
        document.msFullscreenElement ||
        document.mozFullScreenElement
    );
  }

  function shouldLockFullscreenScroll() {
    return forceFullscreenScrollLock || isFullscreenActive();
  }

  function setFullscreenScrollLock(isLocked) {
    const root = document.documentElement;
    const body = document.body;
    if (root) {
      if (isLocked) {
        root.setAttribute(FULLSCREEN_SCROLL_LOCK_ATTR, "1");
      } else {
        root.removeAttribute(FULLSCREEN_SCROLL_LOCK_ATTR);
      }
    }
    if (body) {
      if (isLocked) {
        body.setAttribute(FULLSCREEN_SCROLL_LOCK_ATTR, "1");
      } else {
        body.removeAttribute(FULLSCREEN_SCROLL_LOCK_ATTR);
      }
    }
    if (isLocked && typeof window.scrollTo === "function") {
      window.scrollTo(0, 0);
    }
  }

  function syncFullscreenScrollLock() {
    setFullscreenScrollLock(shouldLockFullscreenScroll());
  }

  function isFullscreenScrollKey(event) {
    const key = typeof event.key === "string" ? event.key : "";
    const code = typeof event.code === "string" ? event.code : "";
    return fullscreenScrollKeys.has(key) || fullscreenScrollCodes.has(code);
  }

  function preventFullscreenScroll(event) {
    if (!shouldLockFullscreenScroll()) {
      return;
    }
    if (event.type === "keydown" && !isFullscreenScrollKey(event)) {
      return;
    }
    if (event.cancelable) {
      event.preventDefault();
    }
  }

  function enforceFullscreenScrollTop() {
    if (
      !shouldLockFullscreenScroll() ||
      (window.scrollX === 0 && window.scrollY === 0) ||
      typeof window.scrollTo !== "function"
    ) {
      return;
    }
    window.scrollTo(0, 0);
  }

  function buildLaunchUrl(mode) {
    const targetUrl = new URL(window.location.href);
    targetUrl.searchParams.set("autostart", "1");
    targetUrl.searchParams.set("launchMode", mode);
    return targetUrl.toString();
  }

  function dismissLoadingScreen() {
    if (loadingScreenDismissed) {
      return;
    }
    loadingScreenDismissed = true;
    loadingScreen.classList.add("is-exiting");
    window.setTimeout(function () {
      loadingScreen.style.display = "none";
    }, 880);
  }

  function resetLaunchState() {
    started = false;
    clearLaunchPanelHideTimer();
    clearFakeProgressTimer();
    clearHandoffPollTimer();
    loadingScreen.classList.remove("is-loading");
    launchPanel.style.display = "";
    launchPanel.classList.remove("is-hidden");
    setProgressVisibility(false);
    fakeProgressValue = 0;
    setProgress(0);
    setStatus(initialStatusText);
  }

  function startFakeProgress() {
    clearFakeProgressTimer();
    fakeProgressValue = 0.02;
    setProgress(fakeProgressValue);
    fakeProgressTimer = window.setInterval(function () {
      if (fakeProgressValue >= 0.92) {
        clearFakeProgressTimer();
        return;
      }
      if (fakeProgressValue < 0.45) {
        fakeProgressValue += 0.08;
      } else if (fakeProgressValue < 0.75) {
        fakeProgressValue += 0.045;
      } else {
        fakeProgressValue += 0.018;
      }
      fakeProgressValue = Math.min(fakeProgressValue, 0.92);
      const percent = setProgress(fakeProgressValue);
      setStatus("Loading " + percent + "%");
    }, 140);
  }

  function completeHandoff() {
    clearFakeProgressTimer();
    clearHandoffPollTimer();
    setProgress(1);
    logLoaderStep("Runtime handoff complete");
    setStatus("Ready");
    window.setTimeout(dismissLoadingScreen, 380);
  }

  function waitForGameHandoff() {
    logLoaderStep("Waiting for runtime handoff");
    clearHandoffPollTimer();
    const deadline = Date.now() + 4000;
    handoffPollTimer = window.setInterval(function () {
      const hasGameContent = gameFrame.childNodes.length > 0;
      if (hasGameContent || Date.now() >= deadline) {
        completeHandoff();
      }
    }, 120);
  }

  function requestFullscreenMode() {
    const target = document.documentElement || document.body || gameFrame;
    if (!target) {
      return Promise.resolve(false);
    }
    if (
      document.fullscreenElement ||
      document.webkitFullscreenElement ||
      document.msFullscreenElement ||
      document.mozFullScreenElement
    ) {
      return Promise.resolve(true);
    }
    const request =
      target.requestFullscreen ||
      target.webkitRequestFullscreen ||
      target.webkitRequestFullScreen ||
      target.msRequestFullscreen ||
      target.mozRequestFullScreen;
    if (typeof request !== "function") {
      return Promise.resolve(false);
    }
    setFullscreenScrollLock(true);
    try {
      return Promise.resolve(request.call(target))
        .then(function () {
          syncFullscreenScrollLock();
          return true;
        })
        .catch(function (err) {
          setFullscreenScrollLock(false);
          console.warn("Fullscreen request failed:", err);
          return false;
        });
    } catch (err) {
      setFullscreenScrollLock(false);
      console.warn("Fullscreen request failed:", err);
      return Promise.resolve(false);
    }
  }

  function consumeAutoStartFlag() {
    const currentUrl = new URL(window.location.href);
    const shouldAutoStart = currentUrl.searchParams.get("autostart") === "1";
    if (shouldAutoStart) {
      currentUrl.searchParams.delete("autostart");
      currentUrl.searchParams.delete("launchMode");
      const cleanedUrl = currentUrl.pathname + currentUrl.search + currentUrl.hash;
      if (window.history && typeof window.history.replaceState === "function") {
        window.history.replaceState(null, "", cleanedUrl || currentUrl.pathname);
      }
    }
    return shouldAutoStart;
  }

  function startFullscreenGame() {
    if (remoteLaunchOnly) {
      const popup = window.open(remoteUrl, "_blank");
      if (!popup || popup.closed) {
        setStatus("New tab blocked. Allow popups or use open here.");
        return;
      }
      try {
        popup.opener = null;
      } catch (err) {
        // Ignore opener hardening failures.
      }
      setStatus("Opened remote stream in a new tab");
      return;
    }
    const popup = window.open(buildLaunchUrl("fullscreen"), "_blank");
    if (!popup || popup.closed) {
      setStatus("New tab blocked. Allow popups or use launch here.");
      return;
    }
    try {
      popup.opener = null;
    } catch (err) {
      // Ignore opener hardening failures.
    }
    setStatus("Opened fullscreen in a new tab");
  }

  function startEmbeddedGame() {
    if (!embedUrl) {
      return Promise.reject(new Error("Embedded game URL is missing"));
    }
    logLoaderStep("Preparing embedded frame");
    return new Promise(function (resolve, reject) {
      let settled = false;
      const frame = document.createElement("iframe");
      const resolvedUrl = new URL(embedUrl, window.location.href).toString();
      frame.className = "ocean-game-embed";
      frame.src = resolvedUrl;
      frame.title = embedTitle;
      frame.loading = "eager";
      frame.referrerPolicy = "strict-origin-when-cross-origin";
      frame.setAttribute(
        "allow",
        "autoplay; fullscreen; gamepad; clipboard-read; clipboard-write"
      );

      function settleWith(fn, value) {
        if (settled) {
          return;
        }
        settled = true;
        fn(value);
      }

      frame.addEventListener("load", function () {
        logLoaderStep("Embedded iframe loaded");
        prepareEmbeddedFrame(frame).finally(function () {
          settleWith(resolve);
        });
      });
      frame.addEventListener("error", function () {
        settleWith(reject, new Error("Embedded game page failed to load"));
      });

      clearGameFrame();
      gameFrame.appendChild(frame);
      logLoaderStep("Embedded iframe attached");
      window.setTimeout(function () {
        settleWith(resolve);
      }, 12000);
    });
  }

  function startGame() {
    if (started) {
      return;
    }
    if (!startMain && !embedUrl && !remoteUrl) {
      setStatus("Game bootstrap is missing");
      return;
    }
    if (remoteLaunchOnly) {
      started = true;
      logLoaderStep("Remote stream handoff starting");
      setStatus("Opening remote stream");
      window.setTimeout(function () {
        window.location.assign(remoteUrl);
      }, 40);
      return;
    }
    started = true;
    logLoaderStep("Launch requested");
    loadingScreen.classList.add("is-loading");
    clearLaunchPanelHideTimer();
    launchPanel.style.display = "";
    launchPanel.classList.add("is-hidden");
    launchPanelHideTimer = window.setTimeout(function () {
      if (launchPanel.classList.contains("is-hidden")) {
        launchPanel.style.display = "none";
      }
      launchPanelHideTimer = 0;
    }, 240);
    setProgressVisibility(true);
    setProgress(0);
    setStatus("Loading 0%");
    startFakeProgress();

    Promise.resolve()
      .then(function () {
        return ensureStorageAccess(document);
      })
      .then(function () {
        if (startMain) {
          logLoaderStep("Invoking inline bootstrap");
          return startMain();
        }
        return startEmbeddedGame();
      })
      .then(function () {
        waitForGameHandoff();
      })
      .catch(function (err) {
        console.error(err);
        resetLaunchState();
        setStatus("Failed to load game");
        alert("Game failed to load: " + err);
      });
  }

  setProgressVisibility(false);
  setProgress(0);
  logLoaderStep("Shell initialized");
  setStatus(initialStatusText);

  window.addEventListener("wheel", preventFullscreenScroll, { passive: false });
  window.addEventListener("touchmove", preventFullscreenScroll, { passive: false });
  window.addEventListener("keydown", preventFullscreenScroll, { passive: false });
  window.addEventListener("scroll", enforceFullscreenScrollTop, { passive: true });
  window.addEventListener("fullscreenchange", syncFullscreenScrollLock);
  window.addEventListener("webkitfullscreenchange", syncFullscreenScrollLock);
  window.addEventListener("mozfullscreenchange", syncFullscreenScrollLock);
  window.addEventListener("MSFullscreenChange", syncFullscreenScrollLock);
  syncFullscreenScrollLock();

  launchFullscreenBtn.addEventListener("click", startFullscreenGame);
  launchFrameBtn.addEventListener("click", startGame);

  if (consumeAutoStartFlag()) {
    startGame();
  }
})();
