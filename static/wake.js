/* Keep the screen awake while the app is open.
 *
 * The problem: your phone counts seconds since the last touch and locks when it
 * hits the limit. Mid-keertan, with a shabad on screen and both hands busy,
 * that is exactly wrong -- and tapping the screen every thirty seconds to stop
 * it is not a solution.
 *
 * The Screen Wake Lock API asks the browser to hold an OS-level lock (a
 * PowerManager lock on Android, the idle-timer equivalent on iOS). While it is
 * held the idle timer is SUSPENDED -- not repeatedly reset, actually suspended.
 *
 * NOT NoSleep.js. That library predates this API and works by playing a hidden
 * looping video, which costs battery and can grab the audio session -- a real
 * problem in an app you use while a recording might be playing. The native API
 * needs no dependency and no media element.
 *
 * REQUIRES HTTPS. `navigator.wakeLock` does not exist in an insecure context,
 * so http://<lan-ip>:8000 straight from a phone cannot do this. Through the
 * Cloudflare tunnel the browser sees https and it works; the plain http hop
 * from cloudflared to this server is invisible to the browser and irrelevant.
 */

const wake = {
  want: true,                 // matches DEFAULT_SETTINGS.wake_lock on the server
  lock: null,
  supported: typeof navigator !== 'undefined' && 'wakeLock' in navigator,
  secure: typeof window === 'undefined' || window.isSecureContext !== false,
  reason: '',
};

/* THE LOAD-BEARING PART.
 *
 * The browser releases the lock every time the page stops being visible -- an
 * app switch, a pulled-down notification shade, a manual press of the power
 * button -- and it does NOT restore it when you come back. The sentinel is
 * permanently dead at that point.
 *
 * So this is called again on every return to visibility. Without that the lock
 * works exactly once per app launch and then silently stops, which reads as a
 * broken feature rather than an expired lock.
 */
async function wakeAcquire() {
  if (!wake.want || wake.lock) return;
  if (!wake.supported) {
    wake.reason = wake.secure ? 'this browser has no Wake Lock API'
                              : 'needs https (the page is not a secure context)';
    return;
  }
  // Requesting while hidden always fails; not an error worth recording.
  if (document.visibilityState !== 'visible') return;
  try {
    const lock = await navigator.wakeLock.request('screen');
    wake.lock = lock;
    wake.reason = '';
    // Fires on the automatic release too, so our idea of whether we hold one
    // never drifts from the browser's.
    lock.addEventListener('release', () => {
      if (wake.lock === lock) wake.lock = null;
    });
  } catch (err) {
    // Rejects on Low Power Mode, an OS policy refusal, or no user activation
    // yet. All are ordinary; none may escape into the app.
    wake.lock = null;
    wake.reason = `${err.name}: ${err.message}`;
  }
}

async function wakeRelease() {
  const lock = wake.lock;
  wake.lock = null;
  if (lock) { try { await lock.release(); } catch { /* already gone */ } }
}

function setWakeLock(on) {
  wake.want = !!on;
  if (on) wakeAcquire(); else wakeRelease();
}

// For the control panel: why it is or isn't working, in words.
function wakeStatus() {
  if (!wake.want) return { state: 'off', detail: 'Switched off in Settings.' };
  if (wake.lock) return { state: 'active', detail: 'The screen will not sleep.' };
  if (!wake.secure)
    return { state: 'blocked', detail: 'Needs https. Open the app through the '
             + 'Cloudflare tunnel rather than a plain http address.' };
  if (!wake.supported)
    return { state: 'unsupported', detail: 'This browser has no Wake Lock API.' };
  return { state: 'idle', detail: wake.reason
           || 'Not held yet — it is taken on the first tap.' };
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') wakeAcquire();
});

// The API is gated behind user activation, so a page cannot flatten a battery
// just by being opened from a link. On a cold load there may not have been one
// yet, so the first request can be refused -- retry on the first tap. Kept
// listening rather than once: it is also the cheapest way back after any
// release we did not see. wakeAcquire returns immediately if a lock is held.
document.addEventListener('pointerdown', wakeAcquire, { passive: true });

(async () => {
  // Acquire optimistically on the server default, then correct if the stored
  // setting says otherwise. Waiting on the fetch first would give up the
  // best chance of getting the lock -- the moment the page opens.
  wakeAcquire();
  try {
    const s = (await api('/api/settings')).settings;
    setWakeLock(s.wake_lock !== '0');
  } catch { /* keep the default */ }
})();
