# Race signals during lap speech

Staging tones, the race-start buzzer, final countdown tones, the timer-end buzzer, spoken remaining-time announcements, and spoken scheduled-start countdowns have a dedicated signal priority. On the service, a signal cancels active speech and clears buffered audio when the previous playback was speech. Pending ordinary lap announcements are dropped; interrupted speech is not replayed. Pending high-priority voice messages are retained, and high-priority voice messages do not themselves interrupt playback. Consecutive signals stay queued in order.

Both the plugin and the separate Sendspin service must be updated for signal preemption. Signal expiry and requested playback timestamps still apply; transport latency and client preparation time can still delay a signal.

Spoken countdowns still need their cached audio or synthesis before they can enter the playback queue. Prepare the pre-cache before racing to avoid generating those phrases during the countdown.
