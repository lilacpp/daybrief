"""Build compact event searches from supplied reporting, without another model call."""

import re
from urllib.parse import urlsplit

GENERAL_QUERIES = (
    "major world news government international affairs latest developments",
    "major economy business public policy news latest developments",
    "major science technology health environment news latest developments",
)


MARKET_QUERIES = (
    "macroeconomic data releases inflation CPI GDP employment unemployment news",
    "central bank monetary policy interest rate decisions Federal Reserve ECB PBOC news",
    "government fiscal policy budget stimulus public debt international trade tariffs news",
)


def macro_candidate_priority(group):
    """Defer explicit previews/background pieces, without discarding reporting."""
    deferred = re.compile(
        r"\b(preview|history|historical|opinion|what.s ahead|"
        r"could .* congress|if congress|explainer|market summary|"
        r"market outlook|weekly outlook)\b", re.IGNORECASE
    )
    return int(all(deferred.search(article.title) for article in group))


def general_enrichment_priority(group):
    """Prioritize usable single-source occurrences; preserve retrieval order on ties."""
    background = re.compile(
        r"\b(opinion|preview|explainer|historical|history|weekly outlook|FAQ)\b",
        re.IGNORECASE,
    )
    usable = [article for article in group if article.snippet.strip()]
    deferred = not usable or all(background.search(a.title) for a in usable)
    hosts = {urlsplit(str(a.url)).hostname for a in usable}
    return (bool(deferred), len(hosts) != 1)


def event_search_query(group, attempt):
    query = _event_search_query(group, attempt)
    if attempt > 0 and group[0].topic == "markets":
        hosts = sorted({urlsplit(str(a.url)).hostname for a in group})
        query += "".join(f" -site:{host}" for host in hosts if host)
    return query


def _event_search_query(group, attempt):
    # Keep the seed event as the anchor across attempts; a secondary title may
    # discuss a different angle or contain publisher-specific wording.
    title = group[0].title
    title = re.split(r"\s+(?:\||—|–)\s+", title)[0]
    title = re.sub(
        r"\s+-\s+[^-]*(?:News|Finance|Wall St|TradingView|Economic Daily).*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"^(?:How Investors Are Reacting To|Why)\s+", "", title, flags=re.IGNORECASE
    )
    title = re.sub(r"\s+Is Getting Attention Today.*$", "", title, flags=re.IGNORECASE)
    text = " ".join([a.title + " " + a.snippet for a in group])
    tickers = list(
        dict.fromkeys(
            re.findall(
                r"\((?:(?:NYSE|NASDAQ|AMEX)\s*:\s*)?([A-Z][A-Z0-9.]{0,5})\)|\$([A-Z][A-Z0-9.]{0,5})\b",
                text,
            )
        )
    )
    ticker = next(
        (
            a or b
            for a, b in tickers
            if (a or b) not in {"AI", "US", "LLM", "GDP", "CEO"}
        ),
        "",
    )
    quarter = re.search(r"\bQ([1-4])\s*(?:FY\s*)?(20\d{2})\b", text, re.IGNORECASE)
    if quarter:
        period = f"Q{quarter[1]} {quarter[2]}"
    else:
        quarter = re.search(
            r"\b(first|second|third|fourth)\s+quarter\s+(?:of\s+)?(20\d{2})\b",
            text,
            re.IGNORECASE,
        )
        period = (
            f"Q{['first', 'second', 'third', 'fourth'].index(quarter[1].lower()) + 1} {quarter[2]}"
            if quarter
            else ""
        )
    macro_queries = []
    # Use only an explicitly reported event period, never the retrieval date.
    event_period = re.search(
        r"\b(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)(?:\s+20\d{2})?\b",
        title, re.IGNORECASE,
    )
    period_anchor = f" {event_period[0]}" if event_period else ""
    if re.search(r"\b(Fed|Federal Reserve)\b", title, re.IGNORECASE):
        action = (
            "rate hikes"
            if re.search(r"\b(hikes?|raises?)\b", title, re.IGNORECASE)
            else "interest rate decision outlook"
        )
        runoff = (
            " balance sheet runoff"
            if re.search(r"runoff|quantitative tightening", title, re.IGNORECASE)
            else ""
        )
        macro_queries.append(f"Federal Reserve {action}{runoff}{period_anchor}")
    if re.search(
        r"PBOC|People.s Bank of China|China.s Central Bank", title, re.IGNORECASE
    ):
        action = (
            "rate cuts"
            if re.search(r"\bcuts?\b", title, re.IGNORECASE)
            else "interest rate policy"
        )
        if re.search(r"loan prime rates?|\bLPRs?\b", title, re.IGNORECASE):
            action = "Loan Prime Rates LPR"
            if re.search(r"unchanged|steady|holds?", title, re.IGNORECASE):
                action += " unchanged"
            elif re.search(r"\bcuts?\b", title, re.IGNORECASE):
                action += " cuts"
            elif re.search(r"\b(hikes?|raises?)\b", title, re.IGNORECASE):
                action += " raises"
        macro_queries.append(f"PBOC China central bank {action}{period_anchor}")
    if macro_queries:
        query = macro_queries[attempt % len(macro_queries)]
        return query if attempt < len(macro_queries) else f"{query} policy announcement"
    # Economic releases may mention "earnings" (e.g. average hourly earnings).
    # Keep those anchored to the indicator instead of treating them as company results.
    if group[0].topic == "markets" and re.search(
        r"\b(GDP|CPI|PCE|inflation|unemployment|payrolls?|employment|fiscal|budget|tariffs?|stimulus)\b"
        r"|gross domestic product|average hourly earnings|public debt"
        r"|\bdeficits?\b|interest (?:costs|payments|expense)|defen[cs]e spending"
        r"|government (?:revenue|outlays|spending)|Treasury yields"
        r"|European Central Bank|Bank of England|Bank of Japan|\b(ECB|BOE|BOJ)\b",
        title,
        re.IGNORECASE,
    ):
        anchor = " ".join(title.split()[:16])
        fiscal_years = set(re.findall(
            r"\b(?:fiscal\s+(?:year\s+)?|FY\s*)(20\d{2})\b",
            group[0].snippet, re.IGNORECASE,
        ))
        # Snippets often mention prior-year comparisons before the actual report.
        # Only use an unambiguous year; never pick the first or newest blindly.
        if len(fiscal_years) == 1:
            fiscal_year = next(iter(fiscal_years))
            if fiscal_year not in anchor:
                anchor += f" fiscal year {fiscal_year}"
        suffix = (
            "data release policy details"
            if attempt == 0
            else "official announcement economic outlook"
        )
        return f"{anchor} {suffix}"
    company = re.split(
        r"\s+(?:To Report|Reports?|Reported|Raised|Raises|Announces?|Earnings|Shares|Stock)\b|\s*\(",
        title,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    if re.search(
        r"\b(earnings|revenue|EPS|net income)\b",
        title + " " + group[0].snippet,
        re.IGNORECASE,
    ) and not re.search(r"\b(markets?|futures|rally)\b", title, re.IGNORECASE):
        action = (
            "earnings results guidance"
            if attempt == 0
            else "quarterly financial results announcement"
        )
        # Quarters are included only when explicitly present in the reporting.
        return " ".join(part for part in (company, ticker, period, action) if part)
    # Other events keep their concrete actions/entities, without publication dates
    # masquerading as event dates or appended publisher names.
    anchor = " ".join(title.split()[:16])
    return anchor if attempt == 0 else f"{anchor} announcement details"
