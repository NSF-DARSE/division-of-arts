"""SceneScout: multi-source arts event discovery pipeline for DelawareScene.com.

Pipeline stages (see PLAN.md):
  A. registry   - load websites.csv, health-check, auto-detect extraction route
  B. extract    - per-route workers pull raw events
  C. normalize  - canonical schema + LLM category/relevance classification
  D. dedupe     - self-dedup, then fuzzy match against live DelawareScene
  E. export     - validated 13-column bulk-upload .xlsx + review report

Scene-side reference data (scene_orgs, scene_listings) is scraped from
delawarescene.com and feeds stages D and E.
"""

__version__ = "0.1.0"
