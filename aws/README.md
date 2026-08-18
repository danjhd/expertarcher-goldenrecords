# Daily sync on AWS

The same integration as [`app.py`](../app.py), running unattended once a day as
an **AWS Step Functions state machine** with **no Lambda**. The state machine
calls both APIs over HTTPS itself, maps the scores in JSONata, submits them
inside the Golden Records throttle, and emails a report of every run.

`app.py` remains the reference implementation and the only way to produce a
[CSV bulk-import file](../README.md#bulk-import-via-csv). This deployment covers
the API-submission path only.

## Why no Lambda

Every step is a native state, so there is no deployment package, no runtime to
keep patched, no cold start, and the whole transformation is readable in the
definition rather than hidden inside code. The two things that would normally
force a Lambda — calling a third-party API, and reshaping JSON — are handled by
the [HTTP Task](https://docs.aws.amazon.com/step-functions/latest/dg/connect-third-party-apis.html)
and by [JSONata](https://docs.aws.amazon.com/step-functions/latest/dg/transforming-data.html)
respectively.

The cost is that the mapping logic is expressed in JSONata rather than Python,
and that **JSONata is not validated at deploy time** — a bad expression is first
discovered by a run failing. There is no local checker; use `--input
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
                          → wait 3s → POST → record submitted / duplicate
                                             / rejected, or skipped with a reason
  ↓
BuildReport → SendReport (SNS email)
```

Each `Fetch*` state is followed by a `*Complete` Choice and a `*Paginate` state:
the Golden Records `paging-headers` response header is parsed, and while
`nextPage` is `"Yes"` the next page is fetched and merged into the same map.

### Files

```
template.yaml               SAM template: connections, SNS topic, SSM parameter,
                            state machine, schedule
statemachine/sync.asl.yaml  The state machine definition (YAML ASL, JSONata)
samconfig.toml              Deploy settings — region, stack name, non-secret parameters
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
sam deploy --parameter-overrides \
    GoldenRecordsApiKey=<club API key> \
    ExpertArcherApiKey=<key> \
    ReportEmail=you@example.com
```

`sam deploy` cannot reuse a previous parameter value, so the two API keys must be
supplied on every deploy. To avoid retyping them, copy `samconfig.toml` to
`samconfig.local.toml` (gitignored), add them to `parameter_overrides`, and use:

```bash
sam deploy --config-file samconfig.local.toml
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

### Confirm on that first run

These cannot be verified without calling the real APIs. If the dry run fails,
check them in this order — the failing state and its input and output are in the
execution history in the Step Functions console.

| # | Assumption | If it is wrong |
|---|---|---|
| 1 | A connection may set the **`Authorization`** header via `ApiKeyName` | `FetchRounds` returns 401. Fall back to a header in `InvocationHttpParameters`. |
| 2 | The HTTP Task applies the connection's **`InvocationHttpParameters` query strings** | `FetchScores` returns 401/403 — the `apikey` parameter is not reaching ExpertArcher. |
| 3 | The `States.Http.StatusCode.*` **retry error names** are right | Retries do not fire; a transient 503 fails the run instead of being retried. |
| 4 | `paging-headers` arrives as a **JSON string** with `nextPage` / `currentPage` | Either only page 1 is read (silent "unmatched round" skips), or the paging loop never terminates. |
| 5 | The shape of **`$states.errorOutput.Cause`** on a 4xx | Duplicate detection still works (it substring-matches the whole cause), but the per-message grouping in `SUBMISSION ERRORS` degrades to a 200-character slice of the raw cause. |

Item 5 is a known soft spot: on a 4xx the HTTP Task fails and the Golden Records
response body arrives as *text* inside `Cause`. JSONata cannot parse a JSON
string in that position, so the `errors` list is recovered by string slicing.
Duplicate detection is robust regardless.

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
- `SUBMISSION ERRORS` — one block per message the API returned, with the archer
  and date of each record that hit it.
- `RECORDS THAT WOULD BE SUBMITTED` — dry runs only.

Duplicates are counted but not listed. They are expected and benign: the window
overlaps the previous run's, so recent scores are deliberately re-offered to pick
up late entries.

A skipped record could not be mapped. Fix it at source in ExpertArcher, or add a
name override to [`mappings.yaml`](../mappings.yaml) and redeploy — no
regeneration step, `sam deploy` picks it up.

**Failures.** There is **no failure-notification path**. Only `PostScore` has a
`Catch`, which is what turns a 4xx into a reported rejection rather than a failed
run. Anything else that fails — `Prepare`, a reference fetch, `FetchScores`, the
Map itself, or `SendReport` — fails the execution with no email. **A day with no
report email is the signal that something went wrong**, and the reason is in the
execution history. Adding a `Catch` on those states, and a `Retry` on
`SendReport`, is the obvious next improvement.

**Logs.** Execution history is kept for 90 days and is the only record — the
state machine has no CloudWatch Logs or X-Ray configuration.

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
  needs.

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
- **No CSV output and no `submission-errors.log`.** Use `app.py` for those.
