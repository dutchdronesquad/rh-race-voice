# Stopping audio and changing heats

Stopping audio or switching heats invalidates pending voice synthesis and prevents already running synthesis from adding obsolete audio afterward. Stop requests are serialized after any in-flight upload on each output, then fresh audio can resume. A slow cloud upload does not delay the local output's stop. Heat changes retain reusable pre-cache files.
