"""Crawling: Stagehand-driven browsers, schemas, and per-council flows.

Only this package knows about Stagehand and the browser. The rest of
Tortoise talks to plain outcomes (:class:`~tortoise.crawl.crawler.CrawlListOutcome`
and friends) and a narrow driver protocol, so crawl logic is testable offline
with a fake driver and the SDK can be swapped without touching the workflow.
"""
