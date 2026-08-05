# GB/T 9704-style 公文 memo template

Render with `write_docx_document(..., layout="gbt9704")` (or the `write_docx`
tool once the `layout` parameter is threaded through). The layout applies:

- Page: A4 with 天头 3.7cm / 地脚 3.5cm / 订口(左) 2.8cm / 翻口(右) 2.6cm.
- Body: 仿宋-class 三号 (16pt), fixed ~28pt line pitch, 首行缩进 2 字符,
  black text throughout (Word/LibreOffice substitute Noto CJK SC when
  仿宋/黑体/楷体 are not installed).
- Headings: 一级标题 黑体, 二级标题 楷体, 三级及以下 仿宋加粗 — all 三号.
- Title: 宋体-class, bold, 二号 (22pt), centered, no first-line indent.

This is a *-style* layout for internal legal memos (请示/报告/法律意见书);
formal red-header 发文 (红头文件) additionally needs the issuing organ's
版头, 发文字号, and 印章, which are organization-specific and out of scope.

## Skeleton (sections for `write_docx_document`)

Title: `关于××××的法律意见书` (or 请示/报告)

```
（主送机关/委托人名称）：

    受……委托，就……事项，本所/本部门依据现行有效的法律、行政法规、
部门规章及规范性文件，出具本法律意见书。

## 一、事实背景

    （简述委托事项、已核查文件清单、核查范围与假设。）

## 二、法律依据

    （逐项列明：法律 → 行政法规 → 部门规章 → 规范性文件 → 司法解释 →
指导案例，每项给出条款级引用。）

## 三、法律分析

    （按争点分节：三级标题用"（一）（二）"式；每一争点给出
规则 → 涵摄 → 结论。）

## 四、结论与建议

    （明确、可执行；标注前提与保留。）

## 附件说明

    附件1：……
    附件2：……

××××律师事务所 / ××公司法务部
××××年××月××日
```

## 资料来源要求

正文后必须保留 `## 资料来源` 一节，逐条给出条款级 pinpoint（第×条第×款
第×项）；无法定位时明确写"无法获得具体条款"。
