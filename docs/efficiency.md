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

This removes waiting for new time synthesis from the first-audio path. It does not speed up that synthesis: the complete-announcement measurements varied, and no throughput improvement is established here. These figures stop at the speech engine's audio-ready callback; they exclude event delivery, output queueing, Sendspin scheduling and player buffering. These historical measurements describe the early-prefix behavior released in 2.1.0. Local playback now waits for complete audio to prevent synthesis pauses within an announcement. Regression tests verify that no prefix is submitted while time synthesis is blocked, other pilots cannot interleave, and stop, signal, failure and expiry paths discard incomplete audio.

## Cori High and Medium comparison (2026-11-22)

A race-day operator reported using Cori High, probably on a Raspberry Pi 4. The following comparison uses the merged audio fixes at `89a3d1c` on the development host: four x86_64 virtual CPUs, Python 3.13.3, Piper 1.8.0, ONNX Runtime 1.31.1 and the production two-thread configuration. It is not a measurement of that Pi. Both models were downloaded into an isolated benchmark directory; production models, settings and race caches were unchanged. The [measurement artifact](measurements/2026-11-22-cori-models.json) records file hashes, parameters and the full results.

The production benchmark ran separately for each model with 12 unique lap times, prepared name/number segments and 30 cached-pair lookups. These figures measure complete speech preparation, excluding HTTP, output queues, client buffering and speakers.

| Warm preparation | Cori High | Cori Medium |
| --- | ---: | ---: |
| New lap, median | 1,965 ms | 198 ms |
| New lap, 95th percentile | 2,272 ms | 201 ms |
| Cached name/number pair, median | 0.473 ms | 0.447 ms |

A separate 30-time phase profile attributed over 99% of generation wall time to ONNX inference for both models. Cori High's 30 generations spent 59.53 seconds in inference out of 59.57 seconds total; phonemization took 10 ms across that entire run. This points to model compute rather than additional Python or reusable-segment caching as the primary cost on this host. The observed roughly tenfold difference between models must not be projected onto ARM hardware without measuring it.

Following this comparison, High-quality models were removed from the supported race-voice catalog. Saved Cori, Lessac and Thorsten High selections migrate to the matching Medium voice. Run **Prepare pre-cache** before racing to prepare the replacement. These timing results cover Cori only; they do not establish a numerical speedup for Lessac or Thorsten. A possible further optimization is explicitly bounded preparation of common complete lap-time phrases, preserving RH's phonetic format and normal synthesis on cache misses. That is not implemented here. More inference threads also need Pi measurements alongside RH responsiveness before changing the existing CPU budget.

To reproduce the historical High comparison, use the benchmark and model catalog at revision `89a3d1c`; the current catalog no longer accepts High models. Run once per model with an existing model directory, `--laps 12 --cached-rounds 30`, and a distinct output file. Model warmup remains useful for startup: in the Cori High restart observation, the first new lap took 4,291 ms with cached WAVs alone versus 1,869 ms after explicit warmup. Each restart variant is one observation, not an acceptance bound; warmup does not remove the inference needed for subsequent new times.

## Remaining limits

Initial subprocess imports and model setup took about 2.4 seconds in the exploratory runs. A new time phrase took about 0.22 seconds on this host. RAM reuse removes repeated file/pipe work; it does not remove the neural synthesis required for a new lap time. The model must still be prepared after a service restart if first-lap responsiveness matters.

Speech duration also limits throughput. For example, three-second announcements allow at most twenty complete announcements per minute even if synthesis takes no time. If crossings exceed that rate, shorter announcements or deliberately dropping older speech are necessary. The queue limits retain recent callouts and preserve the full active announcement; they cannot make overlapping speech intelligible.

The speech engine can expose resident cached name/number segments while the time is being generated. Outputs reserve the same callout slot, shield shared synthesis from individual output cancellation, and retain stop/expiry handling. Local playback waits for the complete announcement. The cloud relay can upload the available segments during synthesis but publishes only one complete event. This favors continuous speech over the earliest possible local start; it does not promise simultaneous local and cloud sound. The benchmark still reports `first_audio_ready` separately from complete preparation, and `early_cached_prefixes` counts early availability to outputs rather than audible playback. Real race traces, player diagnostics and audible validation are needed to establish gate-to-speaker latency.
