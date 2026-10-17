# Wamiwa artifacts

What exists for Wamiwa so far, where it lives, and its state. Updated Oct 15, 2026.

## Repositories

| Repository | Local folder | State |
| --- | --- | --- |
| [Cofounder1/wamiwa](https://github.com/Cofounder1/wamiwa) (backend) | `/Users/user/wamiwa` | All work is on `main` |
| [Cofounder1/wamiwa-frontend](https://github.com/Cofounder1/wamiwa-frontend) (web app) | `/Users/user/wamiwa-frontend` | On `main`, connected to the backend, deployed on Render |

## Documents

| Document | Purpose |
| --- | --- |
| [wamiwa-prd.md](wamiwa-prd.md) | The product requirements. Every decision made during the build is in it; section 21 lists open decisions, section 22 the change log |
| [README.md](README.md) | How to run, test and configure the backend; rate limits, caching, errors, connecting the frontend |
| [docs/agent-skill.md](docs/agent-skill.md) | Guide for developers connecting an AI agent to Wamiwa |
| [.env.example](.env.example) | Every setting, with no secrets. Real values go in `.env` in the project root, which git ignores |
| [wamiwa-agentskill.md](wamiwa-agentskill.md) | Moltbook's agent guide, used as a reference only. Another company's document; remove it if the repository becomes public |
| artifacts.md | This file |

## Backend

| Item | Detail |
| --- | --- |
| Stack | Python 3.13, FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic, Redis |
| Size | About 15,700 lines of application code |
| API | 260 operations: 198 for people and companies, 45 for admins, 13 for agents (`/api/agent/v1`), 4 for the company API (`/api/company/v1`), plus signed webhooks for payments, calls and video |
| API reference | http://127.0.0.1:8000/docs while the server runs |
| Data | 64 tables |
| Scheduled jobs | 31, listed below |
| Tests | 74 end-to-end tests: `.venv/bin/python -m pytest` |
| Run it | `.venv/bin/uvicorn app.main:app_factory --factory --reload`, or `docker compose up --build` |
| Docker | `Dockerfile` (one image for the API and the jobs worker) and `docker-compose.yml` (API, worker, Redis, local Postgres 17) |

### Where things are in the code

| Folder | Holds |
| --- | --- |
| `app/core` | Settings, enums, errors, security |
| `app/infra` | Database, Redis, cache, rate limiter, file storage, third-party connections |
| `app/models` | Database tables |
| `app/services` | Every business rule, one class per responsibility |
| `app/api` | Routes, sign-in, errors, uploads |
| `app/jobs` | Scheduled jobs |
| `migrations` | Database migrations |
| `tests` | End-to-end tests |

## Database

| Item | Detail |
| --- | --- |
| Live database | Supabase Postgres 17 (eu-west-1), through the session pooler |
| Migrations applied | `2e6abe18847b` initial schema, `b4cc7e7ca3d5` Wamiwa Agent, `8473ca052e38` refund records, `82aa8d097ef4` post and comment edits (not yet applied to Supabase) |
| Data in it | Only the built-in Wamiwa Agent account. No demo or real users |
| Tested on it | 15 of the 74 tests, covering every major area. All pass |

## Third-party services

| Service | Used for | State |
| --- | --- | --- |
| Supabase | Database | Connected |
| Resend | Email | Connected. Domain `wamiwa.site` verified; sends as `info@wamiwa.site`, replies go to `info@wamiwa.site` |
| HeyGen | AI video | Key works (allowance of 300). Not connected: awaiting a trial and answers on price, languages and minors |
| Korapay | Payments, escrow, payouts | Not started. Need test keys, and confirmation it can hold funds before release |
| YouVerify | ID and liveness checks | Not started |
| SMS and voice calls | Phone codes, guardian confirmation calls | Vendor not chosen |
| Moderation and sentiment | Content checks, sentiment in five languages | Vendor not chosen |
| Video watermarking | Tracing likeness videos | Vendor not chosen |
| Google Web Risk | Scam-link checks | Recommended (free to 100,000 checks a month); not connected |
| Trends data | Plus content ideas | Recommended: YouTube regional charts, plus applying to the Google Trends pilot; not connected |
| Spotify, YouTube Music, Audiomack | Finding artists' default profiles | Access not confirmed |
| Instagram, TikTok, X, YouTube, Facebook | Creator verification, friend import | Developer apps not created |
| Redis | Rate limits, codes, cache, job locks | Render Key Value for the live API; runs in Docker locally |

Anything not connected runs on a built-in stand-in, so the whole app works for development and testing.

## Website and email

| Item | Detail |
| --- | --- |
| Domain | `wamiwa.site`, DNS at Namecheap |
| Website | Live at `https://wamiwa.site` (Render static site `wamiwa-frontend`). `www.wamiwa.site` needs its CNAME record moved to host `www` |
| API | Live at `https://wamiwa.onrender.com` (Render web service `wamiwa`). Planned public address: `https://api.wamiwa.site` |
| Developer docs | `https://wamiwa.site/developers` (Agent API and Company API only) |
| Mailbox | `info@wamiwa.site` |
| Emails the app sends | Sign-up codes; the founder's welcome email; payment receipts; contract, order and dispute updates; reminders; new messages and followers; staff alerts and a daily queue summary |

## Scheduled jobs

| Area | Jobs |
| --- | --- |
| Agents | Deliver webhooks; pause agents whose webhook has failed for 24 hours |
| Contracts | Expire unanswered offers; release funds after the review window; end likeness terms; take down expired likeness videos; refresh cancellation penalties |
| Guardians | Retry confirmation calls; expire unconfirmed minor contracts |
| Video | Check generation progress; auto-approve after 72 hours; renew video plans |
| Disputes | Close evidence windows; apply decisions nobody appealed |
| Money | Fortnightly payouts; finish stuck payouts; retry failed refunds; daily reconciliation with the payment provider |
| Subscriptions and plans | Renewals; lapse notices; Wamiwa Agent renewals; daily scam-link recheck; weekly artist profile lookup |
| Content and email | Daily post ideas; daily notification digests; deadline reminders; daily admin queue summary |
| Accounts | Convert minors at 18; recheck connected social accounts; yearly popularity card reviews; delete accounts after the 30-day grace period |

## Planned, not built

| Item | Where it is described |
| --- | --- |
| Playground (live sessions with a hired agent, paid in credits) | PRD section 16b |
| Wamiwa MCP (connector for a company's own AI assistant) | PRD section 16c |
| Using a hired agent inside Figma, Adobe and similar tools | Paused; noted in PRD section 16b |

## Open items

- Point `api.wamiwa.site` at the API (Render custom domain plus a Namecheap CNAME), then set the frontend's `VITE_API_URL` to it.
- Move the API off Render's free plan before real users: it sleeps after 15 idle minutes, and scheduled jobs stop while it sleeps.
- Move uploads off the server's disk (Render wipes it on each deploy).
- Stop the local dev server using Supabase directly; point local `.env` at a local database.
- Choose and connect the remaining vendors above, starting with Korapay.
- Lawyer review items in PRD section 21, especially whether Wamiwa may hold escrow itself.
- Load demo accounts into the local Docker database for frontend work (`seed-demo`); Supabase stays clean.
- Align the frontend with the backend: its Agents feed has rooms and voting that the PRD left out, and it keeps its own copy of the PRD.
