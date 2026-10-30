DayBrief

DayBrief is an AI-powered news briefing application that turns current news into concise, evidence-grounded daily briefings with narration and animated visuals.

Live Demo

http://66.201.5.182:8080/

What It Does

DayBrief automatically:

* Retrieves current news using Tavily
* Filters and groups related articles into coherent news events
* Uses NVIDIA Nemotron through Nebius Token Factory for event analysis and reasoning
* Extracts key claims and verifies them against supporting sources
* Ranks the most important stories
* Generates concise, grounded news narratives
* Produces audio narration and animated visual briefings
* Caches useful generated briefings to reduce unnecessary regeneration

The goal is to create a fast, reliable way to understand the most important news without manually reading through multiple articles.

Pipeline

Tavily
   ↓
News Retrieval
   ↓
Filtering & Event Clustering
   ↓
NVIDIA Nemotron
   ↓
Claim Extraction
   ↓
Evidence Verification
   ↓
Story Ranking
   ↓
Narrative Generation
   ↓
Audio + Animated Briefing

DayBrief separates retrieval from reasoning:

* Tavily provides current news and source material.
* NVIDIA Nemotron performs event understanding, claim extraction, verification, ranking, and narrative generation.

This helps keep generated briefings grounded in retrieved evidence instead of relying only on model knowledge.

Tech Stack

AI & Retrieval

* NVIDIA Nemotron
* Nebius Token Factory
* Tavily

Backend

* Python
* FastAPI
* Redis

Frontend

* React
* TypeScript

Infrastructure

* Docker
* Docker Compose
* Nginx
* Nebius Cloud

Grounded Generation

DayBrief is designed to reduce unsupported or unrelated claims.

Before a story reaches the final briefing, the pipeline checks:

* whether articles describe the same underlying event
* whether extracted claims are supported by retrieved sources
* whether evidence comes from sufficiently independent sources
* whether a story contains enough verified information to be useful

Only sufficiently supported stories are included in the final briefing.

Caching

Successfully generated briefings can be cached temporarily.

This allows DayBrief to reuse a recent high-quality briefing instead of repeatedly running the full retrieval and generation pipeline.

Caching is enabled by default and can be configured through environment variables.

Deployment

DayBrief is containerized with Docker Compose and can be deployed directly to a Nebius VM using the included deployment script.

The current version is deployed on Nebius and available at:

http://66.201.5.182:8080/

Why DayBrief

News aggregation is easy.

Producing a short briefing that is current, coherent, evidence-grounded, and actually useful is harder.

DayBrief focuses on that gap by combining real-time retrieval with AI reasoning, verification, ranking, narration, and visualization in one end-to-end pipeline.