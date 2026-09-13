# Race signals during lap speech

Staging tones, the race-start buzzer, final countdown tones, and the timer-end buzzer have a dedicated signal priority. On the service, a signal cancels active speech and clears buffered audio when the previous playback was speech. Pending ordinary lap announcements are dropped; interrupted speech is not replayed. Pending high-priority voice messages are retained, and high-priority voice messages do not themselves interrupt playback. Consecutive signals stay queued in order.

Both the plugin and the separate Sendspin service must be updated for signal preemption. Signal expiry and requested playback timestamps still apply; transport latency and client preparation time can still delay a signal.
