# Release Notes

## v1.1.0

**Release:** https://github.com/speakmancip/speakmanai-local/releases/tag/v1.1.0

### Headline features

**1. Three-tier model control — fast / standard / advanced**
Previously agents only had a "fast" and "advanced" model to choose from. A new **standard** tier sits between them, giving finer control over the cost/quality tradeoff on a per-agent basis across every workflow. No workflow files need editing — providers and models are swapped centrally on the setup page and every agent's abstract tier resolves automatically.

**2. Vertex AI now configurable directly in the app**
Enterprise GCP users no longer need to hand-edit `.env` files or Docker config to use Vertex AI. The setup page now has a one-click "Vertex AI" provider option — enter your GCP project ID and region, and it authenticates via Application Default Credentials (no API key to manage or leak).

**3. New workflow: Requirements Engineering**
A new built-in pipeline (`MCP_REQUIREMENTS_ENGINEERING_V1`) turns a business description into structured, implementation-ready technical contracts — domain model, use cases, field definitions, naming conventions, and a full OpenAPI schema — with validation loops at each stage so downstream coding agents get contracts they can build against directly.

**4. New workflows now appear automatically — no reset required**
Previously, adding a new built-in workflow only took effect on a brand-new install; existing users had to wipe their local database to see it. Now new workflows are picked up automatically on the next launch, without touching any workflows or agents you've already customized.

**5. Broader model support, including the newest frontier models**
Teams who want maximum quality (and are fine paying more per call) can now point any tier at the latest model families — Gemini 3 and Claude 5 (Sonnet 5, Fable) — right from the setup page, alongside the recommended cost-efficient defaults.

### Under the hood
- Fixed several model-resolution bugs where a configured model choice was silently ignored in favor of a hardcoded default (affected Anthropic users most)
- Fixed a Windows desktop-app bug where Vertex AI credentials could resolve to the wrong path
- Updated to `gemini-3.1-flash-lite` (Google's now-GA name, replacing the retired preview alias)

---

## v1.0.0

Initial release — SPEAKMAN.AI local multi-agent workflow engine.
