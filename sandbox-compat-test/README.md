# GeoTrace sandbox compatibility test

This directory is an evidence-first compatibility harness for the GeoTrace
forensic workload. It intentionally contains no credentials.

## Documentation reviewed (2026-09-02)

* Cloudflare Sandbox SDK documentation: current stable is the 0.12.x line;
  Cloudflare recommends the `@cloudflare/sandbox@next` 1.0 preview for new
  projects. The preview uses `sandbox.exec(argv)` and process handles with
  resumable log cursors. It is a Workers/Containers SDK, not a public shared
  account-level shell API.
* Sparkles Sandbox API OpenAPI 3.1.0, API version `1.0.0`, plus its Sandbox
  quickstart and Sandbox events documentation. It creates managed coding
  sandboxes from a GitHub repo plus prompt. Its SSE stream is curated; it does
  not expose arbitrary runtime stdout.

## Safe execution prerequisites

Export credentials in the invoking shell; never paste them into a file:

```sh
export CLOUDFLARE_API_TOKEN='...'
export CLOUDFLARE_ACCOUNT_ID='...'
export SPARKLES_API_KEY='...'
export SPARKLES_REPO='owner/repository'
```

Cloudflare additionally needs a deployable Worker/Containers Sandbox project,
Containers/Sandbox entitlement, and a running local Docker daemon for the
documented deploy path. Sparkles needs a repository accessible to its GitHub
App. Use a disposable repository containing only this directory.

## Runners

`runners/cloudflare-test.ts` is a Worker handler using the documented 1.0
preview process model. Bundle `workload/*.py` and `workload/*.txt` as text
modules, deploy it in a Sandbox-enabled Worker project, then request its
endpoint and save the SSE body as `results/cloudflare-events.ndjson`.

`runners/sparkles-test.mjs` creates the Sparkles sandbox, persists sanitized
SSE envelopes, and retrieves the agent-written result file. It requires Node
18+ and the variables above:

```sh
node runners/sparkles-test.mjs
```

The Sparkles prompt requires the agent to execute the workload and write
`results/sparkles-runtime-result.json`; a narrative response alone is not
accepted as evidence.

## Workload contract

Run the probe from a venv after installing `workload/requirements.txt`:

```sh
python geotrace_probe.py --install --result results/runtime-result.json
```

It emits line-delimited JSON telemetry to stdout and writes a JSON report. It
uses a public Golden Gate Bridge image (rough expected region: San Francisco,
California) to exercise network download, Pillow/OpenCV preprocessing,
optional Tesseract OCR, and two genuine CPU GeoCLIP predictions.

No entry in `results/` constitutes a provider result until its `execution`
field says `completed` and has command/event evidence.
