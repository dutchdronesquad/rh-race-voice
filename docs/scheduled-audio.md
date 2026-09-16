# Scheduled tone playback

Scheduled audio uses the same 50 ms PCM chunks as normal speech. Short scheduled clips include 100 ms of trailing silence to feed buffered resampling/encoding, and idle stream shutdown waits an additional 100 ms after the scheduled tail. WAV assets are unchanged.

The service respects the connected players' reported preparation time (at least 250 ms). A race timestamp with sufficient advance notice is preserved. If audio arrives too late, playback moves to the earliest supported time and logs the extra delay; a clip that would then start beyond its expiry is dropped. This cannot make an already late race event play on time. Compare WindowsSpin and the browser player with isolated tones, countdowns, and heavy lap traffic before race-day rollout.
