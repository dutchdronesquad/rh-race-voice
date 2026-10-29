# Local efficiency measurements

These measurements were taken on 2026-10-25 using the existing `nl_NL-pim-medium` model on a four-CPU x86_64 host with Python 3.13.3, Piper 1.8.0 and ONNX Runtime 1.31.0. They exercise real synthesis in isolated temporary caches and never play audio or change the running service. They do not measure the operator's Raspberry Pi or passage-to-speaker latency, and are not hardware acceptance for #304.

## Changes supported by the measurements

The supervisor now retains reusable pilot, lap-number and countdown WAV bytes in a bounded RAM cache. Model/config metadata checks stay off the event loop, and model changes, voice settings, full cache clears and worker failure are covered by regression tests. Heat cleanup preserves reusable entries. An uncached lap requires one worker command for the new time instead of three worker commands for name, number and time. The name/number audio remains unchanged.

Manual pre-cache preparation now explicitly warms Piper. Previously, a successful preparation could reuse every WAV without loading the inference session, leaving model loading to the first uncached lap time. Warmup happens during explicit preparation, and a failure prevents a misleading ready notification. It does not add automatic synthesis at startup.

The ONNX pool keeps its existing one/two-thread limit and now uses a 1 ms spin window with a maximum backoff of eight. This follows the [ONNX thread-management controls](https://onnxruntime.ai/docs/performance/tune-performance/threading.html). The small tuning experiment used eight unique numeric time phrases per variant and a 200 ms pause after each phrase. CPU time includes all threads in the benchmark process.

| Thread configuration | Median generation wall time | Median generation CPU time | Median CPU during the following 200 ms pause |
| --- | ---: | ---: | ---: |
| Two threads, default spin | 209.3 ms | 417.1 ms | 22.2 ms |
| Two threads, 1 ms spin/backoff 8 | 207.4 ms | 413.4 ms | 3.2 ms |
| Two threads, no spin | 220.8 ms | 413.7 ms | 0.04 ms |
| One thread | 393.8 ms | 393.8 ms | 0.03 ms |

The bounded-spin setting reduced CPU spent waiting after synthesis in this experiment without a visible generation-time penalty. The experiment is too small to establish a throughput guarantee. Native generation still accounts for almost all preparation CPU time, and the ARM result may differ.

## Reproduce cache and restart measurements

The final comparison ran separately from the regression suite. Raw results, including all four thread variants, are in [the measurement artifact](measurements/2026-10-25-efficiency.json).

| Work measured | Disk reuse only | Bounded RAM reuse |
| --- | ---: | ---: |
| Worker commands for 30 new lap times | 90 | 30 |
| Median complete lap preparation | 214.8 ms | 215.4 ms |
| Median cached name/number pair lookup | 1.725 ms | 0.431 ms |
| Worker commands for 100 cached name/number pairs | 200 | 0 |

RAM reuse cut the worker command count for normal lap preparation by two thirds and cached pair lookup time by about four times. Complete preparation of a new time did not get measurably faster: neural synthesis dominates that path. This avoids confusing cache microbenchmarks with an end-to-end improvement.

The restart comparison was more significant. Against an existing WAV cache, preparation without model warmup took 319 ms, but the first new lap still took **2,268 ms** to prepare. With explicit warmup, setup took 2,632 ms and the first new lap took **265 ms**. This transfers work into the operator's explicit preparation step; it does not remove initialization work. Each restart variant is a single observation, so repeat it on the actual timer before claiming a race-day latency bound.

Use an idle machine with the service development dependencies installed and an existing model directory:

```sh
uv run --extra sendspin-service python -m tools.benchmark_race_voice \
  --model-dir /path/to/existing/models \
  --model nl_NL-pim-medium \
  --laps 30 --cached-rounds 100 \
  --output race-voice-efficiency.json
```

The benchmark creates temporary cache directories and symlinks the supplied ONNX/config files. It compares the same worker with RAM reuse disabled and enabled. Each new time is unique within that run, so generation cannot silently turn into a disk-cache hit. It also starts fresh workers against existing reusable WAVs, with and without model warmup, and measures the first new lap time. Results include environment versions and distinguish cache lookup time, worker-command count, setup time and lap preparation time. Run it separately from tests or other CPU-intensive work. Native timing is noisy; fewer worker commands are a direct work reduction, not proof of an equal reduction in total latency.

## First audio availability with prepared laps

A follow-up run on the same host/model exercised thirty unique new lap times with the early local prefix path. Raw results are in [the progressive-lap measurement artifact](measurements/2026-10-25-progressive-laps.json). The disk-only variant had no early prefixes; the RAM variant exposed its prefix before the time finished on all thirty laps.

| Work measured | Median | 95th percentile |
| --- | ---: | ---: |
| First audio ready, disk-only complete preparation | 217.8 ms | 235.3 ms |
| First audio ready, prepared RAM prefix | 0.734 ms | 3.885 ms |
| Complete announcement ready, RAM prefix path | 229.9 ms | 311.8 ms |

This removes waiting for new time synthesis from the first-audio path. It does not speed up that synthesis: the complete-announcement measurements varied, and no throughput improvement is established here. These figures stop at the speech engine's audio-ready callback; they exclude event delivery, output queueing, Sendspin scheduling and player buffering. Deterministic tests separately verify that local backend submission happens while time synthesis is blocked, that other pilots cannot interleave, and that stop, signal, failure and expiry paths discard the continuation.

## Remaining limits

Initial subprocess imports and model setup took about 2.4 seconds in the exploratory runs. A new time phrase took about 0.22 seconds on this host. RAM reuse removes repeated file/pipe work; it does not remove the neural synthesis required for a new lap time. The model must still be prepared after a service restart if first-lap responsiveness matters.

Speech duration also limits throughput. For example, three-second announcements allow at most twenty complete announcements per minute even if synthesis takes no time. If crossings exceed that rate, shorter announcements or deliberately dropping older speech are necessary. The queue limits retain recent callouts and preserve the full active announcement; they cannot make overlapping speech intelligible.

The local playback path now starts a resident cached name/number prefix while the time is being generated. It reserves the same callout slot for the time, shields shared synthesis from individual output cancellation, and retains stop/expiry handling. Cold or disk-only prefixes still wait for complete preparation; cloud relay also waits for the complete announcement. The benchmark reports `first_audio_ready` separately from complete preparation, and `early_cached_prefixes` counts how often this path was exercised. This measures availability to playback, not actual sound. Sendspin normally schedules at least 250 ms ahead for player synchronization, and a slow or failed time synthesis can still cause a pause or incomplete spoken callout. Real race traces, player diagnostics and audible validation are needed to establish gate-to-speaker latency.
