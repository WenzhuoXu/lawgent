"""France: Légifrance and Judilibre through the PISTE API gateway.

fr.legifrance — DILA's Légifrance API: search of the consolidated codes
(CODE_ETAT), laws and decrees (LODA_ETAT), the Journal officiel (JORF) and
administrative / constitutional case law (CETAT, CONSTIT; JURI on request);
article-level fetch (``/consult/getArticle`` by LEGIARTI id, or the article in
force by code + number with ``/consult/getArticleWithIdAndNum``, e.g. Code des
transports L6421-4), whole texts in their version in force
(``/consult/code``, ``/consult/lawDecree``) and decisions (``/consult/juri``).

fr.judilibre — the Cour de cassation's open-data API: full-text search of its
decisions (and those of the cours d'appel / tribunaux judiciaires it
publishes) and the decision text with its zones (exposé, moyens, motivation,
dispositif).

Both need one free PISTE application (piste.gouv.fr → application →
subscribe "Légifrance" and "JUDILIBRE", accept the CGU; no approval step):
``PISTE_CLIENT_ID`` / ``PISTE_CLIENT_SECRET`` for the OAuth client-credentials
grant. ``PISTE_SANDBOX=1`` points both at the sandbox hosts (sandbox apps have
their own keys). legifrance.gouv.fr itself is behind a bot challenge and is
used only for citation URLs.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import threading
import time
from typing import Any, Optional

import httpx

from ...connectors.base import USER_AGENT, request_with_retry
from ..base import Hit, LegalDoc, Source, SourceUnavailable
from ..sections import detect_sections
from ..textify import html_to_text, normalize_lines

_LONG = httpx.Timeout(90.0, connect=10.0)
_AUTH_ENV = ("PISTE_CLIENT_ID", "PISTE_CLIENT_SECRET")
_AUTH_HELP = (
    "Free: create an account and an application at https://piste.gouv.fr, subscribe it to the "
    "'Légifrance' and 'JUDILIBRE' APIs (accept their CGU; no approval), then put the OAuth "
    "client_id / client_secret in legal_helper/api_key as PISTE_CLIENT_ID / PISTE_CLIENT_SECRET."
)


def _sandbox() -> bool:
    return (os.getenv("PISTE_SANDBOX") or "").strip().lower() in ("1", "true", "yes", "on")


def _hosts() -> tuple[str, str]:
    if _sandbox():
        return "https://sandbox-oauth.piste.gouv.fr/api/oauth/token", "https://sandbox-api.piste.gouv.fr"
    return "https://oauth.piste.gouv.fr/api/oauth/token", "https://api.piste.gouv.fr"


def _today() -> str:
    return _dt.date.today().isoformat()


class _PisteToken:
    """One OAuth client-credentials token, shared and reused until shortly before it expires."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0
        self._for = ("", "", "")

    def get(self, *, force: bool = False) -> str:
        cid = os.getenv("PISTE_CLIENT_ID", "").strip()
        secret = os.getenv("PISTE_CLIENT_SECRET", "").strip()
        token_url, _ = _hosts()
        with self._lock:
            if (not force and self._token and time.time() < self._expires
                    and self._for == (cid, secret, token_url)):
                return self._token
            try:
                resp = httpx.post(
                    token_url,
                    data={"grant_type": "client_credentials", "client_id": cid,
                          "client_secret": secret, "scope": "openid"},
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    timeout=httpx.Timeout(20.0, connect=8.0),
                )
            except httpx.HTTPError as exc:
                raise SourceUnavailable(f"PISTE token endpoint did not answer ({type(exc).__name__}); try later.") from exc
            body: dict[str, Any] = {}
            try:
                body = resp.json()
            except ValueError:
                pass
            if resp.status_code != 200 or not body.get("access_token"):
                err = body.get("error") or f"HTTP {resp.status_code}"
                desc = (body.get("error_description") or resp.text or "").split(" (e.g.")[0][:160]
                if err in ("invalid_client", "unauthorized_client") or resp.status_code in (400, 401):
                    raise SourceUnavailable(
                        f"PISTE rejected the credentials ({err}: {desc.strip()}). Check PISTE_CLIENT_ID / "
                        "PISTE_CLIENT_SECRET (production keys, or set PISTE_SANDBOX=1 for sandbox keys) at "
                        "piste.gouv.fr → Mes applications."
                    )
                raise SourceUnavailable(f"PISTE token request failed ({err}: {desc.strip()})")
            self._token = body["access_token"]
            self._expires = time.time() + max(60, int(body.get("expires_in") or 3600) - 60)
            self._for = (cid, secret, token_url)
            return self._token


_TOKEN = _PisteToken()


def _piste(method: str, path: str, *, api: str, json_body: Optional[dict[str, Any]] = None,
           params: Optional[dict[str, Any]] = None) -> Any:
    """One authenticated PISTE call; a 401 refreshes the token once."""
    _, base = _hosts()
    url = base + path
    for attempt in (0, 1):
        token = _TOKEN.get(force=attempt == 1)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Authorization": f"Bearer {token}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = request_with_retry(method, url, json_body=json_body, params=params, headers=headers, timeout=_LONG)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 401 and attempt == 0:
                continue
            if code in (401, 403):
                raise SourceUnavailable(
                    f"PISTE refused the {api} call (HTTP {code}): the application is probably not subscribed to "
                    f"the {api} API or its CGU are not accepted (piste.gouv.fr → application → APIs)."
                ) from exc
            if code == 429:
                raise SourceUnavailable(f"{api} quota reached (HTTP 429); try later.") from exc
            if code == 404:
                raise ValueError(f"{api}: not found ({path})") from exc
            detail = exc.response.text[:200]
            raise SourceUnavailable(f"{api} answered HTTP {code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{api} did not answer ({type(exc).__name__}); try later.") from exc
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceUnavailable(f"{api} answered with something other than JSON") from exc
    raise SourceUnavailable(f"PISTE kept refusing the token for {api}")


# =============================================================== Légifrance

_LF = "/dila/legifrance/lf-engine-app"
_LF_SITE = "https://www.legifrance.gouv.fr"
#: Codes by name → LEGITEXT id (the ones an airline's lawyer reaches for).
_CODES = {
    "code des transports": "LEGITEXT000023086525",
    "code civil": "LEGITEXT000006070721",
    "code de la consommation": "LEGITEXT000006069565",
    "code de l'aviation civile": "LEGITEXT000006074234",
    "code du travail": "LEGITEXT000006072050",
    "code de commerce": "LEGITEXT000005634379",
    "code pénal": "LEGITEXT000006070719",
    "code de procédure civile": "LEGITEXT000006070716",
    "code des assurances": "LEGITEXT000006073984",
    "code de l'environnement": "LEGITEXT000006074220",
    "code des douanes": "LEGITEXT000006071570",
    "code général des impôts": "LEGITEXT000006069577",
}
_ID = re.compile(r"\b(LEGIARTI|LEGITEXT|JORFTEXT|JORFARTI|JURITEXT|CETATEXT|CONSTEXT|JUFITEXT|LEGISCTA)\d{12}\b")
_ART_NUM = re.compile(r"\b(?:art(?:icle)?\.?\s*)?([LRDA])\.?\s?(\d{1,5}(?:-\d{1,5}){0,3})\b", re.I)
_TAG = re.compile(r"<[^>]+>")
_CASE_FONDS = ("CETAT", "CONSTIT")
_STATUS = {"VIGUEUR": "in force", "ABROGE": "repealed", "VIGUEUR_DIFF": "in force later (deferred)",
           "MODIFIE": "amended (superseded version)", "ABROGE_DIFF": "repealed later (deferred)",
           "PERIME": "lapsed", "ANNULE": "annulled", "TRANSFERE": "transferred"}


def _ms_date(v: Any) -> Optional[str]:
    """PISTE dates come as epoch milliseconds or ISO strings."""
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        try:
            d = _dt.datetime.utcfromtimestamp(int(v) / 1000).date()
        except (ValueError, OverflowError, OSError):
            return None
        return None if d.year >= 2999 else d.isoformat()
    s = str(v)[:10]
    return None if s.startswith(("2999", "9999")) else s


def _article_key(num: str) -> str:
    n = (num or "").strip()
    m = re.match(r"^([LRDA])\.?\s*(\d.*)$", n, re.I)
    if m:
        return m.group(1).upper() + m.group(2).replace(" ", "")
    return n.replace(" ", "")


def _code_id(text: str) -> Optional[str]:
    low = " ".join((text or "").lower().replace("’", "'").split())
    for name, cid in sorted(_CODES.items(), key=lambda kv: -len(kv[0])):
        if name in low:
            return cid
    return None


def _criterion(value: str, kind: str = "TOUS_LES_MOTS_DANS_UN_CHAMP") -> dict[str, Any]:
    return {"valeur": value, "typeRecherche": kind, "operateur": "ET"}


def _lf_search(fond: str, champs: list[dict[str, Any]], filtres: list[dict[str, Any]], size: int,
               sort: str = "PERTINENCE") -> dict[str, Any]:
    body = {"fond": fond, "recherche": {
        "champs": champs, "filtres": filtres, "pageNumber": 1, "pageSize": max(1, min(size, 50)),
        "operateur": "ET", "sort": sort, "typePagination": "DEFAUT",
    }}
    data = _piste("POST", f"{_LF}/search", api="Légifrance", json_body=body)
    if not isinstance(data, dict) or "results" not in data:
        raise SourceUnavailable("Légifrance /search answered in an unknown format")
    return data


def _clean(s: Any) -> str:
    return " ".join(_TAG.sub("", str(s or "")).split())


def _hits_from_results(src: "LegifranceSource", fond: str, data: dict[str, Any], limit: int) -> list[Hit]:
    hits: list[Hit] = []
    case = fond in ("JURI", "CETAT", "CONSTIT", "JUFI")
    for r in data.get("results") or []:
        titles = r.get("titles") or [{}]
        t0 = titles[0] if titles else {}
        text_id = t0.get("cid") or t0.get("id") or ""
        text_title = _clean(t0.get("title")) or _clean(r.get("text")) or text_id
        status = _STATUS.get(t0.get("legalStatus") or r.get("etat") or "", t0.get("legalStatus") or r.get("etat"))
        arts = []
        for sec in r.get("sections") or []:
            for ex in sec.get("extracts") or []:
                if (ex.get("type") or "").lower().startswith("article") and ex.get("id"):
                    arts.append((sec, ex))
        if arts and not case:
            for sec, ex in arts:
                num = ex.get("num") or ""
                snippet = " … ".join(_clean(v) for v in ex.get("values") or [])[:500]
                hits.append(Hit(
                    ref=src.ref(ex["id"]), title=f"{text_title}, article {num}".strip(", "),
                    source=src.id, kind="legislation",
                    url=f"{_LF_SITE}/codes/article_lc/{ex['id']}" if fond.startswith("CODE") else f"{_LF_SITE}/loda/article_lc/{ex['id']}",
                    date=_ms_date(ex.get("dateDebut")),
                    status=_STATUS.get(ex.get("legalStatus") or "", ex.get("legalStatus")),
                    snippet=snippet or _clean(sec.get("title"))[:300], citation=f"art. {num}, {text_title}" if num else None,
                    extra={k: v for k, v in {"text_id": text_id, "section": _clean(sec.get("title"))[:160]}.items() if v},
                ))
        elif text_id:
            snippet = " … ".join(_clean(v) for sec in r.get("sections") or [] for ex in sec.get("extracts") or []
                                 for v in ex.get("values") or [])[:500]
            hits.append(Hit(
                ref=src.ref(text_id), title=text_title, source=src.id, kind="case" if case else "legislation",
                url=f"{_LF_SITE}/{'juri' if fond == 'JURI' else 'ceta' if fond == 'CETAT' else 'cons' if fond == 'CONSTIT' else 'loda'}/id/{text_id}",
                date=_ms_date(r.get("date") or r.get("datePublication") or t0.get("startDate")),
                status=status, snippet=snippet or _clean(r.get("resumePrincipal") or "")[:500],
                citation=r.get("num") or r.get("nor") or None,
                extra={k: v for k, v in {"nature": r.get("nature"), "origin": r.get("origin")}.items() if v},
            ))
        if len(hits) >= limit:
            break
    return hits[:limit]


def _render_text(resp: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """A /consult/code or /consult/lawDecree response → text + sections (articles and containers)."""
    lines: list[str] = []
    pos = 0
    sections: list[dict[str, Any]] = []

    def add(line: str) -> int:
        nonlocal pos
        start = pos + (1 if lines else 0)
        lines.append(line)
        pos = start + len(line)
        return start

    def article(a: dict[str, Any]) -> None:
        num = (a.get("num") or "").strip()
        label = f"Article {num}" if num else "Article"
        body = html_to_text(a.get("content") or a.get("texte") or "")
        etat = a.get("etat") or ""
        head = label + (f" ({_STATUS.get(etat, etat)})" if etat and etat != "VIGUEUR" else "")
        start = add(head)
        for ln in body.split("\n"):
            if ln.strip():
                add(ln)
        if a.get("nota"):
            add("NOTA : " + html_to_text(a["nota"]).replace("\n", " "))
        sections.append({"label": label, "key": _article_key(num) if num else "", "heading": head[:120],
                         "start": start, "end": pos, "level": "article" if num else "para"})

    def section(s: dict[str, Any]) -> None:
        title = _clean(s.get("title"))
        start = None
        if title:
            if lines:
                add("")
            start = add(title)
        for a in sorted(s.get("articles") or [], key=lambda x: x.get("intOrdre") or 0):
            article(a)
        for sub in sorted(s.get("sections") or [], key=lambda x: x.get("intOrdre") or 0):
            section(sub)
        if title and start is not None:
            sections.append({"label": title[:80], "key": "", "heading": title[:120], "start": start,
                             "end": pos, "level": "container"})

    for key in ("visa", "visas"):
        if resp.get(key):
            for ln in html_to_text(resp[key]).split("\n"):
                if ln.strip():
                    add(ln)
    for a in sorted(resp.get("articles") or [], key=lambda x: x.get("intOrdre") or 0):
        article(a)
    for s in sorted(resp.get("sections") or [], key=lambda x: x.get("intOrdre") or 0):
        section(s)
    if resp.get("signers"):
        add("")
        add(_clean(resp["signers"]))
    sections.sort(key=lambda s: (s["start"], 0 if s["level"] == "container" else 1))
    return "\n".join(lines), sections


class LegifranceSource(Source):
    id = "fr.legifrance"
    name = "Légifrance (DILA, via PISTE)"
    jurisdictions = ("FR",)
    kinds = ("legislation", "case")
    modes = ("keyword", "fulltext")
    languages = ("fr",)
    auth_env = _AUTH_ENV
    auth_help = _AUTH_HELP
    homepage = _LF_SITE
    probe = {"query": "transporteur aérien responsabilité passagers", "kind": "legislation",
             "ref": "LEGITEXT000023086525/L6421-4", "section": "L6421-4", "min_chars": 100}
    coverage = (
        "Official French law in force: the codes (Code des transports, Code civil, Code de la consommation…), "
        "lois/ordonnances/décrets/arrêtés, the Journal officiel, and Conseil d'État / Conseil constitutionnel "
        "decisions (kind=case). Search in French; an article citation ('Code des transports L6421-4') finds "
        "that article. Fetch: an article by LEGIARTI id or 'LEGITEXT…/L6421-4' (the version in force), a whole "
        "code or text by LEGITEXT/JORFTEXT id (optionally '@YYYY-MM-DD'), a decision by CETATEXT/JURITEXT id. "
        "filters: fond (CODE_ETAT|LODA_ETAT|JORF|CETAT|CONSTIT|JURI), code ('Code des transports'), "
        "nature (LOI|DECRET|ARRETE|ORDONNANCE), date_start/date_end."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        self.require_keys()
        filters = filters or {}
        q = " ".join((query or "").split())
        if not q:
            return []
        code_name = str(filters.get("code") or "")
        code = _code_id(code_name or q)
        art = _ART_NUM.search(q)
        hits: list[Hit] = []
        # 1. An article citation: answer it directly with the article in force.
        if art and code:
            num = art.group(1).upper() + art.group(2)
            try:
                a = _piste("POST", f"{_LF}/consult/getArticleWithIdAndNum", api="Légifrance",
                           json_body={"id": code, "num": num}).get("article") or {}
            except ValueError:
                a = {}
            if a.get("id"):
                hits.append(Hit(
                    ref=self.ref(f"{code}/{num}"), title=f"{(a.get('textTitles') or [{}])[0].get('titre') or code_name or 'Code'}, article {num}",
                    source=self.id, kind="legislation", url=f"{_LF_SITE}/codes/article_lc/{a['id']}",
                    date=_ms_date(a.get("dateDebut")), status=_STATUS.get(a.get("etat") or "", a.get("etat")),
                    snippet=_clean(a.get("texte") or a.get("texteHtml"))[:400], citation=f"art. {num}",
                    extra={"article_id": a["id"]},
                ))
        fonds: list[str]
        if filters.get("fond"):
            fonds = [str(filters["fond"]).upper()]
        elif kind == "case":
            fonds = list(_CASE_FONDS)
        elif kind == "legislation":
            fonds = ["CODE_ETAT", "LODA_ETAT"]
        else:
            fonds = ["CODE_ETAT", "LODA_ETAT", "CETAT"]
        champ_type = "ARTICLE" if mode == "fulltext" else "ALL"
        groups: list[list[Hit]] = []
        for fond in fonds[:3]:
            champs = [{"typeChamp": champ_type if fond in ("CODE_ETAT", "LODA_ETAT", "CODE_DATE", "LODA_DATE") else "ALL",
                       "criteres": [_criterion(q)], "operateur": "ET"}]
            filtres: list[dict[str, Any]] = []
            if fond.startswith("CODE") and (code_name or code):
                name = code_name or next((n for n, i in _CODES.items() if i == code), "")
                if name:
                    filtres.append({"facette": "TEXT_NOM_CODE", "valeurs": [name[:1].upper() + name[1:]]})
            if fond == "LODA_ETAT":
                filtres.append({"facette": "TEXT_LEGAL_STATUS", "valeurs": ["VIGUEUR"]})
            if fond in ("CODE_ETAT", "LODA_ETAT"):
                filtres.append({"facette": "ARTICLE_LEGAL_STATUS", "valeurs": ["VIGUEUR"]})
            if filters.get("nature") and fond.startswith(("LODA", "JORF")):
                filtres.append({"facette": "NATURE", "valeurs": [str(filters["nature"]).upper()]})
            if filters.get("date_start") and fond not in ("CODE_ETAT",):
                facet = "DATE_DECISION" if fond in ("JURI", "CETAT", "CONSTIT", "JUFI") else "DATE_PUBLICATION"
                filtres.append({"facette": facet, "dates": {"start": str(filters["date_start"]),
                                                            "end": str(filters.get("date_end") or _today())}})
            data = _lf_search(fond, champs, filtres, limit)
            groups.append(_hits_from_results(self, fond, data, limit))
        i = 0
        while len(hits) < limit and any(i < len(g) for g in groups):
            for g in groups:
                if i < len(g) and len(hits) < limit:
                    hits.append(g[i])
            i += 1
        return hits

    def fetch(self, native_id, *, language=None):
        self.require_keys()
        nid = native_id.strip()
        on_date = None
        if "@" in nid:
            nid, on_date = nid.split("@", 1)
        m = re.match(r"^(LEGITEXT\d{12})/(.+)$", nid)
        if m or re.match(r"^(LEGIARTI|JORFARTI)\d{12}$", nid):
            return self._article(nid, m)
        if not _ID.fullmatch(nid):
            code = _code_id(nid)
            art = _ART_NUM.search(nid)
            if code and art:
                return self._article(f"{code}/{art.group(1).upper()}{art.group(2)}",
                                     re.match(r"^(LEGITEXT\d{12})/(.+)$", f"{code}/{art.group(1).upper()}{art.group(2)}"))
            raise ValueError("Légifrance refs look like LEGIARTI…, 'LEGITEXT…/L6421-4', LEGITEXT…, JORFTEXT…, "
                             "CETATEXT… or JURITEXT… (take them from a legal_search hit)")
        if nid.startswith(("JURITEXT", "CETATEXT", "CONSTEXT", "JUFITEXT")):
            return self._decision(nid)
        date = on_date or _today()
        if nid.startswith("JORFTEXT"):
            resp = _piste("POST", f"{_LF}/consult/jorf", api="Légifrance", json_body={"textCid": nid})
            used = "consult/jorf"
        else:
            resp, used = {}, ""
            try:
                resp = _piste("POST", f"{_LF}/consult/code", api="Légifrance",
                              json_body={"textId": nid, "date": date})
                used = "consult/code"
            except ValueError:
                resp = {}
            if not (resp.get("sections") or resp.get("articles")):
                resp = _piste("POST", f"{_LF}/consult/lawDecree", api="Légifrance",
                              json_body={"textId": nid, "date": date})
                used = "consult/lawDecree"
        text, sections = _render_text(resp)
        meta: dict[str, Any] = {k: v for k, v in {
            "status": _STATUS.get(resp.get("etat") or "", resp.get("etat")),
            "nature": resp.get("nature"), "nor": resp.get("nor"), "eli": resp.get("eli"),
            "version": _ms_date(resp.get("dateDebutVersion")), "valid_until": _ms_date(resp.get("dateFinVersion")),
            "date": _ms_date(resp.get("dateTexte")), "published": _ms_date(resp.get("dateParution")),
            "version_on": date, "endpoint": used, "citation": resp.get("textNumber") or resp.get("nor"),
        }.items() if v}
        if not text.strip():
            meta["no_text_reason"] = "Légifrance returned no articles for this text on that date"
        kind_path = "codes/texte_lc" if used == "consult/code" else "loda/id" if used == "consult/lawDecree" else "jorf/id"
        return LegalDoc(
            ref=self.ref(native_id.strip()), title=_clean(resp.get("title")) or nid, source=self.id, text=text,
            url=f"{_LF_SITE}/{kind_path}/{nid}", kind="legislation", language="fr", metadata=meta,
            sections=sections or None, ttl=24 * 3600,
        )

    def _article(self, nid: str, m: Optional[re.Match[str]]) -> LegalDoc:
        if m:
            text_id, num = m.group(1), m.group(2).strip()
            data = _piste("POST", f"{_LF}/consult/getArticleWithIdAndNum", api="Légifrance",
                          json_body={"id": text_id, "num": num})
        else:
            data = _piste("POST", f"{_LF}/consult/getArticle", api="Légifrance", json_body={"id": nid})
        a = (data or {}).get("article") or {}
        if not a:
            raise ValueError(f"Légifrance has no article {nid!r} in force")
        num = (a.get("num") or "").strip()
        label = f"Article {num}" if num else "Article"
        body = html_to_text(a.get("texteHtml") or "") if a.get("texteHtml") else normalize_lines(a.get("texte") or "")
        text_title = ""
        for t in a.get("textTitles") or []:
            text_title = _clean(t.get("titre") or t.get("titreLong"))
            if text_title:
                break
        context = _clean(a.get("fullSectionsTitre") or a.get("sectionParentTitre"))
        head = f"{label}"
        text = head + "\n" + body
        if a.get("nota") or a.get("notaHtml"):
            text += "\nNOTA : " + (html_to_text(a["notaHtml"]) if a.get("notaHtml") else _clean(a["nota"]))
        versions = a.get("articleVersions") or []
        meta: dict[str, Any] = {k: v for k, v in {
            "status": _STATUS.get(a.get("etat") or "", a.get("etat")),
            "effective": _ms_date(a.get("dateDebut")), "valid_until": _ms_date(a.get("dateFin")),
            "version": a.get("versionArticle"), "versions": len(versions) or None,
            "article_id": a.get("id"), "text_id": a.get("cidTexte") or a.get("idTexte"),
            "section": context or None, "citation": f"art. {num}, {text_title}" if num and text_title else None,
        }.items() if v}
        if versions:
            meta["version_dates"] = [
                f"{_ms_date(v.get('dateDebut')) or '?'} → {_ms_date(v.get('dateFin')) or 'today'} ({v.get('etat')})"
                for v in versions[-6:]
            ]
        sections = [{"label": label, "key": _article_key(num) if num else "", "heading": head,
                     "start": 0, "end": len(text), "level": "article"}] if num else None
        return LegalDoc(
            ref=self.ref(nid), title=f"{text_title or 'Texte'}, article {num}".strip(), source=self.id, text=text,
            url=f"{_LF_SITE}/codes/article_lc/{a.get('id')}" if a.get("id") else _LF_SITE, kind="legislation",
            language="fr", metadata=meta, sections=sections, ttl=24 * 3600,
        )

    def _decision(self, nid: str) -> LegalDoc:
        data = _piste("POST", f"{_LF}/consult/juri", api="Légifrance", json_body={"textId": nid})
        t = (data or {}).get("text") or {}
        body = html_to_text(t.get("texteHtml") or "") if t.get("texteHtml") else normalize_lines(t.get("texte") or "")
        title = _clean(t.get("titre") or t.get("titreLong")) or nid
        meta = {k: v for k, v in {
            "court": t.get("juridiction") or t.get("natureJuridiction"), "date": _ms_date(t.get("dateTexte")),
            "ecli": t.get("ecli"), "number": "; ".join(t.get("numeroAffaire") or []) or t.get("num"),
            "solution": t.get("solution"), "formation": t.get("formation"),
            "citation": t.get("ecli") or None,
        }.items() if v}
        if not body.strip():
            meta["no_text_reason"] = "Légifrance returned no text for this decision"
        return LegalDoc(
            ref=self.ref(nid), title=title, source=self.id, text=body,
            url=f"{_LF_SITE}/{'juri' if nid.startswith('JURI') else 'ceta' if nid.startswith('CETA') else 'cons'}/id/{nid}",
            kind="case", language="fr", metadata=meta, ttl=30 * 24 * 3600,
        )


# ================================================================ Judilibre

_JD = "/cassation/judilibre/v1.0"
_ZONES = (("introduction", "Introduction"), ("expose", "Exposé du litige"), ("moyens", "Moyens"),
          ("motivations", "Motivation"), ("dispositif", "Dispositif"), ("annexes", "Annexes"))


class JudilibreSource(Source):
    id = "fr.judilibre"
    name = "Judilibre (Cour de cassation, via PISTE)"
    jurisdictions = ("FR",)
    kinds = ("case",)
    modes = ("keyword", "fulltext")
    languages = ("fr",)
    auth_env = _AUTH_ENV
    auth_help = _AUTH_HELP
    homepage = "https://www.courdecassation.fr/acces-rapide-judilibre"
    probe = {"query": "transport aérien retard indemnisation règlement 261/2004", "kind": "case", "search_only": True}
    coverage = (
        "Decisions of the Cour de cassation (all since 1947 published; most since 2021) and of the cours "
        "d'appel / tribunaux judiciaires released in open data, full-text search in French. Fetch by the "
        "decision id from a hit: metadata (chamber, pourvoi number, ECLI, solution, visas) and the text cut "
        "at its zones (Exposé, Moyens, Motivation, Dispositif) and numbered paragraphs ('[5]'). filters: "
        "jurisdiction (cc|ca|tj), chamber (civ1|civ2|civ3|comm|soc|cr|mi|pl), date_start/date_end, "
        "publication (b = Bulletin), sort (date)."
    )

    def search(self, query, *, kind="any", mode="keyword", limit=10, filters=None):
        if kind not in ("any", "case"):
            return []
        self.require_keys()
        filters = filters or {}
        q = " ".join((query or "").split())
        if not q:
            return []
        params: dict[str, Any] = {"query": q, "page_size": max(1, min(int(limit), 50)), "page": 0,
                                  "resolve_references": "true", "operator": "and" if mode == "fulltext" else "or"}
        if str(filters.get("sort") or "").lower() == "date":
            params["sort"], params["order"] = "date", "desc"
        else:
            params["sort"] = "score"
        for key in ("jurisdiction", "chamber", "publication", "type", "solution"):
            if filters.get(key):
                v = filters[key]
                params[f"{key}[]"] = v if isinstance(v, list) else [v]
        for key in ("date_start", "date_end"):
            if filters.get(key):
                params[key] = str(filters[key])[:10]
        data = _piste("GET", f"{_JD}/search", api="JUDILIBRE", params=params)
        if not isinstance(data, dict) or "results" not in data:
            raise SourceUnavailable("Judilibre /search answered in an unknown format")
        hits = []
        for r in data.get("results") or []:
            did = r.get("id")
            if not did:
                continue
            hl = r.get("highlights") or {}
            passages = [_clean(x) for v in hl.values() for x in (v if isinstance(v, list) else [v])]
            juris = r.get("jurisdiction") or ""
            chamber = r.get("chamber") or ""
            number = r.get("number") or ", ".join(r.get("numbers") or [])
            title = " ".join(x for x in (str(juris).upper() if juris == "cc" else str(juris), chamber,
                                         r.get("decision_date") or "", f"n° {number}" if number else "") if x)
            hits.append(Hit(
                ref=self.ref(did), title=title or did, source=self.id, kind="case",
                url=f"https://www.courdecassation.fr/decision/{did}", date=r.get("decision_date"),
                status=r.get("solution"), citation=r.get("ecli") or (f"n° {number}" if number else None),
                snippet=(" … ".join(passages[:3]) or _clean(r.get("summary")))[:600],
                extra={k: v for k, v in {"publication": r.get("publication"), "themes": (r.get("themes") or [])[:4]}.items() if v},
            ))
        return hits[:limit]

    def fetch(self, native_id, *, language=None):
        self.require_keys()
        did = native_id.strip()
        d = _piste("GET", f"{_JD}/decision", api="JUDILIBRE", params={"id": did, "resolve_references": "true"})
        if not isinstance(d, dict) or not d.get("id"):
            raise ValueError(f"Judilibre has no decision {did!r}")
        raw = (d.get("text") or "").replace("\r\n", "\n")
        # Paragraph numbers ("5. Selon l'arrêt attaqué…") → "[5]" so the index finds them.
        text = re.sub(r"(?m)^[ \t]*(\d{1,4})\.[ \t]+(?=\S)", r"[\1] ", raw)
        secs = [
            {"label": s.label, "key": s.key, "heading": s.heading, "start": s.start, "end": s.end, "level": s.level}
            for s in detect_sections(text, kind="case")
        ]
        zones = d.get("zones") or {}
        shift = _offset_map(raw, text)
        for key, label in _ZONES:
            for seg in zones.get(key) or []:
                try:
                    a, b = shift(int(seg["start"])), shift(int(seg["end"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if b > a:
                    secs.append({"label": label, "key": "", "heading": label, "start": a, "end": b, "level": "container"})
        secs.sort(key=lambda s: (s["start"], 0 if s["level"] == "container" else 1))
        number = d.get("number") or ", ".join(d.get("numbers") or [])
        meta = {k: v for k, v in {
            "court": d.get("jurisdiction"), "chamber": d.get("chamber"), "date": d.get("decision_date"),
            "number": number, "ecli": d.get("ecli"), "solution": d.get("solution"), "formation": d.get("formation"),
            "publication": d.get("publication"), "summary": _clean(d.get("summary")) or None,
            "visa": [_clean(v.get("title")) for v in d.get("visa") or [] if isinstance(v, dict) and v.get("title")] or None,
            "themes": (d.get("themes") or [])[:8] or None, "citation": d.get("ecli") or (f"n° {number}" if number else None),
        }.items() if v}
        if not text.strip():
            meta["no_text_reason"] = "Judilibre returned no text for this decision"
        title = " ".join(x for x in ("Cour de cassation" if d.get("jurisdiction") == "cc" else str(d.get("jurisdiction") or ""),
                                     str(d.get("chamber") or ""), str(d.get("decision_date") or ""),
                                     f"n° {number}" if number else "") if x)
        return LegalDoc(
            ref=self.ref(did), title=title or did, source=self.id, text=text,
            url=f"https://www.courdecassation.fr/decision/{did}", kind="case", language="fr",
            metadata=meta, sections=secs or None, ttl=30 * 24 * 3600,
        )


def _offset_map(raw: str, new: str):
    """Map an offset in ``raw`` to the same place in ``new`` (only "N. " → "[N] " rewrites differ)."""
    marks: list[tuple[int, int]] = []  # (raw offset, cumulative delta after it)
    delta = 0
    for m in re.finditer(r"(?m)^[ \t]*(\d{1,4})\.[ \t]+(?=\S)", raw):
        replaced = f"[{m.group(1)}] "
        delta += len(replaced) - (m.end() - m.start())
        marks.append((m.end(), delta))

    def shift(off: int) -> int:
        d = 0
        for at, dd in marks:
            if at <= off:
                d = dd
            else:
                break
        return max(0, min(len(new), off + d))

    return shift


SOURCES = [LegifranceSource(), JudilibreSource()]
