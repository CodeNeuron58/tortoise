"""Tests for crawl flows, run offline against a scripted fake driver."""

import urllib.error
from datetime import date
from pathlib import Path

import pytest

from tortoise.crawl import crawler as crawler_module
from tortoise.crawl.crawler import WeeklyListCrawler
from tortoise.crawl.documents import select_documents, url_filename
from tortoise.crawl.driver import DriverError
from tortoise.crawl.schemas import (
    ApplicationSummary,
    DocumentLink,
    DocumentsList,
    WeeklyListPage,
    WeeklyListRow,
)
from tortoise.models import Council, CrawlMode, WatchZone

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "application_form.pdf"


def make_council(mode=CrawlMode.IDOX_WEEKLY_LIST) -> Council:
    return Council(
        slug="test-council",
        name="Test Council",
        mode=mode,
        zone=WatchZone(label="zone", streets=("Mill Road",), postcode_sectors=("IP33 1",)),
        search_url="https://planning.example.gov.uk/online-applications/search.do?action=weeklyList",
        weekly_list_url="https://www.example.gov.uk/weekly-list",
    )


class FakeDriver:
    """A scripted BrowserDriver: queued result pages plus fixed detail data."""

    seconds_in_browser = 42.0

    def __init__(self, list_pages, summary=None, documents=None, idox_pages=None, idox_dates=None):
        self._list_pages = iter(list_pages)
        self._summary = summary or ApplicationSummary(comment_deadline="6 November 2026")
        self._documents = documents or DocumentsList(documents=[])
        self._idox_pages = iter(idox_pages or [])
        self.idox_dates = idox_dates or {
            "found": True,
            "dates": {
                "Application Validated Date": "Tue 20 Oct 2026",
                "Neighbour Consultation Expiry Date": "Sat 14 Nov 2026",
                "Latest Advertisement Expiry Date": "Fri 20 Nov 2026",
            },
        }
        self.extracted_schemas: list = []
        self.goto_urls: list[str] = []
        self.acts: list[str] = []
        self.fail_acts: set[str] = set()
        self.submitted: bool = False

    async def goto(self, url):
        self.goto_urls.append(url)

    async def act(self, instruction):
        if instruction in self.fail_acts:
            raise DriverError(f"no element for {instruction!r}")
        self.acts.append(instruction)
        return "ok"

    async def extract(self, instruction, schema):
        self.extracted_schemas.append(schema)
        if schema is WeeklyListPage:
            return next(self._list_pages)
        if schema is ApplicationSummary:
            return self._summary
        if schema is DocumentsList:
            return self._documents
        raise AssertionError(f"unexpected schema {schema}")

    async def observe(self, instruction):
        return []

    async def click_direct(self, purpose):
        return getattr(self, "direct_click_result", False)

    async def submit_idox_weekly_form(self):
        self.submitted = True

    async def extract_idox_rows(self):
        return next(self._idox_pages)

    async def extract_idox_documents(self):
        return getattr(self, "idox_documents", {"documents": []})

    async def extract_idox_dates(self):
        return self.idox_dates

    async def fetch_pdf(self, url):
        if getattr(self, "fail_session_fetch", False):
            raise DriverError("session fetch unavailable")
        return FIXTURE_PDF.read_bytes()

    async def close(self):
        pass


def idox_row(reference, address="14a Mill Rd"):
    return {
        "reference": reference,
        "address": address,
        "proposal": "Two-storey rear extension",
        "url": "applicationDetails.do?keyVal=X",
        "validated_on": "19 Oct 2026",
    }


def row(reference, address="14a Mill Rd", url="applicationDetails.do?keyVal=X"):
    return WeeklyListRow(
        reference=reference,
        address=address,
        proposal="Two-storey rear extension",
        validated_on="19 October 2026",
        url=url,
    )


async def collect_pages(pages):
    seen = []
    async def on_page(number):
        seen.append(number)
    return seen


def test_idox_flow_paginates_and_heartbeats():
    driver = FakeDriver(
        [],
        idox_pages=[
            {"rows": [idox_row("26/0001/FUL"), idox_row("26/0002/FUL")], "next_href": "pagedSearchResults.do?action=page&searchCriteria.page=2"},
            {"rows": [idox_row("26/0002/FUL"), idox_row("26/0003/FUL")], "next_href": None},
        ],
    )
    heartbeats: list[int] = []

    async def on_page(number):
        heartbeats.append(number)

    crawler = WeeklyListCrawler(driver, make_council(), on_page=on_page, pause_range=(0, 0))
    import asyncio

    outcome = asyncio.run(crawler.crawl_list())

    assert driver.submitted is True
    assert [a.reference for a in outcome.applications] == ["26/0001/FUL", "26/0002/FUL", "26/0003/FUL"]
    assert outcome.pages_read == 2
    assert heartbeats == [1, 2]
    # Pagination followed the real next-link hrefs, absolutised.
    assert driver.goto_urls[1].endswith("searchCriteria.page=2")
    assert all(a.url.startswith("https://planning.example.gov.uk/") for a in outcome.applications)
    assert outcome.applications[0].validated_on == date(2026, 10, 19)


def test_idox_falls_back_to_llm_when_no_rows_parse():
    driver = FakeDriver(
        # First the deterministic parse returns nothing...
        [WeeklyListPage(rows=[row("26/0001/FUL")], has_next_page=False)],
        idox_pages=[{"rows": [], "next_href": None}],
    )
    crawler = WeeklyListCrawler(driver, make_council(), pause_range=(0, 0))
    import asyncio

    outcome = asyncio.run(crawler.crawl_list())
    assert len(outcome.applications) == 1  # the LLM fallback produced rows


def test_idox_optional_steps_may_fail():
    # Deterministic parse finds nothing -> LLM fallback runs with failing
    # optional steps and still produces a row.
    driver = FakeDriver(
        [WeeklyListPage(rows=[row("26/0001/FUL")], has_next_page=False)],
        idox_pages=[{"rows": [], "next_href": None}],
    )
    driver.fail_acts = {
        "if a search-type choice is shown, choose 'Planning'",
        "set the list period to last week",
    }
    crawler = WeeklyListCrawler(driver, make_council(), pause_range=(0, 0))
    import asyncio

    outcome = asyncio.run(crawler.crawl_list())
    assert len(outcome.applications) == 1  # optional failures don't sink the crawl


def test_idox_llm_fallback_missing_next_click_raises():
    driver = FakeDriver(
        [WeeklyListPage(rows=[row("26/0001/FUL")], has_next_page=True)],
        idox_pages=[{"rows": [], "next_href": None}],
    )
    driver.fail_acts = {"click the Next page button"}
    driver.direct_click_result = False
    crawler = WeeklyListCrawler(driver, make_council(), pause_range=(0, 0))
    import asyncio

    with pytest.raises(DriverError):
        asyncio.run(crawler.crawl_list())


def test_weekly_list_page_mode_uses_the_published_page():
    driver = FakeDriver(
        [WeeklyListPage(rows=[row("26/0009/FUL", url="/app/9")], has_next_page=False)]
    )
    crawler = WeeklyListCrawler(
        driver, make_council(CrawlMode.WEEKLY_LIST_PAGE), pause_range=(0, 0)
    )
    import asyncio

    outcome = asyncio.run(crawler.crawl_list())
    assert driver.goto_urls == ["https://www.example.gov.uk/weekly-list"]
    assert outcome.applications[0].url == "https://www.example.gov.uk/app/9"


def test_fetch_application_downloads_selected_documents(tmp_path, monkeypatch):
    async def fake_download(url, dest_dir, filename, timeout=90.0):
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / filename
        path.write_bytes(FIXTURE_PDF.read_bytes())
        return path

    monkeypatch.setattr(crawler_module, "download_pdf_async", fake_download)

    driver = FakeDriver(
        [],
        summary=ApplicationSummary(comment_deadline="6 November 2026"),
        documents=DocumentsList(
            documents=[
                DocumentLink(name="Location plan.pdf", url="https://x.gov.uk/d1"),
                DocumentLink(name="Application form.pdf", url="https://x.gov.uk/d2"),
                DocumentLink(name="Design and access statement.pdf", url="https://x.gov.uk/d3"),
                DocumentLink(name="Planning statement.pdf", url="https://x.gov.uk/d4"),
                DocumentLink(name="photo of the street.pdf", url="https://x.gov.uk/d5"),
            ]
        ),
    )
    crawler = WeeklyListCrawler(driver, make_council(), pause_range=(0, 0))
    import asyncio

    application = crawler._row_to_application(row("26/0001/FUL"))
    outcome = asyncio.run(crawler.fetch_application(application, tmp_path / "pdfs"))

    # The deadline comes from the Important Dates tab, no LLM involved.
    assert outcome.comment_deadline == date(2026, 11, 14)
    assert ApplicationSummary not in driver.extracted_schemas
    assert driver.goto_urls[0].endswith("keyVal=X&activeTab=dates")
    assert driver.goto_urls[1].endswith("keyVal=X&activeTab=documents")
    names = [d.filename for d in outcome.documents]
    assert names == ["Application form.pdf", "Design and access statement.pdf", "Planning statement.pdf"]
    assert all(Path(d.path).exists() for d in outcome.documents)
    assert outcome.documents[0].pages == 2  # real text was extracted from the fixture PDF
    assert not outcome.documents[0].is_scanned
    assert outcome.pages_read == 2  # important dates + documents page


def test_fetch_application_without_documents(tmp_path, monkeypatch):
    async def refuse(*args, **kwargs):
        raise AssertionError("no downloads expected")

    monkeypatch.setattr(crawler_module, "download_pdf_async", refuse)
    driver = FakeDriver(
        [],
        idox_dates={"found": True, "dates": {"Neighbour Consultation Expiry Date": "Not Available"}},
    )
    crawler = WeeklyListCrawler(driver, make_council(), pause_range=(0, 0))
    import asyncio

    application = crawler._row_to_application(row("26/0002/FUL"))
    outcome = asyncio.run(crawler.fetch_application(application, tmp_path / "pdfs"))
    assert outcome.comment_deadline is None
    # The dates table was there, so "not set yet" is the answer: no LLM call.
    assert ApplicationSummary not in driver.extracted_schemas
    assert outcome.documents == []


def test_select_documents_prioritises_and_limits():
    links = [
        DocumentLink(name="Elevations.pdf", url="https://x/1"),
        DocumentLink(name="Planning statement.pdf", url="https://x/2"),
        DocumentLink(name="application form.pdf", url="https://x/3"),
        DocumentLink(name="Something odd.pdf", url="https://x/4"),
        DocumentLink(name="Design and Access Statement.pdf", url="https://x/5"),
        DocumentLink(name="no url", url=""),
    ]
    chosen = select_documents(links)
    assert [d.name for d in chosen] == [
        "application form.pdf",
        "Design and Access Statement.pdf",
        "Planning statement.pdf",
    ]


def test_url_filename_handles_idox_links():
    assert url_filename("https://x.gov.uk/online-applications/document/G123ABC") == "document.pdf"
    assert url_filename("https://x.gov.uk/docs?filename=plans.pdf") == "plans.pdf"
    assert url_filename("https://x.gov.uk/docs/report.PDF") == "report.PDF"


def test_download_pdf_writes_and_verifies(tmp_path, monkeypatch):
    from tortoise.crawl.documents import DownloadError, download_pdf

    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def read(self, n):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    good = b"%PDF-1.4 fake pdf bytes"
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: FakeResponse(good))
    path = download_pdf("https://x.gov.uk/a.pdf", tmp_path, "a.pdf")
    assert path.read_bytes() == good

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: FakeResponse(b"<html>nope</html>"))
    with pytest.raises(DownloadError):
        download_pdf("https://x.gov.uk/b.pdf", tmp_path, "b.pdf")

    def raise_http(req, timeout):
        raise urllib.error.HTTPError("https://x.gov.uk/c.pdf", 404, "nope", None, None)

    monkeypatch.setattr("urllib.request.urlopen", raise_http)
    with pytest.raises(DownloadError):
        download_pdf("https://x.gov.uk/c.pdf", tmp_path, "c.pdf")


def test_idox_resume_jumps_to_the_heartbeated_page_and_keeps_rows():
    driver = FakeDriver(
        [],
        idox_pages=[
            # Page 1 is loaded (after the form) but not re-parsed into rows...
            {"rows": [idox_row("26/0001/FUL")], "next_href": "pagedSearchResults.do?action=page&searchCriteria.page=2"},
            # ...the jump lands straight on page 3.
            {"rows": [idox_row("26/0005/FUL")], "next_href": None},
        ],
    )
    heartbeats: list[int] = []
    stored_at_heartbeat: list[list[str]] = []
    crawler = None

    async def on_page(number):
        stored_at_heartbeat.append([a.reference for a in crawler.applications_so_far()])
        heartbeats.append(number)

    crawler = WeeklyListCrawler(
        driver, make_council(), on_page=on_page, pause_range=(0, 0), start_page=3
    )
    import asyncio

    outcome = asyncio.run(crawler.crawl_list())
    assert [a.reference for a in outcome.applications] == ["26/0005/FUL"]
    assert heartbeats == [3]
    assert driver.goto_urls[1].endswith("searchCriteria.page=3")
    # Rows are available to store before the heartbeat is sent.
    assert stored_at_heartbeat == [["26/0005/FUL"]]


def test_comment_deadline_prefers_the_neighbour_consultation_expiry():
    from tortoise.crawl.crawler import comment_deadline_from_dates

    dates = {
        "Latest Consultation Expiry Date": "Tue 10 Nov 2026",
        "Neighbour Consultation Expiry Date": "Sat 14 Nov 2026",
        "Latest Advertisement Expiry Date": "Fri 20 Nov 2026",
    }
    assert comment_deadline_from_dates(dates) == date(2026, 11, 14)
    # Tree works have no neighbour letters: the site notice stands in.
    trees = {
        "Neighbour Consultation Expiry Date": "Not Available",
        "Latest Site Notice Expiry Date": "Thu 12 Nov 2026",
        "Latest Consultation Expiry Date": "Tue 10 Nov 2026",
    }
    assert comment_deadline_from_dates(trees) == date(2026, 11, 12)
    assert comment_deadline_from_dates({"Determination Deadline": "Tue 15 Dec 2026"}) is None


def test_idox_tab_urls():
    from tortoise.crawl.crawler import idox_tab_urls

    tabs = idox_tab_urls("https://x.gov.uk/online-applications/applicationDetails.do?keyVal=A&activeTab=summary")
    assert tabs == {
        "dates": "https://x.gov.uk/online-applications/applicationDetails.do?keyVal=A&activeTab=dates",
        "documents": "https://x.gov.uk/online-applications/applicationDetails.do?keyVal=A&activeTab=documents",
    }
    assert idox_tab_urls("https://www.example.gov.uk/app/9") is None


def test_non_idox_application_reads_the_deadline_with_the_llm(tmp_path):
    driver = FakeDriver([], summary=ApplicationSummary(comment_deadline="6 November 2026"))
    crawler = WeeklyListCrawler(driver, make_council(CrawlMode.WEEKLY_LIST_PAGE), pause_range=(0, 0))
    import asyncio

    application = crawler._row_to_application(row("26/0009/FUL", url="/app/9"))
    outcome = asyncio.run(crawler.fetch_application(application, tmp_path / "pdfs"))
    assert outcome.comment_deadline == date(2026, 11, 6)
    assert driver.goto_urls == ["https://www.example.gov.uk/app/9"]
    assert outcome.pages_read == 1


def test_select_documents_skips_pictures_and_duplicates():
    links = [
        DocumentLink(name="Application Form APPLICATION FORM - WITHOUT PERSONAL DATA", url="https://x/1"),
        DocumentLink(name="Application Form APPLICATION FORM - WITHOUT PERSONAL DATA", url="https://x/1"),
        DocumentLink(name="Supporting Documents CONTEXT PICTURE", url="https://x/2"),
        DocumentLink(name="Site and Other Plans/Drawings 001 LOCATION PLAN", url="https://x/3"),
    ]
    assert [d.url for d in select_documents(links)] == ["https://x/1"]
