# Daily sync on AWS

The same integration as [`app.py`](../app.py), running unattended once a day as
an **AWS Step Functions state machine**. Every fetch and every mapping step is a
native state; the only code is one small Lambda that performs the POST, for the
reason below. The state machine emails a report of every run.

`app.py` remains the reference implementation and the only way to produce a
[CSV bulk-import file](../README.md#bulk-import-via-csv). This deployment covers
the API-submission path only.

## Why (almost) no Lambda

The two things that would normally force a Lambda — calling a third-party API,
and reshaping JSON — are handled by the
[HTTP Task](https://docs.aws.amazon.com/step-functions/latest/dg/connect-third-party-apis.html)
and by [JSONata](https://docs.aws.amazon.com/step-functions/latest/dg/transforming-data.html).
So the reference fetches, the score fetch, the whole mapping and the report are
native states: no deployment package, no cold start, and the transformation is
readable in the definition rather than hidden inside code.

**The POST is the exception, and it has to be.** When an API returns a non-2xx,
the HTTP Task throws and **discards the response body**. All that survives is
the error name and the HTTP status text:

```
"error": "States.Http.StatusCode.400",
"cause": "Bad Request"          <- the entire cause
```

Golden Records puts the reason a score was rejected in that body
(`{"errors":["This score already exists in the database."]}`), so with an HTTP
Task there is no expression that can reach it: every rejection reports as "Bad
Request", and duplicates cannot be told apart from genuine validation errors.
There is no option to make the task succeed on a 4xx — the raw response is
visible only through `TestState --inspection-level TRACE`, a debugging feature
not available to a running execution.

So `PostScore` invokes a small Python function
([`function/submit_score/app.py`](function/submit_score/app.py)) that POSTs the
score and returns `{statusCode, ok, duplicate, errors}` for *any* status. It
raises `RetryableStatus` on 429, 5xx and connection failures so the state
machine's own `Retry` policy handles everything transient, and it reads the API
key from the EventBridge connection's own secret so the key still lives in
exactly one place.

It uses `requests` (see [`requirements.txt`](function/submit_score/requirements.txt)),
the same client `app.py` uses, which is why **`sam build` is part of deploying**
— see [Deploying](#deploying).

The other cost of the native-state approach is that mapping logic is expressed
in JSONata, and **JSONata is not validated at deploy time** — a bad expression is
first discovered by a run failing. There is no local checker; use `--input
'{"dryRun":true}'` after any change to the definition (see
[First-run smoke test](#first-run-smoke-test)).

## What it does

```
Prepare                 read the name mappings from SSM, derive the date window
  ↓
FetchRounds     ─┐
FetchBowTypes    │  4 paginated GETs to Golden Records. Each page is projected
FetchAgeGroups   │  straight down to a {name: id} map, so the 82 KB rounds
FetchMembers    ─┘  payload never travels past the state that fetched it.
  ↓
FetchScores             1 GET to ExpertArcher for the window
  ↓
ProcessScores           Map, MaxConcurrency 1, one record at a time:
                          resolve names → validate → build the POST body
                          → wait 3s → PostScore (Lambda) → classify as
                            submitted / duplicate / rejected-with-the-API's-
                            -own-message, or skipped with a reason
  ↓
BuildReport → SendReport (SNS email)
```

Each `Fetch*` state is followed by a `*Complete` Choice and a `*Paginate` state:
the Golden Records `paging-headers` response header is parsed, and while
`nextPage` is `"Yes"` the next page is fetched and merged into the same map.

### Files

```
template.yaml                    SAM template: connections, SNS topic, SSM
                                 parameter, Lambda, state machine, schedule
statemachine/sync.asl.yaml       The state machine definition (YAML ASL, JSONata)
function/submit_score/app.py     The one Lambda: POST a score, report the outcome
function/submit_score/           requests, resolved into .aws-sam/build by
  requirements.txt               sam build
samconfig.toml                   Deploy settings — region, stack name, non-secret
                                 parameters
```

The name overrides in [`mappings.yaml`](../mappings.yaml) are the single source
of truth for both paths. At deploy time CloudFormation inlines that file
(`AWS::Include`) and serialises it to JSON (`Fn::ToJsonString`) into an SSM
parameter, which `Prepare` reads with `$parse()`. Nothing is generated into the
definition, so the CSV and API paths cannot drift.

```bash
cd aws && sam validate --lint      # template only; does not check JSONata
```

## Deploying

Requires the SAM CLI and credentials for the target account.

```bash
cd aws
sam build
sam deploy --parameter-overrides \
    GoldenRecordsApiKey=<club API key> \
    ExpertArcherApiKey=<key> \
    ReportEmail=you@example.com
```

**Always `sam build` first.** It installs `requests` into `.aws-sam/build`, and
once that directory exists `sam deploy` takes `.aws-sam/build/template.yaml` in
preference to `template.yaml` — so a deploy that skips the build ships the
*previously built* function, silently, however recently `app.py` changed. `sam
build` resolves the linux/aarch64 wheels for the `arm64` runtime regardless of
the machine you build on; no Docker or `--use-container` is needed.

`sam deploy` cannot reuse a previous parameter value, so the two API keys must be
supplied on every deploy. To avoid retyping them, copy `samconfig.toml` to
`samconfig.local.toml` (gitignored), add them to `parameter_overrides`, and use:

```bash
sam build && sam deploy --config-file samconfig.local.toml
```

`samconfig.toml` pins the region to **eu-west-2** deliberately: the `AWS_REGION`
environment variable on this machine says `us-west-2` while the configured
profiles say `eu-west-2`, and which one wins should not be left to chance.

**AWS will email a subscription confirmation** on first deploy. Until that is
accepted, no reports arrive.

`ScheduleState` defaults to `DISABLED` so the first deploy does not start
submitting overnight before the smoke test below has been run.

### Credentials

Both API keys are `NoEcho` parameters, kept out of the console and out of
`describe-stacks`. EventBridge stores each in a Secrets Manager secret it owns
and injects it at call time, so **no key reaches the state machine definition,
the execution input, or the execution history**.

`SubmitScoreFunction` reads the Golden Records key from that same
EventBridge-owned secret (`GoldenRecordsConnection.SecretArn`) rather than taking
it as an environment variable, so the key is still typed once and stored once —
nothing is duplicated into the function's configuration. The secret's shape
(`{"api_key_name", "api_key_value"}`) is EventBridge's, not ours, so the function
treats a missing field as fatal instead of guessing; if AWS ever changes it, the
POST fails loudly rather than sending an unauthenticated request.

Golden Records needs the **club-level API key**, sent as
`Authorization: Basic <key>` — the key sits where the base64 `user:pass` value
normally would. HTTP Basic Auth is *not* a workable alternative here: it returns
only the calling user's own member record, which would leave every other archer
reported as an unmatched member name. See
[the authentication notes](../README.md#authentication-notes-for-golden-records).

## Running it by hand

The state machine is named after the stack, so with the default stack name the
ARN is `arn:aws:states:eu-west-2:<account-id>:stateMachine:hb-score-sync`. The
template declares no outputs; `aws stepfunctions list-state-machines --region
eu-west-2` will also find it.

Execution input (all fields optional; the schedule sends `{}`):

| Field | Default | Meaning |
|---|---|---|
| `dryRun` | `false` | Map and report, submit nothing. Also accepts `"true"`. |
| `from` | yesterday | Start of the window, `YYYY-MM-DD`. `null` sends no lower bound. |
| `to` | today | End of the window, `YYYY-MM-DD`. `null` sends no upper bound. |
| `pageSize` | `ReferencePageSize` (500) | Page size for the reference fetches. |

Both dates are derived from the execution start time, so a scheduled run covers
yesterday and today. Passing `null` explicitly is different from omitting the
field: omitting it uses the default, `null` drops the query parameter entirely
and asks ExpertArcher for everything it has.

## First-run smoke test

Do these in order. The dry run makes no writes to Golden Records.

```bash
# 1. Dry run: fetch, map, report. Submits nothing.
aws stepfunctions start-execution --region eu-west-2 \
  --state-machine-arn <arn> --input '{"dryRun":true}'
```

Check the report email against a local `python app.py --dry-run --from … --to …`
over the same window. The counts and the skip reasons should agree.

```bash
# 2. A real run over a single day, to confirm submission end to end.
aws stepfunctions start-execution --region eu-west-2 \
  --state-machine-arn <arn> --input '{"from":"2026-01-01","to":"2026-01-01"}'
```

Then set `ScheduleState=ENABLED` and redeploy.

### Still unverified

Authentication on both APIs, the `paging-headers` parsing and the mapping are all
confirmed by successful runs. These are the parts no run has exercised yet — the
failing state and its input and output are in the execution history in the Step
Functions console.

| # | Assumption | If it is wrong |
|---|---|---|
| 1 | The `States.Http.StatusCode.*` and Lambda **retry error names** are right | Retries do not fire; a transient 503 fails the run instead of being retried. |
| 2 | A Python exception surfaces to `Retry` as its **class name** (`RetryableStatus`) | A throttled POST is not retried and reports as `submission failed: <error>`. Add `States.TaskFailed` to the retrier. |
| 3 | The **paging loop** itself (no table currently exceeds one page) | A second page is never merged, silently turning valid scores into "unmatched round" skips. |
| 4 | The EventBridge connection **secret shape** stays `{"api_key_name","api_key_value"}` | Every POST fails with a `KeyError`, reported as `submission failed`. The GETs are unaffected — they never read the secret directly. |

`Fn::ToJsonString` inlining `AWS::Include` is worth confirming once after the
first deploy — it should print a JSON object starting `{"afb":"American Flatbow"`:

```bash
aws ssm get-parameter --name /hb-score-sync/name-mappings --region eu-west-2 \
  --query Parameter.Value --output text
```

## Operating it

**Reading the report.** Counts first, then up to three sections, each with its
entries grouped and counted so a problem affecting 30 records is one line:

- `SKIPPED RECORDS BY REASON` — one block per reason (`unmatched round`,
  `invalid number`, …), and within it one line per distinct problem with
  `(N records)`. Same collapsing as `app.py`'s report. Sections and the lines
  inside them are sorted alphabetically.
- `SUBMISSION ERRORS` — one block per message the API returned *verbatim*, with
  the archer and date of each record that hit it. A rejection whose body carried
  no `errors` list falls back to `HTTP <code> with no error detail`.
- `RECORDS THAT WOULD BE SUBMITTED` — dry runs only.

Duplicates are counted but not listed, as in `app.py`. They are matched on the
API's own message (`"This score already exists in the database."`) and kept out
of `SUBMISSION ERRORS`, so that section only contains things worth acting on.
Duplicates are expected and benign: the window overlaps the previous run's, so
recent scores are deliberately re-offered to pick up late entries.

A skipped record could not be mapped. Fix it at source in ExpertArcher, or add a
name override to [`mappings.yaml`](../mappings.yaml) and redeploy — no
regeneration step, `sam build && sam deploy` picks it up.

**Failures.** There is **no failure-notification path**. `PostScore` is the only
state with a `Catch`, and it exists so that one unsubmittable record is reported
rather than killing the run — a rejected score no longer throws at all, since the
function returns the outcome for any status. Anything else that fails —
`Prepare`, a reference fetch, `FetchScores`, the Map itself, or `SendReport` —
fails the execution with no email. **A day with no report email is the signal
that something went wrong**, and the reason is in the execution history. Adding a
`Catch` on those states, and a `Retry` on `SendReport`, is the obvious next
improvement.

**Logs.** Execution history is kept for 90 days and is the record of the run
itself — the state machine has no CloudWatch Logs or X-Ray configuration.
`SubmitScoreFunction` gets the log group Lambda creates for it, named after the
generated function name (`/aws/lambda/hb-score-sync-SubmitScoreFunction-…`); find
it via the function's Monitor tab. The template sets no retention, so those logs
are kept indefinitely. The function logs nothing of its own, so anything there is
a Python traceback — scores and member names are never written to it.

## Throttling and limits

Golden Records allows **1 request/second, 20/minute, 200/hour**.

- The `Wait` between submissions (3s) satisfies the per-second and per-minute
  limits. A `Wait` in a Standard workflow costs nothing, unlike sleeping in a
  Lambda, so there is no reason to cut it fine.
- The per-hour limit is **not** enforced by the definition. At 3s per record a
  run reaches 200 submissions in about 10 minutes, so a backlog that large will
  start collecting 429s; those are retried (5 attempts, exponential backoff to
  60s), and anything still throttled after that is reported as rejected. The
  daily window keeps normal runs far below the limit — a first run over a long
  window, or a `null` window, is where this bites. Submit such a backlog with
  `app.py`, or run it in day-sized slices via `from`/`to`.
- Only throttling and 5xx responses are retried. A 4xx means the record was
  rejected; retrying cannot succeed and would spend budget the rest of the run
  needs. `SubmitScoreFunction` enforces this split: it raises `RetryableStatus`
  on 429, 5xx and connection failures, and returns normally on everything else,
  so the retry policy lives in the definition and the function only decides what
  is transient.

A Standard execution allows 25,000 history events, and this uses roughly 8–10 per
record, so the practical ceiling is a few thousand records per run. The 256 KB
limit on state payloads applies to the scores list held by the Map, which is the
other reason not to run an unbounded window.

### Reference paging

The `Fetch*` states request `pageSize` (default 500) records per page and keep
going while `paging-headers` says there is a next page, merging each page into
the same `{name: id}` map. The largest table (rounds, currently 373) fits in one
page today, so the loop is insurance rather than routine.

The loop has no iteration guard: it terminates only when the API stops saying
`nextPage: "Yes"`. If that header were ever wrong, the execution would run until
it hit the history-event limit.

## Known differences from `app.py`

- **Fractional numbers are skipped, not truncated.** `int(600.5)` in Python
  gives 600; the state machine reports the score as an invalid number instead. A
  fractional score is suspicious enough to be worth seeing.
- **`Xs` that is a non-numeric string becomes 0** rather than skipping the
  record, which is what `int("abc")` would cause in `app.py`.
- **Skip detail text differs slightly.** A missing score reads
  `score= hits=1 golds=1` rather than Python's exception text, and a blank detail
  prints as blank rather than `(missing / empty)`. The categories and the
  grouping are the same.
- **A record rejected with several messages** is grouped under all of them joined
  with a semicolon, where `app.py` would count it once per message.
- **A 2xx response carrying a non-empty `errors` list counts as a rejection**, so
  a "success" that is not one cannot be reported as submitted. `app.py` keys only
  off the HTTP status.
- **No CSV output and no `submission-errors.log`.** Use `app.py` for those — the
  report names every rejection reason, but does not dump the full response body
  or the submitted record alongside it.
