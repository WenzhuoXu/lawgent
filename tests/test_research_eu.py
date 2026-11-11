"""EUR-Lex citation → CELEX resolution (eu.eurlex), offline."""

from legal_helper.research.sources._eurlex import _candidate_celex_ids


def test_citations_resolve_to_celex():
    cases = {
        "Regulation (EC) No 261/2004": "32004R0261",
        "Regulation (EC) No 2027/97": "31997R2027",  # two-digit year: 1997, not 2027
        "Regulation (EEC) No 95/93": "31993R0095",
        "Directive 95/46/EC": "31995L0046",
        "Directive 2004/82/EC": "32004L0082",
        "Regulation (EU) 2016/679": "32016R0679",
        "Regulation (EU) 2026/2202": "32026R2202",
        "C-436/21": "62021CJ0436",
        "02004R0261-20050217": "02004R0261-20050217",  # consolidated version
        "52024XC05687": "52024XC05687",
    }
    for citation, celex in cases.items():
        assert _candidate_celex_ids(citation)[0] == celex, citation
