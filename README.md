# Intent Logger

A custom Home Assistant integration that observes Assist pipelines, logs
speech-to-text (STT) transcripts alongside conversation/intent results, and
optionally fires an event when processing fails.

**Experimental preview.** Targets Home Assistant Core **2026.11.1** and Python
3.14. Pipeline signatures and event payloads were checked against that Core tag.
Includes unit tests and Linux API-level tests using the real Core pipeline
controller and intent handler. Installation and end-to-end speech operation
in a real Home Assistant instance have **not** yet been verified.
Do not treat this preview as production-certified.

## AI-assisted development (vibe coding)

This integration was created using a vibe-coding / AI-assisted development
workflow with GitHub Copilot in VS Code. The coding models recorded for this
work were **gpt-6.1-sol** and **gpt-6-luna** (medium reasoning). They assisted
with researching Home Assistant APIs and drafting the implementation,
documentation, and tests. The project was validated with its automated tests,
HACS validation, and hassfest, but has not yet been verified in a live
Home Assistant installation; automated checks are not a substitute for that
testing.

These models were used as development tools only. They are **not** runtime
dependencies, and this integration does not send transcripts or intent data to
them.

## Features

- Captures all Assist pipelines by default, or a selected set.
- Includes speech and typed input **when it goes through an Assist pipeline**.
- Correlates transcripts, structured responses, errors, and available handled
  intent names/slots using the unique pipeline run ID.
- Works with the built-in agent and records the pipeline responses of other
  agents, including LLM agents.
- Stores JSONL locally, with age-based retention and size-based rotation.
- Offers setup and options through **Settings > Devices & services**.
- Fires the optional `intent_logger_failed` event for automations.
- Does not record audio, execute fallback actions, or change conversation agents.
- Has no additional runtime package requirements.

## Install with HACS

This repository is intended for HACS custom-repository installation; it is
**not yet listed in the default HACS catalog**.

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/wolfgangbures/IntentLogger` as **Integration**.
3. Find **Intent Logger**, download the preview, and restart Home Assistant.
   Enable beta/pre-release versions in HACS if needed, or select the main branch.
4. Open **Settings > Devices & services > Add integration > Intent Logger**.
5. Review the privacy notice and choose the logging options.

If you cannot find the integration after restarting, refresh the browser cache.
For manual installation, copy [custom_components/intent_logger](custom_components/intent_logger)
to your Home Assistant configuration's `custom_components` directory and restart.
HACS installation itself still needs validation on a real instance.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| Pipelines | Empty | Empty captures all current and future pipelines. Select IDs/names to restrict capture; custom pipeline IDs can also be entered. |
| Maximum retention | 30 days | Segments expire based on their creation time, not their last write. Range: 1-365 days. |
| Maximum segment size | 10 MiB | Rotate before exceeding this size. Range: 1-100 MiB. |
| Maximum segments | 20 | Delete oldest segments when this count is exceeded. Range: 1-100. |
| Failure events | Enabled | Fire `intent_logger_failed` once per failed run. |

Change settings using the integration's options. Applying options reloads the
integration; runs already active during that reload might only have their
earlier snapshots logged. Start another request after reload to test capture.

## Logs and correlation

Logs are in `<config>/intent_logger/intent_logger-<UTC timestamp>-<unique ID>.jsonl`.
They are deliberately **not** stored in `www`, entity state attributes, or
Home Assistant's normal log.

Each line is a JSON object with `schema_version: 1` and:

- `run_id`, `pipeline_id`, `pipeline_name`, and UTC `timestamp`;
- `event_type`: `stt-end`, `intent-end`, `error`, or `run-end`;
- `source`: `speech` or `text` (TTS-only runs have no user input);
- `device_id`, `satellite_id`, `language`, `conversation_id`, and `agent_id`,
  where available;
- `stt_text` and `text_input` (typed messages have no STT transcript);
- `intent_output`: the structured conversation result exposed by the pipeline;
- `intents`: handled intent names, slot snapshots, and any handler exceptions;
- `errors`, `processed_locally` when available, and `status`.

**Multiple lines per run are intentional.** The `stt-end` snapshot queues the
transcript as soon as it exists, before intent processing completes.
`intent-end` adds the corresponding result. The final `run-end` snapshot is
the combined summary; filter on it for one record per execution, and join
earlier snapshots by `run_id`. `status` is `pending`, `success`, `failed`, or
`incomplete`. A run that never produces an intent result, such as an STT-only
pipeline, is `incomplete`, not an invented intent success.

An unrecognized sentence has an error response even if the pipeline technically
completed. Such a run is classified as failed. Structured partial action failures
also count as failures. A recoverable handler exception followed by a successful
fallback is included in `intents`, but does not by itself mark the final result
as failed.

The configured agent can differ from the agent that handles a local fallback.
Use `processed_locally` and the handled-intent records to interpret these runs.
LLM prose is never guessed to be a failure: an agent must report a structured
error. An LLM's tool calls are not necessarily Home Assistant intents and may
have no name/slots available through this hook. Intent names are also absent
for unmatched sentences and some sentence-trigger responses.

### Limits

- Calls directly to `conversation.process`, `intent.handle`, or an STT provider
  **outside** an Assist pipeline are not captured.
- Only calls through Home Assistant's shared intent handler during a pipeline
  are named. Agents bypassing that handler still have their pipeline result logged.
- Internal TTS/audio URLs, audio tokens, system prompts, and streamed chat-log
  deltas are not collected. The conversation result itself is retained in full.
- Pipeline validation failures occurring before execution starts emit no
  observable run events and cannot be captured.
- Writes use a single off-loop worker. Its queue is limited to 1,000 records.
  An overflow drops a record with an explicit Home Assistant log error rather
  than blocking Assist. A record larger than the selected segment limit is
  rejected with a log error; raise the size limit if your agent returns large results.
- Logging is best-effort, not an audit-grade durable journal. Abrupt termination
  can lose queued records. Disk/serialization failures are reported in Home
  Assistant's normal log; they do not interrupt Assist.
- Storage is capped at segment size multiplied by segment count (200 MiB by
  default), excluding filesystem overhead. Size-based removal can delete logs
  sooner than the age limit.
- Age cleanup runs at startup, on writes, and hourly when idle. Expiry is
  segment-based, so newer records in an old segment may be removed early.
- Unloading stops capture and flushes queued records, but does not delete files.

## Automate failed requests

Listen for `intent_logger_failed` in **Developer tools > Events**. A failed
run fires once, after execution finishes, with the final snapshot as event data.
Failures include structured intent errors (for example `no_intent_match`),
partial action failures, pipeline errors, cancellation, and unexpected execution
exceptions. An STT failure normally has `stt_text: null`.

Example automation:

```yaml
alias: Handle an unrecognized Assist request
triggers:
  - trigger: event
    event_type: intent_logger_failed
conditions:
  - condition: template
    value_template: >-
      {{ trigger.event.data.errors
         | selectattr('code', 'defined')
         | selectattr('code', 'eq', 'no_intent_match')
         | list | count > 0 }}
actions:
  - action: persistent_notification.create
    data:
      title: Assist did not recognize a request
      message: >-
        {{ trigger.event.data.text_input
           or trigger.event.data.stt_text
           or 'No transcript available' }}
mode: queued
max: 10
```

Keep any fallback automation guarded against loops. This integration never
replays an utterance automatically.

## Privacy

Transcripts, response text, intent slots, device identifiers, and event payloads
can contain private information. This integration sends none of them to an
external service; the configured conversation/STT agents keep their own behavior.
Protect access to the config directory and backups. No redaction is applied.
On systems that honor POSIX permissions, new directories/files request modes
`0700`/`0600`; pre-existing permissions and platform ACLs are unchanged.

Failure events expose the same data to event listeners, automations, and
potentially Recorder. If desired, merge this exclusion into your existing
Recorder configuration (it does not configure this integration):

```yaml
recorder:
  exclude:
    event_types:
      - intent_logger_failed
```

Log-file retention does **not** delete Recorder history, automation traces,
notifications, copies, or backups. Disable failure events if you only need files.
To erase logs, first disable/unload the integration, then delete its log segments.

## Compatibility and implementation

Home Assistant does not expose a supported global Assist event subscription.
This preview wraps `PipelineRun.async_execute`, `PipelineRun.process_event`, and
`homeassistant.helpers.intent.async_handle`. It preserves the original client
callback, return values, and exceptions. A task-local context associates handled
intents with the correct run, including simultaneous conversations.

Setup checks the relevant parameter names before installing hooks. This is a
guard, **not a guarantee** that a future release's semantics remain compatible.
On unload, original methods are restored unless another integration has
subsequently wrapped them; in that case the retained chained wrapper is disabled
and a warning is logged. Core upgrades and integrations that also patch these
methods need particular testing.

## Development and release status

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --group dev
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

On Linux, also install `python -m pip install --group ha-test` to run Core
API-level tests. The audio libraries are not supported by this Windows test
environment, so that test module is skipped on Windows.

Tests cover result correlation, simultaneous pipelines, failure events,
cancellation, hook restoration, overflow/write failures, rotation, retention,
setup/unload, and UI configuration schemas. Linux CI also runs hassfest and
HACS validation. Automated tests replace external speech/agent work with a test
processor; they do not replace testing with a real microphone and agents.

Before a stable release:

1. Check that the Linux tests, hassfest, and HACS validation pass.
2. Install through HACS in a test instance; exercise real speech, typed Assist,
   an unrecognized sentence, concurrent requests, and a pipeline/STT error.
3. Verify options reload, shutdown flushing, and removal restore normal Assist.

Default-catalog discovery requires a separate HACS submission/review; GitHub
Marketplace is not the installation mechanism for Home Assistant integrations.

## License

[MIT](LICENSE)
