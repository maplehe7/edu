(function () {
  var root = window.CrazyGames = window.CrazyGames || {};
  var sdk = root.SDK = root.SDK || {};
  var ad = sdk.ad = sdk.ad || {};
  var banner = sdk.banner = sdk.banner || {};
  var game = sdk.game = sdk.game || {};
  var user = sdk.user = sdk.user || {};

  if (typeof sdk.addInitCallback !== "function") {
    sdk.addInitCallback = function (callback) {
      if (typeof callback === "function") {
        callback({});
      }
    };
  }
  if (typeof ad.hasAdblock !== "function") {
    ad.hasAdblock = function (callback) {
      if (typeof callback === "function") {
        callback(null, false);
      }
      return false;
    };
  }
  if (typeof ad.requestAd !== "function") {
    ad.requestAd = function (_adType, callbacks) {
      callbacks = callbacks || {};
      if (typeof callbacks.adStarted === "function") {
        callbacks.adStarted();
      }
      if (typeof callbacks.adFinished === "function") {
        callbacks.adFinished();
      }
      return "closed";
    };
  }
  if (typeof banner.requestOverlayBanners !== "function") {
    banner.requestOverlayBanners = function (_banners, callback) {
      if (typeof callback === "function") {
        callback("", "bannerRendered", null);
      }
      return "bannerRendered";
    };
  }
  if (typeof game.gameplayStart !== "function") {
    game.gameplayStart = function () {};
  }
  if (typeof game.gameplayStop !== "function") {
    game.gameplayStop = function () {};
  }
  if (typeof game.happytime !== "function") {
    game.happytime = function () {};
  }
  if (typeof user.addAuthListener !== "function") {
    user.addAuthListener = function (callback) {
      if (typeof callback === "function") {
        callback({});
      }
    };
  }
  if (typeof user.addScore !== "function") {
    user.addScore = function () {};
  }
  if (typeof user.getUser !== "function") {
    user.getUser = function (callback) {
      if (typeof callback === "function") {
        callback(null, {});
      }
    };
  }
  if (typeof user.getUserToken !== "function") {
    user.getUserToken = function (callback) {
      if (typeof callback === "function") {
        callback(null, "");
      }
      return "";
    };
  }
  if (typeof user.getXsollaUserToken !== "function") {
    user.getXsollaUserToken = function (callback) {
      if (typeof callback === "function") {
        callback(null, "");
      }
      return "";
    };
  }
  if (typeof user.showAccountLinkPrompt !== "function") {
    user.showAccountLinkPrompt = function (callback) {
      if (typeof callback === "function") {
        callback(null, {});
      }
    };
  }
  if (typeof user.showAuthPrompt !== "function") {
    user.showAuthPrompt = function (callback) {
      if (typeof callback === "function") {
        callback(null, {});
      }
    };
  }

  var legacyRoot = window.Crazygames = window.Crazygames || {};
  if (typeof legacyRoot.requestInviteUrl !== "function") {
    legacyRoot.requestInviteUrl = function () {};
  }
})();
