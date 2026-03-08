// ============================================================
// MOCK ADSENSE - Reklamlar devre dışı, oyun odağı korunuyor
// ============================================================

// AdSense script YÜKLENMEYECEK - tamamen mock

// Mock adsbygoogle array
window.adsbygoogle = window.adsbygoogle || [];
window.adsbygoogle.push = function(o) {
    console.log('[MockAds] adsbygoogle.push called');
};

// Mock adBreak ve adConfig
const adBreak = adConfig = function (o) {
    console.log('[MockAds] adBreak/adConfig called:', o?.type || 'config');
    
    // onReady callback'i hemen çağır
    if (o && o.onReady) {
        setTimeout(() => o.onReady(), 10);
    }
    
    // Ad break callbacks
    if (o && o.type) {
        setTimeout(() => {
            // beforeAd çağırma - reklam yok
            
            // afterAd çağırma - reklam yok
            
            // Reward tipi için
            if (o.type === 'reward') {
                // adViewed - ödül verildi
                if (o.adViewed) {
                    console.log('[MockAds] Auto reward given');
                    o.adViewed();
                }
            }
            
            // adBreakDone her zaman çağrılmalı
            if (o.adBreakDone) {
                const placementInfo = {
                    breakType: o.type,
                    breakName: o.name || 'mock-ad',
                    breakFormat: 'interstitial',
                    breakStatus: 'viewed' // Reklam "gösterildi" olarak işaretle
                };
                o.adBreakDone(placementInfo);
            }
            
            // Oyuna odaklan
            refocusCanvas();
            
        }, 50); // Çok kısa gecikme
    }
};

// Reklam sayacı
var cnt = 0;

// SDK Init - Hemen başarılı
function InitSDKJs() {
    console.log('[MockAds] InitSDKJs called');
    
    // Hemen başarılı olarak bildir
    setTimeout(() => {
        if (typeof myGameInstance !== 'undefined' && myGameInstance) {
            myGameInstance.SendMessage('RHMAdsManager', 'InitSucceed', 'mock-ads-disabled');
            console.log('[MockAds] SDK Init success sent');
        }
    }, 100);
}

// Interstitial Ads - Skip
function CallInterstitialAdsJs() {
    console.log('[MockAds] CallInterstitialAdsJs - SKIPPED');
    cnt++;
    
    // Hemen resume et
    setTimeout(() => {
        resumeGameAfterAds();
        refocusCanvas();
    }, 50);
}

// Interstitial Pause Ads - Skip
function CallInterstitialAdsPauseJs() {
    console.log('[MockAds] CallInterstitialAdsPauseJs - SKIPPED');
    
    // Hemen resume et
    setTimeout(() => {
        resumeAudioAfterAds();
        refocusCanvas();
    }, 50);
}

// Load Rewarded Ads - Her zaman "loaded"
function LoadRewardedAdsJs() {
    console.log('[MockAds] LoadRewardedAdsJs - Auto loaded');
    
    // Hemen loaded olarak bildir
    setTimeout(() => {
        RewardedAdsLoaded();
    }, 50);
}

// Call Rewarded Ads - Otomatik ödül ver
function CallRewardedAdsJs() {
    console.log('[MockAds] CallRewardedAdsJs - Auto reward');
    
    // Otomatik ödül ver
    setTimeout(() => {
        RewardSuccessful();
        refocusCanvas();
    }, 100);
}

// Rewarded Ads Loaded
function RewardedAdsLoaded() {
    console.log('[MockAds] Rewarded Ads Available');
    if (typeof myGameInstance !== 'undefined' && myGameInstance) {
        myGameInstance.SendMessage('RHMAdsManager', 'isRewardedAdsLoaded', 'true');
    }
}

// Rewarded Ads Not Loaded
function RewardedAdsNotLoaded() {
    console.log('[MockAds] Rewarded Ads Not Available');
    if (typeof myGameInstance !== 'undefined' && myGameInstance) {
        myGameInstance.SendMessage('RHMAdsManager', 'isRewardedAdsLoaded', 'false');
    }
}

// Rewarded Ads Dismissed
function RewardedAdsDismissed() {
    console.log('[MockAds] Reward Dismissed');
    if (typeof myGameInstance !== 'undefined' && myGameInstance) {
        myGameInstance.SendMessage('RHMAdsManager', 'RewardedAdsFailed');
    }
    refocusCanvas();
}

// Reward Successful
function RewardSuccessful() {
    console.log('[MockAds] Reward Successful');
    if (typeof myGameInstance !== 'undefined' && myGameInstance) {
        myGameInstance.SendMessage('RHMAdsManager', 'RewardedAdsSuccessfull');
    }
    refocusCanvas();
}

// Pause Game
function pauseGameBeforeAds() {
    console.log('[MockAds] pauseGame (skipped - no ads)');
    // Reklam yok, pause etmeye gerek yok
}

// Resume Game
function resumeGameAfterAds() {
    console.log('[MockAds] resumeGame');
    if (typeof myGameInstance !== 'undefined' && myGameInstance) {
        myGameInstance.SendMessage('RHMAdsManager', 'resumeGame');
    }
}

// Pause Audio
function pauseAudioBeforeAds() {
    console.log('[MockAds] pauseAudio (skipped - no ads)');
    // Reklam yok, pause etmeye gerek yok
}

// Resume Audio
function resumeAudioAfterAds() {
    console.log('[MockAds] resumeAudio');
    if (typeof myGameInstance !== 'undefined' && myGameInstance) {
        myGameInstance.SendMessage('RHMAdsManager', 'resumeAudio');
    }
}

// Canvas Focus - ÖNEMLİ
function refocusCanvas() {
    console.log('[MockAds] Refocusing canvas');
    
    // canvas değişkenini bul
    var canvas = document.querySelector('#unity-canvas') || document.querySelector('canvas');
    
    if (canvas) {
        if (!canvas.hasAttribute('tabIndex')) {
            canvas.setAttribute('tabIndex', '0');
        }
        canvas.focus();
        
        // Window focus da yap
        window.focus();
    }
}

// Dil ayarı - İngilizce
function GetLanguage() {
    return 'en';
}

function GetDeviceType() {
    return 'desktop';
}

function IsMobile() {
    return 0;
}

console.log('[MockAds] Mock AdSense loaded - All ads disabled, language: EN');