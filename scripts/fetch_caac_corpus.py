#!/usr/bin/env python
"""Fetch CAAC aviation laws/regulations into the local RAG corpus.

The CAAC public information-disclosure search is the stable index:

* fl=12: 法律法规 (aviation laws + administrative regulations)
* channelid=269689: 民航规章 / CCAR department rules

For each source page we write a cleaned Markdown page and download official
PDF attachments when present. ``legal_helper.rag.ingest`` reads the generated
manifest metadata and indexes both Markdown and PDFs.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import yaml
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_ROOT = REPO_ROOT / "legal_helper" / "rag" / "corpus" / "caac_ccar"
MANIFEST = CORPUS_ROOT / "manifest.yaml"
USER_AGENT = "legal-helper-caac-corpus/1.0 (+https://www.caac.gov.cn/)"
TIMEOUT = httpx.Timeout(30.0, connect=15.0)


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    search_url: str
    url_fragment: str


CATEGORIES: tuple[Category, ...] = (
    Category(
        key="laws",
        label="法律法规",
        search_url="https://www.caac.gov.cn/was5/web/search?page={page}&channelid=211383&fl=12",
        url_fragment="/XXGK/XXGK/FLFG/",
    ),
    Category(
        key="ccar",
        label="民航规章",
        search_url="https://www.caac.gov.cn/was5/web/search?page={page}&channelid=269689",
        url_fragment="/XXGK/XXGK/MHGZ/",
    ),
)

META_KEYS = {
    "主题分类": "subject_category",
    "体裁分类": "document_type",
    "办文单位": "issuing_office",
    "发文日期": "publish_date",
    "成文日期": "adoption_date",
    "名称": "title",
    "字号": "font_size",
    "文号": "document_number",
    "有效性": "validity",
    "部号": "ccar_part",
}


def normalize_space(text: str) -> str:
    return re.sub(r"[ \t\u3000\xa0]+", " ", text).strip()


def safe_name(value: str, fallback: str) -> str:
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", value)
    value = value.strip("._-")
    return value[:90] or fallback


def normalize_caac_url(url: str) -> str:
    return url.replace("http://www.caac.gov.cn/", "https://www.caac.gov.cn/")


def doc_id_from_url(url: str) -> str:
    match = re.search(r"/(t\d+_\d+)\.html", url)
    if match:
        return match.group(1)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"doc_{digest}"


def fetch_text(client: httpx.Client, url: str) -> str:
    resp = client.get(normalize_caac_url(url))
    resp.raise_for_status()
    return resp.text


class DownloadTimeout(RuntimeError):
    pass


@contextlib.contextmanager
def wall_clock_timeout(seconds: int):
    def _raise_timeout(_signum: int, _frame: object) -> None:
        raise DownloadTimeout(f"download exceeded {seconds}s")

    old_handler = signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def fetch_bytes(client: httpx.Client, url: str, *, max_seconds: int) -> bytes:
    with wall_clock_timeout(max_seconds):
        resp = client.get(normalize_caac_url(url))
    resp.raise_for_status()
    return resp.content


def search_category(
    client: httpx.Client,
    category: Category,
    *,
    sleep_seconds: float,
    max_pages: int,
) -> list[tuple[str, str]]:
    seen: set[str] = set()
    docs: list[tuple[str, str]] = []
    empty_pages = 0
    for page in range(1, max_pages + 1):
        html = fetch_text(client, category.search_url.format(page=page))
        soup = BeautifulSoup(html, "lxml")
        page_docs = 0
        for anchor in soup.find_all("a", href=True):
            href = normalize_caac_url(urljoin(category.search_url, anchor["href"]))
            title = normalize_space(anchor.get_text(" ", strip=True))
            if not title or category.url_fragment not in href or not href.endswith(".html"):
                continue
            if href in seen:
                continue
            seen.add(href)
            docs.append((title, href))
            page_docs += 1
        print(f"  {category.label}: page {page} -> {page_docs} document(s)")
        if page_docs == 0:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0
        time.sleep(sleep_seconds)
    return docs


def extract_metadata(soup: BeautifulSoup) -> dict[str, str]:
    meta: dict[str, str] = {}
    nav = soup.select_one(".content_nav") or soup
    lines = [normalize_space(x) for x in nav.get_text("\n", strip=True).splitlines()]
    lines = [x for x in lines if x]
    for i, line in enumerate(lines):
        label = line.rstrip("：:")
        if label in META_KEYS and i + 1 < len(lines):
            value = lines[i + 1]
            if value and value.rstrip("：:") not in META_KEYS:
                meta[META_KEYS[label]] = value
            continue
        for raw, key in META_KEYS.items():
            prefix = f"{raw}："
            if line.startswith(prefix):
                value = line[len(prefix) :].strip()
                if value:
                    meta[key] = value
    title = soup.select_one(".content_t")
    if title:
        meta["title"] = normalize_space(title.get_text(" ", strip=True))
    if not meta.get("title") and soup.title and soup.title.string:
        meta["title"] = normalize_space(soup.title.string)
    return meta


def clean_content(soup: BeautifulSoup) -> str:
    content = soup.select_one(".content") or soup.select_one(".TRS_Editor")
    if content is None:
        return ""
    content = BeautifulSoup(str(content), "lxml")
    for tag in content(["script", "style", "noscript"]):
        tag.decompose()
    lines = [normalize_space(x) for x in content.get_text("\n", strip=True).splitlines()]
    out: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank:
                out.append("")
            blank = True
            continue
        out.append(line)
        blank = False
    return "\n".join(out).strip()


def attachment_links(page_url: str, soup: BeautifulSoup) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select(".content a[href], #id_tblAppendix a[href]"):
        href = normalize_caac_url(urljoin(page_url, anchor["href"]))
        label = normalize_space(anchor.get_text(" ", strip=True)) or Path(href).name
        if ".pdf" not in href.lower() or href in seen:
            continue
        seen.add(href)
        links.append((label, href))
    return links


def write_markdown(
    rel_path: Path,
    *,
    title: str,
    url: str,
    category: Category,
    metadata: dict[str, str],
    body: str,
    attachments: list[tuple[str, str, str]],
) -> None:
    path = CORPUS_ROOT / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {title}",
        "",
        f"- Source URL: {url}",
        f"- CAAC category: {category.label}",
    ]
    for key in [
        "ccar_part",
        "document_number",
        "validity",
        "issuing_office",
        "publish_date",
        "adoption_date",
    ]:
        if metadata.get(key):
            lines.append(f"- {key}: {metadata[key]}")
    lines.extend(["", "## 正文", "", body or "(No inline body; see official attachment.)"])
    if attachments:
        lines.extend(["", "## 附件", ""])
        for label, source_url, file_rel in attachments:
            lines.append(f"- {label}: {source_url} ({file_rel})")
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def build_entry(rel_path: Path, *, category: Category, source_url: str, metadata: dict[str, Any]) -> dict[str, Any]:
    title = metadata.get("title") or rel_path.stem
    return {
        "file": rel_path.as_posix(),
        "source_url": source_url,
        "source_authority": "Civil Aviation Administration of China",
        "jurisdiction": "CN",
        "language": "zh",
        "corpus_category": category.key,
        "legal_category": category.label,
        "title": title,
        "cite_prefix": metadata.get("ccar_part") or title,
        **{k: v for k, v in metadata.items() if v},
    }


def valid_markdown(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 100:
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    return text.startswith("# ") and "## 正文" in text


def valid_pdf(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        return b"%PDF-" in path.read_bytes()[:1024]
    except OSError:
        return False


def parse_existing_markdown(path: Path, title_hint: str, source_url: str) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    metadata: dict[str, str] = {"title": title_hint}
    attachments: list[tuple[str, str, str]] = []
    if match := re.search(r"^#\s+(.+)$", text, re.M):
        metadata["title"] = normalize_space(match.group(1))
    for line in text.splitlines():
        if not line.startswith("- "):
            continue
        item = line[2:].strip()
        if item.startswith("Source URL:"):
            source_url = normalize_space(item.split(":", 1)[1])
            continue
        if item.startswith("CAAC category:"):
            continue
        key, sep, value = item.partition(":")
        if sep and key in {
            "ccar_part",
            "document_number",
            "validity",
            "issuing_office",
            "publish_date",
            "adoption_date",
        }:
            metadata[key] = normalize_space(value)
            continue
        att = re.match(r"(.+?):\s+(https?://\S+)\s+\(([^)]+)\)$", item)
        if att:
            attachments.append(
                (
                    normalize_space(att.group(1)),
                    normalize_caac_url(att.group(2)),
                    att.group(3),
                )
            )
    metadata["source_url"] = normalize_caac_url(source_url)
    return metadata, attachments


def existing_document_entries(category: Category, title_hint: str, url: str) -> list[dict[str, Any]]:
    url = normalize_caac_url(url)
    rel_md = Path("documents") / category.key / f"{doc_id_from_url(url)}.md"
    md_path = CORPUS_ROOT / rel_md
    if not valid_markdown(md_path):
        return []
    metadata, attachments = parse_existing_markdown(md_path, title_hint, url)
    entries = [
        build_entry(
            rel_md,
            category=category,
            source_url=metadata.get("source_url") or url,
            metadata={**metadata, "file_kind": "source_page"},
        )
    ]
    for label, href, rel in attachments:
        pdf_rel = Path(rel)
        if not valid_pdf(CORPUS_ROOT / pdf_rel):
            continue
        pdf_meta = {
            **metadata,
            "title": f"{metadata.get('title') or title_hint} - {label}",
            "parent_url": url,
            "file_kind": "attachment_pdf",
        }
        entries.append(build_entry(pdf_rel, category=category, source_url=href, metadata=pdf_meta))
    return entries


def load_manifest_entries() -> dict[str, dict[str, Any]]:
    if not MANIFEST.is_file():
        return {}
    raw = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    entries: dict[str, dict[str, Any]] = {}
    for entry in raw.get("files") or []:
        rel = entry.get("file")
        if not rel:
            continue
        path = CORPUS_ROOT / rel
        if path.suffix.lower() == ".pdf":
            if not valid_pdf(path):
                continue
        elif not path.is_file() or path.stat().st_size == 0:
            continue
        entries[rel] = dict(entry)
    return entries


def fetch_document(
    client: httpx.Client,
    category: Category,
    title_hint: str,
    url: str,
    *,
    force: bool,
    sleep_seconds: float,
    attachment_timeout: int,
) -> list[dict[str, Any]]:
    url = normalize_caac_url(url)
    if not force:
        existing = existing_document_entries(category, title_hint, url)
        if existing:
            print(f"    = skip existing {existing[0]['file']}")
            return existing

    html = fetch_text(client, url)
    soup = BeautifulSoup(html, "lxml")
    metadata = extract_metadata(soup)
    metadata["title"] = metadata.get("title") or title_hint
    title = metadata["title"]
    doc_id = doc_id_from_url(url)
    body = clean_content(soup)
    entries: list[dict[str, Any]] = []
    downloaded_attachments: list[tuple[str, str, str]] = []

    for idx, (label, href) in enumerate(attachment_links(url, soup), start=1):
        suffix = Path(href.split("?", 1)[0]).suffix.lower() or ".pdf"
        label_stem = label[: -len(suffix)] if label.lower().endswith(suffix) else label
        rel = Path("attachments") / category.key / f"{doc_id}_{idx}_{safe_name(label_stem, 'attachment')}{suffix}"
        out = CORPUS_ROOT / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        needs_download = (
            force
            or not out.exists()
            or out.stat().st_size == 0
            or (suffix == ".pdf" and not valid_pdf(out))
        )
        if needs_download:
            try:
                data = fetch_bytes(client, href, max_seconds=attachment_timeout)
            except Exception as exc:  # noqa: BLE001
                print(f"    ! skipped attachment: {href} ({exc!s})", file=sys.stderr)
                continue
            if suffix == ".pdf" and b"%PDF-" not in data[:1024]:
                print(f"    ! skipped non-PDF attachment: {href}", file=sys.stderr)
                continue
            out.write_bytes(data)
            time.sleep(sleep_seconds)
        downloaded_attachments.append((label, href, rel.as_posix()))
        pdf_meta = {
            **metadata,
            "title": f"{title} - {label}",
            "parent_url": url,
            "file_kind": "attachment_pdf",
        }
        entries.append(build_entry(rel, category=category, source_url=href, metadata=pdf_meta))

    rel_md = Path("documents") / category.key / f"{doc_id}.md"
    write_markdown(
        rel_md,
        title=title,
        url=url,
        category=category,
        metadata=metadata,
        body=body,
        attachments=downloaded_attachments,
    )
    page_meta = {**metadata, "file_kind": "source_page"}
    entries.insert(0, build_entry(rel_md, category=category, source_url=url, metadata=page_meta))
    time.sleep(sleep_seconds)
    return entries


def write_manifest(files: list[dict[str, Any]]) -> None:
    CORPUS_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {
        "caac_ccar_corpus_version": datetime.now(timezone.utc).date().isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "description": "CAAC public aviation laws/regulations and CCAR department rules fetched from caac.gov.cn.",
        "sources": [
            {
                "category": c.key,
                "label": c.label,
                "search_url": c.search_url.format(page=1),
                "url_fragment": c.url_fragment,
            }
            for c in CATEGORIES
        ],
        "files": sorted(files, key=lambda x: x["file"]),
    }
    MANIFEST.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch CAAC laws/regulations for local RAG.")
    parser.add_argument("--force", action="store_true", help="re-fetch existing Markdown pages and re-download existing PDFs")
    parser.add_argument("--category", choices=["all", "laws", "ccar"], default="all")
    parser.add_argument("--limit", type=int, default=0, help="maximum documents per category (debug)")
    parser.add_argument("--max-pages", type=int, default=80, help="maximum CAAC search pages per category")
    parser.add_argument("--sleep", type=float, default=0.35, help="delay between CAAC requests")
    parser.add_argument("--attachment-timeout", type=int, default=25, help="wall-clock seconds per PDF attachment")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected = [c for c in CATEGORIES if args.category in ("all", c.key)]
    headers = {"User-Agent": USER_AGENT}
    files_by_path = load_manifest_entries()
    if files_by_path:
        print(f"==> Resuming with {len(files_by_path)} valid manifest entrie(s)")
    with httpx.Client(headers=headers, follow_redirects=True, timeout=TIMEOUT) as client:
        for category in selected:
            print(f"==> Discovering {category.label}")
            docs = search_category(
                client,
                category,
                sleep_seconds=args.sleep,
                max_pages=args.max_pages,
            )
            if args.limit:
                docs = docs[: args.limit]
            print(f"==> Fetching {len(docs)} {category.label} document(s)")
            for idx, (title, url) in enumerate(docs, start=1):
                print(f"  [{idx}/{len(docs)}] {title}")
                try:
                    entries = fetch_document(
                        client,
                        category,
                        title,
                        url,
                        force=args.force,
                        sleep_seconds=args.sleep,
                        attachment_timeout=args.attachment_timeout,
                    )
                    for entry in entries:
                        files_by_path[entry["file"]] = entry
                    write_manifest(list(files_by_path.values()))
                except Exception as exc:  # noqa: BLE001
                    print(f"    ! failed: {url} ({exc!s})", file=sys.stderr)
                    write_manifest(list(files_by_path.values()))
    write_manifest(list(files_by_path.values()))
    print(f"Done: staged {len(files_by_path)} file entry/entries under {CORPUS_ROOT.relative_to(REPO_ROOT)}")
    print(f"Manifest: {MANIFEST.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
