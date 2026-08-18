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

The cost is that mapping logic is expressed in JSONata rather than Python, and
JSONata errors are not caught at deploy time. That is what `tools/` is for — see
[Before deploying](#before-deploying).

## What it does

```
Prepare                 derive the date window from the execution start time
  ↓
FetchRounds             ┐
FetchBowTypes           │ 4 GETs to Golden Records, each projected straight
FetchAgeGroups          │ down to a {name: id} map so the 82 KB rounds payload
FetchMembers            ┘ never travels further
  ↓
CheckReferenceComplete  fail loudly if a page came back full (see below)
  ↓
FetchScores             1 GET to ExpertArcher for the window
  ↓
Transform               map each score to a POST body, or record why it was skipped
  ↓
Partition               split into submit now / defer / skipped
  ↓
SubmitScores            Map, one at a time, 3s apart, rejections caught and counted
  ↓
BuildReport → PublishReport → Done
```

Anything that fails routes to `ReportFailure`, which emails what happened and
then fails the execution deliberately, so a broken run shows as failed rather
than quietly succeeding.

### Files

```
template.yaml               SAM template: connections, SNS topic, state machine, schedule
statemachine/sync.asl.yaml  The state machine definition (YAML ASL, JSONata)
samconfig.toml              Deploy settings — region, stack name, non-secret parameters
```

## Before deploying

Two checks, both local and read-only. Run them after any change to
`sync.asl.yaml` or `mappings.toml`.

```bash
# 1. mappings.toml is the single source of truth for name overrides; the state
#    machine's copy is generated from it. Fails if they have drifted.
python tools/gen_asl_names.py --check

# 2. Compiles all 51 JSONata expressions, checks the state graph, and runs the
#    real expressions against the real reference data in golden-records/,
#    asserting the POST body matches api_record() in app.py field for field.
cd tools && npm install && npm run check
```

The second one matters more than it looks. Nothing validates JSONata at deploy
time, so without it a typo in a mapping expression is first discovered by a
scheduled run failing halfway through submitting real scores.

```bash
cd aws && sam validate --lint      # template only
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

`sam deploy` cannot reuse a previous parameter value, so those three must be
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

`ScheduleState` defaults to `DISABLED` in `samconfig.toml` so the first deploy
does not start submitting overnight before the smoke test below has been run.

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

## First-run smoke test

Do these in order. The dry run makes no writes to Golden Records.

```bash
# 1. Dry run: fetch, map, report. Submits nothing.
aws stepfunctions start-execution --region eu-west-2 \
  --state-machine-arn <StateMachineArn from the stack outputs> \
  --input '{"dryRun":true}'
```

Check the report email against a local `python app.py --dry-run --from … --to …`
over the same window. The counts and the skip reasons should agree.

```bash
# 2. A real run over a single day, to confirm submission end to end.
aws stepfunctions start-execution --region eu-west-2 \
  --state-machine-arn <arn> --input '{"lookbackDays":1}'
```

Then set `ScheduleState=ENABLED` and redeploy.

### Confirm on that first run

These are the parts that cannot be verified without calling the real APIs. If
the dry run fails, check them in this order — the failing state and its input
and output are in the execution history in the Step Functions console.

| # | Assumption | If it is wrong |
|---|---|---|
| 1 | `ResponseBody` arrives as **parsed JSON**, not a string | Every reference map is empty and every score is skipped as an unmatched round. Add a `$eval()` of the body in the `Fetch*` states. |
| 2 | A connection may set the **`Authorization`** header via `ApiKeyName` | `FetchRounds` returns 401. Fall back to a header in `InvocationHttpParameters`. |
| 3 | The HTTP Task applies the connection's **`InvocationHttpParameters` query strings** | `FetchScores` returns 401/403 — the `apikey` parameter is not reaching ExpertArcher. |
| 4 | The `States.Http.StatusCode.*` **retry error names** are right | Retries do not fire; a transient 503 fails the run instead of being retried. |
| 5 | CloudFormation accepts a **YAML** definition from S3 | The deploy fails at `AWS::StepFunctions::StateMachine`. Convert the definition to JSON at deploy time. |
| 6 | The shape of **`$states.errorOutput.Cause`** on a 4xx | Duplicate detection still works (it substring-matches the whole cause), but per-message error grouping degrades to a truncated string. Adjust the slicing in `ClassifyRejection`. |

Item 6 is a known soft spot: on a 4xx the HTTP Task fails and the Golden Records
response body arrives as *text* inside `Cause`. JSONata cannot parse a JSON
string, so individual error messages are recovered by string slicing. Duplicate
detection is robust regardless.

## Operating it

**Reading the report.** Same shape as `app.py`'s: counts, then skipped records
grouped by reason and collapsed to unique problems with a record count, then
submission errors grouped by the API's own message. Sections are ordered by how
many distinct problems they contain, so the biggest thing to fix comes first.

- *Already present* — duplicates. Expected, and benign: the window is 7 days by
  default, so recent scores are deliberately re-offered to pick up late entries.
- *Deferred* — mapped but over the hourly request cap. The next run takes them.
- *Skipped* — could not be mapped. Fix at source in ExpertArcher, or add a name
  override to [`mappings.toml`](../mappings.toml) and regenerate.

**Adding a name mapping.** Edit `mappings.toml`, then:

```bash
python tools/gen_asl_names.py     # regenerate the block in sync.asl.yaml
cd tools && npm run check         # confirm the new target actually exists
cd ../aws && sam deploy ...
```

Never edit the generated block between the `BEGIN GENERATED` / `END GENERATED`
markers by hand — `--check` will fail, and `mappings.toml` must stay the single
source of truth so the CSV and API paths cannot diverge.

**Failure alerts.** Two independent paths, deliberately:

1. The state machine's own `ReportFailure` state emails what went wrong.
2. An EventBridge rule watches for `FAILED`/`TIMED_OUT`/`ABORTED` executions and
   emails from outside. This catches failures that stop the machine reaching its
   own report state.

A run producing no email at all is itself the signal that something is wrong.

**Logs.** Execution history is kept for 90 days; `ALL`-level logs with execution
data go to `/aws/vendedlogs/states/<stack-name>`.

## Throttling and limits

Golden Records allows **1 request/second, 20/minute, 200/hour**.

- The `Wait` between submissions (3s) satisfies the per-second and per-minute
  limits. A `Wait` in a Standard workflow costs nothing, unlike sleeping in a
  Lambda, so there is no reason to cut it fine.
- The per-hour limit is handled by `SubmitCap` (default 180, leaving headroom for
  the four reference fetches and any retries). Anything over it is reported as
  deferred rather than dropped or throttled into failure.
- Only throttling and 5xx responses are retried. A 4xx means the record was
  rejected; retrying cannot succeed and would spend budget the rest of the run
  needs.

At 3s per record, a 180-record run takes about 9 minutes — well inside Step
Functions' limits (a Standard execution allows 25,000 history events; this uses
roughly 5 per record).

### Reference paging

The `Fetch*` states request **one page** of `ReferencePageSize` (default 500)
records, which comfortably covers the largest table (rounds, currently 373).
If any page comes back full, `CheckReferenceComplete` **stops the run** and
emails why, rather than continuing with incomplete reference data.

That is deliberate. A missed second page would silently turn valid scores into
"unmatched round" skips — a plausible-looking report that is quietly wrong,
which is worse than an obviously failed run. If a table ever exceeds the page
size, raise `ReferencePageSize` or add a paging loop.

## Known differences from `app.py`

Both are checked by `tools/check_statemachine.js`, so these are the deliberate
ones:

- **Fractional numbers are skipped, not truncated.** `int(600.5)` in Python
  gives 600; the state machine reports the score as an invalid number instead. A
  fractional score is suspicious enough to be worth seeing.
- **`Xs` that is a non-numeric string becomes 0** rather than skipping the
  record, which is what `int("abc")` would cause in `app.py`.
- **Skip detail text differs slightly.** A missing score reads
  `score= hits=1 golds=1` rather than Python's exception text. The category and
  the grouping are the same.
- **No CSV output and no `submission-errors.log`.** Use `app.py` for those.
