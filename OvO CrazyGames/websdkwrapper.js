globalThis.WebSdkWrapper = (function () {
  var listeners = {
    pause: [],
    resume: [],
    mute: [],
    unmute: [],
    adStarted: []
  };
  var unlockAllLevelsHandler = null;
  var gameplayStarted = false;
  var crazyListeners = {};

  function addListener(eventName, fn) {
    if (typeof fn !== "function") {
      return;
    }
    if (!Object.prototype.hasOwnProperty.call(listeners, eventName)) {
      listeners[eventName] = [];
    }
    listeners[eventName].push(fn);
  }

  function emit(eventName) {
    var args = Array.prototype.slice.call(arguments, 1);
    var queue = listeners[eventName] || [];
    for (var index = 0; index < queue.length; index += 1) {
      try {
        queue[index].apply(null, args);
      } catch (error) {
        console.warn("Local WebSdkWrapper listener failed:", error);
      }
    }
  }

  function addCrazyListener(eventName, fn) {
    if (typeof fn !== "function") {
      return;
    }
    if (!Object.prototype.hasOwnProperty.call(crazyListeners, eventName)) {
      crazyListeners[eventName] = [];
    }
    crazyListeners[eventName].push(fn);
  }

  function emitCrazy(eventName, payload) {
    var queue = crazyListeners[eventName] || [];
    for (var index = 0; index < queue.length; index += 1) {
      try {
        queue[index](payload || {});
      } catch (error) {
        console.warn("Local CrazyGames listener failed:", error);
      }
    }
  }

  function hideBannerContainer(containerId) {
    if (!containerId) {
      return;
    }
    var element = document.getElementById(containerId);
    if (!element) {
      return;
    }
    element.textContent = "";
    element.innerHTML = "";
    element.style.display = "none";
    element.style.visibility = "hidden";
    element.style.pointerEvents = "none";
  }

  function setAdConfig(config) {
    if (!config || typeof config !== "object") {
      return;
    }
    globalThis.adconfigRemoveSocials = config.removeSocials ? 1 : 0;
    globalThis.adconfigStopAudioInBackground = config.stopAudioInBackground ? 1 : 0;
    globalThis.adconfigRemoveMidrollRewarded = config.removeMidrollRewarded ? 1 : 0;
    globalThis.adconfigNoReligion = config.noReligion ? 1 : 0;
  }

  var localCrazySdk = {
    hasAdblock: false,
    init: function () {
      emitCrazy("adblockDetectionExecuted", { hasAdblock: false });
    },
    addEventListener: function (eventName, handler) {
      addCrazyListener(eventName, handler);
    },
    requestAd: function (adType) {
      var normalizedType = adType === "rewarded" ? "rewarded" : "interstitial";
      emit("adStarted", normalizedType);
      emit("mute");
      emitCrazy("adStarted", { type: normalizedType });
      return Promise.resolve().then(function () {
        emit("unmute");
        emitCrazy("adFinished", { type: normalizedType });
        return true;
      });
    },
    requestBanner: function (banners) {
      if (!Array.isArray(banners)) {
        return;
      }
      for (var index = 0; index < banners.length; index += 1) {
        var banner = banners[index] || {};
        var containerId = banner.containerId || "";
        hideBannerContainer(containerId);
        emitCrazy("bannerError", {
          containerId: containerId,
          error: "disabled"
        });
      }
    },
    gameplayStart: function () {},
    gameplayStop: function () {},
    happytime: function () {}
  };

  var crazyRoot = globalThis.CrazyGames = globalThis.CrazyGames || {};
  crazyRoot.CrazySDK = crazyRoot.CrazySDK || {
    getInstance: function () {
      return localCrazySdk;
    }
  };
  globalThis.Crazygames = globalThis.Crazygames || {};
  if (typeof globalThis.Crazygames.requestInviteUrl !== "function") {
    globalThis.Crazygames.requestInviteUrl = function () {};
  }
  globalThis.crazysdk = localCrazySdk;
  globalThis.adblockIsEnabled = false;

  var Wrapper = {
    get enabled() {
      return true;
    },
    get currentSdk() {
      return { name: "LocalNoAds" };
    },
    init: function (_name, _debug, data) {
      if (data && typeof data === "object") {
        setAdConfig(data);
      }
      return Promise.resolve();
    },
    onPause: function (fn) {
      addListener("pause", fn);
    },
    pause: function () {
      emit("pause");
    },
    onResume: function (fn) {
      addListener("resume", fn);
    },
    resume: function () {
      emit("resume");
    },
    onMute: function (fn) {
      addListener("mute", fn);
    },
    mute: function () {
      emit("mute");
    },
    onUnmute: function (fn) {
      addListener("unmute", fn);
    },
    unmute: function () {
      emit("unmute");
    },
    onUnlockAllLevels: function (fn) {
      unlockAllLevelsHandler = typeof fn === "function" ? fn : null;
    },
    unlockAllLevels: function () {
      if (typeof unlockAllLevelsHandler === "function") {
        unlockAllLevelsHandler();
      }
    },
    hasAdblock: function () {
      return false;
    },
    loadingStart: function () {},
    loadingProgress: function (_progress) {},
    loadingEnd: function () {},
    gameplayStart: function () {
      gameplayStarted = true;
    },
    gameplayStop: function () {
      gameplayStarted = false;
    },
    happyTime: function () {},
    levelStart: function (_level) {},
    replayLevel: function (_level) {},
    score: function (_score) {},
    banner: function (data) {
      if (Array.isArray(data)) {
        localCrazySdk.requestBanner(data);
      }
      return false;
    },
    interstitial: function (handleGameplayStart) {
      var shouldResume = Boolean(handleGameplayStart && gameplayStarted);
      if (shouldResume) {
        Wrapper.gameplayStop();
      }
      emit("adStarted", "interstitial");
      emit("mute");
      return Promise.resolve(true).then(function (success) {
        emit("unmute");
        if (shouldResume) {
          Wrapper.gameplayStart();
        }
        return success;
      });
    },
    rewarded: function (handleGameplayStart) {
      var shouldResume = Boolean(handleGameplayStart && gameplayStarted);
      if (shouldResume) {
        Wrapper.gameplayStop();
      }
      emit("adStarted", "rewarded");
      emit("mute");
      return Promise.resolve(true).then(function (success) {
        emit("unmute");
        if (shouldResume) {
          Wrapper.gameplayStart();
        }
        return success;
      });
    },
    onAdStarted: function (fn) {
      addListener("adStarted", fn);
    },
    hasAds: function () {
      return 0;
    }
  };

  return Wrapper;
})();
