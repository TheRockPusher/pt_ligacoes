#!/usr/bin/env python3
"""Check the public journalist experience, without database or application access."""

import argparse
import json
import re
import sys
from contextlib import suppress
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

BASE_URL = "https://web-production-ca58.up.railway.app"
PM = "Luís Montenegro"
COMPANY = "Solverde"
FORMER_PMS = ("António Costa", "Pedro Passos Coelho", "José Sócrates")
FORMER_NAMES = {
    "António Costa": "António Luís Santos da Costa",
    "Pedro Passos Coelho": "Pedro Manuel Mamede Passos Coelho",
    "José Sócrates": "José Sócrates Carvalho Pinto de Sousa",
}
PARLIAMENT = "Assembleia da República"
BLOCKED = {"ep_reunioes", "rtri", "gov_audiencias"}
MIN_PROFILES, MIN_PM_LINKS, MIN_PM_KINDS = 5000, 4, 3
SAMPLE_SIZE, MAX_AGE_DAYS, TIMEOUT = 20, 45, 30
GOVERNMENT = {"government", "government_department", "government_office"}
PARLIAMENT_BODIES = {"parliamentary_group", "parliamentary_committee"}
RECORD_KINDS = {"contract", "subsidy", "eu_funding"}
CHECKS = (
    "flagship-path",
    "pm-profile",
    "former-pms",
    "source-coverage",
    "freshness",
    "breadth",
    "deputies-depth",
    "company-records",
    "evidence-verifiable",
)
USER_AGENT = "Ligacoes-Acceptance/1.0 (non-profit public evidence checks; polite HTTP client)"


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self):
        return "".join(c.text() if isinstance(c, Node) else c for c in self.children)

    def find(self, cls=None, tag=None, id=None):
        matches = not cls or cls in self.attrs.get("class", "").split()
        matches &= not tag or self.tag == tag
        matches &= not id or self.attrs.get("id") == id
        result: list[Node] = [self] if matches else []
        for child in self.children:
            if isinstance(child, Node):
                result.extend(child.find(cls, tag, id))
        return result


class Page(HTMLParser):
    VOID = (
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    )

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def number(text):
    digits = re.sub(r"\D", "", text)
    require(digits, f"missing numeric count: {text.strip()}")
    return int(digits)


class Acceptance:
    def __init__(self, base):
        self.base = base.rstrip("/") + "/"
        self.cache, self.evidence = {}, {}

    def get(self, path, **params):
        url = urljoin(self.base, path)
        if params:
            url += "?" + urlencode(params)
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ValueError("base URL must use HTTP or HTTPS")
        require(
            urlsplit(url).netloc == urlsplit(self.base).netloc, "unexpected external site request"
        )
        if url not in self.cache:
            # Scheme validated above; same public host only.
            request = Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
            with urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
                require(response.status == 200, f"HTTP {response.status}: {url}")
                self.cache[url] = response.read().decode("utf-8")
        return self.cache[url]

    def page(self, path, **params):
        return Page(self.get(path, **params)).root

    def resolve(self, name, kind):
        queries = (name, FORMER_NAMES[name]) if name in FORMER_NAMES else (name.split()[-1],)
        matches = set()
        for query in queries:
            page = self.page("/caminhos/entidades/", q=query, campo="de")
            for option in page.find("path-option"):
                names, kinds = option.find("path-option-name"), option.find("shape")
                if names and kinds and kinds[0].attrs.get("data-entity-kind") == kind:
                    label = names[0].text().strip().casefold()
                    match = (
                        label in {q.casefold() for q in queries}
                        if name in FORMER_NAMES
                        else all(word.casefold() in label for word in name.split())
                    )
                    if match:
                        matches.add(option.find(tag="input")[0].attrs["value"])
        require(len(matches) == 1, f"{name}: expected one public {kind}, found {len(matches)}")
        return "/entidades/" + next(iter(matches)) + "/"

    def profile(self, path, **params):
        page = self.page(path, **params)
        for link in page.find("evidence-link", "a"):
            href = link.attrs.get("href", "")
            if href.startswith("/evidencias/"):
                self.evidence[href] = None
        return page

    def graph(self, path, **params):
        self.profile(path, **params)
        graph = json.loads(self.get(path + "grafo/", **params))
        nodes = {n["data"]["id"]: n["data"] for n in graph["nodes"]}
        edges = [
            e["data"] for e in graph["edges"] if e["data"].get("url", "").startswith("/evidencias/")
        ]
        for edge in edges:
            self.evidence[edge["url"]] = None
        return nodes, edges, graph

    def has_office(self, nodes, edges, classifications):
        return any(
            e["kind"] == "public_office"
            and any(
                nodes.get(e[end], {}).get("classification") in classifications
                for end in ("source", "target")
            )
            for e in edges
        )

    def flagship_path(self):
        start, end = self.resolve(PM, "person"), self.resolve(COMPANY, "company")
        self.profile(start)
        self.profile(end)
        page = self.page("/caminhos/", de=start.split("/")[2], para=end.split("/")[2])
        hops = page.find("path-hop")
        require(hops, "no documented path shown between Montenegro and Solverde")
        for hop in hops:
            links = hop.find("evidence-link", "a")
            require(links, "path hop has no evidence link")
            for link in links:
                self.get(link.attrs["href"])
        return (
            f"{len(page.find('path', 'article'))} paths; {len(hops)} evidenced hops return HTTP 200"
        )

    def pm_profile(self):
        nodes, edges, _ = self.graph(self.resolve(PM, "person"))
        kinds = {e["kind"] for e in edges}
        require(
            len(edges) >= MIN_PM_LINKS and len(kinds) >= MIN_PM_KINDS,
            f"{len(edges)} documented connections / {len(kinds)} kinds; need {MIN_PM_LINKS} / {MIN_PM_KINDS}",
        )
        require(self.has_office(nodes, edges, GOVERNMENT), "missing Government office")
        require(self.has_office(nodes, edges, {"parliament"}), "missing Assembleia mandate")
        return f"{len(edges)} connections / {len(kinds)} kinds, Government office and Assembleia mandate"

    def former_pms(self):
        failures = []
        for name in FORMER_PMS:
            try:
                nodes, edges, _ = self.graph(self.resolve(name, "person"))
                require(
                    self.has_office(nodes, edges, GOVERNMENT), f"{name}: missing Government office"
                )
            except Exception as exc:
                failures.append(str(exc))
        require(not failures, "; ".join(failures))
        return "all three former Prime Ministers have documented Government offices"

    def sources(self):
        entries = self.page("/fontes/").find("source-entry")
        require(entries, "no dataset entries found")
        return [
            e
            for e in entries
            if e.attrs.get("data-status") == "imported" and e.attrs.get("id") not in BLOCKED
        ]

    def source_coverage(self):
        entries = self.sources()
        require(entries, "no imported datasets found")
        missing = [
            e.attrs["id"] for e in entries if sum(number(n.text()) for n in e.find("figure")) == 0
        ]
        require(not missing, "zero published links/records: " + ", ".join(missing))
        return f"{len(entries)} imported datasets have published content"

    def freshness(self):
        entries = [e for e in self.sources() if sum(number(n.text()) for n in e.find("figure")) > 0]
        require(entries, "no counted datasets found")
        stale = []
        for entry in entries:
            times = entry.find("source-figures")[0].find(tag="time")
            age = (
                (date.today() - date.fromisoformat(times[0].attrs["datetime"])).days
                if times
                else None
            )
            if age is None or not 0 <= age <= MAX_AGE_DAYS:
                stale.append(
                    f"{entry.attrs['id']} ({age} days)"
                    if age is not None
                    else entry.attrs["id"] + " (no date)"
                )
        require(not stale, "stale consultation: " + ", ".join(stale))
        return f"{len(entries)} counted datasets consulted within {MAX_AGE_DAYS} days"

    def breadth(self):
        counts = self.page("/").find(id="filtro-todos")
        require(counts, "directory total selector missing")
        total = number(counts[0].find("count")[0].text())
        require(total >= MIN_PROFILES, f"{total} public profiles; need {MIN_PROFILES}")
        return f"{total} public profiles"

    def deputies_depth(self):
        path = self.resolve(PARLIAMENT, "organisation")
        nodes, edges, _ = self.graph(path, at=date.today().isoformat())
        ids = {
            e[end]
            for e in edges
            if e["kind"] == "public_office" and e.get("temporal_status") == "current"
            for end in ("source", "target")
        }
        people = sorted(
            (n for key, n in nodes.items() if key in ids and n.get("kind") == "person"),
            key=lambda n: n["url"],
        )[:SAMPLE_SIZE]
        require(
            len(people) == SAMPLE_SIZE,
            f"only {len(people)} current deputies available; need {SAMPLE_SIZE}",
        )
        failures = []
        for person in people:
            ns, es, _ = self.graph(person["url"], at=date.today().isoformat())
            bodies = any(
                e["kind"] == "membership"
                and any(
                    ns.get(e[end], {}).get("classification") in PARLIAMENT_BODIES
                    for end in ("source", "target")
                )
                for e in es
            )
            if len(es) < 2 or not self.has_office(ns, es, {"parliament"}) or not bodies:
                failures.append(f"{person['label']} ({len(es)} connections)")
        require(not failures, "shallow deputy profiles: " + "; ".join(failures))
        return f"{len(people)} current deputies have mandate and group/committee connections"

    def company_records(self):
        _, edges, graph = self.graph(self.resolve(COMPANY, "company"))
        records = [
            e["data"]
            for e in graph["edges"]
            if e["data"].get("event_kind") in RECORD_KINDS and e["data"].get("count", 0) > 0
        ]
        require(
            records or len(edges) >= 2,
            f"Solverde: no published funding/contract/subsidy records and {len(edges)} connections",
        )
        return f"Solverde: {len(records)} record aggregates / {len(edges)} documented connections"

    def evidence_verifiable(self):
        if len(self.evidence) < SAMPLE_SIZE:
            for method in (
                self.pm_profile,
                self.former_pms,
                self.company_records,
                self.deputies_depth,
            ):
                # Missing prerequisites are reported by the sample-size check below.
                with suppress(Exception):
                    method()
        links = list(self.evidence)[:SAMPLE_SIZE]
        require(
            len(links) == SAMPLE_SIZE, f"only {len(links)} evidence pages found; need {SAMPLE_SIZE}"
        )
        for link in links:
            sources = self.page(link).find("source-link", "a")
            require(
                any(
                    urlsplit(n.attrs.get("href", "")).scheme in {"http", "https"}
                    and urlsplit(n.attrs["href"]).netloc
                    and urlsplit(n.attrs["href"]).netloc != urlsplit(self.base).netloc
                    for n in sources
                ),
                f"{link}: no external HTTP(S) original source",
            )
        return f"{len(links)} evidence pages return HTTP 200 and link to external originals"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--only", nargs="+", choices=CHECKS, help="run only these checks")
    args = parser.parse_args()
    acceptance, failed = Acceptance(args.base_url), False
    for name in CHECKS:
        if args.only and name not in args.only:
            continue
        try:
            detail = getattr(acceptance, name.replace("-", "_"))()
            print(f"PASS {name}: {detail}")
        except Exception as exc:
            print(f"FAIL {name}: {' '.join(str(exc).split()) or type(exc).__name__}")
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
