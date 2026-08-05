import { describe, it, expect } from "vitest";
import { splitSources, parseSourcesTable } from "../../../src/components/parts/SourcesList";

// Real markdown captured from a live turn (民法典 合同解除).
const ANSWER = `通常所称的**合同法定解除三种情形**，是对《民法典》第五百六十三条的归纳：

1. 因不可抗力…

## 资料来源与核验

| # | 主张/Claim | 依据/Pinpoint (with URL) | 在线核验/Online-checked (✔ / 未核验 / pinpoint unavailable) | 备注/Note |
|---|---|---|---|---|
| 1 | 《民法典》第五百六十三条规定… | [《中华人民共和国民法典》全文（最高人民法院）](https://www.court.gov.cn/zixun/xiangqing/233181.html)，第五百六十三条第一款第一项至第五项 | ✔ | 条文共列五项 |
| 2 | 约定解除与法定解除属于不同制度 | [《中华人民共和国民法典》全文（最高人民法院）](https://www.court.gov.cn/zixun/xiangqing/233181.html)，第五百六十二条、第五百六十三条 | 未核验 | 说明 |
`;

describe("SourcesList parser", () => {
  it("splits body from the 资料来源 section", () => {
    const { body, sources } = splitSources(ANSWER);
    expect(body).toContain("合同法定解除三种情形");
    expect(body).not.toContain("| # |");
    expect(sources).toContain("| 1 |");
  });

  it("parses the sources table into list rows (not a table)", () => {
    const { sources } = splitSources(ANSWER);
    const rows = parseSourcesTable(sources!);
    expect(rows).toHaveLength(2);
    expect(rows![0].claim).toContain("民法典");
    expect(rows![0].pinpointHref).toBe("https://www.court.gov.cn/zixun/xiangqing/233181.html");
    expect(rows![0].pinpoint).toContain("第五百六十三条");
    expect(rows![0].status).toBe("ok"); // ✔
    expect(rows![1].status).toBe("warn"); // 未核验
  });

  it("returns null for non-source tables (leaves them as markdown)", () => {
    const rows = parseSourcesTable("| A | B |\n|---|---|\n| 1 | 2 |");
    expect(rows).toBeNull();
  });

  it("leaves answers without a sources section untouched", () => {
    const { body, sources } = splitSources("just an answer, no sources.");
    expect(body).toBe("just an answer, no sources.");
    expect(sources).toBeUndefined();
  });
});
