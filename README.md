DayBrief

DayBrief is an AI-powered news briefing experience that turns recent news into short, source-grounded audio and animated video briefings.

It combines:

* verified multi-source stories
* concise AI-generated narration
* audio playback
* animated cartoon anchors
* real news imagery when available

The goal is simple: make daily news faster to consume without sacrificing traceability.

Why DayBrief

Many AI news summaries optimize for speed, but often lose source discipline.

DayBrief uses a verification-first pipeline:

Retrieve
→ Cluster
→ Extract claims
→ Verify evidence
→ Synthesize events
→ Select stories
→ Generate grounded narration
→ Audio + visuals

Only supported information is allowed into the final narration.

AI Pipeline

NVIDIA Nemotron, served through Nebius Token Factory, is used throughout the editorial workflow:

* event clustering
* claim extraction
* evidence verification
* verified-event synthesis
* story ranking
* grounded narrative generation
* final narrative verification

The final writer only receives preselected verified claims.

Verified claims
→ Nemotron narrative generation
→ grounding verification
→ accepted narration

If the available evidence only supports a short statement, DayBrief keeps it short instead of inventing additional detail.

Demo Experience

A user:

1. Selects a news topic
2. Chooses a cartoon anchor
3. Generates a DayBrief
4. Receives a small set of grounded stories
5. Listens to the generated audio
6. Watches an animated news briefing

Available anchor styles can include:

* Cat
* Dog
* Panda
* Fox

The same character remains visually consistent throughout the briefing.

Visual Experience

DayBrief prefers real news imagery when available.

Article image available
→ news image + cartoon anchor overlay
No suitable article image
→ cartoon anchor + animated newsroom scene

The intro and outro are playful and expressive, while the actual news narration remains factual.

Sensitive stories automatically use more restrained tone and animation.

Grounding and Reliability

DayBrief includes several safeguards:

* unsupported claims are excluded
* partially supported claims are not promoted to facts
* core claims must be grounded
* source URLs are preserved
* narrative generation only sees selected evidence
* generated prose is verified again after writing
* serious stories use restrained tone and animation

Smart Caching

Usable briefs can be cached to reduce repeated retrieval and model calls.

Default:

DAYBRIEF_CACHE_ENABLED=true
DAYBRIEF_CACHE_TTL_SECONDS=1800

The default cache lifetime is 30 minutes.

Tech Stack

AI

* NVIDIA Nemotron
* Nebius Token Factory
* Tavily

Backend

* Python 3.12
* FastAPI
* Pydantic
* SQLite

Frontend

* Next.js
* TypeScript

Media

* Audio generation
* FFmpeg
* Article image extraction
* Animated cartoon anchors

Deployment

* Docker
* Docker Compose
* Nebius VM

Live Demo

DayBrief is deployed on a Nebius VM and can be accessed at:

http://66.201.5.182:8080/

The application can be deployed directly to a Nebius VM using the included deployment script.

Run Locally

docker compose up -d --build

Open:

http://127.0.0.1:8080/

Health check:

curl http://127.0.0.1:8080/health

Core API

GET  /health
GET  /api/topics
POST /api/briefs/generate
GET  /api/briefs/{brief_id}

What Makes DayBrief Different

DayBrief is not simply:

news → LLM → summary

It is:

news
→ evidence
→ verification
→ grounded synthesis
→ narration
→ audio
→ animated briefing

The goal is to combine trustworthy AI generation with a lightweight and engaging media experience.

Credible enough to trust, short enough to finish, and fun enough to watch.