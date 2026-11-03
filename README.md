# ClipPulse
![CI](https://github.com/a1issa369/clippulse/actions/workflows/ci.yml/badge.svg)

Find Twitch clips that are **rising right now**, before they go stale, and save the best ones to a shotlist for your next edit.

Video editors and clip channels lose the window on a good moment: by the time a clip has big numbers, everyone has already cut it. ClipPulse watches how fast clips gain views, hour by hour, and ranks them by momentum instead of by total views.

![ClipPulse rising feed](docs/rising.png)

## What it does

- **Rising feed.** Clips from the streamers you track, ranked by views gained per hour, with a Surging / Steady / Cooling badge.
- **Watch and chart.** Click a thumbnail to play the clip in the page (Twitch's official embed, loaded only after you click), with a growth chart of its views over time. Cards show a small version of the same chart.
- **Search any streamer.** Type any Twitch username. Their clips load right away and they stay tracked while people keep looking, then drop off after 2 days of no visits. The site owner's own list is pinned and never drops.
- **Per-streamer view.** Sort by momentum, total views or newest, over the last 24 hours, 3 days or 7 days.
- **Shotlist.** Save clips, jot a note on how you would cut each one (the VOD timestamp is shown so you can find the moment in the full stream), and export everything as a CSV.
- **No accounts.** A shotlist is identified by a random, unguessable id kept in your browser. Share the link to share the list.

## How "rising" is calculated

Two numbers, both in views per hour (`backend/src/clippulse/scoring.py`):

| Number | Definition | Why |
| --- | --- | --- |
| Average velocity | `views / (age + 2h)` | The 2 hour prior stops a three-minute-old clip with 4 views from outranking a day-old clip with 900. |
| Recent velocity | Views gained between our own hourly snapshots over the last 6 hours | This is the "taking off right now" signal. Needs at least two snapshots, and is ignored if the data is more than 3 hours stale. |

Clips are ranked by recent velocity when it exists, otherwise by average velocity. Ties break on total views, then clip id, so results are deterministic.

Twitch returns clips most-viewed first, and the poller reads one page. To make sure brand-new clips are never crowded out by older hits on a busy channel, each poll makes a second short-window query (last 6 hours) and merges the two.

## Architecture

```
                       Browser (React)
                             |
                        API Gateway (HTTP API, throttled)
                             |
                   Lambda: api  (/api/* = JSON API, everything else = the built React site)
                             |
   EventBridge (hourly) --> Lambda: poller --> Twitch Helix API
                             |                      |
                             +----> DynamoDB (single table, TTL)
                             SSM Parameter Store: Twitch credentials
```

Optional: give the stack a Discord webhook to get surge alerts through SQS and Step Functions (see below). Optional: set `EnableCloudFront=true` to put CloudFront + a private S3 bucket in front for the site (needs a CloudFront-verified AWS account).

- **Backend:** Python 3.13 on Lambda (arm64) with **zero third-party dependencies**: `urllib` for Twitch, `boto3` from the runtime. Nothing to package, nothing to patch.
- **Data:** one DynamoDB table in on-demand mode. Clips expire through TTL.
- **Frontend:** React 19, plain CSS, bundled with esbuild into a static site (about 65 KB gzipped).
- **Same origin:** the API Lambda serves both `/api/*` and the site (gzip, long-cache hashed assets, strict CSP and security headers), so the browser never makes a cross-origin call and there is no CORS configuration. CloudFront is optional.
- **Infrastructure as code:** one AWS SAM template (`template.yaml`) creates everything, including a security-headers policy with a strict Content Security Policy and an optional AWS Budgets alert.

### DynamoDB single-table layout

| pk | sk | holds |
| --- | --- | --- |
| `STREAMERS` | `S#<login>` | a tracked streamer |
| `B#<broadcaster_id>` | `C#<created_at>#<clip_id>` | a clip, its latest views and a capped `history` list of `[timestamp, views]` |
| `BOARD#<board_id>` | `I#<clip_id>` | one saved shotlist item |

A streamer's recent clips come back from one `Query` on the sort-key range, so the feed needs no scans and no secondary indexes.

## Run it on your laptop (no AWS, no Twitch keys)

```bash
cd frontend && npm install && npm run build && cd ..
python3 backend/dev_server.py          # open http://localhost:8787
```

This runs the real API code against an in-memory store and a **demo** Twitch with made-up channels. It pretends the poller has run for six hours so trends have something to show, and it ticks forward every 20 seconds so you can watch numbers move. Searching `demo_guest` shows how a visitor-added streamer appears.

For frontend work, run `npm run watch` in `frontend/` next to the dev server.

## Tests

```bash
cd backend && python3 -m pytest -q     # 363 tests, standard library + pytest only
cd frontend && npm test                # 21 tests with Node's built-in runner
python3 scripts/check_template.py template.yaml
node scripts/e2e_check.js              # drives the real UI in Chromium against the demo server
```

What the backend suite covers: the ranking math (windows, staleness, tie-breaks), the Twitch client (token reuse and refresh, one retry on 401, rate limits, pagination, with an injected HTTP function), every store behavior as a **contract test that runs against both the in-memory store and the DynamoDB store** (history capping, key layout, floats and Decimals, paginated queries), the service rules (limits, validation, idempotency, CSV formula-injection protection), and every API route including error paths.

The DynamoDB store is exercised against a small in-process fake of boto3's `Table` API that rejects floats and paginates like the real service. To run the same suite against a real DynamoDB engine:

```bash
docker run -p 8000:8000 amazon/dynamodb-local
pip install boto3
CLIPPULSE_TEST_ENDPOINT=http://localhost:8000 python3 -m pytest -q
```

CI (`.github/workflows/ci.yml`) does exactly that with DynamoDB Local as a service container, then builds the frontend, lints the template and runs the browser check.

## Deploy to AWS

You need an AWS account, the [AWS CLI](https://aws.amazon.com/cli/) and the [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html). Use an IAM Identity Center user or an IAM user with limited permissions, never the root account's keys.

**1. Register a Twitch app** at <https://dev.twitch.tv/console/apps> (client type: Confidential, any redirect URL such as `http://localhost`). Copy the Client ID and generate a Client Secret.

**2. Store the credentials in SSM Parameter Store.** The secret is encrypted with the AWS-managed key.

```bash
aws ssm put-parameter --name /clippulse/twitch_client_id     --type String       --value "YOUR_CLIENT_ID"
aws ssm put-parameter --name /clippulse/twitch_client_secret --type SecureString --value "YOUR_CLIENT_SECRET"
```

**3. Build and deploy.** One script builds the React app, bundles it into the Lambda, and runs SAM. Pass your email so AWS Budgets warns you at 80 percent of a monthly budget (default 5 USD).

```bash
scripts/deploy.sh --guided     # first time: stack name clippulse, set AlertEmail when prompted
scripts/deploy.sh              # later deploys reuse the saved answers
```

The last lines print `SiteUrl`. Open it.

> New AWS accounts can be blocked from creating CloudFront distributions until AWS Support verifies the account ("Your account must be verified before you can add new CloudFront resources"). That is why CloudFront is off by default. Once verified, redeploy with `--parameter-overrides EnableCloudFront=true`, then sync `frontend/dist` to `SiteBucketName` and invalidate the distribution.

**4. Add a streamer and take the first snapshot.** Use the box on the site, then trigger the poller by hand so you don't wait an hour. Velocity badges appear once a clip has two snapshots.

```bash
aws lambda invoke --function-name "$(aws cloudformation describe-stacks --stack-name clippulse --query "Stacks[0].Outputs[?OutputKey=='PollerFunctionName'].OutputValue" --output text)" /dev/stdout
```

**Tear it all down:**

```bash
sam delete --stack-name clippulse       # if CloudFront was enabled, empty the site bucket first
```

### Cost

Everything is pay-per-use. At the default limits (10 streamers, top 100 clips each, one poll per hour) the poller does on the order of a thousand DynamoDB writes an hour, which on-demand pricing turns into pennies to low single dollars a month, and Lambda, API Gateway and CloudFront at portfolio traffic add little. Check current pricing for your region, and keep the `AlertEmail` budget set.

## Open search, expiry and rate limits

Anyone can search any Twitch streamer, and the design keeps one visitor (or a script) from running up the bill.

| Piece | Rule |
| --- | --- |
| Pinned streamers | The `PinnedStreamers` template parameter (default `pokimane,rdcgaming,kaicenat,xqc`). Always tracked, never expire. Applied by the hourly run, or run the poller by hand. |
| Visitor streamers | Tracked for 48 hours after anyone last looked at them. Looking extends the stay, rewritten at most once an hour. |
| Shared pool | At most `MaxStreamers` (default 10) visitor streamers at once. When full, only one nobody has looked at for 6 hours can be replaced; otherwise the search is refused with a clear message. A search for a channel that does not exist never evicts anyone. |
| New lookups | Only a streamer ClipPulse is not already tracking costs anything. Each visitor (a hash of their IP, never stored raw) gets 5 per 10 minutes and everyone together gets 30 per hour, counted with an atomic conditional write in DynamoDB. Misspelled and made-up names count too. Rate-limited replies are HTTP 429 with a `Retry-After` header. |
| Per-visitor cap | One visitor can hold at most 3 streamers of their own at a time (`MAX_PER_CLIENT`). A fourth is refused until they remove one or one expires. Streamers someone else already added cost nothing and do not count. |
| Removing | A visitor can stop tracking a streamer they added with the × on its chip (`DELETE /api/streamers/{login}`). Pinned streamers and other visitors' streamers cannot be removed, which stops one person from clearing everyone's list. Removals are limited to 10 per 10 minutes. |
| Search limit | Every search, even for a streamer already tracked, counts toward 30 per 10 minutes per visitor, so the API cannot be hammered from one address. |
| Visitor identity | A visitor is a salted SHA-256 hash of their IP (the salt is the stack id), so raw addresses are never stored and the hash cannot be reversed from a list of IPs. People behind one shared network (a campus, say) share one allowance and one set of streamers. There are no accounts. |
| Front door | API Gateway throttles to 5 requests per second sustained (burst 20). |
| No clips, no slot | Most Twitch accounts have no recent clips. Searching one says so plainly and does not take a slot or push anyone out; the hourly run also drops visitor streamers whose clips have all aged out. |
| Expiry | Checked on every read through an `expires_at` field. DynamoDB's TTL only tidies up afterwards, because it can lag by up to 48 hours. |

Hourly work is bounded by the pool: the pinned streamers plus at most `MaxStreamers` temporary ones. A new streamer's first appearance shows average views per hour; recent velocity, trend badges and the growth chart arrive after a couple of hourly snapshots.

**What this does and does not guarantee.** It bounds the damage; it cannot make it zero, because AWS bills every request. A sustained flood is capped by the throttle: 5 requests per second for a month is roughly 13 million requests, on the order of $13 at HTTP API list pricing (check current rates) plus pennies of Lambda. The budget alert emails you, and `sam delete --stack-name clippulse` removes everything.

## Surge alerts to Discord (optional)

When a clip on one of the owner's pinned streamers starts taking off, ClipPulse posts it to a Discord channel. It is off until you give the stack a webhook, and nothing for it is created while it is off.

```
hourly poller --> SQS queue --> starter Lambda --> Step Functions --> notifier Lambda --> Discord
                     |                                   |
                dead letter queue               retries with backoff,
                                                visible failed runs
```

- **What counts as a surge.** The clip's trend is "up" (recent views per hour at least 1.5 times its lifetime average) and it is gaining at least 30 views per hour, on a pinned streamer, less than 48 hours old. Visitor-added streamers never alert, so nobody else can make the site post anything.
- **Once per clip.** An atomic DynamoDB counter keyed on the clip id means each clip is announced one time, and the Step Functions execution name is derived from the clip id, so a redelivered queue message cannot post it twice.
- **Bounded.** At most 5 alerts per hourly run and 20 per day, fastest clips first.
- **Why four moving parts.** The poller only decides and enqueues, so a slow Discord can never delay the hourly snapshot. SQS holds the work and parks failures in a dead letter queue. Step Functions owns the retry policy: Discord being busy or down (429 or 5xx) is retried with exponential backoff, a rejected webhook is not retried, and giving up leaves a failed execution you can read in the console. The notifier is the only code that touches Discord.
- **Safe messages.** Clip titles are written by strangers, so every message sets `allowed_mentions` to none (a title cannot ping anyone) and only Twitch links are made clickable.
- **The webhook is a secret.** It is stored in SSM Parameter Store as a SecureString and read by the notifier at runtime. It never appears in the template, the repo, or CloudFormation parameters, only its parameter name does.
- **Cost.** SQS, Step Functions (standard) and the extra Lambdas stay inside the free tiers at this volume: at most 20 alerts a day is roughly 60 state transitions a day against a free allowance of 4,000 a month.

Turn it on (the webhook is typed locally and never pasted into chat or git):

```bash
# 1. In Discord: channel settings > Integrations > Webhooks > New Webhook > Copy Webhook URL
read -s -p "Paste the webhook URL (nothing will show): " HOOK; echo
aws ssm put-parameter --region us-east-2 --name /clippulse/discord_webhook --type SecureString --value "$HOOK"
unset HOOK

# 2. Redeploy and answer the DiscordWebhookParam prompt with /clippulse/discord_webhook (Enter keeps the other answers)
scripts/deploy.sh --guided
```

Try it without waiting for a real surge by running one alert through the state machine:

```bash
ARN=$(aws cloudformation describe-stacks --region us-east-2 --stack-name clippulse --query "Stacks[0].Outputs[?OutputKey=='SurgeStateMachineArn'].OutputValue" --output text)
aws stepfunctions start-execution --region us-east-2 --state-machine-arn "$ARN" \
  --input '{"clip_id":"manual-test","title":"Test alert","url":"https://clips.twitch.tv/test","streamer":"ClipPulse","velocity":120,"views":3100,"age_hours":3}'
```

The message should appear in Discord within seconds, and the execution shows up in the Step Functions console. To turn alerts off, redeploy with `DiscordWebhookParam` empty and the queue, state machine and extra Lambdas are removed.

## Design decisions and trade-offs

- **History as a list on the clip item, not one item per snapshot.** One `Query` returns a streamer's clips with their trends, and each poll costs one read and one write per clip. The cost is a read-modify-write per clip, which is safe because a single scheduled poller writes them.
- **DynamoDB over Postgres.** Access patterns are fixed and key-based, there are no joins, and TTL cleans up old clips for free. A relational store would add a VPC or a connection pooler to a function that runs for seconds.
- **Lambda over containers.** Traffic is bursty and tiny, and the hourly poller finishes in seconds.
- **`urllib` instead of `requests`.** No packaging step and no dependency to patch for a handful of JSON calls. The HTTP function is injected, which is also what makes the Twitch client unit-testable without a network.
- **Lambda serves the site by default.** One origin means no CORS and one CSP everywhere, and it deploys on any account. The cost is that each page load invokes a function (the bundle is cached in memory and hashed assets are cached by browsers for a year); CloudFront + S3 is the better choice at real traffic, so it stays available behind a flag.
- **Capability-URL shotlists instead of accounts.** The 128-bit random board id is the key. That keeps the app login-free, at the cost that the link must be treated like a password. Cognito would be the next step if shotlists ever hold anything sensitive.
- **Server looks up the clip.** Saving a clip sends only a clip id and streamer. The server copies the title and URL from its own data, so a caller cannot store arbitrary links or text in someone's shotlist.
- **Open search instead of a fixed streamer list.** A fixed list is cheap and safe but only useful to its owner. Open search makes the project useful to anyone who opens it, and the price is an attack surface that can cost money. I chose open search and spent the effort on bounding the cost (see the limits above) rather than on avoiding the feature.
- **Pinned and visitor streamers are different things.** Pinned ones never expire and cost a fixed amount. Visitor ones expire, share a small pool and are rate limited. Keeping the two apart means a flood of searches can never push out the owner's list.
- **Rate limits live in DynamoDB, not in Lambda memory.** Lambda instances come and go and run in parallel, so an in-memory counter would let a burst of requests slip through. An atomic conditional write on one item per visitor and window is exact, at the cost of one extra write per lookup.
- **Visitors are a salted IP hash, not accounts.** It keeps the app login-free and lets people own and remove their own streamers. The cost is that people behind one shared address share one identity, and an IP is not a person. Real accounts would be the fix if this ever mattered more than a portfolio demo.
- **Removal is limited to your own streamers.** Letting anyone remove any streamer would let one person wipe everyone's list, so pinned streamers and other visitors' streamers are protected.
- **Expiry is checked on read, and TTL only cleans up.** DynamoDB's TTL can lag by up to 48 hours, so correctness comes from comparing `expires_at` on every read instead of trusting the deletion.
- **A 2 hour prior in average velocity.** `views / (age + 2h)` keeps a three-minute-old clip with 4 views from outranking a day-old clip with 900, at the cost of slightly understating very fresh clips that really are taking off. The recent-velocity signal from the hourly snapshots corrects for that once it exists.
- **Alerts go through a queue and a state machine instead of one Lambda call.** Calling Discord straight from the poller is a few lines of code, but then every Discord hiccup would stall or fail the hourly snapshot, and retries would have to be hand-written. SQS plus Step Functions cost more moving parts and more template, in exchange for isolation, backoff, a dead letter queue and a visible history of every delivery. For one webhook this is more machinery than the job strictly needs, and I chose it on purpose to learn those services; a single Lambda with a retry loop would also work.
- **Deduplication uses the rate-limit counter instead of a new table or field.** One atomic conditional write per clip says "announced already", with no extra schema, at the cost of the dedupe window being a fixed number of days. If queueing fails after the counter is spent, that clip's alert is lost rather than duplicated, which is the right way to fail for a notification.
- **CSV export neutralises formulas.** Clip titles are written by strangers, so cells starting with `=`, `+`, `-` or `@` are prefixed with an apostrophe before they reach a spreadsheet.

## Difficulties along the way

What went wrong or surprised me while building this, and what I changed because of it.

- **A new AWS account could not create CloudFront distributions.** The first design served the site from S3 behind CloudFront, and the deploy failed with an account verification error that only AWS Support can clear. I moved the site into the same Lambda that serves the API (one origin, no CORS, one CSP) and kept CloudFront behind the `EnableCloudFront` flag for later. The trade-off is that each page load now invokes a function.
- **Making open search safe was harder than building it.** The first version had no real answer to "what stops someone from running up my AWS bill?". Working that through produced the layers above: an atomic per-visitor and global counter, a small shared pool, expiry, a per-visitor cap, API throttling and a budget alert. It also forced an honest conclusion: the cost can be bounded but never made zero, so the README states the worst case instead of promising none.
- **Real accounts with no clips quietly filled the pool.** Testing against live Twitch showed that most real accounts have no recent clips, and they were taking slots and evicting streamers that did. I had assumed an empty result meant a made-up name. The fix was to refuse such accounts with a clear message, never let them take a slot or evict anyone, and have the hourly run drop visitor streamers whose clips have all aged out.
- **Rate-limit code is easy to get subtly wrong.** The counters use a DynamoDB conditional update, where `ttl` and `hits` are reserved words that need placeholders, and a plain read-then-write would race. I tested it against a fake table that emulates the update, optionally against DynamoDB Local in CI, and deliberately broke each limit to confirm a test fails when the behavior is wrong.
- **Ownership without accounts.** Letting people remove streamers needed some notion of who added what. I used a salted hash of the IP, never sent the stored id to the browser (only a yes/no `mine` flag), and limited removal to your own streamers so one person cannot clear everyone else's list. The cost is the shared-address caveat in the limits below.
- **A flaky browser test.** The end-to-end check sometimes failed because a second search started while the first was still running and the button was disabled. The fix was to wait for the response and for the button to come back before the next action, so the test follows the user's real sequence.
- **Confusing duplicate text on cards.** An early card read "360 views/hr (360 views/hr gained...)" because two code paths each explained the same number. I pulled the wording into one helper and added a test.
- **Discord rejects the default Python user agent.** A plain `urllib` request to a webhook is refused by Discord's edge, so the notifier sends its own `User-Agent`. It is the kind of failure no unit test with a fake HTTP function can show, which is why the README walks through a real test execution after deploying.
- **Telling a retryable failure from a hopeless one.** Retrying a rejected webhook five times is wasted work and noise, while giving up on a 429 loses the alert. The notifier raises two differently named exceptions, `DiscordRetryable` and `DiscordRejected`, and the state machine retries only the first. Step Functions matches on the exception class name, so a test ties the template to the code and fails if either is renamed.
- **Tooling, not just code.** The first push to GitHub failed with an HTTP 400 until I switched git to HTTP/1.1 with a larger post buffer, and a later push was rejected because the remote had moved on. Small, but it is part of shipping.

## Known limits

- **Open to anyone, within limits.** The visitor pool is small and shared. When it is full, a new search is refused until a streamer has gone unvisited for 6 hours, and each visitor can hold at most 3 of their own. The pinned list is never affected. There are no accounts: the chips are shared by everyone, ownership and the limits are per salted IP hash, and people behind one shared address (a campus, for example) share one allowance.
- **Velocity needs history.** A clip shows its average velocity until it has two snapshots at least 30 minutes apart, so a freshly added streamer looks less sharp for the first hour or two.
- **Hourly resolution.** The poll interval is a template parameter (`PollSchedule`); polling faster costs proportionally more Twitch calls and writes.
- **Twitch's data, Twitch's limits.** Only clips Twitch's API returns are visible, with the view counts it reports. ClipPulse is an independent project, not affiliated with or endorsed by Twitch.

## Project layout

```
template.yaml                 SAM template: everything in AWS
backend/src/clippulse/        scoring, twitch client, store, service, api, static site, poller, surge alerts
backend/tests/                363 tests plus fakes
backend/dev_server.py         local demo with fake channels
frontend/src/                 React app (components, hooks, API client, pure helpers)
frontend/test/                logic tests
scripts/                      deploy.sh, template checker, browser end-to-end check
.github/workflows/ci.yml      CI
```
