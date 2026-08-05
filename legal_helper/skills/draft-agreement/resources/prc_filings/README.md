# PRC filing skeletons (文书生成)

Chinese-only litigation-document skeletons for `/draft-agreement`. Each
`.md` file is one document type, structured after the SPC / 司法部
《民事起诉状、答辩状示范文本（试行）》(2024) and the 民事诉讼法 filing
formalities (起诉条件 第122条; 副本 第123条; 起诉状记明事项 第124条;
答辩期 第128条 — re-verify numbering via `pkulaw_fatiao` every run).

## Files

- `qisuzhuang.md` — 民事起诉状 skeleton.
- `dabianzhuang.md` — 民事答辩状 skeleton.

## Usage rules

- Fill every `【 】` placeholder from the case facts; an unfillable
  placeholder becomes a Finding, never a guess.
- Filings are Chinese-only instruments. A courtesy English translation is
  a separate, clearly-labelled non-filing artifact
  (`references/methodology.md` §3).
- The legal-basis paragraph cites only pinpoints verified per the
  clause-basis contract in `SKILL.md`; unverifiable → rephrase without
  the article number, and flag.
- 诉讼请求 must be numbered and executable (methodology §5); interest,
  costs and preservation requests are separate numbered items.
- Keep the closing block intact: 受诉法院、具状人签名/盖章、日期、附项
  (副本份数按被告人数, 证据清单).
