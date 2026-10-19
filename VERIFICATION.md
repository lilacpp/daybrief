# Phase 1 verification

Verified on Python 3.12.14, Node 24.19.0, Next.js 15.5.27.

| Check | Result |
| --- | --- |
| Python dependency installation and editable package install | Passed |
| Ruff lint | Passed |
| Ruff formatting | Passed; 9 files already formatted |
| Backend pytest | 23 passed |
| Running backend `/health` over HTTP | Passed, explicit mock mode |
| Generate and retrieve persisted briefs over HTTP | Passed for all 3 topics |
| Narration estimates | General 55.7s; AI & Technology 56.5s; Science 56.1s |
| Frontend dependency installation | Passed |
| ESLint | Passed |
| TypeScript `tsc --noEmit` | Passed |
| Next.js production build | Passed |
| Running production homepage over HTTP | Passed, 200 and expected page text |
| CORS preflight and compiled frontend API URL | Passed |
| Browser smoke | Blocked: Chromium downloads were corrupt/truncated; test could not launch missing executable |
| Backend static type checking | Not configured; not run |
| Live Tavily / Nebius requests | Not run: no credentials supplied; mock paid-free adapter contract tests passed |
| Source/package secret scan | Passed: no API keys, auth headers with real credentials, or private keys found |

One non-failing warning: installed Starlette deprecates its httpx TestClient
transport. All API tests passed. Browser smoke source is included and runnable
once Chromium is installed. No claim is made that browser interactions or visual
layout were verified in this environment.

The first HTTP probe failed because the environment's default proxy intercepted
loopback requests; final probes disabled proxy use for localhost. The first
production server launch hit restricted interface enumeration; explicitly binding
Next.js to 127.0.0.1 resolved it, and this is now the default startup setting.
